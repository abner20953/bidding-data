"""按需调用 RapidOCR；不在主服务中导入 ONNX Runtime。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


LOCAL_OCR_SERVICE = "rapidocr_local"
LOCAL_OCR_PARSER_VERSION = 1
_LOCAL_OCR_TIMEOUT_BASE_SECONDS = 25
_LOCAL_OCR_TIMEOUT_PER_PAGE_SECONDS = 12


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


def _runtime_env() -> dict[str, str]:
    """限制子进程的数值库线程，避免 2 核服务器被 OCR 抢占。"""
    value = os.environ.copy()
    for key in ("OMP_NUM_THREADS", "OPENBLAS_NUM_THREADS", "MKL_NUM_THREADS", "NUMEXPR_NUM_THREADS"):
        value[key] = "1"
    # 仅子进程使用预下载模型缓存，不能把 HOME 的副作用扩散到 Gunicorn 或其他依赖。
    value["HOME"] = _model_home()
    value["PYTHONUNBUFFERED"] = "1"
    return value


def request_local_ocr(pages: list[dict]) -> tuple[list[dict], dict | None]:
    """一次短进程识别一批临时 JPEG；无常驻模型、无网络请求。

    每个 page 仅允许 ``page`` 和调用方创建的临时 ``path``。子进程只收到这些
    临时文件路径，结果只回传文字与置信度；调用方负责用 TemporaryDirectory 清理。
    """
    if not pages:
        return [], _error("input", "未提供本地 OCR 页面")
    try:
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
            return [], _error("input", "未获得可识别的本地 OCR 图片")
        completed = subprocess.run(
            [sys.executable, "-m", "dashboard.evaluation_workbench.rapidocr_worker"],
            input=json.dumps({"pages": inputs}, ensure_ascii=False), text=True, encoding="utf-8",
            capture_output=True, cwd=str(Path(__file__).resolve().parents[2]), env=_runtime_env(),
            timeout=_LOCAL_OCR_TIMEOUT_BASE_SECONDS + _LOCAL_OCR_TIMEOUT_PER_PAGE_SECONDS * len(inputs),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return [], _error("timeout", "本地 RapidOCR 识别超时，已自动释放子进程")
    except OSError as exc:
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
        return [], _error("unavailable", detail or completed.stderr or "RapidOCR 子进程未返回有效结果")
    values: list[dict] = []
    for item in payload.get("pages") or []:
        if not isinstance(item, dict):
            continue
        try:
            page = int(item.get("page") or 0)
        except (TypeError, ValueError):
            page = 0
        text = str(item.get("text") or "").strip()
        if page <= 0 or not text:
            continue
        values.append({
            "service": LOCAL_OCR_SERVICE,
            "text": text[:12000],
            "line_count": int(item.get("line_count") or 0),
            "confidence": item.get("confidence"),
            "parser_version": LOCAL_OCR_PARSER_VERSION,
            "page": page,
        })
    return values, None
