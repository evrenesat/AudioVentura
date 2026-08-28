from __future__ import annotations

import asyncio
import hashlib
import uuid
from pathlib import Path
from threading import Event

from ace_midi_mock.db import MockDatabase
from ace_midi_mock.renderer import RenderedOutput
from ace_midi_mock.worker import MockWorker, Submission


class RecordingRenderer:
    def __init__(self) -> None:
        self.indices: list[int] = []

    def render(
        self, member_index: int, job_directory: Path, *, cancelled: Event | None = None
    ) -> RenderedOutput:
        del cancelled
        self.indices.append(member_index)
        job_directory.mkdir(parents=True, exist_ok=True)
        path = job_directory / "output.mp3"
        payload = b"ID3" + member_index.to_bytes(2, "big")
        path.write_bytes(payload)
        return RenderedOutput(
            path=path,
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
            duration_seconds=float(member_index + 1),
        )


class NoopUploader:
    async def upload(self, url: str, rendered: RenderedOutput) -> None:
        del url, rendered

    async def aclose(self) -> None:
        return None


def _submission(application_job_id: str, nonce: str, *, prompt: str) -> Submission:
    return Submission(
        application_job_id=application_job_id,
        variation_index=1,
        submission_nonce=nonce,
        result_upload_url="https://transfer.test/result",
        result_upload_max_bytes=1_000_000,
        source={"url": "https://source.invalid/capability"},
        input_payload={"generation": {"prompt": prompt, "duration": 600}},
    )


def test_worker_serializes_claims_and_replays_nonce_without_reallocation(
    fixture_manifest, mock_settings
) -> None:
    _archive_path, manifest = fixture_manifest
    database = MockDatabase(mock_settings.database_path, manifest)
    renderer = RecordingRenderer()
    worker = MockWorker(
        mock_settings,
        manifest,
        database,
        renderer=renderer,  # type: ignore[arg-type]
        uploader=NoopUploader(),  # type: ignore[arg-type]
    )
    application_id = str(uuid.uuid4())
    first = _submission(application_id, str(uuid.uuid4()), prompt="first")
    second = _submission(application_id, str(uuid.uuid4()), prompt="second")
    claimed_ids: list[str] = []

    async def scenario() -> None:
        await worker.start()
        first_job, first_created = worker.submit(first)
        second_job, second_created = worker.submit(second)
        claimed_ids.extend((first_job.id, second_job.id))
        await worker._queue.join()
        replay, replay_created = worker.submit(first)
        assert first_created and second_created and not replay_created
        assert replay.external_uuid == first_job.external_uuid
        await worker.stop()

    asyncio.run(scenario())
    assert renderer.indices == [0, 1]
    jobs = [database.get(job_id) for job_id in claimed_ids]
    assert [job.corpus_index for job in jobs if job is not None] == [0, 1]
    assert database.cursor_snapshot()["last_consumed_index"] == 1
