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
            raw_entry.get("pricing") if isinstance(raw_entry.get("pricing"), dict) else None,
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
            response = http.get(
                "models",
                params={
                    "category": category,
                    "status": "active",
                    "expand": "openapi-3.0",
                    "limit": 50,
                },
                headers=headers,
            )
            response.raise_for_status()
            body = response.json()
            for item in body.get("models", []) if isinstance(body, dict) else []:
                if isinstance(item, dict) and isinstance(item.get("endpoint_id"), str):
                    discovered[item["endpoint_id"]] = item
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
        fingerprint = _discovered_schema_fingerprint(item)
        if fingerprint is not None:
            if fingerprint != entry.schema_sha256:
                schema_changed.append(endpoint_id)
    pricing_changed: list[str] = []
    pricing_unavailable = False
    if include_pricing:
        try:
            response = http.get("models/pricing", headers=headers)
            response.raise_for_status()
            body = response.json()
            prices = body.get("prices", body.get("models", [])) if isinstance(body, dict) else body
            if not isinstance(prices, list):
                raise ValueError("Fal pricing response is malformed")
            discovered_prices = {
                item.get("endpoint_id", item.get("model_id")): {
                    key: item.get(key)
                    for key in ("unit_price", "price", "unit", "billing_unit")
                    if key in item
                }
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


def _discovered_schema_fingerprint(item: Mapping[str, Any]) -> str | None:
    """Return a comparable schema hash from a model-search response.

    Tests and future review tooling may provide the already-normalized shape;
    the live Platform API normally returns an OpenAPI document, for which the
    raw document hash is still useful as a conservative drift signal.
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
    else:
        normalized = openapi
    return hashlib.sha256(
        json.dumps(normalized, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


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
