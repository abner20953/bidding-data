"""腾讯云 OCR 调用封装：按页、可缓存、失败不抛出到评审主流程。"""

from __future__ import annotations

import base64
import json
import threading
from typing import Any

from dashboard.evaluation_workbench import storage


_OCR_REQUEST_GATE = threading.BoundedSemaphore(1)


def _flatten_text(value: object, values: list[str]) -> None:
    if isinstance(value, dict):
        for key, item in value.items():
            if key in {
                "RequestId", "Confidence", "Polygon", "ItemPolygon", "Coord", "Type", "Angle",
                "ColBr", "ColTl", "RowBr", "RowTl", "PdfPageSize", "Data",
            }:
                continue
            _flatten_text(item, values)
    elif isinstance(value, list):
        for item in value:
            _flatten_text(item, values)
    elif isinstance(value, (str, int, float)) and str(value).strip():
        text = str(value).strip()
        if text not in values:
            values.append(text)


def _result_from_response(service: str, response: object) -> dict:
    try:
        payload = json.loads(response.to_json_string())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("腾讯 OCR 未返回可解析结果") from exc
    body = payload.get("Response") if isinstance(payload, dict) else {}
    body = body if isinstance(body, dict) else {}
    lines: list[str] = []
    confidences: list[float] = []
    for item in body.get("TextDetections") or []:
        if not isinstance(item, dict):
            continue
        text = str(item.get("DetectedText") or "").strip()
        if text:
            lines.append(text)
        try:
            confidences.append(float(item.get("Confidence")))
        except (TypeError, ValueError):
            pass
    if not lines:
        # 表格、营业执照等专用接口的结构不同；保留全部有意义字段供后续文本模型判断。
        _flatten_text(body, lines)
    return {
        "service": service,
        "text": "\n".join(lines)[:12000],
        "line_count": len(lines),
        "confidence": round(sum(confidences) / len(confidences), 1) if confidences else None,
        "request_id": str(body.get("RequestId") or ""),
    }


def request_tencent_ocr(app, task: dict, service: str, image_bytes: bytes) -> tuple[dict | None, str]:
    """执行一次OCR；所有服务端/鉴权异常都转换为可回退状态。"""
    if service not in storage.TENCENT_OCR_SERVICES:
        return None, "不支持的 OCR 接口"
    usage_id = storage.reserve_ocr_request(app, task, service)
    if not usage_id:
        return None, "OCR额度不足、接口未启用或未配置凭据"
    try:
        credentials = storage.tencent_ocr_credentials(app)
    except ValueError as exc:
        storage.complete_ocr_request(app, usage_id, status="unavailable", detail="凭据解密失败")
        return None, f"腾讯 OCR 凭据不可用：{str(exc)[:160]}"
    if not credentials:
        storage.complete_ocr_request(app, usage_id, status="unavailable", detail="未配置凭据")
        return None, "未配置腾讯 OCR 凭据"
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.ocr.v20181119 import models, ocr_client
    except ImportError:
        storage.complete_ocr_request(app, usage_id, status="unavailable", detail="OCR SDK 未安装")
        return None, "腾讯 OCR SDK 未安装"
    secret_id, secret_key, region = credentials
    image_base64 = base64.b64encode(image_bytes).decode("ascii")
    try:
        request_class = getattr(models, f"{storage.TENCENT_OCR_SERVICES[service]['action']}Request")
        request = request_class()
        request.from_json_string(json.dumps({"ImageBase64": image_base64}, ensure_ascii=False))
        with _OCR_REQUEST_GATE:
            client = ocr_client.OcrClient(credential.Credential(secret_id, secret_key), region)
            response = getattr(client, storage.TENCENT_OCR_SERVICES[service]["action"])(request)
        value = _result_from_response(service, response)
        storage.complete_ocr_request(app, usage_id, status="success", request_id=value.get("request_id", ""))
        return value, ""
    except TencentCloudSDKException as exc:
        request_id = str(getattr(exc, "request_id", "") or "")
        storage.complete_ocr_request(app, usage_id, status="error", request_id=request_id, detail=str(exc))
        return None, f"腾讯 OCR 调用失败：{str(exc)[:200]}"
    except Exception as exc:  # noqa: BLE001 - 外部SDK不可让评审任务失败
        storage.complete_ocr_request(app, usage_id, status="error", detail=str(exc))
        return None, f"腾讯 OCR 调用失败：{str(exc)[:200]}"
