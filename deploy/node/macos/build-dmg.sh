#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd)
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
    echo "build-dmg: $*" >&2
    exit 1
}

command -v hdiutil >/dev/null || fail "hdiutil is required"
command -v codesign >/dev/null || fail "codesign is required"
available_kib=$(df -P -k "$repo_root" | awk 'NR == 2 { print $4 }')
[[ "$available_kib" =~ ^[0-9]+$ ]] || fail "could not measure free storage"
(( available_kib * 1024 >= 10 * 1024 * 1024 * 1024 )) || fail "at least 10 GiB free storage is required"

app_dir="$dist_dir/AudioVentura-ACE-Node-$version-arm64.app"
[[ -d "$app_dir" ]] || fail "app is missing; run build-app.sh first"
if [[ "$mode" == release ]]; then
    sign_identity=$(printenv CODESIGN_IDENTITY 2>/dev/null || true)
    [[ -n "$sign_identity" ]] || fail "set CODESIGN_IDENTITY for a release build"
    dmg_name="AudioVentura-ACE-Node-$version-arm64.dmg"
else
    sign_identity=-
    dmg_name="AudioVentura-ACE-Node-$version-arm64-dev.dmg"
fi

staging="$repo_root/.build/macos-dmg-staging"
rm -rf "$staging"
mkdir -p "$staging"
ditto "$app_dir" "$staging/AudioVentura ACE Node.app"
ln -s /Applications "$staging/Applications"
output="$dist_dir/$dmg_name"
rm -f "$output"
hdiutil create \
    -volname "AudioVentura ACE Node $version" \
    -srcfolder "$staging" \
    -format UDZO \
    -imagekey zlib-level=9 \
    -ov "$output"
if [[ "$mode" == release ]]; then
    codesign --force --timestamp --sign "$sign_identity" "$output"
else
    codesign --force --sign "$sign_identity" "$output"
fi
codesign --verify --strict --verbose=2 "$output"
hdiutil verify "$output"
size_bytes=$(stat -f %z "$output")
(( size_bytes <= 10 * 1024 * 1024 * 1024 )) || fail "DMG exceeds 10 GiB"
if [[ "$mode" == release ]]; then
    release_json="${output%.dmg}.release.json"
    python3 - "$app_dir/Contents/Resources/receipt/release-manifest.json" "$release_json" "$output" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

manifest_name, output_name, dmg_name = sys.argv[1:]
payload = json.loads(Path(manifest_name).read_text(encoding="utf-8"))
payload["dmg_filename"] = Path(dmg_name).name
payload["dmg_sha256"] = hashlib.sha256(Path(dmg_name).read_bytes()).hexdigest()
Path(output_name).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
fi
rm -rf "$staging"
echo "dmg ready: $output"
