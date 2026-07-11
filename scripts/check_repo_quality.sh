#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

echo "== bash syntax =="
while IFS= read -r script; do
  bash -n "$script"
done < <(find scripts -maxdepth 1 -type f -name '*.sh' -print | sort)

for script in ./*.sh; do
  [[ -f "$script" ]] || continue
  bash -n "$script"
done

echo "== python compile =="
python3 -m compileall -q scripts tests

echo "== unit tests =="
python3 -m unittest discover -s tests -v

if command -v rg >/dev/null 2>&1; then
  echo "== hardcoded local path audit =="
  if rg -n --hidden \
    --glob '!**/.git/**' \
    --glob '!chengdu/**' \
    --glob '!chengdu_benchmark/scenarios/**' \
    --glob '!scripts/check_repo_quality.sh' \
    --glob '!*.md' \
    --glob '!requirements.yaml' \
    '(/root/|/Users/|/opt/homebrew|/usr/share/sumo)' .; then
    echo "hardcoded local paths found" >&2
    exit 1
  fi
else
  echo "== hardcoded local path audit skipped: rg not installed =="
fi
