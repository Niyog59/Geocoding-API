"""Structured logging.

Every log line is one JSON object written to stderr. The request ID comes from
a context variable and is added by a filter on the handler, so records from
any library (uvicorn, httpx, redis) are formatted the same way and never fail
for lack of a field.

A redaction filter removes API keys, the admin secret and provider keys from
every message before it is written, including URLs logged by HTTP clients.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from contextvars import ContextVar
from datetime import UTC, datetime

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

_PATTERNS = [
    re.compile(r"gk_[0-9a-f]{12}_[A-Za-z0-9_-]{20,}"),
    re.compile(r"(?i)((?:api_?key|apikey|key|token|secret|password)=)[^&\s\"']+"),
    re.compile(r"(?i)((?:x-api-key|x-admin-secret|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',}]+"),
]


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: list[str] | None = None) -> None:
        super().__init__()
        self.secrets = [s for s in (secrets or []) if s and len(s) >= 8]

    def redact(self, text: str) -> str:
        for secret in self.secrets:
            text = text.replace(secret, "[REDACTED]")
        for pattern in _PATTERNS:
            text = pattern.sub(lambda m: (m.group(1) if m.groups() else "") + "[REDACTED]", text)
        return text

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = self.redact(record.getMessage())
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = self.redact(record.exc_text)
        return True


class RequestIdFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "request_id": getattr(record, "request_id", "-"),
            "message": record.getMessage(),
        }
        for field in ("route", "method", "status", "duration_ms", "cache", "client"):
            value = getattr(record, field, None)
            if value is not None:
                entry[field] = value
        if record.exc_text:
            entry["exception"] = record.exc_text
        return json.dumps(entry, ensure_ascii=False)


def configure_logging(level: str = "INFO", secrets: list[str] | None = None) -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.addFilter(RequestIdFilter())
    handler.addFilter(RedactingFilter(secrets))
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Route uvicorn's loggers through the same handler. Its access log is
    # replaced by our own request log, which omits query strings (they can
    # contain addresses) and includes the request ID.
    for name in ("uvicorn", "uvicorn.error"):
        logging.getLogger(name).handlers = []
        logging.getLogger(name).propagate = True
    logging.getLogger("uvicorn.access").disabled = True
    # httpx logs every request URL at INFO, and provider URLs carry API keys.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
