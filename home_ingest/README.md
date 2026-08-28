# AudioVentura Home Ingest

Home Ingest is the private media boundary for AudioVentura. It is the only
component allowed to contact YouTube or execute `yt-dlp`, `ffprobe`, and
`ffmpeg`; the controller and transfer service never probe or transcode media.

The bearer-authenticated v2 API provides:

```text
POST /v2/prepare-source
POST /v2/prepare-clip
POST /v2/prepare-playback-derivative
```

Each operation streams through the configured bounded transfer client, accepts
only the configured HTTPS host/port and exact v2 route, verifies byte count and
SHA-256, and refuses redirects. Source and container inputs are judged by
actual `ffprobe`/`ffmpeg` capability, use the first audio stream, discard
video, strip metadata, and normalize to stereo 48 kHz 192 kbps MP3. Source
ingest has no generation-duration ceiling; byte, command, and network
timeouts remain enforced. Temporary directories and partial files are removed
on success, failure, timeout, and cancellation.

`/v1/prepare-youtube-cover` and the restricted SFTP uploader remain only for
recovery of persisted legacy jobs. Keep this service on localhost or an
explicitly private Tailscale interface, keep tokens and URLs out of logs, and
run its checks with:

```text
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
uv run mypy src
```

The repository-level architecture and deployment rules are authoritative in
[`ARCHITECTURE.md`](../ARCHITECTURE.md) and
[`docs/OPERATIONS.md`](../docs/OPERATIONS.md).
