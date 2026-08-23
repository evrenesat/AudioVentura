"""Canonical, secret-free deployment identity payloads for capacity managers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from .base import CapacityError, CapacityErrorKind

RUNPOD_ENV_KEYS = (
    "ACE_TRANSFER_ALLOWED_HOST",
    "ACE_WORKER_CHECKPOINTS_DIR",
    "ACE_WORKER_IMAGE_DIGEST",
)
SALAD_ENV_KEYS = (
    "ACE_TRANSFER_ALLOWED_HOST",
    "ACE_WORKER_IMAGE_DIGEST",
    "SALAD_QUEUE_WORKER_LOG_LEVEL",
)


def _required(mapping: Mapping[str, Any], key: str, operation: str) -> Any:
    value = mapping.get(key)
    if value is None:
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity identity field is missing: {key}",
        )
    return value


def _string(mapping: Mapping[str, Any], key: str, operation: str) -> str:
    value = _required(mapping, key, operation)
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity identity field is invalid: {key}",
        )
    return cast(str, value)


def _integer(mapping: Mapping[str, Any], key: str, operation: str) -> int:
    value = _required(mapping, key, operation)
    if isinstance(value, bool) or not isinstance(value, int):
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity identity field is invalid: {key}",
        )
    return cast(int, value)


def _boolean(mapping: Mapping[str, Any], key: str, operation: str) -> bool:
    value = _required(mapping, key, operation)
    if not isinstance(value, bool):
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity identity field is invalid: {key}",
        )
    return value


def _mapping(mapping: Mapping[str, Any], key: str, operation: str) -> Mapping[str, Any]:
    value = _required(mapping, key, operation)
    if not isinstance(value, Mapping):
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity identity object is invalid: {key}",
        )
    return value


def _string_list(mapping: Mapping[str, Any], key: str, operation: str) -> list[str]:
    value = _required(mapping, key, operation)
    if (
        not isinstance(value, list)
        or not value
        or len(value) > 32
        or any(not isinstance(item, str) or not item or len(item) > 256 for item in value)
    ):
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity identity list is invalid: {key}",
        )
    return list(value)


def _environment(
    mapping: Mapping[str, Any], key: str, required_keys: tuple[str, ...], operation: str
) -> dict[str, str]:
    value = _mapping(mapping, key, operation)
    if set(value) != set(required_keys):
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"capacity environment key set is incompatible: {key}",
        )
    result: dict[str, str] = {}
    for env_key in required_keys:
        env_value = value.get(env_key)
        if not isinstance(env_value, str) or not env_value or len(env_value) > 512:
            raise CapacityError(
                CapacityErrorKind.DRIFT,
                operation,
                f"capacity environment value is invalid: {env_key}",
            )
        result[env_key] = env_value
    return result


def build_runpod_fingerprint_payload(endpoint: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize every immutable RunPod field used by the spend-side contract."""

    operation = "inspect"
    template = _mapping(endpoint, "template", operation)
    network_volume_id = _string(endpoint, "networkVolumeId", operation)
    network_volume_ids = _string_list(endpoint, "networkVolumeIds", operation)
    endpoint_fields: dict[str, Any] = {
        "id": _string(endpoint, "id", operation),
        "name": _string(endpoint, "name", operation),
        "template_id": _string(template, "id", operation),
        "workers_max": _integer(endpoint, "workersMax", operation),
        "gpu_count": _integer(endpoint, "gpuCount", operation),
        "min_cuda_version": _string(endpoint, "minCudaVersion", operation),
        "execution_timeout_ms": _integer(endpoint, "executionTimeoutMs", operation),
        "idle_timeout_seconds": _integer(endpoint, "idleTimeout", operation),
        "flash_boot": _boolean(endpoint, "flashBoot", operation),
        "network_volume_id": network_volume_id,
        "network_volume_ids": network_volume_ids,
        "gpu_type_ids": _string_list(endpoint, "gpuTypeIds", operation),
        "scaler": {
            "type": _string(endpoint, "scalerType", operation),
            "value": _integer(endpoint, "scalerValue", operation),
        },
    }
    if network_volume_id not in network_volume_ids:
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            "RunPod network volume identity is inconsistent",
        )
    template_fields = {
        "id": _string(template, "id", operation),
        "name": _string(template, "name", operation),
        "category": _string(template, "category", operation),
        "container_disk_in_gb": _integer(template, "containerDiskInGb", operation),
        "is_serverless": _boolean(template, "isServerless", operation),
        "ports": _string_list(template, "ports", operation),
        "readme": _required(template, "readme", operation),
        "volume_mount_path": _string(template, "volumeMountPath", operation),
        "image_name": _string(template, "imageName", operation),
        "environment": _environment(template, "env", RUNPOD_ENV_KEYS, operation),
    }
    if not isinstance(template_fields["is_serverless"], bool) or not isinstance(
        template_fields["readme"], str
    ):
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            "RunPod template identity fields are invalid",
        )
    return {"endpoint": endpoint_fields, "template": template_fields}


