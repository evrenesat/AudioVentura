# aflow Handoff Plan: Hetzner + Home Ingest + Runpod Flex ACE-Step Service

Build the first usable release of a private music-generation service with a deliberately split runtime:

1. **Hetzner VM is the permanent control plane and web application.**
2. **Home server is the YouTube/media-ingest node.** Every `yt-dlp`, `ffprobe`, and `ffmpeg` operation runs there so YouTube requests originate from the residential/home connection.
3. **Runpod Serverless Flex is the only ACE-Step inference backend in v1.** The MacBook/MLX path is deferred.
4. **Runpod never receives YouTube credentials, SSH keys, SFTP credentials, or direct access to the home network.**
5. **Large audio never travels inside the Runpod `/run` JSON payload.** Hetzner exposes narrowly scoped, short-lived HTTPS capability URLs for source download and result upload.

The first release supports two workflows:

1. Generate an original song from creative instructions, optional lyrics, optional musical metadata, and one to four sequential variations.
2. Generate a cover or stylistic reinterpretation from a single public YouTube video after the home server downloads and prepares the source audio.

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
- Every `ffmpeg` and `ffprobe` invocation stays on the home server by design.
- Runpod receives only clean generation parameters and, for covers, a temporary HTTPS URL to a prepared source file.
- The MacBook and home server are absent from original-song generation. A home-server outage disables only new YouTube cover ingestion.
