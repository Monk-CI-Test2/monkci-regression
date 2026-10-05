# Readiness and recovery regression coverage

This suite tests the Olive allocation-delay mitigation and the older paths sharing
its Redis records. It has two independent verdicts: source state-machine contracts
and live GitHub workflow behavior. Neither verdict replaces the other.

## Source contracts: workflow 06

`06-source-contracts.yml` tests the exact controller, custom-MIG and MIGlet refs
selected at dispatch. Set `SOURCE_READ_TOKEN` in this repository to a token with
**read-only** access to all three private `MonkCi-Inc` source repositories.
The source fix must first be committed and pushed in all three repositories;
selecting the currently published pre-fix branch tips cannot test local edits.
After those fixes merge, run this workflow against `main` in each source repo.

Local validation includes uncommitted changes and does not edit those checkouts:

```sh
python3 scripts/source_contracts.py \
  --controller ../stale-readiness-mig-controller \
  --custom-mig ../stale-readiness-custom-mig \
  --miglet ../stale-readiness-miglet-agent \
  --race --output .source-contracts/my-run
```

Go and Docker are required. Redis 7 runs in an owned, disposable container on a
random **loopback** port. A Go overlay redirects existing unit tests' Redis
addresses to that port and adds this repo's contracts to temporary source copies.
No implementation is replaced. No staging or production credentials are used.
The container and temporary source copies are removed on success, failure and
handled interruption. A hard process kill can require manual container cleanup.

The runner executes all existing tests in the selected Redis, scheduler, handlers,
events, allocator, token, runner, agent, NATS and RPC packages, in addition to the
central contracts. Source helpers are reused for fixture setup; the assertions
exercise the actual implementation, including real Redis Lua. Incompatible source
APIs fail compilation instead of silently skipping the new contracts.

`report.json` records source HEAD, dirty status, content fingerprint, central leaf
case count, existing leaf case count, failures and skips. A pass requires every
central contract to execute and pass. JSONL files contain the complete Go test
output. Race mode covers changed concurrency paths. It omits the agent package's
existing idle-timer race, which is also present on its pre-fix main; ordinary agent
tests still run. This is not a claim that all agent code is race-free.

| Area | Contracts and existing tests exercised |
|---|---|
| Readiness admission | Fresh current-boot heartbeat; old boot, missing clocks, expired/future heartbeat, BOOTING/UNKNOWN/ERROR/IDLE/running state, stale indexes, durable retirement |
| Message ordering | 25 current-state/late-state combinations through both heartbeat and event scripts; failed writes leave the full VM record unchanged; READY event does not fabricate a heartbeat |
| Stop/start | Repeated restarts for reserved and on-demand capacity invalidate both clocks; missed restart repairs preserve claims and current-boot evidence |
| Three-minute recovery | Silent `VM_ALLOCATED`, BOOTING and stale READY claims; post-claim heartbeat barrier; registered runner wins over retirement |
| Multi-job incident | Twelve stalled claims are all recovered, deletion must be proved, repeated sweeps charge once, a younger healthy claim is untouched |
| Retirement | Durable intent, reconstructed/missing record, explicit deletion proof, heartbeat/update/claim CAS and primary/reservation class indexes |
| Late registration | Registration versus retirement in both orders; approval replay does not restart receipt age; obsolete/completed assignments rejected |
| GitHub errors | HTTP 403/404/429/500/502/503 produce no invented state or immediate lookup retries; repeated auditor sweeps pace failed polls instead of issuing 50 API calls |
| Runner listener | Approval retries, exhaustion with successful/failed cleanup, no fabricated customer-job failure, duplicate public commands start one listener |
| Old lifecycle flows | GitHub completion before enqueue, cancellation/completion races, reruns and job identity, parked queued/in-progress/completed/unavailable receipts, poll leases, bounded retries, queue lifetime |
| Capacity and cache | Demand isolation, idle warm floor, no-demand behavior, headroom, reservation protection, allocator stockout/fallback and cache lease/publication/reader tests |

## Live workflow flows: workflow 75

