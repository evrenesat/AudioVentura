from __future__ import annotations

import asyncio
import hashlib
import re
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

import ace_service.web as web_routes
from ace_service.app import create_app
from ace_service.config import ServiceSettings
from ace_service.db import create_database_engine, create_session_factory, initialize_database
from ace_service.models import JobStatus, JobType, SubmissionQuote
from ace_service.repository import (
    EvidenceConflictError,
    create_job,
    create_output,
    finalize_cover_job_duration,
    get_job,
    get_submission_quote,
    transition_job,
    upsert_gpu_rate,
    upsert_runtime_calibration,
)
from ace_service.transfers import create_transfer_app
from ace_service.web import capture_submission_quote

RUNTIME_A = "sha256:" + "a" * 64
RUNTIME_B = "sha256:" + "b" * 64


class FakeRunpod:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    async def health(self) -> None:
        if not self.available:
            raise RuntimeError("Runpod unavailable")

    async def submit(self, payload: Any, execution_timeout_ms: int, ttl_ms: int) -> str:
        del payload, execution_timeout_ms, ttl_ms
        return "runpod-job"

    async def status(self, runpod_job_id: str) -> Any:
        del runpod_job_id
        raise RuntimeError("not used by web tests")

    async def aclose(self) -> None:
        return None


class FakeHome:
    def __init__(self, *, available: bool = True) -> None:
        self.available = available

    async def health(self) -> None:
        if not self.available:
            raise RuntimeError("home unavailable")

    async def aclose(self) -> None:
        return None


class StalledHealth:
    def __init__(self) -> None:
        self.cancelled = 0

    async def health(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise

    async def aclose(self) -> None:
        return None


class FakeWorker:
    def __init__(self) -> None:
        self.enqueued: list[str] = []

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    def enqueue(self, job_id: str) -> bool:
        self.enqueued.append(job_id)
        return True


@pytest.fixture
def web_app(settings: ServiceSettings):
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    worker = FakeWorker()
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    yield app, factory, worker
    engine.dispose()


def _auth(client: TestClient) -> tuple[str, str]:
    del client
    return ("change-me", "test-password")


def _csrf(client: TestClient, path: str = "/create") -> str:
    response = client.get(path, auth=_auth(client))
    assert response.status_code == 200
    token = client.cookies.get("ace_csrf")
    assert token
    return token


def test_auth_matrix_csrf_and_security_headers(web_app) -> None:
    app, _, _ = web_app
    with TestClient(app) as client:
        assert client.get("/").status_code == 401
        assert client.get("/", auth=("change-me", "wrong")).status_code == 401
        response = client.get("/", auth=_auth(client))
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "default-src 'self'" in response.headers["content-security-policy"]

        token = _csrf(client)
        missing = client.post("/create", auth=_auth(client), data={"description": "A valid song"})
        assert missing.status_code == 403
        invalid = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": "wrong", "description": "A valid song"},
        )
        assert invalid.status_code == 403
        accepted = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "A valid song"},
            follow_redirects=False,
        )
        assert accepted.status_code == 303
        assert accepted.headers["location"].startswith("/jobs/")