def _probe(value: Mapping[str, Any], key: str, operation: str) -> dict[str, Any]:
    probe = _mapping(value, key, operation)
    result: dict[str, Any] = {
        "failure_threshold": _integer(probe, "failure_threshold", operation),
        "initial_delay_seconds": _integer(probe, "initial_delay_seconds", operation),
        "period_seconds": _integer(probe, "period_seconds", operation),
        "success_threshold": _integer(probe, "success_threshold", operation),
        "timeout_seconds": _integer(probe, "timeout_seconds", operation),
    }
    http = _mapping(probe, "http", operation)
    headers = _required(http, "headers", operation)
    if not isinstance(headers, list) or len(headers) > 8:
        raise CapacityError(
            CapacityErrorKind.DRIFT,
            operation,
            f"Salad probe headers are invalid: {key}",
        )
    normalized_headers: list[dict[str, str]] = []
    for header in headers:
        if not isinstance(header, Mapping):
            raise CapacityError(
                CapacityErrorKind.DRIFT,
                operation,
                f"Salad probe header is invalid: {key}",
            )
        normalized_headers.append(
            {
                "name": _string(header, "name", operation),
                "value": _string(header, "value", operation),
            }
        )
    result["http"] = {
        "headers": normalized_headers,
        "path": _string(http, "path", operation),
        "port": _integer(http, "port", operation),
        "scheme": _string(http, "scheme", operation),
    }
    return result


def build_salad_fingerprint_payload(
    queue: Mapping[str, Any], group: Mapping[str, Any], *, organization: str, project: str
) -> dict[str, Any]:
    """Normalize the reviewed Salad group without copying registry secrets."""

    operation = "inspect"
    queue_payload = {
        "name": _string(queue, "name", operation),
        "display_name": _string(queue, "display_name", operation),
        "description": _string(queue, "description", operation),
    }
    container = _mapping(group, "container", operation)
    resources = _mapping(container, "resources", operation)
    environment = _environment(container, "environment_variables", SALAD_ENV_KEYS, operation)
    resources_payload = {
        "cpu": _integer(resources, "cpu", operation),
        "memory": _integer(resources, "memory", operation),
        "gpu_classes": _string_list(resources, "gpu_classes", operation),
        "shm_size": _integer(resources, "shm_size", operation),
        "storage_amount": _integer(resources, "storage_amount", operation),
    }
    connection = _mapping(group, "queue_connection", operation)
    autoscaler = _mapping(group, "queue_autoscaler", operation)
    autoscaler_payload = {
        "desired_queue_length": _integer(autoscaler, "desired_queue_length", operation),
        "max_downscale_per_minute": _integer(autoscaler, "max_downscale_per_minute", operation),
        "max_replicas": _integer(autoscaler, "max_replicas", operation),
        "max_upscale_per_minute": _integer(autoscaler, "max_upscale_per_minute", operation),
        "polling_period": _integer(autoscaler, "polling_period", operation),
    }
    return {
        "organization": organization,
        "project": project,
        "queue": queue_payload,
        "group": {
            "name": _string(group, "name", operation),
            "display_name": _string(group, "display_name", operation),
            "autostart_policy": _boolean(group, "autostart_policy", operation),
            "restart_policy": _string(group, "restart_policy", operation),
            "container": {
                "image": _string(container, "image", operation),
                "image_caching": _boolean(container, "image_caching", operation),
                "priority": _string(container, "priority", operation),
                "resources": resources_payload,
                "environment_variables": environment,
            },
            "queue_connection": {
                "path": _string(connection, "path", operation),
                "port": _integer(connection, "port", operation),
                "queue_name": _string(connection, "queue_name", operation),
            },
            "queue_autoscaler": autoscaler_payload,
            "startup_probe": _probe(group, "startup_probe", operation),
            "readiness_probe": _probe(group, "readiness_probe", operation),
            "liveness_probe": _probe(group, "liveness_probe", operation),
        },
    }
