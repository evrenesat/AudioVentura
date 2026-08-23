from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

import httpx

from ace_service.providers.base import BackendOperation
from ace_service.providers.fal_catalog import audit_catalog, load_catalog


def test_reviewed_catalog_contains_original_and_cover_choices() -> None:
    catalog = load_catalog()
    original = catalog.selectable(BackendOperation.TEXT_TO_MUSIC)
    cover = catalog.selectable(BackendOperation.AUDIO_TRANSFORM)

    assert any(item.endpoint_id == "cassetteai/music-generator" for item in original)
    assert any(item.endpoint_id == "minimax/music-3" for item in original)
    assert all(item.media_kind.value == "music" for item in original)
    assert any(item.endpoint_id == "fal-ai/ace-step/audio-to-audio" for item in cover)
    assert all(item.schema_sha256 == item.schema_fingerprint() for item in catalog.entries)
    assert not any(item.endpoint_id == "fal-ai/minimax-music" for item in catalog.entries)


def test_catalog_snapshot_fixture_matches_reviewed_hashes() -> None:
    root = Path(__file__).parent / "fixtures" / "fal_music_catalog_snapshot.json"
    fixture = json.loads(root.read_text(encoding="utf-8"))
    catalog = load_catalog()
    assert fixture["catalog_revision"] == catalog.revision
    assert len(fixture["entries"]) == len(catalog.entries)
    for endpoint_id, expected in fixture["entries"].items():
        assert (
            catalog.by_backend_id(f"fal/{endpoint_id}").schema_sha256 == expected["schema_sha256"]
        )


def test_audit_uses_normalized_schema_fixture_without_mutating_catalog() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/models"
        assert request.url.params["status"] == "active"
        assert request.url.params["expand"] == "openapi-3.0"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "endpoint_id": descriptor.endpoint_id,
                        "openapi": descriptor.normalized_schema(),
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)

    assert result["schema_changed"] == []


def test_audit_paginates_with_fal_expanded_page_limit() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.params["limit"] == "10"
        cursor = request.url.params.get("cursor")
        if cursor is None:
            return httpx.Response(200, json={"models": [], "next_cursor": "page-2"})
        assert cursor == "page-2"
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "endpoint_id": descriptor.endpoint_id,
                        "openapi": descriptor.normalized_schema(),
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)

    assert result["schema_changed"] == []
    assert len(requests) == 4


def _openapi_fixture(descriptor) -> dict[str, object]:
    properties: dict[str, object] = {}
    required: list[str] = []
    for policy in descriptor.fields.values():
        field_type = "string" if policy.type == "url" else policy.type
        value: dict[str, object] = {"type": field_type}
        if policy.type == "url":
            value["format"] = "uri"
        if policy.minimum is not None:
            value["minimum"] = policy.minimum
        if policy.maximum is not None:
            value["maximum"] = policy.maximum
        if policy.choices:
            value["enum"] = list(policy.choices)
        properties[policy.fal_name] = value
        if policy.required:
            required.append(policy.fal_name)

    response: dict[str, object] = {"type": "object", "properties": {}}
    current = response["properties"]
    assert isinstance(current, dict)

    def set_response_path(path: str, schema: dict[str, object]) -> None:
        nonlocal current
        current = response["properties"]
        assert isinstance(current, dict)
        parts = path.split(".")
        for part in parts[:-1]:
            nested = current.get(part)
            if not isinstance(nested, dict):
                nested = {"type": "object", "properties": {}}
                current[part] = nested
            nested_properties = nested["properties"]
            assert isinstance(nested_properties, dict)
            current = nested_properties
        current[parts[-1]] = schema

    set_response_path(descriptor.output.result_path, {"type": "string", "format": "uri"})
    if descriptor.output.seed_path:
        set_response_path(descriptor.output.seed_path, {"type": "integer"})
    if descriptor.output.duration_path:
        set_response_path(descriptor.output.duration_path, {"type": "number"})
    request_schema: dict[str, object] = {"type": "object", "properties": properties}
    if required:
        request_schema["required"] = required
    return {
        "openapi": "3.0.0",
        "paths": {
            "/": {
                "post": {
                    "requestBody": {"content": {"application/json": {"schema": request_schema}}},
                    "responses": {"200": {"content": {"application/json": {"schema": response}}}},
                }
            }
        },
    }


def test_audit_normalizes_live_openapi_shape_before_fingerprinting() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "models": [
                    {
                        "endpoint_id": descriptor.endpoint_id,
                        "openapi": _openapi_fixture(descriptor),
                    }
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)
    assert result["schema_changed"] == []
    assert result["unclassified"] == []
    assert result["removed"]


def test_audit_ignores_new_optional_input_fields() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")
    live = _openapi_fixture(descriptor)
    operation = live["paths"]["/"]["post"]
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    request_schema["properties"]["new_optional"] = {"type": "string"}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"endpoint_id": descriptor.endpoint_id, "openapi": live}]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)
    assert result["schema_changed"] == []


def test_audit_prefers_result_get_schema_over_queue_receipt() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")
    live = _openapi_fixture(descriptor)
    paths = live["paths"]
    paths["/queue"] = paths.pop("/")
    paths["/queue/requests/{request_id}"] = {
        "get": deepcopy(paths["/queue"]["post"]),
    }
    paths["/queue"]["post"]["responses"]["200"]["content"]["application/json"]["schema"] = {
        "type": "object",
        "required": ["request_id", "status"],
        "properties": {
            "request_id": {"type": "string"},
            "status": {"type": "string"},
        },
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"endpoint_id": descriptor.endpoint_id, "openapi": live}]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)
    assert result["schema_changed"] == []


def test_audit_detects_new_required_live_input_fields() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/cassetteai/music-generator")
    live = _openapi_fixture(descriptor)
    paths = live["paths"]
    assert isinstance(paths, dict)
    operation = paths["/"]["post"]
    assert isinstance(operation, dict)
    request_schema = operation["requestBody"]["content"]["application/json"]["schema"]
    assert isinstance(request_schema, dict)
    properties = request_schema["properties"]
    assert isinstance(properties, dict)
    properties["new_required"] = {"type": "string"}
    request_schema["required"] = [*request_schema.get("required", []), "new_required"]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"endpoint_id": descriptor.endpoint_id, "openapi": deepcopy(live)}]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)
    assert result["schema_changed"] == [descriptor.endpoint_id]


def test_audit_detects_seed_and_duration_output_type_drift() -> None:
    catalog = load_catalog()
    descriptor = catalog.by_backend_id("fal/fal-ai/elevenlabs/music")
    live = _openapi_fixture(descriptor)
    paths = live["paths"]
    assert isinstance(paths, dict)
    operation = paths["/"]["post"]
    assert isinstance(operation, dict)
    response_schema = operation["responses"]["200"]["content"]["application/json"]["schema"]
    assert isinstance(response_schema, dict)
    properties = response_schema["properties"]
    assert isinstance(properties, dict)
    properties["seed"] = {"type": "string"}
    properties["duration"] = {"type": "object", "properties": {"value": {"type": "number"}}}

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"models": [{"endpoint_id": descriptor.endpoint_id, "openapi": deepcopy(live)}]},
        )

    transport = httpx.MockTransport(handler)
    with httpx.Client(transport=transport, base_url="https://api.fal.ai/v1/") as client:
        result = audit_catalog(catalog, client=client)
    assert result["schema_changed"] == [descriptor.endpoint_id]
