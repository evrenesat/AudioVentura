# fal.ai Model API operations

This runbook covers the optional managed fal.ai backends. It does not deploy a
Fal Serverless app, modify `runpod_worker/`, or replace the existing Runpod or
Salad runtime. Fal requests are paid external work; obtain an explicit test
budget before live acceptance.

## Catalog and configuration

The reviewed inventory is [the packaged catalog](../src/ace_service/providers/fal_music_catalog.json).
It contains endpoint-specific field mappings, output paths, native formats,
schema fingerprints, media-kind policy, and exclusions. Live discovery is
read-only:

```text
uv run python -m ace_service fal-catalog audit \
  --catalog src/ace_service/providers/fal_music_catalog.json
```

Run the audit with the deployment key before enabling a new endpoint. Resolve
every added, removed, inactive, unclassified, or schema-drifted result by
reviewing the endpoint contract and updating the catalog and fixture in one
change. The audit never edits or enables a backend.

The audit canonicalizes the live OpenAPI request and response shapes. Extra
live input properties, newly required fields, missing result metadata paths,
and incompatible result types are schema drift and must be reviewed before
deployment.

Set `FAL_KEY`, then add exact reviewed IDs to
`INFERENCE_ENABLED_BACKENDS`. Use `FAL_ALLOWED_MEDIA_KINDS=music` by default;
hybrid music/SFX entries remain omitted until an operator intentionally allows
`music_and_sfx`. Set mode defaults to IDs in the enabled list. The application
refuses missing credentials, invalid catalog paths, short retention, and
transfer lifetimes that cannot cover the inference deadline plus recovery time.

Disabling an ID removes it from new-job selectors only. The controller unions
the enabled list with backend IDs owned by nonterminal jobs, keeps those
adapters available for exact-request recovery, and requires `FAL_KEY` while
any retained Fal request is nonterminal.

Fal endpoint activity is cached per backend for a short interval. An endpoint
reported inactive or unreachable is omitted from new-job selectors and is
reported as unhealthy by readiness; existing jobs still retain their exact
backend for recovery.

## Request and result boundary

The Original selector exposes enabled reviewed text-to-music entries. Cover /
Remix exposes compatible audio transform, inpaint, and outpaint entries. A job
stores the exact backend and catalog snapshot before it enters the queue. One
backend owns every variation in that job; there is no automatic fallback.

ACE-Step audio transforms ask for the source style/description and optionally
the source lyrics; inpaint regions and outpaint extensions are checked again
against the measured source before a paid submission.

The controller submits to `https://queue.fal.run/<reviewed-endpoint-id>` with
`sync_mode=false`, `X-Fal-Store-IO: 0`, no-fallback, private lifecycle, and
bounded JSON headers. Audio bytes never appear in the request. Cover source
audio is provided only through a short-lived controller transfer URL.

When Fal reports success, the controller parses only the catalog-declared
result path, requires an HTTPS URL on an allowed Fal CDN hostname, exchanges
`FAL_KEY` for a short-lived CDN token, and streams the response to the private
output root. Redirects, unapproved hosts, wrong MIME types, empty/oversized
responses, symlinked targets, and failed hashes are rejected. The `.part` file
is mode `0600`, fsynced, and atomically renamed. A restart resumes the persisted
Fal request ID and an already materialized output is idempotent.

Fal returns native output formats. The UI records the native format and
returned seed/duration when available; it does not transcode or probe duration.
Unknown duration or non-MP3 output is not eligible for the existing MP3 cover
continuation path.

## Pricing and recovery

The selector uses a short-lived, read-only cache of
`GET https://api.fal.ai/v1/models/pricing`. It displays the exact UTC fetch
time and stale/unavailable state. A total is shown only when the catalog
declares a safe unit mapping; otherwise the UI says that total estimation is
unsafe. Fal attempts record compute evidence as
`provider_managed_pricing`; the existing GPU-hour formula is never reused.

If submission returns no request ID, treat the result as uncertain and do not
resubmit. Keep `FAL_KEY` and the catalog available until all nonterminal Fal
jobs reconcile or are failed at their durable deadline. If a backend is
disabled, historical jobs still require their exact configured backend; they
do not silently fall back to another endpoint.
