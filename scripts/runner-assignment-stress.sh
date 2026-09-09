#!/usr/bin/env bash
# Runner Assignment Stress — multi-wave torture test for receipt-based
# assignment reconciliation (assigned-start timeout + requeue) in mig-controller.
#
# Attack model:
#   - Waves of bursts timed near the recovery cycle, so replacement runners for
#     recovered jobs compete with fresh younger demand.
#   - "Late" queued cancels land AFTER the controller has assigned/registered a
#     runner, freeing live runners that GitHub then hands to other jobs.
#   - The first wave can oversubscribe the pool (surge) to guarantee backlog and
#     runner permutation.
#   - A final zero-chaos clean wave measures baseline behavior after sustained
#     abuse (expected: zero anomalies, small queue times).
#
# Verdict: this script exits nonzero when any job that must run either never
# received a runner or exceeded RECOVERY_SLO_SECONDS of queue time. A green
# orchestrator run therefore certifies the assignment ledger under this load.
set -Eeuo pipefail

required=(
  GH_TOKEN GITHUB_REPOSITORY TARGET_WORKFLOW RUNNER_LABEL WAVES RUNS_PER_WAVE
  WAVE_INTERVAL_SECONDS CHAOS_INTENSITY SURGE_FACTOR_PERCENT
  RECOVERY_SLO_SECONDS OBSERVATION_MINUTES CHAOS_SEED WORKFLOW_REF GITHUB_RUN_ID
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: $name" >&2
    exit 64
  fi
done

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
for name in WAVES RUNS_PER_WAVE WAVE_INTERVAL_SECONDS CHAOS_INTENSITY \
            SURGE_FACTOR_PERCENT RECOVERY_SLO_SECONDS OBSERVATION_MINUTES CHAOS_SEED; do
  if ! is_uint "${!name}"; then
    echo "$name must be a non-negative integer" >&2
    exit 64
  fi
done

(( WAVES >= 1 && WAVES <= 6 )) || { echo "WAVES must be 1..6" >&2; exit 64; }
(( RUNS_PER_WAVE >= 4 && RUNS_PER_WAVE <= 20 )) || { echo "RUNS_PER_WAVE must be 4..20" >&2; exit 64; }
(( WAVES * RUNS_PER_WAVE <= 80 )) || { echo "WAVES*RUNS_PER_WAVE must be <= 80" >&2; exit 64; }
(( WAVE_INTERVAL_SECONDS >= 30 && WAVE_INTERVAL_SECONDS <= 600 )) || { echo "WAVE_INTERVAL_SECONDS must be 30..600" >&2; exit 64; }
(( CHAOS_INTENSITY <= 90 )) || { echo "CHAOS_INTENSITY must be <= 90 (leave some baselines)" >&2; exit 64; }
(( SURGE_FACTOR_PERCENT >= 100 && SURGE_FACTOR_PERCENT <= 300 )) || { echo "SURGE_FACTOR_PERCENT must be 100..300" >&2; exit 64; }
(( RECOVERY_SLO_SECONDS >= 120 && RECOVERY_SLO_SECONDS <= 900 )) || { echo "RECOVERY_SLO_SECONDS must be 120..900" >&2; exit 64; }
(( OBSERVATION_MINUTES >= 5 && OBSERVATION_MINUTES <= 25 )) || { echo "OBSERVATION_MINUTES must be 5..25" >&2; exit 64; }

for tool in gh jq python3; do
  command -v "$tool" >/dev/null 2>&1 || { echo "Required command not found: $tool" >&2; exit 69; }
done
gh auth status >/dev/null

seed="$CHAOS_SEED"
(( seed == 0 )) && seed="$GITHUB_RUN_ID"
experiment_id="ras-${GITHUB_RUN_ID}-${seed}"
output_dir=".runner-assignment-stress"
manifest="$output_dir/manifest.jsonl"
results="$output_dir/results.json"
verdict_file="$output_dir/verdict.json"
forced_file="$output_dir/forced-cancels.txt"
mkdir -p "$output_dir"
: > "$manifest"
: > "$forced_file"

echo "experiment_id=$experiment_id" >> "$GITHUB_OUTPUT"

# Probe hold duration for capacity-style scenarios. Long enough to keep runners
# occupied across a recovery cycle, short enough to keep the experiment moving.
HOLD_SECONDS=150

hash_mod() {
  local checksum
  checksum=$(printf '%s' "$1" | cksum | awk '{print $1}')
  echo $((checksum % $2))
}

find_run_id() {
  local expected_title="$1" attempts=0 run_id=""
  while (( attempts < 20 )); do
    run_id=$(gh run list \
      --repo "$GITHUB_REPOSITORY" \
      --workflow "$TARGET_WORKFLOW" \
      --event workflow_dispatch \
      --limit 100 \
      --json databaseId,displayTitle \
      | jq -r --arg title "$expected_title" '.[] | select(.displayTitle == $title) | .databaseId' \
      | head -n1)
    if [[ "$run_id" =~ ^[0-9]+$ ]]; then
      printf '%s\n' "$run_id"
      return 0
    fi
    attempts=$((attempts + 1))
    sleep 1
  done
  return 1
}

# dispatch_case <case_id> <wave> <chaos_kind> <scenario> <duration> <group> <cancel_in_progress>
dispatch_case() {
  local case_id="$1" wave="$2" chaos_kind="$3" scenario="$4" duration="$5"
  local concurrency_group="$6" cancel_in_progress="$7"
  local title="runner-lag-chaos/${experiment_id}/${case_id}/${scenario}"
  local output run_url run_id

  output=$(gh workflow run "$TARGET_WORKFLOW" \
    --repo "$GITHUB_REPOSITORY" \
    --ref "$WORKFLOW_REF" \
    -f "experiment_id=$experiment_id" \
    -f "case_id=$case_id" \
    -f "scenario=$scenario" \
    -f "runner_label=$RUNNER_LABEL" \
    -f "duration_seconds=$duration" \
    -f "concurrency_group=$concurrency_group" \
    -f "cancel_in_progress=$cancel_in_progress" 2>&1 || true)

  run_url=$(printf '%s\n' "$output" | grep -Eo 'https://[^[:space:]]+/actions/runs/[0-9]+' | tail -n1 || true)
  run_id="${run_url##*/}"
  if ! [[ "$run_id" =~ ^[0-9]+$ ]]; then
    run_id=$(find_run_id "$title") || {
      echo "Could not discover run ID for $case_id. gh output: $output" >&2
      return 1
    }
    run_url="https://github.com/${GITHUB_REPOSITORY}/actions/runs/${run_id}"
  fi

  jq -nc \
    --arg experiment_id "$experiment_id" \
    --arg case_id "$case_id" \
    --arg chaos_kind "$chaos_kind" \
    --arg scenario "$scenario" \
    --arg runner_label "$RUNNER_LABEL" \
    --arg run_id "$run_id" \
    --arg run_url "$run_url" \
    --arg concurrency_group "$concurrency_group" \
    --argjson wave "$wave" \
    --argjson duration_seconds "$duration" \
    --argjson dispatched_epoch "$(date +%s)" \
    '{experiment_id:$experiment_id,case_id:$case_id,wave:$wave,chaos_kind:$chaos_kind,scenario:$scenario,runner_label:$runner_label,run_id:($run_id|tonumber),run_url:$run_url,concurrency_group:$concurrency_group,duration_seconds:$duration_seconds,dispatched_epoch:$dispatched_epoch}' \
    >> "$manifest"

  echo "DISPATCHED wave=$wave case=$case_id kind=$chaos_kind run_id=$run_id url=$run_url"
}

cancel_after_delay() {
  local run_id="$1" case_id="$2" delay="$3" kind="$4"
  sleep "$delay"
  echo "CANCEL kind=$kind case=$case_id run_id=$run_id delay=${delay}s"
  gh run cancel "$run_id" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1 || true
}

cancel_after_runner_start() {
  local run_id="$1" case_id="$2" delay="$3"
  local deadline=$(( $(date +%s) + RECOVERY_SLO_SECONDS + 120 ))
  while (( $(date +%s) < deadline )); do
    local jobs status conclusion
    jobs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs?filter=all" 2>/dev/null || echo '{"jobs":[]}')
    status=$(jq -r '[.jobs[] | select(.name == "probe")][0].status // "missing"' <<<"$jobs")
    conclusion=$(jq -r '[.jobs[] | select(.name == "probe")][0].conclusion // ""' <<<"$jobs")
    if [[ "$status" == "in_progress" ]]; then
      echo "RUNNER_STARTED case=$case_id run_id=$run_id cancel_delay=${delay}s"
      sleep "$delay"
      gh run cancel "$run_id" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1 || true
      echo "CANCEL kind=cancel_running case=$case_id run_id=$run_id"
      return 0
    fi
    if [[ "$status" == "completed" || -n "$conclusion" ]]; then
      echo "SKIP_CANCEL case=$case_id run_id=$run_id status=$status conclusion=$conclusion"
      return 0
    fi
    sleep 3
  done
  # Under a working fix this should not fire: the run should reach a runner
  # within the SLO. The analyzer independently flags the run as stranded.
  echo "RUNNER_START_TIMEOUT case=$case_id run_id=$run_id" >&2
  return 0
}

echo "Starting stress experiment $experiment_id on $RUNNER_LABEL"
echo "Waves: $WAVES x ~$RUNS_PER_WAVE (surge ${SURGE_FACTOR_PERCENT}%) + clean wave, interval ${WAVE_INTERVAL_SECONDS}s"
echo "Chaos intensity: ${CHAOS_INTENSITY}% | Recovery SLO: ${RECOVERY_SLO_SECONDS}s | Seed: $seed"

case_counter=0
total_waves=$((WAVES + 1))   # final wave is the clean control wave

for ((wave = 1; wave <= total_waves; wave++)); do
  wave_start=$(date +%s)
  clean_wave=0
  wave_size=$RUNS_PER_WAVE
  if (( wave == 1 )); then
    wave_size=$((RUNS_PER_WAVE * SURGE_FACTOR_PERCENT / 100))
    (( wave_size > 24 )) && wave_size=24
  fi
  if (( wave == total_waves )); then
    clean_wave=1
    wave_size=$((RUNS_PER_WAVE < 8 ? RUNS_PER_WAVE : 8))
    echo "WAVE $wave (CLEAN CONTROL) size=$wave_size"
  else
    echo "WAVE $wave size=$wave_size"
  fi

  # Chaos bucket boundaries within [0,100): fractions of CHAOS_INTENSITY.
  fast_end=$((CHAOS_INTENSITY * 25 / 100))
  late_end=$((fast_end + CHAOS_INTENSITY * 25 / 100))
  running_end=$((late_end + CHAOS_INTENSITY * 20 / 100))
  conc_end=$((running_end + CHAOS_INTENSITY * 15 / 100))
  fail_end=$((conc_end + CHAOS_INTENSITY * 15 / 100))

  for ((i = 1; i <= wave_size; i++)); do
    case_counter=$((case_counter + 1))
    case_id=$(printf 'w%02d-case-%03d' "$wave" "$case_counter")
    bucket=$(hash_mod "${seed}:${wave}:${i}:scenario" 100)
    jitter=$(hash_mod "${seed}:${wave}:${i}:jitter" 21)
    (( clean_wave )) && bucket=100   # clean wave: baselines only

    chaos_kind="" scenario="" duration=0 concurrency_group="" cancel_in_progress="false"

    if (( bucket < fast_end )); then
      # Cancel almost immediately: usually before VM allocation. Removes demand
      # the controller may already be provisioning for.
      chaos_kind="cancel_queued_fast"; scenario="cancel_queued"; duration=$HOLD_SECONDS
    elif (( bucket < late_end )); then
      # Cancel 10-25s in: typically AFTER assignment/registration. Frees a live
      # registered runner for GitHub to hand to a different job. Theft fuel.
      chaos_kind="cancel_queued_late"; scenario="cancel_queued"; duration=$HOLD_SECONDS
    elif (( bucket < running_end )); then
      chaos_kind="cancel_running"; scenario="cancel_running"; duration=$HOLD_SECONDS
    elif (( bucket < conc_end )); then
      chaos_kind="concurrency_replace"; scenario="concurrency_replace"; duration=$HOLD_SECONDS
      lane=$(hash_mod "${seed}:${wave}:${i}:lane" 2)
      concurrency_group="${experiment_id}-w${wave}-lane-${lane}"
      cancel_in_progress="true"
    elif (( bucket < fail_end )); then
      chaos_kind="fail_after_start"; scenario="fail_after_start"; duration=$((5 + jitter))
    else
      if (( case_counter % 2 == 0 )); then
        chaos_kind="capacity_hold"; scenario="capacity_hold"; duration=$HOLD_SECONDS
      else
        chaos_kind="success"; scenario="success"; duration=$((10 + jitter))
      fi
    fi

    dispatch_case "$case_id" "$wave" "$chaos_kind" "$scenario" "$duration" "$concurrency_group" "$cancel_in_progress"
    run_id=$(tail -n1 "$manifest" | jq -r '.run_id')

    case "$chaos_kind" in
      cancel_queued_fast)
        delay=$(hash_mod "${seed}:${wave}:${i}:fastc" 3)
        cancel_after_delay "$run_id" "$case_id" "$delay" cancel_queued_fast &
        ;;
      cancel_queued_late)
        delay=$((10 + $(hash_mod "${seed}:${wave}:${i}:latec" 16)))
        cancel_after_delay "$run_id" "$case_id" "$delay" cancel_queued_late &
        ;;
      cancel_running)
        delay=$((5 + $(hash_mod "${seed}:${wave}:${i}:runc" 16)))
        cancel_after_runner_start "$run_id" "$case_id" "$delay" &
        ;;
    esac
  done

  if (( wave < total_waves )); then
    elapsed=$(( $(date +%s) - wave_start ))
    remaining=$(( WAVE_INTERVAL_SECONDS - elapsed ))
    if (( remaining > 0 )); then
      echo "WAVE $wave dispatched in ${elapsed}s; sleeping ${remaining}s before next wave"
      sleep "$remaining"
    else
      echo "WAVE $wave dispatch took ${elapsed}s (>= interval); starting next wave immediately"
    fi
  fi
