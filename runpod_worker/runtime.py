"""Process-global ACE-Step initialization for the Runpod worker."""

from __future__ import annotations

import hashlib
import importlib
import inspect
import json
import logging
import os
import platform
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from .transfer_client import TransferClient

LOGGER = logging.getLogger(__name__)
DIT_MODEL = "acestep-v15-xl-turbo"
LM_MODEL = "acestep-5Hz-lm-1.7B"
EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
VAE_MODEL = "vae"
DEFAULT_HF_CACHE_ROOT = "/runpod-volume/huggingface-cache/hub"
MODEL_BUNDLE_ID = "audioventura-ace-step-v0.1.8"
MODEL_BUNDLE_TOTAL_BYTES = 25_253_680_505
MODEL_BUNDLE_MANIFEST = "bundle-manifest.json"
MAX_MODEL_MANIFEST_BYTES = 1_048_576
ACE_SOURCE_REPOSITORY = "https://github.com/ace-step/ACE-Step-1.5.git"
ACE_SOURCE_TAG = "v0.1.8"
ACE_SOURCE_COMMIT = "dce621408bee8c31b4fcf4811682eb9359e1bc94"
MODEL_SOURCES = {
    "ACE-Step/Ace-Step1.5": "19671f406d603126926c1b7e2adc169acbcade22",
    "ACE-Step/acestep-v15-xl-turbo": "d4a0b288b83ebb7e25a8c0b32c573c22e134e8ee",
}
REQUIRED_MODEL_DIRECTORIES = (
    f"checkpoints/{EMBEDDING_MODEL}",
    f"checkpoints/{LM_MODEL}",
    f"checkpoints/{DIT_MODEL}",
    f"checkpoints/{VAE_MODEL}",
)
_IMAGE_DIGEST_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}@)?sha256:[0-9a-f]{64}$")
_MODEL_REPO_PART_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,94}[A-Za-z0-9])?$")
_MODEL_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")
_MODEL_TAG_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9._-]{0,126}[A-Za-z0-9])?$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class WorkerInitializationError(RuntimeError):
    """Raised when the worker cannot initialize its pinned model set."""


@dataclass(frozen=True, slots=True)
class AcceleratorConfig:
    """Immutable accelerator choices made before loading ACE-Step.

    The worker intentionally has no implicit CPU fallback.  Keeping this
    decision in a small value object makes the cloud CUDA and Apple Silicon
    paths inspectable without importing the heavyweight runtime in the
    controller process.
    """

    accelerator: str
    device: str
    lm_backend: str
    use_mlx_dit: bool
    compile_model: bool
    offload_to_cpu: bool
    offload_dit_to_cpu: bool
    quantization: bool
    memory_bytes: int
    gpu_name: str

    @property
    def torch_compile(self) -> bool:
        """Compatibility spelling used by node/runtime callers."""

        return self.compile_model


def _torch_module(torch_module: Any | None) -> Any:
    if torch_module is not None:
        return torch_module
    try:
        return importlib.import_module("torch")
    except ImportError as exc:
        raise WorkerInitializationError("pinned PyTorch runtime is not installed") from exc


def _cuda_memory_bytes(torch_module: Any) -> int:
    try:
        properties = torch_module.cuda.get_device_properties(0)
        value = getattr(properties, "total_memory", None)
        if isinstance(value, int) and value > 0:
            return value
    except Exception:
        pass
    try:
        available, total = torch_module.cuda.mem_get_info(0)
        del available
        if isinstance(total, int) and total > 0:
            return total
    except Exception:
        pass
    return 0


def _mps_memory_bytes(torch_module: Any) -> int:
    mps = getattr(torch_module, "mps", None)
    for name in ("recommended_max_memory", "driver_allocated_memory", "current_allocated_memory"):
        try:
            value = getattr(mps, name)()
        except Exception:
            continue
        if isinstance(value, int) and value > 0:
            return value
    return 0


