# Home ingest module

This directory owns the private home-server agent. It is the only component
allowed to contact YouTube or execute `yt-dlp`, `ffprobe`, and `ffmpeg`.

Keep the HTTP service bound to localhost (or an explicitly private Tailscale
interface), require the configured bearer token, and never put user metadata
in local or remote filenames. New source, clip, and playback-derivative
operations use the configured v2 bounded transfer client; the legacy YouTube
recovery path may still upload to the restricted SFTP incoming directory using
a UUID-derived `.part` path. Uploaded inputs must use the local-only
ffprobe/ffmpeg protocol allowlist and no redirects. Run checks from this
directory with `uv run pytest -q`, `uv run ruff check .`, `uv run ruff format
--check .`, and `uv run mypy src`.
