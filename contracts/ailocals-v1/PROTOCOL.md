# ailocals.v1 protocol

Normative transport contract for the ailocals universal worker protocol. Every
conforming client (Swift) and backend (Python, Go) implements exactly the rules
in this file; `schema.json` and `fixtures/` are the machine-checkable form of
the same rules. Where prose and fixtures disagree, this file wins and the
fixture set must be fixed before any consumer imports it.

Status: frozen at the commit recorded in `ORIGIN.json`. Any incompatible wire
change requires `ailocals.v2` or a new capability version. Never silently relax
v1 while implementing consumers.

## 1. Naming

- Protocol version string: `ailocals.v1`.
- Route namespace: `api/ailocals/v1`, appended to a connection's configured
  base URL while preserving any path prefix (only a trailing slash is
  normalized away).
- Authentication headers: `X-Ailocals-Worker-Token`,
  `X-Ailocals-Enrollment-Token`, `X-Ailocals-Lease-Token`.
- Capability IDs: `music.ace-step.v1`, `tts.apple-speech.v1`,
  `tts.chatterbox.v3`, `llm.openai-relay.v1`.

## 2. Transport rules

- HTTPS only for remote endpoints. Userinfo, query, fragment, redirects, and
  non-HTTPS remote endpoints are rejected. Loopback HTTP is allowed only for
  explicit development fixture connections; never a silent downgrade.
- JSON is UTF-8. Timestamps are RFC 3339 UTC with millisecond precision and a
  `Z` suffix (for example `2026-09-05T12:00:00.000Z`).
- Integers reject booleans and fractions. Identifiers are nonempty ASCII
  `[A-Za-z0-9_-]` strings of at most 128 bytes; UUID and ULID domains stay
  opaque. Keychain/account namespaces use a local connection UUID, never a job
  ID alone.
- Every request body is limited by `limits.control_max_bytes` (262144).
  Non-lease JSON responses use the same limit; lease responses use the 3 MiB
  encoded-response limit (section 6). Multipart completion has the per-part and
  total limits in section 7. Servers enforce body bounds while streaming, not
  after allocating `request.body()`.
- Reject: duplicate object keys, unknown envelope fields, trailing JSON,
  `NaN`/`Infinity`, unsupported protocol strings, malformed hashes, and
  unadvertised capabilities. Known workload payload schemas own their permitted
  fields; do not reject arbitrary valid JSON Schema keywords inside
  `response_format.schema`.

## 3. Authentication

- Credentials are random 32-byte base64url values, stored hashed server-side
  and privately in Keychain on macOS.
- Enrollment tokens are single-use, expire after 30 minutes, and are generated
  by each product's existing owner-authenticated, CSRF-protected settings path.
- Missing, invalid, or revoked worker credential: `401` with code
  `unauthorized`. Transport errors never cause automatic re-enrollment; a 401
  stops only that connection and offers re-enrollment.
- The worker token appears only in the enrollment response, never in status.
- Authentication headers are never forwarded to a local engine or artifact
  URL. Legacy headers are accepted only at legacy routes; using a common header
  avoids collision with independently configured HTTP perimeter authentication.

## 4. Operations

All routes are `{base}/api/ailocals/v1/...`.

### 4.1 GET info (unauthenticated)

Response 200:

```json
{
  "protocol_version": "ailocals.v1",
  "service_kind": "audioventura",
  "environment": "beta",
  "supported_capabilities": ["music.ace-step.v1"],
  "limits": {
    "poll_max_seconds": 25,
    "lease_seconds": 90,
    "heartbeat_seconds": 30,
    "presence_seconds": 20,
    "control_max_bytes": 262144,
    "payload_max_bytes": 2097152,
    "result_max_bytes": 2097152
  }
}
```

- `service_kind` is a lowercase ASCII identifier `[a-z0-9_-]{1,64}`,
  initially `audioventura` or `doublangu`; client core behavior depends on
  capabilities, not a closed product enum.
- `environment` is `beta`, `production`, or `development`, from server config.
- `supported_capabilities` is an array of capability ID strings only — never
  enrolled machine-specific parameter values.
- No workers, owner data, addresses, revisions, keys, or model inventory in
  this public response. The whole response is at most 32 KiB.

### 4.2 POST enroll (X-Ailocals-Enrollment-Token)

Request: `{"protocol_version":"ailocals.v1","worker_name":...,"software_version":...,"capabilities":[...]}`

- `worker_name`: 1–120 Unicode scalars. `software_version`: 1–64 printable
  ASCII. `capabilities`: 1–32 unique capability entries (section 6).
