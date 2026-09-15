# Staging validation — 2026-09-15

Suite: `edges-0368107de1ae`, primary pool `monkci-ubuntu-24.04-4`, secondary pool
`monkci-ubuntu-24.04-2`. Workload window: 05:14:52–05:28:09 UTC. State observation
ended at 05:32:07 UTC. No controller deployment or direct live-store writes were made.

Staging ran the earlier `abd7637` build, identified by controller image digest
`sha256:46c8b74abfd0c09e9b8a51acee5013410ebbfdbfac1260d21f83f3789714e162`.
**This does not validate the later `4714098` controller fixes.**

| Validation | Result |
|---|---|
| Harness tests | 25 passed, including actual read-only Lua execution against throwaway Redis 7 |
| Workflow validation | actionlint passed for all four added/modified workflows |
| Live workload | 19 job attempts verified, including four reruns and two clean probes |
| Postgres conclusions | All 19 matched GitHub |
| Redis reconciliation | **FAIL:** 16 completed GitHub jobs remained `RUNNING` and in `ASSIGNED`; 12 retained their VM reverse mappings |
| Actual runner VM cleanup | No violations in the final state sample |
| Existing log verifier | GREEN: zero OOM releases and no excessive recovery count |

The state verifier sampled seven times over its settling window and exited 1 with
the same 44 errors across 16 jobs. It never reached the subsequent eleven-minute
clean-observation phase. The three cancelled jobs (including the timeout) were
completed in Redis; the other outcomes were stale. This is precisely why the
workload/log verdicts cannot substitute for checking live state.

## Workload evidence

| Scenario | Run |
|---|---|
| Queued cancellation, then successful rerun | [34931951366](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34931951366) |
| Running cancellation, then successful rerun | [34931960200](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34931960200) |
| Fast success, then successful rerun | [34931968650](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34931968650) |
| Fast failure | [34931977960](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34931977960) |
| Long-running success | [34932026351](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932026351) |
| GitHub-enforced one-minute timeout | [34932035842](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932035842) |
| First attempt fails; failed-jobs rerun succeeds | [34932045143](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932045143) |
| Second-pool success/failure | [34932054366](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932054366), [34932064268](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932064268) |
| Final clean probes | [34932682848](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932682848), [34932691935](https://github.com/Monk-CI-Test2/monkci-regression/actions/runs/34932691935) |

The first live execution uncovered an incorrect harness assumption: GitHub used
`cancelled`, not `timed_out`, for the one-minute timeout. Its check-run annotation
confirmed the execution-time limit. The harness was corrected to require that
annotation while preserving GitHub's actual conclusion. Replaying the saved
job/run evidence through the corrected grader verified all 19 attempts; the
original failing report was retained separately. This was a grader correction,
not a rerun of the entire workload. A plain cancellation without timeout evidence
still fails the timeout case.

Local evidence from this execution is in `.lifecycle-edges/live-20260915/`:
`report.json` (original), `regraded-report.json`, `state-verdict.json` and
`controller-logs.txt`. These files are ignored by Git and contain no credentials.

For example, GitHub job `104261874578` (first fast success) and job `104261900523`
(first fast failure) were completed in Postgres with the correct conclusions,
but both remained `RUNNING` in Redis's `ASSIGNED` index at the final sample.

Deploy the intended candidate through the normal staging deployment process,
then repeat both workload and state verification before treating this as a
production release verdict. See [coverage and remaining fault scenarios](lifecycle-coverage.md).
