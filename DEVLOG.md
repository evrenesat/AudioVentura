# Development Log

## 2026-08-08

- Recorded Checkpoint 1's application/deployment heads, private network
  bindings, v1 schema and queue-recovery behavior, current `create_all()`
  startup limitation, SQLite backup/restore rehearsal, and pre-edit test
  baseline in `docs/CHECKPOINT-1-BASELINE.md`.
- Added the fixed-input, blinded listener contract and USD 5 hard campaign
  guard in `docs/QUALITY-EVALUATION.md`. Stored the CC0 fixture manifest and
  honest no-run baseline result under the private data root; no evaluation
  media or paid inference state is tracked in Git.
- Re-ran the full Checkpoint 1 verification after the approved baseline
  evidence: the six known Docker-only `lameenc` test failures remain isolated,
  while Ruff, format, mypy, and all home-ingest checks pass. Revalidated the
  private fixture hash, no-run result shape, and `0600` permissions without
  making a Runpod call.
- Implemented the focused Checkpoint 2 recovery: immutable v2 profiles and
  prompt modes, explicit original duration, independent cover controls,
  source-duration staging/confirmation, strict dual-version worker parsing,
  fail-closed ACE constructor checks, sequential reproducible variations, and
  bounded versioned result metadata in existing JSON fields.
- Repaired the v2 lifecycle overlay: staged covers can cancel safely, polling
  reveals confirmation asynchronously, pinned ACE LM metadata and effective
  captions persist without rewriting lyrics, status-loss recovery requires
  validated v2 evidence, worker images require immutable digests, and matching
  numeric duration prose is accepted without changing structured seconds.
- Added the read-only schema-v1 rollback gate with fail-closed v2 lifecycle
  classification, and corrected pinned LM lyric truth for empty-input
  Enhance/auto-compose requests while preserving supplied lyrics exactly.
- Unified bounded result projection so profile, generated metadata, resolved
  parameters, output evidence, and worker identity survive on outputs in both
  result-before-upload and normal arrival orders; queue/execution timing stays
  on attempts.
- Added `lameenc==1.8.4` to the development group so the local worker contract
  suite uses the same in-process MP3 encoder already installed by the worker
  image; production controller dependencies remain media-free.

## 2026-08-07

- Repaired the Runpod output boundary: removed the worker image's `ffmpeg`
  package, pinned `lameenc==1.8.4`, and changed MP3 generation to encode a
  temporary PCM WAV in-process while preserving requested output metadata and
  cleanup guarantees.
- Started the Hetzner controller foundation with typed settings, SQLite
  persistence, path-safe records, and baseline tests.
- Corrected configuration validation to require HTTPS for public transfers and
  reject credential placeholders without a runtime bypass.
- Added durable variation attempts, explicit controller transitions, singleton
  data-root locking, serialized Runpod orchestration, and restart recovery.
- Added the isolated home-ingest agent with strict YouTube URL validation,
  bounded yt-dlp download, local ffprobe/ffmpeg canonicalization, restricted
  SFTP `.part` upload, checksum metadata, and failure cleanup.
- Connected cover jobs to home-ingest, verified SFTP source finalization,
  signed Runpod source/output capabilities, cover payload mapping, restart
  polling, and non-retained source cleanup.
- Added the authenticated mobile-friendly controller UI with Basic auth,
  same-site CSRF, job forms/history/status polling, readiness banners,
  authenticated playback/download routes, and media containment/checksum
  validation. Kept the UI app separate from the public transfer app and added
  an authenticated home-ingest health probe.
- Added Checkpoint 9 operational hardening: private UTC rotating logs with
  credential/capability/prompt redaction, startup and periodic controller and
  home cleanup, terminal capability revocation, bounded source retention, and
  deployment/acceptance runbooks.