def test_beta_root_path_keeps_complete_browser_contract_under_prefix(settings) -> None:
    beta_settings = ServiceSettings(**{**settings.model_dump(), "service_root_path": "/beta"})
    engine = create_database_engine(beta_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    worker = FakeWorker()
    app = create_app(
        beta_settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    payload = b"prefix-safe generated mp3"
    output_id: int
    original_job_id = "123e4567-e89b-12d3-a456-426614174001"
    relative_path = f"{original_job_id}/variation-01.mp3"
    output_path = beta_settings.paths.outputs / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)
    with factory() as session:
        create_job(
            session,
            job_type=JobType.ORIGINAL,
            job_id=original_job_id,
            variation_count=1,
        )
        output = create_output(
            session,
            job_id=original_job_id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        session.commit()
        output_id = output.id

    def assert_prefixed_browser_attributes(html: str) -> None:
        values = re.findall(r'(?:href|src|action)="([^"]+)"', html)
        assert values
        assert all(value.startswith("/beta/") or value == "/beta/" for value in values)
        assert all(not value.startswith("/beta/beta/") for value in values)

    try:
        with TestClient(app) as client:
            assert client.get("/").status_code == 401
            dashboard = client.get("/", auth=_auth(client))
            assert dashboard.status_code == 200
            assert dashboard.headers["cache-control"] == "no-store"
            assert "default-src 'self'" in dashboard.headers["content-security-policy"]
            assert_prefixed_browser_attributes(dashboard.text)
            history = client.get("/jobs", auth=_auth(client))
            assert history.status_code == 200
            assert_prefixed_browser_attributes(history.text)

            original_form = client.get("/create", auth=_auth(client))
            cover_form = client.get("/cover", auth=_auth(client))
            assert 'action="/beta/create"' in original_form.text
            assert 'action="/beta/cover"' in cover_form.text
            assert_prefixed_browser_attributes(original_form.text)
            assert_prefixed_browser_attributes(cover_form.text)

            token = client.cookies.get("ace_csrf")
            assert token
            missing_csrf = client.post(
                "/create", auth=_auth(client), data={"description": "A valid song"}
            )
            assert missing_csrf.status_code == 403
            accepted = client.post(
                "/create",
                auth=_auth(client),
                data={"csrf_token": token, "description": "A valid song"},
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            assert re.fullmatch(r"/beta/jobs/[0-9a-f-]+", accepted.headers["location"])

            detail = client.get(f"/jobs/{original_job_id}", auth=_auth(client))
            assert f'data-status-url="/beta/jobs/{original_job_id}/status"' in detail.text
            assert f'src="/beta/media/{output_id}"' in detail.text
            assert f'href="/beta/files/{output_id}/download"' in detail.text
            assert_prefixed_browser_attributes(detail.text)

            status_response = client.get(
                f"/jobs/{original_job_id}/status", auth=_auth(client)
            )
            status_body = status_response.json()
            assert status_body["detail_url"] == f"/beta/jobs/{original_job_id}"
            assert status_body["status_url"] == f"/beta/jobs/{original_job_id}/status"
            assert status_body["outputs"][0]["media_url"] == f"/beta/media/{output_id}"
            assert status_body["outputs"][0]["download_url"] == (
                f"/beta/files/{output_id}/download"
            )

            cover_token = client.cookies.get("ace_csrf")
            assert cover_token
            created_cover = client.post(
                "/cover",
                auth=_auth(client),
                data={
                    "csrf_token": cover_token,
                    "youtube_url": "https://www.youtube.com/watch?v=abc123",
                    "target_style": "dreamy synthwave",
                    "rights_confirmation": "true",
                },
                follow_redirects=False,
            )
            assert created_cover.status_code == 303
            assert created_cover.headers["location"].startswith("/beta/jobs/")
            cover_job_id = created_cover.headers["location"].rsplit("/", 1)[-1]
            with factory() as session:
                transition_job(session, cover_job_id, JobStatus.INGESTING)
                finalize_cover_job_duration(session, cover_job_id, 42.0)
                transition_job(session, cover_job_id, JobStatus.STAGING)
                session.commit()
            cover_detail = client.get(f"/jobs/{cover_job_id}", auth=_auth(client))
            assert f'action="/beta/cover/{cover_job_id}/confirm"' in cover_detail.text
            assert f'action="/beta/cover/{cover_job_id}/cancel"' in cover_detail.text
            assert_prefixed_browser_attributes(cover_detail.text)

            static = client.get("/beta/static/app.css", auth=_auth(client))
            assert static.status_code == 200
            assert static.headers["cache-control"] == "public, max-age=3600"
            assert client.get(f"/beta/media/{output_id}").status_code == 401
            assert (
                client.get(f"/beta/media/{output_id}", auth=_auth(client)).content == payload
            )
            download = client.get(
                f"/beta/files/{output_id}/download", auth=_auth(client)
            )
            assert download.status_code == 200
            assert "attachment" in download.headers["content-disposition"]
    finally:
        engine.dispose()


def test_form_validation_escaping_and_cover_rights(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client)
        invalid_original = client.post(
            "/create",
            auth=_auth(client),
            data={"csrf_token": token, "description": "no"},
        )
        assert invalid_original.status_code == 422
        assert "description" in invalid_original.text

        cover_token = _csrf(client, "/cover")
        invalid_cover = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": cover_token,
                "youtube_url": "https://www.youtube.com/playlist?list=unsafe",
                "target_style": "synthwave",
                "rights_confirmation": "true",
            },
        )
        assert invalid_cover.status_code == 422

        accepted = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "description": "<script>alert('owned')</script>",
            },
            follow_redirects=False,
        )
        job_id = accepted.headers["location"].rsplit("/", 1)[-1]
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            assert job.prompt == "<script>alert('owned')</script>"
        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert "&lt;script&gt;alert(&#39;owned&#39;)&lt;/script&gt;" in detail.text
        assert "<script>alert('owned')</script>" not in detail.text
        assert worker.enqueued == [job_id]


