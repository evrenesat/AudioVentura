# AudioVentura Beta Subpath Compatibility AFlow Plan

## Summary

Make the accepted AudioVentura controller build work correctly when nginx publishes it below `https://player.evren.io/beta/`. This is a deploy-enabling slice only: preserve all existing root-path behavior, security controls, transfer routes, job behavior, and data formats. The following deployment run will consume the resulting committed revision.

## Objective

Support one configurable ASGI root path so every browser-visible link, form action, redirect, static asset, status response, audio source, and download link stays inside `/beta/` behind a prefix-stripping reverse proxy, while the default empty prefix remains byte-for-byte compatible at the HTTP contract level.

## Scope

### In scope

- Add a typed `ACE_SERVICE_ROOT_PATH` setting with a safe normalized empty-or-single-prefix contract.
- Pass that setting into the FastAPI application root path.
- Replace browser-facing hardcoded application paths with route-aware URL generation.
- Make redirect and JSON output URLs root-path-aware.
- Add focused tests for empty-prefix compatibility and `/beta` behavior.
- Update concise setup and architecture documentation for the new deployment setting.

### Out of scope

- nginx, systemd, Ansible, TLS, registry, Runpod, SFTP, Tailscale, credentials, deployment, or public mutation.
- Transfer-app paths and signed transfer capability semantics.
- Authentication, CSRF, rate limiting, job lifecycle, database schema, worker payloads, quality campaign, or model tuning.
- Any paid Runpod job.

## Constraints

1. Preserve the current route definitions. The application still registers `/`, `/create`, `/cover`, `/jobs`, `/media`, `/files`, `/healthz`, `/readyz`, and `/static`; the reverse proxy strips `/beta` before forwarding.
2. `ACE_SERVICE_ROOT_PATH` defaults to the empty string. Accept only empty or a normalized absolute path prefix such as `/beta`; reject `/`, trailing slashes, query/fragment content, dot segments, repeated slashes, backslashes, control characters, and full URLs.
3. Never derive the public prefix from untrusted request headers. nginx will set the deployment value explicitly.
4. Use named-route URL generation through the current request for HTML links, forms, redirects, static assets, media URLs, and download URLs. Do not hand-concatenate the prefix into templates.
5. Generated same-origin URLs may be path-only or absolute, but tests must prove they contain exactly one `/beta` prefix and never escape to the root application.
6. Preserve Basic Auth, CSRF cookies/tokens, CSP and security headers, anonymous denial, and signed transfer restrictions.
7. Keep the work limited to the accepted AudioVentura worktree and one checkpoint. Do not edit evreniops in this run.
8. Preserve unrelated dirty work. Do not commit secrets, generated databases, audio, environment files, or Runpod credentials.

## Git Tracking

- Plan Branch: `aflow-audioventura-beta-subpath-aflow-plan-20260809-013220`
- Pre-Handoff Base HEAD: `f13bcf6773f102168b595f6cee0ec2c68e16b9b2`
- Worktree at planning time: `/root/code/worktrees/aflow-ace-step-quality-cost-observability-aflow-plan-20260808-110317`
- Deployment consumer worktree: `/root/code/evreniops-audioventura-deploy`
- Deployment target path: `https://player.evren.io/beta/`

### [x] Checkpoint 0: Accepted product base

- Accepted source commit: `f13bcf6773f102168b595f6cee0ec2c68e16b9b2`.
- The prior quality and cost work through Checkpoint 4 is reviewed and committed on the recorded plan branch.
- This checkpoint records inherited accepted state only. It requires no implementation turn.

### [x] Checkpoint 2: Prefix-safe browser contract

### Files

Expected production files:

- `src/ace_service/config.py`
- `src/ace_service/app.py`
- `src/ace_service/web.py`
- `src/ace_service/templates/base.html`
- `src/ace_service/templates/dashboard.html`
- `src/ace_service/templates/original_form.html`
- `src/ace_service/templates/cover_form.html`
- `src/ace_service/templates/jobs.html`
- `src/ace_service/templates/job_detail.html`
- `src/ace_service/templates/error.html`

Expected tests and docs:

- `tests/test_config.py`
- `tests/test_web.py`
- `README.md`
- `ARCHITECTURE.md`
- `DEVLOG.md`

