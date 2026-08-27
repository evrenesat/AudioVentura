# MIDI mock backend

This directory owns the private sequential MIDI integration-test service. It
is deliberately independent from the controller, transfer service, Home
Ingest, and GPU worker. The service may read the immutable corpus and
soundfont, but it must not execute `yt-dlp`, `ffprobe`, or `ffmpeg`, and it
must never log prompts, lyrics, source URLs, transfer tokens, or full corpus
paths.

Keep corpus validation, cursor transactions, rendering, signed-result upload,
and HTTP contracts in their separate modules. Run checks from this directory:

```text
uv sync --frozen
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The production and beta instances use separate state roots and databases.
Only the archive and renderer installation are shared, read-only.
