from __future__ import annotations

from types import SimpleNamespace

import ace_service.transfer_main as transfer_main


def test_transfer_main_disables_uvicorn_access_log(monkeypatch) -> None:
    settings = SimpleNamespace(transfer_host="127.0.0.1", transfer_port=18001)
    transfer_app = object()
    captured: dict[str, object] = {}

    monkeypatch.setattr(transfer_main, "ServiceSettings", lambda: settings)
    monkeypatch.setattr(transfer_main, "create_transfer_app", lambda value: transfer_app)

    def capture_run(application: object, **kwargs: object) -> None:
        captured["application"] = application
        captured.update(kwargs)

    monkeypatch.setattr(transfer_main.uvicorn, "run", capture_run)

    transfer_main.main()

    assert captured == {
        "application": transfer_app,
        "host": "127.0.0.1",
        "port": 18001,
        "access_log": False,
    }