Discovery may show that fewer templates need edits once route-aware context is centralized. Do not widen scope beyond the files above without recording why in the plan Review Log.

### Implementation

- [x] Add `ServiceSettings.service_root_path`, exposed as `ACE_SERVICE_ROOT_PATH`, defaulting to `""`. Add a field validator that enforces the exact constraints above and returns the normalized value unchanged.
- [x] Pass `resolved_settings.service_root_path` as FastAPI's `root_path`. Do not change transfer-app construction.
- [x] Give every browser route an explicit stable name where needed. Use `request.url_for(...)` for redirects and for all template navigation/form targets.
- [x] Centralize route URL creation in the render/view boundary so templates receive named URLs rather than raw prefix concatenation. Dynamic job, confirmation, cancellation, media, download, and status URLs must use route parameters through named routes.
- [x] Change job/status view construction to receive the current request or a bounded URL factory. Ensure both initial HTML and later status JSON return prefix-safe media/download URLs.
- [x] Preserve default empty-root behavior: existing requests to `/`, `/create`, `/cover`, and `/jobs` still work and default redirects remain rooted at the public origin.
- [x] Add validation tests covering default empty, valid `/beta`, and representative invalid prefixes.
- [x] Add an application test with `ACE_SERVICE_ROOT_PATH=/beta` proving:
  - authenticated dashboard HTML links and static assets stay under `/beta`;
  - original and cover form actions stay under `/beta`;
  - successful POST redirects stay under `/beta/jobs/<id>`;
  - job detail, status polling, media, download, confirm, and cancel URLs stay under `/beta`;
  - no rendered `href`, `src`, form `action`, redirect location, or JSON media/download field points to the unprefixed browser routes;
  - auth, CSRF, and security headers still work.
- [x] Update README with the environment variable and reverse-proxy contract. Update ARCHITECTURE with the prefix boundary. Add one compact DEVLOG entry.
- [x] Run verification, inspect the diff for scope, and commit only after the checkpoint reviewer approves.

### Verification

Run:

```bash
uv run pytest -q tests/test_config.py tests/test_web.py
uv run pytest -q
uv run python -m compileall -q src tests
git diff --check
git status --short
git diff --name-only
git diff --stat
```

Expected:

- Both focused and full suites pass with no new failure.
- `/beta` tests prove all browser navigation and generated asset/media/download/status URLs remain below the prefix.
- Default empty-prefix tests remain compatible.
- No transfer, deployment, secret, campaign, or worker behavior changes.
- Only the listed files change.

## Review and Commit Rules

1. A reviewer must inspect the actual diff and focused test evidence.
2. A material finding produces one focused repair plan for Checkpoint 2; do not start a broader redesign.
3. On approval, create one commit using the repository convention.
4. Record the approved commit SHA and verification in this plan.
5. Stop after Checkpoint 2. The evreniops deployment is a separate AFlow run so each repository retains clean Git ownership.

## Review Log

- Checkpoint 2 execution uses the repository's existing canonical
  `docs/ARCHITECTURE.md`; the plan's expected root-level `ARCHITECTURE.md`
  entry was a path-casing/location mismatch, not an additional document.
- Execution verification: focused `tests/test_config.py tests/test_web.py` =
  37 passed; full suite = 365 passed; compileall and `git diff --check` passed.
  Five existing framework deprecation warnings remain. Scoped changes are
  intentionally uncommitted for checkpoint review.
- Checkpoint 2 approval (`cp2 v01`): independent worktree-fallback review found
  no material defects. Reviewer verification repeated the 37-test focused and
  365-test full suites, compileall, `git diff --check`, branch/base reachability,
  and scoped-diff checks successfully. The exact approval commit is the commit
  carrying the `cp2 v01` label in Git history; no deployment or external
  service action was performed.

## Done When

- [x] `ACE_SERVICE_ROOT_PATH=/beta` produces a complete prefix-safe browser contract.
- [x] Default empty-prefix behavior remains compatible.
- [x] Focused and full verification pass.
- [x] The checkpoint is reviewed and committed.
- [x] The final commit SHA is ready for the isolated evreniops deployment plan.
