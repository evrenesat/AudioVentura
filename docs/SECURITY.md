# Security

This is the operator checklist for AudioVentura's trust boundaries.

## Exposed services

The controller UI binds to loopback port 8000. It requires HTTP Basic
authentication and same-site CSRF tokens. Expose it only through the private
tailnet.

The transfer app binds to loopback port 8001. A public HTTPS proxy may forward
only:

```text
/transfer/v1/source/*
/transfer/v1/output/*
/asset-transfer/v2/upload/<token>
/asset-transfer/v2/download/<token>
```

The proxy must reject every other path and disable access logging for these
routes. The final path segment is a bearer credential. Only v2 upload accepts
the exact 512 MiB raw-body exception; v2 download has no large-body nginx
override. Beta exposes the same v2 shapes below `/beta-transfer` to port 8011,
never production port 8001.

Home Ingest binds to loopback port 8100 by default. It requires a bearer token
and should be reachable only over the private tailnet.

The p100 home host also carries isolated beta Home Ingest on `:8101` and the
sequential MIDI mock on `:8201`; production counterparts are `:8100` and
`:8200`. These are private Tailscale-bound services, not public proxy routes.
Beta Home Ingest uses a separate service user, state root, bearer token, and
restricted SFTP account. Beta and production mock instances use separate
users, state roots, tokens, SQLite cursors, and systemd units.

The isolated beta uses separate loopback services on 8010 (controller) and
8011 (transfer), with `/beta/` private UI routing and `/beta-transfer/` signed
transfer routing. Its environment, database, media/trash root, and rollback
snapshots are separate from production. Beta has no capacity-management or
Web Push credentials, and its Home Ingest target is the separate beta p100
service.
The beta proxy must not widen the production transfer allow-list or forward
beta controller paths to port 8000.

Do not publish any of these ports directly.

## Credentials

Keep long-lived credentials in deployment-managed environment files or a
secret store, never in Git. This includes:

- controller username and password;
- Home Ingest bearer token;
- beta Home Ingest and MIDI mock bearer tokens;
- SFTP private key;
- Runpod and Salad API keys;
- private registry credentials;
- Fal API and CDN credentials;
- any future provider credentials.

The GPU worker does not need those credentials. It receives only bounded job
metadata and short-lived transfer URLs.

Before committing or sharing diagnostics, check `.env`, database files, logs,
shell history, provider responses, and copied plan evidence. Do not print
credentials in deployment commands when a protected environment variable or
file descriptor is available.

## Transfer capabilities

Capability tokens are random 256-bit URL-safe values. SQLite stores only their
SHA-256 hashes. Each capability is restricted by:

- exactly one source, job, or derivative owner;
- purpose and upload/download direction;
- storage namespace and exact UUID-derived relative path;
- extension and MIME expectations;
- maximum byte count;
- UTC expiry.

V2 uploads use a raw streamed PUT: declared oversize is rejected before body
read, lying/missing lengths remain bounded, partial files are removed on
failure, and an identical retry is idempotent while conflicting bytes return
409. V2 downloads permit only bounded opens and validate containment, file type,
extension, size, hash, and symlink safety immediately before streaming.
Conflicting replays cannot replace accepted bytes. V1 capabilities retain their
legacy recovery contract.

Never place a capability URL in logs, tickets, chat, provider metadata, or a
durable incident record.

## Media and paths

The configured data root is the containment boundary for the database,
`uploads/`, `incoming/`, `outputs/`, `library/`, trash, partial files, and logs.
All media paths are stored as relative paths and resolved below their expected
root. Traversal, symlink components, unsupported types, size mismatch, and
SHA-256 mismatch fail closed. Upload filenames, YouTube IDs, titles, and
provider IDs never enter storage paths.

Authenticated playback and download accept a database media-file ID, not an
arbitrary path, and revalidate the active file before streaming it. The
verifier permits only the configured library root, regular non-symlink MP3
files, the recorded MIME type, positive size, and exact SHA-256. The generic
media routes expose no filesystem path and return byte ranges only after this
verification.

Library deletion uses `active -> pending -> deleted`: the file is moved to a
deterministic private trash path with restrictive directory/file modes, then
the database state is reconciled. Cleanup may purge only a deleted item whose
trash path is still contained and whose delayed deletion policy has elapsed.
Project deletion requires all jobs to be terminal, records a bounded audit
summary with allow-listed numeric cost fields and safe provider names, then
removes dependent media/playlists/files without leaving a live capability.