def test_cover_confirmation_gates_second_enqueue_and_displays_source_duration(web_app) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client, "/cover")
        created = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )
        assert created.status_code == 303
        job_id = created.headers["location"].rsplit("/", 1)[-1]
        assert worker.enqueued == [job_id]
        worker.enqueued.clear()
        initial_detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert initial_detail.status_code == 200
        assert "data-cover-confirmation-form" not in initial_detail.text

        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            transition_job(session, job.id, JobStatus.STAGING)
            session.commit()

        status_response = client.get(f"/jobs/{job_id}/status", auth=_auth(client))
        assert status_response.status_code == 200
        assert status_response.json()["cover_confirmation_status"] == "awaiting_confirmation"
        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert detail.status_code == 200
        assert "Detected source duration: 42.0 seconds" in detail.text
        assert detail.text.count("data-cover-confirmation-form") == 1
        static_status = client.get("/static/status.js", auth=_auth(client))
        assert "window.location.reload()" in static_status.text
        confirm_token = client.cookies.get("ace_csrf")
        assert confirm_token
        confirmed = client.post(
            f"/cover/{job_id}/confirm",
            auth=_auth(client),
            data={"csrf_token": confirm_token},
            follow_redirects=False,
        )
        assert confirmed.status_code == 303
        assert worker.enqueued == [job_id]

        replay = client.post(
            f"/cover/{job_id}/confirm",
            auth=_auth(client),
            data={"csrf_token": confirm_token},
        )
        assert replay.status_code == 409
        assert worker.enqueued == [job_id]


def test_staged_cover_cancellation_is_authenticated_csrf_protected_and_single_use(
    web_app, settings
) -> None:
    app, factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client, "/cover")
        created = client.post(
            "/cover",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "youtube_url": "https://www.youtube.com/watch?v=abc123",
                "target_style": "dreamy synthwave",
                "rights_confirmation": "true",
            },
            follow_redirects=False,
        )
        job_id = created.headers["location"].rsplit("/", 1)[-1]
        worker.enqueued.clear()
        source = settings.paths.incoming / job_id / "source.mp3"
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_bytes(b"prepared source")
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            transition_job(session, job.id, JobStatus.INGESTING)
            finalize_cover_job_duration(session, job.id, 42.0)
            job.source_byte_size = source.stat().st_size
            job.source_sha256 = hashlib.sha256(source.read_bytes()).hexdigest()
            transition_job(session, job.id, JobStatus.STAGING)
            session.commit()

        assert client.post(f"/cover/{job_id}/cancel").status_code == 401
        assert (
            client.post(
                f"/cover/{job_id}/cancel",
                auth=_auth(client),
                data={"csrf_token": "wrong"},
            ).status_code
            == 403
        )
        cancelled = client.post(
            f"/cover/{job_id}/cancel",
            auth=_auth(client),
            data={"csrf_token": token},
            follow_redirects=False,
        )
        assert cancelled.status_code == 303
        assert worker.enqueued == []
        assert not source.exists()
        with factory() as session:
            job = get_job(session, job_id)
            assert job is not None
            assert job.status is JobStatus.FAILED
            assert job.error_code == "cover_staging_cancelled"
            assert job.user_facing_error == ("Cover preparation was cancelled before confirmation.")

        replay = client.post(
            f"/cover/{job_id}/cancel",
            auth=_auth(client),
            data={"csrf_token": token},
        )
        assert replay.status_code == 409
        assert worker.enqueued == []


