#!/usr/bin/env bash
set -Eeuo pipefail

required=(
  GH_TOKEN GITHUB_REPOSITORY TARGET_WORKFLOW RUNNER_LABEL RUN_COUNT CHAOS_SEED
  CANCEL_QUEUED_PERCENT CANCEL_RUNNING_PERCENT CONCURRENCY_PERCENT FAIL_PERCENT
  HOLD_SECONDS OBSERVATION_MINUTES WORKFLOW_REF GITHUB_RUN_ID
)
for name in "${required[@]}"; do
  if [[ -z "${!name:-}" ]]; then
    echo "Missing required environment variable: $name" >&2
    exit 64
  fi
done

is_uint() { [[ "$1" =~ ^[0-9]+$ ]]; }
for name in RUN_COUNT CHAOS_SEED CANCEL_QUEUED_PERCENT CANCEL_RUNNING_PERCENT CONCURRENCY_PERCENT FAIL_PERCENT HOLD_SECONDS OBSERVATION_MINUTES; do
  if ! is_uint "${!name}"; then
    echo "$name must be a non-negative integer" >&2
    exit 64
  fi
done

if (( RUN_COUNT < 4 || RUN_COUNT > 60 )); then
  echo "RUN_COUNT must be between 4 and 60" >&2
  exit 64
fi
if (( HOLD_SECONDS < 30 || HOLD_SECONDS > 720 )); then
  echo "HOLD_SECONDS must be between 30 and 720" >&2
  exit 64
fi
if (( OBSERVATION_MINUTES < 3 || OBSERVATION_MINUTES > 25 )); then
  echo "OBSERVATION_MINUTES must be between 3 and 25" >&2
  exit 64
fi

sum=$((CANCEL_QUEUED_PERCENT + CANCEL_RUNNING_PERCENT + CONCURRENCY_PERCENT + FAIL_PERCENT))
if (( sum > 90 )); then
  echo "Chaos percentages must total 90 or less, leaving at least 10% baselines" >&2
  exit 64
fi

for tool in gh jq python3; do
  command -v "$tool" >/dev/null 2>&1 || {
    echo "Required command not found: $tool" >&2
    exit 69
  }
done

gh auth status >/dev/null

seed="$CHAOS_SEED"
if (( seed == 0 )); then
  seed="$GITHUB_RUN_ID"
fi
experiment_id="rlc-${GITHUB_RUN_ID}-${seed}"
output_dir=".runner-lag-chaos"
manifest="$output_dir/manifest.jsonl"
results="$output_dir/results.json"
summary_csv="$output_dir/results.csv"
mkdir -p "$output_dir"
: > "$manifest"

echo "experiment_id=$experiment_id" >> "$GITHUB_OUTPUT"

hash_mod() {
  local value="$1"
  local modulo="$2"
  local checksum
  checksum=$(printf '%s' "$value" | cksum | awk '{print $1}')
  echo $((checksum % modulo))
}

