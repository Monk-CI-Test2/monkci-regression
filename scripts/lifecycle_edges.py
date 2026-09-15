#!/usr/bin/env python3
"""Deterministic staging jobs. Uses gh authentication; never accesses live stores."""
import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import signal
import subprocess
import time
import uuid

REPO = "Monk-CI-Test2/monkci-regression"
POOLS = tuple(f"monkci-ubuntu-24.04-{n}" for n in (2, 4, 8, 16, 32))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def seconds(start, end):
    if not start or not end:
        raise ValueError("missing timestamp")
    return (datetime.fromisoformat(end.replace("Z", "+00:00")) -
            datetime.fromisoformat(start.replace("Z", "+00:00"))).total_seconds()


def grade(case, run, jobs, slo):
    """Fail closed: a green run alone is not evidence that the probe executed."""
    errors = []
    if run.get("run_attempt") != case["attempt"]:
        errors.append("wrong run attempt")
    if run.get("status") != "completed":
        errors.append("run did not complete")
    if run.get("conclusion") not in case["run_conclusions"]:
        errors.append(f"unexpected run conclusion: {run.get('conclusion')}")
    probes = [job for job in jobs if job.get("name") == "probe"]
    if len(probes) != 1:
        return errors + [f"expected exactly one probe, found {len(probes)}"]
    job = probes[0]
    if job.get("run_attempt") != case["attempt"]:
        errors.append("job belongs to a different attempt")
    if job.get("status") != "completed" or job.get("conclusion") != case["job_conclusion"]:
        errors.append(f"unexpected job outcome: {job.get('status')}/{job.get('conclusion')}")
    runner = job.get("runner_name") or ""
    if case.get("cancel_phase") == "queued":
        if not case.get("cancel_observed") or runner:
            errors.append("queued cancellation was not proven; it raced into assignment or was not observed")
    else:
        prefix = case["pool"].replace("monkci-", "monkci--", 1).replace("24.04", "24-04") + "--"
        if not runner.startswith(prefix):
            errors.append("missing runner or runner from the wrong pool")
        if case["pool"] not in job.get("labels", []):
            errors.append("probe did not request the expected pool")
        try:
            wait = seconds(job.get("created_at"), job.get("started_at"))
            duration = seconds(job.get("started_at"), job.get("completed_at"))
            if not 0 <= wait <= slo:
                errors.append(f"queue wait {wait}s outside 0..{slo}s")
            if duration < case.get("min_duration", 0):
                errors.append("job finished before the intended long-running scenario was exercised")
        except ValueError:
            errors.append("missing or invalid execution timestamps")
        if case.get("cancel_phase") == "running" and not case.get("cancel_observed"):
            errors.append("running cancellation was not observed during the Act step")
    old = case.get("previous_job")
    if old:
        if old.get("id") == job.get("id"):
            errors.append("rerun reused the previous GitHub job ID")
        if runner and runner == old.get("runner_name"):
            errors.append("rerun reused an ephemeral runner")
    return errors


def grade_identities(cases):
    errors, ids, runners = [], set(), set()
    for case in cases:
        for job in case.get("jobs", []):
            if job.get("name") != "probe":
                continue
            if job.get("id") in ids:
                errors.append(f"{case['name']}: GitHub job ID appeared in more than one case/attempt")
            ids.add(job.get("id"))
            runner = job.get("runner_name")
            if runner:
                if runner in runners:
                    errors.append(f"{case['name']}: ephemeral runner executed more than one job")
                runners.add(runner)
    return errors


class GitHub:
    def api(self, path, method="GET", payload=None):
        # Only reads may be retried: replaying dispatch/rerun could create extra work.
        for attempt in range(3 if method == "GET" else 1):
            cmd = ["gh", "api", "--method", method, f"repos/{REPO}/{path}"]
            if payload is not None:
                cmd += ["--input", "-"]
            result = subprocess.run(cmd, input=json.dumps(payload) if payload is not None else None,
                                    text=True, capture_output=True, timeout=45)
            if result.returncode == 0:
                return json.loads(result.stdout) if result.stdout.strip() else None
            if method != "GET" or attempt == 2:
                raise RuntimeError(f"GitHub {method} {path} failed: {result.stderr.strip()}")
            time.sleep(2)

    def jobs(self, run_id, attempt):
        jobs = []
        for page in range(1, 101):
            batch = self.api(f"actions/runs/{run_id}/attempts/{attempt}/jobs?per_page=100&page={page}")
            jobs.extend(batch["jobs"])
            if len(batch["jobs"]) < 100:
                return jobs
        raise RuntimeError("job pagination exceeded limit")


