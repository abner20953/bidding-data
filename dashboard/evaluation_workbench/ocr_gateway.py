"""腾讯云 OCR 调用封装：按页、可缓存、失败不抛出到评审主流程。"""

from __future__ import annotations

import base64
import json
import threading

from dashboard.evaluation_workbench import storage


_OCR_REQUEST_GATE = threading.BoundedSemaphore(1)
OCR_PARSER_VERSION = 3

_BIZ_LICENSE_FIELD_LABELS = {
    "RegNum": "统一社会信用代码",
    "Name": "企业名称",
    "Capital": "注册资本",
    "Person": "法定代表人",
    "Address": "住所",
    "Business": "经营范围",
    "SetDate": "成立日期",
    "Period": "营业期限",
    "ComposingForm": "组成形式",
    "Type": "主体类型",
    "NationalEmblem": "国徽是否可见",
    "Electronic": "是否电子营业执照",
    "IsDuplication": "复印件告警",
    "RecognizeWarnCode": "识别告警码",
    "RecognizeWarnMsg": "识别告警",
}


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


def _table_text(body: dict) -> list[str]:
    """保留表格单元格的行列关系，避免只拍平文字后丢失评分表语义。"""
    values: list[str] = []
    for table_index, table in enumerate(body.get("TableDetections") or [], start=1):
        if not isinstance(table, dict):
            continue
        cells = []
        for cell in table.get("Cells") or []:
            if not isinstance(cell, dict):
                continue
            text = str(cell.get("Text") or "").strip()
            if not text:
                continue
            try:
                row = int(cell.get("RowTl") or 0)
            except (TypeError, ValueError):
                row = 0
            try:
                column = int(cell.get("ColTl") or 0)
            except (TypeError, ValueError):
                column = 0
            cells.append((row, column, text))
        for row, column, text in sorted(cells, key=lambda item: (item[0], item[1])):
            values.append(f"表{table_index}·第{row + 1}行第{column + 1}列：{text}")
    return values


def _biz_license_text(body: dict) -> list[str]:
    """营业执照接口返回结构化字段；字段名必须与字段值一起交给后续模型。"""
    values: list[str] = []
    for key, label in _BIZ_LICENSE_FIELD_LABELS.items():
        value = body.get(key)
        if value is None or (isinstance(value, str) and not value.strip()):
            continue
        values.append(f"{label}：{str(value).strip()}")
    return values


def _result_from_response(service: str, response: object) -> dict:
    try:
        payload = json.loads(response.to_json_string())
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError("腾讯 OCR 未返回可解析结果") from exc
    # SDK 的 response.to_json_string() 通常直接返回业务字段；API Explorer/HTTP 示例
    # 才常见 {"Response": {...}} 外层。两种结构都必须兼容。
    body = payload.get("Response") if isinstance(payload, dict) and isinstance(payload.get("Response"), dict) else payload
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
        # 专用接口必须保留结构语义：营业执照保留字段名，表格保留单元格行列。
        if service == "table":
            lines.extend(_table_text(body))
        elif service == "biz_license":
            lines.extend(_biz_license_text(body))
        if not lines:
            _flatten_text(body, lines)
    return {
        "service": service,
        "text": "\n".join(lines)[:12000],
        "line_count": len(lines),
        "confidence": round(sum(confidences) / len(confidences), 1) if confidences else None,
        "request_id": str(body.get("RequestId") or ""),
        "parser_version": OCR_PARSER_VERSION,
    }


def _ocr_error(kind: str, message: object, *, code: str = "", retryable: bool = False) -> dict:
    return {
        "kind": kind,
        "code": str(code or "")[:80],
        "message": str(message or "腾讯 OCR 调用失败")[:240],
        "retryable": bool(retryable),
    }


def _classify_ocr_exception(exc: Exception) -> dict:
    """区分鉴权/额度/临时故障与单页失败，避免错误地禁用后续可靠页面。"""
    raw_code = getattr(exc, "code", "")
    if not raw_code:
        getter = getattr(exc, "get_code", None)
        try:
            raw_code = getter() if callable(getter) else ""
        except Exception:  # noqa: BLE001 - 分类失败不能穿透到评审主流程
            raw_code = ""
    code = str(raw_code or "")
    message = str(exc)
    normalized = f"{code} {message}".lower()
    if any(token in normalized for token in ("authfailure", "unauthorized", "signature", "secretid", "secretkey")):
        return _ocr_error("auth", message, code=code)
    if any(token in normalized for token in ("requestlimit", "internalerror", "resourceunavailable", "timeout", "network", "overload")):
        return _ocr_error("transient", message, code=code, retryable=True)
    if any(token in normalized for token in ("limitexceeded", "quota", "resourceinsufficient", "resource not enough")):
        return _ocr_error("quota", message, code=code)
    if any(token in normalized for token in ("unsupported", "not support", "unavailable operation", "invalidaction")):
        return _ocr_error("unsupported", message, code=code)
    if any(token in normalized for token in ("invalidparameter", "image", "picture", "base64", "too large")):
        return _ocr_error("page", message, code=code)
    return _ocr_error("unknown", message, code=code)


def request_tencent_ocr(app, task: dict, service: str, image_bytes: bytes) -> tuple[dict | None, dict]:
    """执行一次OCR；所有服务端/鉴权异常都转换为可回退状态。"""
    if service not in storage.TENCENT_OCR_SERVICES:
        return None, _ocr_error("unsupported", "不支持的 OCR 接口")
    usage_id = storage.reserve_ocr_request(app, task, service)
    if not usage_id:
        return None, _ocr_error("quota", "OCR额度不足、接口未启用或未配置凭据")
    try:
        credentials = storage.tencent_ocr_credentials(app)
    except ValueError as exc:
        storage.complete_ocr_request(app, usage_id, status="unavailable", detail="凭据解密失败")
        return None, _ocr_error("auth", f"腾讯 OCR 凭据不可用：{str(exc)[:160]}")
    if not credentials:
        storage.complete_ocr_request(app, usage_id, status="unavailable", detail="未配置凭据")
        return None, _ocr_error("auth", "未配置腾讯 OCR 凭据")
    try:
        from tencentcloud.common import credential
        from tencentcloud.common.exception.tencent_cloud_sdk_exception import TencentCloudSDKException
        from tencentcloud.ocr.v20181119 import models, ocr_client
    except ImportError:
        storage.complete_ocr_request(app, usage_id, status="unavailable", detail="OCR SDK 未安装")
        return None, _ocr_error("unsupported", "腾讯 OCR SDK 未安装")
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
        return None, _classify_ocr_exception(exc)
    except Exception as exc:  # noqa: BLE001 - 外部SDK不可让评审任务失败
        storage.complete_ocr_request(app, usage_id, status="error", detail=str(exc))
        return None, _classify_ocr_exception(exc)
