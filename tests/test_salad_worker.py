from __future__ import annotations

from typing import Any

from fastapi.testclient import TestClient

from deploy.salad.worker_api import create_app


def test_worker_initializes_before_readiness_and_wraps_existing_event() -> None:
    runtime = object()
    configured: list[object] = []
    events: list[dict[str, Any]] = []

    def initialize() -> Any:
        return runtime

    def configure(value: Any) -> None:
        configured.append(value)

    def handle(event: dict[str, Any]) -> dict[str, Any]:
        events.append(event)
        return {"status": "ok"}

    app = create_app(
        runtime_initializer=initialize,
        runtime_configurer=configure,
        request_handler=handle,
    )
    with TestClient(app) as client:
        assert client.get("/live").json() == {"status": "alive"}
        assert client.get("/ready").json() == {"status": "ready"}
        response = client.post("/process", json={"schema_version": 2, "job_id": "job"})
        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    assert configured == [runtime]
    assert events == [{"input": {"schema_version": 2, "job_id": "job"}}]
    assert app.state.runtime_ready is False


def test_worker_refuses_processing_before_runtime_is_ready() -> None:
    app = create_app()
    app.state.runtime_ready = False
    client = TestClient(app)
    try:
        response = client.post("/process", json={"job_id": "not-ready"})
    finally:
        client.close()

    assert response.status_code == 503
    assert response.json() == {"detail": "runtime is not ready"}