find_run_id() {
  local expected_title="$1"
  local attempts=0
  local run_id=""

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

dispatch_case() {
  local case_id="$1"
  local scenario="$2"
  local duration="$3"
  local concurrency_group="$4"
  local cancel_in_progress="$5"
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
    --arg scenario "$scenario" \
    --arg runner_label "$RUNNER_LABEL" \
    --arg run_id "$run_id" \
    --arg run_url "$run_url" \
    --arg concurrency_group "$concurrency_group" \
    --argjson duration_seconds "$duration" \
    --argjson dispatched_epoch "$(date +%s)" \
    '{experiment_id:$experiment_id,case_id:$case_id,scenario:$scenario,runner_label:$runner_label,run_id:($run_id|tonumber),run_url:$run_url,concurrency_group:$concurrency_group,duration_seconds:$duration_seconds,dispatched_epoch:$dispatched_epoch}' \
    >> "$manifest"

  echo "DISPATCHED case=$case_id scenario=$scenario run_id=$run_id url=$run_url"
}

cancel_queued_case() {
  local run_id="$1"
  local case_id="$2"
  local delay="$3"
  sleep "$delay"
  echo "CANCEL_QUEUED case=$case_id run_id=$run_id delay=${delay}s"
  gh run cancel "$run_id" --repo "$GITHUB_REPOSITORY" || true
}

cancel_after_runner_start() {
  local run_id="$1"
  local case_id="$2"
  local delay="$3"
  local deadline=$(( $(date +%s) + 360 ))

  while (( $(date +%s) < deadline )); do
    local jobs status conclusion
    jobs=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs?filter=all" 2>/dev/null || echo '{"jobs":[]}')
    status=$(jq -r '[.jobs[] | select(.name == "probe")][0].status // "missing"' <<<"$jobs")
    conclusion=$(jq -r '[.jobs[] | select(.name == "probe")][0].conclusion // ""' <<<"$jobs")

    if [[ "$status" == "in_progress" ]]; then
      echo "RUNNER_STARTED case=$case_id run_id=$run_id cancel_delay=${delay}s"
      sleep "$delay"
      gh run cancel "$run_id" --repo "$GITHUB_REPOSITORY" || true
      echo "CANCEL_RUNNING case=$case_id run_id=$run_id"
      return 0
    fi

    if [[ "$status" == "completed" || -n "$conclusion" ]]; then
      echo "SKIP_CANCEL case=$case_id run_id=$run_id status=$status conclusion=$conclusion"
      return 0
    fi

    sleep 3
  done

  echo "RUNNER_START_TIMEOUT case=$case_id run_id=$run_id" >&2
  return 0
}

echo "Starting experiment $experiment_id on $RUNNER_LABEL"
echo "Repository: $GITHUB_REPOSITORY"
echo "Workflow ref: $WORKFLOW_REF"
echo "Seed: $seed"

# Dispatch rapidly so identical-label jobs overlap and GitHub has opportunities
# to assign jobs to a runner reserved for a neighboring job.
for ((i = 1; i <= RUN_COUNT; i++)); do
  case_id=$(printf 'case-%03d' "$i")
  bucket=$(hash_mod "${seed}:${i}:scenario" 100)
  jitter=$(hash_mod "${seed}:${i}:duration" 21)
  scenario=""
  duration=0
  concurrency_group=""
  cancel_in_progress="false"

  q_end=$CANCEL_QUEUED_PERCENT
  r_end=$((q_end + CANCEL_RUNNING_PERCENT))
  c_end=$((r_end + CONCURRENCY_PERCENT))
  f_end=$((c_end + FAIL_PERCENT))

  if (( bucket < q_end )); then
    scenario="cancel_queued"
    duration=$HOLD_SECONDS
  elif (( bucket < r_end )); then
    scenario="cancel_running"
    duration=$HOLD_SECONDS
  elif (( bucket < c_end )); then
    scenario="concurrency_replace"
    duration=$HOLD_SECONDS
    # Two groups preserve churn while allowing more than one concurrency lane.
    lane=$(hash_mod "${seed}:${i}:lane" 2)
    concurrency_group="${experiment_id}-hot-${lane}"
    cancel_in_progress="true"
  elif (( bucket < f_end )); then
    scenario="fail_after_start"
    duration=$((5 + jitter))
  else
    # Alternate clean baselines with capacity-holding jobs.
    if (( i % 2 == 0 )); then
      scenario="capacity_hold"
      duration=$HOLD_SECONDS
    else
      scenario="success"
      duration=$((10 + jitter))
    fi
  fi

  dispatch_case "$case_id" "$scenario" "$duration" "$concurrency_group" "$cancel_in_progress"
  run_id=$(tail -n1 "$manifest" | jq -r '.run_id')

  if (( bucket < q_end )); then
    delay=$(hash_mod "${seed}:${i}:queued-cancel" 3)
    cancel_queued_case "$run_id" "$case_id" "$delay" &
  elif (( bucket < r_end )); then
    delay=$((5 + $(hash_mod "${seed}:${i}:running-cancel" 21)))
    cancel_after_runner_start "$run_id" "$case_id" "$delay" &
  fi

done

# Wait for all external cancellation workers. A worker times out rather than
# holding the orchestrator forever when the very bug under test occurs.
wait || true

echo "All dispatch and cancellation actions completed"

# Observe until every child run is terminal or the experiment deadline expires.
deadline=$(( $(date +%s) + OBSERVATION_MINUTES * 60 ))
while (( $(date +%s) < deadline )); do
  active=0
  while IFS= read -r item; do
    run_id=$(jq -r '.run_id' <<<"$item")
    status=$(gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" --jq '.status' 2>/dev/null || echo unknown)
    if [[ "$status" != "completed" ]]; then
      active=$((active + 1))
    fi
  done < "$manifest"

  echo "OBSERVE active_runs=$active timestamp=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if (( active == 0 )); then
    break
  fi
  sleep 15
done

# Collect raw API evidence first. This survives even when later analysis fails.
raw_dir="$output_dir/raw"
mkdir -p "$raw_dir"
while IFS= read -r item; do
  run_id=$(jq -r '.run_id' <<<"$item")
  gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}" > "$raw_dir/run-${run_id}.json" 2>/dev/null || echo '{}' > "$raw_dir/run-${run_id}.json"
  gh api "repos/${GITHUB_REPOSITORY}/actions/runs/${run_id}/jobs?filter=all" > "$raw_dir/jobs-${run_id}.json" 2>/dev/null || echo '{"jobs":[]}' > "$raw_dir/jobs-${run_id}.json"
done < "$manifest"

python3 - "$manifest" "$raw_dir" "$results" "$summary_csv" <<'PY'
import csv
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

manifest_path, raw_dir, results_path, csv_path = map(Path, sys.argv[1:])

def parse_time(value):
    if not value:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))

