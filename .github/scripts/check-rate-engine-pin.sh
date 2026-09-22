#!/usr/bin/env bash
# The rate-engine pin must name a commit on rate-engine's MAIN branch.
#
# It once named a commit that existed only on a feature branch. Squash-merging
# that branch and deleting it would have made the pin unresolvable and broken
# `pip install` of this package -- silently, because nothing here installs from
# the pin on every run.
#
# A comment saying "re-pin before merging" is not a check. This is.
set -euo pipefail

PIN=$(grep -oE 'openparking-rate-engine @ git\+https://github\.com/openparking-ai/rate-engine@[0-9a-f]{40}' pyproject.toml \
      | grep -oE '[0-9a-f]{40}$' || true)

if [ -z "$PIN" ]; then
  echo "no 40-character rate-engine commit pin found in pyproject.toml"
  echo "the dependency must be pinned to an exact commit, not a branch or a tag"
  exit 1
fi

echo "pinned rate-engine commit: $PIN"

TMP=$(mktemp -d)
git clone --quiet --filter=blob:none --no-checkout \
    https://github.com/openparking-ai/rate-engine.git "$TMP/rate-engine"

if ! git -C "$TMP/rate-engine" cat-file -e "$PIN^{commit}" 2>/dev/null; then
  echo "FAIL: $PIN does not exist in openparking-ai/rate-engine at all."
  exit 1
fi

if git -C "$TMP/rate-engine" merge-base --is-ancestor "$PIN" origin/main 2>/dev/null; then
  echo "OK: the pin is an ancestor of rate-engine main."
  exit 0
fi

echo "FAIL: $PIN is NOT on rate-engine main. It is reachable only from:"
git -C "$TMP/rate-engine" branch -r --contains "$PIN" | sed 's/^/    /'
cat <<'MSG'

A pin to a commit that lives only on a feature branch stops resolving the
moment that branch is deleted, and `pip install` of this package breaks.

Merge order:
    1. merge rate-engine's pull request
    2. re-pin pyproject.toml to the resulting commit on rate-engine main
    3. merge this one
MSG
exit 1
