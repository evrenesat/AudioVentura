from __future__ import annotations

import json
from pathlib import Path

import pytest

from deploy.salad.saladctl import InfraError, desired_container_group, desired_queue, load_config


def _config(tmp_path: Path) -> dict[str, object]:
    path = tmp_path / "deployment.json"
    path.write_text(
        json.dumps(
            {
                "container_group": {"display_name": "Group", "name": "group-name"},
                "gpu_names": ["RTX 4090"],
                "probes": {
                    "liveness_failure_threshold": 3,
                    "readiness_failure_threshold": 3,
                    "startup_failure_threshold": 180,
                },
                "queue": {"description": "Jobs", "display_name": "Queue", "name": "queue-name"},
                "resources": {
                    "cpu": 8,
                    "memory_mb": 32768,
                    "shm_bytes": 8589934592,
                    "storage_bytes": 2147483648,
                },
            }
        )
    )
    return load_config(path)


def test_desired_state_is_scale_to_zero_and_secret_is_not_serialized_elsewhere(
    tmp_path: Path,
) -> None:
    config = _config(tmp_path)
    image = "ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:" + "a" * 64
    group = desired_container_group(
        config,
        image_ref=image,
        gpu_ids=["gpu-id"],
        ghcr_username="user",
        ghcr_token="token",
    )

    assert desired_queue(config)["name"] == "queue-name"
    assert group["display_name"] == "Group"
    assert group["replicas"] == 0
    assert group["queue_autoscaler"] == {
        "desired_queue_length": 1,
        "max_downscale_per_minute": 1,
        "max_replicas": 1,
        "max_upscale_per_minute": 1,
        "min_replicas": 0,
        "polling_period": 15,
    }
    assert group["queue_connection"] == {
        "path": "/process",
        "port": 8080,
        "queue_name": "queue-name",
    }
    assert group["container"]["image"] == image
    assert group["container"]["registry_authentication"]["basic"]["password"] == "token"
    environment = group["container"]["environment_variables"]
    assert environment["SALAD_QUEUE_WORKER_LOG_LEVEL"] == "info"
    assert "SALAD_LOG_LEVEL" not in environment


def test_desired_state_rejects_mutable_or_unrelated_image(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(InfraError, match="immutable AudioVentura GHCR digest"):
        desired_container_group(
            config,
            image_ref="ghcr.io/evrenesat/audioventura-ace-step-salad-worker:latest",
            gpu_ids=["gpu-id"],
            ghcr_username="user",
            ghcr_token="token",
        )


def test_worker_image_uses_official_queue_worker_log_variable() -> None:
    dockerfile = Path("deploy/salad/Dockerfile").read_text(encoding="utf-8")

    assert "SALAD_QUEUE_WORKER_LOG_LEVEL=info" in dockerfile
    assert "SALAD_LOG_LEVEL=" not in dockerfile