- Response 201: `{"protocol_version":"ailocals.v1","worker_id":...,"worker_token":...,"environment":...}`.
- Token expiry/use is consumed atomically with worker creation. A second
  non-revoked universal-client enrollment in the same environment returns
  `409 client_already_enrolled`, leaving the new token unused.
- Replacement is an explicit owner revoke/resolve-old-work then new-enroll
  action; concurrent replay yields exactly one success.
- A lost enrollment response requires a new enrollment token and owner
  revocation of the orphan row; there is no token-recovery API.

### 4.3 POST presence (X-Ailocals-Worker-Token)

Request: full replacement snapshot of the enrolled capabilities:

```json
{
  "protocol_version": "ailocals.v1",
  "capabilities": [
    {"id": "music.ace-step.v1", "state": "busy", "accepting": false,
     "active_jobs": 1, "reason": null}
  ]
}
```

- Omitted entries become disabled. Advertised IDs must be enrolled; anything
  else is rejected before state changes.
- `state`: `ready`, `busy`, `paused`, `setup_required`, `error`.
- `active_jobs` is 0 or 1. `reason` is null or one of `slot_busy`,
  `memory_pressure`, `insufficient_memory`, `storage_unavailable`,
  `local_service_unreachable`, `setup_missing`, `user_paused`.
- An enabled resource wait is `busy` with `active_jobs=0`, `accepting=false`,
  and a resource reason. A running job is `busy` with `active_jobs=1`.
  `paused`/`setup_required`/`error` always set `accepting=false`.
- Response 200: `{"protocol_version":"ailocals.v1","server_time":...}`.
- Presence does not renew job leases. Presence older than 120 seconds means
  the worker is offline. Busy supported workers stay known (existing backend
  queue/deadline behavior still applies); a disabled/setup/error capability
  does not count as usable. Presence is independent of leasing so resource
  contention never looks like network failure.

### 4.4 POST lease (X-Ailocals-Worker-Token)

Request: `{"protocol_version":"ailocals.v1","capability_id":...,"wait_seconds":0..25}`.

- The worker must have acquired a local execution slot before calling.
- Exactly one capability per request. `wait_seconds` integer 0–25; an
  immediate poll (`0`) still performs one claim attempt. The server never
  holds a database transaction through the wait.
- No work: `204` with an empty body.
- Work: `200` with the lease envelope (section 6).
- A second active lease for the same worker and capability is `409
  worker_busy`. Capabilities with different IDs (Apple speech and Chatterbox)
  may each hold one lease concurrently. A broad category is never an
  authorization, binding, or concurrency key.

### 4.5 POST jobs/{job_id}/heartbeat (worker + lease headers)

Request: `{"protocol_version":"ailocals.v1","attempt":N,"progress_percent":0..100}`.

- Response 200:
  `{"protocol_version":"ailocals.v1","lease_expires_at":...,"cancel_requested":bool}`.
- Renewal sets expiry to now + 90 seconds, only for the current valid attempt,
  and only until the product's absolute deadline when one exists.
- `cancel_requested` stays true until the attempt is terminal.
- Expired or replaced attempt: `409 lease_lost`, never resurrection. Clients
  stop execution when lease validity can no longer be established.

### 4.6 POST jobs/{job_id}/complete (worker + lease headers, multipart)

Multipart with parts described in section 7. Response 200:
`{"protocol_version":"ailocals.v1","accepted":true}`.

- An identical accepted completion retry returns 200 for the same
  worker/job/attempt/token and bytes, even after the old lease deadline.
- A different result for an already-accepted attempt: `409 result_conflict`.
- Another attempt/worker, or an expired uncommitted attempt: `409 lease_lost`.

### 4.7 POST jobs/{job_id}/fail (worker + lease headers)

Request: `{"protocol_version":"ailocals.v1","attempt":N,"code":...,"retryable":bool}`.

- `code` is exactly one of: `canceled`, `interrupted`, `resource_exhausted`,
  `invalid_payload`, `setup_required`, `execution_failed`,
  `relay_unreachable`, `relay_auth`, `relay_invalid_response`,
  `relay_model_unknown`. These are execution outcomes, distinct from HTTP
  error codes.
- `retryable` is advice; the product backend decides its existing retry
  budget. `canceled` is a terminal user/server cancellation. `interrupted`
  covers unexpected owned-process loss. `resource_exhausted` and
  `relay_unreachable` may request retry, bounded by backend policy.
  Authentication/schema/setup/model-unknown failures are not retried
  automatically. A server cancellation always wins over retry advice.