The orchestrator runs on GitHub-hosted Ubuntu. Only target jobs use MonkCI runners.
The suite adds **26 executed jobs** in seven attempts on one pool, or **28 jobs**
with a second pool; conditionally skipped jobs are inspected separately. Counts
include only executions, not successful jobs retained by a failed-only rerun.

| Scenario | Expected behavior |
|---|---|
| Diamond graph, twice | Seed outputs feed parallel branches then a join; a marker in one ephemeral runner cannot leak into the next runner |
| Failed prerequisites | Expected failure skips its dependent job, without runner allocation; `always()` cleanup receives exact dependency results |
| Continued step failure | Failed step retains its failure outcome, the job continues successfully, and its dependent job executes |
| Mixed matrix | Six shards with fail-fast disabled: two fail, four succeed; siblings cannot disappear when one shard fails |
| Long job | Runs for at least 365 seconds, crossing the historical five-minute busy-READY cleanup threshold without being killed |
| Failed-only matrix rerun | One failed shard reruns on a new job ID and ephemeral runner; five successful shards retain their execution evidence, even when GitHub clones their API records; first-attempt evidence remains unchanged |
| Optional two pools | Concurrent primary and secondary jobs execute on the correct pool |
| Final clean graph | New work executes after failures and rerun without old work reclaiming its runners |

The grader requires exact outcomes, all expected jobs, valid attempt/job/runner
identities, unique ephemeral runners, real execution times, pool labels, and no
execution of unrelated conditional jobs. Its default queue limit is **180 seconds**
from when dependencies make a job eligible. The three-minute VM retirement
threshold alone does not guarantee this end-to-end SLO: deletion and replacement
can add time. Set another explicit queue limit when measuring capacity contention;
the grader does not quietly relax it.

Run after publishing these workflows (new dispatch workflows must be available to
GitHub on the default branch):

```sh
gh workflow run 75-workflow-flows.yml -R Monk-CI-Test2/monkci-regression \
  -f runner=monkci-ubuntu-24.04-4 -f second_pool=monkci-ubuntu-24.04-2
```

Workflow 00 includes this suite by default. Existing smoke, fast jobs, cancellation,
queue pressure, lifecycle/rerun/timeout and Docker cache workflows continue to run.
No fault injection is enabled by the additions. The older workflows 60/61/80 remain
explicit chaos tools. Workflow 80's parked-recovery harness was written for the
older five-minute/three-retry implementation and log events: do not treat its old
counter/log expectations as proof for the new coordinated mitigation. Source
contracts cover the updated parked-recovery semantics safely.

## Read-only convergence check

Download the workflow 75 `report.json`, then use the existing staging tunnels and:

```sh
python3 scripts/verify_lifecycle_state.py path/to/report.json
```

The verifier now includes every executed flow job, the `RETIRING` schedule index,
and warm/busy reservation class indexes. It preserves GitHub's actual conclusions
and observes after convergence so late events that resurrect demand fail the
verdict. It still only reads staging stores; it does not repair state. Intentionally
skipped GitHub jobs are excluded from store reconciliation because they never ran
on a MonkCI runner. Their no-runner evidence is checked by the live grader.

## Harness validation

Workflow 05 checks the Python verdicts, the actual read-only Lua on disposable
Redis, Go fixture formatting and every workflow with actionlint. Negative cases
ensure green runs cannot hide missing/wrong jobs, malformed clocks, pool mistakes,
identity reuse, changed successful rerun shards, or leaked Redis indexes.

These tests bound recovery and preserve existing behavior. They do not diagnose
GCP guest startup failures or prove the absence of every possible event ordering.
Live workflows must be run against the coordinated deployed fix to validate its
actual cloud, GitHub and NATS integration.

## Validation on October 4, 2026

The local coordinated fix trees passed **81 central source contract leaf cases**
and **652 existing package leaf cases** in ordinary mode. The affected concurrency
packages also passed the race detector (77 central and 463 existing leaf cases).
Local source changes are included; these counts are not evidence of deployment.
The Python harness tests, including actual read-only Redis snapshot checks, and
workflow actionlint checks passed. The live workflow suite has not been dispatched.
