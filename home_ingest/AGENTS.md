# Home ingest module

This directory owns the private home-server agent. It is the only component
allowed to contact YouTube or execute `yt-dlp`, `ffprobe`, and `ffmpeg`.

Keep the HTTP service bound to localhost (or an explicitly private Tailscale
interface), require the configured bearer token, and never put user metadata
in local or remote filenames. Prepared media is uploaded only to the
configured restricted SFTP incoming directory using a UUID-derived `.part`
path. Run checks from this directory with `uv run pytest -q`, `uv run ruff
check .`, `uv run ruff format --check .`, and `uv run mypy src`.
