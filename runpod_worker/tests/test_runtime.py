from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from runpod_worker.runtime import WorkerInitializationError, resolve_checkpoint_paths


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
