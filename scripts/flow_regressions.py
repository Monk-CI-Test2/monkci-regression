#!/usr/bin/env python3
"""Live workflow graphs; grades every job, not just workflow conclusions."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import signal
from urllib.parse import urlencode

from lifecycle_edges import GitHub, POOLS, REPO, Suite, seconds, utcnow

WORKFLOW = "flow-regression-target.yml"
DEPENDENCIES = {"left": ["seed"], "right": ["seed"], "join": ["left", "right"],
                "skipped": ["prerequisite"], "cleanup": ["prerequisite", "skipped"],
                "after_soft_failure": ["soft_failure"]}


def expectations(scenario, attempt=1):
    if scenario == "graph":
        return dict.fromkeys(("seed", "left", "right", "join"), "success")
    if scenario == "failures":
        return {"prerequisite": "failure", "skipped": "skipped", "cleanup": "success",
                "soft_failure": "success", "after_soft_failure": "success"}
    if scenario in ("matrix", "fail_once_matrix"):
        failures = (1, 4) if scenario == "matrix" else ((1,) if attempt == 1 else ())
        return {f"shard-{i}": "failure" if i in failures else "success" for i in range(6)}
    if scenario == "long":
        return {"long": "success"}
    if scenario == "cross_pool":
        return {"primary": "success", "secondary": "success"}
    raise ValueError("unknown scenario")


def grade_flow(case, run, jobs, slo):
    errors = []
    expected = expectations(case["scenario"], case["attempt"])
    conclusion = "failure" if "failure" in expected.values() else "success"
    if run.get("status") != "completed" or run.get("conclusion") != conclusion or run.get("run_attempt") != case["attempt"]:
        errors.append("wrong workflow outcome or attempt")
    # The workflow contains conditionally skipped jobs for other scenarios.
    actual = {}
    for job in jobs:
        name = job.get("name")
        if name not in expected:
            if job.get("conclusion") != "skipped" or job.get("runner_name"):
                errors.append(f"unexpected executed job: {name}")
            continue
        if name in actual:
            errors.append(f"duplicate job name: {name}")
        actual[name] = job
    prior = case.get("previous_jobs", {})
    # GitHub can omit unaffected successful jobs from a failed-only attempt.
    for name, job in prior.items():
        if job.get("conclusion") == "success" and name not in actual:
            actual[name] = job
    for name, outcome in expected.items():
        job = actual.get(name)
        if not job:
            errors.append(f"missing job: {name}")
            continue
        prefix = name + ": "
        if type(job.get("id")) is not int or job["id"] <= 0:
            errors.append(prefix + "invalid job ID")
        if job.get("status") != "completed" or job.get("conclusion") != outcome:
            errors.append(prefix + "wrong job outcome")
        if outcome == "skipped":
            if job.get("runner_name") or any(s.get("status") == "completed" for s in job.get("steps", [])):
                errors.append(prefix + "skipped prerequisite consumed a runner")
            continue
        previous = prior.get(name)
        if previous and previous["conclusion"] == "success":
            if job != previous:
                errors.append(prefix + "failed-only rerun changed an unaffected successful job")
        elif job.get("run_attempt") != case["attempt"]:
            errors.append(prefix + "wrong job attempt")
        if previous and previous["conclusion"] == "failure":
            if job.get("id") == previous.get("id") or job.get("runner_name") == previous.get("runner_name"):
                errors.append(prefix + "failed-only rerun reused job or runner identity")
        pool = case.get("second_pool", case["pool"]) if name == "secondary" else case["pool"]
        runner_prefix = pool.replace("monkci-", "monkci--", 1).replace("24.04", "24-04") + "--"
        if pool not in job.get("labels", []) or not (job.get("runner_name") or "").startswith(runner_prefix):
            errors.append(prefix + "wrong or missing pool/runner")
        try:
            created = job["created_at"]
            eligible = [created] + [actual[d]["completed_at"] for d in DEPENDENCIES.get(name, []) if actual[d].get("completed_at")]
            eligible_at = max(eligible, key=lambda x: datetime.fromisoformat(x.replace("Z", "+00:00")))
            queue = seconds(eligible_at, job.get("started_at"))
            duration = seconds(job.get("started_at"), job.get("completed_at"))
            if not 0 <= queue <= slo or duration < 0:
                errors.append(prefix + f"queue/execution time invalid or exceeds {slo}s")
            if name == "long" and duration < 365:
                errors.append(prefix + "long job did not cross the recovery/heartbeat deadline")
        except (ValueError, KeyError, TypeError, AttributeError):
            errors.append(prefix + "missing/invalid execution timestamps or dependency evidence")
    return errors


def grade_unique(cases):
    errors, ids, runners = [], {}, {}
    for case in cases:
        relevant = expectations(case["scenario"], case["attempt"])
        for job in case.get("jobs", []):
            if job.get("name") not in relevant or job.get("conclusion") == "skipped":
                continue
            identity = (case["run_id"], job.get("name"))
            jid, runner = job.get("id"), job.get("runner_name")
            # Same successful job retained by a failed-only rerun is intentional.
            if jid in ids and ids[jid] == identity:
                continue
            if jid in ids:
                errors.append(f"job ID reused across workflows: {jid}")
            if runner and runner in runners:
                errors.append(f"ephemeral runner reused across jobs: {runner}")
            ids[jid], runners[runner] = identity, jid
    return errors


class FlowSuite(Suite):
    def find_run(self, title):
        for page in range(1, 6):
            query = urlencode({"event": "workflow_dispatch", "branch": self.args.ref, "per_page": 100, "page": page})
            runs = self.gh.api(f"actions/workflows/{WORKFLOW}/runs?{query}")["workflow_runs"]
            matches = [r for r in runs if r["display_title"] == title and r["head_branch"] == self.args.ref]
            if len(matches) > 1:
                raise RuntimeError("ambiguous dispatch title")
            if matches:
                return matches[0]["id"]
            if len(runs) < 100:
                return None
        return None

    def dispatch_flow(self, scenario):
        case = {"name": scenario, "scenario": scenario, "pool": self.args.pool,
                "second_pool": self.args.second_pool or self.args.pool, "attempt": 1,
                "title": f"flows/{self.suite}/{scenario}-{len(self.report['cases'])}/{scenario}"}
        self.report["cases"].append(case)
        self.save()
        self.gh.api(f"actions/workflows/{WORKFLOW}/dispatches", "POST", {"ref": self.args.ref, "inputs": {
            "suite_id": self.suite, "case_id": case["title"].split("/")[2], "scenario": scenario,
            "runner_label": case["pool"], "second_pool": case["second_pool"]}})
        for _ in range(24):
            case["run_id"] = self.find_run(case["title"])
            if case["run_id"]:
                self.save()
                print(f"{scenario}: https://github.com/{REPO}/actions/runs/{case['run_id']}", flush=True)
                return case
            self.pause()
        raise TimeoutError("could not resolve dispatch")

    def wait_flows(self, cases):
        pending = list(cases)
        while pending:
            for case in list(pending):
                run = self.gh.api(f"actions/runs/{case['run_id']}")
                if run.get("run_attempt", 0) < case["attempt"] or run["status"] != "completed":
                    continue
                jobs = self.gh.jobs(case["run_id"], case["attempt"])
                case.update(run=run, jobs=jobs, errors=grade_flow(case, run, jobs, self.args.queue_slo))
                self.save()
                pending.remove(case)
            if pending:
                self.pause()

    def execute(self):
        cases = [self.dispatch_flow(s) for s in ("graph", "failures", "matrix", "long", "fail_once_matrix")]
        if self.args.second_pool:
            cases.append(self.dispatch_flow("cross_pool"))
        self.wait_flows(cases)
        first = next(c for c in cases if c["scenario"] == "fail_once_matrix")
        if first["errors"]:
            raise RuntimeError("failed-only rerun prerequisite did not pass grading")
        relevant = expectations(first["scenario"])
        prior = {j["name"]: j for j in first["jobs"] if j["name"] in relevant}
        self.gh.api(f"actions/runs/{first['run_id']}/rerun-failed-jobs", "POST")
        second = {k: v for k, v in first.items() if k not in ("jobs", "run", "errors")}
        second.update(name="matrix_failed_only_rerun", attempt=2, previous_jobs=prior)
        self.report["cases"].append(second)
        self.wait_flows([second])
        if self.gh.jobs(first["run_id"], 1) != first["jobs"]:
            self.report["errors"].append("rerun changed first-attempt evidence")
        # A clean DAG after mixed failures/rerun checks that old recovery does not resurrect demand.
        clean = self.dispatch_flow("graph")
        clean["name"] = "graph_after_recovery"
        self.wait_flows([clean])

    def finish(self):
        self.report["suite_type"] = "workflow_flows"
        self.report["ended_at"] = utcnow()
        self.report["errors"].extend(grade_unique(self.report["cases"]))
        self.report["passed"] = bool(self.report["cases"]) and not self.report["errors"] and all(
            "errors" in c and not c["errors"] for c in self.report["cases"])
        self.save()
        summary = f"Workflow regressions: {'PASS' if self.report['passed'] else 'FAIL'}; {len(self.report['cases'])} cases\n"
        print(summary, flush=True)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as out:
                out.write(summary)
        return 0 if self.report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--pool", choices=POOLS, default=POOLS[1])
    parser.add_argument("--second-pool", choices=POOLS)
    parser.add_argument("--queue-slo", type=int, choices=range(60, 1201), default=180)
    parser.add_argument("--deadline-minutes", type=int, choices=range(15, 61), default=35)
    parser.add_argument("--output", type=Path, default=Path(".flow-regressions/run"))
    args = parser.parse_args()
    if args.second_pool == args.pool:
        parser.error("second-pool must differ from pool")
    suite = FlowSuite(args)
    def interrupted(*_):
        raise RuntimeError("suite interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        suite.execute()
    except (Exception, KeyboardInterrupt) as exc:
        suite.report["errors"].append(str(exc))
    finally:
        suite.cleanup()
    raise SystemExit(suite.finish())


if __name__ == "__main__":
    main()
