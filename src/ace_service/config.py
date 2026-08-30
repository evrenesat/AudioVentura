"""Typed service configuration and private filesystem layout."""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import AliasChoices, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from .providers.base import BackendId, MediaKind
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
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
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
    def uploads(self) -> Path:
        return self.root / "uploads"

    @property
    def outputs(self) -> Path:
        return self.root / "outputs"

    @property
    def library(self) -> Path:
        return self.root / "library"

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
    def trash(self) -> Path:
        return self.root / "trash"

    @property
    def campaign_database(self) -> Path:
        return self.evaluations / "quality-campaign.sqlite3"

    @property
    def all_directories(self) -> tuple[Path, ...]:
        return (
            self.root,
            self.uploads,
            self.incoming,
            self.outputs,
            self.library,
            self.temporary,
            self.logs,
            self.evaluations,
            self.trash,
        )

    def job_incoming(self, job_id: str) -> Path:
        return self.incoming / job_id

    def job_outputs(self, job_id: str) -> Path:
        return self.outputs / job_id

    def job_temporary(self, job_id: str) -> Path:
        return self.temporary / job_id

    @staticmethod
    def _uuid_component(value: str) -> str:
        normalized = str(value)
        if not re.fullmatch(
            r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}",
            normalized,
        ):
            raise ValueError("asset identity must be a UUID")
        return normalized.lower()

    def source_upload_directory(self, source_asset_id: str) -> Path:
        return self.uploads / self._uuid_component(source_asset_id)

    def source_upload_part(self, source_asset_id: str) -> Path:
        return self.source_upload_directory(source_asset_id) / "source.bin.part"

    def source_upload_final(self, source_asset_id: str) -> Path:
        return self.source_upload_directory(source_asset_id) / "source.bin"

    def source_library(self, source_asset_id: str) -> Path:
        return self.library / "sources" / self._uuid_component(source_asset_id) / "source.mp3"

    def generated_playback(self, media_item_id: str) -> Path:
        return self.library / "generated" / self._uuid_component(media_item_id) / "playback.mp3"

    def media_trash_item(self, media_item_id: str) -> Path:
        return self.trash / "media" / media_item_id


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
    incoming_directory_mode: str = Field(
        default="0700",
        validation_alias=AliasChoices(
            "ACE_SERVICE_INCOMING_DIRECTORY_MODE", "incoming_directory_mode"
        ),
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
    direct_upload_max_bytes: int = Field(
        default=536_870_912,
        validation_alias=AliasChoices("ACE_DIRECT_UPLOAD_MAX_BYTES", "direct_upload_max_bytes"),
        gt=0,
    )
    canonical_source_max_bytes: int = Field(
        default=536_870_912,
        validation_alias=AliasChoices(
            "ACE_CANONICAL_SOURCE_MAX_BYTES", "canonical_source_max_bytes"
        ),
        gt=0,
    )
    asset_download_max_opens: int = Field(
        default=3,
        validation_alias=AliasChoices("ACE_ASSET_DOWNLOAD_MAX_OPENS", "asset_download_max_opens"),
        ge=1,
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
    mock_base_url: str = Field(
        default="http://127.0.0.1:8200",
        validation_alias=AliasChoices("MOCK_BASE_URL", "mock_base_url"),
        min_length=1,
    )
    mock_token: str = Field(
        default="change-me",
        validation_alias=AliasChoices("MOCK_TOKEN", "mock_token"),
        min_length=1,
    )
    mock_poll_interval_seconds: float = Field(
        default=2,
        validation_alias=AliasChoices("MOCK_POLL_INTERVAL_SECONDS", "mock_poll_interval_seconds"),
        gt=0,
    )
    mock_connect_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices(
            "MOCK_CONNECT_TIMEOUT_SECONDS", "mock_connect_timeout_seconds"
        ),
        gt=0,
    )
    mock_read_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("MOCK_READ_TIMEOUT_SECONDS", "mock_read_timeout_seconds"),
        gt=0,
    )
    mock_write_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("MOCK_WRITE_TIMEOUT_SECONDS", "mock_write_timeout_seconds"),
        gt=0,
    )
    mock_pool_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices("MOCK_POOL_TIMEOUT_SECONDS", "mock_pool_timeout_seconds"),
        gt=0,
    )
    ace_node_base_url: str = Field(
        default="http://127.0.0.1:8210",
        validation_alias=AliasChoices("ACE_NODE_BASE_URL", "ace_node_base_url"),
        min_length=1,
    )
    ace_node_token: str = Field(
        default="change-me",
        validation_alias=AliasChoices("ACE_NODE_TOKEN", "ace_node_token"),
        min_length=1,
    )
    ace_node_connect_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices(
            "ACE_NODE_CONNECT_TIMEOUT_SECONDS", "ace_node_connect_timeout_seconds"
        ),
        gt=0,
    )
    ace_node_read_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices(
            "ACE_NODE_READ_TIMEOUT_SECONDS", "ace_node_read_timeout_seconds"
        ),
        gt=0,
    )
    ace_node_write_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices(
            "ACE_NODE_WRITE_TIMEOUT_SECONDS", "ace_node_write_timeout_seconds"
        ),
        gt=0,
    )
    ace_node_pool_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices(
            "ACE_NODE_POOL_TIMEOUT_SECONDS", "ace_node_pool_timeout_seconds"
        ),
        gt=0,
    )
    inference_enabled_backends: str = Field(
        default="runpod/ace-step-v15-xl-turbo,salad/ace-step-v15-xl-turbo",
        validation_alias=AliasChoices("INFERENCE_ENABLED_BACKENDS", "inference_enabled_backends"),
    )
    default_original_backend: str = Field(
        default="runpod/ace-step-v15-xl-turbo",
        validation_alias=AliasChoices("DEFAULT_ORIGINAL_BACKEND", "default_original_backend"),
    )
    default_cover_backend: str = Field(
        default="salad/ace-step-v15-xl-turbo",
        validation_alias=AliasChoices("DEFAULT_COVER_BACKEND", "default_cover_backend"),
    )
    fal_key: str | None = Field(default=None, validation_alias=AliasChoices("FAL_KEY", "fal_key"))
    fal_allowed_media_kinds: str = Field(
        default="music",
        validation_alias=AliasChoices("FAL_ALLOWED_MEDIA_KINDS", "fal_allowed_media_kinds"),
    )
    fal_catalog_path: Path | None = Field(
        default=None, validation_alias=AliasChoices("FAL_CATALOG_PATH", "fal_catalog_path")
    )
    fal_poll_interval_seconds: float = Field(
        default=2,
        validation_alias=AliasChoices("FAL_POLL_INTERVAL_SECONDS", "fal_poll_interval_seconds"),
        gt=0,
    )
    fal_connect_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices("FAL_CONNECT_TIMEOUT_SECONDS", "fal_connect_timeout_seconds"),
        gt=0,
    )
    fal_read_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("FAL_READ_TIMEOUT_SECONDS", "fal_read_timeout_seconds"),
        gt=0,
    )
    fal_write_timeout_seconds: float = Field(
        default=30,
        validation_alias=AliasChoices("FAL_WRITE_TIMEOUT_SECONDS", "fal_write_timeout_seconds"),
        gt=0,
    )
    fal_pool_timeout_seconds: float = Field(
        default=5,
        validation_alias=AliasChoices("FAL_POOL_TIMEOUT_SECONDS", "fal_pool_timeout_seconds"),
        gt=0,
    )
    fal_output_retention_seconds: int = Field(
        default=86_400,
        validation_alias=AliasChoices(
            "FAL_OUTPUT_RETENTION_SECONDS", "fal_output_retention_seconds"
        ),
        gt=0,
    )
    fal_cdn_token_ttl_seconds: int = Field(
        default=900,
        validation_alias=AliasChoices("FAL_CDN_TOKEN_TTL_SECONDS", "fal_cdn_token_ttl_seconds"),
        gt=0,
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
    salad_capacity_expected_fingerprint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "SALAD_CAPACITY_EXPECTED_FINGERPRINT", "salad_capacity_expected_fingerprint"
        ),
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
    runpod_capacity_expected_fingerprint: str | None = Field(
        default=None,
        validation_alias=AliasChoices(
            "RUNPOD_CAPACITY_EXPECTED_FINGERPRINT", "runpod_capacity_expected_fingerprint"
        ),
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
    web_push_vapid_public_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WEB_PUSH_VAPID_PUBLIC_KEY", "web_push_vapid_public_key"),
    )
    web_push_vapid_private_key: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WEB_PUSH_VAPID_PRIVATE_KEY", "web_push_vapid_private_key"),
    )
    web_push_vapid_subject: str | None = Field(
        default=None,
        validation_alias=AliasChoices("WEB_PUSH_VAPID_SUBJECT", "web_push_vapid_subject"),
    )
    web_push_allowed_endpoint_origins: str = Field(
        default="",
        validation_alias=AliasChoices(
            "WEB_PUSH_ALLOWED_ENDPOINT_ORIGINS", "web_push_allowed_endpoint_origins"
        ),
    )
    web_push_send_timeout_seconds: float = Field(
        default=10,
        validation_alias=AliasChoices(
            "WEB_PUSH_SEND_TIMEOUT_SECONDS", "web_push_send_timeout_seconds"
        ),
        gt=0,
        le=60,
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
        if normalized not in {"runpod", "salad", "mock", "node"}:
            raise ValueError("inference provider must be runpod, salad, mock, or node")
        return normalized

    @field_validator("inference_enabled_backends")
    @classmethod
    def validate_backend_list_text(cls, value: str) -> str:
        if not isinstance(value, str):
            raise ValueError("enabled backends must be a comma-separated string")
        values = [item.strip() for item in value.split(",") if item.strip()]
        if not values:
            raise ValueError("at least one inference backend must be enabled")
        if len(set(values)) != len(values):
            raise ValueError("enabled inference backends must not contain duplicates")
        for item in values:
            BackendId(item)
        return ",".join(values)

    @field_validator("default_original_backend", "default_cover_backend")
    @classmethod
    def validate_backend_id_field(cls, value: str) -> str:
        return str(BackendId(value.strip()))

    @field_validator("fal_allowed_media_kinds")
    @classmethod
    def validate_fal_media_kinds(cls, value: str) -> str:
        values = [item.strip() for item in value.split(",") if item.strip()]
        allowed = {item.value for item in MediaKind}
        if (
            not values
            or any(item not in allowed for item in values)
            or len(set(values)) != len(values)
        ):
            raise ValueError("FAL_ALLOWED_MEDIA_KINDS contains an unsupported or duplicate value")
        if any(item in {"sfx", "speech", "utility"} for item in values):
            raise ValueError("pure non-music Fal media kinds are not supported")
        return ",".join(values)

    @field_validator("fal_key")
    @classmethod
    def validate_fal_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        return normalized or None

    @field_validator("fal_catalog_path")
    @classmethod
    def validate_fal_catalog_path(cls, value: Path | None) -> Path | None:
        if value is None:
            return None
        if not value.is_absolute():
            raise ValueError("FAL_CATALOG_PATH must be absolute")
        if value.is_symlink() or not value.is_file():
            raise ValueError("FAL_CATALOG_PATH must be an existing regular file")
        try:
            if value.stat().st_mode & 0o022:
                raise ValueError("FAL_CATALOG_PATH must not be group/world writable")
        except OSError as exc:
            raise ValueError("FAL_CATALOG_PATH cannot be inspected") from exc
        return value

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

    @field_validator("incoming_directory_mode")
    @classmethod
    def validate_incoming_directory_mode(cls, value: str) -> str:
        normalized = value.strip()
        if (
            len(normalized) != 4
            or normalized[0] != "0"
            or any(character not in "01234567" for character in normalized)
            or int(normalized, 8) & 0o007
        ):
            raise ValueError(
                "ACE_SERVICE_INCOMING_DIRECTORY_MODE must be a four-digit octal mode "
                "without world access"
            )
        return normalized

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

    @field_validator("salad_capacity_expected_fingerprint", "runpod_capacity_expected_fingerprint")
    @classmethod
    def validate_capacity_fingerprint(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not _FINGERPRINT_RE.fullmatch(normalized):
            raise ValueError("capacity fingerprint must be 64 lowercase hexadecimal characters")
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

    @field_validator("mock_base_url")
    @classmethod
    def validate_mock_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("mock base URL must be a private HTTP(S) endpoint")
        hostname = parsed.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if not (hostname == "localhost" or hostname.endswith(".ts.net")):
                raise ValueError("mock base URL must resolve to a private p100 endpoint") from None
        else:
            if address.is_global:
                raise ValueError("mock base URL must resolve to a private p100 endpoint")
        return value.rstrip("/")

    @field_validator("ace_node_base_url")
    @classmethod
    def validate_ace_node_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.path not in {"", "/"}
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("ACE_NODE_BASE_URL must be a private HTTP(S) endpoint")
        hostname = parsed.hostname.rstrip(".").lower()
        try:
            address = ipaddress.ip_address(hostname)
        except ValueError:
            if hostname != "localhost" and not hostname.endswith(".ts.net"):
                raise ValueError(
                    "ACE_NODE_BASE_URL must be private or an exact .ts.net host"
                ) from None
        else:
            if address.is_global:
                raise ValueError("ACE_NODE_BASE_URL must not be globally routable")
        return value.rstrip("/")

    @model_validator(mode="after")
    def validate_runtime_values(self) -> ServiceSettings:
        for field_name in _CREDENTIAL_FIELDS:
            value = getattr(self, field_name).strip().lower()
            if value in _PLACEHOLDERS or value.endswith(".example.invalid"):
                raise ValueError(f"{field_name} still contains a configuration placeholder")

        enabled = self.enabled_backend_ids
        # Preserve the old single-provider environment as a migration alias
        # when the new backend settings were not changed.
        explicit_backend_selection = bool(
            self.model_fields_set
            & {"inference_enabled_backends", "default_original_backend", "default_cover_backend"}
        )
        if (
            not explicit_backend_selection
            and self.inference_provider in {"runpod", "salad", "mock", "node"}
            and self.inference_enabled_backends
            == "runpod/ace-step-v15-xl-turbo,salad/ace-step-v15-xl-turbo"
            and self.default_original_backend == "runpod/ace-step-v15-xl-turbo"
            and self.default_cover_backend == "salad/ace-step-v15-xl-turbo"
        ):
            enabled = (
                ("mock/midi-sequential",)
                if self.inference_provider == "mock"
                else (f"{self.inference_provider}/ace-step-v15-xl-turbo",)
            )
            self.inference_enabled_backends = enabled[0]
            self.default_original_backend = enabled[0]
            self.default_cover_backend = enabled[0]
        elif (
            self.inference_provider in {"runpod", "salad", "mock", "node"}
            and len(enabled) == 1
            and enabled[0].split("/", 1)[0] != self.inference_provider
            and enabled[0]
            in {
                "runpod/ace-step-v15-xl-turbo",
                "salad/ace-step-v15-xl-turbo",
            }
            and self.default_original_backend == enabled[0]
            and self.default_cover_backend == enabled[0]
        ):
            enabled = (
                ("mock/midi-sequential",)
                if self.inference_provider == "mock"
                else (f"{self.inference_provider}/ace-step-v15-xl-turbo",)
            )
            self.inference_enabled_backends = enabled[0]
            self.default_original_backend = enabled[0]
            self.default_cover_backend = enabled[0]

        if any(item.startswith("runpod/") for item in enabled):
            for field_name in ("runpod_api_key", "runpod_endpoint_id"):
                value = getattr(self, field_name).strip().lower()
                if value in _PLACEHOLDERS:
                    raise ValueError(f"{field_name} still contains a configuration placeholder")
        if any(item.startswith("salad/") for item in enabled):
            for field_name in ("salad_api_key", "salad_organization", "salad_project"):
                value = getattr(self, field_name)
                if value is None or not value.strip() or value.strip().lower() in _PLACEHOLDERS:
                    raise ValueError(f"{field_name} is required for Salad")
            assert self.salad_organization is not None and self.salad_project is not None
            if not is_salad_resource_name(self.salad_organization) or not is_salad_resource_name(
                self.salad_project
            ):
                raise ValueError("Salad organization and project must be DNS-compatible")

        if any(item.startswith("mock/") for item in enabled):
            self.validate_mock_runtime()

        if any(item.startswith("node/") for item in enabled):
            self.validate_node_runtime()

        if any(item.startswith("fal/") for item in enabled):
            if self.fal_key is None or self.fal_key.lower() in _PLACEHOLDERS:
                raise ValueError("fal_key is required when a Fal backend is enabled")
            if self.transfer_token_ttl_seconds < self.inference_job_timeout_seconds + 600:
                raise ValueError(
                    "transfer_token_ttl_seconds must exceed the inference deadline by ten minutes"
                )
            if self.fal_output_retention_seconds < 86_400:
                raise ValueError("fal_output_retention_seconds is below the recovery window")

        if (
            self.default_original_backend not in enabled
            or self.default_cover_backend not in enabled
        ):
            raise ValueError("mode defaults must be present in INFERENCE_ENABLED_BACKENDS")
        if self.eligible_gpu_ids and self.runpod_worker_runtime_identity is None:
            raise ValueError(
                "runpod_worker_runtime_identity is required when eligible GPU IDs are configured"
            )

        vapid_values = (
            self.web_push_vapid_public_key,
            self.web_push_vapid_private_key,
            self.web_push_vapid_subject,
        )
        if any(value is not None and not value.strip() for value in vapid_values):
            raise ValueError("Web Push VAPID values must not be empty")
        if any(value is not None for value in vapid_values) and not all(vapid_values):
            raise ValueError(
                "Web Push VAPID public key, private key, and subject are required together"
            )
        if self.web_push_vapid_subject is not None:
            subject = self.web_push_vapid_subject.strip()
            parsed_subject = urlsplit(subject)
            if not (
                subject.lower().startswith("mailto:")
                or (parsed_subject.scheme == "https" and parsed_subject.hostname)
            ):
                raise ValueError("WEB_PUSH_VAPID_SUBJECT must be a mailto or HTTPS contact URI")
            self.web_push_vapid_subject = subject
        origins = [
            item.strip().rstrip("/")
            for item in self.web_push_allowed_endpoint_origins.split(",")
            if item.strip()
        ]
        for origin in origins:
            parsed_origin = urlsplit(origin)
            if (
                parsed_origin.scheme != "https"
                or not parsed_origin.hostname
                or parsed_origin.port not in {None, 443}
                or parsed_origin.path not in {"", "/"}
                or parsed_origin.query
                or parsed_origin.fragment
                or parsed_origin.username
                or parsed_origin.password
            ):
                raise ValueError("Web Push endpoint origins must be exact HTTPS origins")
        if any(value is not None for value in vapid_values) and not origins:
            raise ValueError("Web Push requires at least one endpoint origin")
        if origins and not any(value is not None for value in vapid_values):
            raise ValueError("Web Push endpoint origins require VAPID configuration")
        self.web_push_allowed_endpoint_origins = ",".join(origins)

        self.data_root = self.data_root.expanduser().resolve()
        if self.evaluation_campaign_database is not None:
            campaign_database = self.evaluation_campaign_database.expanduser().resolve()
            if not campaign_database.parent.is_relative_to(self.data_root):
                raise ValueError("evaluation campaign database must remain under data_root")
            self.evaluation_campaign_database = campaign_database
        return self

    def validate_mock_runtime(self) -> None:
        """Require private mock credentials when a persisted job needs recovery."""

        if (
            self.mock_token.strip().lower() in _PLACEHOLDERS
            or self.mock_token.strip().lower().endswith(".example.invalid")
        ):
            raise ValueError("mock_token is required when a mock backend is enabled")
        self.mock_base_url = self.validate_mock_base_url(self.mock_base_url)

    def validate_node_runtime(self) -> None:
        """Require private node credentials only when the backend is enabled."""

        token = self.ace_node_token.strip().lower()
        if token in _PLACEHOLDERS or token.endswith(".example.invalid") or not token:
            raise ValueError("ace_node_token is required when the ACE Node backend is enabled")
        self.ace_node_base_url = self.validate_ace_node_base_url(self.ace_node_base_url)

    @property
    def paths(self) -> DataPaths:
        return DataPaths(self.data_root)

    @property
    def enabled_backend_ids(self) -> tuple[str, ...]:
        return tuple(item for item in self.inference_enabled_backends.split(",") if item)

    @property
    def fal_allowed_media_kind_values(self) -> frozenset[str]:
        return frozenset(item for item in self.fal_allowed_media_kinds.split(",") if item)

    @property
    def web_push_enabled(self) -> bool:
        return all(
            value is not None
            for value in (
                self.web_push_vapid_public_key,
                self.web_push_vapid_private_key,
                self.web_push_vapid_subject,
            )
        )

    @property
    def web_push_allowed_origins(self) -> frozenset[str]:
        return frozenset(item for item in self.web_push_allowed_endpoint_origins.split(",") if item)

    @property
    def campaign_database_path(self) -> Path:
        """Return the dedicated, non-product quality campaign database path."""

        return self.evaluation_campaign_database or self.paths.campaign_database

    def ensure_data_layout(self) -> DataPaths:
        """Create the controller directories with private permissions where possible."""

        paths = self.paths
        for directory in paths.all_directories:
            mode = int(self.incoming_directory_mode, 8) if directory == paths.incoming else 0o700
            directory.mkdir(parents=True, exist_ok=True, mode=mode)
            try:
                directory.chmod(mode)
            except (NotImplementedError, PermissionError):
                pass
        return paths