- Response 200: `{"protocol_version":"ailocals.v1","accepted":true}`.
- An identical terminal failure acknowledgement is idempotent. Success cannot
  be overwritten by failure.

## 5. Errors

Every error response is JSON:

```json
{"protocol_version":"ailocals.v1",
 "error":{"code":"...","message":"..."}}
```

- `message` is bounded safe English, at most 256 characters. No internal
  exception text, request body, URL, prompt, or token appears in messages.
- Codes: `unauthorized`, `enrollment_invalid`, `protocol_unsupported`,
  `invalid_request`, `unsupported_capability`, `worker_busy`,
  `client_already_enrolled`, `lease_lost`, `result_conflict`,
  `payload_too_large`, `rate_limited`, `internal_error`.
- Status mapping: 400 invalid request/protocol/capability/payload shape,
  401 unauthorized, 409 worker_busy/client_already_enrolled/lease_lost/
  result_conflict, 413 payload_too_large, 429 rate_limited, 503 internal
  temporary unavailability. `unsupported_capability`: unknown non-enrolled
  capability is 400; a supported capability already owned is 409 `worker_busy`.
- 503 signals temporary server unavailability; retryable local engine failures
  are reported through `fail`, not HTTP 503 to the product user.

## 6. Capabilities and the lease envelope

Capability IDs are immutable versioned implementations grouped into
descriptive categories (categories are UI labels only):

| Capability ID | Category |
| --- | --- |
| `music.ace-step.v1` | music |
| `tts.apple-speech.v1` | tts |
| `tts.chatterbox.v3` | tts |
| `llm.openai-relay.v1` | llm |

Enrollment entry: `{"id":...,"category":...,"parameters":{...}}` where
`parameters` is a strict implementation-specific object:

- ACE: `{"worker_schema":2,"model_bundle_revision":...,"manifest_sha256":...,
  "accelerator":"mps","formats":["mp3","flac","wav"]}`. Existing source/output
  limits stay with the backend.
- Apple/Chatterbox: the existing exact `speech.WorkerCapability` fields nested
  unchanged: `{"engine":"avspeech"|"chatterbox","languages":[...],
  "unit_kinds":[...],"max_bytes":N,"max_duration_ms":N}` with the existing
  bounds from `speech-worker-v1.schema.json` (languages 1–32, unit_kinds 1–8
  of `word|phrase|sentence|*`, nonnegative integers).
- Relay: `{"max_completion_bytes":2097152,"operations":["chat_completion",
  "list_models"]}`. No local URL, key, or model names; model inventory uses
  `list_models` through the authenticated job path.

Capability sets are owner-selected product subsets: AV accepts
`music.ace-step.v1`; DL accepts the two TTS capabilities and the relay
capability; enrollment need not include every capability a backend supports.
Server interest never expands a grant.

Lease response exact top-level fields:

```json
{
  "protocol_version": "ailocals.v1",
  "job_id": "...",
  "attempt": 1,
  "lease_token": "...",
  "lease_expires_at": "2026-09-05T12:01:30.000Z",
  "deadline_at": null,
  "capability_id": "music.ace-step.v1",
  "payload_encoding": "base64",
  "payload_base64": "...",
  "payload_sha256": "..."
}
```

- `attempt >= 1`. `deadline_at` is a UTC timestamp when the product has a
  persisted absolute deadline, otherwise null. Do not invent a new timeout or
  migration merely to populate it. Existing parent cancellation and local
  request timeouts still apply.
- `payload_base64` is canonical padded RFC 4648 base64; its decoded bytes must
  match lowercase 64-hex `payload_sha256`.
- Encoded response hard limit: 3 MiB. Decoded payload limit:
  `payload_max_bytes` (2 MiB); ACE decoded input is additionally capped at
  65536 bytes by its own schema.
- Base64 is intentional: domain payload bytes and request hashes are reused
  without JSON canonicalization differences among Swift, Go, and Python. It is
  not encryption. No payload bytes enter logs.
- Domain hashes are never replaced by `payload_sha256`; it verifies transport
  integrity only. TTS `request_hash` and the relay's domain-separated request
  hash stay authoritative.

## 7. Workload payloads and completion

### 7.1 ACE music payload (`music.ace-step.v1`)

