from __future__ import annotations

import uuid

import httpx
import pytest

from ace_midi_mock.app import create_app
from ace_midi_mock.db import MockDatabase
from ace_midi_mock.worker import MockWorker


class NoopUploader:
    async def aclose(self) -> None:
        return None


@pytest.mark.anyio
async def test_api_auth_nonce_replay_and_safe_result_metadata(
    fixture_manifest, mock_settings
) -> None:
    _archive_path, manifest = fixture_manifest
    database = MockDatabase(mock_settings.database_path, manifest)
    worker = MockWorker(mock_settings, manifest, database, uploader=NoopUploader())
    app = create_app(mock_settings, manifest, database=database, worker=worker)
    application_id = str(uuid.uuid4())
    payload = {
        "schema_version": 2,
        "application_job_id": application_id,
        "variation_index": 1,
        "submission_nonce": str(uuid.uuid4()),
        "input": {
            "generation": {"prompt": "must not be returned", "lyrics": "private"},
            "source": {"url": "https://private.invalid/token"},
        },
        "source": {"url": "https://private.invalid/token", "bytes": 10},
        "result_upload": {"url": "https://transfer.test/result", "max_bytes": 1_000_000},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://mock.test") as client:
        headers = {"Authorization": "Bearer test-token"}
        assert (await client.get("/healthz")).status_code == 401
        first = await client.post("/v1/jobs", headers=headers, json=payload)
        second = await client.post("/v1/jobs", headers=headers, json=payload)
        assert first.status_code == second.status_code == 202
        assert first.json()["job_id"] == second.json()["job_id"]
        assert first.json()["corpus_index"] == second.json()["corpus_index"] == 0
        job = database.get(first.json()["job_id"])
        assert job is not None
        database.mark_running(job.external_uuid)
        database.mark_succeeded(
            job.external_uuid,
            output_bytes=4,
            output_sha256="0" * 64,
            duration_seconds=1.25,
        )
        result = await client.get(f"/v1/jobs/{job.external_uuid}/result", headers=headers)
        assert result.status_code == 200
        body = result.json()
        assert "prompt" not in result.text
        assert body["metadata"]["output"]["bytes"] == 4
        assert body["metadata"]["worker"]["ace_tag"] == "mock/midi-sequential"
