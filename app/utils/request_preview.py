from __future__ import annotations

import base64
import hashlib
import json
import re
from typing import Any

from fastapi import Request


SENSITIVE_REQUEST_KEYS = {
    "api_key",
    "api-key",
    "apikey",
    "api_key_secret",
    "secret",
    "token",
    "access_token",
    "refresh_token",
    "authorization",
    "password",
    "credential",
}
LARGE_REQUEST_KEYS = {
    "audio",
    "image",
    "video",
    "file",
    "files",
    "data",
    "base64",
    "b64_json",
    "image_base64",
    "audio_data",
    "video_data",
}


def sanitize_request_value(value: Any, *, key: str = "", depth: int = 0) -> Any:
    lowered_key = key.lower()
    if is_sensitive_request_key(lowered_key):
        return "[已隐藏敏感字段]"
    if depth > 8:
        return "[层级过深，已省略]"
    if isinstance(value, dict):
        return {str(item_key): sanitize_request_value(item_value, key=str(item_key), depth=depth + 1) for item_key, item_value in value.items()}
    if isinstance(value, list):
        if len(value) > 80:
            return {
                "_type": "list",
                "_length": len(value),
                "_preview": [sanitize_request_value(item, key=key, depth=depth + 1) for item in value[:20]],
                "_truncated": True,
            }
        return [sanitize_request_value(item, key=key, depth=depth + 1) for item in value]
    if isinstance(value, bytes):
        return {"_type": "bytes", "_bytes": len(value), "_sha256": hashlib.sha256(value).hexdigest()[:16]}
    if isinstance(value, str):
        return sanitize_request_string(value, lowered_key)
    return value


def sanitize_request_string(value: str, lowered_key: str) -> Any:
    if not value:
        return value
    looks_large_by_key = any(token in lowered_key for token in LARGE_REQUEST_KEYS)
    if is_probable_base64(value):
        return {
            "_type": "base64",
            "_chars": len(value),
            "_sha256": hashlib.sha256(value[:100000].encode("utf-8", errors="ignore")).hexdigest()[:16],
            "_preview": value[:80] + ("..." if len(value) > 80 else ""),
        }
    if value.startswith("data:") and "," in value:
        header, packed = value.split(",", 1)
        return {
            "_type": "data_uri",
            "_media_type": header[:120],
            "_chars": len(value),
            "_payload_chars": len(packed),
            "_preview": header[:120] + ",...",
        }
    max_length = 500 if looks_large_by_key else 3000
    if len(value) > max_length:
        return {
            "_type": "text",
            "_chars": len(value),
            "_preview": value[:max_length] + "...",
            "_truncated": True,
        }
    return value


def is_sensitive_request_key(lowered_key: str) -> bool:
    if not lowered_key:
        return False
    if lowered_key in SENSITIVE_REQUEST_KEYS:
        return True
    separators = ("_", "-", ".")
    return any(
        lowered_key.endswith(f"{separator}{suffix}")
        for separator in separators
        for suffix in ("key", "token", "secret", "password")
    )


def is_probable_base64(value: str) -> bool:
    text = value.strip()
    if len(text) < 2048 or len(text) % 4 != 0:
        return False
    if not re.fullmatch(r"[A-Za-z0-9+/=\s]+", text):
        return False
    try:
        base64.b64decode(text[:4096], validate=False)
    except Exception:
        return False
    return True


def json_preview(value: Any) -> str:
    return json.dumps(sanitize_request_value(value), ensure_ascii=False, indent=2)


def request_client_ip(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "")
    if forwarded_for:
        return forwarded_for.split(",", 1)[0].strip()[:80]
    return (request.client.host if request.client else "")[:80]
