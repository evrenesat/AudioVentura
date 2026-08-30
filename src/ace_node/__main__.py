"""Run the ACE Node HTTP service."""

from __future__ import annotations

import uvicorn

from .app import create_app
from .config import NodeSettings


def main() -> None:
    settings = NodeSettings()
    uvicorn.run(
        create_app(settings),
        host=settings.listen_host,
        port=settings.listen_port,
        access_log=False,
    )


if __name__ == "__main__":
    main()
