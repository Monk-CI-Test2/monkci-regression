#!/usr/bin/env bash
# Dispatch one workflow of this repo and wait for its run to finish.
# Usage: scripts/run-suite.sh <workflow-file> [-f key=value ...]
# Prints "<conclusion> <run-url>" and exits non-zero unless the conclusion is success.
set -euo pipefail
REPO="${GITHUB_REPOSITORY:-Monk-CI-Test2/monkci-regression}"
REF="${GITHUB_REF_NAME:-main}"
WF="$1"; shift
before=$(date -u +%FT%TZ)
gh workflow run "$WF" --repo "$REPO" --ref "$REF" "$@" >/dev/null
id=""
for attempt in $(seq 1 30); do
  id=$(gh run list --repo "$REPO" --workflow "$WF" --limit 5 --json databaseId,createdAt \
       --jq "[.[] | select(.createdAt >= \"$before\")] | sort_by(.createdAt) | last | .databaseId // empty")
  [[ -n "$id" ]] && break
  sleep 2
done
[[ -n "$id" ]] || { echo "could not find run for $WF" >&2; exit 1; }
gh run watch "$id" --repo "$REPO" --exit-status >/dev/null 2>&1 && concl=success || concl=$(gh run view "$id" --repo "$REPO" --json conclusion --jq .conclusion)
echo "$concl https://github.com/$REPO/actions/runs/$id"
[[ "$concl" == success ]]
