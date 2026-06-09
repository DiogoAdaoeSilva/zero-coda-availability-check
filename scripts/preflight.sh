#!/usr/bin/env sh
set -eu

cd "$(dirname "$0")/.."

test -f zc_monitor.py
test -f test_zc_monitor.py
test -f .github/workflows/zerocoda-monitor.yml

python3 -m unittest -v

if git status --short --ignored | grep -E '^\?\? .zc-monitor|^\?\? __pycache__/' >/dev/null; then
  echo "Generated state/cache files are not ignored correctly." >&2
  exit 1
fi

echo "Preflight OK: monitor, tests, workflow, and ignore rules are ready."
