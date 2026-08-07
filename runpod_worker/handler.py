"""Runpod Serverless handler for metadata-only ACE-Step jobs."""

from __future__ import annotations

import inspect
import logging
import os
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .runtime import WorkerRuntime, initialize_runtime
from .schemas import WorkerRequest

LOGGER = logging.getLogger(__name__)
_RUNTIME: WorkerRuntime | None = None


class GenerationError(RuntimeError):
    """Raised when ACE-Step did not produce one valid output."""


def configure_runtime(runtime: WorkerRuntime) -> None:
    """Install the process-global runtime before accepting handler calls."""

    global _RUNTIME
    _RUNTIME = runtime


def handler(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate, generate, upload, and return bounded metadata only."""

    runtime = _RUNTIME
    if runtime is None:
        raise RuntimeError("worker runtime has not been initialized")
    allowed_host = _configured_transfer_host()
    request = WorkerRequest.from_event(event, allowed_transfer_host=allowed_host)
    return _handle_request(request, runtime)


def _handle_request(request: WorkerRequest, runtime: WorkerRuntime) -> dict[str, Any]:
    transfer_client = runtime.transfer_client_factory()
    with tempfile.TemporaryDirectory(prefix="ace-step-") as temporary_root:
        temporary_path = Path(temporary_root)
        source_path: Path | None = None
        if request.source is not None:
            source_path = temporary_path / "source.mp3"
            transfer_client.download_source(request.source, source_path)

        output_directory = temporary_path / "output"
        output_directory.mkdir(mode=0o700)
        params = _build_generation_params(runtime, request, source_path)
        config = _build_generation_config(runtime, request)
        result = runtime.generate_music(
            runtime.dit_handler,
            runtime.llm_handler,
            params,
            config,
            save_dir=str(output_directory),
        )
        audio = _one_audio(result)
        output_path = _safe_generated_path(audio.get("path"), output_directory)
        output_format = request.generation.output_format
        if output_path.suffix.lower().lstrip(".") != output_format:
            raise GenerationError("ACE-Step returned an unexpected output format")
        uploaded = transfer_client.upload_output(request.result_upload, output_path)
        metadata = _small_result_metadata(request, runtime, audio, uploaded)
        LOGGER.info(
            "completed job=%s variation=%d bytes=%d",
            request.job_id,
            request.variation_index,
            uploaded.bytes,
        )
        return metadata


def _build_generation_params(
    runtime: WorkerRuntime, request: WorkerRequest, source_path: Path | None
) -> Any:
    generation = request.generation
    caption = generation.prompt
    if generation.instrumental:
        caption = f"{caption}\nInstrumental only; no vocals."
    values: dict[str, Any] = {
        "task_type": "cover" if request.task_type == "cover" else "text2music",
        "caption": caption,
        "lyrics": "" if generation.instrumental else generation.lyrics,
        "instrumental": generation.instrumental,
        "vocal_language": generation.vocal_language,
        "duration": generation.duration if generation.duration is not None else -1.0,
        "bpm": generation.bpm,
        "keyscale": generation.key_scale or "",
        "timesignature": str(generation.time_signature) if generation.time_signature else "",
        "seed": generation.seed if generation.seed is not None else -1,
        "thinking": True,
        "use_format": True,
        "src_audio": str(source_path) if source_path is not None else None,
        "audio_cover_strength": generation.cover_strength,
    }
    return runtime.generation_params_type(
        **_supported_values(runtime.generation_params_type, values)
    )


def _build_generation_config(runtime: WorkerRuntime, request: WorkerRequest) -> Any:
    seed = request.generation.seed
    values: dict[str, Any] = {
        "batch_size": 1,
        "allow_lm_batch": False,
        "use_random_seed": seed is None,
        "seeds": [seed] if seed is not None else None,
        "audio_format": request.generation.output_format,
        "mp3_bitrate": "192k",
        "mp3_sample_rate": 48_000,
    }
    return runtime.generation_config_type(
        **_supported_values(runtime.generation_config_type, values)
    )


def _supported_values(callable_type: Any, values: dict[str, Any]) -> dict[str, Any]:
    parameters = inspect.signature(callable_type).parameters.values()
    if any(parameter.kind is inspect.Parameter.VAR_KEYWORD for parameter in parameters):
        return values
    accepted = set(inspect.signature(callable_type).parameters)
    return {key: value for key, value in values.items() if key in accepted}


def _one_audio(result: Any) -> Mapping[str, Any]:
    if isinstance(result, Mapping):
        success = result.get("success", False)
        audios = result.get("audios", [])
    else:
        success = getattr(result, "success", False)
        audios = getattr(result, "audios", [])
    if not success:
        raise GenerationError("ACE-Step generation failed")
    if not isinstance(audios, list) or len(audios) != 1 or not isinstance(audios[0], Mapping):
        raise GenerationError("ACE-Step did not return exactly one output")
    return audios[0]


def _safe_generated_path(value: Any, output_directory: Path) -> Path:
    if not isinstance(value, str) or not value:
        raise GenerationError("ACE-Step returned no output path")
    candidate = Path(value)
    resolved_directory = output_directory.resolve()
    resolved_candidate = candidate.resolve()
    if (
        not resolved_candidate.is_relative_to(resolved_directory)
        or candidate.is_symlink()
        or not candidate.is_file()
    ):
        raise GenerationError("ACE-Step output path escaped its temporary directory")
    return candidate


def _small_result_metadata(
    request: WorkerRequest, runtime: WorkerRuntime, audio: Mapping[str, Any], uploaded: Any
) -> dict[str, Any]:
    audio_params = audio.get("params")
    seed = (
        audio_params.get("seed") if isinstance(audio_params, Mapping) else request.generation.seed
    )
    if not isinstance(seed, int):
        seed = None
    sample_rate = audio.get("sample_rate")
    if not isinstance(sample_rate, int):
        sample_rate = None
    return {
        "schema_version": request.schema_version,
        "job_id": request.job_id,
        "submission_nonce": request.submission_nonce,
        "variation_index": request.variation_index,
        "status": "uploaded",
        "output": {
            "format": request.generation.output_format,
            "mime_type": {
                "mp3": "audio/mpeg",
                "flac": "audio/flac",
                "wav": "audio/wav",
            }[request.generation.output_format],
            "bytes": uploaded.bytes,
            "sha256": uploaded.sha256,
            "seed": seed,
            "sample_rate": sample_rate,
        },
        "worker": {
            "model": runtime.model_name,
            "gpu": runtime.gpu_name,
            "vram_bytes": runtime.gpu_vram_bytes,
        },
    }


def _configured_transfer_host() -> str | None:
    value = os.environ.get("ACE_TRANSFER_ALLOWED_HOST")
    return value.strip() if value and value.strip() else None


def main() -> None:
    """Initialize process-global models, then start the Runpod SDK loop."""

    import runpod  # type: ignore[import-not-found]

    configure_runtime(initialize_runtime())
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":
    main()
