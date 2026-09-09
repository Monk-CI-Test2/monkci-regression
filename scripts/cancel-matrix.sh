#!/usr/bin/env bash
# Dispatches N regression-target runs with a deterministic mix of scenarios,
# cancels the ones that are meant to be cancelled (before or after a runner is
# assigned), waits for everything to finish, and grades the outcome. Ends with
# one clean probe that must start within the SLO: proof that the pool is healthy
# after the chaos and no demand leaked.
#
# Requires: gh (authenticated with actions:write on this repo), jq.
# Env: RUNNER_LABEL COUNT HOLD_SECONDS CANCEL_QUEUED_PCT CANCEL_RUNNING_PCT FAIL_PCT
#      QUEUE_SLO_SECONDS TIMEOUT_MINUTES SEED
set -euo pipefail

REPO="${GITHUB_REPOSITORY:?}"
SUITE="cm-${SEED:-$RANDOM}-$(date -u +%H%M%S)"
RUNNER_LABEL="${RUNNER_LABEL:-monkci-ubuntu-24.04-4}"
COUNT="${COUNT:-8}"
HOLD_SECONDS="${HOLD_SECONDS:-60}"
CANCEL_QUEUED_PCT="${CANCEL_QUEUED_PCT:-25}"
CANCEL_RUNNING_PCT="${CANCEL_RUNNING_PCT:-25}"
FAIL_PCT="${FAIL_PCT:-12}"
QUEUE_SLO_SECONDS="${QUEUE_SLO_SECONDS:-600}"
TIMEOUT_MINUTES="${TIMEOUT_MINUTES:-25}"
SUMMARY="${GITHUB_STEP_SUMMARY:-/dev/stdout}"
WORKFLOW="regression-target.yml"
REF="${GITHUB_REF_NAME:-main}"

log() { printf '%s %s\n' "$(date -u +%T)" "$*"; }

# ---- plan --------------------------------------------------------------------
# Deterministic scenario per case from the seed, so a run can be repeated exactly.
declare -a CASE SCEN
for ((i=1; i<=COUNT; i++)); do
  r=$(( ( ${SEED:-1} * 7919 + i * 104729 ) % 100 ))
  if   (( r < CANCEL_QUEUED_PCT )); then s=cancel_queued
  elif (( r < CANCEL_QUEUED_PCT + CANCEL_RUNNING_PCT )); then s=cancel_running
  elif (( r < CANCEL_QUEUED_PCT + CANCEL_RUNNING_PCT + FAIL_PCT )); then s=fail
  else s=success; fi
  CASE[i]="c$(printf '%02d' "$i")"; SCEN[i]="$s"
done
log "suite=$SUITE runner=$RUNNER_LABEL count=$COUNT plan: $(for ((i=1;i<=COUNT;i++)); do printf '%s=%s ' "${CASE[i]}" "${SCEN[i]}"; done)"

# ---- dispatch ----------------------------------------------------------------
find_run() { # $1 case -> run id or empty
  gh run list --repo "$REPO" --workflow "$WORKFLOW" --limit 100 \
    --json databaseId,displayTitle \
    --jq ".[] | select(.displayTitle == \"target/$SUITE/$1/$2\") | .databaseId" | head -n1
}

declare -a RUN
for ((i=1; i<=COUNT; i++)); do
  gh workflow run "$WORKFLOW" --repo "$REPO" --ref "$REF" \
    -f suite_id="$SUITE" -f case_id="${CASE[i]}" -f scenario="${SCEN[i]}" \
    -f hold_seconds="$HOLD_SECONDS" -f runner_label="$RUNNER_LABEL" >/dev/null
done
log "dispatched $COUNT targets; resolving run ids"
for ((i=1; i<=COUNT; i++)); do
  for attempt in $(seq 1 30); do
    id=$(find_run "${CASE[i]}" "${SCEN[i]}" || true)
    [[ -n "$id" ]] && break
    sleep 2
  done
  [[ -n "${id:-}" ]] || { echo "::error::could not resolve run for ${CASE[i]}"; exit 1; }
  RUN[i]="$id"
  # cancel_queued: cancel immediately, before any runner can be assigned.
  if [[ "${SCEN[i]}" == cancel_queued ]]; then
    gh run cancel "$id" --repo "$REPO" >/dev/null || true
    log "${CASE[i]} cancelled while queued (run $id)"
  fi
done