def select_accelerator(
    accelerator: str | None = None,
    *,
    system: str | None = None,
    machine: str | None = None,
    torch_module: Any | None = None,
) -> AcceleratorConfig:
    """Select exactly one supported CUDA or Apple Silicon MPS runtime.

    ``torch`` is imported only when this function is called, which keeps the
    normal controller environment free of GPU/runtime dependencies.
    """

    requested = (accelerator or os.environ.get("ACE_NODE_ACCELERATOR", "auto")).strip().lower()
    if requested not in {"auto", "cuda", "mps"}:
        raise WorkerInitializationError("ACE_NODE_ACCELERATOR must be auto, cuda, or mps")
    detected_system = system or platform.system()
    detected_machine = (machine or platform.machine()).lower()
    if detected_system == "Linux" and detected_machine in {"x86_64", "amd64"}:
        if requested == "mps":
            raise WorkerInitializationError("MPS is supported only on macOS arm64")
        torch = _torch_module(torch_module)
        cuda = getattr(torch, "cuda", None)
        if cuda is None or not bool(cuda.is_available()):
            raise WorkerInitializationError("CUDA is required; refusing CPU inference")
        try:
            device_count = int(cuda.device_count())
        except Exception as exc:
            raise WorkerInitializationError("CUDA device count could not be measured") from exc
        if device_count != 1:
            raise WorkerInitializationError("exactly one CUDA GPU is required")
        try:
            gpu_name = str(cuda.get_device_name(0))
        except Exception:
            gpu_name = "NVIDIA CUDA"
        return AcceleratorConfig(
            accelerator="cuda",
            device="cuda",
            lm_backend="vllm",
            use_mlx_dit=False,
            compile_model=False,
            offload_to_cpu=False,
            offload_dit_to_cpu=False,
            quantization=False,
            memory_bytes=_cuda_memory_bytes(torch),
            gpu_name=gpu_name,
        )
    if detected_system == "Darwin" and detected_machine in {"arm64", "aarch64"}:
        if requested == "cuda":
            raise WorkerInitializationError("CUDA is supported only on Linux x86_64")
        torch = _torch_module(torch_module)
        backends = getattr(torch, "backends", None)
        mps_backend = getattr(backends, "mps", None)
        if mps_backend is None or not bool(mps_backend.is_available()):
            raise WorkerInitializationError(
                "Apple Silicon MPS is unavailable; refusing CPU inference"
            )
        return AcceleratorConfig(
            accelerator="mps",
            device="mps",
            lm_backend="mlx",
            use_mlx_dit=True,
            compile_model=False,
            offload_to_cpu=False,
            offload_dit_to_cpu=False,
            quantization=False,
            memory_bytes=_mps_memory_bytes(torch),
            gpu_name="Apple Silicon MPS",
        )
    raise WorkerInitializationError(
        "unsupported ACE Node platform; require Linux x86_64 or macOS arm64"
    )


# Descriptive alias for callers that prefer the longer name.
AcceleratorConfiguration = AcceleratorConfig
resolve_accelerator = select_accelerator


def compute_local_runtime_receipt(
    application_revision: str, lock_path: str | Path = "uv.lock"
) -> str:
    """Derive an immutable node deployment receipt from commit and lockfile."""

    if not isinstance(application_revision, str) or not re.fullmatch(
        r"[0-9a-f]{40}", application_revision
    ):
        raise WorkerInitializationError("node application revision must be an exact commit SHA")
    path = Path(lock_path)
    if path.is_symlink() or not path.is_file():
        raise WorkerInitializationError("node runtime lockfile is missing")
    digest = hashlib.sha256()
    digest.update(b"audioventura-ace-node-runtime-receipt-v1\0")
    digest.update(application_revision.encode("ascii"))
    digest.update(b"\0")
    try:
        digest.update(path.read_bytes())
    except OSError as exc:
        raise WorkerInitializationError("node runtime lockfile is unreadable") from exc
    return "sha256:" + digest.hexdigest()


runtime_receipt = compute_local_runtime_receipt


class TransferClientLike(Protocol):
    """Minimal transfer surface needed by the handler and test doubles."""

    def download_source(self, source: Any, destination: Path) -> Any: ...

    def upload_output(self, upload: Any, source_path: Path) -> Any: ...


@dataclass(frozen=True, slots=True)
class CheckpointPaths:
    root: Path
    dit: Path
    lm: Path
    embedding: Path
    vae: Path


