"""Run the private controller UI with Uvicorn."""

from __future__ import annotations

import uvicorn

from ace_service.app import create_app
from ace_service.config import ServiceSettings


def main() -> None:
    settings = ServiceSettings()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
