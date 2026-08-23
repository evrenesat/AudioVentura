from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx

from ace_service.capacity.runpod import RunpodCapacityManager


def test_runpod_capacity_patches_only_workers_min() -> None:
    endpoint = {
        "id": "endpoint",
        "name": "AudioVentura",
        "workersMin": 0,
        "workersMax": 1,
        "gpuCount": 1,
        "minCudaVersion": "12.8",
        "executionTimeoutMs": 1200000,
        "networkVolumeId": "volume",
        "networkVolumeIds": ["volume"],
        "idleTimeout": 60,
        "gpuTypeIds": ["NVIDIA GeForce RTX 3090"],
        "scalerType": "QUEUE_DELAY",
        "scalerValue": 2,
        "flashboot": True,
        "template": {
            "id": "template",
            "name": "AudioVentura worker",
            "category": "NVIDIA",
            "imageName": "ghcr.io/example",
            "containerDiskInGb": 40,
            "ports": ["8888/http"],
            "isServerless": True,
            "readme": "",
            "volumeMountPath": "/workspace",
            "env": {
                "ACE_TRANSFER_ALLOWED_HOST": "player.evren.io",
                "ACE_WORKER_CHECKPOINTS_DIR": "/runpod-volume/huggingface-cache/hub",
                "ACE_WORKER_IMAGE_DIGEST": "ghcr.io/example",
            },
        },
    }
    patches: list[dict[str, object]] = []
    inspect_queries: list[dict[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/endpoints"):
            inspect_queries.append(dict(request.url.params))
            return httpx.Response(200, json=[endpoint])
        if request.url.path.endswith("/health"):
            return httpx.Response(
                200,
                json={
                    "workers": {"idle": 0, "running": 0},
                    "jobs": {"inQueue": 0, "inProgress": 0},
                },
            )
        if request.method == "PATCH":
            body = request.content
            import json

            patches.append(json.loads(body))
            endpoint["workersMin"] = patches[-1]["workersMin"]
            return httpx.Response(200, json=endpoint)
        return httpx.Response(404)

    client = httpx.AsyncClient(
        base_url="https://rest.runpod.test/v1/", transport=httpx.MockTransport(handler)
    )
    manager = RunpodCapacityManager("secret", "endpoint", "0" * 64, http_client=client)
    manager.expected_fingerprint = manager._fingerprint(endpoint)
    before = asyncio.run(manager.inspect())
    after = asyncio.run(manager.retain_one(before))
    assert after.configured_floor == 1
    assert patches == [{"workersMin": 1}]
    assert inspect_queries == [
        {"includeTemplate": "true", "includeWorkers": "true"},
        {"includeTemplate": "true", "includeWorkers": "true"},
    ]
    asyncio.run(manager.aclose())


def test_runpod_fingerprint_fixture_matches_provider_normalization() -> None:
    fixture = json.loads(
        (
            Path(__file__).parents[1] / "src/ace_service/capacity/fingerprint_fixtures.json"
        ).read_text(encoding="utf-8")
    )["runpod"]
    canonical = fixture["payload"]
    endpoint = {
        "id": canonical["endpoint"]["id"],
        "name": canonical["endpoint"]["name"],
        "workersMax": canonical["endpoint"]["workers_max"],
        "workersMin": 0,
        "gpuCount": canonical["endpoint"]["gpu_count"],
        "minCudaVersion": canonical["endpoint"]["min_cuda_version"],
        "executionTimeoutMs": canonical["endpoint"]["execution_timeout_ms"],
        "idleTimeout": canonical["endpoint"]["idle_timeout_seconds"],
        "flashboot": canonical["endpoint"]["flash_boot"],
        "networkVolumeId": canonical["endpoint"]["network_volume_id"],
        "networkVolumeIds": canonical["endpoint"]["network_volume_ids"],
        "gpuTypeIds": canonical["endpoint"]["gpu_type_ids"],
        "scalerType": canonical["endpoint"]["scaler"]["type"],
        "scalerValue": canonical["endpoint"]["scaler"]["value"],
        "template": {
            "id": canonical["template"]["id"],
            "name": canonical["template"]["name"],
            "category": canonical["template"]["category"],
            "containerDiskInGb": canonical["template"]["container_disk_in_gb"],
            "isServerless": canonical["template"]["is_serverless"],
            "ports": canonical["template"]["ports"],
            "readme": canonical["template"]["readme"],
            "volumeMountPath": canonical["template"]["volume_mount_path"],
            "imageName": canonical["template"]["image_name"],
            "env": canonical["template"]["environment"],
        },
    }
    manager = RunpodCapacityManager("secret", endpoint["id"], fixture["sha256"])
    assert manager._fingerprint(endpoint) == fixture["sha256"]
    asyncio.run(manager.aclose())
