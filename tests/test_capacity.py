from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import select

from ace_service.capacity.base import (
    CapacityError,
    CapacityErrorKind,
    CapacityPhase,
    CapacitySnapshot,
    canonical_fingerprint,
)
from ace_service.capacity.controller import CapacityController
from ace_service.capacity.fingerprints import (
    build_runpod_fingerprint_payload,
    build_salad_fingerprint_payload,
)
from ace_service.capacity.registry import CapacityRegistry
from ace_service.models import CapacityLease, JobStatus, JobType, NotificationEvent
from ace_service.providers.base import BackendId, ProviderName
from ace_service.repository import (
    acquire_capacity_action_lease,
    ensure_capacity_lease,
    get_keep_warm_seconds,
    set_keep_warm_seconds,
    update_capacity_lease_if_owner,
)


class FakeManager:
    key = "runpod/test-endpoint"
    provider = ProviderName.RUNPOD
    backend_ids = frozenset({BackendId("runpod/ace-step-v15-xl-turbo")})

    def __init__(self) -> None:
        self.floor = 0
        self.instances = 0
        self.ready = False
        self.retain_calls = 0
        self.release_calls = 0

    async def inspect(self) -> CapacitySnapshot:
        return CapacitySnapshot(
            self.key,
            self.provider,
            CapacityPhase.READY if self.ready else CapacityPhase.COLD,
            self.floor,
            1,
            self.instances,
            1 if self.ready else 0,
            0,
            None,
            datetime.now(UTC),
        )

    async def retain_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        self.retain_calls += 1
        self.floor = self.instances = 1
        self.ready = True
        return await self.inspect()

    async def release_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        self.release_calls += 1
        self.floor = self.instances = 0
        self.ready = False
        return await self.inspect()


class LostReleaseManager(FakeManager):
    async def release_one(self, before: CapacitySnapshot) -> CapacitySnapshot:
        self.release_calls += 1
        self.floor = 0
        self.instances = 1
        self.ready = False
        raise CapacityError(
            CapacityErrorKind.TRANSIENT,
            "release",
            "provider response was lost",
        )

    async def inspect(self) -> CapacitySnapshot:
        phase = (
            CapacityPhase.RELEASING
            if self.floor == 0 and self.instances
            else (CapacityPhase.READY if self.ready else CapacityPhase.COLD)
        )
        return CapacitySnapshot(
            self.key,
            self.provider,
            phase,
            self.floor,
            1,
            self.instances,
            1 if self.ready else 0,
            0,
            None,
            datetime.now(UTC),
        )


class FakeSaladManager(FakeManager):
    key = "salad/test-group"
    provider = ProviderName.SALAD
    backend_ids = frozenset({BackendId("salad/ace-step-v15-xl-turbo")})


def test_setting_allow_list_and_stale_fencing(session) -> None:
    assert get_keep_warm_seconds(session) == 900
    for value in (0, 60, 120, 180, 300, 600, 900, 1800, 2700, 3600, 7200, 10800, 14400):
        set_keep_warm_seconds(session, value)
        session.commit()
        assert get_keep_warm_seconds(session) == value
    ensure_capacity_lease(session, "runpod/test-endpoint", ProviderName.RUNPOD)
    session.commit()
    first = acquire_capacity_action_lease(session, "runpod/test-endpoint", "one", ttl_seconds=1)
    session.commit()
    assert first == 1
    second = acquire_capacity_action_lease(
        session,
        "runpod/test-endpoint",
        "two",
        now=datetime.now(UTC) + timedelta(seconds=2),
        ttl_seconds=30,
    )
    session.commit()
    assert second == 2
    assert (
        update_capacity_lease_if_owner(
            session,
            "runpod/test-endpoint",
            owner="one",
            fencing_token=first,
            state="retained",
        )
        is None
    )


def test_registry_excludes_unmanaged_provider() -> None:
    manager = FakeManager()
    registry = CapacityRegistry([manager])
    assert registry.for_backend("fal/music") is None
    assert registry.for_backend(next(iter(manager.backend_ids))) is manager


