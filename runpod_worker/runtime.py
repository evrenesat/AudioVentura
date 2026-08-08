"""Process-global ACE-Step initialization for the Runpod worker."""

from __future__ import annotations

import inspect
import logging
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from .transfer_client import TransferClient

LOGGER = logging.getLogger(__name__)
DIT_MODEL = "acestep-v15-xl-turbo"
LM_MODEL = "acestep-5Hz-lm-1.7B"
EMBEDDING_MODEL = "Qwen3-Embedding-0.6B"
VAE_MODEL = "vae"
DEFAULT_CHECKPOINTS_DIR = "/runpod-volume/checkpoints"
_IMAGE_DIGEST_RE = re.compile(r"^(?:[A-Za-z0-9][A-Za-z0-9._:/-]{0,254}@)?sha256:[0-9a-f]{64}$")


class WorkerInitializationError(RuntimeError):
    """Raised when the worker cannot initialize its pinned model set."""


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
    model_name: str = DIT_MODEL
    lm_model_name: str = LM_MODEL
    ace_tag: str = field(default_factory=lambda: os.environ.get("ACE_STEP_TAG", "v0.1.8"))
    ace_commit: str = field(default_factory=lambda: os.environ.get("ACE_STEP_COMMIT", ""))
    worker_image_digest: str = field(
        default_factory=lambda: os.environ.get("ACE_WORKER_IMAGE_DIGEST", "")
    )
    transfer_client_factory: Callable[[], TransferClientLike] = field(default=TransferClient)

    def __post_init__(self) -> None:
        validate_runtime_signatures(self.generation_params_type, self.generation_config_type)
        validate_worker_image_digest(self.worker_image_digest)


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


def resolve_checkpoint_paths(checkpoints_dir: str | Path | None = None) -> CheckpointPaths:
    """Resolve and fail closed when any pinned checkpoint directory is absent."""

    root = (
        Path(
            checkpoints_dir or os.environ.get("ACE_WORKER_CHECKPOINTS_DIR", DEFAULT_CHECKPOINTS_DIR)
        )
        .expanduser()
        .resolve()
    )
    paths = CheckpointPaths(
        root=root,
        dit=root / DIT_MODEL,
        lm=root / LM_MODEL,
        embedding=root / EMBEDDING_MODEL,
        vae=root / VAE_MODEL,
    )
    missing = [
        str(path)
        for path in (paths.dit, paths.lm, paths.embedding, paths.vae)
        if not _has_weights(path)
    ]
    if missing:
        raise WorkerInitializationError(
            "required ACE-Step checkpoints are missing: " + ", ".join(missing)
        )
    return paths


def initialize_runtime(checkpoints_dir: str | Path | None = None) -> WorkerRuntime:
    """Initialize ACE-Step once before Runpod starts accepting jobs."""

    image_digest = validate_worker_image_digest(os.environ.get("ACE_WORKER_IMAGE_DIGEST"))
    import torch  # type: ignore[import-not-found]

    if not torch.cuda.is_available():
        raise WorkerInitializationError("CUDA is required; refusing CPU inference")
    device_name = str(torch.cuda.get_device_name(0))
    device_properties = torch.cuda.get_device_properties(0)
    vram_bytes = int(device_properties.total_memory)
    LOGGER.info("CUDA worker GPU=%s vram_bytes=%d", device_name, vram_bytes)

    paths = resolve_checkpoint_paths(checkpoints_dir)
    os.environ["ACESTEP_CHECKPOINTS_DIR"] = str(paths.root)

    try:
        from acestep.handler import AceStepHandler  # type: ignore[import-not-found]
        from acestep.inference import (  # type: ignore[import-not-found]
            GenerationConfig,
            GenerationParams,
            generate_music,
        )
        from acestep.llm_inference import LLMHandler  # type: ignore[import-not-found]
    except ImportError as exc:
        raise WorkerInitializationError("pinned ACE-Step runtime is not installed") from exc

    validate_runtime_signatures(GenerationParams, GenerationConfig)

    dit_handler = AceStepHandler()
    dit_status, dit_ok = dit_handler.initialize_service(
        project_root=str(paths.root.parent),
        config_path=DIT_MODEL,
        device="cuda",
        use_flash_attention=False,
        compile_model=False,
        offload_to_cpu=False,
        offload_dit_to_cpu=False,
        use_mlx_dit=False,
    )
    if not dit_ok:
        raise WorkerInitializationError("ACE-Step DiT initialization failed")
    LOGGER.info("ACE-Step DiT initialized model=%s", DIT_MODEL)

    llm_handler = LLMHandler()
    llm_status, llm_ok = llm_handler.initialize(
        checkpoint_dir=str(paths.root),
        lm_model_path=LM_MODEL,
        backend="vllm",
        device="cuda",
        offload_to_cpu=False,
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
        gpu_name=device_name,
        gpu_vram_bytes=vram_bytes,
        ace_tag=os.environ.get("ACE_STEP_TAG", "v0.1.8"),
        ace_commit=os.environ.get("ACE_STEP_COMMIT", ""),
        worker_image_digest=image_digest,
    )


def _has_weights(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(
        candidate.is_file()
        and candidate.suffix.lower() in {".safetensors", ".bin", ".pt", ".index"}
        for candidate in path.rglob("*")
    )
