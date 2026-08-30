#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd)
runtime_project="$repo_root/deploy/node"
build_root="$repo_root/deploy/node/macos/.build"
stage="$build_root/runtime-staging"
runtime_out="$build_root/runtime"
min_free_bytes=$((30 * 1024 * 1024 * 1024))

relocatable=0
emit_package=1
for argument in "$@"; do
    case "$argument" in
        --relocatable) relocatable=1 ;;
        --no-emit-package) emit_package=0 ;;
        *)
            echo "usage: $0 [--relocatable] [--no-emit-package]" >&2
            exit 64
            ;;
    esac
done

fail() {
    echo "build-runtime: $*" >&2
    exit 1
}

[[ "$(uname -m)" == "arm64" ]] || fail "an arm64 build Mac is required"
command -v uv >/dev/null || fail "uv is required"
command -v xcrun >/dev/null || fail "Xcode command-line tools are required"
command -v shasum >/dev/null || fail "shasum is required"
command -v python3 >/dev/null || fail "python3 is required for receipt generation"
command -v rg >/dev/null || fail "rg is required"
uv --version
xcodebuild -version
sw_vers -productVersion
venv_help=$(uv venv --help)
export_help=$(uv export --help)
lock_help=$(uv lock --help)
[[ "$venv_help" == *"--relocatable"* ]] || fail "installed uv lacks uv venv --relocatable"
[[ "$export_help" == *"--no-emit-package"* ]] || fail "installed uv lacks uv export --no-emit-package"
[[ "$lock_help" == *"--check"* ]] || fail "installed uv lacks uv lock --check"

available_kib=$(df -P -k "$repo_root" | awk 'NR == 2 { print $4 }')
[[ "$available_kib" =~ ^[0-9]+$ ]] || fail "could not measure free storage"
available_bytes=$((available_kib * 1024))
(( available_bytes >= min_free_bytes )) || fail "at least 30 GiB free storage is required"

[[ -f "$runtime_project/pyproject.toml" ]] || fail "deployment project is missing"
[[ -f "$runtime_project/uv.lock" ]] || fail "deployment lockfile is missing"
rg -q -F -- "dce621408bee8c31b4fcf4811682eb9359e1bc94" \
    "$runtime_project/pyproject.toml" "$runtime_project/uv.lock" \
    || fail "deployment lock does not pin the reviewed ACE-Step commit"
git -C "$repo_root" diff --quiet || fail "working tree is dirty"
git -C "$repo_root" diff --cached --quiet || fail "staged changes are present"
application_revision=$(git -C "$repo_root" rev-parse HEAD)
[[ "$application_revision" =~ ^[0-9a-f]{40}$ ]] || fail "invalid application revision"
uv --project "$runtime_project" lock --check

mkdir -p "$build_root"
rm -rf "$stage"
mkdir -p "$stage/runtime/python/bin" "$stage/runtime/venv" "$stage/runtime/receipt"

uv python install 3.12
managed_python=$(uv python find 3.12)
[[ -x "$managed_python" ]] || fail "uv-managed CPython 3.12 was not found"
managed_root=$(cd -- "$(dirname -- "$managed_python")/.." && pwd)
ditto "$managed_root" "$stage/runtime/python"
runtime_python="$stage/runtime/python/bin/$(basename "$managed_python")"
[[ -x "$runtime_python" ]] || fail "staged CPython interpreter is missing"

if (( relocatable == 1 )); then
    uv venv --relocatable "$stage/runtime/venv" --python "$runtime_python"
else
    uv venv "$stage/runtime/venv" --python "$runtime_python"
fi
venv_python="$stage/runtime/venv/bin/python"
[[ -x "$venv_python" ]] || fail "staged virtual environment is missing"

export UV_PROJECT_ENVIRONMENT="$stage/runtime/venv"
uv export \
    --project "$runtime_project" \
    --frozen \
    --no-dev \
    --no-emit-project \
    --no-emit-package ace-service \
    > "$stage/runtime/receipt/node-requirements.txt"
uv pip install --python "$venv_python" --requirements "$stage/runtime/receipt/node-requirements.txt"

