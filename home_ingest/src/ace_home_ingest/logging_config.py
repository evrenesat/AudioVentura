"""Redacted rotating logs for the home-ingest service."""

from __future__ import annotations

import logging
import re
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import HomeIngestSettings

_AUTH_RE = re.compile(r"(?i)(authorization\s*[:=]\s*(?:bearer\s+)?)[^\s,;]+")
_SECRET_RE = re.compile(
    r"(?i)(\b(?:password|secret|token|private[_ -]?key)\b\s*[:=]\s*)"
    r"(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


def redact_text(value: str, *, secrets: tuple[str, ...] = ()) -> str:
    """Remove home bearer/SFTP credentials from a log line."""

    redacted = value
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _AUTH_RE.sub(r"\1[REDACTED]", redacted)
    return _SECRET_RE.sub(r"\1[REDACTED]", redacted)


class RedactingFilter(logging.Filter):
    def __init__(self, secrets: tuple[str, ...]) -> None:
        super().__init__()
        self._secrets = secrets

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_text(record.getMessage(), secrets=self._secrets)
        record.args = ()
        if not hasattr(record, "component"):
            record.component = "home_ingest"
        return True


class UTCFormatter(logging.Formatter):
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
    settings: HomeIngestSettings, *, component: str = "home-ingest"
) -> RotatingFileHandler:
    settings.ensure_data_layout()
    logger = logging.getLogger("ace_home_ingest")
    logger.setLevel(settings.log_level)
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
    handler.addFilter(RedactingFilter((settings.token,)))
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