def seconds_between(start, end):
    a, b = parse_time(start), parse_time(end)
    if not a or not b:
        return None
    return round((b - a).total_seconds(), 3)

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
    queue_seconds = seconds_between(created_at, started_at)
    execution_seconds = seconds_between(started_at, completed_at)

    if not job:
        assignment = "no_job_record"
    elif job.get("runner_name"):
        assignment = "runner_assigned"
    elif (job.get("status") == "completed" and job.get("conclusion") == "cancelled"):
        assignment = "cancelled_before_runner"
    else:
        assignment = "not_assigned"

    anomalies = []
    if queue_seconds is not None and queue_seconds >= 180:
        anomalies.append("queue_over_180s")
    if run.get("status") != "completed":
        anomalies.append("not_terminal_at_deadline")
    if case["scenario"] == "cancel_queued" and assignment == "runner_assigned":
        anomalies.append("queued_cancel_raced_into_assignment")
    if case["scenario"] == "cancel_running" and assignment != "runner_assigned":
        anomalies.append("running_cancel_never_reached_runner")
    if case["scenario"] == "fail_after_start" and run.get("conclusion") != "failure":
        anomalies.append("intentional_failure_not_observed")
    if case["scenario"] in {"success", "capacity_hold"} and run.get("conclusion") not in {"success", None}:
        anomalies.append("baseline_did_not_succeed")

    rows.append({
        **case,
        "run_status": run.get("status"),
        "run_conclusion": run.get("conclusion"),
        "run_created_at": run.get("created_at"),
        "run_updated_at": run.get("updated_at"),
        "job_id": job.get("id"),
        "job_status": job.get("status"),
        "job_conclusion": job.get("conclusion"),
        "job_created_at": created_at,
        "job_started_at": started_at,
        "job_completed_at": completed_at,
        "runner_name": job.get("runner_name"),
        "labels": job.get("labels") or [],
        "assignment": assignment,
        "queue_seconds": queue_seconds,
        "execution_seconds": execution_seconds,
        "anomalies": anomalies,
    })

results_path.write_text(json.dumps(rows, indent=2, sort_keys=True) + "\n")

