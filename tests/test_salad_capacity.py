from __future__ import annotations

import asyncio

import httpx

from ace_service.capacity.salad import SaladCapacityManager


def test_salad_capacity_inspection_is_fingerprint_pinned() -> None:
    queue = {
        "name": "jobs",
        "display_name": "AudioVentura jobs",
        "description": "AudioVentura ACE-Step inference jobs",
        "current_queue_length": 0,
    }
    group = {
        "name": "group",
        "display_name": "AudioVentura ACE-Step",
        "autostart_policy": True,
        "restart_policy": "always",
        "priority": "medium",
        "replicas": 0,
        "container": {
            "image": "ghcr.io/example@sha256:" + "a" * 64,
            "image_caching": True,
            "resources": {
                "cpu": 8,
                "memory": 32768,
                "gpu_classes": ["RTX 3090"],
                "shm_size": 8192,
                "storage_amount": 2147483648,
            },
            "environment_variables": {
                "ACE_TRANSFER_ALLOWED_HOST": "player.evren.io",
                "ACE_WORKER_IMAGE_DIGEST": "ghcr.io/example@sha256:" + "a" * 64,
                "SALAD_QUEUE_WORKER_LOG_LEVEL": "info",
            },
        },
        "queue_connection": {"queue_name": "jobs", "path": "/process", "port": 8080},
        "queue_autoscaler": {
            "desired_queue_length": 1,
            "max_replicas": 1,
            "min_replicas": 0,
            "max_upscale_per_minute": 1,
            "max_downscale_per_minute": 1,
            "polling_period": 5,
        },
        "startup_probe": {
            "failure_threshold": 20,
            "initial_delay_seconds": 0,
            "period_seconds": 90,
            "success_threshold": 1,
            "timeout_seconds": 5,
            "http": {
                "headers": [{"name": "Accept", "value": "application/json"}],
                "path": "/ready",
                "port": 8080,
                "scheme": "http",
            },
        },
        "readiness_probe": {
            "failure_threshold": 3,
            "initial_delay_seconds": 0,
            "period_seconds": 10,
            "success_threshold": 1,
            "timeout_seconds": 5,
            "http": {
                "headers": [{"name": "Accept", "value": "application/json"}],
                "path": "/ready",
                "port": 8080,
                "scheme": "http",
            },
        },
        "liveness_probe": {
            "failure_threshold": 3,
            "initial_delay_seconds": 0,
            "period_seconds": 30,
            "success_threshold": 1,
            "timeout_seconds": 5,
            "http": {
                "headers": [{"name": "Accept", "value": "application/json"}],
                "path": "/live",
                "port": 8080,
                "scheme": "http",
            },
        },
    }
    instances: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/queues/jobs"):
            return httpx.Response(200, json=queue)
        if request.url.path.endswith("/containers/group"):
            return httpx.Response(200, json=group)
        if request.url.path.endswith("/queues/jobs/jobs"):
            return httpx.Response(200, json={"items": []})
        if request.url.path.endswith("/containers/group/instances"):
            return httpx.Response(200, json={"instances": instances})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(base_url="https://api.salad.test/", transport=transport)
    manager = SaladCapacityManager(
        "secret",
        "org",
        "project",
        "jobs",
        "group",
        "0" * 64,
        http_client=client,
    )
    manager.expected_fingerprint = manager._fingerprint(queue, group)
    snapshot = asyncio.run(manager.inspect())
    assert snapshot.configured_floor == 0
    assert snapshot.phase.value == "cold"
    group["replicas"] = 1
    group["queue_autoscaler"]["min_replicas"] = 1
    instances.append({"state": "running", "ready": True})
    snapshot = asyncio.run(manager.inspect())
    assert snapshot.phase.value == "ready"
    assert snapshot.ready_instances == 1
    asyncio.run(manager.aclose())
