"""Run the private MIDI mock service from its environment."""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn

from .app import create_app
from .config import MockSettings
from .corpus import build_manifest, load_verified_corpus, write_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Run or validate the private MIDI mock backend")
    parser.add_argument("command", nargs="?", choices=("serve", "manifest"), default="serve")
    parser.add_argument("--archive")
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.command == "manifest":
        if not args.archive or not args.output:
            parser.error("manifest requires --archive and --output")
        manifest = build_manifest(Path(args.archive))
        write_manifest(manifest, Path(args.output))
        print(
            f"archive_sha256={manifest.archive_sha256} members={manifest.member_count} "
            f"manifest_sha256={manifest.manifest_sha256}"
        )
        return
    settings = MockSettings.from_env()
    settings.ensure_state_layout()
    manifest = load_verified_corpus(
        settings.corpus_archive,
        settings.manifest_path,  # type: ignore[arg-type]
        expected_manifest_sha256=settings.expected_manifest_sha256,
    )
    app = create_app(settings, manifest)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info", access_log=False)


if __name__ == "__main__":
    main()
