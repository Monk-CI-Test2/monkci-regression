# 100-job allotment measurement

The existing `20 Fast Jobs` workflow now supports a deterministic 100-job mixed
workload and an optional comparison against a previous measurement. It is still
manual and keeps the original 12-job fast workload as its default. No merge to the
default branch is necessary: the already registered workflow can dispatch the
`allotment-time-fix` ref.

There are exactly 100 MonkCI jobs when `count=100`; hosted plan/verdict jobs do not
consume MonkCI capacity. `mixed` runs 25 jobs each for 0, 3, 15 and 30 seconds, with
fail-fast disabled. This creates overlapping demand and fast completions so org
runner sharing and receipt reconciliation affect the measurement. `fast` runs
100 immediate completions and exercises delayed/missing start receipts.

Run the same workload against the old deployment first:

```sh
gh workflow run 20-fast-jobs.yml -R Monk-CI-Test2/monkci-regression \
  --ref allotment-time-fix -f runner=monkci-ubuntu-24.04-4 -f count=100 \
  -f workload=mixed -f max_queue_seconds=600 -f build_id=baseline-flags-off
```

After deploying the fix, repeat with the baseline's run ID:

```sh
gh workflow run 20-fast-jobs.yml -R Monk-CI-Test2/monkci-regression \
  --ref allotment-time-fix -f runner=monkci-ubuntu-24.04-4 -f count=100 \
  -f workload=mixed -f max_queue_seconds=600 -f build_id=allotment-time-fix \
  -f baseline_run_id=BASELINE_RUN_ID
```

Use `workload=fast` for the immediate-completion case, and separately repeat with
the 2- and 8-vCPU pools to check routing. Keep reserve/capacity, receipt grace,
concurrency/plan entitlement, runner image, workload and background traffic fixed
when comparing ledger flags off versus on. Record the deployed image/binary
versions in `build_id`. Repeat each pair at least three times and run cold and
steady-state trials separately; the report cannot verify cloud configuration or
distinguish warm inventory automatically.

Every required job must have a distinct ID and ephemeral runner, the correct pool
and attempt, valid timestamps, the required hold time, and successful completion.
Missing, cancelled, duplicate, unstarted and incorrectly routed jobs fail. The
report includes mean, p50/p90/p95/p99, max, and counts over 90/180/300 seconds,
along with all raw GitHub jobs and the exact workflow SHA. It paginates the jobs
API, so the hosted jobs cannot truncate the 100-job cohort.

Queue time starts at the later of the job's GitHub `created_at` and its plan
dependency's completion. It is **GitHub eligible-to-start**, not the backend
accepted-to-start dashboard metric. Do not compare it directly to that dashboard.
The 600-second maximum is an explicit initial completion guard, not a claimed
performance target. A paired comparison passes only when mean does not increase
and p95 and p99 both decrease, with full valid coverage in both runs. Without a
baseline the workflow checks correctness/SLO and records measurements, but makes
no improvement claim.

The baseline is read from its `allotment-RUN_ID-attempt-*` artifact; an old workflow
run without this measurement artifact cannot be used. Pool, count, and runtime
pattern must match. Choose a run that was not rerun, or download the intended
attempt's report and compare locally using `--baseline-report`.

Download the artifact from the Actions run. After convergence, run the existing
read-only staging log and window audits to check that completed jobs never
reappear as demand. Normal `org_supply_restorations` are separate from timeout
`recovery_count`; do not count legitimate org swaps as recovery-budget failures.
The source regression/race tests cover receipt duplicates, publication uncertainty,
concurrent claims and cancellation races; this workload does not inject faults.

The existing `75-workflow-flows.yml` remains available for the user's earlier
diamond/dependency, failure, rerun, long-running and pool-routing workflow.
