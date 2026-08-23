"""Typed service configuration and private filesystem layout."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .providers.salad_names import is_salad_resource_name

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
)
_WORKER_RUNTIME_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SERVICE_ROOT_PATH_RE = re.compile(r"^/(?:[A-Za-z0-9._~-]+(?:/[A-Za-z0-9._~-]+)*)$")


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
    def evaluations(self) -> Path:
        return self.root / "evaluations"

    @property
    def campaign_database(self) -> Path:
        return self.evaluations / "quality-campaign.sqlite3"

    @property
    def all_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.incoming,
            self.outputs,
            self.temporary,
            self.logs,
            self.evaluations,
        )

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
    service_root_path: str = Field(
        default="",
        validation_alias=AliasChoices("ACE_SERVICE_ROOT_PATH", "service_root_path"),
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
    inference_provider: str = Field(
        default="runpod",
        validation_alias=AliasChoices("INFERENCE_PROVIDER", "inference_provider"),
    )
    inference_job_timeout_seconds: int = Field(
        default=7200,
        validation_alias=AliasChoices(
            "INFERENCE_JOB_TIMEOUT_SECONDS",
            "RUNPOD_JOB_TIMEOUT_SECONDS",
            "inference_job_timeout_seconds",
            "runpod_job_timeout_seconds",
        ),
        gt=0,
    )
    salad_api_key: str | None = Field(
        default=None, validation_alias=AliasChoices("SALAD_API_KEY", "salad_api_key")
    )
    salad_organization: str | None = Field(
        default=None, validation_alias=AliasChoices("SALAD_ORGANIZATION", "salad_organization")
    )
    salad_project: str | None = Field(
        default=None, validation_alias=AliasChoices("SALAD_PROJECT", "salad_project")
    )
    salad_queue_name: str = Field(
        default="audioventura-jobs",
        validation_alias=AliasChoices("SALAD_QUEUE_NAME", "salad_queue_name"),
    )
    salad_container_group_name: str = Field(
        default="audioventura-ace-step-v2",
        validation_alias=AliasChoices("SALAD_CONTAINER_GROUP_NAME", "salad_container_group_name"),
    )
    salad_poll_interval_seconds: float = Field(
        default=2,
        validation_alias=AliasChoices("SALAD_POLL_INTERVAL_SECONDS", "salad_poll_interval_seconds"),
        gt=0,
    )
    salad_connect_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices(
            "SALAD_CONNECT_TIMEOUT_SECONDS", "salad_connect_timeout_seconds"
        ),
        gt=0,
    )
    salad_read_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("SALAD_READ_TIMEOUT_SECONDS", "salad_read_timeout_seconds"),
        gt=0,
    )
    salad_write_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("SALAD_WRITE_TIMEOUT_SECONDS", "salad_write_timeout_seconds"),
        gt=0,
    )
    salad_pool_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices("SALAD_POOL_TIMEOUT_SECONDS", "salad_pool_timeout_seconds"),
        gt=0,
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
    runpod_worker_runtime_identity: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "RUNPOD_WORKER_RUNTIME_IDENTITY", "runpod_worker_runtime_identity"
        ),
        max_length=71,
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
    cleanup_stale_after_seconds: int = Field(
        default=86_400,
        validation_alias=AliasChoices(
            "ACE_CLEANUP_STALE_AFTER_SECONDS", "cleanup_stale_after_seconds"
        ),
        gt=0,
    )
    transfer_record_retention_seconds: int = Field(
        default=86_400,
        validation_alias=AliasChoices(
            "ACE_TRANSFER_RECORD_RETENTION_SECONDS", "transfer_record_retention_seconds"
        ),
        gt=0,
    )
    evaluation_campaign_database: Path | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "ACE_EVALUATION_CAMPAIGN_DATABASE", "evaluation_campaign_database"
        ),
    )
    evaluation_media_retention_days: int = Field(
        default=7,
        validation_alias=AliasChoices(
            "EVALUATION_MEDIA_RETENTION_DAYS", "evaluation_media_retention_days"
        ),
        ge=1,
        le=365,
    )
    eligible_gpu_ids: list[str] = Field(
        default_factory=list,
        validation_alias=AliasChoices("ACE_ELIGIBLE_GPU_IDS", "eligible_gpu_ids"),
    )

    @field_validator("eligible_gpu_ids")
    @classmethod
    def validate_eligible_gpu_ids(cls, value: list[str]) -> list[str]:
        normalized: list[str] = []
        for gpu_id in value:
            if not isinstance(gpu_id, str) or not gpu_id.strip():
                raise ValueError("eligible GPU IDs must be non-empty strings")
            normalized.append(gpu_id.strip())
        if len(set(normalized)) != len(normalized):
            raise ValueError("eligible GPU IDs must not contain duplicates")
        return normalized

    @field_validator("inference_provider")
    @classmethod
    def validate_inference_provider(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"runpod", "salad"}:
            raise ValueError("inference provider must be runpod or salad")
        return normalized

    @field_validator("salad_queue_name", "salad_container_group_name")
    @classmethod
    def validate_salad_names(cls, value: str) -> str:
        if not is_salad_resource_name(value):
            raise ValueError("Salad resource names must be DNS-compatible")
        return value

    @field_validator("service_root_path")
    @classmethod
    def validate_service_root_path(cls, value: str) -> str:
        if value == "":
            return value
        if (
            value == "/"
            or value.endswith("/")
            or "//" in value
            or "\\" in value
            or "?" in value
            or "#" in value
            or any(ord(character) < 32 or ord(character) == 127 for character in value)
            or not _SERVICE_ROOT_PATH_RE.fullmatch(value)
            or any(segment in {".", ".."} for segment in value.split("/"))
        ):
            raise ValueError("service root path must be empty or a normalized absolute path prefix")
        return value

    @field_validator("runpod_worker_runtime_identity")
    @classmethod
    def validate_runpod_worker_runtime_identity(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _WORKER_RUNTIME_DIGEST_RE.fullmatch(normalized):
            raise ValueError(
                "worker runtime identity must be an exact sha256:<64 lowercase hex> digest"
            )
        return normalized

    @field_validator("host", "transfer_host")
    @classmethod
    def reject_wildcard_bind(cls, value: str) -> str:
        if value.strip() in {"0.0.0.0", "::", "::0"}:
            raise ValueError("application hosts must not bind to a wildcard address")
        return value.strip()

    @field_validator("log_level")
    @classmethod
    def normalize_log_level(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG"}:
            raise ValueError("log level must be one of CRITICAL, ERROR, WARNING, INFO, DEBUG")
        return normalized

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

        if self.inference_provider == "runpod":
            for field_name in ("runpod_api_key", "runpod_endpoint_id"):
                value = getattr(self, field_name).strip().lower()
                if value in _PLACEHOLDERS:
                    raise ValueError(f"{field_name} still contains a configuration placeholder")
        else:
            for field_name in ("salad_api_key", "salad_organization", "salad_project"):
                value = getattr(self, field_name)
                if value is None or not value.strip() or value.strip().lower() in _PLACEHOLDERS:
                    raise ValueError(f"{field_name} is required for Salad")
            assert self.salad_organization is not None and self.salad_project is not None
            if not is_salad_resource_name(self.salad_organization) or not is_salad_resource_name(
                self.salad_project
            ):
                raise ValueError("Salad organization and project must be DNS-compatible")

        if self.eligible_gpu_ids and self.runpod_worker_runtime_identity is None:
            raise ValueError(
                "runpod_worker_runtime_identity is required when eligible GPU IDs are configured"
            )

        self.data_root = self.data_root.expanduser().resolve()
        if self.evaluation_campaign_database is not None:
            campaign_database = self.evaluation_campaign_database.expanduser().resolve()
            if not campaign_database.parent.is_relative_to(self.data_root):
                raise ValueError("evaluation campaign database must remain under data_root")
            self.evaluation_campaign_database = campaign_database
        return self

    @property
    def paths(self) -> DataPaths:
        return DataPaths(self.data_root)

    @property
    def campaign_database_path(self) -> Path:
        """Return the dedicated, non-product quality campaign database path."""

        return self.evaluation_campaign_database or self.paths.campaign_database

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
