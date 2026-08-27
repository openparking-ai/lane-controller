#!/usr/bin/env bash
# The vehicle-id pin must name a commit on vehicle-id's MAIN branch.
#
# It once named a commit that existed only on a feature branch. Squash-merging
# that branch and deleting it would have made the pin unresolvable and broken
# `pip install` of this package -- silently, because nothing here installs from
# the pin on every run.
#
# A comment saying "re-pin before merging" is not a check. This is.
set -euo pipefail

PIN=$(grep -oE 'openparking-vehicle-id @ git\+https://github\.com/openparking-ai/vehicle-id@[0-9a-f]{40}' pyproject.toml \
      | grep -oE '[0-9a-f]{40}$' || true)

if [ -z "$PIN" ]; then
  echo "no 40-character vehicle-id commit pin found in pyproject.toml"
  echo "the dependency must be pinned to an exact commit, not a branch or a tag"
  exit 1
fi

echo "pinned vehicle-id commit: $PIN"

TMP=$(mktemp -d)
git clone --quiet --filter=blob:none --no-checkout \
    https://github.com/openparking-ai/vehicle-id.git "$TMP/vehicle-id"

if ! git -C "$TMP/vehicle-id" cat-file -e "$PIN^{commit}" 2>/dev/null; then
  echo "FAIL: $PIN does not exist in openparking-ai/vehicle-id at all."
  exit 1
fi

if git -C "$TMP/vehicle-id" merge-base --is-ancestor "$PIN" origin/main 2>/dev/null; then
  echo "OK: the pin is an ancestor of vehicle-id main."
  exit 0
fi

echo "FAIL: $PIN is NOT on vehicle-id main. It is reachable only from:"
git -C "$TMP/vehicle-id" branch -r --contains "$PIN" | sed 's/^/    /'
cat <<'MSG'

A pin to a commit that lives only on a feature branch stops resolving the
moment that branch is deleted, and `pip install` of this package breaks.

Merge order:
    1. merge vehicle-id's pull request
    2. re-pin pyproject.toml to the resulting commit on vehicle-id main
    3. merge this one
MSG
exit 1