@dataclass(frozen=True, slots=True)
class WorkerRuntime:
    """All process-global dependencies used by one handler process."""

    dit_handler: Any
    llm_handler: Any
    generation_params_type: type[Any]
    generation_config_type: type[Any]
    generate_music: Callable[..., Any]
    gpu_name: str
    gpu_vram_bytes: int
    model_repo: str
    model_revision: str
    model_tag: str
    model_manifest_sha256: str
    model_name: str = DIT_MODEL
    lm_model_name: str = LM_MODEL
    ace_tag: str = field(default_factory=lambda: os.environ.get("ACE_STEP_TAG", "v0.1.8"))
    ace_commit: str = field(default_factory=lambda: os.environ.get("ACE_STEP_COMMIT", ""))
    worker_image_digest: str = field(
        default_factory=lambda: os.environ.get("ACE_WORKER_IMAGE_DIGEST", "")
    )
    accelerator_config: AcceleratorConfig | None = None
    transfer_client_factory: Callable[[], TransferClientLike] = field(default=TransferClient)

    def __post_init__(self) -> None:
        validate_runtime_signatures(self.generation_params_type, self.generation_config_type)
        validate_worker_image_digest(self.worker_image_digest)
        validate_model_repo(self.model_repo)
        validate_model_revision(self.model_revision)
        validate_model_tag(self.model_tag)
        validate_model_manifest_sha256(self.model_manifest_sha256)
        if self.accelerator_config is None:
            object.__setattr__(
                self,
                "accelerator_config",
                AcceleratorConfig(
                    accelerator="cuda",
                    device="cuda",
                    lm_backend="vllm",
                    use_mlx_dit=False,
                    compile_model=False,
                    offload_to_cpu=False,
                    offload_dit_to_cpu=False,
                    quantization=False,
                    memory_bytes=self.gpu_vram_bytes,
                    gpu_name=self.gpu_name,
                ),
            )


_REQUIRED_GENERATION_PARAMS = frozenset(
    {
        "task_type",
        "caption",
        "lyrics",
        "instrumental",
        "vocal_language",
        "duration",
        "bpm",
        "keyscale",
        "timesignature",
        "seed",
        "inference_steps",
        "shift",
        "thinking",
        "use_cot_metas",
        "use_cot_caption",
        "use_cot_language",
        "src_audio",
        "audio_cover_strength",
        "cover_noise_strength",
        "lm_temperature",
        "lm_cfg_scale",
        "lm_top_k",
        "lm_top_p",
        "lm_negative_prompt",
    }
)
_REQUIRED_GENERATION_CONFIG = frozenset(
    {
        "batch_size",
        "allow_lm_batch",
        "use_random_seed",
        "seeds",
        "audio_format",
        "mp3_bitrate",
        "mp3_sample_rate",
    }
)


def validate_runtime_signatures(
    generation_params_type: type[Any], generation_config_type: type[Any]
) -> None:
    """Fail closed when a pinned ACE constructor cannot receive our values."""

    _validate_callable_signature(
        generation_params_type, _REQUIRED_GENERATION_PARAMS, "GenerationParams"
    )
    _validate_callable_signature(
        generation_config_type, _REQUIRED_GENERATION_CONFIG, "GenerationConfig"
    )


def validate_worker_image_digest(value: Any) -> str:
    """Require an immutable OCI image digest for reproducible worker results."""

    if (
        not isinstance(value, str)
        or value != value.strip()
        or not 7 <= len(value) <= 512
        or not _IMAGE_DIGEST_RE.fullmatch(value)
    ):
        raise WorkerInitializationError(
            "ACE_WORKER_IMAGE_DIGEST must be a non-empty sha256 image digest"
        )
    return value


def validate_model_repo(value: Any) -> str:
    """Require one unambiguous Hugging Face ``owner/repo`` identifier."""

    if not isinstance(value, str) or value != value.strip() or value.count("/") != 1:
        raise WorkerInitializationError("ACE_WORKER_MODEL_REPO must be an owner/repo identifier")
    owner, repo = value.split("/", 1)
    if not _MODEL_REPO_PART_RE.fullmatch(owner) or not _MODEL_REPO_PART_RE.fullmatch(repo):
        raise WorkerInitializationError("ACE_WORKER_MODEL_REPO must be an owner/repo identifier")
    if "--" in owner or "--" in repo or ".." in owner or ".." in repo:
        raise WorkerInitializationError("ACE_WORKER_MODEL_REPO is ambiguous in the cache layout")
    return value