done

# Wait for all cancellation workers. Workers have their own deadlines and
# cannot block the orchestrator indefinitely.
wait || true
echo "All waves dispatched and cancellation workers finished"

# Observe until every child run is terminal or the deadline expires.
deadline=$(( $(date +%s) + OBSERVATION_MINUTES * 60 ))
while (( $(date +%s) < deadline )); do
  active=0
  while IFS= read -r item; do
    run_id=$(jq -r '.run_id' <<<"$item")
    status=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" --jq '.status' 2>/dev/null || echo unknown)
    [[ "$status" != "completed" ]] && active=$((active + 1))
  done < "$manifest"
  echo "OBSERVE active_runs=$active timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  (( active == 0 )) && break
  sleep 15
done

# Force-cancel anything still nonterminal so the org is left clean. Every
# forced cancel is recorded and counts as a hard failure in the verdict:
# a stranded run is the exact bug this harness exists to catch.
while IFS= read -r item; do
  run_id=$(jq -r '.run_id' <<<"$item")
  status=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" --jq '.status' 2>/dev/null || echo unknown)
  if [[ "$status" != "completed" ]]; then
    echo "FORCED_CANCEL run_id=$run_id status=$status"
    echo "$run_id" >> "$forced_file"
    gh run cancel "$run_id" --repo "$GITHUB_REPOSITORY" >/dev/null 2>&1 || true
  fi
