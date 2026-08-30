#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd)
package_dir="$script_dir/app"
dist_dir="$repo_root/dist/macos"
version=$(tr -d '[:space:]' < "$script_dir/VERSION")
mode=development

for argument in "$@"; do
    case "$argument" in
        --development) mode=development ;;
        --release) mode=release ;;
        *)
            echo "usage: $0 [--development|--release]" >&2
            exit 64
            ;;
    esac
done

fail() {
    echo "build-app: $*" >&2
    exit 1
}

[[ "$(uname -m)" == "arm64" ]] || fail "an arm64 build Mac is required"
command -v swift >/dev/null || fail "Swift is required"
command -v codesign >/dev/null || fail "codesign is required"
command -v plutil >/dev/null || fail "plutil is required"
[[ "$version" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || fail "VERSION must be semantic"
[[ -z "$(git -C "$repo_root" status --porcelain --untracked-files=all)" ]] || fail "working tree is dirty"
application_revision=$(git -C "$repo_root" rev-parse HEAD)
build_number=$(git -C "$repo_root" rev-list --count HEAD)
[[ "$application_revision" =~ ^[0-9a-f]{40}$ ]] || fail "invalid application revision"

swift test --package-path "$package_dir"
swift build --package-path "$package_dir" -c release --arch arm64

if [[ "$mode" == release && ! -d "$script_dir/.build/runtime" ]]; then
    fail "release app requires deploy/node/macos/.build/runtime"
fi
codesign_identity=$(printenv CODESIGN_IDENTITY 2>/dev/null || true)
if [[ "$mode" == release && -z "$codesign_identity" ]]; then
    fail "set CODESIGN_IDENTITY for a release build"
fi

app_name="AudioVentura-ACE-Node-$version-arm64.app"
app_dir="$dist_dir/$app_name"
mkdir -p "$dist_dir"
rm -rf "$app_dir"
mkdir -p "$app_dir/Contents/MacOS" "$app_dir/Contents/Resources/receipt" "$app_dir/Contents/Frameworks"
cp "$package_dir/.build/arm64-apple-macosx/release/AudioVenturaACEWorker" "$app_dir/Contents/MacOS/AudioVenturaACEWorker"
chmod 755 "$app_dir/Contents/MacOS/AudioVenturaACEWorker"
ditto "$script_dir/Resources/Assets.xcassets" "$app_dir/Contents/Resources/Assets.xcassets"
cp "$script_dir/Resources/ACEWorkerMenu.entitlements" "$app_dir/Contents/Resources/ACEWorkerMenu.entitlements"
cp "$repo_root/deploy/node/uv.lock" "$app_dir/Contents/Resources/receipt/deploy-node-uv.lock"

if [[ -d "$script_dir/.build/runtime" ]]; then
    ditto "$script_dir/.build/runtime" "$app_dir/Contents/Resources/runtime"
fi

lock_sha256=$(shasum -a 256 "$repo_root/deploy/node/uv.lock" | awk '{print $1}')
runtime_receipt="sha256:$(python3 - "$application_revision" "$repo_root/deploy/node/uv.lock" <<'PY'
import hashlib
import sys
from pathlib import Path

revision, lock_name = sys.argv[1:]
digest = hashlib.sha256()
digest.update(b"audioventura-ace-node-runtime-receipt-v1\0")
digest.update(revision.encode("ascii"))
digest.update(b"\0")
digest.update(Path(lock_name).read_bytes())
print(digest.hexdigest())
PY
)"
if [[ -d "$script_dir/.build/runtime" ]]; then
    runtime_receipt_path="$script_dir/.build/runtime/receipt/runtime.json"
    [[ -f "$runtime_receipt_path" ]] || fail "runtime receipt is missing"
    python3 - "$runtime_receipt_path" "$application_revision" "$lock_sha256" "$runtime_receipt" <<'PY'
import json
import sys
from pathlib import Path

receipt_name, revision, lock_sha256, runtime_receipt = sys.argv[1:]
payload = json.loads(Path(receipt_name).read_text(encoding="utf-8"))
if {
    "schema_version",
    "application_commit",
    "deploy_node_lock_sha256",
    "runtime_receipt",
    "python_version",
    "architecture",
} != set(payload):
    raise SystemExit("runtime receipt fields do not match")
if (
    payload["schema_version"] != 1
    or payload["application_commit"] != revision
    or payload["deploy_node_lock_sha256"] != lock_sha256
    or payload["runtime_receipt"] != runtime_receipt
    or not isinstance(payload["python_version"], str)
    or not payload["python_version"].startswith("3.12")
    or payload["architecture"] != "arm64"
):
    raise SystemExit("runtime receipt does not match this app revision")
PY
fi
python_version=3.12.0
if [[ -x "$app_dir/Contents/Resources/runtime/venv/bin/python" ]]; then
    python_version=$(
        "$app_dir/Contents/Resources/runtime/venv/bin/python" -c 'import platform; print(platform.python_version())'
    )
fi
model_revision="88b8c7fa089446b53382c1040037492463430bed"
model_manifest_sha256="39a8180ef6852e2dfccb9088efa7231ca7de7e4c05c8d65e3ac5a3e7a5bfd0fc"
python3 - "$app_dir/Contents/Resources/receipt/release-manifest.json" <<PY
import json
import sys
from datetime import datetime, timezone

payload = {
    "schema_version": 1,
    "app_version": "$version",
    "bundle_id": "io.evren.audioventura.ace-node",
    "application_commit": "$application_revision",
    "deploy_node_lock_sha256": "$lock_sha256",
    "runtime_receipt": "$runtime_receipt",
    "ace_step_commit": "dce621408bee8c31b4fcf4811682eb9359e1bc94",
    "model_repo": "evrenesat/audioventura-ace-step-v0.1.8",
    "model_revision": "$model_revision",
    "model_manifest_sha256": "$model_manifest_sha256",
    "python_version": "$python_version",
    "minimum_macos": "14.0",
    "architecture": "arm64",
    "built_at_utc": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
}
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    json.dump(payload, handle, indent=2, sort_keys=True)
    handle.write("\n")
PY

sed -e "s/@VERSION@/$version/g" -e "s/@BUILD@/$build_number/g" \
    "$script_dir/Resources/Info.plist.in" > "$app_dir/Contents/Info.plist"
plutil -lint "$app_dir/Contents/Info.plist"

sign_target() {
    if [[ "$mode" == release ]]; then
        codesign --force --options runtime --timestamp --sign "$codesign_identity" "$1"
    else
        codesign --force --sign - "$1"
    fi
}

while IFS= read -r -d '' candidate; do
    case "$candidate" in
        *.dylib|*.so|*/MacOS/*|*/bin/*)
            sign_target "$candidate"
            ;;
    esac
done < <(find "$app_dir/Contents" \( -type f -o -type d -name '*.framework' \) -print0)

if [[ "$mode" == release ]]; then
    codesign --force --options runtime --timestamp --sign "$codesign_identity" \
        --entitlements "$script_dir/Resources/ACEWorkerMenu.entitlements" "$app_dir"
else
    codesign --force --sign - --entitlements "$script_dir/Resources/ACEWorkerMenu.entitlements" "$app_dir"
fi
codesign --verify --strict --verbose=2 "$app_dir"
codesign -d --arch arm64 "$app_dir" >/dev/null 2>&1

echo "app ready: $app_dir"