def validate_model_revision(value: Any) -> str:
    if not isinstance(value, str) or not _MODEL_REVISION_RE.fullmatch(value):
        raise WorkerInitializationError("ACE_WORKER_MODEL_REVISION must be 40 lowercase hex")
    return value


def validate_model_tag(value: Any) -> str:
    if (
        not isinstance(value, str)
        or value != value.strip()
        or not _MODEL_TAG_RE.fullmatch(value)
        or ".." in value
    ):
        raise WorkerInitializationError("ACE_WORKER_MODEL_TAG is malformed")
    return value


def validate_model_manifest_sha256(value: Any) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise WorkerInitializationError("ACE_WORKER_MODEL_MANIFEST_SHA256 must be 64 lowercase hex")
    return value


def _validate_callable_signature(
    callable_type: type[Any], required: frozenset[str], label: str
) -> None:
    try:
        parameters = inspect.signature(callable_type).parameters
    except (TypeError, ValueError) as exc:
        raise WorkerInitializationError(f"cannot inspect {label} signature") from exc
    missing = sorted(required - set(parameters))
    positional_only = sorted(
        name
        for name in required
        if name in parameters and parameters[name].kind is inspect.Parameter.POSITIONAL_ONLY
    )
    if missing or positional_only:
        details: list[str] = []
        if missing:
            details.append("missing fields: " + ", ".join(missing))
        if positional_only:
            details.append("not keyword-capable: " + ", ".join(positional_only))
        raise WorkerInitializationError(
            f"incompatible {label} signature (" + "; ".join(details) + ")"
        )


def resolve_checkpoint_paths(cache_root: str | Path | None = None) -> CheckpointPaths:
    """Validate and resolve only the configured immutable cached-model snapshot."""

    model_repo = validate_model_repo(os.environ.get("ACE_WORKER_MODEL_REPO"))
    revision = validate_model_revision(os.environ.get("ACE_WORKER_MODEL_REVISION"))
    validate_model_tag(os.environ.get("ACE_WORKER_MODEL_TAG"))
    manifest_sha256 = validate_model_manifest_sha256(
        os.environ.get("ACE_WORKER_MODEL_MANIFEST_SHA256")
    )
    cache = Path(
        cache_root or os.environ.get("ACE_WORKER_HF_CACHE_ROOT", DEFAULT_HF_CACHE_ROOT)
    ).expanduser()
    if not cache.is_absolute():
        raise WorkerInitializationError("ACE_WORKER_HF_CACHE_ROOT must be absolute")
    cache = cache.resolve()
    owner, repo = model_repo.split("/", 1)
    model_cache_root = cache / f"models--{owner}--{repo}"
    snapshot = model_cache_root / "snapshots" / revision
    if snapshot.is_symlink() or not snapshot.is_dir():
        raise WorkerInitializationError("required cached model revision is missing")
    _validate_model_manifest(snapshot, model_cache_root, manifest_sha256)
    root = snapshot / "checkpoints"
    paths = CheckpointPaths(
        root=root,
        dit=root / DIT_MODEL,
        lm=root / LM_MODEL,
        embedding=root / EMBEDDING_MODEL,
        vae=root / VAE_MODEL,
    )
    missing = [
        str(path) for path in (paths.dit, paths.lm, paths.embedding, paths.vae) if not path.is_dir()
    ]
    if missing:
        raise WorkerInitializationError(
            "required ACE-Step checkpoints are missing: " + ", ".join(missing)
        )
    return paths


