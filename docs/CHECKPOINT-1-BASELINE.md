# Checkpoint 1 baseline and wiring audit

Recorded on 2026-08-08 in UTC before any Checkpoint 2 implementation. This
document records what was observable from the checked-in application,
deployment checkout, and unauthenticated network probes. It does not claim
live Runpod or authenticated service acceptance where the required operator
credentials were unavailable.

## Revisions and deployment inventory

| Item | Baseline evidence |
|---|---|
| Application checkout | `/root/code/worktrees/aflow-ace-step-quality-cost-observability-aflow-plan-20260808-110317`, clean before edits |
| Application HEAD | `3386e982d61833a23b99a1ecd68917cb46f5e30a` |
| Reference application checkout | `/root/code/audioventura`, `main`, same HEAD, clean |
| Deployment checkout | `/root/code/evreniops-audioventura-deploy`, `feat/audioventura-deployment`, clean |
| Deployment HEAD | `554a28db7b27504d6f862579833ecd4e5fffd9d9` |
| Public hostname | `player.evren.io` |
| Runpod endpoint | `p1t6aef0dlpz5e` from the checked-in Ansible playbook |
| Runpod template ID | Not present in the deployment checkout; live API credential was not available |
| Worker image tag/digest | Not recorded in checked-in deployment state; live digest unavailable |
| ACE-Step release | `v0.1.8`, pinned source commit `dce621408bee8c31b4fcf4811682eb9359e1bc94` |
| DiT / planner models | `acestep-v15-xl-turbo` / `acestep-5Hz-lm-1.7B` |
| GPU policy | Target is one RTX 4090-class 24 GB GPU; the deployment README requires an EU-RO allow-list with live capacity, but the live allow-list and actual GPU were not available |
| Worker scaling | Queue endpoint, `workersMin=0`, `workersMax=1`, one GPU, 30-second idle timeout, 1200-second execution timeout |
| Model volume | EU-RO Runpod network volume; expected checkpoint directories are documented in `docs/RUNPOD.md` |
| Controller data root | `/srv/ace-service/data` |
| Release/current paths | `/opt/audioventura/releases/<git-sha>` and `/opt/audioventura/current` |
| Rollback release | The previous `current` target was not readable: the deployment SSH probe was rejected. The runbook requires retaining the previous release directory and repointing `current`; its exact live SHA must be captured before deployment. |

The checked-in deployment state contains no template ID or image digest. Those
values are intentionally recorded as unavailable rather than inferred from a
tag, model name, or application commit.

## Bindings and path audit

The deployment templates and service units establish these intended paths:

- Public HTTPS terminates at nginx on `443` for `player.evren.io`. HTTP `80`
  redirects to HTTPS.
- The authenticated UI and `/create`/`/cover` submissions proxy to the
  controller at `127.0.0.1:8000`.
- Only `/transfer/v1/source/<capability>` and
  `/transfer/v1/output/<capability>` proxy to the transfer service at
  `127.0.0.1:8001`; capability locations have nginx access logging disabled.
- The Tailscale administrative UI is `100.101.140.74:8088`. The home-ingest
  service is `100.103.69.9:8100` on p100. Home ingest uploads through the
  restricted SFTP account to `/srv/ace-sftp/incoming`, which is a bind mount of
  the controller's `incoming` directory.
- `audioventura-controller.service` and `audioventura-transfer.service` run
  from `/opt/audioventura/current` with separate loopback listeners. The
  home service runs from `/opt/audioventura-home/current/home_ingest` and is
  bound to the home Tailscale address.

The controller wiring is metadata-only at the inference boundary:

1. The authenticated controller validates and persists a job before the
   singleton `ControllerWorker` starts a variation.
2. Covers call the authenticated home-ingest API, verify the returned source
   metadata, and create a short-lived signed source capability.
3. The controller creates a separate signed output capability and sends only
   bounded metadata plus those capability URLs through `RunpodClient`.
4. The worker downloads the source capability when needed, generates locally,
   and uploads through the output capability. The controller polls the durable
   Runpod ID and never calls ACE-Step directly.

The live path was only probed without credentials. No paid job or source/output
capability was submitted during this checkpoint.

## Network probe evidence

Probes ran on 2026-08-08 at approximately 11:18 UTC:

- `https://player.evren.io/` returned HTTP `401` with the `ACE Service` Basic
  challenge and security headers. This verifies public 443 and the auth gate.
- `https://player.evren.io/transfer/v1/source/checkpoint-1-no-capability`
  returned HTTP `404`, as expected for an invalid capability.
- `GET https://player.evren.io/transfer/v1/output/checkpoint-1-no-capability`
  returned HTTP `405`; the output capability location is routed, while the
  unauthenticated/read-only method is rejected.
- Requests to `http://player.evren.io:8000/` and `:8001/` timed out; neither
  internal port was reachable through the public hostname.
