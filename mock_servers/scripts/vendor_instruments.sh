#!/usr/bin/env bash
#
# Copy the real instrument packages from a marvel_digitalisation checkout into
# vendor/, so the mocks run with no checkout beside them. Run it again to pick
# up changes in the real servers, then run the tests.
#
# Usage:
#   scripts/vendor_instruments.sh /path/to/marvel_digitalisation
#
set -euo pipefail

src="${1:?usage: $0 /path/to/marvel_digitalisation}"
dst="$(cd "$(dirname "$0")/.." && pwd)/vendor"

[[ -f "$src/x3dsc/sila/server.py" ]] || { echo "not a marvel_digitalisation checkout: $src" >&2; exit 1; }

# Same excludes for every copy: caches, tests and packaging leftovers.
rs=(rsync -a --delete
    --exclude '__pycache__' --exclude '*.py[cod]' --exclude 'tests'
    --exclude '*.egg-info' --exclude 'build' --exclude '.pytest_cache')

for instrument in x3dsc sdt gpc ftir; do
    mkdir -p "$dst/$instrument"
    "${rs[@]}" "$src/$instrument/sila" "$dst/$instrument/"
    "${rs[@]}" "$src/$instrument/${instrument}_automation" "$dst/$instrument/"
done
# ftir_automation reads the well geometry from the file beside it.
cp "$src/ftir/hts_xt.json" "$dst/ftir/hts_xt.json"

mkdir -p "$dst/common"
"${rs[@]}" "$src/sila_common/sila_common" "$dst/common/"
"${rs[@]}" "$src/trios_common/trios_common" "$dst/common/"

commit="$(git -C "$src" rev-parse --short HEAD 2>/dev/null || echo unknown)"
printf 'marvel_digitalisation %s\n' "$commit" > "$dst/SOURCE"
echo "vendored from $src at $commit into $dst"
