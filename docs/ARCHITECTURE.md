# Architecture

## Checkpoint 1 boundary

The initial controller foundation is a synchronous Python package under
`src/ace_service`. It owns typed configuration, a private data-root layout,
and durable SQLite records for jobs, generated outputs, and short-lived
transfer capabilities.

`config.py` resolves every persistent path below `ACE_SERVICE_DATA_ROOT`,
rejects wildcard application binds and unconfigured credential placeholders,
and requires the public transfer URL to use HTTPS. Credential placeholders
cannot be enabled through deployment settings.
`db.py` creates a SQLAlchemy 2 synchronous engine with SQLite WAL, foreign-key
enforcement, and a busy timeout. `models.py` stores UTC-aware timestamps and
the state needed for future Runpod submission recovery. `repository.py` keeps
token plaintext out of the database; callers receive a capability token only
when it is issued.

This checkpoint has no network clients, FastAPI routes, media-processing
commands, Runpod code, home-ingest code, or inference dependencies. Later
checkpoints may consume these records through the repository while preserving
the boundary that the controller does not run `yt-dlp`, `ffmpeg`, or
`ffprobe`.