class Suite:
    def __init__(self, args):
        self.args, self.gh = args, GitHub()
        self.suite = "edges-" + uuid.uuid4().hex[:12]
        self.deadline = time.monotonic() + args.deadline_minutes * 60
        self.report = {"suite_id": self.suite, "repository": REPO, "ref": args.ref,
                       "started_at": utcnow(), "cases": [], "errors": []}
        args.output.mkdir(parents=True, exist_ok=False)

    def save(self):
        path = self.args.output / "report.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.report, indent=2) + "\n")
        tmp.replace(path)

    def pause(self):
        if time.monotonic() >= self.deadline:
            raise TimeoutError("suite deadline exceeded")
        time.sleep(5)

    def find_run(self, title):
        # Match the unique title AND ref, never 'the latest run'.
        for page in range(1, 6):
            from urllib.parse import urlencode
            query = urlencode({"event": "workflow_dispatch", "branch": self.args.ref,
                               "per_page": 100, "page": page})
            runs = self.gh.api(f"actions/workflows/regression-target.yml/runs?{query}")["workflow_runs"]
            matches = [r for r in runs if r["display_title"] == title and r["head_branch"] == self.args.ref]
            if len(matches) > 1:
                raise RuntimeError(f"ambiguous dispatch: {title}")
            if matches:
                return matches[0]["id"]
            if len(runs) < 100:
                break
        return None

    def dispatch(self, name, scenario, pool, conclusion="success", **extra):
        case = {"name": name, "scenario": scenario, "pool": pool, "attempt": 1,
                "job_conclusion": conclusion, "run_conclusions": [conclusion], **extra}
        if conclusion == "timed_out":
            case["run_conclusions"] = ["failure", "timed_out"]
        case["title"] = f"target/{self.suite}/{name}/{scenario}"
        self.report["cases"].append(case)
        self.save()  # Also retain dispatch intent if the API response is lost.
        self.gh.api("actions/workflows/regression-target.yml/dispatches", "POST", {
            "ref": self.args.ref,
            "inputs": {"suite_id": self.suite, "case_id": name, "scenario": scenario,
                       "runner_label": pool, "hold_seconds": str(extra.get("hold", 180))}})
        for _ in range(24):
            case["run_id"] = self.find_run(case["title"])
            if case["run_id"]:
                self.save()
                print(f"{name}: https://github.com/{REPO}/actions/runs/{case['run_id']}", flush=True)
                return case
            self.pause()
        raise TimeoutError(f"could not resolve {name}")

    def cancel(self, case, phase):
        case["cancel_phase"] = phase
        while True:
            jobs = self.gh.jobs(case["run_id"], case["attempt"])
            probe = next((j for j in jobs if j["name"] == "probe"), {})
            observed = (probe.get("status") == "queued" and not probe.get("runner_name")) if phase == "queued" else (
                bool(probe.get("runner_name")) and probe.get("status") == "in_progress" and
                any(s["name"] == "Act" and s["status"] == "in_progress" for s in probe.get("steps", [])))
            if observed:
                case["cancel_observed"] = {"at": utcnow(), "job": probe}
                break
            if probe.get("status") == "completed" or (phase == "queued" and probe.get("runner_name")):
                self.report["errors"].append(f"{case['name']}: missed {phase} cancellation window")
                break
            self.pause()
        self.save()
        self.gh.api(f"actions/runs/{case['run_id']}/cancel", "POST")

    def wait(self, cases):
        pending = list(cases)
        while pending:
            for case in pending[:]:
                run = self.gh.api(f"actions/runs/{case['run_id']}")
                if run["run_attempt"] < case["attempt"] or run["status"] != "completed":
                    continue
                case["run"] = run
                case["jobs"] = self.gh.jobs(case["run_id"], case["attempt"])
                case["errors"] = grade(case, run, case["jobs"], self.args.queue_slo)
                print(f"{case['name']}: {case['errors'] or 'PASS'}", flush=True)
                pending.remove(case)
                self.save()
            if pending:
                self.pause()

    def rerun(self, first, failed_only=False):
        if first.get("errors"):
            raise RuntimeError(f"cannot test rerun: {first['name']} did not establish its prerequisite")
        old = next(j for j in first["jobs"] if j["name"] == "probe")
        case = {"name": first["name"] + "-rerun", "pool": first["pool"],
                "run_id": first["run_id"], "attempt": first["attempt"] + 1,
                "job_conclusion": "success", "run_conclusions": ["success"], "previous_job": old}
        self.report["cases"].append(case)
        self.save()
        endpoint = "rerun-failed-jobs" if failed_only else "rerun"
        self.gh.api(f"actions/runs/{case['run_id']}/{endpoint}", "POST")
        return case

    def execute(self):
        pool = self.args.pool
        # Try the queued cancellation before warming the pool. A lost race is a
        # coverage failure, never silently counted as a queued cancellation pass.
        queued = self.dispatch("queued-cancel", "cancel_queued", pool, "cancelled")
        self.cancel(queued, "queued")
        long_job = self.dispatch("long-running", "hold", pool, hold=180, min_duration=175)
        first = [queued, long_job]
        for n in range(self.args.burst):
            first.append(self.dispatch(f"fast-{n}", "success" if n % 2 == 0 else "fail", pool,
                                       "success" if n % 2 == 0 else "failure"))
        first.append(self.dispatch("timeout", "timeout", pool, "timed_out", min_duration=50))
        fail_once = self.dispatch("fail-once", "fail_once", pool, "failure")
        first.append(fail_once)
        if self.args.second_pool:
            for scenario, outcome in (("success", "success"), ("fail", "failure")):
                first.append(self.dispatch(f"other-pool-{scenario}", scenario, self.args.second_pool, outcome))
        # Observe/cancel this immediately; dispatch discovery for the other cases
        # can take long enough for an earlier short hold to finish naturally.
        running = self.dispatch("running-cancel", "cancel_running", pool, "cancelled", hold=180)
        first.append(running)
        self.cancel(running, "running")
        self.wait(first)
        success = next(c for c in first if c["name"] == "fast-0")
        reruns = []
        for case in (fail_once, success, queued, running):
            if case.get("errors"):
                self.report["errors"].append(f"{case['name']}: rerun not exercised because its prerequisite failed")
            else:
                reruns.append(self.rerun(case, failed_only=case is fail_once))
        self.wait(reruns)
        # Replay old job reads after reruns: their conclusions must remain intact.
        for case in first:
            jobs = self.gh.jobs(case["run_id"], 1)
            if [(j["id"], j["conclusion"]) for j in jobs] != [(j["id"], j["conclusion"]) for j in case["jobs"]]:
                self.report["errors"].append(f"{case['name']}: first-attempt results changed after rerun")
        # Fresh demand after the storm must still receive capacity in each pool.
        self.wait([self.dispatch(f"clean-{p}", "success", p) for p in
                   dict.fromkeys(p for p in (pool, self.args.second_pool) if p)])

    def cleanup(self):
        # Only this suite's unique dispatches. Failure remains failure after cleanup.
        seen = set()
        for case in self.report["cases"]:
            try:
                run_id = case.get("run_id") or self.find_run(case["title"])
                if not run_id or run_id in seen:
                    continue
                seen.add(run_id)
                if self.gh.api(f"actions/runs/{run_id}")["status"] != "completed":
                    self.gh.api(f"actions/runs/{run_id}/cancel", "POST")
                    self.report["errors"].append(f"cleanup cancelled unfinished run {run_id}")
            except Exception as exc:
                self.report["errors"].append(f"cleanup failed: {exc}")

    def finish(self):
        self.report["ended_at"] = utcnow()
        self.report["errors"].extend(grade_identities(self.report["cases"]))
        self.report["passed"] = not self.report["errors"] and all(
            "errors" in c and not c["errors"] for c in self.report["cases"])
        self.save()
        lines = [f"## Lifecycle edges: {self.suite}", "", "| Case | Attempt | Result | Run |",
                 "|---|---|---|---|"]
        for c in self.report["cases"]:
            result = "; ".join(c.get("errors", ["not graded"])) or "PASS"
            lines.append(f"| {c['name']} | {c['attempt']} | {result} | {c.get('run_id', '')} |")
        lines += ["", *self.report["errors"], "", "GitHub verdict only. Run verify_lifecycle_state.py for Redis/Postgres reconciliation."]
        summary = "\n".join(lines) + "\n"
        (self.args.output / "summary.md").write_text(summary)
        if os.environ.get("GITHUB_STEP_SUMMARY"):
            with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
                f.write(summary)
        print(summary, flush=True)
        return 0 if self.report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ref", required=True, help="Pushed ref containing updated regression-target.yml")
    parser.add_argument("--pool", choices=POOLS, default=POOLS[1])
    parser.add_argument("--second-pool", choices=POOLS)
    parser.add_argument("--burst", type=int, default=6)
    parser.add_argument("--queue-slo", type=int, default=600)
    parser.add_argument("--deadline-minutes", type=int, default=30)
    parser.add_argument("--output", type=Path, default=Path(".lifecycle-edges") / utcnow().replace(":", "-"))
    args = parser.parse_args()
    if not 2 <= args.burst <= 12 or not 60 <= args.queue_slo <= 1200 or not 10 <= args.deadline_minutes <= 40:
        parser.error("burst must be 2..12, queue-slo 60..1200, deadline-minutes 10..40")
    if args.second_pool == args.pool:
        parser.error("second-pool must differ from pool")
    suite = Suite(args)
    def interrupted(signum, frame):
        raise KeyboardInterrupt(f"signal {signum}")
    signal.signal(signal.SIGTERM, interrupted)
    try:
        suite.execute()
    except (Exception, KeyboardInterrupt) as exc:
        suite.report["errors"].append(str(exc) or "interrupted")
    finally:
        suite.cleanup()
    return suite.finish()


if __name__ == "__main__":
    raise SystemExit(main())
