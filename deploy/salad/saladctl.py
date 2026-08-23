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

    def request(self, method: str, path: str, *, json_body: object | None = None) -> Any:
        try:
            response = self._client.request(method, path, json=json_body)
        except httpx.HTTPError as exc:
            raise InfraError(f"Salad API {method} failed with {type(exc).__name__}") from exc
        if response.status_code < 200 or response.status_code >= 300:
            raise InfraError(f"Salad API {method} failed with HTTP {response.status_code}")
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
    name_re = re.compile(r"^[a-z][a-z0-9-]{0,61}[a-z0-9]$")
    if not name_re.fullmatch(organization) or not name_re.fullmatch(project):
        raise InfraError("Salad organization and project names are malformed")
    return f"/organizations/{organization}/projects/{project}/{suffix}"


def resolve_gpu_ids(api: SaladApi, organization: str, names: list[str]) -> list[str]:
    value = api.request("GET", f"/organizations/{organization}/gpu-classes")
    items = _items(value, "GPU classes")
    by_name = {
        str(item.get("name", "")).casefold(): str(item.get("id", ""))
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


def _http_probe(path: str, failure_threshold: int, *, period: int) -> dict[str, Any]:
    return {
        "failure_threshold": failure_threshold,
        "initial_delay_seconds": 0,
        "period_seconds": period,
        "success_threshold": 1,
        "timeout_seconds": 5,
        "http": {"headers": [], "path": path, "port": 8080, "scheme": "http"},
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
            "priority": "batch",
            "registry_authentication": {
                "basic": {"username": ghcr_username, "password": ghcr_token}
            },
            "resources": {
                "cpu": resources["cpu"],
                "memory": resources["memory_mb"],
                "gpu_classes": gpu_ids,
                "shm_size": resources["shm_bytes"],
                "storage_amount": resources["storage_bytes"],
            },
            "environment_variables": {
                "ACE_WORKER_IMAGE_DIGEST": image_ref,
                "ACE_TRANSFER_ALLOWED_HOST": "player.evren.io",
                "SALAD_QUEUE_WORKER_LOG_LEVEL": "info",
            },
        },
        "queue_connection": {"path": "/process", "port": 8080, "queue_name": queue["name"]},
        "queue_autoscaler": {
            "desired_queue_length": 1,
            "max_downscale_per_minute": 1,
            "max_replicas": 1,
            "max_upscale_per_minute": 1,
            "min_replicas": 0,
            "polling_period": 15,
        },
        "startup_probe": _http_probe("/ready", probes["startup_failure_threshold"], period=10),
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
    """Exclude the write-only registry password from remote drift checks."""

    value = copy.deepcopy(dict(desired))
    container = value.get("container")
    if isinstance(container, dict):
        container.pop("registry_authentication", None)
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


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=("inspect", "apply"))
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
        else:
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
    finally:
        api.close()
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
