# ACE Node deployment module

This directory owns the launcher and service templates for one manually
managed, persistent ACE Node. It is not a GPU provisioner and must not contain
credentials, model files, generated audio, or provider-management code.

The launcher receives exactly one absolute environment-file path, verifies a
clean checkout and an exact `ACE_NODE_APPLICATION_REVISION`, checks that the
runtime receipt lock is the tracked `deploy/node/uv.lock`, then executes the
pre-created opt-in node virtual environment. Keep the node bound to loopback,
a private interface, or the operator's exact Tailscale `.ts.net` address.
The node bearer token and supervisor drain token are separate deployment
secrets; never reuse or print either one.

The deployment project owns the heavyweight ACE-Step, CUDA, and Apple Silicon
MLX dependency graph. Keep it in `deploy/node/pyproject.toml` and
`deploy/node/uv.lock`; do not add those dependencies to the controller's root
project or lockfile.

Run the deployment checks from the repository root:

```text
shellcheck deploy/node/run-node.sh
git diff --check
```

Do not install, bootstrap, stop, or otherwise control a service during code
changes. Operators substitute the placeholders documented in
`docs/ACE-NODE.md` on the target host.
