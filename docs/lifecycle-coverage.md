# Lifecycle regression coverage

The original suites exercise useful workloads, but a successful GitHub workflow
alone cannot prove that the controller removed demand or preserved the job's
conclusion. Use both GitHub evidence and the read-only state verdict below.

## Existing coverage and gaps

| Scenario | Existing coverage | Added coverage |
|---|---|---|
| Ordinary execution | 10 smoke | Exact job result, pool-specific runner identity, required timestamps |
| Very short successful jobs | 20 fast jobs | Deterministic mixed short successes and failures; reconcile each conclusion |
| Backlog and shared runner assignment | 30 queue pressure, 60 lag chaos, 61 assignment stress | Long job concurrent with short jobs; clean probes after the workload |
| Queued/running cancellation | 40 cancel matrix, 60/61 | Record the observed phase; a missed cancellation window fails coverage instead of passing |
| Job exceeds its GitHub timeout | None | Real one-minute job timeout; require GitHub's timeout annotation and preserve its actual conclusion (`cancelled` or `timed_out`) |
| Rerun a failed job | None | First attempt intentionally fails, failed-jobs rerun succeeds; new job ID and runner |
| Rerun successful/cancelled runs | None | Rerun after success, queued cancellation and running cancellation |
| Old attempt identity and conclusion | None | Read attempt-specific job lists again after reruns and check old results remain unchanged |
| Multiple pools at once | Existing suites choose one pool | Optional second pool with success/failure jobs and its own clean probe; check runner name matches pool |
| Completed jobs remain in Redis | Log checks only | Compare every attempt's GitHub result with Postgres and Redis, including completion tombstones |
| Leftover or returning demand | Log-based recovery counts | Check queue, pending demand, all five schedule indexes across all five pools, pending receipts and parked flags |
| VM cleanup after actual execution | Smoke checks runner identity | Check the VM GitHub actually used has left warm/busy state and membership after settling |
| Harness false positives | No isolated verdict tests | Unit tests reject missing evidence, wrong outcomes, wrong attempts, missed cancel phases and stale state; real local Redis executes the read-only snapshot |

`60-runner-lag-chaos` currently produces an observational anomaly report; its green
workflow is not an assertion that all reported anomalies are absent. `61` has a
stronger failing verdict. The new suite grades every planned case explicitly.

## Run the new suite

`70-lifecycle-edges.yml` runs on GitHub-hosted `ubuntu-latest`. By default it creates
11 initial runs, reruns four of them, and adds one clean probe: **16 job attempts**.
An optional second pool adds three attempts. Burst size is bounded to 2..12.
Allow approximately 10–20 minutes, with a 30-minute overall harness deadline.
The full regression includes it by default; set `include_lifecycle_edges=false`
to omit it. The full run consequently takes longer than the original 25–35 minutes.

```sh
gh workflow run 70-lifecycle-edges.yml -R Monk-CI-Test2/monkci-regression \
  -f runner=monkci-ubuntu-24.04-4 -f second_pool=monkci-ubuntu-24.04-2
```

Before the new workflow exists on the default branch, run the same harness locally
against a **pushed branch** containing the updated, already registered
`regression-target.yml`:

```sh
python3 scripts/lifecycle_edges.py --ref test/lifecycle-edge-cases \
  --pool monkci-ubuntu-24.04-4 --second-pool monkci-ubuntu-24.04-2 \
  --output .lifecycle-edges/candidate
```

The harness only dispatches/cancels/reruns its own uniquely identified targets in
`Monk-CI-Test2/monkci-regression`. It preserves raw run/job records, cancellation
observations and per-attempt verdicts in `report.json`. A failure or interruption
attempts to cancel its remaining children and retains the failure in the report.
If GitHub is unavailable during cleanup, follow the reported run URLs and cancel
the remaining runs manually. Parent Actions hard termination can also prevent
cleanup; each child has its own job timeout.

A queued cancellation can legitimately race with a warm runner. That execution
is **not evidence of the queued-cancel scenario**, so this suite reports failure.
Repeat that case in a quiet/cold pool instead of weakening the assertion.

