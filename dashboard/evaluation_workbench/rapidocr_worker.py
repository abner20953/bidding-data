"""短生命周期的 RapidOCR 子进程。

此模块绝不能被 Web 服务或评审 worker 顶层导入。它只由 local_ocr_gateway
以独立进程按批启动，进程退出后 ONNX Runtime 与模型内存随之释放。
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

try:  # Linux 容器提供 resource；Windows 本地开发环境安全退化为未知。
    import resource
except ImportError:  # pragma: no cover - 仅 Windows
    resource = None


def _detector_limit_side_len() -> int:
    """取得受限的检测长边，保护 2 核 2 GB 运行环境。"""
    try:
        requested = int(os.environ.get("RAPIDOCR_LIMIT_SIDE_LEN", "960"))
    except (TypeError, ValueError):
        requested = 960
    return max(640, min(1280, requested))


def _engine():
    from rapidocr import EngineType, LangDet, LangRec, ModelType, OCRVersion, RapidOCR

    # 明确锁定 PP-OCRv5 mobile + ONNX CPU，避免 RapidOCR 版本升级后默认模型变化。
    limit_side_len = _detector_limit_side_len()
    return RapidOCR(params={
        "Det.engine_type": EngineType.ONNXRUNTIME,
        "Det.lang_type": LangDet.CH,
        "Det.model_type": ModelType.MOBILE,
        "Det.ocr_version": OCRVersion.PPOCRV5,
        # 2GB 服务器默认限制为 960；确有小字扫描件且云端验收内存稳定时，
        # 可通过 RAPIDOCR_LIMIT_SIDE_LEN 在 640-1280 间调整，毋须改代码。
        "Det.limit_side_len": limit_side_len,
        "Det.limit_type": "max",
        "Rec.engine_type": EngineType.ONNXRUNTIME,
        "Rec.lang_type": LangRec.CH,
        "Rec.model_type": ModelType.MOBILE,
        "Rec.ocr_version": OCRVersion.PPOCRV5,
    })


def _result_for_page(engine, item: dict) -> dict:
    page = int(item.get("page") or 0)
    path = Path(str(item.get("path") or ""))
    if page <= 0 or not path.is_file():
        return {"page": page, "error": "本地 OCR 输入页面无效"}
    try:
        # PDF 渲染页方向通常已正确；跳过分类模型可降低 CPU 和内存峰值。
        output = engine(str(path), use_cls=False)
        lines = [str(value).strip() for value in (getattr(output, "txts", None) or []) if str(value).strip()]
        scores = []
        for value in getattr(output, "scores", None) or []:
            try:
                scores.append(float(value))
            except (TypeError, ValueError):
                pass
        return {
            "page": page,
            "text": "\n".join(lines)[:12000],
            "line_count": len(lines),
            "confidence": round(sum(scores) / len(scores) * 100, 1) if scores else None,
            "elapsed_seconds": round(float(getattr(output, "elapse", 0) or 0), 3),
        }
    except Exception as exc:  # noqa: BLE001 - 外部模型异常必须可回退
        return {"page": page, "error": str(exc)[:240]}


def main() -> int:
    started = time.perf_counter()
    try:
        if "--warmup" in sys.argv[1:]:
            _engine()
            print(json.dumps({"ok": True, "warmup": True}, ensure_ascii=False), flush=True)
            return 0
        payload = json.load(sys.stdin)
        pages = payload.get("pages") if isinstance(payload, dict) else []
        if not isinstance(pages, list) or not pages:
            raise ValueError("未提供本地 OCR 页面")
        engine = _engine()
        values = [_result_for_page(engine, item) for item in pages if isinstance(item, dict)]
        peak_rss_kb = None
        if resource is not None:
            try:
                peak_rss_kb = max(0, int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss))
            except (AttributeError, OSError, ValueError):
                peak_rss_kb = None
        print(json.dumps({
            "ok": True, "pages": values,
            "metrics": {
                "elapsed_ms": max(0, int((time.perf_counter() - started) * 1000)),
                "peak_rss_kb": peak_rss_kb,
                "model": "PP-OCRv5-mobile-onnx",
                "limit_side_len": _detector_limit_side_len(),
            },
        }, ensure_ascii=False), flush=True)
        return 0
    except Exception as exc:  # noqa: BLE001 - 调用方会转为回退状态
        print(json.dumps({"ok": False, "error": str(exc)[:400]}, ensure_ascii=False), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