# ---- cancel_running: cancel once the job is actually executing -----------------
job_status() { gh api "repos/$REPO/actions/runs/$1/jobs" --jq '.jobs[0].status // "unknown"'; }
pending_running=()
for ((i=1; i<=COUNT; i++)); do [[ "${SCEN[i]}" == cancel_running ]] && pending_running+=("$i"); done
deadline=$(( $(date +%s) + TIMEOUT_MINUTES*60 ))
while (( ${#pending_running[@]} > 0 )) && (( $(date +%s) < deadline )); do
  still=()
  for i in "${pending_running[@]}"; do
    st=$(job_status "${RUN[i]}")
    if [[ "$st" == in_progress ]]; then
      sleep 5 # let the runner actually execute a step
      gh run cancel "${RUN[i]}" --repo "$REPO" >/dev/null || true
      log "${CASE[i]} cancelled while running (run ${RUN[i]})"
    elif [[ "$st" == completed ]]; then
      log "${CASE[i]} finished before it could be cancelled while running"
    else
      still+=("$i")
    fi
  done
  pending_running=("${still[@]:-}")
  [[ -z "${pending_running[*]:-}" ]] && pending_running=()
  (( ${#pending_running[@]} > 0 )) && sleep 5
done

# ---- wait for all ------------------------------------------------------------
all_done() {
  for ((i=1; i<=COUNT; i++)); do
    st=$(gh api "repos/$REPO/actions/runs/${RUN[i]}" --jq .status)
    [[ "$st" == completed ]] || return 1
  done
}
while ! all_done && (( $(date +%s) < deadline )); do sleep 10; done

# ---- grade -------------------------------------------------------------------
FAILED=0
{
  echo "## Cancel matrix: suite $SUITE on $RUNNER_LABEL"
  echo ""
  echo "| case | scenario | conclusion | expected | queued (s) | runner | ok |"
  echo "|---|---|---|---|---|---|---|"
} >> "$SUMMARY"
for ((i=1; i<=COUNT; i++)); do
  run_json=$(gh api "repos/$REPO/actions/runs/${RUN[i]}")
  status=$(jq -r .status <<<"$run_json"); concl=$(jq -r '.conclusion // "none"' <<<"$run_json")
  job=$(gh api "repos/$REPO/actions/runs/${RUN[i]}/jobs" --jq '.jobs[0]')
  created=$(jq -r .created_at <<<"$job"); started=$(jq -r '.started_at // empty' <<<"$job"); runner=$(jq -r '.runner_name // "-"' <<<"$job")
  q="-"; [[ -n "$started" ]] && q=$(( $(date -d "$started" +%s) - $(date -d "$created" +%s) ))
  case "${SCEN[i]}" in
    success)        exp=success ;;
    fail)           exp=failure ;;
    cancel_queued)  exp=cancelled ;;
    cancel_running) exp=cancelled ;;
  esac
  ok=yes
  [[ "$status" == completed ]] || { ok="no (status $status)"; FAILED=1; }
  [[ "$concl" == "$exp" ]] || { ok="no"; FAILED=1; }
  if [[ "${SCEN[i]}" != cancel_queued && "$q" != "-" ]] && (( q > QUEUE_SLO_SECONDS )); then ok="no (queue ${q}s)"; FAILED=1; fi
  echo "| ${CASE[i]} | ${SCEN[i]} | $concl | $exp | $q | $runner | $ok |" >> "$SUMMARY"
done

# ---- clean probe: the pool must be healthy afterwards ---------------------------
log "clean probe"
gh workflow run "$WORKFLOW" --repo "$REPO" --ref "$REF" \
  -f suite_id="$SUITE" -f case_id=clean -f scenario=success -f runner_label="$RUNNER_LABEL" >/dev/null
for attempt in $(seq 1 30); do id=$(find_run clean success || true); [[ -n "$id" ]] && break; sleep 2; done
t0=$(date +%s)
while (( $(date +%s) - t0 < QUEUE_SLO_SECONDS + 120 )); do
  [[ "$(gh api "repos/$REPO/actions/runs/$id" --jq .status)" == completed ]] && break
  sleep 10
done
cjob=$(gh api "repos/$REPO/actions/runs/$id/jobs" --jq '.jobs[0]')
cconcl=$(gh api "repos/$REPO/actions/runs/$id" --jq '.conclusion // "none"')
cq="-"; cs=$(jq -r '.started_at // empty' <<<"$cjob"); cc=$(jq -r .created_at <<<"$cjob")
[[ -n "$cs" ]] && cq=$(( $(date -d "$cs" +%s) - $(date -d "$cc" +%s) ))
cok=yes; [[ "$cconcl" == success ]] || { cok=no; FAILED=1; }
[[ "$cq" != "-" ]] && (( cq > QUEUE_SLO_SECONDS )) && { cok="no (queue ${cq}s)"; FAILED=1; }
{
  echo "| clean | success | $cconcl | success | $cq | $(jq -r '.runner_name // "-"' <<<"$cjob") | $cok |"
  echo ""
  echo "Suite id \`$SUITE\` — use it with \`scripts/verify-staging-logs.sh\` to check the controller side."
} >> "$SUMMARY"
echo "SUITE_ID=$SUITE" >> "${GITHUB_OUTPUT:-/dev/null}"
(( FAILED == 0 )) || { echo "::error::cancel matrix failed; see summary"; exit 1; }
log "cancel matrix passed"
