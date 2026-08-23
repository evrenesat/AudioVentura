from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from deploy.salad.saladctl import (
    InfraError,
    SaladApi,
    apply,
    desired_container_group,
    desired_queue,
    desired_queue_autoscaler,
    load_config,
    resolve_gpu_ids,
    session_start,
    session_stop,
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
    assert group["container"]["priority"] == "medium"
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


def _deployed_group(config: dict[str, object], *, min_replicas: int, replicas: int) -> dict:
    image = "ghcr.io/evrenesat/audioventura-ace-step-salad-worker@sha256:" + "a" * 64
    group = desired_container_group(
        config,
        image_ref=image,
        gpu_ids=["gpu-id"],
        ghcr_username="unused-user",
        ghcr_token="unused-token",
    )
    group["container"].pop("registry_authentication")
    group["priority"] = group["container"].pop("priority")
    group["autostart_policy"] = False
    group["replicas"] = replicas
    group["queue_autoscaler"] = desired_queue_autoscaler(min_replicas=min_replicas)
    return group


class _SessionApi:
    def __init__(
        self,
        config: dict[str, object],
        *,
        min_replicas: int = 0,
        replicas: int = 0,
        queue_length: object = 0,
        jobs: object | None = None,
        fail_patch: bool = False,
    ) -> None:
        self.queue = {**desired_queue(config), "current_queue_length": queue_length}
        self.group = _deployed_group(config, min_replicas=min_replicas, replicas=replicas)
        self.jobs = {"items": []} if jobs is None else jobs
        self.fail_patch = fail_patch
        self.calls: list[tuple[str, str, object | None, bool]] = []

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        merge_patch: bool = False,
    ) -> object:
        self.calls.append((method, path, json_body, merge_patch))
        if method == "GET" and path.endswith("/queues/queue-name"):
            return self.queue
        if method == "GET" and path.endswith("/containers/group-name"):
            return self.group
        if method == "GET" and path.endswith("/queues/queue-name/jobs?page=1&page_size=100"):
            return self.jobs
        if method == "PATCH" and path.endswith("/containers/group-name"):
            if self.fail_patch:
                raise InfraError("Salad API PATCH failed with HTTP 500")
            assert merge_patch is True
            assert isinstance(json_body, dict)
            self.group.update(json.loads(json.dumps(json_body)))
            return self.group
        raise InfraError("tracked Salad resource is missing")


