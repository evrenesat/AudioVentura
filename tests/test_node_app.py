from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from ace_node.app import create_app
from ace_node.config import NodeSettings
from runpod_worker.tests.test_schemas import _v2_payload

APP = "11111111-1111-4111-8111-111111111111"
NONCE = "22222222-2222-4222-8222-222222222222"


class _Runtime:
    def execute(self, payload: dict[str, Any], _job_id: str) -> dict[str, Any]:
        return {
            "schema_version": 2,
            "job_id": payload["job_id"],
            "submission_nonce": payload["submission_nonce"],
            "variation_index": 1,
            "status": "uploaded",
            "output": {"bytes": 4, "sha256": "a" * 64},
            "input": {"prompt": "do not persist"},
            "capability_url": "https://player.evren.io/transfer/v1/secret",
        }


def _body(nonce: str = NONCE) -> dict[str, Any]:
    value = _v2_payload("original")
    value.update({"job_id": APP, "submission_nonce": nonce})
    value["result_upload"]["url"] = "https://player.evren.io/transfer/v1/output/capability"
    return {
        "schema_version": 2,
        "application_job_id": APP,
        "variation_index": 1,
        "submission_nonce": nonce,
        "input": value,
        "source": None,
        "result_upload": value["result_upload"],
    }


def _settings(tmp_path: Path, **kwargs: Any) -> NodeSettings:
    return NodeSettings(
        data_root=tmp_path,
        token="node-secret",
        supervisor_token="supervisor-secret",
        runtime_receipt="sha256:" + "a" * 64,
        **kwargs,
    )


def test_node_api_authentication_idempotency_and_result(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path), runtime_factory=lambda: _Runtime())) as client:
        assert client.get("/healthz").status_code == 401
        headers = {"Authorization": "Bearer node-secret"}
        health = client.get("/healthz", headers=headers).json()
        assert health["status"] == "ready"
        assert set(health) == {
            "status",
            "phase",
            "error_code",
            "queue_depth",
            "running",
            "running_elapsed_seconds",
            "max_concurrency",
            "accepting",
            "accelerator",
            "model",
            "lm_model",
        }
        assert health["phase"] == "ready"
        assert health["accepting"] is True
        response = client.post("/v1/jobs", headers=headers, json=_body())
        assert response.status_code == 202
        duplicate = client.post("/v1/jobs", headers=headers, json=_body())
        assert duplicate.status_code == 202
        assert duplicate.json()["job_id"] == response.json()["job_id"]
        conflict = _body()
        conflict["application_job_id"] = "33333333-3333-4333-8333-333333333333"
        conflict_input = conflict["input"]
        assert isinstance(conflict_input, dict)
        conflict_input["job_id"] = conflict["application_job_id"]
        assert client.post("/v1/jobs", headers=headers, json=conflict).status_code == 409
        job_id = response.json()["job_id"]
        for _ in range(100):
            status = client.get(f"/v1/jobs/{job_id}", headers=headers).json()
            if status["status"] == "succeeded":
                break
            time.sleep(0.01)
        assert status["status"] == "succeeded"
        result = client.get(f"/v1/jobs/{job_id}/result", headers=headers)
        assert result.status_code == 200
        assert "transfer/v1" not in result.text


def test_supervisor_drain_is_separate_and_requires_an_empty_body(tmp_path: Path) -> None:
    with TestClient(create_app(_settings(tmp_path), runtime_factory=lambda: _Runtime())) as client:
        controller_headers = {"Authorization": "Bearer node-secret"}
        supervisor_headers = {"Authorization": "Bearer supervisor-secret"}
        assert client.post("/v1/supervisor/drain", headers=controller_headers).status_code == 401
        assert (
            client.post(
                "/v1/supervisor/drain",
                headers=supervisor_headers,
                content=b"not-empty",
            ).status_code
            == 400
        )
        response = client.post("/v1/supervisor/drain", headers=supervisor_headers)
        assert response.status_code == 200
        assert response.json() == {"accepting": False, "running": False, "queue_depth": 0}
        assert client.post("/v1/jobs", headers=controller_headers, json=_body()).status_code == 503


def test_node_api_stays_alive_when_runtime_initialization_fails(tmp_path: Path) -> None:
    def broken() -> Any:
        raise RuntimeError("private failure")

    with TestClient(create_app(_settings(tmp_path), runtime_factory=broken)) as client:
        headers = {"Authorization": "Bearer node-secret"}
        health = client.get("/healthz", headers=headers)
        assert health.status_code == 200
        assert health.json()["status"] == "failed"
        assert client.post("/v1/jobs", headers=headers, json=_body()).status_code == 503
