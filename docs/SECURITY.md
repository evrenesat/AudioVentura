# Security boundary

## Public surface

The transfer service binds to 127.0.0.1:8001. The public HTTPS proxy may
forward only these paths:

    /transfer/v1/source/*
    /transfer/v1/output/*

Every other path must return a proxy-level 404 or 403 without reaching the
private controller or UI. The transfer FastAPI app disables its documentation
and OpenAPI routes and mounts no controller routes.

## Capabilities

Capabilities use random 256-bit URL-safe tokens. The plaintext token exists
only in the newly returned capability URL; SQLite stores only its SHA-256
hash. A capability is bound to one job, direction, deterministic relative
path, expected extension, byte limit, and UTC expiry. Wrong-direction,
expired, revoked, and malformed capabilities are rejected without revealing
whether another capability exists.

Source downloads resolve below the configured incoming root and reject
traversal, symlink components, non-regular files, unexpected extensions, and
files over the recorded limit. Canonical source content is served as
audio/mpeg; repeat GETs remain valid until expiry so Runpod retries do not
need a new token.

Output uploads require the output capability and enforce Content-Length when
provided. The body is streamed through a hard byte limit into a deterministic
.part file. SHA-256 is computed while receiving; the file is flushed and
fsynced before atomic rename, and failures remove the .part file. The output
record and consumed capability are committed together. A byte-identical
completed retry returns success only within the original capability TTL; after
expiry, replays are rejected without replacing the accepted file or erasing
the consumed capability history. A conflicting retry is rejected without
replacing the accepted file.

## Runpod and persistence

Runpod receives metadata and short-lived capability URLs, never source audio,
YouTube credentials, SSH/SFTP credentials, or the controller API key. The
adapter uses explicit HTTP timeouts and does not include response bodies or
credentials in raised API errors.

Before /run, the controller commits a fresh submission nonce and its pre-submit
state. It persists the returned Runpod job ID immediately after a successful
response. On recovery, a nonce without a cloud ID becomes a failed
uncertain_cloud_submission attempt and is never automatically resubmitted; an
existing cloud ID resumes polling. This prevents a crash window from creating
a duplicate paid job.

No live Runpod transfer acceptance test is claimed by this local checkpoint.
