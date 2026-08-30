#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd)
dist_dir="$repo_root/dist/macos"

if (( $# != 1 )); then
    echo "usage: $0 /absolute/path/to/release.dmg" >&2
    exit 64
fi
dmg=$1
[[ "$dmg" == /* && -f "$dmg" ]] || { echo "DMG must be an existing absolute path" >&2; exit 64; }
[[ "$dmg" != *-dev.dmg ]] || { echo "development DMGs are not notarized" >&2; exit 64; }

codesign_identity=$(printenv CODESIGN_IDENTITY 2>/dev/null || true)
notary_profile=$(printenv NOTARY_PROFILE 2>/dev/null || true)
[[ -n "$codesign_identity" ]] || { echo "CODESIGN_IDENTITY is required" >&2; exit 64; }
[[ -n "$notary_profile" ]] || { echo "NOTARY_PROFILE is required" >&2; exit 64; }
command -v xcrun >/dev/null || { echo "xcrun is required" >&2; exit 1; }
command -v codesign >/dev/null || { echo "codesign is required" >&2; exit 1; }
command -v jq >/dev/null || { echo "jq is required" >&2; exit 1; }

codesign --verify --deep --strict --verbose=2 "$dmg"
mkdir -p "$dist_dir"
log_path="$dist_dir/$(basename "$dmg").notary.json"
xcrun notarytool submit "$dmg" --keychain-profile "$notary_profile" --wait --output-format json > "$log_path"
submission_id=$(jq -r '.id // empty' "$log_path")
[[ -n "$submission_id" ]] || { echo "notarytool returned no submission id" >&2; exit 1; }
xcrun notarytool log "$submission_id" --keychain-profile "$notary_profile" \
    "$dist_dir/$(basename "$dmg").notary-detail.json"
xcrun stapler staple "$dmg"
xcrun stapler validate "$dmg"
spctl --assess --type open --context context:primary-signature --verbose=4 "$dmg"
shasum -a 256 "$dmg" > "$dmg.sha256"
release_json="${dmg%.dmg}.release.json"
if [[ -f "$release_json" ]]; then
    python3 - "$release_json" "$dmg" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

release_name, dmg_name = sys.argv[1:]
payload = json.loads(Path(release_name).read_text(encoding="utf-8"))
payload["dmg_sha256"] = hashlib.sha256(Path(dmg_name).read_bytes()).hexdigest()
Path(release_name).write_text(
    json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
)
PY
fi
echo "notarized DMG: $dmg"
