from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.events.models import MonitorEvent

_SECRET_KEYS = {
    "api_key",
    "authorization",
    "cookie",
    "password",
    "secret",
    "set-cookie",
    "token",
}
_BEARER = re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=-]+")


def redact_event(event: MonitorEvent) -> MonitorEvent:
    redacted = event.model_copy(deep=True)
    changed: list[str] = []
    redacted.args = _redact(redacted.args, "args", changed)
    redacted.result = _redact(redacted.result, "result", changed)
    redacted.raw = _redact(redacted.raw, "raw", changed)
    if redacted.content:
        content = _BEARER.sub(r"\1 [REDACTED]", redacted.content)
        if content != redacted.content:
            changed.append("content")
        redacted.content = content
    if redacted.target and redacted.target.startswith(("http://", "https://")):
        redacted.target = _redact_url(redacted.target)
    if changed:
        redacted.metadata["redaction"] = {
            "policy_version": "redaction:v1",
            "fields": sorted(set(changed)),
        }
    return redacted


def _redact(value: Any, path: str, changed: list[str]) -> Any:
    if isinstance(value, dict):
        output = {}
        for key, item in value.items():
            child = f"{path}.{key}"
            if key.lower() in _SECRET_KEYS:
                output[key] = "[REDACTED]"
                changed.append(child)
            else:
                output[key] = _redact(item, child, changed)
        return output
    if isinstance(value, list):
        return [_redact(item, f"{path}[]", changed) for item in value]
    if isinstance(value, str):
        redacted = _BEARER.sub(r"\1 [REDACTED]", value)
        if value.startswith(("http://", "https://")):
            redacted = _redact_url(redacted)
        if redacted != value:
            changed.append(path)
        return redacted
    return value


def _redact_url(value: str) -> str:
    try:
        parts = urlsplit(value)
    except ValueError:
        return value
    hostname = parts.hostname or ""
    netloc = hostname
    if parts.port:
        netloc = f"{hostname}:{parts.port}"
    query = "&".join(
        f"{key}={'[REDACTED]' if key.lower() in _SECRET_KEYS else item}"
        for pair in parts.query.split("&")
        if pair
        for key, _, item in [pair.partition("=")]
    )
    return urlunsplit((parts.scheme, netloc, parts.path, query, parts.fragment))
