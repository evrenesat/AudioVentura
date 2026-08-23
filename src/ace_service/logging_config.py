"""Redacted rotating logs for the controller runtime."""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Iterable
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

from ace_service.config import ServiceSettings

_TRANSFER_URL_RE = re.compile(
    r"https?://[^\s\"'<>]+/transfer/v1/(?:source|output)/[^\s\"'<>]+",
    re.IGNORECASE,
)
_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+")
_SECRET_FIELD_RE = re.compile(
    r"(?i)(\b(?:api[_ -]?key|password|secret|token|private[_ -]?key)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_PROMPT_JSON_RE = re.compile(r"(?i)([\"'](?:prompt|lyrics)[\"']\s*:\s*)[\"'][^\"']*[\"']")
_PROMPT_FIELD_RE = re.compile(
    r"(?i)(\b(?:prompt|lyrics)\b\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact_text(value: str, *, secrets: Iterable[str] = ()) -> str:
    """Remove credentials, capability URLs, and user-authored text from a log line."""

    redacted = value
    for secret in sorted({secret for secret in secrets if secret}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _TRANSFER_URL_RE.sub("[REDACTED_TRANSFER_URL]", redacted)
    redacted = _AUTH_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _SECRET_FIELD_RE.sub(r"\1[REDACTED]", redacted)
    redacted = _PROMPT_JSON_RE.sub(r'\1"[REDACTED]"', redacted)
    return _PROMPT_FIELD_RE.sub(r"\1[REDACTED]", redacted)


class RedactingFilter(logging.Filter):
    """Sanitize the fully formatted message before it reaches a file handler."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = tuple(secrets)

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        except Exception:
            message = "<unformattable log message>"
        record.msg = redact_text(message, secrets=self._secrets)
        record.args = ()
        if not hasattr(record, "component"):
            record.component = "controller"
        return True


class UTCFormatter(logging.Formatter):
    """Format timestamps in UTC so logs are comparable across components."""

    @staticmethod
    def converter(timestamp: float | None) -> time.struct_time:
        return time.gmtime(timestamp)


class PrivateRotatingFileHandler(RotatingFileHandler):
    """Keep the active file private after initial open and every rollover."""

    def _open(self):  # type: ignore[no-untyped-def]
        stream = super()._open()
        try:
            Path(self.baseFilename).chmod(0o600)
        except (NotImplementedError, PermissionError):
            pass
        return stream


def configure_logging(
    settings: ServiceSettings, *, component: str = "controller"
) -> RotatingFileHandler:
    """Install one private rotating handler for the controller package."""

    settings.ensure_data_layout()
    logger = logging.getLogger("ace_service")
    logger.setLevel(settings.log_level)
    logger.propagate = True
    path = Path(settings.paths.logs) / f"{component}.log"
    for existing in list(logger.handlers):
        existing_path = getattr(existing, "ace_log_path", None)
        if existing_path is not None and Path(existing_path).parent.parent != settings.paths.root:
            logger.removeHandler(existing)
            existing.close()
            continue
        if getattr(existing, "ace_log_path", None) == str(path):
            return existing  # type: ignore[return-value]

    handler = PrivateRotatingFileHandler(
        path,
        maxBytes=settings.log_max_bytes,
        backupCount=settings.log_backup_count,
        encoding="utf-8",
    )
    handler.ace_log_path = str(path)  # type: ignore[attr-defined]
    handler.setLevel(settings.log_level)
    handler.addFilter(
        RedactingFilter(
            secrets=(
                settings.service_password,
                settings.home_ingest_token,
                settings.runpod_api_key,
                settings.runpod_endpoint_id,
                settings.salad_api_key or "",
            )
        )
    )
    handler.setFormatter(
        UTCFormatter(
            "%(asctime)sZ level=%(levelname)s component=%(component)s logger=%(name)s %(message)s",
            datefmt="%Y-%m-%dT%H:%M:%S",
        )
    )
    logger.addHandler(handler)
    try:
        path.chmod(0o600)
    except (NotImplementedError, PermissionError):
        pass
    return handler


def log_context(
    logger: logging.Logger, *, component: str = "controller"
) -> logging.LoggerAdapter[Any]:
    """Return an adapter that adds the stable component field to records."""

    return logging.LoggerAdapter(logger, {"component": component})
