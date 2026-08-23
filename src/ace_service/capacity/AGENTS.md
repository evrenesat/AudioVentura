# Capacity management boundary

This package owns controller-side leases and provider-specific capacity
mutations. It must not submit inference jobs, expose provider bodies, or add
deployment methods to the inference provider protocol. Every manager is
bounded, idempotent, and safe to call again after a lost response.
