# ACE Service

The controller is a private FastAPI web app for original-song and YouTube-cover
jobs. It binds to `127.0.0.1:8000`, uses HTTP Basic plus same-site CSRF for the
browser UI, persists jobs in SQLite, and serves completed audio only through
authenticated controller routes. The separate transfer app remains the only
publicly proxied surface and binds to `127.0.0.1:8001`.

Run the controller locally with configured credentials using:

```text
uv run python -m ace_service
```

Operational deployment, Tailscale/proxy policy, cleanup, backups, and live
acceptance are documented in [docs/OPERATIONS.md](docs/OPERATIONS.md).

The detailed deployment and distributed-runtime handoff follows below.

# aflow Handoff Plan: Hetzner + Home Ingest + Runpod Flex ACE-Step Service

Build the first usable release of a private music-generation service with a deliberately split runtime:

1. **Hetzner VM is the permanent control plane and web application.**
2. **Home server is the YouTube/media-ingest node.** Every `yt-dlp`, `ffprobe`, and `ffmpeg` operation runs there so YouTube requests originate from the residential/home connection. Runpod encodes generated MP3 output in-process with LAME; Hetzner performs no media processing.
3. **Runpod Serverless Flex is the only ACE-Step inference backend in v1.** The MacBook/MLX path is deferred.
4. **Runpod never receives YouTube credentials, SSH keys, SFTP credentials, or direct access to the home network.**
5. **Large audio never travels inside the Runpod `/run` JSON payload.** Hetzner exposes narrowly scoped, short-lived HTTPS capability URLs for source download and result upload.

The first release supports two workflows:

1. Generate an original song from creative instructions, optional lyrics, optional musical metadata, and one to four sequential variations.
2. Generate a cover or stylistic reinterpretation from a single public YouTube video after the home server downloads and prepares the source audio.

New jobs use a strict version-2 worker payload. Original requests choose
`direct`, `enhance`, or `auto-compose` prompting and either model-selected
duration (`auto`, sent as `-1.0`) or an explicit 10-600 second custom value.
The final caption and lyrics are bounded at 511 and 4095 characters. Cover
requests expose ACE-Step's independent `audio_cover_strength` and
`cover_noise_strength` controls, retain the probed source duration, and stage
for browser confirmation before any Runpod submission. The staged cover page
can confirm or cancel preparation, and asynchronous status polling reloads it
once when confirmation becomes available. Two to four cover variations run
sequentially; a supplied seed advances deterministically. Duration prose is
accepted only when bounded numeric seconds/minutes match an explicit custom
duration; it never changes the structured value.

Immediately before starting a schema-v1 controller rollback, run the local
read-only gate against the configured database:

```text
ACE_SERVICE_DATA_ROOT=/srv/ace-service/data uv run python -m ace_service.rollback_readiness
```

The command exits zero only when no nonterminal or malformed schema-v2 state
or unconfirmed cover staging is present. A nonzero or indeterminate result
means the v2-capable controller and worker must remain active.

### Runtime Architecture

```text
Trusted browser / phone
        |
        | Tailscale HTTPS
        v
+----------------------------------------------------+
| Hetzner VM                                         |
|                                                    |
| FastAPI controller/UI on 127.0.0.1:8000           |
|   - auth + CSRF                                    |
|   - SQLite                                         |
|   - job state machine                              |
|   - one controller worker                         |
|   - Runpod API client                              |
|   - persistent source/output storage               |
|                                                    |
| Public transfer app on 127.0.0.1:8001             |
|   - ONLY signed /transfer/v1/* routes              |
|   - source GET for Runpod                          |
|   - generated-output PUT from Runpod               |
|                                                    |
| Caddy/Nginx on public HTTPS                        |
|   - forwards ONLY /transfer/v1/* to :8001          |
|   - binds public interface                         |
+-------------------+----------------+---------------+
                    |                ^
      Tailscale API |                | short-lived HTTPS
                    v                | capability URLs
+--------------------------------+   |
| Home server                    |   |
|                                |   |
| private ingest agent           |   |
| - YouTube metadata             |   |
| - yt-dlp audio download        |   |
| - ffprobe validation           |   |
| - ffmpeg normalization         |   |
| - SFTP upload to Hetzner       |   |
+--------------------------------+   |
                                     |
                                     v
                           +-------------------------+
                           | Runpod Serverless Flex  |
                           |                         |
                           | custom ACE-Step worker  |
                           | RTX 4090 24 GB target   |
                           | workersMin = 0          |
                           | workersMax = 1          |
                           | batch_size = 1          |
                           | XL Turbo + 1.7B LM      |
                           +-------------------------+
```

### Why This Split Exists

- Hetzner is lightweight enough for the control plane. It does no ML inference and no audio transcoding.
- YouTube access stays on the home connection. Cloud/datacenter IP reputation cannot break the main controller or force media download from Hetzner.
- Home is the only runtime that invokes `ffmpeg` or `ffprobe`; Runpod's generated-output MP3 path uses in-process LAME and the Hetzner control plane performs no media processing.
- Runpod receives only clean generation parameters and, for covers, a temporary HTTPS URL to a prepared source file.
- The MacBook and home server are absent from original-song generation. A home-server outage disables only new YouTube cover ingestion.
