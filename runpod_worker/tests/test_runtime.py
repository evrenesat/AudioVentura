from __future__ import annotations

import hashlib
import importlib
import json
from pathlib import Path

import pytest

from runpod_worker.runtime import (
    ACE_SOURCE_COMMIT,
    ACE_SOURCE_REPOSITORY,
    ACE_SOURCE_TAG,
    MODEL_BUNDLE_ID,
    MODEL_BUNDLE_TOTAL_BYTES,
    MODEL_SOURCES,
    REQUIRED_MODEL_DIRECTORIES,
    WorkerInitializationError,
    WorkerRuntime,
    initialize_runtime,
    resolve_checkpoint_paths,
    validate_runtime_signatures,
    validate_worker_image_digest,
)

TEST_IMAGE_DIGEST = "sha256:" + "b" * 64
TEST_MODEL_REPO = "evrenesat/audioventura-ace-step-v0.1.8"
TEST_MODEL_REVISION = "6f196b2c116474c43a96fc8331ebcd2057e18eef"
TEST_MODEL_TAG = "av-v0.1.8-bundle-1"
TEST_MANIFEST_SHA256 = "c" * 64


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


def test_worker_image_sets_offline_mode_before_runtime_command() -> None:
    dockerfile = Path("runpod_worker/Dockerfile").read_text()
    command_offset = dockerfile.index('CMD ["python", "-m", "runpod_worker.handler"]')
    assert dockerfile.index("HF_HUB_OFFLINE=1") < command_offset
    assert dockerfile.index("TRANSFORMERS_OFFLINE=1") < command_offset
    assert "ACE_WORKER_CHECKPOINTS_DIR" not in dockerfile
    assert "/runpod-volume/checkpoints" not in dockerfile


def _manifest(files: list[dict[str, object]]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "bundle_id": MODEL_BUNDLE_ID,
        "ace_step_source": {
            "repository": ACE_SOURCE_REPOSITORY,
            "tag": ACE_SOURCE_TAG,
            "commit": ACE_SOURCE_COMMIT,
        },
        "sources": [
            {"repo_id": repo_id, "revision": revision}
            for repo_id, revision in MODEL_SOURCES.items()
        ],
        "components": [
            {
                "name": "embedding",
                "destination_directory": "checkpoints/Qwen3-Embedding-0.6B",
                "source_repo_id": "ACE-Step/Ace-Step1.5",
                "source_revision": MODEL_SOURCES["ACE-Step/Ace-Step1.5"],
                "source_path": "Qwen3-Embedding-0.6B",
            },
            {
                "name": "language_model",
                "destination_directory": "checkpoints/acestep-5Hz-lm-1.7B",
                "source_repo_id": "ACE-Step/Ace-Step1.5",
                "source_revision": MODEL_SOURCES["ACE-Step/Ace-Step1.5"],
                "source_path": "acestep-5Hz-lm-1.7B",
            },
            {
                "name": "dit",
                "destination_directory": "checkpoints/acestep-v15-xl-turbo",
                "source_repo_id": "ACE-Step/acestep-v15-xl-turbo",
                "source_revision": MODEL_SOURCES["ACE-Step/acestep-v15-xl-turbo"],
                "source_path": ".",
            },
            {
                "name": "vae",
                "destination_directory": "checkpoints/vae",
                "source_repo_id": "ACE-Step/Ace-Step1.5",
                "source_revision": MODEL_SOURCES["ACE-Step/Ace-Step1.5"],
                "source_path": "vae",
            },
        ],
        "files": files,
        "total_bytes": MODEL_BUNDLE_TOTAL_BYTES,
        "required_directories": list(REQUIRED_MODEL_DIRECTORIES),
        "created_at": "2026-08-22T12:00:00Z",
    }


def _cached_bundle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, dict[str, object]]:
    model_root = tmp_path / "models--evrenesat--audioventura-ace-step-v0.1.8"
    snapshot = model_root / "snapshots" / TEST_MODEL_REVISION
    sizes = [1, 1, 1, MODEL_BUNDLE_TOTAL_BYTES - 3]
    files: list[dict[str, object]] = []
    for directory, size in zip(REQUIRED_MODEL_DIRECTORIES, sizes, strict=True):
        path = snapshot / directory / "model.safetensors"
        path.parent.mkdir(parents=True)
        with path.open("wb") as checkpoint:
            checkpoint.truncate(size)
        files.append(
            {
                "path": f"{directory}/model.safetensors",
                "size": size,
                "object_identity": f"lfs-sha256:{'a' * 64}",
            }
        )
    manifest = _manifest(files)
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (snapshot / "bundle-manifest.json").write_bytes(manifest_bytes)
    monkeypatch.setenv("ACE_WORKER_MODEL_REPO", TEST_MODEL_REPO)
    monkeypatch.setenv("ACE_WORKER_MODEL_REVISION", TEST_MODEL_REVISION)
    monkeypatch.setenv("ACE_WORKER_MODEL_TAG", TEST_MODEL_TAG)
    monkeypatch.setenv(
        "ACE_WORKER_MODEL_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest()
    )
    return snapshot, manifest


