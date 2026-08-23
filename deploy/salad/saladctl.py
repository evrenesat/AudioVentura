"""Safe initial provisioning and inspection for AudioVentura on SaladCloud."""

from __future__ import annotations

import argparse
import copy
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import httpx

API_ROOT = "https://api.salad.com/api/public"
CONFIG_PATH = Path(__file__).with_name("deployment.json")
IMAGE_RE = re.compile(
    r"^ghcr\.io/evrenesat/audioventura-ace-step-salad-worker@sha256:[0-9a-f]{64}$"
)
GPU_VRAM_SUFFIX_RE = re.compile(r"\s+\([0-9]+\s*GB\)\s*$", re.IGNORECASE)
RESOURCE_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
QUEUE_JOB_STATUSES = frozenset({"pending", "running", "succeeded", "cancelled", "failed"})
ACTIVE_QUEUE_JOB_STATUSES = frozenset({"pending", "running"})
MAX_QUEUE_JOBS = 100
MAX_API_RESPONSE_BYTES = 1_048_576


class InfraError(RuntimeError):
    """A bounded, secret-free infrastructure error."""


def load_config(path: Path = CONFIG_PATH) -> dict[str, Any]:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise InfraError("Salad deployment config must be an object")
    return value


class SaladApi:
    def __init__(self, api_key: str) -> None:
        if not api_key:
            raise InfraError("SALAD_API_KEY is missing")
        self._client = httpx.Client(
            base_url=API_ROOT,
            headers={"Salad-Api-Key": api_key, "Accept": "application/json"},
            timeout=httpx.Timeout(30, connect=10),
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        json_body: object | None = None,
        merge_patch: bool = False,
    ) -> Any:
        try:
            headers = {"Content-Type": "application/merge-patch+json"} if merge_patch else None
            response = self._client.request(method, path, json=json_body, headers=headers)
        except httpx.HTTPError as exc:
            raise InfraError(f"Salad API {method} failed with {type(exc).__name__}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise InfraError(f"Salad API {method} failed with HTTP {response.status_code}")
        if len(response.content) > MAX_API_RESPONSE_BYTES:
            raise InfraError(f"Salad API {method} returned an oversized response")
        try:
            return response.json()
        except ValueError as exc:
            raise InfraError(f"Salad API {method} returned malformed JSON") from exc


def _items(value: Any, label: str) -> list[dict[str, Any]]:
    if not isinstance(value, Mapping) or not isinstance(value.get("items"), list):
        raise InfraError(f"Salad {label} response is malformed")
    items = value["items"]
    if not all(isinstance(item, dict) for item in items):
        raise InfraError(f"Salad {label} response is malformed")
    return cast(list[dict[str, Any]], items)


def _resource_path(organization: str, project: str, suffix: str) -> str:
    if not RESOURCE_NAME_RE.fullmatch(organization) or not RESOURCE_NAME_RE.fullmatch(project):
        raise InfraError("Salad organization and project names are malformed")
    return f"/organizations/{organization}/projects/{project}/{suffix}"


def resolve_gpu_ids(api: SaladApi, organization: str, names: list[str]) -> list[str]:
    value = api.request("GET", f"/organizations/{organization}/gpu-classes")
    items = _items(value, "GPU classes")
    by_name = {
        GPU_VRAM_SUFFIX_RE.sub("", str(item.get("name", "")).strip()).casefold(): str(
            item.get("id", "")
        )
        for item in items
        if item.get("name") and item.get("id")
    }
    resolved = [by_name[name.casefold()] for name in names if name.casefold() in by_name]
    if not resolved:
        raise InfraError("none of the configured Salad GPU classes are available")
    return resolved


def desired_queue(config: Mapping[str, Any]) -> dict[str, Any]:
    queue = config["queue"]
    return {
        "name": queue["name"],
        "display_name": queue["display_name"],
        "description": queue["description"],
    }


def desired_queue_autoscaler(*, min_replicas: int = 0) -> dict[str, int]:
    if min_replicas not in {0, 1}:
        raise InfraError("interactive minimum replicas must be zero or one")
    return {
        "desired_queue_length": 1,
        "max_downscale_per_minute": 1,
        "max_replicas": 1,
        "max_upscale_per_minute": 1,
        "min_replicas": min_replicas,
        "polling_period": 15,
    }


def _http_probe(path: str, failure_threshold: int, *, period: int) -> dict[str, Any]:
    return {
        "failure_threshold": failure_threshold,
        "initial_delay_seconds": 0,
        "period_seconds": period,
        "success_threshold": 1,
        "timeout_seconds": 5,
        "http": {
            "headers": [{"name": "Accept", "value": "application/json"}],
            "path": path,
            "port": 8080,
            "scheme": "http",
        },
    }


def desired_container_group(
    config: Mapping[str, Any],
    *,
    image_ref: str,
    gpu_ids: list[str],
    ghcr_username: str,
    ghcr_token: str,
) -> dict[str, Any]:
    if not IMAGE_RE.fullmatch(image_ref):
        raise InfraError("Salad worker image must be the immutable AudioVentura GHCR digest")
    if not ghcr_username or not ghcr_token:
        raise InfraError("GHCR_USERNAME and GHCR_TOKEN are required for the private image")
    image_digest = image_ref.rsplit("@", 1)[1]
    group = config["container_group"]
    queue = config["queue"]
    resources = config["resources"]
    probes = config["probes"]
    return {
        "name": group["name"],
        "display_name": group["display_name"],
        "autostart_policy": True,
        "replicas": 0,
        "restart_policy": "always",
        "container": {
            "image": image_ref,
            "image_caching": True,
            "priority": "medium",
            "registry_authentication": {
                "basic": {"username": ghcr_username, "password": ghcr_token}
            },
            "resources": {
                "cpu": resources["cpu"],
                "memory": resources["memory_mb"],
                "gpu_classes": gpu_ids,
                "shm_size": resources["shm_mb"],
                "storage_amount": resources["storage_bytes"],
            },
            "environment_variables": {
                "ACE_WORKER_IMAGE_DIGEST": image_digest,
                "ACE_TRANSFER_ALLOWED_HOST": "player.evren.io",
                "SALAD_QUEUE_WORKER_LOG_LEVEL": "info",
            },
        },
        "queue_connection": {"path": "/process", "port": 8080, "queue_name": queue["name"]},
        "queue_autoscaler": desired_queue_autoscaler(),
        "startup_probe": _http_probe(
            "/ready",
            probes["startup_failure_threshold"],
            period=probes["startup_period_seconds"],
        ),
        "readiness_probe": _http_probe("/ready", probes["readiness_failure_threshold"], period=10),
        "liveness_probe": _http_probe("/live", probes["liveness_failure_threshold"], period=30),
    }


def _find_named(items: list[dict[str, Any]], name: str) -> dict[str, Any] | None:
    return next((item for item in items if item.get("name") == name), None)


def _require_desired_subset(actual: Any, desired: Any, label: str) -> None:
    """Fail closed when a remote resource differs from tracked desired state."""

    if isinstance(desired, Mapping):
        if not isinstance(actual, Mapping):
            raise InfraError(f"existing Salad {label} has incompatible drift")
        for key, value in desired.items():
            if key not in actual:
                raise InfraError(f"existing Salad {label} has incompatible drift")
            _require_desired_subset(actual[key], value, label)
        return
    if actual != desired:
        raise InfraError(f"existing Salad {label} has incompatible drift")


def _verifiable_group_state(desired: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize create-only fields to Salad's readable group representation."""

    value = copy.deepcopy(dict(desired))
    value.pop("autostart_policy", None)
    container = value.get("container")
    if isinstance(container, dict):
        container.pop("registry_authentication", None)
        priority = container.pop("priority", None)
        if priority is not None:
            value["priority"] = priority
    return value


def inspect_state(
    api: SaladApi, organization: str, project: str, config: Mapping[str, Any]
) -> dict[str, Any]:
    queue_items = _items(
        api.request("GET", _resource_path(organization, project, "queues")), "queues"
    )
    group_items = _items(
        api.request("GET", _resource_path(organization, project, "containers")),
        "container groups",
    )
    queue = _find_named(queue_items, config["queue"]["name"])
    group = _find_named(group_items, config["container_group"]["name"])
    return {
        "container_group": None
        if group is None
        else {
            "current_state": group.get("current_state"),
            "name": group.get("name"),
            "replicas": group.get("replicas"),
            "status": group.get("status"),
            "version": group.get("version"),
        },
        "queue": None
        if queue is None
        else {
            "current_queue_length": queue.get("current_queue_length"),
            "name": queue.get("name"),
        },
    }


def apply(
    api: SaladApi,
    organization: str,
    project: str,
    config: Mapping[str, Any],
    *,
    image_ref: str,
    ghcr_username: str,
    ghcr_token: str,
) -> dict[str, Any]:
    gpu_ids = resolve_gpu_ids(api, organization, list(config["gpu_names"]))
    queue_items = _items(
        api.request("GET", _resource_path(organization, project, "queues")), "queues"
    )
    group_items = _items(
        api.request("GET", _resource_path(organization, project, "containers")),
        "container groups",
    )
    wanted_queue = desired_queue(config)
    wanted_group = desired_container_group(
        config,
        image_ref=image_ref,
        gpu_ids=gpu_ids,
        ghcr_username=ghcr_username,
        ghcr_token=ghcr_token,
    )
    existing_queue = _find_named(queue_items, wanted_queue["name"])
    existing_group = _find_named(group_items, wanted_group["name"])
    if existing_queue is not None:
        _require_desired_subset(existing_queue, wanted_queue, "queue")
    if existing_group is not None:
        existing_group = api.request(
            "GET",
            _resource_path(
                organization,
                project,
                f"containers/{wanted_group['name']}",
            ),
        )
        _require_desired_subset(
            existing_group,
            _verifiable_group_state(wanted_group),
            "container group",
        )
    if existing_queue is None:
        api.request(
            "POST",
            _resource_path(organization, project, "queues"),
            json_body=wanted_queue,
        )
    if existing_group is None:
        api.request(
            "POST",
            _resource_path(organization, project, "containers"),
            json_body=wanted_group,
        )
    return inspect_state(api, organization, project, config)


def _tracked_resource_names(config: Mapping[str, Any]) -> tuple[str, str]:
    try:
        queue_name = config["queue"]["name"]
        group_name = config["container_group"]["name"]
    except (KeyError, TypeError) as exc:
        raise InfraError("tracked Salad resource names are malformed") from exc
    if (
        not isinstance(queue_name, str)
        or not RESOURCE_NAME_RE.fullmatch(queue_name)
        or not isinstance(group_name, str)
        or not RESOURCE_NAME_RE.fullmatch(group_name)
    ):
        raise InfraError("tracked Salad resource names are malformed")
    return queue_name, group_name


def _bounded_int(value: Any, *, minimum: int, maximum: int, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise InfraError(f"Salad {label} is malformed")
    return cast(int, value)


def _session_resources(
    api: SaladApi, organization: str, project: str, config: Mapping[str, Any]
) -> tuple[str, str, dict[str, Any], dict[str, Any]]:
    queue_name, group_name = _tracked_resource_names(config)
    queue_path = _resource_path(organization, project, f"queues/{queue_name}")
    group_path = _resource_path(organization, project, f"containers/{group_name}")
    queue = api.request("GET", queue_path)
    group = api.request("GET", group_path)
    if not isinstance(queue, dict) or queue.get("name") != queue_name:
        raise InfraError("tracked Salad queue is missing or malformed")
    if not isinstance(group, dict) or group.get("name") != group_name:
        raise InfraError("tracked Salad container group is missing or malformed")
    try:
        tracked_queue = desired_queue(config)
    except (KeyError, TypeError) as exc:
        raise InfraError("tracked Salad deployment shape is malformed") from exc
    _require_desired_subset(queue, tracked_queue, "queue")
    _bounded_int(
        queue.get("current_queue_length"),
        minimum=0,
        maximum=2_147_483_647,
        label="queue length",
    )
    try:
        tracked_group = config["container_group"]
        tracked_resources = config["resources"]
        expected_group = {
            "display_name": tracked_group["display_name"],
            "priority": "medium",
            "queue_connection": {
                "path": "/process",
                "port": 8080,
                "queue_name": queue_name,
            },
            "container": {
                "resources": {
                    "cpu": tracked_resources["cpu"],
                    "memory": tracked_resources["memory_mb"],
                    "shm_size": tracked_resources["shm_mb"],
                    "storage_amount": tracked_resources["storage_bytes"],
                }
            },
        }
    except (KeyError, TypeError) as exc:
        raise InfraError("tracked Salad deployment shape is malformed") from exc
    _require_desired_subset(group, expected_group, "container group")
    container = group.get("container")
    image = container.get("image") if isinstance(container, Mapping) else None
    if not isinstance(image, str) or not IMAGE_RE.fullmatch(image):
        raise InfraError("existing Salad container group has incompatible drift")
    resources = container.get("resources") if isinstance(container, Mapping) else None
    gpu_classes = resources.get("gpu_classes") if isinstance(resources, Mapping) else None
    if (
        not isinstance(gpu_classes, list)
        or not 1 <= len(gpu_classes) <= 100
        or any(
            not isinstance(gpu_id, str)
            or not 1 <= len(gpu_id) <= 128
            or any(not character.isprintable() for character in gpu_id)
            for gpu_id in gpu_classes
        )
    ):
        raise InfraError("existing Salad container group has incompatible drift")
    replicas = _bounded_int(
        group.get("replicas"), minimum=0, maximum=1, label="container group replicas"
    )
    autoscaler = group.get("queue_autoscaler")
    if not isinstance(autoscaler, dict):
        raise InfraError("existing Salad container group autoscaler is malformed")
    min_replicas = _bounded_int(
        autoscaler.get("min_replicas"), minimum=0, maximum=1, label="minimum replicas"
    )
    _require_desired_subset(
        autoscaler,
        {
            key: value
            for key, value in desired_queue_autoscaler(min_replicas=min_replicas).items()
            if key != "min_replicas"
        },
        "container group autoscaler",
    )
    if set(autoscaler) != set(desired_queue_autoscaler()):
        raise InfraError("existing Salad container group autoscaler has incompatible drift")
    group["replicas"] = replicas
    return queue_name, group_name, queue, group


def _session_patch(
    api: SaladApi,
    organization: str,
    project: str,
    group_name: str,
    group: Mapping[str, Any],
    *,
    min_replicas: int,
    replicas: int,
) -> bool:
    autoscaler = desired_queue_autoscaler(min_replicas=min_replicas)
    if group["queue_autoscaler"] == autoscaler and group["replicas"] == replicas:
        return False
    patched = api.request(
        "PATCH",
        _resource_path(organization, project, f"containers/{group_name}"),
        json_body={"queue_autoscaler": autoscaler, "replicas": replicas},
        merge_patch=True,
    )
    if not isinstance(patched, Mapping):
        raise InfraError("Salad container group update response is malformed")
    if patched.get("name") != group_name:
        raise InfraError("Salad container group update response is malformed")
    if patched.get("queue_autoscaler") != autoscaler or patched.get("replicas") != replicas:
        raise InfraError("Salad container group update did not reach requested session state")
    return True


def session_start(
    api: SaladApi, organization: str, project: str, config: Mapping[str, Any]
) -> dict[str, Any]:
    queue_name, group_name, queue, group = _session_resources(api, organization, project, config)
    changed = _session_patch(
        api,
        organization,
        project,
        group_name,
        group,
        min_replicas=1,
        replicas=1,
    )
    return {
        "changed": changed,
        "container_group": group_name,
        "min_replicas": 1,
        "queue": queue_name,
        "queue_length": queue["current_queue_length"],
        "replicas": 1,
        "session": "started",
    }


def session_stop(
    api: SaladApi, organization: str, project: str, config: Mapping[str, Any]
) -> dict[str, Any]:
    queue_name, group_name, queue, group = _session_resources(api, organization, project, config)
    if queue["current_queue_length"] != 0:
        raise InfraError("interactive session cannot stop while the queue is nonempty")
    jobs = _items(
        api.request(
            "GET",
            _resource_path(
                organization,
                project,
                f"queues/{queue_name}/jobs?page=1&per_page={MAX_QUEUE_JOBS}",
            ),
        ),
        "queue jobs",
    )
    if len(jobs) > MAX_QUEUE_JOBS:
        raise InfraError("Salad queue jobs response is malformed")
    for job in jobs:
        status = job.get("status")
        if status not in QUEUE_JOB_STATUSES:
            raise InfraError("Salad queue jobs response is malformed")
        if status in ACTIVE_QUEUE_JOB_STATUSES:
            raise InfraError("interactive session cannot stop while queue work is active")
    changed = _session_patch(
        api,
        organization,
        project,
        group_name,
        group,
        min_replicas=0,
        replicas=0,
    )
    return {
        "changed": changed,
        "container_group": group_name,
        "min_replicas": 0,
        "queue": queue_name,
        "queue_length": 0,
        "recent_jobs_checked": len(jobs),
        "replicas": 0,
        "session": "stopped",
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "apply", "session-start", "session-stop"))
    parser.add_argument("--organization", required=True)
    parser.add_argument("--project", required=True)
    parser.add_argument("--image-ref")
    return parser


def main() -> int:
    args = _parser().parse_args()
    api = SaladApi(os.environ.get("SALAD_API_KEY", ""))
    try:
        config = load_config()
        if args.mode == "inspect":
            result = inspect_state(api, args.organization, args.project, config)
        elif args.mode == "apply":
            if not args.image_ref:
                raise InfraError("--image-ref is required for apply")
            result = apply(
                api,
                args.organization,
                args.project,
                config,
                image_ref=args.image_ref,
                ghcr_username=os.environ.get("GHCR_USERNAME", ""),
                ghcr_token=os.environ.get("GHCR_TOKEN", ""),
            )
        elif args.mode == "session-start":
            result = session_start(api, args.organization, args.project, config)
        else:
            result = session_stop(api, args.organization, args.project, config)
    finally:
        api.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