def test_fingerprint_fixtures_are_canonical_and_pinned() -> None:
    fixture_path = Path(__file__).parents[1] / "src/ace_service/capacity/fingerprint_fixtures.json"
    fixtures = json.loads(fixture_path.read_text(encoding="utf-8"))
    for item in fixtures.values():
        encoded = json.dumps(
            {"schema": item["schema"], **item["payload"]},
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")
        assert hashlib.sha256(encoded).hexdigest() == item["sha256"]


def test_fingerprints_require_the_complete_secret_free_identity() -> None:
    with pytest.raises(CapacityError, match="missing"):
        build_runpod_fingerprint_payload({"id": "endpoint"})
    with pytest.raises(CapacityError, match="environment key set"):
        build_runpod_fingerprint_payload(
            {
                "id": "endpoint",
                "name": "name",
                "workersMax": 1,
                "gpuCount": 1,
                "minCudaVersion": "12.8",
                "executionTimeoutMs": 1,
                "idleTimeout": 30,
                "flashboot": True,
                "networkVolumeId": "volume",
                "networkVolumeIds": ["volume"],
                "gpuTypeIds": ["gpu"],
                "scalerType": "QUEUE_DELAY",
                "scalerValue": 2,
                "template": {
                    "id": "template",
                    "name": "name",
                    "category": "NVIDIA",
                    "containerDiskInGb": 1,
                    "isServerless": True,
                    "ports": ["1/http"],
                    "readme": "",
                    "volumeMountPath": "/workspace",
                    "imageName": "image",
                    "env": {"ACE_TRANSFER_ALLOWED_HOST": "player.evren.io"},
                },
            }
        )


def test_salad_fingerprint_builder_uses_nested_allow_list() -> None:
    fixture_path = Path(__file__).parents[1] / "src/ace_service/capacity/fingerprint_fixtures.json"
    fixture = json.loads(fixture_path.read_text(encoding="utf-8"))["salad"]["payload"]
    payload = build_salad_fingerprint_payload(
        fixture["queue"],
        fixture["group"],
        organization=fixture["organization"],
        project=fixture["project"],
    )
    assert (
        canonical_fingerprint(payload, schema="salad-capacity-v1")
        == json.loads(fixture_path.read_text(encoding="utf-8"))["salad"]["sha256"]
    )


def test_lost_release_response_becomes_overdue_after_grace(settings) -> None:
    from ace_service.db import create_database_engine, create_session_factory, initialize_database
    from ace_service.models import Job
    from ace_service.repository import create_job, set_keep_warm_seconds

    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    manager = LostReleaseManager()
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    controller = CapacityController(
        settings,
        factory,
        CapacityRegistry([manager]),
        clock=lambda: current[0],
    )
    with factory() as database_session:
        create_job(
            database_session,
            job_type=JobType.ORIGINAL,
            inference_provider="runpod",
            inference_backend="runpod/ace-step-v15-xl-turbo",
        )
        database_session.commit()
    asyncio.run(controller.reconcile_once())
    with factory() as database_session:
        job = database_session.query(Job).one()
        job.status = JobStatus.COMPLETED
        job.completed_at = current[0]
        set_keep_warm_seconds(database_session, 0)
        database_session.commit()
    first = asyncio.run(controller.reconcile_once())
    assert first[0].state == "releasing"
    with factory() as database_session:
        lease = ensure_capacity_lease(database_session, manager.key, manager.provider)
        assert lease.release_requested_at == current[0]
        assert lease.state == "releasing"
    current[0] += timedelta(seconds=301)
    second = asyncio.run(controller.reconcile_once())
    assert second[0].state == "release_overdue"
    assert manager.release_calls == 1
    engine.dispose()


def test_generation_started_event_requires_an_exact_capacity_manager(session) -> None:
    from ace_service.repository import create_job
    from ace_service.worker import ControllerWorker

    runpod = FakeManager()
    salad = FakeSaladManager()
    worker = object.__new__(ControllerWorker)
    worker.capacity_registry = CapacityRegistry([runpod, salad])
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    jobs = (
        ("runpod/ace-step-v15-xl-turbo", "runpod", True),
        ("salad/ace-step-v15-xl-turbo", "salad", True),
        ("runpod/unmanaged", "runpod", False),
        ("salad/unmanaged", "salad", False),
        ("fal/music", "fal", False),
    )
    for index, (backend, provider, _managed) in enumerate(jobs):
        job = create_job(
            session,
            job_type=JobType.ORIGINAL,
            job_id=f"job-{index}",
            prompt="test" if provider == "fal" else None,
            inference_provider=provider,
            inference_backend=(
                backend
                if _managed
                else (
                    "runpod/ace-step-v15-xl-turbo"
                    if provider == "runpod"
                    else "salad/ace-step-v15-xl-turbo"
                    if provider == "salad"
                    else "fal/music"
                )
            ),
        )
        if not _managed:
            job.inference_backend = backend
            session.flush()
        worker._insert_managed_generation_started(session, job, timestamp)
    event_keys = set(session.scalars(select(NotificationEvent.event_key)))
    assert event_keys == {"generation-started:job-0", "generation-started:job-1"}


def test_controller_retains_and_releases_once(settings) -> None:
    from ace_service.db import create_database_engine, create_session_factory, initialize_database

    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    manager = FakeManager()
    controller = CapacityController(settings, factory, CapacityRegistry([manager]))
    with factory() as database_session:
        from ace_service.repository import create_job

        create_job(
            database_session,
            job_type=JobType.ORIGINAL,
            inference_provider="runpod",
            inference_backend="runpod/ace-step-v15-xl-turbo",
        )
        database_session.commit()
    asyncio.run(controller.reconcile_once())
    assert manager.retain_calls == 1
    with factory() as database_session:
        from ace_service.models import Job
        from ace_service.repository import set_keep_warm_seconds

        job = database_session.query(Job).one()
        job.status = JobStatus.COMPLETED
        set_keep_warm_seconds(database_session, 0)
        database_session.commit()
    asyncio.run(controller.reconcile_once())
    asyncio.run(controller.reconcile_once())
    assert manager.release_calls == 1
    engine.dispose()


def test_warm_session_begins_only_after_provider_reports_ready(settings) -> None:
    from ace_service.db import create_database_engine, create_session_factory, initialize_database

    engine = create_database_engine(settings)
    initialize_database(engine)
    factory = create_session_factory(engine)
    manager = FakeManager()
    manager.floor = manager.instances = 1
    current = [datetime(2026, 1, 1, tzinfo=UTC)]
    controller = CapacityController(
        settings,
        factory,
        CapacityRegistry([manager]),
        clock=lambda: current[0],
    )

    asyncio.run(controller.ensure_retained(next(iter(manager.backend_ids))))
    with factory() as database_session:
        lease = database_session.get(CapacityLease, manager.key)
        assert lease is not None
        session_id = lease.session_id
        assert session_id is not None
        assert lease.state == "warming"
        assert lease.warmed_at is None
        assert lease.next_reminder_at is None

    current[0] += timedelta(minutes=2)
    manager.ready = True
    asyncio.run(controller.reconcile_once())
    with factory() as database_session:
        lease = database_session.get(CapacityLease, manager.key)
        assert lease is not None
        assert lease.session_id == session_id
        assert lease.state == "idle"
        assert lease.warmed_at == current[0]
        assert lease.next_reminder_at == current[0] + timedelta(minutes=5)
    engine.dispose()