done < "$manifest"

# Collect raw API evidence. Survives even if the analysis below fails.
raw_dir="$output_dir/raw"
mkdir -p "$raw_dir"
while IFS= read -r item; do
  run_id=$(jq -r '.run_id' <<<"$item")
  gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" > "$raw_dir/run-${run_id}.json" 2>/dev/null || echo '{}' > "$raw_dir/run-${run_id}.json"
  gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs?filter=all" > "$raw_dir/jobs-${run_id}.json" 2>/dev/null || echo '{"jobs":[]}' > "$raw_dir/jobs-${run_id}.json"
done < "$manifest"

analyzer_rc=0
RECOVERY_SLO_SECONDS="$RECOVERY_SLO_SECONDS" python3 - "$manifest" "$raw_dir" "$results" "$verdict_file" "$forced_file" <<'PY' || analyzer_rc=$?
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from statistics import median

manifest_path, raw_dir, results_path, verdict_path, forced_path = map(Path, sys.argv[1:])
SLO = int(os.environ["RECOVERY_SLO_SECONDS"])
# One recovery cycle: assigned-start timeout (90s) + sweep interval (30s) plus
# slack. Used only to ESTIMATE how many recovery cycles a queue time implies;
# capacity waits inflate this, so it is a proxy, not a measurement.
RECOVERY_CYCLE = 130