- `http://100.101.140.74:8088/` returned HTTP `401` without credentials.
- `http://100.103.69.9:8100/healthz` returned HTTP `401` without the home
  bearer token. The private socket is reachable, but an authenticated health
  assertion was not attempted without the protected token.
- A read-only SSH inspection of the VPS was rejected with `Permission denied
  (publickey)`, so the live release symlink, systemd state, nginx rendering,
  endpoint template, image digest, and rollback SHA remain operator evidence
  to collect before Checkpoint 6.

## Characterization assertions for the two defects

These are recorded assertions, deliberately not regression tests. Checkpoint 2
must replace them with desired-behavior tests; no test should continue to pass
because either omission remains in place.

- A v1 cover payload with `generation.cover_strength=0.75` reaches the worker
  as `audio_cover_strength=0.75`, while `cover_noise_strength` is absent from
  the constructed ACE-Step parameters. The controller's current cover model
  and normalized JSON contain only `cover_strength`.
- A v1 original payload without `generation.duration` reaches the worker with
  `duration=-1.0`, from the fallback in `runpod_worker/handler.py`. The
  controller currently permits an omitted original duration and does not map
  duration-like prose into the field.

The current worker boundary is intentionally strict: `SCHEMA_VERSION = 1`,
unknown top-level and generation fields are rejected, and any schema version
other than 1 raises `SchemaError`. The controller emits
`WORKER_SCHEMA_VERSION = 1` for new normalized requests and persists the
normalized JSON in the existing job JSON column. This is the compatibility
baseline that the dual-v1/v2 worker must preserve.

## Queue recovery and database migration baseline

Queued and in-flight state is recovered by the controller worker on startup:

- queued jobs/variations are re-enqueued after durable state is read;
- a persisted Runpod ID is polled rather than submitted again;
- a submission nonce without a Runpod ID becomes an uncertain failed attempt,
  preventing an automatic duplicate paid submission; and
- interrupted ingestion is resumed or failed closed according to the existing
  source/output validation rules.

SQLite startup currently calls `Base.metadata.create_all(engine)` from
`initialize_database`. That creates missing foundation tables but does not
alter an existing table, track an application schema version, reject an older
schema, or make a migration backup. Checkpoint 4 must replace this startup
assumption with an ordered additive migration gate.

The exact SQLite backup rehearsal uses the SQLite backup API, not a raw copy of
a live WAL database. Run it with services stopped and pair the database with a
copy of `outputs/`:

```bash
DB=/srv/ace-service/data/service.db
BACKUP=/srv/ace-service/backups/service.db.pre-migration-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p /srv/ace-service/backups
uv run python -c 'import sqlite3,sys; source,destination=sys.argv[1:]; src=sqlite3.connect(source); dst=sqlite3.connect(destination); src.backup(dst); dst.close(); src.close()' "$DB" "$BACKUP"
```

Restore rehearsal uses the same API in the reverse direction, after the old
application release is active and with the target database moved aside:

```bash
DB=/srv/ace-service/data/service.db
RESTORE=/srv/ace-service/backups/service.db.pre-migration-<timestamp>
uv run python -c 'import sqlite3,sys; source,destination=sys.argv[1:]; src=sqlite3.connect(source); dst=sqlite3.connect(destination); src.backup(dst); dst.close(); src.close()' "$RESTORE" "$DB"
```

`<timestamp>` is the exact backup suffix recorded in the migration rehearsal;
the command must not be pointed at an unverified broad directory.

## Pre-edit verification baseline

Commands were run from this application worktree unless noted otherwise:

```text
uv run pytest -q tests runpod_worker/tests       112 passed, 6 failed
uv run ruff check .                              passed
uv run ruff format --check .                    passed (67 files)
uv run mypy src runpod_worker                   passed (31 source files)
cd home_ingest && uv run pytest -q               passed (33 tests)
cd home_ingest && uv run ruff check .            passed
cd home_ingest && uv run ruff format --check .  passed (14 files)
cd home_ingest && uv run mypy src                passed (8 source files)
```

The six root-suite failures are pre-existing worker MP3 tests. They all fail
when importing `lameenc` because the application development environment does
not install the worker image's Docker-only `lameenc==1.8.4` dependency; the
Dockerfile installs it separately. No Checkpoint 1 change caused these
failures. As a diagnosis-only check, the same exact suite passed `118` tests
when invoked with `uv run --with lameenc==1.8.4`; this does not replace the
prescribed baseline command. This baseline must be distinguished from any new
failure after the checkpoint.

The private fixture manifest and no-run baseline result are stored at:

- `/srv/ace-service/data/evaluations/quality-fixture-v1/manifest.json`
- `/srv/ace-service/data/evaluations/quality-fixture-v1/baseline-result.json`

They are outside Git, mode-restricted, and must not be copied into the
repository or logs.
