from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from typing import Any

from fastapi.testclient import TestClient

from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import (
    AssetTransferCapability,
    AssetTransferPurpose,
    Job,
    JobType,
    Project,
    SourceAsset,
    SourceAssetOrigin,
    utc_now,
)
from ace_service.providers.base import (
    BackendId,
    BackendOperation,
    InferenceMode,
    ProviderCapabilities,
    ProviderName,
    RequestFeature,
)
from ace_service.providers.registry import BackendRegistry
from ace_service.repository import (
    complete_asset_upload,
    create_project,
    create_source_asset,
    issue_asset_transfer_capability,
    mark_source_preparing,
)
from ace_service.source_assets import publish_ready_source


class _SourceCoordinator:
    def __init__(self) -> None:
        self.home_ingest_semaphore = asyncio.Semaphore(1)
        self.enqueued_source: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def enqueue_source(self, source_asset_id: str) -> None:
        self.enqueued_source.append(source_asset_id)


class _Worker:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def enqueue(self, job_id: str) -> bool:
        self.enqueued.append(job_id)
        return True


class _Home:
    async def health(self) -> None:
        return None

    async def aclose(self) -> None:
        return None


class _SourceBackend:
    def __init__(
        self,
        *,
        name: ProviderName = ProviderName.MOCK,
        backend_id: str = "mock/midi-sequential",
        source_duration_min_seconds: float | None = 1,
        source_duration_max_seconds: float | None = 600,
        operation: BackendOperation = BackendOperation.AUDIO_TRANSFORM,
    ) -> None:
        self.capabilities = ProviderCapabilities(
            name=name,
            modes=frozenset({InferenceMode.AUDIO_TO_AUDIO}),
            request_features=frozenset(RequestFeature),
            accepts_worker_schema=frozenset({2}),
            supports_pending_cancel=True,
            supports_running_cancel=False,
            not_found_after_deadline_is_terminal=True,
            backend_id=BackendId(backend_id),
            operation=operation,
            native_formats=frozenset({"mp3"}),
            source_duration_min_seconds=source_duration_min_seconds,
            source_duration_max_seconds=source_duration_max_seconds,
            output_duration_min_seconds=1,
            output_duration_max_seconds=600,
        )


def _auth() -> tuple[str, str]:
    return ("change-me", "test-password")


def _make_app(
    settings: ServiceSettings,
    *,
    providers: list[_SourceBackend] | None = None,
    defaults: dict[BackendOperation, str] | None = None,
    selectable_backends: list[str] | None = None,
):
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    coordinator = _SourceCoordinator()
    worker = _Worker()
    source_providers = providers or [_SourceBackend()]
    registry = BackendRegistry(
        source_providers,
        defaults=defaults,
        selectable_backends=selectable_backends
        or [str(provider.capabilities.backend_id) for provider in source_providers],
    )
    app = create_app(
        settings,
        session_factory=factory,
        provider_registry=registry,
        home_ingest_client=_Home(),
        worker=worker,
        source_coordinator=coordinator,
    )
    return app, engine, factory, coordinator, worker


def _csrf(client: TestClient, path: str = "/sources/new") -> str:
    response = client.get(path, auth=_auth())
    assert response.status_code == 200
    token = client.cookies.get("ace_csrf")
    assert token
    return token