@pytest.mark.parametrize(
    ("description", "duration_mode", "duration_seconds", "expected_status"),
    [
        ("a 30-second song", "custom", "30", 303),
        ("a 45-second song", "custom", "30", 422),
        ("a 30-second song", "auto", "", 422),
        ("make it longer", "custom", "30", 422),
        ("plain piano arrangement", "custom", "30", 303),
    ],
)
def test_original_form_duration_language_validation(
    web_app,
    description: str,
    duration_mode: str,
    duration_seconds: str,
    expected_status: int,
) -> None:
    app, _factory, worker = web_app
    with TestClient(app) as client:
        token = _csrf(client)
        response = client.post(
            "/create",
            auth=_auth(client),
            data={
                "csrf_token": token,
                "description": description,
                "duration_mode": duration_mode,
                "duration_seconds": duration_seconds,
            },
            follow_redirects=False,
        )

        assert response.status_code == expected_status
        if expected_status == 303:
            assert len(worker.enqueued) == 1


def test_quote_runtime_identity_config_is_bounded_and_required(
    settings: ServiceSettings,
) -> None:
    with pytest.raises(ValueError, match="runpod_worker_runtime_identity is required"):
        ServiceSettings(
            data_root=settings.data_root,
            service_password="test-password",
            home_ingest_token="test-home-token",
            runpod_api_key="test-runpod-key",
            runpod_endpoint_id="test-endpoint",
            eligible_gpu_ids=["L40S"],
        )
    for invalid in ("worker-schema-v2", "sha256:abc", "sha256:" + "g" * 64):
        with pytest.raises(ValueError, match="exact sha256"):
            ServiceSettings(
                data_root=settings.data_root,
                service_password="test-password",
                home_ingest_token="test-home-token",
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-endpoint",
                eligible_gpu_ids=["L40S"],
                runpod_worker_runtime_identity=invalid,
            )


