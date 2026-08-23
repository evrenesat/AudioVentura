from __future__ import annotations

import json
from pathlib import Path

import pytest

from deploy.salad.saladctl import (
    InfraError,
    apply,
    desired_container_group,
    desired_queue,
    load_config,
    resolve_gpu_ids,
)


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
                    "startup_failure_threshold": 20,
                    "startup_period_seconds": 90,
                },
                "queue": {"description": "Jobs", "display_name": "Queue", "name": "queue-name"},
                "resources": {
                    "cpu": 8,
                    "memory_mb": 32768,
                    "shm_mb": 8192,
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
    assert group["container"]["resources"]["shm_size"] == 8192
    assert group["startup_probe"]["failure_threshold"] == 20
    assert group["startup_probe"]["period_seconds"] == 90
    assert group["startup_probe"]["http"]["headers"] == [
        {"name": "Accept", "value": "application/json"}
    ]
    assert group["container"]["registry_authentication"]["basic"]["password"] == "token"
    environment = group["container"]["environment_variables"]
    assert environment["ACE_WORKER_IMAGE_DIGEST"] == "sha256:" + "a" * 64
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


class _FakeSaladApi:
    def __init__(self) -> None:
        self.queues: list[dict[str, object]] = []
        self.groups: list[dict[str, object]] = []
        self.posts: list[str] = []

    def request(self, method: str, path: str, *, json_body: object | None = None) -> object:
        if path == "/organizations/org-name/gpu-classes":
            return {"items": [{"id": "gpu-id", "name": "RTX 4090"}]}
        if method == "GET" and path.endswith("/queues"):
            return {"items": self.queues}
        if method == "GET" and path.endswith("/containers"):
            return {"items": self.groups}
        if method == "GET" and "/containers/" in path:
            name = path.rsplit("/", 1)[-1]
            return next(group for group in self.groups if group["name"] == name)
        if method == "POST" and path.endswith("/queues"):
            assert isinstance(json_body, dict)
            self.posts.append(path)
            queue = dict(json_body)
            self.queues.append(queue)
            return queue
        if method == "POST" and path.endswith("/containers"):
            assert isinstance(json_body, dict)
            self.posts.append(path)
            group = json.loads(json.dumps(json_body))
            group["container"].pop("registry_authentication")
            group["priority"] = group["container"].pop("priority")
            group["autostart_policy"] = False
            self.groups.append(group)
            return group
        raise AssertionError(f"unexpected fake request: {method} {path}")


def test_gpu_resolution_accepts_only_the_catalog_vram_annotation() -> None:
    class _GpuApi:
        def request(self, method: str, path: str) -> object:
            assert method == "GET"
            assert path == "/organizations/org-name/gpu-classes"
            return {
                "items": [
                    {"id": "3090-id", "name": "RTX 3090 (24 GB)"},
                    {"id": "3090ti-id", "name": "RTX 3090 Ti (24 GB)"},
                    {"id": "4090-id", "name": "RTX 4090 (24 GB)"},
                ]
            }

    assert resolve_gpu_ids(
        _GpuApi(),  # type: ignore[arg-type]
        "org-name",
        ["RTX 3090", "RTX 3090 Ti", "RTX 4090"],
    ) == ["3090-id", "3090ti-id", "4090-id"]


def test_apply_is_idempotent_and_ignores_only_write_only_registry_auth(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _FakeSaladApi()
    image = "ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:" + "a" * 64

    first = apply(
        api,
        "org-name",
        "project-name",
        config,
        image_ref=image,
        ghcr_username="user",
        ghcr_token="token",
    )
    second = apply(
        api,
        "org-name",
        "project-name",
        config,
        image_ref=image,
        ghcr_username="user",
        ghcr_token="token",
    )

    assert first == second
    assert len(api.posts) == 2


def test_apply_stops_on_existing_queue_drift(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _FakeSaladApi()
    api.queues.append({**desired_queue(config), "description": "unexpected"})
    image = "ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:" + "a" * 64

    with pytest.raises(InfraError, match="queue has incompatible drift"):
        apply(
            api,
            "org-name",
            "project-name",
            config,
            image_ref=image,
            ghcr_username="user",
            ghcr_token="token",
        )
    assert api.posts == []


def test_apply_detects_group_drift_before_creating_missing_queue(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _FakeSaladApi()
    image = "ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:" + "a" * 64
    group = desired_container_group(
        config,
        image_ref=image,
        gpu_ids=["gpu-id"],
        ghcr_username="user",
        ghcr_token="token",
    )
    group["container"].pop("registry_authentication")
    group["priority"] = group["container"].pop("priority")
    group["container"]["image"] = (
        "ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:" + "b" * 64
    )
    api.groups.append(group)

    with pytest.raises(InfraError, match="container group has incompatible drift"):
        apply(
            api,
            "org-name",
            "project-name",
            config,
            image_ref=image,
            ghcr_username="user",
            ghcr_token="token",
        )
    assert api.posts == []


def test_worker_image_uses_official_queue_worker_log_variable() -> None:
    dockerfile = Path("deploy/salad/Dockerfile").read_text(encoding="utf-8")

    assert "SALAD_QUEUE_WORKER_LOG_LEVEL=info" in dockerfile
    assert "SALAD_LOG_LEVEL=" not in dockerfile
