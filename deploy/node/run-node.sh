#!/usr/bin/env bash
set -euo pipefail

if (( $# != 1 )); then
    echo "usage: $0 /absolute/path/to/ace-node.env" >&2
    exit 64
fi

environment_file=$1
if [[ "$environment_file" != /* || ! -f "$environment_file" ]]; then
    echo "ACE Node environment file must be an existing absolute path" >&2
    exit 64
fi

# The environment file is deployment-owned and is never printed or committed.
set -a
# shellcheck disable=SC1090
source "$environment_file"
set +a

: "${ACE_NODE_CHECKOUT:?ACE_NODE_CHECKOUT is required}"
: "${ACE_NODE_VENV:?ACE_NODE_VENV is required}"
: "${ACE_NODE_TOKEN:?ACE_NODE_TOKEN is required}"
: "${ACE_NODE_APPLICATION_REVISION:?ACE_NODE_APPLICATION_REVISION is required}"
: "${ACE_NODE_RUNTIME_LOCK_PATH:?ACE_NODE_RUNTIME_LOCK_PATH is required}"
: "${ACE_NODE_SUPERVISOR_TOKEN:?ACE_NODE_SUPERVISOR_TOKEN is required}"

if [[ "$ACE_NODE_CHECKOUT" != /* || "$ACE_NODE_VENV" != /* || "$ACE_NODE_RUNTIME_LOCK_PATH" != /* ]]; then
    echo "ACE_NODE_CHECKOUT, ACE_NODE_VENV, and ACE_NODE_RUNTIME_LOCK_PATH must be absolute paths" >&2
    exit 64
fi
if [[ ! -d "$ACE_NODE_CHECKOUT/.git" || ! -x "$ACE_NODE_VENV/bin/python" ]]; then
    echo "ACE Node checkout or opt-in virtual environment is unavailable" >&2
    exit 78
fi
expected_runtime_lock="$ACE_NODE_CHECKOUT/deploy/node/uv.lock"
if [[ "$ACE_NODE_RUNTIME_LOCK_PATH" != "$expected_runtime_lock" || ! -f "$ACE_NODE_RUNTIME_LOCK_PATH" ]]; then
    echo "ACE_NODE_RUNTIME_LOCK_PATH must point to the clean checkout's deploy/node/uv.lock" >&2
    exit 78
fi

cd "$ACE_NODE_CHECKOUT"
if [[ -n "$(git status --porcelain --untracked-files=all)" ]]; then
    echo "ACE Node checkout is dirty; refusing to start" >&2
    exit 78
fi
actual_revision=$(git rev-parse HEAD)
if [[ "$actual_revision" != "$ACE_NODE_APPLICATION_REVISION" ]]; then
    echo "ACE Node checkout revision does not match ACE_NODE_APPLICATION_REVISION" >&2
    exit 78
fi

exec "$ACE_NODE_VENV/bin/python" -m ace_node
