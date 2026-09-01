"""Runpod Serverless handler for bounded ACE-Step generation metadata."""

from __future__ import annotations

import json
import logging
import math
import os
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .audio_output import finalize_generated_output, internal_audio_format, probe_audio_duration
from .runtime import (
    WorkerInitializationError,
    WorkerRuntime,
    initialize_runtime,
    validate_worker_image_digest,
)
from .schemas import (
    MAX_CAPTION_LENGTH,
    MAX_LYRICS_LENGTH,
    MAX_RESULT_METADATA_BYTES,
    SCHEMA_VERSION,
    WorkerRequest,
)
from .source_audio import prepare_cover_source_to_duration

LOGGER = logging.getLogger(__name__)
_RUNTIME: WorkerRuntime | None = None
_PRIVATE_METADATA_KEYS = frozenset(
    {
        "path",
        "src_audio",
        "source_path",
        "output_path",
        "url",
        "source_url",
        "output_url",
        "capability_url",
    }
)
_LM_METADATA_FIELDS = frozenset(
    {
        "bpm",
        "caption",
        "duration",
        "key_scale",
        "keyscale",
        "lyrics",
        "timesignature",
        "time_signature",
        "vocal_language",
    }
)
_LM_TEXT_FIELDS = frozenset(
    {
        "caption",
        "key_scale",
        "keyscale",
        "lyrics",
        "timesignature",
        "time_signature",
        "vocal_language",
    }
)
_LM_NUMERIC_FIELDS = frozenset({"bpm", "duration"})
_MAX_LM_METADATA_BYTES = 16_384
_PROGRESS_KIND = "audioventura_progress_v1"


class GenerationError(RuntimeError):
    """Raised when ACE-Step did not produce one valid output."""


def configure_runtime(runtime: WorkerRuntime) -> None:
    """Install the process-global runtime only after its signature check."""

    global _RUNTIME
    _RUNTIME = runtime


def clear_runtime(runtime: WorkerRuntime) -> None:
    """Release the installed runtime without clearing a newer replacement."""

    global _RUNTIME
    if _RUNTIME is runtime:
        _RUNTIME = None


def handler(event: Mapping[str, Any]) -> dict[str, Any]:
    """Validate, generate, upload, and return bounded metadata only."""

    started = time.monotonic()
    runtime = _RUNTIME
    if runtime is None:
        raise RuntimeError("worker runtime has not been initialized")
    request: WorkerRequest | None = None
    try:
        allowed_host = _configured_transfer_host()
        request = WorkerRequest.from_event(event, allowed_transfer_host=allowed_host)
        return _handle_request(request, runtime, event)
    except Exception as exc:
        LOGGER.error(
            "job=%s stage=worker error_code=worker_request_failed exception_class=%s elapsed_ms=%d",
            request.job_id if request is not None else "unknown",
            type(exc).__name__,
            int((time.monotonic() - started) * 1000),
        )
        raise


