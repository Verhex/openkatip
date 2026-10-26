#!/usr/bin/env bash
# Export a reviewed commit without creating a repository or remote.
set -euo pipefail

fail() { printf 'Export refused: %s\n' "$*" >&2; exit 1; }
script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
repo=$(git -C "$script_dir" rev-parse --show-toplevel)
cd -- "$repo"
[[ $# -le 1 ]] || fail 'usage: export-openkatip.sh [target-dir]'
[[ -z $(git status --porcelain=v1 --untracked-files=all) ]] || fail 'source tree is dirty (including untracked files)'

denylist=${OPENKATIP_DENYLIST:-"$script_dir/openkatip-denylist.txt"}
[[ -f "$denylist" ]] || fail 'denylist is missing; set OPENKATIP_DENYLIST to a reviewed list'
# Use a temporary filtered list so comments/empty lines cannot match everything.
patterns=$(mktemp)
trap 'rm -f -- "$patterns"' EXIT
sed -e 's/\r$//' -e '/^[[:space:]]*#/d' -e '/^[[:space:]]*$/d' "$denylist" > "$patterns"
[[ -s "$patterns" ]] || fail 'denylist has no active patterns'

target=${1:-../openkatip}
[[ ! -L "$target" ]] || fail 'target must not be a symlink'
# Resolve parents before creating anything, including paths containing '..'.
target=$(realpath -m -- "$target")
source_dir=$(pwd -P)
[[ "$target" != "$source_dir" && "$target" != "$source_dir/"* ]] || fail 'target must be outside the source checkout'
if [[ -e "$target" ]]; then
    [[ -d "$target" ]] || fail 'target is not a directory'
    [[ -z $(find "$target" -mindepth 1 -maxdepth 1 -print -quit) ]] || fail 'target directory is not empty'
fi
mkdir -p -- "$target"
target=$(cd -- "$target" && pwd -P)
[[ "$target" != "$source_dir" && "$target" != "$source_dir/"* ]] || fail 'target must be outside the source checkout'

git archive --format=tar HEAD | tar -x -C "$target"

# Inspect every archived path against effective source attributes. This handles
# files below excluded directories without maintaining a second path inventory.
while IFS= read -r -d '' path; do
    relative=${path#"$target/"}
    [[ ! -d "$path" ]] || relative="$relative/"
    attribute=$(git check-attr export-ignore -- "$relative")
    [[ "$attribute" != *': set' ]] || fail "excluded path exists in export: $relative"
done < <(find "$target" -mindepth 1 -print0)

# Literal mandatory paths also guard against accidentally removing an attribute.
for path in verhex-ops deploy/verhex railway.json docker/railway.Dockerfile \
    docker/entrypoint-railway.sh docs/railway-kurulum.md docs/test-skorlari.md \
    docs/maliyet.md DESIGN.md security_best_practices_report.md .agents \
    docs/internal plan.md engines/ops/M1_REPORT.md engines/ops/M2_M3_REPORT.md \
    engines/ops/M4_M5_REPORT.md engines/ops/RUNBOOK.md scripts/openkatip-denylist.txt \
    OPENKATIP_RELEASE_REPORT.md tests/test_verhex_deploy.py; do
    [[ ! -e "$target/$path" && ! -L "$target/$path" ]] || fail "private path exists: $path"
done

# Scan binary files too (-a) and report names only, avoiding sensitive line output.
scan_status=0
grep -r -a -i -E -l -f "$patterns" -- "$target" || scan_status=$?
[[ $scan_status -ne 0 ]] || fail 'organization-specific strings found in the listed files'
[[ $scan_status -eq 1 ]] || fail "denylist scan failed (grep status $scan_status)"
printf 'Export verified: %s\nCommit: %s\nFiles: %s\nExcluded paths and denylist: passed\n' \
    "$target" "$(git rev-parse HEAD)" "$(find "$target" -type f | wc -l | tr -d ' ')"