wheel_dir="$build_root/wheel"
rm -rf "$wheel_dir"
mkdir -p "$wheel_dir"
uv build --wheel --out-dir "$wheel_dir" "$repo_root"
wheel=$(find "$wheel_dir" -type f -name '*.whl' -print -quit)
[[ -n "$wheel" ]] || fail "repository wheel was not built"
uv pip install --python "$venv_python" --no-deps "$wheel"

python3 - "$application_revision" "$runtime_project/uv.lock" "$stage/runtime/receipt/runtime.json" "$runtime_python" <<'PY'
import hashlib
import json
import platform
import subprocess
import sys
from pathlib import Path

revision, lock_name, output_name, python_name = sys.argv[1:]
lock = Path(lock_name)
lock_bytes = lock.read_bytes()
digest = hashlib.sha256()
digest.update(b"audioventura-ace-node-runtime-receipt-v1\0")
digest.update(revision.encode("ascii"))
digest.update(b"\0")
digest.update(lock_bytes)
runtime_receipt = "sha256:" + digest.hexdigest()
python_version = subprocess.check_output(
    [python_name, "-c", "import platform; print(platform.python_version())"],
    text=True,
).strip()
payload = {
    "schema_version": 1,
    "application_commit": revision,
    "deploy_node_lock_sha256": hashlib.sha256(lock_bytes).hexdigest(),
    "runtime_receipt": runtime_receipt,
    "python_version": python_version,
    "architecture": platform.machine(),
}
if payload["architecture"] != "arm64" or not python_version.startswith("3.12"):
    raise SystemExit("staged runtime identity is not arm64 CPython 3.12")
Path(output_name).write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
PY
cp "$runtime_project/uv.lock" "$stage/runtime/receipt/deploy-node-uv.lock"

"$venv_python" - <<'PY'
import importlib
import platform
import torch

if platform.machine() != "arm64":
    raise SystemExit("runtime is not arm64")
if not torch.backends.mps.is_available():
    raise SystemExit("MPS is unavailable")
for module in ("ace_node", "runpod_worker", "mlx", "mlx_lm", "acestep"):
    importlib.import_module(module)
PY

find "$stage/runtime" -type d -name '__pycache__' -prune -exec rm -rf {} +
find "$stage/runtime" -type f \( -name '*.pyc' -o -name '*.pyo' -o -name '*.safetensors' -o -name '*.ckpt' -o -name '*.bin' \) -delete
find "$stage/runtime" -type d \( -name '.git' -o -name 'tests' -o -name '__pycache__' \) -prune -exec rm -rf {} +
if find "$stage/runtime" -type f -name '.env' -print -quit | grep -q .; then
    fail "runtime contains an environment file"
fi
if rg -a -F -- "$repo_root" "$stage/runtime" >/dev/null 2>&1; then
    fail "runtime contains an absolute build-checkout path"
fi
escaping_symlink=
while IFS= read -r -d '' link; do
    target=$(realpath "$link") || fail "could not resolve runtime symlink"
    case "$target" in
        "$stage/runtime"/*) ;;
        *) escaping_symlink=1; break ;;
    esac
done < <(find "$stage/runtime" -type l -print0)
[[ -z "$escaping_symlink" ]] || fail "runtime contains an escaping symlink"

rm -rf "$runtime_out"
mv "$stage/runtime" "$runtime_out"
relocation_probe="$build_root/runtime relocation probe"
rm -rf "$relocation_probe"
restore_runtime() {
    if [[ -d "$relocation_probe" && ! -e "$runtime_out" ]]; then
        mv "$relocation_probe" "$runtime_out"
    fi
}
trap restore_runtime EXIT
mv "$runtime_out" "$relocation_probe"
"$relocation_probe/venv/bin/python" - <<'PY'
import importlib

for module in ("ace_node", "runpod_worker", "torch", "mlx", "mlx_lm", "acestep"):
    importlib.import_module(module)
PY
mv "$relocation_probe" "$runtime_out"
trap - EXIT
rm -rf "$stage" "$wheel_dir"
if (( emit_package == 1 )); then
    echo "runtime ready: $runtime_out"
fi