def _handle_request(
    request: WorkerRequest, runtime: WorkerRuntime, event: Mapping[str, Any]
) -> dict[str, Any]:
    started = time.monotonic()
    transfer_client = runtime.transfer_client_factory()
    with tempfile.TemporaryDirectory(prefix="ace-step-") as temporary_root:
        temporary_path = Path(temporary_root)
        source_path: Path | None = None
        if request.source is not None:
            _report_progress(event, "source_download", 10)
            source_path = temporary_path / "source.mp3"
            transfer_client.download_source(request.source, source_path)

        generation_source_path = source_path
        if _requires_custom_cover_source(request):
            if source_path is None:
                raise GenerationError("custom cover is missing its source audio")
            target_duration = _target_duration(request)
            if target_duration is None:
                raise GenerationError("custom cover is missing its target duration")
            generation_source_path = prepare_cover_source_to_duration(
                source_path,
                temporary_path / "prepared-cover-source.wav",
                target_duration_seconds=target_duration,
            )
            LOGGER.info(
                "job=%s stage=source_preparation mode=custom target_duration_seconds=%.3f",
                request.job_id,
                target_duration,
            )

        output_directory = temporary_path / "output"
        output_directory.mkdir(mode=0o700)
        params = _build_generation_params(runtime, request, generation_source_path)
        config = _build_generation_config(runtime, request)
        _report_progress(event, "generation", 20)
        result = runtime.generate_music(
            runtime.dit_handler,
            runtime.llm_handler,
            params,
            config,
            save_dir=str(output_directory),
        )
        _report_progress(event, "finalizing", 30)
        audio = _one_audio(result)
        lm_metadata = _lm_metadata_from_result(result)
        output_format = request.generation.output_format
        internal_format = internal_audio_format(output_format)
        output_path = _safe_generated_path(audio.get("path"), output_directory, internal_format)
        actual_duration = probe_audio_duration(output_path, audio)
        target_duration = _target_duration(request)
        duration_evidence = _duration_evidence(actual_duration, target_duration)
        if target_duration is not None and not duration_evidence["duration_within_tolerance"]:
            LOGGER.warning(
                "job=%s stage=duration_validation actual_duration_seconds=%.3f "
                "target_duration_seconds=%.3f tolerance_seconds=%.3f",
                request.job_id,
                actual_duration,
                target_duration,
                duration_evidence["duration_tolerance_seconds"],
            )
            raise GenerationError("ACE-Step output duration is outside the accepted tolerance")
        output_path = finalize_generated_output(
            output_path,
            requested_format=output_format,
            temporary_root=temporary_path,
        )
        if output_path.suffix.lower().lstrip(".") != output_format:
            raise GenerationError("ACE-Step returned an unexpected output format")
        _report_progress(event, "output_upload", 40)
        uploaded = transfer_client.upload_output(request.result_upload, output_path)
        metadata = _result_metadata(
            request,
            runtime,
            audio,
            uploaded,
            actual_duration=actual_duration,
            target_duration=target_duration,
            duration_evidence=duration_evidence,
            lm_metadata=lm_metadata,
        )
        LOGGER.info(
            "completed job=%s stage=worker variation=%d bytes=%d elapsed_ms=%d",
            request.job_id,
            request.variation_index,
            uploaded.bytes,
            int((time.monotonic() - started) * 1000),
        )
        return metadata


def _report_progress(event: Mapping[str, Any], phase: str, sequence: int) -> None:
    """Publish advisory progress without changing the generation outcome."""

    if not isinstance(event.get("id"), str) or not event["id"]:
        return
    payload = {"kind": _PROGRESS_KIND, "phase": phase, "sequence": sequence}
    try:
        import runpod

        runpod.serverless.progress_update(event, payload)
    except Exception as exc:
        LOGGER.warning(
            "stage=progress phase=%s error_code=progress_update_failed exception_class=%s",
            phase,
            type(exc).__name__,
        )


def _requires_custom_cover_source(request: WorkerRequest) -> bool:
    return (
        request.schema_version == SCHEMA_VERSION
        and request.task_type == "cover"
        and request.generation.duration_mode == "custom"
    )


def _build_generation_params(
    runtime: WorkerRuntime, request: WorkerRequest, source_path: Path | None
) -> Any:
    generation = request.generation
    resolved = request.resolved_parameters or {}
    is_v2 = request.schema_version == SCHEMA_VERSION
    caption = (
        str(resolved["caption"])
        if is_v2
        else generation.prompt
        + ("\nInstrumental only; no vocals." if generation.instrumental else "")
    )
    lyrics = (
        str(resolved["lyrics"]) if is_v2 else ("" if generation.instrumental else generation.lyrics)
    )
    values: dict[str, Any] = {
        "task_type": "cover" if request.task_type == "cover" else "text2music",
        "caption": caption,
        "lyrics": lyrics,
        "instrumental": generation.instrumental,
        "vocal_language": generation.vocal_language,
        "duration": resolved.get("duration", -1.0) if is_v2 else generation.duration or -1.0,
        "bpm": generation.bpm,
        "keyscale": generation.key_scale or "",
        "timesignature": str(generation.time_signature) if generation.time_signature else "",
        "seed": generation.seed if generation.seed is not None else -1,
        "inference_steps": resolved.get("inference_steps", 8),
        "shift": resolved.get("shift", 1.0),
        "thinking": resolved.get("thinking", True) if is_v2 else True,
        "use_cot_metas": resolved.get("use_cot_metas", True) if is_v2 else True,
        "use_cot_caption": resolved.get("use_cot_caption", True) if is_v2 else True,
        "use_cot_language": resolved.get("use_cot_language", True) if is_v2 else True,
        "lm_temperature": resolved.get("lm_temperature", 0.85),
        "lm_cfg_scale": resolved.get("lm_cfg_scale", 2.0),
        "lm_top_k": resolved.get("lm_top_k", 0),
        "lm_top_p": resolved.get("lm_top_p", 0.9),
        "lm_negative_prompt": resolved.get("lm_negative_prompt", "NO USER INPUT"),
        "src_audio": str(source_path) if source_path is not None else None,
        "audio_cover_strength": generation.audio_cover_strength,
        "cover_noise_strength": generation.cover_noise_strength,
    }
    return runtime.generation_params_type(**values)


