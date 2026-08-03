"""按需调用 RapidOCR；不在主服务中导入 ONNX Runtime。

默认使用常驻单例 worker 子进程（``--serve`` 行协议）：引擎只加载一次，之后
各批页面复用同一进程，消除每批数秒的模型加载开销。常驻进程数量与空闲回收
受环境变量约束；任何启动/通信/超时异常都会自动回退到逐批短进程模式，行为
与旧版本完全一致。
"""

from __future__ import annotations

import atexit
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path


LOCAL_OCR_SERVICE = "rapidocr_local"
LOCAL_OCR_PARSER_VERSION = 1
_LOCAL_OCR_TIMEOUT_BASE_SECONDS = 25
_LOCAL_OCR_TIMEOUT_PER_PAGE_SECONDS = 12
# 常驻 worker 首个批次包含 ONNX 引擎加载，需要额外宽限；热进程批处理不再加。
_SERVE_FIRST_BATCH_GRACE_SECONDS = 30
# 空闲回收：综合评审结束后不再白占数百 MB 内存；下次请求时按需重新拉起。
_SERVE_IDLE_SECONDS = 300
_SERVE_REAPER_INTERVAL_SECONDS = 30
_SERVE_ACQUIRE_WAIT_SECONDS = 5


def local_ocr_max_workers() -> int:
    """本地 OCR 允许的并行子进程数。

    每个 RapidOCR/ONNX 子进程峰值约 500-650 MB，2 核 2 GB 服务器必须保持 1；
    升级到 4 GB 以上内存后可用 LOCAL_OCR_MAX_WORKERS=2 放开一路，上限 4 防止
    误配把服务器拖垮。
    """
    try:
        requested = int(os.environ.get("LOCAL_OCR_MAX_WORKERS", "1"))
    except (TypeError, ValueError):
        return 1
    return max(1, min(4, requested))


def _persistent_enabled() -> bool:
    """常驻 worker 默认开启；RAPIDOCR_WORKER_MODE=oneshot 可临时回到逐批短进程。"""
    return str(os.environ.get("RAPIDOCR_WORKER_MODE") or "").strip().lower() != "oneshot"


def _model_home() -> str:
    """返回仅供 OCR 子进程使用的模型缓存目录。

    生产镜像在构建期创建并预热 ``/app/model_data/rapidocr``。本地开发环境
    通常没有该目录，回退到当前用户的缓存路径，避免把无效的 ``/app`` 写入
    子进程 HOME；主服务的 HOME 始终不受影响。
    """
    configured = str(os.environ.get("RAPIDOCR_MODEL_HOME") or "").strip()
    if configured:
        return configured
    container_home = Path("/app/model_data/rapidocr")
    if container_home.is_dir():
        return str(container_home)
    return str(Path.home() / ".cache" / "evaluation-workbench-rapidocr")


def _error(kind: str, message: object) -> dict:
    return {"kind": kind, "message": str(message or "本地 OCR 调用失败")[:300]}


def _ocr_numeric_thread_count() -> str:
    """OCR 子进程数值库线程数。

    默认 1：多份投标文件并行评审时避免 OCR 抢占 2 核 CPU。单路 OCR 且其余环节
    多在等待网络时，可用 RAPIDOCR_OMP_THREADS=2 让 ONNX 推理用满双核提速。
    """
    try:
        requested = int(os.environ.get("RAPIDOCR_OMP_THREADS", "1"))
    except (TypeError, ValueError):
        requested = 1
    return str(max(1, min(4, requested)))


def _runtime_env() -> dict[str, str]:
    """限制子进程的数值库线程，避免 2 核服务器被 OCR 抢占。"""
    value = os.environ.copy()
    threads = _ocr_numeric_thread_count()
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        value[key] = threads
    # 仅子进程使用预下载模型缓存，不能把 HOME 的副作用扩散到 Gunicorn 或其他依赖。
    value["HOME"] = _model_home()
    value["PYTHONUNBUFFERED"] = "1"
    return value


def _default_serve_command() -> list[str]:
    return [sys.executable, "-m", "dashboard.evaluation_workbench.rapidocr_worker", "--serve"]


def _read_line_with_timeout(stream, timeout: float):
    """用守护线程实现跨平台的 stdout 行读取超时；超时返回 None，由调用方杀进程。"""
    result: dict = {}

    def target() -> None:
        try:
            result["line"] = stream.readline()
        except (OSError, ValueError) as exc:  # 进程已死或流被关闭
            result["error"] = exc

    reader = threading.Thread(target=target, daemon=True)
    reader.start()
    reader.join(timeout)
    if reader.is_alive():
        return None
    return result.get("line")


