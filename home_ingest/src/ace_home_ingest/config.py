"""Typed configuration and private local paths for the home ingest agent."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDERS = frozenset({"change-me", "changeme", "replace-me", "replace_me"})


@dataclass(frozen=True, slots=True)
class IngestPaths:
    """Private home-server paths used during one preparation request."""

    root: Path

    @property
    def temporary(self) -> Path:
        return self.root / "temporary"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def all_directories(self) -> tuple[Path, ...]:
        return (self.root, self.temporary, self.logs)

    def job_temporary(self, job_id: str) -> Path:
        return self.temporary / job_id


class HomeIngestSettings(BaseSettings):
    """Runtime settings for the localhost-only home ingestion service."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
    )

    data_root: Path = Field(
        default=Path("~/.local/share/ace-home-ingest"),
        validation_alias=AliasChoices("ACE_HOME_INGEST_ROOT", "data_root"),
    )
    host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("ACE_HOME_INGEST_HOST", "host"),
    )
    port: int = Field(
        default=8100,
        validation_alias=AliasChoices("ACE_HOME_INGEST_PORT", "port"),
        ge=1,
        le=65535,
    )
    token: str = Field(
        default="change-me",
        validation_alias=AliasChoices("ACE_HOME_INGEST_TOKEN", "token"),
        min_length=1,
    )
    max_duration_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices("MAX_SOURCE_DURATION_SECONDS", "max_duration_seconds"),
        gt=0,
        le=600,
    )
    max_source_bytes: int = Field(
        default=536_870_912,
        validation_alias=AliasChoices("ACE_HOME_MAX_SOURCE_BYTES", "max_source_bytes"),
        gt=0,
        le=1_073_741_824,
    )
    canonical_source_max_bytes: int = Field(
        default=536_870_912,
        validation_alias=AliasChoices(
            "ACE_HOME_CANONICAL_SOURCE_MAX_BYTES", "canonical_source_max_bytes"
        ),
        gt=0,
        le=1_073_741_824,
    )
    transfer_base_url: str = Field(
        default="https://transfer.example.invalid",
        validation_alias=AliasChoices("ACE_HOME_TRANSFER_BASE_URL", "transfer_base_url"),
        min_length=1,
    )
    transfer_connect_timeout_seconds: float = Field(
        default=10,
        validation_alias=AliasChoices(
            "ACE_HOME_TRANSFER_CONNECT_TIMEOUT_SECONDS", "transfer_connect_timeout_seconds"
        ),
        gt=0,
        le=120,
    )
    transfer_read_timeout_seconds: float = Field(
        default=1800,
        validation_alias=AliasChoices(
            "ACE_HOME_TRANSFER_READ_TIMEOUT_SECONDS", "transfer_read_timeout_seconds"
        ),
        gt=0,
        le=7200,
    )
    transfer_write_timeout_seconds: float = Field(
        default=1800,
        validation_alias=AliasChoices(
            "ACE_HOME_TRANSFER_WRITE_TIMEOUT_SECONDS", "transfer_write_timeout_seconds"
        ),
        gt=0,
        le=7200,
    )
    command_timeout_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices(
            "ACE_HOME_MEDIA_COMMAND_TIMEOUT_SECONDS", "command_timeout_seconds"
        ),
        gt=0,
        le=7200,
    )
    retain_debug_artifacts: bool = Field(
        default=False,
        validation_alias=AliasChoices("ACE_HOME_RETAIN_DEBUG_ARTIFACTS", "retain_debug_artifacts"),
    )
    debug_retention_seconds: int = Field(
        default=3600,
        validation_alias=AliasChoices(
            "ACE_HOME_DEBUG_RETENTION_SECONDS", "debug_retention_seconds"
        ),
        gt=0,
        le=86_400,
    )
    log_level: str = Field(
        default="INFO",
        validation_alias=AliasChoices("ACE_LOG_LEVEL", "log_level"),
        min_length=1,
    )
    log_max_bytes: int = Field(
        default=10_485_760,
        validation_alias=AliasChoices("ACE_LOG_MAX_BYTES", "log_max_bytes"),
        gt=0,
    )
    log_backup_count: int = Field(
        default=5,
        validation_alias=AliasChoices("ACE_LOG_BACKUP_COUNT", "log_backup_count"),
        ge=1,
    )
    cleanup_interval_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices("ACE_CLEANUP_INTERVAL_SECONDS", "cleanup_interval_seconds"),
        gt=0,
    )
    orphan_age_seconds: int = Field(
        default=86_400,
        validation_alias=AliasChoices("ACE_ORPHAN_AGE_SECONDS", "orphan_age_seconds"),
        gt=0,
    )
    sftp_host: str = Field(
        default="",
        validation_alias=AliasChoices("ACE_SFTP_HOST", "sftp_host"),
    )
    sftp_port: int = Field(
        default=22,
        validation_alias=AliasChoices("ACE_SFTP_PORT", "sftp_port"),
        ge=1,
        le=65535,
    )
    sftp_username: str = Field(
        default="",
        validation_alias=AliasChoices("ACE_SFTP_USERNAME", "sftp_username"),
    )
    sftp_private_key: Path = Field(
        default=Path("~/.ssh/ace-service-incoming"),
        validation_alias=AliasChoices("ACE_SFTP_PRIVATE_KEY", "sftp_private_key"),
    )
    sftp_remote_root: str = Field(
        default="/srv/ace-service/data/incoming",
        validation_alias=AliasChoices("ACE_SFTP_REMOTE_ROOT", "sftp_remote_root"),
    )
    sftp_connect_timeout_seconds: float = Field(
        default=15,
        validation_alias=AliasChoices(
            "ACE_SFTP_CONNECT_TIMEOUT_SECONDS", "sftp_connect_timeout_seconds"
        ),
        gt=0,
        le=120,
    )

    @field_validator("host")
    @classmethod
    def reject_wildcard_bind(cls, value: str) -> str:
        value = value.strip()
        if value in {"0.0.0.0", "::", "::0"}:
            raise ValueError("home ingest must not bind to a wildcard address")
        if not value:
            raise ValueError("home ingest host must not be empty")
        return value

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("log level must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG")
        return normalized

    @field_validator("sftp_remote_root")
    @classmethod
    def validate_remote_root(cls, value: str) -> str:
        root = PurePosixPath(value.strip())
        if not root.is_absolute() or any(part == ".." for part in root.parts):
            raise ValueError("SFTP remote root must be an absolute path without traversal")
        return str(root)

    @field_validator("sftp_host", "sftp_username")
    @classmethod
    def strip_sftp_text(cls, value: str) -> str:
        return value.strip()

    @field_validator("sftp_private_key")
    @classmethod
    def expand_private_key(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @model_validator(mode="after")
    def validate_runtime_values(self) -> HomeIngestSettings:
        if self.token.strip().lower() in _PLACEHOLDERS:
            raise ValueError("token still contains a configuration placeholder")
        self.data_root = self.data_root.expanduser().resolve()
        self.transfer_base_url = validate_private_tailscale_url(self.transfer_base_url)
        return self

    @property
    def paths(self) -> IngestPaths:
        return IngestPaths(self.data_root)

    def ensure_data_layout(self) -> IngestPaths:
        """Create private local directories used by the agent."""

        paths = self.paths
        for directory in paths.all_directories:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except (NotImplementedError, PermissionError):
                pass
        return paths

    def validate_sftp_runtime(self) -> None:
        """Reject incomplete SFTP settings immediately before a live upload."""

        if not self.sftp_host or not self.sftp_username:
            raise ValueError("SFTP host and username must be configured")
        if not self.sftp_private_key.is_file():
            raise ValueError("configured SFTP private key does not exist")


def validate_private_tailscale_url(value: str) -> str:
    """Validate an optional private HTTP endpoint without accepting wildcards."""

    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("private endpoint must include an http(s) scheme and host")
    if parsed.username or parsed.password or parsed.fragment:
        raise ValueError("private endpoint must not include credentials or fragments")
    return value.rstrip("/")
