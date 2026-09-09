# monkci-regression

Fast, repeatable checks that a MonkCI controller build behaves correctly in a
live environment. Point them at the staging pools by running them in this org
(`Monk-CI-Test2` is served by the staging app), read the workflow summaries, then
run `scripts/verify-staging-logs.sh` for the controller-side verdict.

Each suite is a `workflow_dispatch` so nothing runs by accident. Orchestrators run
on `ubuntu-latest` so they never consume pool capacity themselves.

| Suite | What it proves | Runtime |
|---|---|---|
| `10-smoke.yml` | One ordinary job gets a VM, runs, completes. Baseline. | 2 to 4 min |
| `20-fast-jobs.yml` | A burst of sub-5-second jobs. This is the production zombie trigger: GitHub often skips `in_progress` and the webhook does not publish success, so the controller must learn completion from Postgres and stop claiming VMs. Grades every job's queue wait. | 3 to 6 min |
| `40-cancel-matrix.yml` | Mixed cancels while queued, cancels after start, failures and successes, then a clean probe that must get a runner within the SLO. Exercises cancelled completions, runner theft after late cancels, and pool health afterwards. | about 10 min |
| `30-queue-pressure.yml` | More jobs than warm VMs, each holding its runner. Drives the receipt-timeout and recovery path through GitHub's FIFO assignment. Every job must run within the SLO. | 4 to 8 min |
| `50-docker-cache-example.yml` | Writer seeds the per-repo docker cache, a reader on a fresh VM must hit it. The small example; the heavy matrices live in `docker-cache-test`. | 3 to 5 min |
| `00-full-regression.yml` | Runs all of the above in order and prints the log window to verify. | 25 to 35 min |

`regression-target.yml` is the building block the cancel matrix dispatches.

## Run

```sh
gh workflow run 00-full-regression.yml -R Monk-CI-Test2/monkci-regression -f runner=monkci-ubuntu-24.04-4
# or one suite
gh workflow run 20-fast-jobs.yml -R Monk-CI-Test2/monkci-regression -f runner=monkci-ubuntu-24.04-4 -f count=12
```

The cancel matrix and the full regression dispatch other workflows with
`GITHUB_TOKEN`. If org policy blocks that, add a repo secret `REGRESSION_GH_TOKEN`
with `actions:write` on this repo.

## Verify the controller side

Every suite prints a UTC window in its summary. Then:

```sh
scripts/verify-staging-logs.sh --start 2026-09-09T11:00:00Z --end 2026-09-09T11:40:00Z
# or simply
scripts/verify-staging-logs.sh --since 1h
```

Green means: the watchdog never logged an OOM release, and no job was returned
to demand more than `max_assignment_recoveries` times. The script also prints how
often the controller retired a job it learned had completed elsewhere, which is
the fix doing its work during `20-fast-jobs`.

## What a regression looks like

- `20-fast-jobs`: any job restored to demand three or more times, or the OOM
  watchdog line appearing about ten minutes after the run. That is the loop.
- `40-cancel-matrix`: the clean probe waiting longer than the SLO. That is leaked
  demand or leaked VMs after cancels.
- `30-queue-pressure`: jobs exceeding the SLO or the OOM line appearing later.
  That is recovery not backfilling stolen runners.
- `10-smoke` red: read nothing else until it is green.

## Notes

- Pools on staging start with zero warm VMs, so the first job of every suite
  includes a VM boot. The SLO defaults allow for that.
- This org has several GitHub Apps installed. Only the staging app's controller
  should register runners for these jobs; if runners from another environment
  pick jobs up, the queue-wait numbers are still valid but the log verdict is
  not, because it only reads staging.
