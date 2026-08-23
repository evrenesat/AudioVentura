from __future__ import annotations

import json
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
    assert result["unclassified"] == []
    assert result["removed"]
