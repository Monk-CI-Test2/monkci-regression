#!/usr/bin/env bash
# Controller-side verdict for a regression window, from Cloud Logging.
#
# Usage:
#   scripts/verify-staging-logs.sh --since 2h
#   scripts/verify-staging-logs.sh --start 2026-09-09T11:00:00Z --end 2026-09-09T11:40:00Z
#   scripts/verify-staging-logs.sh --since 1h --project monkcidev --namespace staging
#
# Needs gcloud with logging.viewer on the project. Exit code 1 if a red signal
# is present. Signals:
#   RED   busy_vm_released_oom_restart   the alert condition; must be 0 in a regression window
#   RED   any job restored to demand more than max_assignment_recoveries times
#   AMBER assigned_job_recovery_exhausted  a job was parked; should be rare
#   AMBER stale_registration_parked        an old REGISTERING record was parked (expected once after deploy)
#   INFO  assigned_job_completed_elsewhere, runner_registered_for_completed_job,
#         busy_vm_released_job_completed_elsewhere: the fix doing its job
set -euo pipefail

PROJECT="monkcidev"; NAMESPACE="staging"; SINCE=""; START=""; END=""; MAX_RECOVERIES=3
while (( $# )); do
  case "$1" in
    --project) PROJECT="$2"; shift 2 ;;
    --namespace) NAMESPACE="$2"; shift 2 ;;
    --since) SINCE="$2"; shift 2 ;;
    --start) START="$2"; shift 2 ;;
    --end) END="$2"; shift 2 ;;
    --max-recoveries) MAX_RECOVERIES="$2"; shift 2 ;;
    *) echo "unknown arg $1" >&2; exit 2 ;;
  esac
done
if [[ -n "$SINCE" ]]; then
  START=$(date -u -d "-${SINCE%h}h" +%FT%TZ 2>/dev/null || date -u -d "-$SINCE" +%FT%TZ)
  END=$(date -u +%FT%TZ)
fi
[[ -n "$START" && -n "$END" ]] || { echo "give --since <Nh> or --start/--end" >&2; exit 2; }

BASE="resource.type=\"k8s_container\" resource.labels.namespace_name=\"$NAMESPACE\" resource.labels.container_name=\"mig-controller\" timestamp>=\"$START\" timestamp<=\"$END\""

count() { gcloud logging read "$BASE $1" --project "$PROJECT" --format=json 2>/dev/null | jq 'length'; }
by_job() { gcloud logging read "$BASE $1" --project "$PROJECT" --format=json 2>/dev/null | jq -r '.[] | (.jsonPayload.job_id // "?")' | sort | uniq -c | sort -rn; }

ev() { echo "(jsonPayload.event=\"$1\" OR textPayload:\"event=$1\")"; }
msg() { echo "(jsonPayload.message:\"$1\" OR textPayload:\"$1\")"; }

echo "window: $START .. $END  project=$PROJECT ns=$NAMESPACE"
echo
RED=0
oom=$(count "$(ev busy_vm_released_oom_restart)")
oom_msg=$(count "$(msg 'likely OOM restart mid-job')")
restored=$(by_job "$(ev assigned_job_demand_restored)")
exhausted=$(count "$(ev assigned_job_recovery_exhausted)")
parked_reg=$(count "$(ev stale_registration_parked)")
elsewhere=$(count "$(ev assigned_job_completed_elsewhere)")
on_register=$(count "$(ev runner_registered_for_completed_job)")
vm_released_done=$(count "$(ev busy_vm_released_job_completed_elsewhere)")
reconciled=$(count "$(ev runner_registered_reconciled_from_vm)")
not_queued=$(count "$(msg 'Job assignment ignored (job not queued)')")

printf '%-48s %s\n' "watchdog OOM release (alert condition)" "$oom (message match: $oom_msg)"
printf '%-48s %s\n' "job completed elsewhere, retired by auditor" "$elsewhere"
printf '%-48s %s\n' "job completed elsewhere, retired on register" "$on_register"
printf '%-48s %s\n' "busy VM released, job completed elsewhere" "$vm_released_done"
printf '%-48s %s\n' "lost RUNNER_REGISTERED reconciled from VM" "$reconciled"
printf '%-48s %s\n' "recovery exhausted (parked)" "$exhausted"
printf '%-48s %s\n' "stale REGISTERING parked" "$parked_reg"
printf '%-48s %s\n' "'job not queued' at registration (info)" "$not_queued"
echo
echo "demand restored, per job (count job_id):"
if [[ -n "$restored" ]]; then echo "$restored" | sed 's/^/  /'; else echo "  none"; fi
echo

(( oom == 0 && oom_msg == 0 )) || { echo "RED: watchdog OOM release fired $oom times"; RED=1; }
while read -r n job; do
  [[ -z "${n:-}" ]] && continue
  (( n > MAX_RECOVERIES )) && { echo "RED: job $job restored $n times (> $MAX_RECOVERIES)"; RED=1; }
done <<< "$restored"
(( exhausted == 0 )) || echo "AMBER: $exhausted job(s) parked after max recoveries; check they resolved"
(( parked_reg == 0 )) || echo "AMBER: $parked_reg stale REGISTERING record(s) parked"

if (( RED )); then echo; echo "VERDICT: RED"; exit 1; fi
echo "VERDICT: GREEN"
