from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from runpod_worker.runtime import (
    WorkerInitializationError,
    WorkerRuntime,
    initialize_runtime,
    resolve_checkpoint_paths,
    validate_runtime_signatures,
    validate_worker_image_digest,
)

TEST_IMAGE_DIGEST = "sha256:" + "b" * 64


class CompatibleParams:
    def __init__(
        self,
        *,
        task_type: object,
        caption: object,
        lyrics: object,
        instrumental: object,
        vocal_language: object,
        duration: object,
        bpm: object,
        keyscale: object,
        timesignature: object,
        seed: object,
        inference_steps: object,
        shift: object,
        thinking: object,
        use_cot_metas: object,
        use_cot_caption: object,
        use_cot_language: object,
        src_audio: object,
        audio_cover_strength: object,
        cover_noise_strength: object,
        lm_temperature: object,
        lm_cfg_scale: object,
        lm_top_k: object,
        lm_top_p: object,
        lm_negative_prompt: object,
    ) -> None:
        del (
            task_type,
            caption,
            lyrics,
            instrumental,
            vocal_language,
            duration,
            bpm,
            keyscale,
            timesignature,
            seed,
            inference_steps,
            shift,
            thinking,
            use_cot_metas,
            use_cot_caption,
            use_cot_language,
            src_audio,
            audio_cover_strength,
            cover_noise_strength,
            lm_temperature,
            lm_cfg_scale,
            lm_top_k,
            lm_top_p,
            lm_negative_prompt,
        )
        pass


class CompatibleConfig:
    def __init__(
        self,
        *,
        batch_size: object,
        allow_lm_batch: object,
        use_random_seed: object,
        seeds: object,
        audio_format: object,
        mp3_bitrate: object,
        mp3_sample_rate: object,
    ) -> None:
        del batch_size, allow_lm_batch, use_random_seed, seeds
        del audio_format, mp3_bitrate, mp3_sample_rate
        pass


def test_handler_dependency_chain_imports_as_package() -> None:
    handler_module = importlib.import_module("runpod_worker.handler")

    assert handler_module.__package__ == "runpod_worker"


def test_checkpoint_resolution_fails_closed_when_weights_are_missing(tmp_path: Path) -> None:
    with pytest.raises(WorkerInitializationError, match="required ACE-Step checkpoints"):
        resolve_checkpoint_paths(tmp_path)


def test_checkpoint_resolution_accepts_all_required_components(tmp_path: Path) -> None:
    for component in ("acestep-v15-xl-turbo", "acestep-5Hz-lm-1.7B", "Qwen3-Embedding-0.6B", "vae"):
        component_path = tmp_path / component
        component_path.mkdir()
        (component_path / "model.safetensors").write_bytes(b"test")

    paths = resolve_checkpoint_paths(tmp_path)

    assert paths.root == tmp_path.resolve()
    assert paths.dit.name == "acestep-v15-xl-turbo"
    assert paths.lm.name == "acestep-5Hz-lm-1.7B"


def test_incompatible_ace_constructor_is_rejected_before_handling() -> None:
    class IncompatibleParams:
        def __init__(self, *, task_type: str) -> None:
            self.task_type = task_type

    class CompatibleConfig:
        def __init__(
            self,
            *,
            batch_size: int,
            allow_lm_batch: bool,
            use_random_seed: bool,
            seeds: list[int] | None,
            audio_format: str,
            mp3_bitrate: str,
            mp3_sample_rate: int,
        ) -> None:
            del batch_size, allow_lm_batch, use_random_seed, seeds
            del audio_format, mp3_bitrate, mp3_sample_rate

    with pytest.raises(WorkerInitializationError, match="missing fields"):
        validate_runtime_signatures(IncompatibleParams, CompatibleConfig)


@pytest.mark.parametrize(
    "value", [None, "", "latest", "sha256:not-a-digest", " sha256:" + "a" * 64]
)
def test_worker_image_digest_rejects_missing_or_mutable_identity(value: object) -> None:
    with pytest.raises(WorkerInitializationError, match="image digest"):
        validate_worker_image_digest(value)


def test_worker_image_digest_accepts_immutable_digest_reference() -> None:
    assert validate_worker_image_digest(TEST_IMAGE_DIGEST) == TEST_IMAGE_DIGEST
    assert (
        validate_worker_image_digest("ghcr.io/example/ace-worker@" + TEST_IMAGE_DIGEST)
        == "ghcr.io/example/ace-worker@" + TEST_IMAGE_DIGEST
    )


def _make_runtime(image_digest: str) -> WorkerRuntime:
    return WorkerRuntime(
        dit_handler=object(),
        llm_handler=object(),
        generation_params_type=CompatibleParams,
        generation_config_type=CompatibleConfig,
        generate_music=_generate_music,
        gpu_name="test-gpu",
        gpu_vram_bytes=1,
        worker_image_digest=image_digest,
    )


def _generate_music(*_args: object, **_kwargs: object) -> None:
    return None


def test_worker_runtime_requires_image_identity() -> None:
    with pytest.raises(WorkerInitializationError, match="image digest"):
        _make_runtime("")
    runtime = _make_runtime(TEST_IMAGE_DIGEST)
    assert runtime.worker_image_digest == TEST_IMAGE_DIGEST


def test_initialize_runtime_requires_image_identity_before_model_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("ACE_WORKER_IMAGE_DIGEST", raising=False)
    with pytest.raises(WorkerInitializationError, match="image digest"):
        initialize_runtime()
