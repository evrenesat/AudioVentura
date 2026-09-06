# ailocals facade

This package is the controller side of the shared ailocals.v1 universal
worker protocol: enrollment, presence, atomic claims with lease-time transfer
issuance, heartbeats, completion, and failure. `protocol.py` is a pure
transport boundary; `service.py` owns durable rows; `routes.py` is the only
HTTP surface. `providers/ailocals.py` adapts rows to the inference provider
contract.

Rules:

- Queue rows hold bounded safe identity and state only. Never store creative
  payloads, transfer URLs, audio bytes, or credentials in ailocals tables or
  logs.
- Transfer capabilities issued at claim time carry ailocals linkage and are
  fenced by `transfers._ensure_ailocals_authority`; late or superseded
  uploads are rejected.
- Never broaden an enrollment grant after the fact; the owner re-enrolls.
- One claimed ACE attempt per submission: no automatic rerender after an
  ambiguous lease. Reconciliation uses the existing transfer evidence.
- The frozen wire contract lives in `contracts/ailocals-v1/`; any
  incompatible wire change requires ailocals.v2, never a silent v1 change.
