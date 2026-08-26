"""Reviewed Fal Model API catalog and read-only audit helpers.

The catalog is deliberately static at runtime. Fal's discovery API is useful
for review, but live schemas never become production request mappings.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Any

import httpx

from .base import BackendId, BackendOperation, MediaKind, ProviderName, ResultDeliveryMode

CATALOG_SCHEMA_VERSION = 1
DEFAULT_CATALOG_RESOURCE = "fal_music_catalog.json"
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ENDPOINT_RE = re.compile(r"^[A-Za-z0-9._~/-]{1,256}$")
_KNOWN_KEYS = {
    "backend_id",
    "endpoint_id",
    "label",
    "operation",
    "media_kind",
    "adapter",
    "schema_sha256",
    "fields",
    "output",
    "catalog_revision",
    "available",
    "unavailable_reason",
    "pricing",
}
_FIELD_KEYS = {
    "ui_name",
    "fal_name",
    "type",
    "required",
    "default",
    "minimum",
    "maximum",
    "choices",
    "semantic_note",
    "advanced",
}
_OUTPUT_KEYS = {
    "result_path",
    "native_formats",
    "format_field",
    "seed_path",
    "duration_path",
    "max_bytes",
    "content_types",
}
_PRICING_KEYS = {"unit_price", "unit"}
_PRICING_UNIT_ALIASES = {
    "seconds": "second",
    "compute seconds": "compute_second",
    "compute_seconds": "compute_second",
}


@dataclass(frozen=True, slots=True)
class FieldPolicy:
    ui_name: str
    fal_name: str
    type: str
    required: bool = False
    default: Any = None
    minimum: float | int | None = None
    maximum: float | int | None = None
    choices: tuple[str, ...] = ()
    semantic_note: str | None = None
    advanced: bool = False

    @classmethod
    def from_json(cls, name: str, value: Any) -> FieldPolicy:
        if not isinstance(value, dict) or set(value) - _FIELD_KEYS:
            raise ValueError(f"catalog field {name} is malformed")
        ui_name = value.get("ui_name", name)
        fal_name = value.get("fal_name")
        field_type = value.get("type", "string")
        if (
            not isinstance(ui_name, str)
            or not ui_name
            or not isinstance(fal_name, str)
            or not fal_name
        ):
            raise ValueError(f"catalog field {name} has invalid names")
        if field_type not in {"string", "integer", "number", "boolean", "url"}:
            raise ValueError(f"catalog field {name} has unsupported type")
        choices = value.get("choices", ())
        if not isinstance(choices, (list, tuple)) or any(
            not isinstance(item, str) for item in choices
        ):
            raise ValueError(f"catalog field {name} choices are invalid")
        minimum = value.get("minimum")
        maximum = value.get("maximum")
        if minimum is not None and (
            isinstance(minimum, bool) or not isinstance(minimum, (int, float))
        ):
            raise ValueError(f"catalog field {name} minimum is invalid")
        if maximum is not None and (
            isinstance(maximum, bool) or not isinstance(maximum, (int, float))
        ):
            raise ValueError(f"catalog field {name} maximum is invalid")
        if minimum is not None and maximum is not None and minimum > maximum:
            raise ValueError(f"catalog field {name} bounds are reversed")
        return cls(
            ui_name,
            fal_name,
            field_type,
            bool(value.get("required", False)),
            value.get("default"),
            minimum,
            maximum,
            tuple(choices),
            value.get("semantic_note"),
            bool(value.get("advanced", False)),
        )


@dataclass(frozen=True, slots=True)
class OutputPolicy:
    result_path: str
    native_formats: tuple[str, ...]
    format_field: str | None
    seed_path: str | None
    duration_path: str | None
    max_bytes: int
    content_types: tuple[str, ...]

    @classmethod
    def from_json(cls, value: Any) -> OutputPolicy:
        if not isinstance(value, dict) or set(value) - _OUTPUT_KEYS:
            raise ValueError("catalog output policy is malformed")
        result_path = value.get("result_path")
        native_formats = value.get("native_formats")
        max_bytes = value.get("max_bytes", 268_435_456)
        if (
            not isinstance(result_path, str)
            or not result_path
            or not isinstance(native_formats, list)
        ):
            raise ValueError("catalog output path/formats are invalid")
        if not native_formats or any(item not in {"mp3", "flac", "wav"} for item in native_formats):
            raise ValueError("catalog output formats are unsupported")
        if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
            raise ValueError("catalog output byte limit is invalid")
        content_types = value.get("content_types") or [
            {"mp3": "audio/mpeg", "flac": "audio/flac", "wav": "audio/wav"}[native_formats[0]]
        ]
        if not isinstance(content_types, list) or any(
            not isinstance(item, str) for item in content_types
        ):
            raise ValueError("catalog output content types are invalid")
        return cls(
            result_path,
            tuple(native_formats),
            value.get("format_field"),
            value.get("seed_path"),
            value.get("duration_path"),
            max_bytes,
            tuple(content_types),
        )


def _pricing_policy(value: Any) -> dict[str, str] | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != _PRICING_KEYS:
        raise ValueError("Fal catalog pricing policy is malformed")
    unit = value.get("unit")
    raw_price = value.get("unit_price")
    if not isinstance(unit, str):
        raise ValueError("Fal catalog pricing unit is invalid")
    unit = _PRICING_UNIT_ALIASES.get(unit.strip().lower(), unit.strip().lower().replace(" ", "_"))
    if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", unit):
        raise ValueError("Fal catalog pricing unit is invalid")
    if isinstance(raw_price, bool) or not isinstance(raw_price, (str, int, Decimal)):
        raise ValueError("Fal catalog unit price must be exact decimal text")
    try:
        price = Decimal(raw_price)
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("Fal catalog unit price is invalid") from exc
    if not price.is_finite() or price <= 0:
        raise ValueError("Fal catalog unit price is invalid")
    return {"unit_price": format(price, "f"), "unit": unit}


@dataclass(frozen=True, slots=True)
class CatalogDescriptor:
    backend_id: BackendId
    endpoint_id: str
    label: str
    operation: BackendOperation
    media_kind: MediaKind
    adapter: str
    schema_sha256: str
    fields: dict[str, FieldPolicy]
    output: OutputPolicy
    catalog_revision: str
    available: bool = True
    unavailable_reason: str | None = None
    pricing: dict[str, Any] | None = None

    @property
    def provider(self) -> ProviderName:
        return ProviderName.FAL

    @property
    def result_delivery(self) -> ResultDeliveryMode:
        return ResultDeliveryMode.CONTROLLER_PULL

    def snapshot(self) -> dict[str, Any]:
        return {
            "backend_id": str(self.backend_id),
            "provider": self.provider.value,
            "endpoint_id": self.endpoint_id,
            "label": self.label,
            "operation": self.operation.value,
            "media_kind": self.media_kind.value,
            "catalog_revision": self.catalog_revision,
            "adapter": self.adapter,
            "native_formats": list(self.output.native_formats),
            "result_path": self.output.result_path,
            "result_delivery": self.result_delivery.value,
        }

    def normalized_schema(self) -> dict[str, Any]:
        return {
            "endpoint_id": self.endpoint_id,
            "operation": self.operation.value,
            "media_kind": self.media_kind.value,
            "fields": {
                name: {
                    "fal_name": field.fal_name,
                    "type": field.type,
                    "required": field.required,
                    "minimum": field.minimum,
                    "maximum": field.maximum,
                    "choices": list(field.choices),
                }
                for name, field in sorted(self.fields.items())
            },
            "output": {
                "result_path": self.output.result_path,
                "native_formats": list(self.output.native_formats),
                "format_field": self.output.format_field,
                "seed_path": self.output.seed_path,
                "duration_path": self.output.duration_path,
            },
        }

    def schema_fingerprint(self) -> str:
        encoded = json.dumps(
            self.normalized_schema(), sort_keys=True, separators=(",", ":")
        ).encode()
        return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ReviewedCatalog:
    revision: str
    entries: tuple[CatalogDescriptor, ...]
    excluded: tuple[dict[str, str], ...]

    def by_backend_id(self, backend_id: BackendId | str) -> CatalogDescriptor:
        normalized = BackendId(str(backend_id))
        for entry in self.entries:
            if entry.backend_id == normalized:
                return entry
        raise KeyError(f"unknown catalog backend: {normalized}")

    def selectable(
        self,
        operation: BackendOperation | str,
        *,
        allowed_media_kinds: frozenset[str] = frozenset({"music"}),
    ) -> tuple[CatalogDescriptor, ...]:
        requested = BackendOperation(operation)
        return tuple(
            entry
            for entry in self.entries
            if entry.available
            and entry.operation is requested
            and entry.media_kind.value in allowed_media_kinds
        )


def _resource_path() -> Path:
    return Path(str(files("ace_service.providers").joinpath(DEFAULT_CATALOG_RESOURCE)))


def load_catalog(path: Path | str | None = None) -> ReviewedCatalog:
    resolved = Path(path) if path is not None else _resource_path()
    if path is not None and (
        not resolved.is_absolute() or resolved.is_symlink() or not resolved.is_file()
    ):
        raise ValueError("catalog path must be an absolute regular non-symlink file")
    try:
        raw = json.loads(resolved.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Fal catalog is not valid UTF-8 JSON") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != CATALOG_SCHEMA_VERSION:
        raise ValueError("unsupported Fal catalog schema")
    revision = raw.get("catalog_revision")
    values = raw.get("entries")
    excluded = raw.get("excluded", [])
    if not isinstance(revision, str) or not revision or not isinstance(values, list):
        raise ValueError("Fal catalog root is malformed")
    if not isinstance(excluded, list) or any(
        not isinstance(item, dict)
        or set(item) - {"endpoint_id", "reason"}
        or not isinstance(item.get("endpoint_id"), str)
        or not isinstance(item.get("reason"), str)
        for item in excluded
    ):
        raise ValueError("Fal catalog exclusions are malformed")
    entries: list[CatalogDescriptor] = []
    seen: set[BackendId] = set()
    for raw_entry in values:
        if not isinstance(raw_entry, dict) or set(raw_entry) - _KNOWN_KEYS:
            raise ValueError("Fal catalog entry has unknown keys")
        endpoint_id = raw_entry.get("endpoint_id")
        backend_id = raw_entry.get("backend_id")
        if not isinstance(endpoint_id, str) or not _ENDPOINT_RE.fullmatch(endpoint_id):
            raise ValueError("Fal catalog endpoint ID is invalid")
        if not isinstance(backend_id, str) or BackendId(backend_id) in seen:
            raise ValueError("Fal catalog backend IDs must be valid and unique")
        if backend_id != f"fal/{endpoint_id}":
            raise ValueError("Fal backend ID must be derived from the catalog endpoint")
        seen.add(BackendId(backend_id))
        schema_sha256 = raw_entry.get("schema_sha256")
        if not isinstance(schema_sha256, str) or not _SHA256_RE.fullmatch(schema_sha256):
            raise ValueError("Fal catalog schema fingerprint is invalid")
        fields_raw = raw_entry.get("fields")
        if not isinstance(fields_raw, dict):
            raise ValueError("Fal catalog fields are malformed")
        fields = {name: FieldPolicy.from_json(name, value) for name, value in fields_raw.items()}
        output = OutputPolicy.from_json(raw_entry.get("output"))
        operation_value = raw_entry.get("operation")
        media_kind_value = raw_entry.get("media_kind")
        if not isinstance(operation_value, str) or not isinstance(media_kind_value, str):
            raise ValueError("Fal catalog operation or media kind is invalid")
        entry = CatalogDescriptor(
            BackendId(backend_id),
            endpoint_id,
            str(raw_entry.get("label", "")),
            BackendOperation(operation_value),
            MediaKind(media_kind_value),
            str(raw_entry.get("adapter", "")),
            schema_sha256,
            fields,
            output,
            str(raw_entry.get("catalog_revision", revision)),
            bool(raw_entry.get("available", True)),
            raw_entry.get("unavailable_reason"),
            _pricing_policy(raw_entry.get("pricing")),
        )
        if not entry.label or not entry.adapter:
            raise ValueError("Fal catalog label and adapter are required")
        entries.append(entry)
    return ReviewedCatalog(
        revision,
        tuple(entries),
        tuple({"endpoint_id": item["endpoint_id"], "reason": item["reason"]} for item in excluded),
    )


def audit_catalog(
    catalog: ReviewedCatalog,
    *,
    client: httpx.Client | None = None,
    api_key: str | None = None,
    include_pricing: bool = False,
) -> dict[str, Any]:
    """Compare the reviewed catalog with active Fal endpoint metadata."""

    owns_client = client is None
    http = client or httpx.Client(base_url="https://api.fal.ai/v1/", timeout=30)
    headers = {"Authorization": f"Key {api_key}"} if api_key else {}
    discovered: dict[str, dict[str, Any]] = {}
    try:
        for category in ("text-to-audio", "audio-to-audio"):
            cursor: str | None = None
            seen_cursors: set[str] = set()
            while True:
                params = {
                    "category": category,
                    "status": "active",
                    "expand": "openapi-3.0",
                    # Fal limits expanded model-list responses to ten rows.
                    "limit": 10,
                }
                if cursor is not None:
                    params["cursor"] = cursor
                response = http.get("models", params=params, headers=headers)
                response.raise_for_status()
                body = response.json()
                for item in body.get("models", []) if isinstance(body, dict) else []:
                    if isinstance(item, dict) and isinstance(item.get("endpoint_id"), str):
                        discovered[item["endpoint_id"]] = {**item, "_category": category}
                next_cursor = body.get("next_cursor") if isinstance(body, dict) else None
                if not isinstance(next_cursor, str) or not next_cursor:
                    break
                if next_cursor in seen_cursors:
                    raise ValueError("Fal model catalog returned a repeated cursor")
                seen_cursors.add(next_cursor)
                cursor = next_cursor
    except Exception:
        if owns_client:
            http.close()
        raise
    reviewed = {entry.endpoint_id: entry for entry in catalog.entries}
    added = sorted(set(discovered) - set(reviewed))
    removed = sorted(set(reviewed) - set(discovered))
    schema_changed: list[str] = []
    for endpoint_id, item in discovered.items():
        entry = reviewed.get(endpoint_id)
        if entry is None:
            continue
        fingerprint = _discovered_schema_fingerprint(item, descriptor=entry)
        if fingerprint is not None:
            if fingerprint != entry.schema_sha256:
                schema_changed.append(endpoint_id)
    pricing_changed: list[str] = []
    pricing_unavailable = False
    if include_pricing:
        try:
            priced_endpoints = sorted(
                endpoint_id for endpoint_id, entry in reviewed.items() if entry.pricing is not None
            )
            response = http.get(
                "models/pricing",
                params={"endpoint_id": ",".join(priced_endpoints)},
                headers=headers,
            )
            response.raise_for_status()
            body = json.loads(response.content, parse_float=Decimal)
            prices = body.get("prices", body.get("models", [])) if isinstance(body, dict) else body
            if not isinstance(prices, list):
                raise ValueError("Fal pricing response is malformed")
            discovered_prices = {
                item.get("endpoint_id", item.get("model_id")): _pricing_policy(
                    {
                        "unit_price": item.get("unit_price", item.get("price")),
                        "unit": item.get("unit", item.get("billing_unit")),
                    }
                )
                for item in prices
                if isinstance(item, dict)
                and isinstance(item.get("endpoint_id", item.get("model_id")), str)
            }
            for endpoint_id, entry in reviewed.items():
                if entry.pricing is not None and entry.pricing != discovered_prices.get(
                    endpoint_id
                ):
                    pricing_changed.append(endpoint_id)
        except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError):
            pricing_unavailable = True
    if owns_client:
        http.close()
    return {
        "catalog_revision": catalog.revision,
        "reviewed": len(catalog.entries),
        "active_discovered": len(discovered),
        "added": added,
        "removed": removed,
        "deprecated": [],
        "schema_changed": sorted(schema_changed),
        "pricing_changed": sorted(pricing_changed),
        "pricing_unavailable": pricing_unavailable,
        "unclassified": sorted(
            endpoint_id for endpoint_id in discovered if endpoint_id not in reviewed
        ),
    }


def _discovered_schema_fingerprint(
    item: Mapping[str, Any], *, descriptor: CatalogDescriptor | None = None
) -> str | None:
    """Return a comparable schema hash from a model-search response.

    Tests and future review tooling may provide the already-normalized shape;
    the live Platform API normally returns an OpenAPI document, which is
    reduced to the same reviewed input/output representation before hashing.
    """

    declared = item.get("schema_sha256")
    if isinstance(declared, str) and _SHA256_RE.fullmatch(declared):
        return declared
    openapi = item.get("openapi")
    if not isinstance(openapi, dict):
        return None
    normalized_keys = {"endpoint_id", "operation", "media_kind", "fields", "output"}
    if normalized_keys <= set(openapi):
        normalized = {
            "endpoint_id": openapi["endpoint_id"],
            "operation": openapi["operation"],
            "media_kind": openapi["media_kind"],
            "fields": openapi["fields"],
            "output": openapi["output"],
        }
    elif descriptor is not None:
        normalized = normalize_openapi_schema(openapi, descriptor=descriptor)
    else:
        normalized = normalize_openapi_schema(openapi, endpoint_id=str(item.get("endpoint_id", "")))
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _resolve_openapi_ref(schema: Any, components: Mapping[str, Any]) -> Any:
    if not isinstance(schema, Mapping):
        return schema
    reference = schema.get("$ref")
    if not isinstance(reference, str) or not reference.startswith("#/components/"):
        return schema
    current: Any = components
    for part in reference.removeprefix("#/components/").split("/"):
        if not isinstance(current, Mapping):
            return {}
        current = current.get(part)
    return current if current is not None else {}


def _openapi_schema_properties(schema: Any, components: Mapping[str, Any]) -> dict[str, Any]:
    resolved = _resolve_openapi_ref(schema, components)
    if not isinstance(resolved, Mapping):
        return {}
    properties = resolved.get("properties")
    return dict(properties) if isinstance(properties, Mapping) else {}


def _openapi_request_schema(openapi: Mapping[str, Any], components: Mapping[str, Any]) -> Any:
    paths = openapi.get("paths")
    if isinstance(paths, Mapping):
        for path_item in paths.values():
            if not isinstance(path_item, Mapping):
                continue
            for operation in path_item.values():
                if not isinstance(operation, Mapping):
                    continue
                body = operation.get("requestBody")
                if not isinstance(body, Mapping):
                    continue
                content = body.get("content")
                if isinstance(content, Mapping):
                    json_content = content.get("application/json")
                    if isinstance(json_content, Mapping) and "schema" in json_content:
                        return _resolve_openapi_ref(json_content["schema"], components)
    for name in ("Input", "InputSchema", "Request", "RequestSchema"):
        candidate = (
            components.get("schemas", {}).get(name)
            if isinstance(components.get("schemas"), Mapping)
            else None
        )
        if candidate is not None:
            return _resolve_openapi_ref(candidate, components)
    return {}


def _openapi_response_schema(openapi: Mapping[str, Any], components: Mapping[str, Any]) -> Any:
    paths = openapi.get("paths")
    if isinstance(paths, Mapping):
        # Fal's expanded OpenAPI describes both queue submission and eventual
        # result retrieval. Prefer the result GET; the root POST response is
        # only the queue receipt and never contains the generated artifact.
        for path, path_item in paths.items():
            if not str(path).endswith("/requests/{request_id}") or not isinstance(
                path_item, Mapping
            ):
                continue
            operation = path_item.get("get")
            if not isinstance(operation, Mapping):
                continue
            responses = operation.get("responses")
            if not isinstance(responses, Mapping):
                continue
            response = responses.get("200", responses.get("201", responses.get("default")))
            if not isinstance(response, Mapping):
                continue
            content = response.get("content")
            if isinstance(content, Mapping):
                json_content = content.get("application/json")
                if isinstance(json_content, Mapping) and "schema" in json_content:
                    return _resolve_openapi_ref(json_content["schema"], components)
        for path_item in paths.values():
            if not isinstance(path_item, Mapping):
                continue
            for operation in path_item.values():
                if not isinstance(operation, Mapping):
                    continue
                responses = operation.get("responses")
                if not isinstance(responses, Mapping):
                    continue
                response = responses.get("200", responses.get("201", responses.get("default")))
                if not isinstance(response, Mapping):
                    continue
                content = response.get("content")
                if isinstance(content, Mapping):
                    json_content = content.get("application/json")
                    if isinstance(json_content, Mapping) and "schema" in json_content:
                        return _resolve_openapi_ref(json_content["schema"], components)
    for name in ("Output", "OutputSchema", "Response", "ResponseSchema"):
        candidate = (
            components.get("schemas", {}).get(name)
            if isinstance(components.get("schemas"), Mapping)
            else None
        )
        if candidate is not None:
            return _resolve_openapi_ref(candidate, components)
    return {}


def _openapi_path_schema(schema: Any, path: str, components: Mapping[str, Any]) -> Any | None:
    current = _resolve_openapi_ref(schema, components)
    for part in path.split("."):
        match = re.fullmatch(r"([A-Za-z0-9_-]+)(?:\[(\d+)\])?", part)
        if match is None:
            return None
        properties = _openapi_schema_properties(current, components)
        current = properties.get(match.group(1))
        if current is None:
            return None
        current = _resolve_openapi_ref(current, components)
        if match.group(2) is not None:
            items = current.get("items") if isinstance(current, Mapping) else None
            current = _resolve_openapi_ref(items, components)
    return current


def _openapi_field_type(schema: Any) -> str:
    if not isinstance(schema, Mapping):
        return "string"
    variants = schema.get("anyOf")
    if isinstance(variants, list):
        concrete = [
            item for item in variants if isinstance(item, Mapping) and item.get("type") != "null"
        ]
        if len(concrete) == 1:
            return _openapi_field_type(concrete[0])
    if schema.get("format") in {"uri", "url"}:
        return "url"
    value = schema.get("type")
    return (
        value
        if value in {"string", "integer", "number", "boolean", "object", "array"}
        else "string"
    )


def _openapi_field_contract(
    name: str, schema: Any, required: set[str], components: Mapping[str, Any]
) -> dict[str, Any]:
    resolved = _resolve_openapi_ref(schema, components)
    return {
        "fal_name": name,
        "type": _openapi_field_type(resolved),
        "required": name in required,
        "minimum": resolved.get("minimum") if isinstance(resolved, Mapping) else None,
        "maximum": resolved.get("maximum") if isinstance(resolved, Mapping) else None,
        "choices": list(resolved.get("enum", []))
        if isinstance(resolved, Mapping) and isinstance(resolved.get("enum"), list)
        else [],
    }


def normalize_openapi_schema(
    openapi: Mapping[str, Any],
    *,
    descriptor: CatalogDescriptor | None = None,
    endpoint_id: str | None = None,
    operation: BackendOperation | str | None = None,
    media_kind: MediaKind | str | None = None,
) -> dict[str, Any]:
    """Normalize an OpenAPI 3 document into the reviewed catalog shape."""

    if {"endpoint_id", "operation", "media_kind", "fields", "output"} <= set(openapi):
        return {
            "endpoint_id": openapi["endpoint_id"],
            "operation": openapi["operation"],
            "media_kind": openapi["media_kind"],
            "fields": openapi["fields"],
            "output": openapi["output"],
        }
    resolved_endpoint = endpoint_id or (str(descriptor.endpoint_id) if descriptor else "")
    resolved_operation = (
        descriptor.operation.value
        if descriptor is not None
        else BackendOperation(operation).value
        if operation is not None
        else "text_to_music"
    )
    resolved_media = (
        descriptor.media_kind.value
        if descriptor is not None
        else MediaKind(media_kind).value
        if media_kind is not None
        else "music"
    )
    components = openapi.get("components")
    components = components if isinstance(components, Mapping) else {}
    request_schema = _openapi_request_schema(openapi, components)
    request_properties = _openapi_schema_properties(request_schema, components)
    request_required = _resolve_openapi_ref(request_schema, components).get("required", [])
    request_required = set(request_required) if isinstance(request_required, list) else set()
    fields: dict[str, Any] = {}
    if descriptor is not None:
        declared_names = {policy.fal_name for policy in descriptor.fields.values()}
        for name, policy in sorted(descriptor.fields.items()):
            actual = request_properties.get(policy.fal_name)
            if actual is None:
                fields[name] = {"missing": True}
                continue
            contract = _openapi_field_contract(
                policy.fal_name, actual, request_required, components
            )
            # Fal commonly omits the optional JSON-Schema URI format even
            # though the endpoint accepts the controller's validated URL.
            if policy.type == "url" and contract["type"] == "string":
                contract["type"] = "url"
            fields[name] = contract
        for fal_name, actual in sorted(request_properties.items()):
            if fal_name not in declared_names and fal_name in request_required:
                fields[f"__unexpected__:{fal_name}"] = _openapi_field_contract(
                    fal_name, actual, request_required, components
                )
        for fal_name in sorted(request_required - set(request_properties)):
            fields[f"__unexpected_required__:{fal_name}"] = {
                "fal_name": fal_name,
                "required": True,
                "missing": True,
            }
    else:
        for name, actual_value in sorted(request_properties.items()):
            fields[name] = _openapi_field_contract(name, actual_value, request_required, components)
    output = descriptor.output if descriptor is not None else None
    response_schema = _openapi_response_schema(openapi, components)
    result_schema = (
        _openapi_path_schema(response_schema, output.result_path, components)
        if output is not None
        else _openapi_path_schema(response_schema, "audio.url", components)
    )
    result_path = (
        "__missing__"
        if output is not None and result_schema is None
        else output.result_path
        if output is not None
        else "audio.url"
    )
    normalized_output: dict[str, Any] = {
        "result_path": result_path,
        "native_formats": list(output.native_formats) if output is not None else [],
        "format_field": output.format_field if output is not None else None,
        "seed_path": output.seed_path if output is not None else None,
        "duration_path": output.duration_path if output is not None else None,
    }
    if output is not None and result_schema is not None:
        result_type = _openapi_field_type(result_schema)
        # Result URLs are frequently declared as plain strings. Runtime still
        # requires HTTPS and a maintained Fal CDN host before downloading.
        if result_type not in {"url", "string"}:
            normalized_output["result_type"] = result_type
    if output is not None:
        expected_types = {
            path: {"integer"} if path == output.seed_path else {"integer", "number"}
            for path in (output.seed_path, output.duration_path)
            if path
        }
        incompatible_types: dict[str, dict[str, Any]] = {}
        for path, accepted_types in expected_types.items():
            actual = _openapi_path_schema(response_schema, path, components)
            if actual is None:
                continue
            actual_type = _openapi_field_type(actual)
            if actual_type not in accepted_types:
                incompatible_types[path] = {
                    "expected": sorted(accepted_types),
                    "actual": actual_type,
                }
        if incompatible_types:
            normalized_output["__incompatible_types__"] = incompatible_types
        missing_paths = sorted(
            path
            for path in (output.seed_path, output.duration_path)
            if path and _openapi_path_schema(response_schema, path, components) is None
        )
        if missing_paths:
            normalized_output["__missing_paths__"] = missing_paths
    return {
        "endpoint_id": resolved_endpoint,
        "operation": resolved_operation,
        "media_kind": resolved_media,
        "fields": fields,
        "output": normalized_output,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ace_service fal-catalog audit")
    parser.add_argument("--catalog", type=Path, default=None)
    args = parser.parse_args(argv)
    catalog = load_catalog(args.catalog)
    api_key = os.environ.get("FAL_KEY")
    print(
        json.dumps(
            audit_catalog(catalog, api_key=api_key, include_pricing=bool(api_key)),
            indent=2,
            sort_keys=True,
        )
    )
    return 0