def initialize_runtime(cache_root: str | Path | None = None) -> WorkerRuntime:
    """Initialize ACE-Step once before Runpod starts accepting jobs."""

    image_digest = validate_worker_image_digest(os.environ.get("ACE_WORKER_IMAGE_DIGEST"))
    accelerator = select_accelerator()
    LOGGER.info(
        "worker accelerator=%s device=%s gpu=%s memory_bytes=%d",
        accelerator.accelerator,
        accelerator.device,
        accelerator.gpu_name,
        accelerator.memory_bytes,
    )

    paths = resolve_checkpoint_paths(cache_root)
    os.environ["ACESTEP_CHECKPOINTS_DIR"] = str(paths.root)

    try:
        handler_module = importlib.import_module("acestep.handler")
        inference_module = importlib.import_module("acestep.inference")
        llm_module = importlib.import_module("acestep.llm_inference")
        AceStepHandler = handler_module.AceStepHandler
        GenerationConfig = inference_module.GenerationConfig
        GenerationParams = inference_module.GenerationParams
        generate_music = inference_module.generate_music
        LLMHandler = llm_module.LLMHandler
    except ImportError as exc:
        raise WorkerInitializationError("pinned ACE-Step runtime is not installed") from exc

    validate_runtime_signatures(GenerationParams, GenerationConfig)

    dit_handler = AceStepHandler()
    dit_status, dit_ok = dit_handler.initialize_service(
        project_root=str(paths.root.parent),
        config_path=DIT_MODEL,
        device=accelerator.device,
        use_flash_attention=False,
        compile_model=accelerator.compile_model,
        offload_to_cpu=accelerator.offload_to_cpu,
        offload_dit_to_cpu=accelerator.offload_dit_to_cpu,
        use_mlx_dit=accelerator.use_mlx_dit,
    )
    if not dit_ok:
        raise WorkerInitializationError("ACE-Step DiT initialization failed")
    LOGGER.info("ACE-Step DiT initialized model=%s", DIT_MODEL)

    llm_handler = LLMHandler()
    llm_status, llm_ok = llm_handler.initialize(
        checkpoint_dir=str(paths.root),
        lm_model_path=LM_MODEL,
        backend=accelerator.lm_backend,
        device=accelerator.device,
        offload_to_cpu=accelerator.offload_to_cpu,
    )
    if not llm_ok:
        raise WorkerInitializationError("ACE-Step 5Hz LM initialization failed")
    LOGGER.info("ACE-Step LM initialized model=%s backend=vllm", LM_MODEL)
    del dit_status, llm_status

    return WorkerRuntime(
        dit_handler=dit_handler,
        llm_handler=llm_handler,
        generation_params_type=GenerationParams,
        generation_config_type=GenerationConfig,
        generate_music=generate_music,
        gpu_name=accelerator.gpu_name,
        gpu_vram_bytes=accelerator.memory_bytes,
        model_repo=validate_model_repo(os.environ.get("ACE_WORKER_MODEL_REPO")),
        model_revision=validate_model_revision(os.environ.get("ACE_WORKER_MODEL_REVISION")),
        model_tag=validate_model_tag(os.environ.get("ACE_WORKER_MODEL_TAG")),
        model_manifest_sha256=validate_model_manifest_sha256(
            os.environ.get("ACE_WORKER_MODEL_MANIFEST_SHA256")
        ),
        ace_tag=os.environ.get("ACE_STEP_TAG", "v0.1.8"),
        ace_commit=os.environ.get("ACE_STEP_COMMIT", ""),
        worker_image_digest=image_digest,
        accelerator_config=accelerator,
    )


