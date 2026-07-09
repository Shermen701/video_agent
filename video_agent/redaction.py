from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

SENSITIVE_KEYS = {"password", "api_token", "token", "meeting_password", "authorization"}
SECRET_PATTERNS = [
    re.compile(r"(Bearer\s+)[A-Za-z0-9._\-]+", re.IGNORECASE),
    re.compile(r"((?:password|token|api_token)\s*[=:]\s*)\S+", re.IGNORECASE),
]


def redact_text(text: str) -> str:
    redacted = text
    for pattern in SECRET_PATTERNS:
        redacted = pattern.sub(r"\1***", redacted)
    return redacted


def redact_mapping(value: Any) -> Any:
    if isinstance(value, Mapping):
        output: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SENSITIVE_KEYS:
                output[key] = "***"
            else:
                output[key] = redact_mapping(item)
        return output
    if isinstance(value, list):
        return [redact_mapping(item) for item in value]
    if isinstance(value, str):
        return redact_text(value)
    return value
