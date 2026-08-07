# Architecture

## Checkpoint 5 original-song workflow

Original-song requests are validated at the controller boundary and persisted
as a normalized, metadata-only Runpod request before the job is enqueued. The
description is trimmed, creative lyrics are preserved, optional generation
fields are omitted when absent, and instrumental requests cannot carry
non-empty lyrics. A supplied seed advances by one per serialized variation;
each variation remains an independent Runpod job.

Before each original variation crosses the cloud submission boundary, the
controller issues a new short-lived output-upload capability bound to
`<job-id>/variation-XX.<format>`. The worker payload contains only bounded
generation metadata, the current variation seed, the nonce, and that capability
URL; it contains no audio bytes and no `variation_count` field. Output records
retain deterministic paths and checksums, while the variation attempt stores
the bounded worker result and any Runpod queue-delay/execution timing returned
by the API. The existing single controller queue submits variations serially,
so a later failure leaves earlier durable outputs available.

## Checkpoint 4 controller orchestration

The controller owns one POSIX advisory lock per configured data root and one
`asyncio.Queue[str]` serviced by one coroutine. Enqueue deduplication is
in-process; the lock prevents a second controller process from polling or
submitting the same SQLite-backed jobs. Runpod submission is serialized across
all jobs and variations.

Parent jobs use the lifecycle transitions defined by the handoff plan. An
original job's `variation_attempts` rows hold the individual variation status,
submission nonce, Runpod ID, result metadata, and terminal error. This keeps a
multi-variation parent active while each variation is submitted and polled in
order. A nonce is committed before `/run`; a returned Runpod ID is committed
immediately after acceptance. Nonce-only attempts are failed as uncertain on
restart and are never submitted again.

Startup recovery runs while the singleton lock is held and before the worker
accepts new enqueues. Queued jobs are enqueued, interrupted ingestion is
advanced only when its canonical source is fully verified, and persisted
Runpod IDs are polled without resubmission. Missing IDs and uncertain
submissions become stable terminal errors. Completed Runpod status is accepted
only after a deterministic output file and matching durable output record pass
size, path, and SHA-256 validation; a valid consumed output can recover a job
when Runpod status retention has expired.

## Checkpoint 3 boundary

The controller's Runpod adapter is an asynchronous metadata-only client for
the queue endpoint at /v2/<endpoint-id>. It uses separate connect, read,
write, and pool timeouts, sends the API key only in an authorization header,
validates response IDs and states, and maps Runpod's queue/running/terminal
states to cloud_queued, generating, completed, and failed. The adapter never
carries audio in a Runpod JSON body.

Submission persistence is deliberately ordered: a fresh nonce and the
pre-submit cloud_queued record are committed before /run; the returned Runpod
job ID is committed immediately after acceptance. A nonce-only record on
restart is marked uncertain_cloud_submission and is never submitted again.
Records with a persisted Runpod ID resume polling instead.

The transfer application is a separate FastAPI app with documentation and
OpenAPI disabled. It binds to 127.0.0.1:8001 and exposes only:

    GET /transfer/v1/source/{token}
    PUT /transfer/v1/output/{token}

Capability tokens are random 256-bit values; only their SHA-256 hashes are
stored. Capabilities bind a job, direction, relative storage path, extension,
byte limit, and UTC expiry. Source GETs are repeatable during the TTL. Output
PUTs stream to a deterministic .part file, hash while receiving, fsync before
atomic rename, and transactionally create the durable output record while
consuming the capability. Identical completed retries are idempotent and
conflicting retries cannot replace the accepted output.

The public proxy may forward only /transfer/v1/source/* and
/transfer/v1/output/* to 127.0.0.1:8001. It must reject every other path at
the proxy before the private controller/UI is reached. The proxy uses
publicly trusted HTTPS; this checkpoint does not claim a live Runpod transfer
test.

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