def _build_generation_config(runtime: WorkerRuntime, request: WorkerRequest) -> Any:
    seed = request.generation.seed
    internal_format = internal_audio_format(request.generation.output_format)
    values: dict[str, Any] = {
        "batch_size": 1,
        "allow_lm_batch": False,
        "use_random_seed": seed is None,
        "seeds": [seed] if seed is not None else None,
        "audio_format": internal_format,
        "mp3_bitrate": "192k",
        "mp3_sample_rate": 48_000,
    }
    return runtime.generation_config_type(**values)


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


def _lm_metadata_from_result(result: Any) -> dict[str, Any]:
    """Extract only the pinned GenerationResult LM metadata field."""

    if isinstance(result, Mapping):
        extra_outputs = result.get("extra_outputs")
    else:
        extra_outputs = getattr(result, "extra_outputs", None)
    if extra_outputs is None:
        return {}
    if not isinstance(extra_outputs, Mapping):
        raise GenerationError("ACE-Step returned malformed extra outputs")
    return _bounded_lm_metadata(extra_outputs.get("lm_metadata"))


def _bounded_lm_metadata(value: Any) -> dict[str, Any]:
    """Keep the bounded, JSON-safe metadata fields produced by the pinned LM."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GenerationError("ACE-Step returned malformed LM metadata")
    result: dict[str, Any] = {}
    for key, child in value.items():
        if not isinstance(key, str) or len(key) > 128:
            raise GenerationError("ACE-Step returned malformed LM metadata")
        if key not in _LM_METADATA_FIELDS:
            continue
        if key in _LM_TEXT_FIELDS:
            if not isinstance(child, str) or len(child) > MAX_LYRICS_LENGTH:
                raise GenerationError("ACE-Step returned oversized LM text metadata")
            if key == "caption" and len(child) > MAX_CAPTION_LENGTH:
                raise GenerationError("ACE-Step returned an oversized LM caption")
            result[key] = child
            continue
        if key in _LM_NUMERIC_FIELDS:
            if isinstance(child, bool) or not isinstance(child, (int, float, str)):
                raise GenerationError("ACE-Step returned malformed LM numeric metadata")
            if isinstance(child, float) and not math.isfinite(child):
                raise GenerationError("ACE-Step returned non-finite LM metadata")
            if isinstance(child, str) and len(child) > 128:
                raise GenerationError("ACE-Step returned oversized LM numeric metadata")
            result[key] = child
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise GenerationError("ACE-Step returned non-JSON LM metadata") from exc
    if len(encoded) > _MAX_LM_METADATA_BYTES:
        raise GenerationError("ACE-Step returned oversized LM metadata")
    return result


def _safe_generated_path(value: Any, output_directory: Path, expected_format: str) -> Path:
    if not isinstance(value, str) or not value:
        raise GenerationError("ACE-Step returned no output path")
    candidate = Path(value)
    resolved_directory = output_directory.resolve()
    resolved_candidate = candidate.resolve()
    if (
        not resolved_candidate.is_relative_to(resolved_directory)
        or candidate.is_symlink()
        or not candidate.is_file()
        or candidate.suffix.lower().lstrip(".") != expected_format
    ):
        raise GenerationError("ACE-Step output path escaped its temporary directory")
    return candidate


def _target_duration(request: WorkerRequest) -> float | None:
    if request.schema_version != SCHEMA_VERSION:
        return None
    value = (request.resolved_parameters or {}).get("target_duration_seconds")
    if value is None:
        value = (request.resolved_parameters or {}).get("duration")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or float(value) <= 0:
        return None
    return float(value)


def _duration_evidence(actual: float, target: float | None) -> dict[str, Any]:
    if target is None:
        return {
            "duration_seconds": actual,
            "target_duration_seconds": None,
            "duration_tolerance_seconds": None,
            "duration_within_tolerance": True,
        }
    tolerance = max(2.0, target * 0.02)
    return {
        "duration_seconds": actual,
        "target_duration_seconds": target,
        "duration_tolerance_seconds": tolerance,
        "duration_within_tolerance": abs(actual - target) <= tolerance,
    }


def _result_metadata(
    request: WorkerRequest,
    runtime: WorkerRuntime,
    audio: Mapping[str, Any],
    uploaded: Any,
    *,
    actual_duration: float,
    target_duration: float | None,
    duration_evidence: Mapping[str, Any],
    lm_metadata: Mapping[str, Any],
) -> dict[str, Any]:
    audio_params = audio.get("params")
    effective_seed = audio_params.get("seed") if isinstance(audio_params, Mapping) else None
    if (
        isinstance(effective_seed, bool)
        or not isinstance(effective_seed, int)
        or effective_seed < 0
    ):
        raise GenerationError("ACE-Step did not return an effective integer seed")
    sample_rate = audio.get("sample_rate")
    if isinstance(sample_rate, bool) or not isinstance(sample_rate, int) or sample_rate <= 0:
        sample_rate = None
    if request.schema_version == SCHEMA_VERSION:
        effective_caption = request.generation.prompt
        if request.generation.prompt_mode in {"enhance", "auto-compose"}:
            generated_caption = lm_metadata.get("caption")
            if generated_caption is not None:
                effective_caption = generated_caption
        effective_lyrics = request.generation.lyrics
        if request.generation.prompt_mode in {"enhance", "auto-compose"} and not effective_lyrics:
            generated_lyrics = lm_metadata.get("lyrics")
            if isinstance(generated_lyrics, str):
                effective_lyrics = generated_lyrics
    else:
        effective_caption = audio.get(
            "caption", audio.get("effective_caption", request.generation.prompt)
        )
        effective_lyrics = audio.get(
            "lyrics", audio.get("effective_lyrics", request.generation.lyrics)
        )
    caption_limit = MAX_CAPTION_LENGTH if request.schema_version == SCHEMA_VERSION else 4_000
    lyrics_limit = MAX_LYRICS_LENGTH if request.schema_version == SCHEMA_VERSION else 20_000
    if not isinstance(effective_caption, str) or len(effective_caption) > caption_limit:
        raise GenerationError("ACE-Step returned an oversized effective caption")
    if not isinstance(effective_lyrics, str) or len(effective_lyrics) > lyrics_limit:
        raise GenerationError("ACE-Step returned oversized effective lyrics")
    requested_seed = request.generation.seed
    resolved_parameters = dict(request.resolved_parameters or {})
    if not resolved_parameters:
        resolved_parameters = {
            "task_type": request.task_type,
            "duration": request.generation.duration
            if request.generation.duration is not None
            else -1.0,
            "audio_cover_strength": request.generation.audio_cover_strength,
            "cover_noise_strength": request.generation.cover_noise_strength,
        }
    generated_metadata = (
        dict(lm_metadata)
        if request.schema_version == SCHEMA_VERSION
        else _bounded_private_mapping(audio.get("metadata", audio.get("generated_metadata")))
    )
    quality_scores = _bounded_private_mapping(audio.get("quality_scores"))
    try:
        image_digest = validate_worker_image_digest(runtime.worker_image_digest)
    except WorkerInitializationError as exc:
        raise GenerationError("worker image identity is missing or malformed") from exc
    result: dict[str, Any] = {
        "schema_version": request.schema_version,
        "job_id": request.job_id,
        "submission_nonce": request.submission_nonce,
        "variation_index": request.variation_index,
        "status": "uploaded",
        "profile_id": request.profile_id,
        "input": {
            "caption": request.generation.prompt,
            "lyrics": request.generation.lyrics,
            "prompt_mode": request.generation.prompt_mode,
            "duration_mode": request.generation.duration_mode,
        },
        "effective": {
            "caption": effective_caption,
            "lyrics": effective_lyrics,
        },
        "resolved_parameters": resolved_parameters,
        "generated_metadata": generated_metadata,
        "output": {
            "format": request.generation.output_format,
            "mime_type": {
                "mp3": "audio/mpeg",
                "flac": "audio/flac",
                "wav": "audio/wav",
            }[request.generation.output_format],
            "bytes": uploaded.bytes,
            "sha256": uploaded.sha256,
            "requested_seed": requested_seed,
            "effective_seed": effective_seed,
            "seed": effective_seed,
            "sample_rate": sample_rate,
            "quality_scores": quality_scores,
            **dict(duration_evidence),
        },
        "worker": {
            "profile_id": request.profile_id,
            "dit_model": runtime.model_name,
            "lm_model": runtime.lm_model_name,
            "runtime_kind": runtime.accelerator_config.accelerator
            if runtime.accelerator_config is not None
            else "cuda",
            "ace_tag": runtime.ace_tag,
            "ace_commit": runtime.ace_commit,
            "image_digest": image_digest,
            "gpu": runtime.gpu_name,
            "vram_bytes": runtime.gpu_vram_bytes,
            "model_bundle": {
                "repo": runtime.model_repo,
                "revision": runtime.model_revision,
                "tag": runtime.model_tag,
                "manifest_sha256": runtime.model_manifest_sha256,
            },
        },
    }
    _validate_result_metadata(result)
    return result


def _bounded_private_mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise GenerationError("ACE-Step returned malformed private metadata")
    result = _sanitize_private_metadata(value, depth=0)
    if not isinstance(result, dict):
        raise GenerationError("ACE-Step returned malformed private metadata")
    try:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise GenerationError("ACE-Step returned non-JSON private metadata") from exc
    if len(encoded) > 16_384:
        raise GenerationError("ACE-Step returned oversized private metadata")
    return result


def _sanitize_private_metadata(value: Any, *, depth: int) -> Any:
    if depth > 5:
        raise GenerationError("ACE-Step returned overly deep private metadata")
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or len(key) > 128:
                raise GenerationError("ACE-Step returned malformed private metadata")
            normalized_key = key.lower()
            if normalized_key in _PRIVATE_METADATA_KEYS or normalized_key.endswith(
                ("_url", "_path")
            ):
                continue
            result[key] = _sanitize_private_metadata(child, depth=depth + 1)
        return result
    if isinstance(value, list):
        if len(value) > 128:
            raise GenerationError("ACE-Step returned oversized private metadata")
        return [_sanitize_private_metadata(child, depth=depth + 1) for child in value]
    if isinstance(value, str):
        if "/transfer/v1/" in value:
            raise GenerationError("worker result metadata must not contain transfer URLs")
        return value
    if isinstance(value, float) and not math.isfinite(value):
        raise GenerationError("ACE-Step returned non-finite private metadata")
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise GenerationError("ACE-Step returned non-JSON private metadata")


def _validate_result_metadata(value: Mapping[str, Any]) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()
    except (TypeError, ValueError) as exc:
        raise GenerationError("worker result metadata is not JSON serializable") from exc
    if len(encoded) > MAX_RESULT_METADATA_BYTES:
        raise GenerationError("worker result metadata is too large")
    if "/transfer/v1/" in encoded.decode("utf-8", errors="ignore"):
        raise GenerationError("worker result metadata must not contain transfer URLs")


def _configured_transfer_host() -> str | None:
    value = os.environ.get("ACE_TRANSFER_ALLOWED_HOST")
    return value.strip() if value and value.strip() else None


def main() -> None:
    """Initialize process-global models, then start the Runpod SDK loop."""

    import runpod

    configure_runtime(initialize_runtime())
    runpod.serverless.start({"handler": handler})


if __name__ == "__main__":
    main()