forced = set()
if forced_path.exists():
    forced = {line.strip() for line in forced_path.read_text().splitlines() if line.strip()}

def parse_time(v):
    return datetime.fromisoformat(v.replace("Z", "+00:00")) if v else None

def seconds_between(a, b):
    a, b = parse_time(a), parse_time(b)
    return round((b - a).total_seconds(), 3) if a and b else None

MUST_RUN = {"success", "capacity_hold", "fail_after_start"}
MAY_CANCEL = {"cancel_queued_fast", "cancel_queued_late", "cancel_running", "concurrency_replace"}

rows = []
for line in manifest_path.read_text().splitlines():
    if not line.strip():
        continue
    case = json.loads(line)
    run_id = case["run_id"]
    try:
        run = json.loads((raw_dir / f"run-{run_id}.json").read_text())
    except Exception:
        run = {}
    try:
        jobs_doc = json.loads((raw_dir / f"jobs-{run_id}.json").read_text())
    except Exception:
        jobs_doc = {"jobs": []}

    jobs = jobs_doc.get("jobs") or []
    job = next((j for j in jobs if j.get("name") == "probe"), jobs[0] if jobs else {})

    created_at = job.get("created_at") or run.get("created_at")
    started_at = job.get("started_at")
    completed_at = job.get("completed_at") or run.get("updated_at")
    queue_seconds = seconds_between(created_at, started_at) if job.get("runner_name") else None
    execution_seconds = seconds_between(started_at, completed_at)

    kind = case["chaos_kind"]
    has_runner = bool(job.get("runner_name"))
    conclusion = run.get("conclusion")
    was_forced = str(run_id) in forced

    est_recoveries = 0
    if queue_seconds is not None and queue_seconds > 45:
        est_recoveries = int(queue_seconds // RECOVERY_CYCLE)

    hard, soft = [], []
    if was_forced:
        hard.append("stranded_forced_cancel")
    if kind in MUST_RUN:
        if not has_runner:
            hard.append("stranded_no_runner")
        elif queue_seconds is not None and queue_seconds > SLO:
            hard.append("queue_over_slo")
    if run.get("status") != "completed" and not was_forced:
        hard.append("not_terminal_at_deadline")

    if kind == "fail_after_start" and has_runner and conclusion != "failure":
        soft.append("intentional_failure_not_observed")
    if kind in {"success", "capacity_hold"} and has_runner and conclusion != "success":
        soft.append("baseline_did_not_succeed")
    if kind == "cancel_running" and not has_runner and not was_forced:
        # Its cancel worker only fires after in_progress; if the run ended
        # cancelled without a runner the orchestrator's SLO-bounded worker gave
        # up: the run never reached a runner. Hard assignment failure.
        hard.append("running_cancel_never_reached_runner")

    rows.append({
        **case,
        "run_status": run.get("status"),
        "run_conclusion": conclusion,
        "job_id": job.get("id"),
        "job_status": job.get("status"),
        "job_conclusion": job.get("conclusion"),
        "job_created_at": created_at,
        "job_started_at": started_at,
        "job_completed_at": completed_at,
        "runner_name": job.get("runner_name"),
        "queue_seconds": queue_seconds,
        "execution_seconds": execution_seconds,
        "estimated_recovery_cycles": est_recoveries,
        "forced_cancel": was_forced,
        "hard_failures": hard,
        "soft_anomalies": soft,
    })

results_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")

hard_rows = [r for r in rows if r["hard_failures"]]
queue_values = [r["queue_seconds"] for r in rows if r["queue_seconds"] is not None]
recovered = [r for r in rows if r["estimated_recovery_cycles"] >= 1]
clean_wave = max(r["wave"] for r in rows) if rows else 0
clean_rows = [r for r in rows if r["wave"] == clean_wave]
clean_bad = [r for r in clean_rows if r["hard_failures"] or r["soft_anomalies"]]

dist = {}
for r in rows:
    dist[r["estimated_recovery_cycles"]] = dist.get(r["estimated_recovery_cycles"], 0) + 1

verdict = {
    "total_runs": len(rows),
    "hard_failures": len(hard_rows),
    "runner_assigned": sum(1 for r in rows if r["runner_name"]),
    "max_queue_seconds": max(queue_values) if queue_values else None,
    "median_queue_seconds": round(median(queue_values), 1) if queue_values else None,
    "estimated_recovery_distribution": {str(k): v for k, v in sorted(dist.items())},
    "clean_wave_anomalies": len(clean_bad),
    "passed": not hard_rows,
}
verdict_path.write_text(json.dumps(verdict, indent=2) + "\n")

lines = []
lines.append("## Runner assignment stress result")
lines.append("")
lines.append(f"**VERDICT: {'PASS' if verdict['passed'] else 'FAIL'}**")
lines.append("")
lines.append(f"- Runs: **{len(rows)}** across **{clean_wave}** waves (last wave is the clean control)")
lines.append(f"- Runner assigned: **{verdict['runner_assigned']}**")
lines.append(f"- Hard failures: **{len(hard_rows)}**")
if queue_values:
    lines.append(f"- Queue seconds: median **{verdict['median_queue_seconds']}**, max **{verdict['max_queue_seconds']}** (SLO {SLO}s)")
lines.append(f"- Jobs with >=1 estimated recovery cycle (proxy, includes capacity waits): **{len(recovered)}**")
lines.append(f"- Estimated recovery-cycle distribution: `{verdict['estimated_recovery_distribution']}`")
lines.append(f"- Clean-wave anomalies: **{len(clean_bad)}** (expected 0)")
lines.append("")
lines.append("### Per-wave queue seconds")
lines.append("")
lines.append("| Wave | Runs | Assigned | Median queue | Max queue | Hard failures |")
lines.append("|---:|---:|---:|---:|---:|---:|")
for w in sorted({r["wave"] for r in rows}):
    wrows = [r for r in rows if r["wave"] == w]
    wq = [r["queue_seconds"] for r in wrows if r["queue_seconds"] is not None]
    lines.append(
        f"| {w}{' (clean)' if w == clean_wave else ''} | {len(wrows)} | "
        f"{sum(1 for r in wrows if r['runner_name'])} | "
        f"{round(median(wq),1) if wq else ''} | {max(wq) if wq else ''} | "
        f"{sum(1 for r in wrows if r['hard_failures'])} |"
    )
lines.append("")
flagged = [r for r in rows if r["hard_failures"] or r["soft_anomalies"]]
lines.append("### Cases requiring inspection")
lines.append("")
lines.append("| Case | Kind | Run | Runner | Queue s | Est. recoveries | Status | Failures/Anomalies |")
lines.append("|---|---|---:|---|---:|---:|---|---|")
for r in flagged[:40]:
    issues = ", ".join(r["hard_failures"] + r["soft_anomalies"])
    lines.append(
        f"| {r['case_id']} | {r['chaos_kind']} | {r['run_id']} | {r.get('runner_name') or ''} | "
        f"{r.get('queue_seconds') if r.get('queue_seconds') is not None else ''} | "
        f"{r['estimated_recovery_cycles']} | {r.get('run_status')}/{r.get('run_conclusion')} | {issues} |"
    )
if not flagged:
    lines.append("| — | — | — | — | — | — | — | No anomalies detected |")

summary = "\n".join(lines) + "\n"
Path(results_path.parent / "summary.md").write_text(summary)
print(summary)

sys.exit(0 if verdict["passed"] else 3)
PY

cat "$output_dir/summary.md" >> "$GITHUB_STEP_SUMMARY" || true

# Logs Explorer filter for correlating controller/webhook/miglet logs.
start_epoch=$(jq -s 'map(.dispatched_epoch) | min - 120' "$manifest")
start_time=$(date -u -d "@${start_epoch}" +%Y-%m-%dT%H:%M:%SZ)
end_time=$(date -u -d "+2 minutes" +%Y-%m-%dT%H:%M:%SZ)
search_terms=$(jq -r '
  [ .[] | (.run_id | tostring), ((.job_id // empty) | tostring) ]
  | unique
  | map("  SEARCH(\"" + . + "\")")
  | join("\n  OR\n")
' "$results")
cat > "$output_dir/logs-explorer-filter.txt" <<EOF
# Search controller, webhook and Miglet logs for this experiment.
timestamp >= "${start_time}"
timestamp <= "${end_time}"
(
${search_terms}
)
EOF

echo "Evidence written to $output_dir"

if (( analyzer_rc != 0 )); then
  echo "STRESS VERDICT: FAIL (see summary above)" >&2
  exit 1
fi
echo "STRESS VERDICT: PASS"