The payload is the existing NodeProvider submission envelope with
`schema_version=2` and exactly the fields `schema_version`,
`application_job_id`, `variation_index`, `submission_nonce`, `input`,
`source`, `result_upload`. The backend's existing schema/identity validators
remain authoritative. `input` is the existing worker schema-2 request:
`schema_version`, `job_id`, `submission_nonce`, `variation_index`,
`task_type` (`original`|`cover`), `profile_id` (`fast-beta-v1`|`quality-v1`),
`resolved_parameters`, cover duration metadata (`source_duration_seconds`,
`resolved_target_duration_seconds`, `ace_duration_seconds`, cover only),
`cover_staging` (cover only), `generation`, `source`, `result_upload`.
Scoped transfer URLs are generated at lease time and never stored in protocol
queue records or logs. Audio is PUT by the Python runtime through the
product's transfer service, never carried in provider API JSON.

Result bytes are the existing bounded worker result metadata
(`schema_version=2`, at most 65536 encoded bytes, no transfer URLs).
Completion verifies nonce, variation, output size/hash, model identity, and
committed transfer output using current AV validation.

### 7.2 TTS payload (`tts.apple-speech.v1`, `tts.chatterbox.v3`)

The payload contains the current speech lease's domain fields, excluding the
transport fields `protocol_version`, `job_id`, `attempt`, `lease_token`,
`lease_expires_at`, `job_type`:

```json
{
  "render_id": "...", "request_hash": "...", "speech_unit_id": "...",
  "language": "nl", "unit_kind": "word", "spoken_text": "...",
  "context_pronunciation_key": null,
  "profile": {"engine":"avspeech","model_revision":"...","language":"nl",
              "voice_identifier":"...","reference_audio_hash":null,
              "speed_milli":1000,"pitch_cents":0,"mapping_version":"...",
              "mime_type":"audio/mp4","codec":"aac-lc","sample_rate_hz":24000,
              "channels":1,"active":true},
  "limits": {"max_bytes":N,"max_duration_ms":N}
}
```

The capability selects the implementation. Keep `render_id`, `request_hash`,
`speech_unit_id`, `language`, `unit_kind`, `spoken_text`,
`context_pronunciation_key`, `profile`, `limits` exactly as today, including
the `nl-NL` → `nl` canonicalization for Chatterbox and the existing Apple
Xander voice identity. Model/reference/profile identity stays in the leased
profile and is revalidated before execution.

Result is `{"artifact":<existing ArtifactMetadata>}`: `request_hash`,
`sha256`, `size_bytes`, `mime_type` (`audio/mp4`), `codec` (`aac-lc`),
`sample_rate_hz` (24000), `channels` (1), `duration_ms`. The artifact file
follows the existing mono 24 kHz AAC-LC M4A rules and the exact output limits
from the lease.

### 7.3 LLM relay payload (`llm.openai-relay.v1`)

The payload is the exact stored existing `llm.relay.v1` request JSON,
including its legacy internal `protocol_version` (`speech-worker.v1`) and
`operation` (`chat_completion` | `list_models`). That nested field is a
legacy domain payload version, not the outer transport version. The adapter
forwards existing validated `messages`/`model`/`options`/`response_format`/
`limits`. Keep the current user/assistant role constraints, non-streaming
responses, existing byte bounds, and for this release the current DL
schema-name rule (`doublangu_stage_artifact`, strict). Future consumers can
define another versioned capability; DL validators are not weakened to
generalize speculatively.

- `chat_completion` result stays the current `RelayChatResult`:
  `{"request_id","content","reported_model","provider_request_id",
  "finish_reason","usage":{"prompt_tokens","completion_tokens",
  "total_tokens"},"timing":{...known keys...}}`.
- `list_models` result stays `{"request_id","models":[...]}`.
- No endpoint, arbitrary HTTP method, server-supplied credential, file path,
  tool execution, or OMLX admin request appears in a lease.

### 7.4 Completion multipart

Every completion request contains exactly:

- `metadata`: JSON `{"protocol_version":"ailocals.v1","attempt":N,
  "result_sha256":"<64 hex>"}` (≤ 8 KiB).
- `result`: `application/json` bytes ≤ `result_max_bytes` (2 MiB).
- TTS additionally contains exactly one `artifact` part, media type
  `audio/mp4`, filename `audio.m4a`, bounded by the lease artifact limits.
  ACE and relay forbid artifact parts.

Duplicate parts, unknown parts, malformed content types, oversize
fields/files, and hash mismatches are rejected before publication. Total body
bound: metadata limit + result bound + TTS lease artifact bound + 64 KiB
multipart overhead, enforced while streaming. The server ignores supplied
filenames for filesystem paths.

Idempotency:

