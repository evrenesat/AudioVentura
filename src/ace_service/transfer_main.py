"""Run the isolated public transfer app with Uvicorn."""

from __future__ import annotations

import uvicorn

from ace_service.config import ServiceSettings
from ace_service.transfers import create_transfer_app


def main() -> None:
    settings = ServiceSettings()
    uvicorn.run(
        create_transfer_app(settings),
        host=settings.transfer_host,
        port=settings.transfer_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