def test_acceptance_quote_uses_only_configured_exact_runtime_identity(
    settings: ServiceSettings,
) -> None:
    runtime_settings = ServiceSettings(
        data_root=settings.data_root,
        service_password="test-password",
        home_ingest_token="test-home-token",
        runpod_api_key="test-runpod-key",
        runpod_endpoint_id="test-endpoint",
        eligible_gpu_ids=["L40S"],
        runpod_worker_runtime_identity=RUNTIME_A,
    )
    engine = create_database_engine(runtime_settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    now = datetime.now(UTC)
    with factory() as session:
        upsert_gpu_rate(
            session,
            gpu_id="L40S",
            rate_micro_usd_per_hour=700_000,
            hourly_rate_usd="0.7000004",
            source="runpod_flex_api",
            calibration_version=1,
            captured_at=now,
        )
        upsert_runtime_calibration(
            session,
            version=1,
            task_mode="original",
            profile_id="fast-beta-v1",
            model_identity=runtime_settings.acestep_model,
            runtime_identity=RUNTIME_A,
            gpu_class="L40S",
            duration_mode="custom",
            duration_band_min_seconds=30.0,
            duration_band_max_seconds=30.0,
            output_count=1,
            execution_low_ms=20_531,
            execution_high_ms=20_531,
            evidence_source="accepted-local-measurement-v1",
            conservative_margin="0",
            captured_at=now,
        )
        session.commit()

    worker = FakeWorker()
    app = create_app(
        runtime_settings,
        session_factory=factory,
        runpod_client=FakeRunpod(),
        home_ingest_client=FakeHome(),
        worker=worker,
    )
    try:
        with TestClient(app) as client:
            token = _csrf(client)
            accepted = client.post(
                "/create",
                auth=_auth(client),
                data={
                    "csrf_token": token,
                    "description": "plain piano arrangement",
                    "duration_mode": "custom",
                    "duration_seconds": "30",
                    # Browser input cannot override the server-owned identity.
                    "runpod_worker_runtime_identity": RUNTIME_B,
                },
                follow_redirects=False,
            )
            assert accepted.status_code == 303
            first_job_id = accepted.headers["location"].rsplit("/", 1)[-1]

            with factory() as session:
                first_job = get_job(session, first_job_id)
                assert first_job is not None
                first_quote = get_submission_quote(session, first_job_id)
                assert first_quote is not None and first_quote.unavailable_reason_code is None
                assert first_quote.calibration_version == 1
                quote_id = first_quote.id
                captured_at = first_quote.captured_at
                repeated = capture_submission_quote(app, session, first_job)
                assert repeated is not None and repeated.id == quote_id
                assert repeated.captured_at == captured_at

            changed_settings = ServiceSettings(
                data_root=settings.data_root,
                service_password="test-password",
                home_ingest_token="test-home-token",
                runpod_api_key="test-runpod-key",
                runpod_endpoint_id="test-endpoint",
                eligible_gpu_ids=["L40S"],
                runpod_worker_runtime_identity=RUNTIME_B,
            )
            app.state.settings = changed_settings
            with factory() as session:
                first_job = get_job(session, first_job_id)
                assert first_job is not None
                with pytest.raises(EvidenceConflictError, match="conflicting submission quote"):
                    capture_submission_quote(app, session, first_job)
                session.rollback()
                unchanged = get_submission_quote(session, first_job_id)
                assert unchanged is not None
                assert (unchanged.id, unchanged.captured_at, unchanged.calibration_version) == (
                    quote_id,
                    captured_at,
                    1,
                )

            token = _csrf(client)
            missing = client.post(
                "/create",
                auth=_auth(client),
                data={
                    "csrf_token": token,
                    "description": "plain piano arrangement",
                    "duration_mode": "custom",
                    "duration_seconds": "30",
                    "runpod_worker_runtime_identity": RUNTIME_A,
                },
                follow_redirects=False,
            )
            assert missing.status_code == 303
            second_job_id = missing.headers["location"].rsplit("/", 1)[-1]
            with factory() as session:
                second_quote = get_submission_quote(session, second_job_id)
                assert second_quote is not None
                assert second_quote.unavailable_reason_code == "calibration_missing"
                assert session.query(SubmissionQuote).count() == 2
    finally:
        engine.dispose()


def test_status_polling_and_timing_metadata(web_app) -> None:
    app, factory, _ = web_app
    with factory() as session:
        job = create_job(session, job_type=JobType.ORIGINAL, variation_count=2)
        session.commit()
        job_id = job.id
    with TestClient(app) as client:
        response = client.get(f"/jobs/{job_id}/status", auth=_auth(client))
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "queued"
        assert body["status_label"] == "Queued"
        assert body["variation_count"] == 2
        assert body["elapsed_seconds"] >= 0
        assert body["outputs"] == []
        detail = client.get(f"/jobs/{job_id}", auth=_auth(client))
        assert "data-status-url" in detail.text
        assert "/static/status.js" in detail.text


def test_readiness_reports_components_and_preserves_original_availability(settings) -> None:
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=FakeRunpod(available=False),
        home_ingest_client=FakeHome(available=False),
        worker=FakeWorker(),
    )
    try:
        with TestClient(app) as client:
            dashboard = client.get("/", auth=_auth(client))
            assert dashboard.status_code == 200
            assert "Runpod is unavailable" in dashboard.text
            assert "Home ingest is unavailable" in dashboard.text
            ready = client.get("/readyz", auth=_auth(client))
            assert ready.status_code == 503
            body = ready.json()
            assert body["components"]["runpod_api"]["ok"] is False
            assert body["components"]["home_ingest"]["ok"] is False
    finally:
        engine.dispose()


