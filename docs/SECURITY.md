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
```

The proxy must reject every other path and disable access logging for these
routes. The final path segment is a bearer credential.

Home Ingest binds to loopback port 8100 by default. It requires a bearer token
and should be reachable only over the private tailnet.

Do not publish any of these ports directly.

## Credentials

Keep long-lived credentials in deployment-managed environment files or a
secret store, never in Git. This includes:

- controller username and password;
- Home Ingest bearer token;
- SFTP private key;
- Runpod and Salad API keys;
- private registry credentials;
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

- job;
- source-download or output-upload direction;
- deterministic relative path;
- extension and MIME expectations;
- maximum byte count;
- UTC expiry.

Source downloads validate path containment, file type, extension, size, and
symlink safety. Output uploads stream through a hard byte limit into a private
partial file, compute SHA-256 while receiving, fsync, and atomically rename.
Conflicting replays cannot replace an accepted output.

Never place a capability URL in logs, tickets, chat, provider metadata, or a
durable incident record.

## Media and paths

The configured data root is the containment boundary for the database,
incoming sources, outputs, partial files, and logs. All media paths are stored
as relative paths and resolved below their expected root. Traversal, symlink
components, unsupported types, size mismatch, and SHA-256 mismatch fail
closed.

Authenticated playback accepts a database output ID, not an arbitrary path,
and revalidates the file before streaming it.

Home Ingest is the only component allowed to contact YouTube or run media
tools. Its SFTP account must be key-only, have no shell, and be restricted to
the incoming directory. GPU workers must not receive YouTube cookies or home
credentials.

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

## Logs and retention

Controller and Home Ingest logs use UTC rotating files with private
permissions. Redaction covers configured secrets, authorization values,
capability URLs, prompts, lyrics, and token-shaped fields. Do not log raw
requests or provider responses.

Cleanup removes expired capabilities, stale partial files, old Home Ingest
temporary directories, and non-retained terminal cover sources. It does not
remove completed outputs. Backups therefore contain private user media and
must receive the same access controls as the live data root.

## Release check

Before deployment or public source publication:

1. scan the complete Git history, not only the current tree;
2. confirm `.env`, databases, audio, fixtures, keys, and logs are untracked;
3. verify proxy routing and access-log suppression;
4. verify private-service binds and authentication;
5. verify worker images contain no credentials;
6. verify the configured image and model revisions are immutable;
7. run the full tests and static checks;
8. run one bounded live transfer/inference acceptance when deployment changed.