def _validate_model_manifest(snapshot: Path, model_cache_root: Path, expected_sha256: str) -> None:
    manifest_path = snapshot / MODEL_BUNDLE_MANIFEST
    manifest_bytes = _read_cache_file(manifest_path, model_cache_root, MAX_MODEL_MANIFEST_BYTES)
    if hashlib.sha256(manifest_bytes).hexdigest() != expected_sha256:
        raise WorkerInitializationError("cached model manifest digest does not match")
    try:
        manifest = json.loads(manifest_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise WorkerInitializationError("cached model manifest is malformed") from exc
    if not isinstance(manifest, dict):
        raise WorkerInitializationError("cached model manifest is malformed")
    required_keys = {
        "schema_version",
        "bundle_id",
        "ace_step_source",
        "sources",
        "components",
        "files",
        "total_bytes",
        "required_directories",
        "created_at",
    }
    if set(manifest) != required_keys:
        raise WorkerInitializationError("cached model manifest fields do not match the contract")
    if manifest["schema_version"] != 1 or manifest["bundle_id"] != MODEL_BUNDLE_ID:
        raise WorkerInitializationError("cached model manifest identity does not match")
    if manifest["ace_step_source"] != {
        "repository": ACE_SOURCE_REPOSITORY,
        "tag": ACE_SOURCE_TAG,
        "commit": ACE_SOURCE_COMMIT,
    }:
        raise WorkerInitializationError("cached model ACE-Step source identity does not match")
    _validate_manifest_sources(manifest["sources"])
    _validate_manifest_components(manifest["components"])
    if manifest["required_directories"] != list(REQUIRED_MODEL_DIRECTORIES):
        raise WorkerInitializationError("cached model required directories do not match")
    if manifest["total_bytes"] != MODEL_BUNDLE_TOTAL_BYTES:
        raise WorkerInitializationError("cached model total bytes do not match")
    created_at = manifest["created_at"]
    if not isinstance(created_at, str) or not created_at.endswith("Z"):
        raise WorkerInitializationError("cached model creation timestamp is malformed")
    try:
        created_at_value = datetime.fromisoformat(created_at.removesuffix("Z") + "+00:00")
    except ValueError as exc:
        raise WorkerInitializationError("cached model creation timestamp is malformed") from exc
    if created_at_value.tzinfo is None or created_at_value.astimezone(UTC) != created_at_value:
        raise WorkerInitializationError("cached model creation timestamp is malformed")
    expected_files = _validate_manifest_files(manifest["files"])
    actual_files = _inventory_checkpoint_files(snapshot, model_cache_root)
    if actual_files != expected_files:
        raise WorkerInitializationError(
            "cached model file inventory does not match: "
            + _bounded_inventory_diff(expected_files, actual_files)
        )
    if sum(actual_files.values()) != MODEL_BUNDLE_TOTAL_BYTES:
        raise WorkerInitializationError("cached model file sizes do not match total bytes")


def _validate_manifest_sources(value: Any) -> None:
    if not isinstance(value, list) or len(value) != len(MODEL_SOURCES):
        raise WorkerInitializationError("cached model sources do not match")
    parsed: dict[str, str] = {}
    for source in value:
        if not isinstance(source, dict) or set(source) != {"repo_id", "revision"}:
            raise WorkerInitializationError("cached model source entry is malformed")
        repo_id = source.get("repo_id")
        revision = source.get("revision")
        if not isinstance(repo_id, str) or not isinstance(revision, str):
            raise WorkerInitializationError("cached model source entry is malformed")
        parsed[repo_id] = revision
    if parsed != MODEL_SOURCES:
        raise WorkerInitializationError("cached model sources do not match")


def _bounded_inventory_diff(expected: dict[str, int], actual: dict[str, int]) -> str:
    """Describe cache-shape drift without emitting an unbounded worker log."""

    limit = 5
    missing = sorted(expected.keys() - actual.keys())
    unexpected = sorted(actual.keys() - expected.keys())
    different = sorted(
        path for path in expected.keys() & actual.keys() if expected[path] != actual[path]
    )

    def sample(paths: list[str]) -> str:
        values = paths[:limit]
        suffix = f",...(+{len(paths) - limit})" if len(paths) > limit else ""
        return ",".join(values) + suffix

    size_sample = different[:limit]
    sizes = ",".join(f"{path}={expected[path]}!={actual[path]}" for path in size_sample)
    if len(different) > limit:
        sizes += f",...(+{len(different) - limit})"
    return (
        f"missing[{len(missing)}]={sample(missing)}; "
        f"unexpected[{len(unexpected)}]={sample(unexpected)}; "
        f"sizes[{len(different)}]={sizes}"
    )


def _validate_manifest_components(value: Any) -> None:
    expected = {
        "embedding": (REQUIRED_MODEL_DIRECTORIES[0], "ACE-Step/Ace-Step1.5", EMBEDDING_MODEL),
        "language_model": (REQUIRED_MODEL_DIRECTORIES[1], "ACE-Step/Ace-Step1.5", LM_MODEL),
        "dit": (REQUIRED_MODEL_DIRECTORIES[2], "ACE-Step/acestep-v15-xl-turbo", "."),
        "vae": (REQUIRED_MODEL_DIRECTORIES[3], "ACE-Step/Ace-Step1.5", VAE_MODEL),
    }
    if not isinstance(value, list) or len(value) != len(expected):
        raise WorkerInitializationError("cached model components do not match")
    parsed: dict[str, tuple[str, str, str]] = {}
    keys = {
        "name",
        "destination_directory",
        "source_repo_id",
        "source_revision",
        "source_path",
    }
    for component in value:
        if not isinstance(component, dict) or set(component) != keys:
            raise WorkerInitializationError("cached model component entry is malformed")
        name = component.get("name")
        repo_id = component.get("source_repo_id")
        revision = component.get("source_revision")
        destination = component.get("destination_directory")
        source_path = component.get("source_path")
        if (
            not isinstance(name, str)
            or not isinstance(repo_id, str)
            or not isinstance(revision, str)
            or not isinstance(destination, str)
            or not isinstance(source_path, str)
        ):
            raise WorkerInitializationError("cached model component entry is malformed")
        if MODEL_SOURCES.get(repo_id) != revision:
            raise WorkerInitializationError("cached model component source does not match")
        parsed[name] = (destination, repo_id, source_path)
    if parsed != expected:
        raise WorkerInitializationError("cached model components do not match")


def _validate_manifest_files(value: Any) -> dict[str, int]:
    if not isinstance(value, list) or not value:
        raise WorkerInitializationError("cached model file inventory is malformed")
    result: dict[str, int] = {}
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "object_identity"}:
            raise WorkerInitializationError("cached model file entry is malformed")
        path = entry.get("path")
        size = entry.get("size")
        identity = entry.get("object_identity")
        if (
            not isinstance(path, str)
            or not path.startswith("checkpoints/")
            or path.startswith("/")
            or "\\" in path
            or ".." in Path(path).parts
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size <= 0
            or not isinstance(identity, str)
            or not 1 <= len(identity) <= 512
            or not identity.startswith(("lfs-sha256:", "xet:", "git-blob:"))
        ):
            raise WorkerInitializationError("cached model file entry is malformed")
        if path in result:
            raise WorkerInitializationError("cached model file inventory contains duplicates")
        result[path] = size
    return result


