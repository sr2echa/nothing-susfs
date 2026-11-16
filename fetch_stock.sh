#!/usr/bin/env bash
# Fetch stock boot-chain images (boot, init_boot, vendor_boot, dtbo, vbmeta...) for Nothing Phone 3
# from spike0en/nothing_archive, verify sha256, extract to stock/<tag>/.
# Usage: ./fetch_stock.sh [TAG]   (default: Metroid_C5.0-260924-2115)
set -euo pipefail

TAG="${1:-Metroid_C5.0-260924-2115}"
REPO="spike0en/nothing_archive"
OUT="$(cd "$(dirname "$0")" && pwd)/stock/$TAG"
SEVENZ="$(command -v 7zz || command -v 7z || true)"
[ -n "$SEVENZ" ] || { echo "need 7-Zip: brew install sevenzip"; exit 1; }

mkdir -p "$OUT/dl"
api="https://api.github.com/repos/$REPO/releases/tags/$TAG"
assets="$(curl -fsSL "$api" | jq -r '.assets[] | select(.name|test("image-boot\\.7z$|hash\\.sha256$")) | "\(.name) \(.digest) \(.browser_download_url)"')"
[ -n "$assets" ] || { echo "no assets found for $TAG"; exit 1; }

while read -r name digest url; do
  f="$OUT/dl/$name"
  [ -f "$f" ] || curl -fsSL --retry 3 -o "$f" "$url"
  echo "${digest#sha256:}  $f" | shasum -a 256 -c -   # GitHub-recorded digest
done <<< "$assets"

"$SEVENZ" x -y -o"$OUT" "$OUT"/dl/*image-boot.7z >/dev/null

# Cross-check extracted images against the release's hash list (lines: "<sha256>  <file>").
cd "$OUT"
grep -E '\.img$' dl/*hash.sha256 | while read -r sum file; do
  base="$(basename "$file")"
  [ -f "$base" ] && echo "$sum  $base" | shasum -a 256 -c -
done
ls -la "$OUT"
