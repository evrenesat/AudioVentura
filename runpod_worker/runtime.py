"""Process-global ACE-Step initialization for the Runpod worker."""

from __future__ import annotations

import logging
import os
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
    transfer_client_factory: Callable[[], TransferClientLike] = field(default=TransferClient)


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
    )


def _has_weights(path: Path) -> bool:
    if not path.is_dir():
        return False
    return any(
        candidate.is_file()
        and candidate.suffix.lower() in {".safetensors", ".bin", ".pt", ".index"}
        for candidate in path.rglob("*")
    )