def _seed_ready_source(
    factory: Any,
    settings: ServiceSettings,
    *,
    duration: float,
    preferred_backend: str | None = None,
) -> tuple[str, str]:
    with factory() as session:
        project = create_project(session, job_type=JobType.COVER, title="Long source")
        asset = create_source_asset(
            session,
            project=project,
            origin=SourceAssetOrigin.YOUTUBE,
            display_title="Long source",
            youtube_url="https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            youtube_video_id="dQw4w9WgXcQ",
            rights_confirmation_at=utc_now(),
            preferred_remix_backend=preferred_backend,
        )
        mark_source_preparing(session, asset.id)
        payload = b"verified source mp3"
        path = settings.paths.source_library(asset.id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
        issued = issue_asset_transfer_capability(
            session,
            purpose=AssetTransferPurpose.HOME_SOURCE_MP3_UPLOAD,
            source_asset_id=asset.id,
            expected_relative_path=f"sources/{asset.id}/source.mp3",
            expected_extension=".mp3",
            expected_mime_type="audio/mpeg",
            max_bytes=settings.canonical_source_max_bytes,
            expires_at=utc_now() + timedelta(hours=1),
        )
        complete_asset_upload(
            session,
            issued.capability.id,
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        item = publish_ready_source(
            session,
            asset.id,
            settings=settings,
            duration_seconds=duration,
            canonical_byte_size=len(payload),
            canonical_sha256=hashlib.sha256(payload).hexdigest(),
        )
        session.commit()
        return project.id, item.id


def test_source_picker_upload_lifecycle_hides_capability_from_status(settings) -> None:
    app, engine, _factory, coordinator, _worker = _make_app(settings)
    try:
        with TestClient(app) as client:
            new_page = client.get("/sources/new", auth=_auth())
            assert new_page.status_code == 200
            assert 'data-source-tab="youtube"' in new_page.text
            assert 'data-source-tab="upload"' in new_page.text
            csrf = _csrf(client)
            init = client.post(
                "/sources/uploads",
                auth=_auth(),
                json={
                    "csrf_token": csrf,
                    "backend": "mock/midi-sequential",
                    "project_title": "Uploaded source",
                    "filename": "concert.mp4",
                    "byte_size": 1234,
                    "rights_confirmation": True,
                },
            )
            assert init.status_code == 200
            body = init.json()
            upload_url = body["upload_url"]
            assert "/asset-transfer/v2/upload/" in upload_url
            status_page = client.get(
                body["status_url"], auth=_auth(), headers={"Accept": "text/html"}
            )
            assert status_page.status_code == 200
            assert upload_url not in status_page.text
            assert body["source_asset_id"] not in coordinator.enqueued_source

            incomplete = client.post(
                body["upload_complete_url"],
                auth=_auth(),
                json={"csrf_token": csrf},
            )
            assert incomplete.status_code == 409

            cancelled = client.post(
                body["cancel_url"],
                auth=_auth(),
                json={"csrf_token": csrf},
                headers={"Accept": "application/json"},
            )
            assert cancelled.status_code == 200
            assert cancelled.json()["status"] == "cancelled"

            retried = client.post(
                body["status_url"].replace("/status", "/retry"),
                auth=_auth(),
                json={"csrf_token": csrf},
                headers={"Accept": "application/json"},
            )
            assert retried.status_code == 200
            assert retried.json()["status"] == "awaiting_upload"
            assert retried.json()["upload_url"] != upload_url
    finally:
        engine.dispose()


def test_youtube_source_enqueues_and_long_source_remix_freezes_range(settings) -> None:
    app, engine, factory, coordinator, worker = _make_app(settings)
    try:
        with TestClient(app) as client:
            csrf = _csrf(client)
            youtube = client.post(
                "/sources/youtube",
                auth=_auth(),
                data={
                    "csrf_token": csrf,
                    "backend": "mock/midi-sequential",
                    "project_title": "YouTube source",
                    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "rights_confirmation": "1",
                },
                follow_redirects=False,
            )
            assert youtube.status_code == 303
            youtube_source_id = youtube.headers["location"].split("/")[-2]
            assert youtube_source_id in coordinator.enqueued_source

        project_id, source_item_id = _seed_ready_source(factory, settings, duration=700)
        with TestClient(app) as client:
            remix = client.get(
                f"/projects/{project_id}/remixes/new?backend=mock/midi-sequential",
                auth=_auth(),
            )
            assert remix.status_code == 200
            assert 'data-source-duration="700.0"' in remix.text
            csrf = client.cookies.get("ace_csrf")
            assert csrf
            fields = {
                "csrf_token": csrf,
                "backend": "mock/midi-sequential",
                "source_media_item_id": source_item_id,
                "clip_start_seconds": "0",
                "clip_end_seconds": "600",
                "target_style": "minimal piano",
                "duration_mode": "source",
                "variation_count": "1",
                "output_format": "mp3",
            }
            needs_confirmation = client.post(
                f"/projects/{project_id}/remixes",
                auth=_auth(),
                data=fields,
                follow_redirects=False,
            )
            assert needs_confirmation.status_code == 422
            assert "confirm the selected range" in needs_confirmation.text
            assert worker.enqueued == []

            fields["range_confirmation"] = "1"
            accepted = client.post(
                f"/projects/{project_id}/remixes",
                auth=_auth(),
                data=fields,
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            job_id = accepted.headers["location"].split("/")[-1]
            assert worker.enqueued == [job_id]

        with factory() as session:
            from ace_service.repository import get_job

            job = get_job(session, job_id)
            assert job is not None
            assert job.source_url is None
            assert job.source_media_item_id == source_item_id
            assert job.source_clip_start_seconds == 0
            assert job.source_clip_end_seconds == 600
            assert job.source_clip_duration_seconds == 600
            assert job.backend_snapshot_json["source_duration_max_seconds"] == 600
    finally:
        engine.dispose()


def test_source_picker_renders_one_required_reviewed_backend_selector(settings) -> None:
    app, engine, _factory, _coordinator, _worker = _make_app(settings)
    try:
        with TestClient(app) as client:
            response = client.get("/sources/new", auth=_auth())
        assert response.status_code == 200
        assert 'id="backend" name="backend" form="youtube-source-form"' in response.text
        assert 'id="backend"' in response.text
        assert response.text.count('value="mock/midi-sequential"') == 1
        assert response.text.count('value="mock/midi-sequential" selected') == 1
        assert 'src="/static/backend_selector.js"' in response.text
        assert "Source preparation stays at Home Ingest" in response.text
    finally:
        engine.dispose()


def test_source_backend_is_required_before_project_or_capability_creation(settings) -> None:
    app, engine, factory, coordinator, _worker = _make_app(settings)
    try:
        with TestClient(app) as client:
            csrf = _csrf(client)
            youtube = client.post(
                "/sources/youtube",
                auth=_auth(),
                data={
                    "csrf_token": csrf,
                    "project_title": "Missing backend",
                    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "rights_confirmation": "1",
                },
            )
            assert youtube.status_code == 422
            assert "reviewed remix backend" in youtube.text

            upload = client.post(
                "/sources/uploads",
                auth=_auth(),
                json={
                    "csrf_token": csrf,
                    "backend": "mock/not-selectable",
                    "project_title": "Invalid backend",
                    "filename": "source.mp4",
                    "byte_size": 1234,
                    "rights_confirmation": True,
                },
            )
            assert upload.status_code == 422
            assert "reviewed remix backend" in upload.text

        with factory() as session:
            assert session.query(Project).count() == 0
            assert session.query(SourceAsset).count() == 0
            assert session.query(AssetTransferCapability).count() == 0
        assert coordinator.enqueued_source == []
    finally:
        engine.dispose()


def test_source_backend_preference_survives_upload_and_youtube_creation(settings) -> None:
    app, engine, factory, _coordinator, _worker = _make_app(settings)
    try:
        with TestClient(app) as client:
            csrf = _csrf(client)
            init = client.post(
                "/sources/uploads",
                auth=_auth(),
                json={
                    "csrf_token": csrf,
                    "backend": "mock/midi-sequential",
                    "project_title": "Preference upload",
                    "filename": "source.mp4",
                    "byte_size": 1234,
                    "rights_confirmation": True,
                },
            )
            assert init.status_code == 200
            upload_source_id = init.json()["source_asset_id"]

            youtube = client.post(
                "/sources/youtube",
                auth=_auth(),
                data={
                    "csrf_token": csrf,
                    "backend": "mock/midi-sequential",
                    "project_title": "Preference YouTube",
                    "youtube_url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                    "rights_confirmation": "1",
                },
                follow_redirects=False,
            )
            assert youtube.status_code == 303
            youtube_source_id = youtube.headers["location"].split("/")[-2]

            cancelled = client.post(
                init.json()["cancel_url"],
                auth=_auth(),
                json={"csrf_token": csrf},
                headers={"Accept": "application/json"},
            )
            assert cancelled.status_code == 200

            retried = client.post(
                init.json()["status_url"].replace("/status", "/retry"),
                auth=_auth(),
                json={"csrf_token": csrf},
                headers={"Accept": "application/json"},
            )
            assert retried.status_code == 200

        with factory() as session:
            for source_id in (upload_source_id, youtube_source_id):
                source = session.get(SourceAsset, source_id)
                assert source is not None
                assert source.preferred_remix_backend == "mock/midi-sequential"
    finally:
        engine.dispose()


def test_source_remix_preference_precedence_stale_fallback_and_atomic_update(settings) -> None:
    values = settings.model_dump()
    runpod_backend = "runpod/ace-step-v15-xl-turbo"
    values.update(
        inference_enabled_backends=f"mock/midi-sequential,{runpod_backend}",
        default_original_backend=runpod_backend,
        default_cover_backend=runpod_backend,
        mock_token="test-mock-token",
    )
    multi_settings = ServiceSettings(**values)
    providers = [
        _SourceBackend(),
        _SourceBackend(name=ProviderName.RUNPOD, backend_id=runpod_backend),
    ]
    app, engine, factory, _coordinator, worker = _make_app(
        multi_settings,
        providers=providers,
        defaults={BackendOperation.AUDIO_TRANSFORM: runpod_backend},
    )
    try:
        project_id, source_item_id = _seed_ready_source(
            factory,
            multi_settings,
            duration=30,
            preferred_backend="mock/midi-sequential",
        )
        with TestClient(app) as client:
            default_form = client.get(f"/projects/{project_id}/remixes/new", auth=_auth())
            assert default_form.status_code == 200
            assert 'value="mock/midi-sequential" selected' in default_form.text
            assert 'value="runpod/ace-step-v15-xl-turbo" selected' not in default_form.text

            override = client.get(
                f"/projects/{project_id}/remixes/new?backend={runpod_backend}",
                auth=_auth(),
            )
            assert override.status_code == 200
            assert f'value="{runpod_backend}" selected' in override.text

            tampered_query = client.get(
                f"/projects/{project_id}/remixes/new?backend=mock/not-selectable",
                auth=_auth(),
            )
            assert tampered_query.status_code == 200
            assert 'value="mock/midi-sequential" selected' in tampered_query.text
            assert "mock/not-selectable" not in tampered_query.text
            assert "previous remix backend is no longer available" in tampered_query.text

            csrf = client.cookies.get("ace_csrf")
            assert csrf
            invalid = client.post(
                f"/projects/{project_id}/remixes",
                auth=_auth(),
                data={
                    "csrf_token": csrf,
                    "backend": runpod_backend,
                    "source_media_item_id": source_item_id,
                    "clip_start_seconds": "0",
                    "clip_end_seconds": "0",
                    "target_style": "minimal piano",
                    "duration_mode": "source",
                    "variation_count": "1",
                    "output_format": "mp3",
                },
            )
            assert invalid.status_code == 422
            assert worker.enqueued == []

        with factory() as session:
            source = session.query(SourceAsset).filter(SourceAsset.project_id == project_id).one()
            assert source is not None
            assert source.preferred_remix_backend == "mock/midi-sequential"
            assert session.query(Job).count() == 0

        with TestClient(app) as client:
            csrf = _csrf(client, f"/projects/{project_id}/remixes/new")
            accepted = client.post(
                f"/projects/{project_id}/remixes",
                auth=_auth(),
                data={
                    "csrf_token": csrf,
                    "backend": runpod_backend,
                    "source_media_item_id": source_item_id,
                    "clip_start_seconds": "0",
                    "clip_end_seconds": "10",
                    "target_style": "minimal piano",
                    "duration_mode": "source",
                    "variation_count": "1",
                    "output_format": "mp3",
                },
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            job_id = accepted.headers["location"].rsplit("/", 1)[-1]

        with factory() as session:
            source = session.query(SourceAsset).filter(SourceAsset.project_id == project_id).one()
            job = session.get(Job, job_id)
            assert source is not None and job is not None
            assert source.preferred_remix_backend == runpod_backend
            assert job.inference_backend == runpod_backend
            assert job.inference_provider == "runpod"
            assert job.backend_snapshot_json["backend_id"] == runpod_backend
            assert worker.enqueued == [job_id]
    finally:
        engine.dispose()


def test_stale_source_preference_falls_back_without_echoing_unavailable_id(settings) -> None:
    stale_backend = "runpod/ace-step-v15-xl-turbo"
    app, engine, factory, _coordinator, _worker = _make_app(
        settings,
        providers=[
            _SourceBackend(),
            _SourceBackend(name=ProviderName.RUNPOD, backend_id=stale_backend),
        ],
        selectable_backends=["mock/midi-sequential"],
    )
    try:
        project_id, _source_item_id = _seed_ready_source(
            factory, settings, duration=30, preferred_backend=stale_backend
        )
        with TestClient(app) as client:
            response = client.get(f"/projects/{project_id}/remixes/new", auth=_auth())
        assert response.status_code == 200
        assert 'value="mock/midi-sequential" selected' in response.text
        assert "previous remix backend is no longer available" in response.text
        assert stale_backend not in response.text
    finally:
        engine.dispose()
