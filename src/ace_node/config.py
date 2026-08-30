"""Typed configuration for one private ACE Node process."""

from __future__ import annotations

import ipaddress
import re
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from runpod_worker.runtime import (
    ACE_SOURCE_COMMIT,
    DEFAULT_HF_CACHE_ROOT,
    validate_model_manifest_sha256,
    validate_model_repo,
    validate_model_revision,
    validate_model_tag,
)

_PLACEHOLDERS = frozenset({"change-me", "changeme", "replace-me", "replace_me", "example.invalid"})
_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_PINNED_MODEL_REPO = "evrenesat/audioventura-ace-step-v0.1.8"
_PINNED_MODEL_REVISION = "88b8c7fa089446b53382c1040037492463430bed"
_PINNED_MODEL_TAG = "av-v0.1.8-bundle-2"
_PINNED_MODEL_MANIFEST_SHA256 = "39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc"
_PINNED_TRANSFER_HOST = "player.evren.io"


class NodeSettings(BaseSettings):
    """Environment-backed node settings with no model/runtime imports."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="",
        extra="ignore",
        case_sensitive=True,
        populate_by_name=True,
    )

    listen_host: str = Field(
        default="127.0.0.1",
        validation_alias=AliasChoices("ACE_NODE_LISTEN_HOST", "listen_host"),
    )
    listen_port: int = Field(
        default=8210,
        validation_alias=AliasChoices("ACE_NODE_LISTEN_PORT", "listen_port"),
        ge=1,
        le=65535,
    )
    token: str = Field(
        default="change-me",
        validation_alias=AliasChoices("ACE_NODE_TOKEN", "token"),
        min_length=1,
    )
    data_root: Path = Field(
        default=Path("/var/lib/audioventura/ace-node"),
        validation_alias=AliasChoices("ACE_NODE_DATA_ROOT", "data_root"),
    )
    accelerator: str = Field(
        default="auto",
        validation_alias=AliasChoices("ACE_NODE_ACCELERATOR", "accelerator"),
    )
    transfer_allowed_host: str = Field(
        default="player.evren.io",
        validation_alias=AliasChoices("ACE_TRANSFER_ALLOWED_HOST", "transfer_allowed_host"),
        min_length=1,
    )
    worker_hf_cache_root: Path = Field(
        default=Path(DEFAULT_HF_CACHE_ROOT),
        validation_alias=AliasChoices("ACE_WORKER_HF_CACHE_ROOT", "worker_hf_cache_root"),
    )
    worker_model_repo: str = Field(
        default="evrenesat/audioventura-ace-step-v0.1.8",
        validation_alias=AliasChoices("ACE_WORKER_MODEL_REPO", "worker_model_repo"),
    )
    worker_model_revision: str = Field(
        default="88b8c7fa089446b53382c1040037492463430bed",
        validation_alias=AliasChoices("ACE_WORKER_MODEL_REVISION", "worker_model_revision"),
    )
    worker_model_tag: str = Field(
        default="av-v0.1.8-bundle-2",
        validation_alias=AliasChoices("ACE_WORKER_MODEL_TAG", "worker_model_tag"),
    )
    worker_model_manifest_sha256: str = Field(
        default="39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc",
        validation_alias=AliasChoices(
            "ACE_WORKER_MODEL_MANIFEST_SHA256", "worker_model_manifest_sha256"
        ),
    )
    job_timeout_seconds: int = Field(
        default=1800,
        validation_alias=AliasChoices("ACE_NODE_JOB_TIMEOUT_SECONDS", "job_timeout_seconds"),
        gt=0,
    )
    max_output_bytes: int = Field(
        default=268_435_456,
        validation_alias=AliasChoices("ACE_NODE_MAX_OUTPUT_BYTES", "max_output_bytes"),
        gt=0,
        le=268_435_456,
    )
    application_revision: str = Field(
        default="",
        validation_alias=AliasChoices("ACE_NODE_APPLICATION_REVISION", "application_revision"),
    )
    runtime_receipt: str | None = Field(
        default=None,
        validation_alias=AliasChoices("ACE_NODE_RUNTIME_RECEIPT", "runtime_receipt"),
    )
    runtime_lock_path: Path = Field(
        default=Path("deploy/node/uv.lock"),
        validation_alias=AliasChoices("ACE_NODE_RUNTIME_LOCK_PATH", "runtime_lock_path"),
    )
    request_max_bytes: int = 65_536

    @field_validator("listen_host")
    @classmethod
    def validate_listen_host(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or normalized in {"0.0.0.0", "::", "::0"}:
            raise ValueError("ACE_NODE_LISTEN_HOST must be a private, non-wildcard address")
        try:
            address = ipaddress.ip_address(normalized)
        except ValueError:
            # Hostnames are permitted for a manually managed private address;
            # operators must bind the exact private interface in deployment.
            if any(character.isspace() for character in normalized):
                raise ValueError("ACE_NODE_LISTEN_HOST is malformed") from None
        else:
            if address.is_global:
                raise ValueError("ACE_NODE_LISTEN_HOST must not be globally routable")
        return normalized

    @field_validator("accelerator")
    @classmethod
    def validate_accelerator(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"auto", "cuda", "mps"}:
            raise ValueError("ACE_NODE_ACCELERATOR must be auto, cuda, or mps")
        return normalized

    @field_validator("token")
    @classmethod
    def validate_token(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("ACE_NODE_TOKEN must not be empty")
        return normalized

    @field_validator("transfer_allowed_host")
    @classmethod
    def validate_transfer_host(cls, value: str) -> str:
        normalized = value.strip().rstrip(".").lower()
        if not normalized or "/" in normalized or any(char.isspace() for char in normalized):
            raise ValueError("ACE_TRANSFER_ALLOWED_HOST is malformed")
        if normalized != _PINNED_TRANSFER_HOST:
            raise ValueError("ACE_TRANSFER_ALLOWED_HOST must be player.evren.io")
        return normalized

    @field_validator("worker_hf_cache_root")
    @classmethod
    def validate_cache_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("ACE_WORKER_HF_CACHE_ROOT must be absolute")
        return value.expanduser().resolve()

    @field_validator("data_root")
    @classmethod
    def validate_data_root(cls, value: Path) -> Path:
        if not value.is_absolute():
            raise ValueError("ACE_NODE_DATA_ROOT must be absolute")
        return value.expanduser().resolve()

    @field_validator("worker_model_repo")
    @classmethod
    def validate_repo(cls, value: str) -> str:
        normalized = validate_model_repo(value)
        if normalized != _PINNED_MODEL_REPO:
            raise ValueError("ACE_WORKER_MODEL_REPO must be the pinned ACE-Step bundle")
        return normalized

    @field_validator("worker_model_revision")
    @classmethod
    def validate_revision(cls, value: str) -> str:
        normalized = validate_model_revision(value)
        if normalized != _PINNED_MODEL_REVISION:
            raise ValueError("ACE_WORKER_MODEL_REVISION must be the pinned ACE-Step bundle")
        return normalized

    @field_validator("worker_model_tag")
    @classmethod
    def validate_tag(cls, value: str) -> str:
        normalized = validate_model_tag(value)
        if normalized != _PINNED_MODEL_TAG:
            raise ValueError("ACE_WORKER_MODEL_TAG must be the pinned ACE-Step bundle")
        return normalized

    @field_validator("worker_model_manifest_sha256")
    @classmethod
    def validate_manifest(cls, value: str) -> str:
        normalized = validate_model_manifest_sha256(value)
        if normalized != _PINNED_MODEL_MANIFEST_SHA256:
            raise ValueError("ACE_WORKER_MODEL_MANIFEST_SHA256 must be the pinned ACE-Step bundle")
        return normalized

    @field_validator("application_revision")
    @classmethod
    def validate_application_revision(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized and not _REVISION_RE.fullmatch(normalized):
            raise ValueError("ACE_NODE_APPLICATION_REVISION must be an exact commit SHA")
        return normalized

    @field_validator("runtime_receipt")
    @classmethod
    def validate_runtime_receipt(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        normalized = value.strip().lower()
        if not _SHA256_RE.fullmatch(normalized):
            raise ValueError("ACE_NODE_RUNTIME_RECEIPT must be an exact sha256 digest")
        return normalized

    @field_validator("runtime_lock_path")
    @classmethod
    def validate_lock_path(cls, value: Path) -> Path:
        if not value.is_absolute():
            # The deployment launcher resolves this against its clean checkout.
            return value
        return value.expanduser().resolve()

    @property
    def database_path(self) -> Path:
        return self.data_root / "node.sqlite3"

    @property
    def model_repo(self) -> str:
        return self.worker_model_repo

    @property
    def model_revision(self) -> str:
        return self.worker_model_revision

    @property
    def model_tag(self) -> str:
        return self.worker_model_tag

    @property
    def model_manifest_sha256(self) -> str:
        return self.worker_model_manifest_sha256

    def require_token(self) -> str:
        """Return the bearer token only when the service has a real secret."""

        if self.token.lower() in _PLACEHOLDERS or self.token.lower().endswith(".example.invalid"):
            raise ValueError("ACE_NODE_TOKEN must be a non-placeholder bearer token")
        return self.token

    def ensure_data_layout(self) -> Path:
        self.data_root = self.data_root.expanduser().resolve()
        self.data_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.data_root.chmod(0o700)
        except (NotImplementedError, PermissionError):
            pass
        return self.data_root

    def model_environment(self) -> dict[str, str]:
        """Return only immutable runtime configuration for the worker process."""

        return {
            "ACE_WORKER_MODEL_REPO": self.worker_model_repo,
            "ACE_WORKER_MODEL_REVISION": self.worker_model_revision,
            "ACE_WORKER_MODEL_TAG": self.worker_model_tag,
            "ACE_WORKER_MODEL_MANIFEST_SHA256": self.worker_model_manifest_sha256,
            "ACE_WORKER_HF_CACHE_ROOT": str(self.worker_hf_cache_root),
            "ACE_TRANSFER_ALLOWED_HOST": self.transfer_allowed_host,
            "ACE_NODE_ACCELERATOR": self.accelerator,
            "ACE_STEP_COMMIT": ACE_SOURCE_COMMIT,
            "ACE_STEP_TAG": "v0.1.8",
            "ACESTEP_CHECKPOINTS_DIR": str(
                self.worker_hf_cache_root
                / f"models--{self.worker_model_repo.replace('/', '--')}"
                / "snapshots"
                / self.worker_model_revision
                / "checkpoints"
            ),
            "ACE_WORKER_MAX_OUTPUT_BYTES": str(self.max_output_bytes),
        }
