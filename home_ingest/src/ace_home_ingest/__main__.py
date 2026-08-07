"""Run the localhost-only home ingest API."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import HomeIngestSettings


def main() -> None:
    settings = HomeIngestSettings()
    settings.ensure_data_layout()
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
