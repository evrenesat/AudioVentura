#!/usr/bin/env bash
set -euo pipefail

script_dir=$(cd -- "$(dirname -- "$0")" && pwd)
repo_root=$(cd -- "$script_dir/../../.." && pwd)

if (( $# != 1 )); then
    echo "usage: $0 /absolute/path/to/AudioVentura-ACE-Node-*.dmg" >&2
    exit 64
fi
dmg=$1
[[ "$dmg" == /* && -f "$dmg" ]] || { echo "DMG must be an existing absolute path" >&2; exit 64; }

fail() {
    echo "verify-release: $*" >&2
    exit 1
}

command -v hdiutil >/dev/null || fail "hdiutil is required"
command -v codesign >/dev/null || fail "codesign is required"
command -v plutil >/dev/null || fail "plutil is required"
command -v file >/dev/null || fail "file is required"
command -v lipo >/dev/null || fail "lipo is required"
command -v realpath >/dev/null || fail "realpath is required"
command -v rg >/dev/null || fail "rg is required"
mkdir -p "$repo_root/.build"
hdiutil verify "$dmg"

mountpoint=$(mktemp -d "$repo_root/.build/verify-mount.XXXXXX")
copy_root=$(mktemp -d "$repo_root/.build/verify-copy.XXXXXX")
mounted=0
cleanup() {
    if (( mounted == 1 )); then
        hdiutil detach "$mountpoint" -force >/dev/null 2>&1 || true
    fi
    rm -rf "$mountpoint" "$copy_root"
}
trap cleanup EXIT

hdiutil attach "$dmg" -readonly -nobrowse -mountpoint "$mountpoint" >/dev/null
mounted=1
app=$(find "$mountpoint" -type d -name '*.app' -print -quit)
[[ -n "$app" ]] || fail "DMG does not contain an app bundle"
ditto "$app" "$copy_root/AudioVentura.app"
hdiutil detach "$mountpoint" >/dev/null
mounted=0
app="$copy_root/AudioVentura.app"

[[ -f "$app/Contents/MacOS/AudioVenturaACEWorker" ]] || fail "app executable is missing"
if find "$app" -type d -name '.git' -print -quit | grep -q .; then
    fail "app contains a Git directory"
fi
[[ -f "$app/Contents/Resources/receipt/deploy-node-uv.lock" ]] || fail "lock receipt is missing"
[[ -f "$app/Contents/Resources/receipt/release-manifest.json" ]] || fail "release manifest is missing"
plutil -lint "$app/Contents/Info.plist"
[[ "$(plutil -extract CFBundleIdentifier raw -o - "$app/Contents/Info.plist")" == "io.evren.audioventura.ace-node" ]] || fail "bundle id mismatch"
[[ "$(plutil -extract LSUIElement raw -o - "$app/Contents/Info.plist")" == "true" ]] || fail "LSUIElement is not enabled"
codesign --verify --strict --verbose=2 "$app"

while IFS= read -r -d '' candidate; do
    case "$candidate" in
        *.dylib|*.so|*/MacOS/*|*/bin/*)
            codesign --verify --strict --verbose=2 "$candidate"
            ;;
    esac
done < <(find "$app/Contents" -type f -print0)

if find "$app" -type f \( -name '*.safetensors' -o -name '*.ckpt' -o -name '.env' -o -name '*token*' \) -print -quit | grep -q .; then
    fail "app contains model or secret material"
fi
if rg -a -F -- "$repo_root" "$app" >/dev/null 2>&1; then
    fail "app contains an absolute build-checkout path"
fi
escaping_symlink=
while IFS= read -r -d '' link; do
    target=$(realpath "$link") || fail "could not resolve app symlink"
    case "$target" in
        "$app"/*) ;;
        *) escaping_symlink=1; break ;;
    esac
done < <(find "$app" -type l -print0)
[[ -z "$escaping_symlink" ]] || fail "app contains an escaping symlink"

while IFS= read -r -d '' candidate; do
    description=$(file -b "$candidate")
    case "$description" in
        *Mach-O*)
            archs=$(lipo -archs "$candidate" 2>/dev/null || true)
            [[ "$archs" == "arm64" ]] || fail "non-arm64 Mach-O: $candidate"
            ;;
    esac
done < <(find "$app/Contents" -type f -print0)

echo "release verification passed: $dmg"