class _ServeWorker:
    """单个常驻 OCR 子进程；协议为逐行 JSON 请求/响应。"""

    def __init__(self, command: list[str]):
        self.command = list(command)
        self.proc: subprocess.Popen | None = None
        self.busy = False
        self.served_batches = 0
        self.last_used = time.monotonic()

    def is_alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def idle_seconds(self) -> float:
        return time.monotonic() - self.last_used

    def request(self, inputs: list[dict], timeout: float) -> dict:
        """向常驻进程发送一批页面；任何异常都抛出，由池决定销毁与回退。"""
        if not self.is_alive():
            self.proc = subprocess.Popen(
                self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                cwd=str(Path(__file__).resolve().parents[2]), env=_runtime_env(),
            )
            self.served_batches = 0
        assert self.proc is not None and self.proc.stdin is not None and self.proc.stdout is not None
        effective_timeout = timeout + (_SERVE_FIRST_BATCH_GRACE_SECONDS if self.served_batches == 0 else 0)
        self.proc.stdin.write(json.dumps({"pages": inputs}, ensure_ascii=False) + "\n")
        self.proc.stdin.flush()
        line = _read_line_with_timeout(self.proc.stdout, effective_timeout)
        self.last_used = time.monotonic()
        if line is None:
            raise TimeoutError("常驻 RapidOCR 识别超时")
        payload = {}
        for candidate_line in reversed(str(line).splitlines()):
            try:
                candidate = json.loads(candidate_line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if not isinstance(payload, dict) or not payload:
            raise RuntimeError("常驻 RapidOCR 未返回有效结果")
        self.served_batches += 1
        return payload

    def close(self) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            if proc.stdin:
                proc.stdin.close()
        except OSError:
            pass
        try:
            proc.terminate()
            proc.wait(timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            try:
                proc.kill()
            except OSError:
                pass


class _ServePool:
    """常驻 worker 池；大小受 local_ocr_max_workers 约束，空闲自动回收。"""

    def __init__(self, command_factory=_default_serve_command):
        self._command_factory = command_factory
        self._workers: list[_ServeWorker] = []
        self._cond = threading.Condition()

    def acquire(self) -> _ServeWorker | None:
        deadline = time.monotonic() + _SERVE_ACQUIRE_WAIT_SECONDS
        with self._cond:
            while True:
                for worker in self._workers:
                    if not worker.busy and not worker.is_alive():
                        # 已死进程先清理，避免占住池位。
                        self._workers.remove(worker)
                        worker.close()
                        continue
                    if not worker.busy:
                        worker.busy = True
                        return worker
                if len(self._workers) < local_ocr_max_workers():
                    worker = _ServeWorker(self._command_factory())
                    worker.busy = True
                    self._workers.append(worker)
                    return worker
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._cond.wait(timeout=remaining)

    def release(self, worker: _ServeWorker, *, broken: bool = False) -> None:
        with self._cond:
            if broken and worker in self._workers:
                self._workers.remove(worker)
                worker.close()
            worker.busy = False
            self._cond.notify()

    def reap_idle(self) -> None:
        with self._cond:
            for worker in list(self._workers):
                if not worker.busy and worker.is_alive() and worker.idle_seconds() > _SERVE_IDLE_SECONDS:
                    self._workers.remove(worker)
                    worker.close()

    def shutdown(self) -> None:
        with self._cond:
            workers, self._workers = self._workers, []
        for worker in workers:
            worker.close()


_POOL_LOCK = threading.Lock()
_POOL: _ServePool | None = None
_REAPER_STARTED = False


def _pool() -> _ServePool:
    global _POOL, _REAPER_STARTED
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = _ServePool()
            atexit.register(_POOL.shutdown)
        if not _REAPER_STARTED:
            _REAPER_STARTED = True

            def reap_loop() -> None:
                while True:
                    time.sleep(_SERVE_REAPER_INTERVAL_SECONDS)
                    try:
                        pool = _POOL
                        if pool is not None:
                            pool.reap_idle()
                    except Exception:  # noqa: BLE001 - 回收线程绝不拖垮主服务
                        pass

            threading.Thread(target=reap_loop, daemon=True, name="rapidocr-serve-reaper").start()
        return _POOL


def _serve_request(inputs: list[dict], timeout: float) -> dict | None:
    """经常驻池执行一批页面；任何失败返回 None，调用方回退逐批短进程。"""
    try:
        worker = _pool().acquire()
    except Exception:  # noqa: BLE001 - 池异常不应阻断 OCR，回退短进程
        return None
    if worker is None:
        return None
    broken = False
    try:
        return worker.request(inputs, timeout)
    except Exception:  # noqa: BLE001 - 进程死亡/超时/协议错误统一回退
        broken = True
        return None
    finally:
        try:
            _pool().release(worker, broken=broken)
        except Exception:  # noqa: BLE001
            pass


def request_local_ocr(pages: list[dict], *, metrics: dict | None = None) -> tuple[list[dict], dict | None]:
    """识别一批临时 JPEG；无常驻模型、无网络请求。

    优先复用常驻 worker 进程（引擎常驻、逐批喂页）；常驻路径不可用时回退到
    逐批短进程。每个 page 仅允许 ``page`` 和调用方创建的临时 ``path``；结果
    只回传文字与置信度；调用方负责用 TemporaryDirectory 清理。
    """
    started = time.perf_counter()
    telemetry = metrics if isinstance(metrics, dict) else {}
    telemetry.clear()

    def finish_metrics(status: str, error_kind: str = "", child: object = None) -> None:
        value = child if isinstance(child, dict) else {}
        telemetry.update({
            "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
            "peak_rss_kb": value.get("peak_rss_kb"),
            "status": status,
            "error_kind": error_kind,
            "model": str(value.get("model") or "PP-OCRv5-mobile-onnx")[:80],
            "limit_side_len": value.get("limit_side_len"),
        })

    if not pages:
        finish_metrics("error", "input")
        return [], _error("input", "未提供本地 OCR 页面")
    inputs = []
    for item in pages:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        path = Path(str(item.get("path") or ""))
        if page <= 0 or not path.is_file():
            continue
        inputs.append({"page": page, "path": str(path)})
    if not inputs:
        finish_metrics("error", "input")
        return [], _error("input", "未获得可识别的本地 OCR 图片")

    timeout = _LOCAL_OCR_TIMEOUT_BASE_SECONDS + _LOCAL_OCR_TIMEOUT_PER_PAGE_SECONDS * len(inputs)
    payload = _serve_request(inputs, timeout) if _persistent_enabled() else None
    if payload is None:
        try:
            completed = subprocess.run(
                [sys.executable, "-m", "dashboard.evaluation_workbench.rapidocr_worker"],
                input=json.dumps({"pages": inputs}, ensure_ascii=False), text=True, encoding="utf-8",
                capture_output=True, cwd=str(Path(__file__).resolve().parents[2]), env=_runtime_env(),
                timeout=timeout,
                check=False,
            )
        except subprocess.TimeoutExpired:
            finish_metrics("timeout", "timeout")
            return [], _error("timeout", "本地 RapidOCR 识别超时，已自动释放子进程")
        except OSError as exc:
            finish_metrics("unavailable", "unavailable")
            return [], _error("unavailable", exc)
        payload = {}
        # 第三方推理库可能在初始化时输出 INFO；只把最后一条合法 JSON 视为子进程协议。
        for line in reversed(str(completed.stdout or "").splitlines()):
            try:
                candidate = json.loads(line)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if completed.returncode != 0 or not isinstance(payload, dict) or not payload.get("ok"):
            detail = payload.get("error") if isinstance(payload, dict) else ""
            finish_metrics("unavailable", "unavailable", payload.get("metrics") if isinstance(payload, dict) else None)
            return [], _error("unavailable", detail or completed.stderr or "RapidOCR 子进程未返回有效结果")
    if not isinstance(payload, dict) or not payload.get("ok"):
        detail = payload.get("error") if isinstance(payload, dict) else ""
        finish_metrics("unavailable", "unavailable", (payload or {}).get("metrics") if isinstance(payload, dict) else None)
        return [], _error("unavailable", detail or "RapidOCR 子进程未返回有效结果")
    # 保留每个页面的结果状态，不能把“空白/单页失败”悄悄丢掉。调用方据此
    # 决定是否只能形成部分 OCR 结论；这对腾讯关闭后的本地直连路径尤其重要。
    values: list[dict] = []
    for item in payload.get("pages") or []:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        if page <= 0:
            continue
        page_error = str(item.get("error") or "").strip()
        if page_error:
            values.append({
                "service": LOCAL_OCR_SERVICE, "page": page, "state": "failed",
                "error": page_error[:240], "parser_version": LOCAL_OCR_PARSER_VERSION,
            })
            continue
        text = str(item.get("text") or "").strip()
        if not text:
            values.append({
                "service": LOCAL_OCR_SERVICE, "page": page, "state": "empty",
                "line_count": int(item.get("line_count") or 0), "confidence": item.get("confidence"),
                "parser_version": LOCAL_OCR_PARSER_VERSION,
            })
            continue
        values.append({
            "service": LOCAL_OCR_SERVICE,
            "text": text[:12000],
            "line_count": int(item.get("line_count") or 0),
            "confidence": item.get("confidence"),
            "parser_version": LOCAL_OCR_PARSER_VERSION,
            "page": page,
            "state": "recognized",
        })
    failed = sum(1 for item in values if item.get("state") == "failed")
    recognized = sum(1 for item in values if item.get("state") == "recognized")
    finish_metrics("partial" if failed else "success", "page_error" if failed else "", payload.get("metrics"))
    telemetry.update({"recognized_pages": recognized, "failed_pages": failed,
                      "empty_pages": sum(1 for item in values if item.get("state") == "empty")})
    return values, None
