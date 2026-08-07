"""Typed service configuration and private filesystem layout."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_PLACEHOLDERS = frozenset(
    {
        "change-me",
        "changeme",
        "replace-me",
        "replace_me",
        "example.invalid",
    }
)
_CREDENTIAL_FIELDS = (
    "service_password",
    "home_ingest_token",
    "runpod_api_key",
    "runpod_endpoint_id",
)


@dataclass(frozen=True, slots=True)
class DataPaths:
    """All persistent controller paths, resolved below one data root."""

    root: Path

    @property
    def database(self) -> Path:
        return self.root / "service.db"

    @property
    def incoming(self) -> Path:
        return self.root / "incoming"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def temporary(self) -> Path:
        return self.root / "temporary"

    @property
    def logs(self) -> Path:
        return self.root / "logs"

    @property
    def all_directories(self) -> tuple[Path, ...]:
        return (self.root, self.incoming, self.outputs, self.temporary, self.logs)

    def job_incoming(self, job_id: str) -> Path:
        return self.incoming / job_id

    def job_outputs(self, job_id: str) -> Path:
        return self.outputs / job_id

    def job_temporary(self, job_id: str) -> Path:
        return self.temporary / job_id


class ServiceSettings(BaseSettings):
    """Runtime settings for the Hetzner controller and transfer boundary."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
    )

    data_root: Path = Field(
        default=Path("/srv/ace-service/data"),
        validation_alias=AliasChoices("ACE_SERVICE_DATA_ROOT", "data_root"),
    )
    host: str = Field(
        default="127.0.0.1", validation_alias=AliasChoices("ACE_SERVICE_HOST", "host")
    )
    port: int = Field(
        default=8000, validation_alias=AliasChoices("ACE_SERVICE_PORT", "port"), ge=1, le=65535
    )
    transfer_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("ACE_TRANSFER_HOST", "transfer_host"),
    )
    transfer_port: int = Field(
        default=8001,
        validation_alias=AliasChoices("ACE_TRANSFER_PORT", "transfer_port"),
        ge=1,
        le=65535,
    )
    service_username: str = Field(
        default="change-me",
        validation_alias=AliasChoices("ACE_SERVICE_USERNAME", "service_username"),
        min_length=1,
    )
    service_password: str = Field(
        default="change-me",
        validation_alias=AliasChoices("ACE_SERVICE_PASSWORD", "service_password"),
        min_length=1,
    )
    service_public_hostname: str = Field(
        default="hetzner-name.tailnet-name.ts.net",
        validation_alias=AliasChoices("ACE_SERVICE_PUBLIC_HOSTNAME", "service_public_hostname"),
        min_length=1,
    )
    transfer_public_base_url: str = Field(
        default="https://transfer.example.invalid",
        validation_alias=AliasChoices("ACE_TRANSFER_PUBLIC_BASE_URL", "transfer_public_base_url"),
        min_length=1,
    )
    transfer_token_ttl_seconds: int = Field(
        default=14400,
        validation_alias=AliasChoices(
            "ACE_TRANSFER_TOKEN_TTL_SECONDS", "transfer_token_ttl_seconds"
        ),
        gt=0,
    )
    transfer_max_source_bytes: int = Field(
        default=67108864,
        validation_alias=AliasChoices("ACE_TRANSFER_MAX_SOURCE_BYTES", "transfer_max_source_bytes"),
        gt=0,
    )
    transfer_max_output_bytes: int = Field(
        default=268435456,
        validation_alias=AliasChoices("ACE_TRANSFER_MAX_OUTPUT_BYTES", "transfer_max_output_bytes"),
        gt=0,
    )
    home_ingest_base_url: str = Field(
        default="https://home-name.tailnet-name.ts.net",
        validation_alias=AliasChoices("ACE_HOME_INGEST_BASE_URL", "home_ingest_base_url"),
        min_length=1,
    )
    home_ingest_token: str = Field(
        default="change-me",
        validation_alias=AliasChoices("ACE_HOME_INGEST_TOKEN", "home_ingest_token"),
        min_length=1,
    )
    runpod_api_key: str = Field(
        default="change-me",
        validation_alias=AliasChoices("RUNPOD_API_KEY", "runpod_api_key"),
        min_length=1,
    )
    runpod_endpoint_id: str = Field(
        default="change-me",
        validation_alias=AliasChoices("RUNPOD_ENDPOINT_ID", "runpod_endpoint_id"),
        min_length=1,
    )
    runpod_poll_interval_seconds: float = Field(
        default=2,
        validation_alias=AliasChoices(
            "RUNPOD_POLL_INTERVAL_SECONDS", "runpod_poll_interval_seconds"
        ),
        gt=0,
    )
    runpod_job_timeout_seconds: int = Field(
        default=7200,
        validation_alias=AliasChoices("RUNPOD_JOB_TIMEOUT_SECONDS", "runpod_job_timeout_seconds"),
        gt=0,
    )
    runpod_execution_timeout_ms: int = Field(
        default=1200000,
        validation_alias=AliasChoices("RUNPOD_EXECUTION_TIMEOUT_MS", "runpod_execution_timeout_ms"),
        gt=0,
    )
    runpod_job_ttl_ms: int = Field(
        default=7200000,
        validation_alias=AliasChoices("RUNPOD_JOB_TTL_MS", "runpod_job_ttl_ms"),
        gt=0,
    )
    runpod_connect_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices(
            "RUNPOD_CONNECT_TIMEOUT_SECONDS", "runpod_connect_timeout_seconds"
        ),
        gt=0,
    )
    runpod_read_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("RUNPOD_READ_TIMEOUT_SECONDS", "runpod_read_timeout_seconds"),
        gt=0,
    )
    runpod_write_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices(
            "RUNPOD_WRITE_TIMEOUT_SECONDS", "runpod_write_timeout_seconds"
        ),
        gt=0,
    )
    runpod_pool_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices("RUNPOD_POOL_TIMEOUT_SECONDS", "runpod_pool_timeout_seconds"),
        gt=0,
    )
    acestep_model: str = Field(
        default="acestep-v15-xl-turbo",
        validation_alias=AliasChoices("ACESTEP_MODEL", "acestep_model"),
        min_length=1,
    )
    acestep_lm_model: str = Field(
        default="acestep-5Hz-lm-1.7B",
        validation_alias=AliasChoices("ACESTEP_LM_MODEL", "acestep_lm_model"),
        min_length=1,
    )
    max_generation_duration_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices(
            "MAX_GENERATION_DURATION_SECONDS", "max_generation_duration_seconds"
        ),
        gt=0,
    )
    max_source_duration_seconds: int = Field(
        default=600,
        validation_alias=AliasChoices("MAX_SOURCE_DURATION_SECONDS", "max_source_duration_seconds"),
        gt=0,
    )
    retain_cover_source: bool = Field(
        default=False,
        validation_alias=AliasChoices("RETAIN_COVER_SOURCE", "retain_cover_source"),
    )
    sqlite_busy_timeout_ms: int = Field(
        default=5000,
        validation_alias=AliasChoices("ACE_SQLITE_BUSY_TIMEOUT_MS", "sqlite_busy_timeout_ms"),
        gt=0,
    )

    @field_validator("host", "transfer_host")
    @classmethod
    def reject_wildcard_bind(cls, value: str) -> str:
        if value.strip() in {"0.0.0.0", "::", "::0"}:
            raise ValueError("application hosts must not bind to a wildcard address")
        return value.strip()

    @field_validator("transfer_public_base_url")
    @classmethod
    def validate_transfer_public_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("public transfer base URL must use https and include a host")
        return value.rstrip("/")

    @field_validator("home_ingest_base_url")
    @classmethod
    def validate_home_ingest_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("home ingest base URL must include an http(s) scheme and host")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_runtime_values(self) -> ServiceSettings:
        for field_name in _CREDENTIAL_FIELDS:
            value = getattr(self, field_name).strip().lower()
            if value in _PLACEHOLDERS or value.endswith(".example.invalid"):
                raise ValueError(f"{field_name} still contains a configuration placeholder")

        self.data_root = self.data_root.expanduser().resolve()
        return self

    @property
    def paths(self) -> DataPaths:
        return DataPaths(self.data_root)

    def ensure_data_layout(self) -> DataPaths:
        """Create the controller directories with private permissions where possible."""

        paths = self.paths
        for directory in paths.all_directories:
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            try:
                directory.chmod(0o700)
            except (NotImplementedError, PermissionError):
                pass
        return paths
