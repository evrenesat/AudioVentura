from __future__ import annotations

import types
from pathlib import Path

import pytest

from ace_node.config import NodeSettings
from ace_node.worker import AceStepNodeRuntime
from runpod_worker.runtime import (
    WorkerInitializationError,
    compute_local_runtime_receipt,
    select_accelerator,
)


class _Cuda:
    def is_available(self) -> bool:
        return True

    def device_count(self) -> int:
        return 1

    def get_device_name(self, _index: int) -> str:
        return "Test NVIDIA"

    def get_device_properties(self, _index: int) -> types.SimpleNamespace:
        return types.SimpleNamespace(total_memory=24 * 1024**3)


class _Mps:
    def is_available(self) -> bool:
        return True


def test_linux_cuda_selection_is_explicit_and_no_offload() -> None:
    config = select_accelerator(
        system="Linux",
        machine="x86_64",
        torch_module=types.SimpleNamespace(cuda=_Cuda()),
    )
    assert config.device == "cuda"
    assert config.accelerator == "cuda"
    assert config.lm_backend == "vllm"
    assert config.use_mlx_dit is False
    assert config.offload_to_cpu is False
    assert config.memory_bytes == 24 * 1024**3


def test_macos_arm64_selects_mlx_without_cpu_fallback() -> None:
    torch = types.SimpleNamespace(
        backends=types.SimpleNamespace(mps=_Mps()),
        mps=types.SimpleNamespace(recommended_max_memory=lambda: 16 * 1024**3),
    )
    config = select_accelerator(system="Darwin", machine="arm64", torch_module=torch)
    assert config.device == "mps"
    assert config.accelerator == "mps"
    assert config.lm_backend == "mlx"
    assert config.use_mlx_dit is True
    assert config.memory_bytes == 16 * 1024**3


def test_macos_mps_selection_defaults_to_safe_vae_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACESTEP_MLX_VAE_CHUNK", raising=False)
    torch = types.SimpleNamespace(
        backends=types.SimpleNamespace(mps=_Mps()),
        mps=types.SimpleNamespace(recommended_max_memory=lambda: 16 * 1024**3),
    )
    select_accelerator(system="Darwin", machine="arm64", torch_module=torch)
    assert __import__("os").environ["ACESTEP_MLX_VAE_CHUNK"] == "512"


@pytest.mark.parametrize(
    ("system", "machine"),
    [("Windows", "AMD64"), ("Linux", "aarch64"), ("Darwin", "x86_64")],
)
def test_unsupported_platforms_fail_closed(system: str, machine: str) -> None:
    with pytest.raises(WorkerInitializationError, match="unsupported|supported only"):
        select_accelerator(system=system, machine=machine, torch_module=types.SimpleNamespace())


def test_local_runtime_receipt_is_derived_from_exact_commit_and_lockfile(tmp_path: Path) -> None:
    lock = tmp_path / "uv.lock"
    lock.write_bytes(b"lock-v1")
    receipt = compute_local_runtime_receipt("a" * 40, lock)
    assert receipt.startswith("sha256:")
    assert receipt == compute_local_runtime_receipt("a" * 40, lock)
    with pytest.raises(WorkerInitializationError):
        compute_local_runtime_receipt("main", lock)


def test_node_settings_reject_public_bind_and_placeholder_service_token(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="globally routable"):
        NodeSettings(
            listen_host="8.8.8.8",
            data_root=tmp_path,
            token="node-secret",
        )
    settings = NodeSettings(data_root=tmp_path, token="change-me")
    with pytest.raises(ValueError, match="non-placeholder"):
        settings.require_token()
    settings = NodeSettings(data_root=tmp_path, token="example.invalid")
    with pytest.raises(ValueError, match="non-placeholder"):
        settings.require_token()
    with pytest.raises(ValueError, match="DATA_ROOT must be absolute"):
        NodeSettings(data_root=Path("relative-node-data"), token="node-secret")
    with pytest.raises(ValueError, match="pinned ACE-Step bundle"):
        NodeSettings(data_root=tmp_path, token="node-secret", worker_model_tag="unreviewed")
    with pytest.raises(ValueError, match="player.evren.io"):
        NodeSettings(data_root=tmp_path, token="node-secret", transfer_allowed_host="example.com")


def test_node_runtime_receipt_defaults_to_deployment_lock(tmp_path: Path) -> None:
    settings = NodeSettings(data_root=tmp_path, token="node-secret")
    assert settings.runtime_lock_path == Path("deploy/node/uv.lock")


def test_node_runtime_requires_a_committed_application_receipt(tmp_path: Path) -> None:
    settings = NodeSettings(
        data_root=tmp_path,
        token="node-secret",
        runtime_receipt="sha256:" + "a" * 64,
    )
    with pytest.raises(WorkerInitializationError, match="application revision"):
        AceStepNodeRuntime(settings).initialize()
