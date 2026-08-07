# Development Log

## 2026-08-07

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