def test_salad_api_sends_merge_patch_content_type() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["content_type"] = request.headers["Content-Type"]
        return httpx.Response(200, json={"name": "group-name"})

    api = SaladApi("secret-key")
    api._client.close()
    api._client = httpx.Client(
        base_url="https://api.salad.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        api.request("PATCH", "/group", json_body={"replicas": 1}, merge_patch=True)
    finally:
        api.close()

    assert seen == {"content_type": "application/merge-patch+json"}


def test_session_start_is_exact_idempotent_merge_patch_without_registry_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("GHCR_USERNAME", raising=False)
    monkeypatch.delenv("GHCR_TOKEN", raising=False)
    config = _config(tmp_path)
    api = _SessionApi(config)

    first = session_start(api, "org-name", "project-name", config)  # type: ignore[arg-type]
    second = session_start(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    patch = {
        "queue_autoscaler": desired_queue_autoscaler(min_replicas=1),
        "replicas": 1,
    }
    assert api.calls == [
        ("GET", "/organizations/org-name/projects/project-name/queues/queue-name", None, False),
        (
            "GET",
            "/organizations/org-name/projects/project-name/containers/group-name",
            None,
            False,
        ),
        (
            "PATCH",
            "/organizations/org-name/projects/project-name/containers/group-name",
            patch,
            True,
        ),
        ("GET", "/organizations/org-name/projects/project-name/queues/queue-name", None, False),
        (
            "GET",
            "/organizations/org-name/projects/project-name/containers/group-name",
            None,
            False,
        ),
    ]
    assert first == {
        "changed": True,
        "container_group": "group-name",
        "min_replicas": 1,
        "queue": "queue-name",
        "queue_length": 0,
        "replicas": 1,
        "session": "started",
    }
    assert second == {**first, "changed": False}


def test_session_stop_proves_idle_then_restores_zero_in_exact_order(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _SessionApi(
        config,
        min_replicas=1,
        replicas=1,
        jobs={"items": [{"status": "succeeded"}, {"status": "cancelled"}]},
    )

    result = session_stop(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert [call[:2] for call in api.calls] == [
        ("GET", "/organizations/org-name/projects/project-name/queues/queue-name"),
        ("GET", "/organizations/org-name/projects/project-name/containers/group-name"),
        (
            "GET",
            "/organizations/org-name/projects/project-name/queues/queue-name/jobs?page=1&page_size=100",
        ),
        ("PATCH", "/organizations/org-name/projects/project-name/containers/group-name"),
    ]
    assert api.calls[-1][2:] == (
        {"queue_autoscaler": desired_queue_autoscaler(), "replicas": 0},
        True,
    )
    assert result == {
        "changed": True,
        "container_group": "group-name",
        "min_replicas": 0,
        "queue": "queue-name",
        "queue_length": 0,
        "recent_jobs_checked": 2,
        "replicas": 0,
        "session": "stopped",
    }


def test_session_stop_is_idempotent_but_still_rechecks_idle_evidence(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config, min_replicas=0, replicas=0, jobs={"items": []})

    result = session_stop(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert [call[0] for call in api.calls] == ["GET", "GET", "GET"]
    assert result["changed"] is False
    assert result["session"] == "stopped"


@pytest.mark.parametrize("status", ("pending", "running"))
def test_session_stop_refuses_active_work_before_mutation(tmp_path: Path, status: str) -> None:
    config = _config(tmp_path)
    api = _SessionApi(
        config,
        min_replicas=1,
        replicas=1,
        jobs={"items": [{"status": status}]},
    )

    with pytest.raises(InfraError, match="queue work is active"):
        session_stop(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert all(call[0] != "PATCH" for call in api.calls)


def test_session_stop_refuses_nonempty_queue_before_job_read_or_mutation(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config, min_replicas=1, replicas=1, queue_length=1)

    with pytest.raises(InfraError, match="queue is nonempty"):
        session_stop(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert len(api.calls) == 2


@pytest.mark.parametrize(
    "jobs",
    (
        {},
        {"items": "invalid"},
        {"items": [{"status": "unknown"}]},
        {"items": [{"status": "succeeded"}] * 101},
    ),
)
def test_session_stop_refuses_unproven_job_list(tmp_path: Path, jobs: object) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config, min_replicas=1, replicas=1, jobs=jobs)

    with pytest.raises(InfraError, match="queue jobs response is malformed"):
        session_stop(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert all(call[0] != "PATCH" for call in api.calls)


@pytest.mark.parametrize("missing", ("queue", "group"))
def test_session_start_refuses_missing_exact_resource(tmp_path: Path, missing: str) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config)
    setattr(api, missing, None)

    with pytest.raises(InfraError, match="missing or malformed"):
        session_start(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert all(call[0] != "PATCH" for call in api.calls)


@pytest.mark.parametrize("drift", ("autoscaler", "image", "resources", "queue"))
def test_session_start_refuses_drift_or_malformed_resources(tmp_path: Path, drift: str) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config)
    if drift == "autoscaler":
        api.group["queue_autoscaler"]["polling_period"] = 30
    elif drift == "image":
        api.group["container"]["image"] = "untracked:latest"
    elif drift == "resources":
        api.group["container"]["resources"]["memory"] = "large"
    else:
        api.queue["current_queue_length"] = True

    with pytest.raises(InfraError, match="malformed|drift"):
        session_start(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert all(call[0] != "PATCH" for call in api.calls)


def test_session_start_refuses_malformed_tracked_shape(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config)
    del config["queue"]["display_name"]

    with pytest.raises(InfraError, match="tracked Salad deployment shape is malformed"):
        session_start(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert all(call[0] != "PATCH" for call in api.calls)


def test_session_patch_http_failure_is_bounded(tmp_path: Path) -> None:
    config = _config(tmp_path)
    api = _SessionApi(config, fail_patch=True)

    with pytest.raises(InfraError, match="HTTP 500") as captured:
        session_start(api, "org-name", "project-name", config)  # type: ignore[arg-type]

    assert "secret" not in str(captured.value).lower()