def _inventory_checkpoint_files(snapshot: Path, model_cache_root: Path) -> dict[str, int]:
    checkpoints = snapshot / "checkpoints"
    if checkpoints.is_symlink() or not checkpoints.is_dir():
        raise WorkerInitializationError("cached model checkpoints directory is missing")
    result: dict[str, int] = {}
    for directory, child_directories, filenames in os.walk(checkpoints, followlinks=False):
        directory_path = Path(directory)
        for child in child_directories:
            if (directory_path / child).is_symlink():
                raise WorkerInitializationError("cached model contains a directory symlink")
        for filename in filenames:
            candidate = directory_path / filename
            relative = candidate.relative_to(snapshot).as_posix()
            result[relative] = _cache_file_size(candidate, model_cache_root)
    return result


def _cache_file_size(path: Path, model_cache_root: Path) -> int:
    try:
        resolved_root = model_cache_root.resolve(strict=True)
        resolved = path.resolve(strict=True)
        if not resolved.is_relative_to(resolved_root) or not resolved.is_file():
            raise WorkerInitializationError("cached model file escapes its model cache")
        return resolved.stat().st_size
    except WorkerInitializationError:
        raise
    except OSError as exc:
        raise WorkerInitializationError("cached model contains a broken file link") from exc


def _read_cache_file(path: Path, model_cache_root: Path, maximum_bytes: int) -> bytes:
    size = _cache_file_size(path, model_cache_root)
    if size > maximum_bytes:
        raise WorkerInitializationError("cached model manifest is too large")
    try:
        return path.resolve(strict=True).read_bytes()
    except OSError as exc:
        raise WorkerInitializationError("cached model contains a broken file link") from exc