## Check live controller state without changing it

First run `gcloud auth print-access-token >/dev/null`. If it reports a reauth error,
a human must run `gcloud auth login` before continuing. Open only the staging
tunnel, using the existing non-production bastion:

```sh
gcloud compute ssh monkci-non-prod-bastion --zone us-central1-a --project monkcidev \
  --tunnel-through-iap --quiet -- -N -o ExitOnForwardFailure=yes \
  -L 6443:172.16.0.2:443 -L 16378:10.71.134.3:6378 -L 15432:10.46.251.2:5432
```

Download the `lifecycle-edges-<run-id>` artifact if the suite ran on Actions, or use
the local output directory. In another terminal:

```sh
python3 scripts/verify_lifecycle_state.py .lifecycle-edges/candidate/report.json
```

This verifier is fixed to staging. It reads Redis/Postgres credentials from the
Kubernetes secret at runtime. Redis uses one `EVAL_RO` snapshot per sample (Redis
7+); Postgres queries use a read-only session through a throwaway PostgreSQL client
container. No credentials are placed in reports. No direct writes are made to
either live store. The verifier requires a passing workload report and records the
currently deployed controller image digest in `state-verdict.json`.

The first sample may need up to three minutes to converge. After convergence,
eleven minutes of observation must remain clean: a completed job returning to
demand, remaining parked, acquiring a pending receipt, retaining its owned reverse
mapping, or disagreeing with GitHub makes the verifier fail. Missing Redis or
Postgres evidence also fails; lack of access is not a passing test. The VM check
uses GitHub's actual runner, since a VM merely intended for job A might be running B.

Use the existing log verifier over the workload **and observation** window to
check watchdog/OOM signals too. Its recovery-event count is diagnostic: a genuine
GitHub-queued response can reset the per-job recovery budget, so a total count
over a long window is not itself proof that one recovery budget was exceeded.

## What these workflows cannot prove

| Remaining scenario | Appropriate validation |
|---|---|
| Two controller replicas overlap during rollout | Exercise an approved staging rollout while 30/61 runs; workload dispatch cannot create this condition by itself |
| Completion or start arrives exactly between a read and write | Controller Redis integration tests with controlled interleaving |
| NATS delivered registration but its acknowledgement/event was lost | Controller lifecycle integration tests with real NATS and injected delivery outcomes |
| Postgres/GitHub outage, rate limit, or delayed webhook | Controller completion-source tests with controlled failures; no live dependency outage is injected here |
| Parked job resumes after an authoritative queued response | Controller auditor tests; ordinary workloads cannot guarantee parking or the required API response |
| GitHub omits `in_progress` or a runner steals a particular job | Short jobs and queue pressure increase exposure; only event/state evidence proves the path occurred |
| Dependency skips, matrix fail-fast and selective rerun within a multi-job DAG | Not added in this suite; the existing target remains a single job, so failed-jobs rerun coverage here is single-job only |
| Redis/VM ownership changes at the exact retirement boundary | Controller atomic state-transition tests, plus live workload observation |

Before production, run the controller integration suite and live suites against
the **candidate image**, compare the recorded image digest with the release, and
require both workload and state verdicts. Passing against an older staging image
does not validate later controller fixes. A clean workflow run does not claim
that all unforced fault paths above occurred.

## Validate the harness itself

```sh
python3 -m unittest discover -s tests -v
# With a throwaway local Redis (DB 15) for the snapshot integration test:
LIFECYCLE_TEST_REDIS_PORT=6379 python3 -m unittest discover -s tests -v
```

`05-harness-checks.yml` runs these checks with a local Redis service on pull requests
that change the harness/workflows, and can also be dispatched manually. It does
not consume staging runner capacity.

The API reads deliberately use GitHub's
[attempt-specific job endpoint](https://docs.github.com/en/rest/actions/workflow-jobs#list-jobs-for-a-workflow-run-attempt)
so later reruns do not hide earlier attempts. Reruns use the documented
[workflow run endpoints](https://docs.github.com/en/rest/actions/workflow-runs).
