from __future__ import annotations

import logging
from pathlib import Path

from ace_service.logging_config import configure_logging, redact_text


def test_redaction_removes_credentials_capabilities_and_user_text() -> None:
    value = (
        "password=service-password Authorization: Bearer bearer-secret "
        "url=https://transfer.example/transfer/v1/output/plain-capability "
        'prompt="private prompt" lyrics="private lyrics"'
    )

    redacted = redact_text(
        value,
        secrets=("service-password", "bearer-secret", "plain-capability"),
    )

    assert "service-password" not in redacted
    assert "bearer-secret" not in redacted
    assert "plain-capability" not in redacted
    assert "private prompt" not in redacted
    assert "private lyrics" not in redacted
    assert "REDACTED" in redacted


def test_controller_logging_rotates_and_keeps_private_permissions(settings) -> None:
    settings.log_max_bytes = 128
    settings.log_backup_count = 2
    handler = configure_logging(settings, component="test")
    logger = logging.getLogger("ace_service.test_logging")
    for index in range(12):
        logger.info("job=%s stage=test elapsed_ms=%d", index, index)
    handler.flush()

    log_path = Path(settings.paths.logs) / "test.log"
    assert log_path.is_file()
    assert (Path(f"{log_path}.1")).is_file()
    assert log_path.stat().st_mode & 0o077 == 0