- TTS compares the accepted result SHA plus the actual artifact SHA/size with
  existing domain metadata validation.
- ACE compares metadata SHA and durable output identity.
- Relay compares exact result bytes using its existing atomic persistence.
- Audio/result publication and job success share the existing product
  transaction boundary or its proven file+transaction reconciliation path.
  Success is never claimed merely because an upload returned HTTP 200.

## 8. Retries, restart, and cancellation

- HTTP request timeouts: info/enroll/presence/fail 10 s; lease wait + 10 s;
  heartbeat 10 s. Completion/upload timeout follows the existing workload
  byte/time limits and product deadline, with heartbeats maintained
  throughout. Separate URL sessions/clients prevent a stalled completion from
  starving heartbeats.
- Offline retry backoff: 1, 2, 4, 8, 16, then 30 s cap, with jitter; bounded
  `Retry-After` is honored. Retries never acquire another lease while an
  acquired lease is unresolved.
- DL keeps its existing three-attempt, 5 s/30 s/2 m policy and
  parent-request deadline/cancellation. Parent cancellation cancels the relay
  child; never silently fall back to a cloud provider.
- AV universal ACE defaults to one inference attempt. Lease expiration after
  claim becomes `worker_lost` and invokes the existing uncertain-result
  reconciliation; never blindly rerender or issue a second nonce. Explicit
  owner retry uses the existing AV retry path after checking whether output
  was already committed.
- A network-ambiguous initial lease response may consume an attempt; no
  parallel retry race is allowed. The lost lease expires server-side; there
  are no exactly-once inference guarantees.
- Each speech service keeps per-connection/per-service durable ready/uploading
  spool and attempts idempotent upload recovery before new work on that
  binding. Apple speech and Chatterbox never block each other's recovery.
  Existing rendering recovery remains fenced by lease validity.
- Relay stays memory-only: no prompt/result journal. A crash is resolved by
  backend lease expiry/parent cancellation.
- ACE stores only safe owned-process identity and terminal metadata needed
  for recovery, never creative payloads, transfer URLs, or a second local
  queue. In-memory input is discarded at terminal completion.
- Drain disables future lease acquisition immediately and continues
  presence/heartbeats for currently owned jobs. It has no dependency on info,
  health, or presence success, so Quit Now always works.
- Stop/cancel closes local requests and terminates only verified app-owned
  children. The app cannot cancel computation inside independently managed
  OMLX after disconnect; UI states that limitation. No OMLX process is killed.

## 9. Enrollment grants

- The Mac's connection record persists `enrolledCapabilityIDs` and
  `serviceBindings`. The enrollment request contains exactly the selected
  intersection of locally configured service capabilities and
  `info.supported_capabilities`.
- Local selection begins empty; compatible rows have separate unchecked boxes,
  including independent Apple speech and Chatterbox rows. The exact
  URL/environment and selected service list are shown before Enroll.
- The returned credential authorizes no more than that stored enrollment set.
  All lease/presence operations enforce the subset on both sides; the Mac also
  checks a received lease against its local selection before handing payload
  bytes to a plugin. A server cannot reach an unshared service by returning
  its capability ID in a job; reject before execution and stop that malformed
  job path.
- Pause is an immediate local reduction in accepting work and needs no
  re-enrollment. Expanding or persistently changing the grant uses an explicit
  Edit shared services → resolve/drain old jobs → revoke/re-enroll flow,
  reusing the existing credential lifecycle. Old credentials/spool are
  retained until outstanding work is resolved. Discovery, relaunch, and server
  catalog changes never auto-expand a grant. Other connections are unaffected.

## 10. Conformance summary

Fixtures in `fixtures/` (manifest: `fixtures/manifest.json`) carry literal
bytes and expected HTTP outcomes for: enrollment expiry/reuse/concurrency and
wrong/revoked tokens; prefix-preserving URLs; malformed/version/unknown/
duplicate-key/payload-hash/byte-bound rejections; opaque UUID/ULID IDs;
atomic claims and `wait=0`/`wait=25`; one active capability lease; busy vs
offline presence; heartbeats during cold load/slow upload/outage; 90 s
expiry; old-attempt rejection; current-attempt cancellation; identical
completion after lost ACK; altered bytes; TTS extra/duplicate part; ACE
missing/wrong nonce upload; relay parent cancellation; two connections with
the same job ID; legacy client compatibility; log hygiene; independent
service enrollment; and stop/pause/quit scenarios. Consumer test suites map
each relevant case to exact HTTP and state assertions; schema validation alone
is not proof.
