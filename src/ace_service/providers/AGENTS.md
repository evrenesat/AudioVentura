# Inference provider boundary

This package translates provider APIs into the finite AudioVentura inference
contract. Keep deployment management, credentials, signed URL contents, audio
bytes, prompts, and lyrics out of exceptions and logs. Provider adapters
classify remote behavior; durable deadlines, retries, transitions, and cleanup
remain owned by the controller worker.

Never retry submission here. Validate identifiers and bounded JSON before
returning it. New providers must declare capabilities explicitly and must not
be selected as a fallback for a persisted job.