def _rewrite_manifest(
    monkeypatch: pytest.MonkeyPatch, snapshot: Path, manifest: dict[str, object]
) -> None:
    manifest_bytes = (
        json.dumps(manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    (snapshot / "bundle-manifest.json").write_bytes(manifest_bytes)
    monkeypatch.setenv(
        "ACE_WORKER_MODEL_MANIFEST_SHA256", hashlib.sha256(manifest_bytes).hexdigest()
    )


def test_checkpoint_resolution_accepts_exact_cached_revision(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, _ = _cached_bundle(monkeypatch, tmp_path)

    paths = resolve_checkpoint_paths(tmp_path)

    assert paths.root == snapshot / "checkpoints"
    assert paths.dit.name == "acestep-v15-xl-turbo"
    assert paths.lm.name == "acestep-5Hz-lm-1.7B"


def test_checkpoint_resolution_rejects_missing_revision_even_with_another_snapshot(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cached_bundle(monkeypatch, tmp_path)
    monkeypatch.setenv("ACE_WORKER_MODEL_REVISION", "f" * 40)
    with pytest.raises(WorkerInitializationError, match="revision is missing"):
        resolve_checkpoint_paths(tmp_path)


def test_checkpoint_resolution_does_not_follow_mutable_ref(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, _ = _cached_bundle(monkeypatch, tmp_path)
    refs = snapshot.parent.parent / "refs"
    refs.mkdir()
    (refs / "main").write_text(TEST_MODEL_REVISION)
    monkeypatch.setenv("ACE_WORKER_MODEL_REVISION", "f" * 40)
    with pytest.raises(WorkerInitializationError, match="revision is missing"):
        resolve_checkpoint_paths(tmp_path)


@pytest.mark.parametrize("case", ["missing", "oversized", "malformed", "wrong_sha"])
def test_checkpoint_resolution_rejects_invalid_manifest(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, _ = _cached_bundle(monkeypatch, tmp_path)
    path = snapshot / "bundle-manifest.json"
    if case == "missing":
        path.unlink()
    elif case == "oversized":
        path.write_bytes(b"x" * (1_048_576 + 1))
    elif case == "malformed":
        path.write_bytes(b"{")
        monkeypatch.setenv("ACE_WORKER_MODEL_MANIFEST_SHA256", hashlib.sha256(b"{").hexdigest())
    else:
        monkeypatch.setenv("ACE_WORKER_MODEL_MANIFEST_SHA256", "f" * 64)
    with pytest.raises(WorkerInitializationError):
        resolve_checkpoint_paths(tmp_path)


@pytest.mark.parametrize("case", ["extra_file", "wrong_size", "traversal", "missing_component"])
def test_checkpoint_resolution_rejects_inventory_drift(
    case: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, manifest = _cached_bundle(monkeypatch, tmp_path)
    files = manifest["files"]
    assert isinstance(files, list)
    if case == "extra_file":
        (snapshot / "checkpoints" / "vae" / "extra.bin").write_bytes(b"x")
    elif case == "wrong_size":
        first = files[0]
        assert isinstance(first, dict)
        first["size"] = 2
        _rewrite_manifest(monkeypatch, snapshot, manifest)
    elif case == "traversal":
        first = files[0]
        assert isinstance(first, dict)
        first["path"] = "checkpoints/../escape.bin"
        _rewrite_manifest(monkeypatch, snapshot, manifest)
    else:
        directories = manifest["required_directories"]
        assert isinstance(directories, list)
        directories.pop()
        _rewrite_manifest(monkeypatch, snapshot, manifest)
    with pytest.raises(WorkerInitializationError):
        resolve_checkpoint_paths(tmp_path)


def test_checkpoint_resolution_rejects_broken_and_escaping_symlinks(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    snapshot, _ = _cached_bundle(monkeypatch, tmp_path)
    candidate = snapshot / "checkpoints" / "vae" / "model.safetensors"
    candidate.unlink()
    candidate.symlink_to(tmp_path / "missing")
    with pytest.raises(WorkerInitializationError, match="broken"):
        resolve_checkpoint_paths(tmp_path)

    outside = tmp_path.parent / "outside-model.bin"
    outside.write_bytes(b"x")
    candidate.unlink()
    candidate.symlink_to(outside)
    with pytest.raises(WorkerInitializationError, match="escapes"):
        resolve_checkpoint_paths(tmp_path)


def test_legacy_network_volume_layout_is_not_a_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    volume = tmp_path / "checkpoints"
    for directory in REQUIRED_MODEL_DIRECTORIES:
        path = volume / Path(directory).name
        path.mkdir(parents=True)
        (path / "model.safetensors").write_bytes(b"x")
    monkeypatch.setenv("ACE_WORKER_MODEL_REPO", TEST_MODEL_REPO)
    monkeypatch.setenv("ACE_WORKER_MODEL_REVISION", TEST_MODEL_REVISION)
    monkeypatch.setenv("ACE_WORKER_MODEL_TAG", TEST_MODEL_TAG)
    monkeypatch.setenv("ACE_WORKER_MODEL_MANIFEST_SHA256", TEST_MANIFEST_SHA256)
    with pytest.raises(WorkerInitializationError, match="revision is missing"):
        resolve_checkpoint_paths(tmp_path)


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
        model_repo=TEST_MODEL_REPO,
        model_revision=TEST_MODEL_REVISION,
        model_tag=TEST_MODEL_TAG,
        model_manifest_sha256=TEST_MANIFEST_SHA256,
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