fields = [
    "experiment_id", "case_id", "scenario", "runner_label", "run_id", "job_id",
    "run_status", "run_conclusion", "job_status", "job_conclusion", "assignment",
    "runner_name", "job_created_at", "job_started_at", "job_completed_at",
    "queue_seconds", "execution_seconds", "anomalies", "run_url",
]
with csv_path.open("w", newline="") as f:
    writer = csv.DictWriter(f, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        out = {key: row.get(key) for key in fields}
        out["anomalies"] = ";".join(row["anomalies"])
        writer.writerow(out)

# Produce markdown for both stdout and GITHUB_STEP_SUMMARY.
terminal = sum(r["run_status"] == "completed" for r in rows)
assigned = sum(r["assignment"] == "runner_assigned" for r in rows)
pre_cancel = sum(r["assignment"] == "cancelled_before_runner" for r in rows)
stuck = [r for r in rows if "not_terminal_at_deadline" in r["anomalies"]]
slow = [r for r in rows if "queue_over_180s" in r["anomalies"]]
queue_values = [r["queue_seconds"] for r in rows if r["queue_seconds"] is not None]

print("## Runner lag chaos result")
print()
print(f"- Runs: **{len(rows)}**")
print(f"- Terminal by deadline: **{terminal}/{len(rows)}**")
print(f"- Runner assigned: **{assigned}**")
print(f"- Cancelled before runner assignment: **{pre_cancel}**")
print(f"- Still nonterminal: **{len(stuck)}**")
print(f"- Queue time >= 180s: **{len(slow)}**")
if queue_values:
    print(f"- Maximum observed queue time: **{max(queue_values):.3f}s**")
print()
print("### Cases requiring inspection")
print()
print("| Case | Scenario | Run | Job | Runner | Queue seconds | Status | Anomalies |")
print("|---|---|---:|---:|---|---:|---|---|")
interesting = [r for r in rows if r["anomalies"]]
for r in interesting[:30]:
    print(
        f"| {r['case_id']} | {r['scenario']} | {r['run_id']} | {r.get('job_id') or ''} | "
        f"{r.get('runner_name') or ''} | {r.get('queue_seconds') if r.get('queue_seconds') is not None else ''} | "
        f"{r.get('run_status')}/{r.get('run_conclusion')} | {', '.join(r['anomalies'])} |"
    )
if not interesting:
    print("| — | — | — | — | — | — | — | No anomalies detected |")
PY

python3 - "$results" > "$output_dir/summary.md" <<'PY'
import json
import sys
from pathlib import Path

rows = json.loads(Path(sys.argv[1]).read_text())
terminal = sum(r["run_status"] == "completed" for r in rows)
assigned = sum(r["assignment"] == "runner_assigned" for r in rows)
pre_cancel = sum(r["assignment"] == "cancelled_before_runner" for r in rows)
stuck = [r for r in rows if "not_terminal_at_deadline" in r["anomalies"]]
slow = [r for r in rows if "queue_over_180s" in r["anomalies"]]
queue_values = [r["queue_seconds"] for r in rows if r["queue_seconds"] is not None]

print("## Runner lag chaos result")
print()
print(f"- Runs: **{len(rows)}**")
print(f"- Terminal by deadline: **{terminal}/{len(rows)}**")
print(f"- Runner assigned: **{assigned}**")
print(f"- Cancelled before runner assignment: **{pre_cancel}**")
print(f"- Still nonterminal: **{len(stuck)}**")
print(f"- Queue time >= 180s: **{len(slow)}**")
if queue_values:
    print(f"- Maximum observed queue time: **{max(queue_values):.3f}s**")
print()
print("### Cases requiring inspection")
print()
print("| Case | Scenario | Run | Job | Runner | Queue seconds | Status | Anomalies |")
print("|---|---|---:|---:|---|---:|---|---|")
interesting = [r for r in rows if r["anomalies"]]
for r in interesting[:30]:
    print(
        f"| {r['case_id']} | {r['scenario']} | {r['run_id']} | {r.get('job_id') or ''} | "
        f"{r.get('runner_name') or ''} | {r.get('queue_seconds') if r.get('queue_seconds') is not None else ''} | "
        f"{r.get('run_status')}/{r.get('run_conclusion')} | {', '.join(r['anomalies'])} |"
    )
if not interesting:
    print("| — | — | — | — | — | — | — | No anomalies detected |")
PY

cat "$output_dir/summary.md" >> "$GITHUB_STEP_SUMMARY"
cat "$output_dir/summary.md"

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