def test_readiness_bounds_stalled_probes_without_gating_original_submission(
    settings, monkeypatch
) -> None:
    timeout = 0.05
    monkeypatch.setattr(web_routes, "_READINESS_PROBE_TIMEOUT_SECONDS", timeout)
    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    runpod = StalledHealth()
    home = StalledHealth()
    app = create_app(
        settings,
        session_factory=factory,
        runpod_client=runpod,
        home_ingest_client=home,
        worker=FakeWorker(),
    )
    try:
        with TestClient(app) as client:
            for path in ("/", "/cover", "/readyz"):
                started = time.monotonic()
                response = client.get(path, auth=_auth(client))
                elapsed = time.monotonic() - started
                assert elapsed < 1
                assert response.status_code in {200, 503}

            ready = client.get("/readyz", auth=_auth(client))
            assert ready.status_code == 503
            body = ready.json()
            assert body["components"]["runpod_api"] == {
                "ok": False,
                "message": "unreachable",
            }
            assert body["components"]["home_ingest"] == {
                "ok": False,
                "message": "unreachable",
            }
            assert runpod.cancelled >= 1
            assert home.cancelled >= 1

            token = _csrf(client)
            accepted = client.post(
                "/create",
                auth=_auth(client),
                data={"csrf_token": token, "description": "A valid song"},
                follow_redirects=False,
            )
            assert accepted.status_code == 303
    finally:
        engine.dispose()


def test_authenticated_playback_download_and_symlink_escape(web_app, settings) -> None:
    app, factory, _ = web_app
    payload = b"valid generated mp3"
    job_id = "123e4567-e89b-12d3-a456-426614174000"
    relative_path = f"{job_id}/variation-01.mp3"
    output_path = settings.paths.outputs / relative_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(payload)
    with factory() as session:
        create_job(session, job_type=JobType.ORIGINAL, job_id=job_id)
        output = create_output(
            session,
            job_id=job_id,
            variation_index=1,
            result_index=0,
            relative_path=relative_path,
            mime_type="audio/mpeg",
            byte_size=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )
        session.commit()
        output_id = output.id
    with TestClient(app) as client:
        assert client.get(f"/media/{output_id}").status_code == 401
        media = client.get(f"/media/{output_id}", auth=_auth(client))
        assert media.status_code == 200
        assert media.content == payload
        assert media.headers["content-type"].startswith("audio/mpeg")
        download = client.get(f"/files/{output_id}/download", auth=_auth(client))
        assert download.status_code == 200
        assert "attachment" in download.headers["content-disposition"]

        outside = Path(settings.data_root).parent / "not-an-output.mp3"
        outside.write_bytes(payload)
        output_path.unlink()
        output_path.symlink_to(outside)
        assert client.get(f"/media/{output_id}", auth=_auth(client)).status_code == 404
        outside.unlink()


def test_public_transfer_app_does_not_mount_private_ui(web_app, settings) -> None:
    _, factory, _ = web_app
    transfer_app = create_transfer_app(settings, session_factory=factory)
    with TestClient(transfer_app) as client:
        assert client.get("/").status_code == 404
        assert client.get("/create").status_code == 404
        assert client.get("/jobs").status_code == 404
        assert client.get("/media/1").status_code == 404
        assert client.get("/healthz").status_code == 404