The player queue contains only safe IDs, titles, project labels, duration, and
same-origin route URLs. It never returns audio bytes, prompts, lyrics,
provider payloads, transfer capabilities, or source paths. The library has no
source-upload endpoint and the browser stores playback preferences/position,
not an offline audio cache.

Home Ingest is the only component allowed to contact YouTube or run media
tools. Uploaded/container inputs are probed locally, select only the first
audio stream, and run with a local-only ffmpeg/ffprobe protocol allowlist so a
crafted container cannot cause a network fetch. Its v2 transfer client follows
no redirects and accepts only the configured scheme, host, port, and route.
Its legacy SFTP account must be key-only, have no shell, and be restricted to
the incoming directory. GPU workers must not receive YouTube cookies or home
credentials.

The sequential MIDI mock is not a source-ingest service. It may receive a
bounded source capability description for schema coverage, but it must never
follow a source URL, receive source bytes, or log prompts, lyrics, capability
URLs, or raw provider bodies. Its corpus archive and soundfont are read-only;
only its private per-instance state and temporary render directories are
writable. The mock binds to its private p100 address, requires bearer auth,
accepts only worker schema 2 and MP3 results, and serializes rendering to one
job at a time.

## Provider submissions

Before provider submission, the controller commits a unique nonce. A returned
provider job ID is then stored immediately. If a crash leaves a nonce without
a provider reference, the submission is uncertain and must not be retried
automatically. This prevents duplicate paid jobs.

Each persisted attempt remains owned by its original provider. A different
provider is never used as an implicit fallback. Provider errors contain only a
bounded safe classification; raw response bodies are not persisted or logged.

The controller accepts completion only when provider metadata agrees with the
uploaded output's job ID, nonce, variation, size, and SHA-256.

Fal-specific controls are mandatory: use only the reviewed static catalog;
send `sync_mode=false`, no-IO, no-fallback, and private lifecycle headers;
never send audio bytes or data URIs; require exact endpoint/request ownership;
exchange the API key for a short-lived CDN bearer token; reject redirects,
non-HTTPS URLs, unapproved CDN hosts, wrong content types, and oversized
responses; and materialize results below the private output root atomically.
The controller does not log raw Fal request/result bodies or CDN URLs.

## Logs and retention

Controller and Home Ingest logs use UTC rotating files with private
permissions. Redaction covers configured secrets, authorization values,
capability URLs, prompts, lyrics, and token-shaped fields. Do not log raw
requests or provider responses.

Cleanup removes expired capabilities, stale upload/clip parts, old Home Ingest
temporary directories, non-retained terminal cover sources, and eligible
library-trash files. It does not remove active completed outputs or published
source MP3s. Backups
therefore contain private user media and must receive the same access controls
as the live data root.

## Web Push and capacity safety

Push subscriptions contain endpoint URLs and browser authentication keys. They
are stored only in the private SQLite database, never rendered into HTML,
logged, or returned after creation. Accept only HTTPS endpoints whose exact
origin is in `WEB_PUSH_ALLOWED_ENDPOINT_ORIGINS`; this allow-list is the SSRF
boundary for the server-side dispatcher. VAPID private keys stay in the
protected environment file.

The secret-free service-worker script is publicly fetchable so browser-internal
worker installation does not depend on forwarding a Basic Auth challenge. Its
configuration and subscription routes remain authenticated. The worker is
served with `Service-Worker-Allowed` for the configured root path and accepts
only finite event kinds, bounded copy, and same-origin paths below that scope.
Notification payloads contain no prompts, lyrics, provider identifiers,
subscription data, or capability URLs. Push failure is isolated from job
completion and capacity release. A 404/410 disables only the affected
subscription.

Capacity fingerprints are spend-side guards, not secrets. The controller
refuses resource identity, maximum-one, GPU, deployment, or queue drift and
uses a fenced durable action lease for every provider mutation. Release is not
considered complete until provider-observed zero evidence exists.

## Release check

Before deployment or public source publication:

1. scan the complete Git history, not only the current tree;
2. confirm `.env`, databases, audio, fixtures, keys, and logs are untracked;
3. verify root and beta v1/v2 proxy routing, token-path access-log suppression,
   and separate 8000/8001 versus 8010/8011 upstreams;
4. verify private p100 binds on 8100/8200 and 8101/8201, separate service
   identities, authentication, corpus permissions, and beta SFTP confinement;
5. verify worker images contain no credentials;
6. verify the configured image and model revisions are immutable;
7. run the full tests and static checks;
8. run one bounded live transfer/inference acceptance when deployment changed.
