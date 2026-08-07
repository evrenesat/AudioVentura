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
