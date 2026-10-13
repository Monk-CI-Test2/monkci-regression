#!/usr/bin/env python3
"""Grade a deterministic 100-job allocation workload using authoritative GitHub jobs."""
import argparse
from datetime import datetime
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys

from lifecycle_edges import GitHub, POOLS, REPO


def timestamp(value):
    stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return stamp.timestamp()


def quantile(values, q):
    values = sorted(values)
    pos = (len(values) - 1) * q
    low, high = math.floor(pos), math.ceil(pos)
    return values[low] + (values[high] - values[low]) * (pos - low)


def grade(jobs, pool, count, workload, max_queue, attempt=1):
    errors, slo_errors, rows, ids, runners = [], [], [], set(), set()
    plans = [j for j in jobs if j.get("name") == "plan"]
    try:
        if len(plans) != 1 or plans[0].get("conclusion") != "success":
            raise ValueError("missing or unsuccessful plan")
        eligible = timestamp(plans[0]["completed_at"])
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"errors": [str(exc)], "validity_errors": [str(exc)], "slo_errors": [], "rows": [], "metrics": None}
    expected = {f"fast-{i}" for i in range(1, count + 1)}
    probes = [j for j in jobs if str(j.get("name", "")).startswith("fast-")]
    if len(probes) != count or {j.get("name") for j in probes} != expected:
        errors.append(f"expected exactly {count} distinct workload jobs")
    prefix = pool.replace("monkci-", "monkci--", 1).replace("24.04", "24-04") + "--"
    for job in probes:
        name, runner, job_id = job.get("name"), job.get("runner_name"), job.get("id")
        if type(job_id) is not int or job_id <= 0 or job_id in ids:
            errors.append(f"{name}: missing or duplicate job identity")
        ids.add(job_id)
        if job.get("status") != "completed" or job.get("conclusion") != "success":
            errors.append(f"{name}: job did not complete successfully")
        if job.get("run_attempt") != attempt:
            errors.append(f"{name}: wrong attempt")
        if pool not in (job.get("labels") or []) or not runner or not runner.startswith(prefix):
            errors.append(f"{name}: wrong pool or runner")
        if runner in runners:
            errors.append(f"{name}: ephemeral runner reused")
        runners.add(runner)
        try:
            created, start, end = (timestamp(job[k]) for k in ("created_at", "started_at", "completed_at"))
            ready = max(created, eligible)
            wait, duration = start - ready, end - start
            if wait < 0 or duration < 0:
                raise ValueError("negative elapsed time")
            if wait > max_queue:
                slo_errors.append(f"{name}: {wait}s wait exceeds {max_queue}s")
            shard = int(re.fullmatch(r"fast-(\d+)", name).group(1))
            hold = (0, 3, 15, 30)[(shard - 1) % 4] if workload == "mixed" else 0
            if duration < hold:
                errors.append(f"{name}: workload completed before its required hold")
            rows.append({"job_id": job_id, "name": name, "runner_name": runner,
                         "eligible_at_ms": ready * 1000, "queue_seconds": wait,
                         "execution_seconds": duration, "hold_seconds": hold})
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            errors.append(f"{name}: invalid timing/name: {exc}")
    metrics = None
    if len(rows) == count:
        waits = [r["queue_seconds"] for r in rows]
        metrics = {"n": len(waits), "mean": statistics.mean(waits), "max": max(waits),
                   **{f"p{q}": quantile(waits, q / 100) for q in (50, 90, 95, 99)},
                   **{f"over_{limit}s": sum(w > limit for w in waits) for limit in (90, 180, 300)}}
    return {"errors": errors + slo_errors, "validity_errors": errors, "slo_errors": slo_errors,
            "rows": rows, "metrics": metrics}


def baseline_validity_errors(baseline):
    # Re-grade original job evidence for legacy reports whose single error list
    # combined workload validity and latency SLOs. A slow, otherwise valid run
    # is precisely the baseline a latency fix needs; retain its original errors.
    if "raw_jobs" in baseline:
        try:
            workload = baseline["workload"]
            checked = grade(baseline["raw_jobs"], workload["pool"], workload["count"],
                            workload["pattern"], math.inf, baseline["run_attempt"])
            errors = list(checked["validity_errors"])
            if checked["metrics"] != baseline["metrics"]:
                errors.append("baseline metrics differ from its original job evidence")
            return errors
        except (KeyError, TypeError, ValueError) as exc:
            return [f"invalid baseline evidence: {exc}"]
    return baseline.get("validity_errors", baseline["errors"])


def compare(candidate, baseline):
    errors = []
    if candidate["workload"] != baseline["workload"]:
        errors.append("baseline pool/count/runtime pattern differs")
    if candidate["run_id"] == baseline["run_id"]:
        errors.append("baseline and candidate must be different runs")
    if candidate["errors"] or baseline_validity_errors(baseline) or not candidate["metrics"] or not baseline["metrics"]:
        errors.append("both runs require complete, valid workload coverage")
    if errors:
        return {"accepted": False, "errors": errors, "deltas_seconds": {}}
    deltas = {m: candidate["metrics"][m] - baseline["metrics"][m] for m in ("mean", "p50", "p90", "p95", "p99", "max")}
    if deltas["mean"] > 0:
        errors.append("mean queue time regressed")
    for tail in ("p95", "p99"):
        if deltas[tail] >= 0:
            errors.append(f"{tail} did not improve")
    return {"accepted": not errors, "errors": errors, "deltas_seconds": deltas}


def collect(gh, run_id, args, attempt):
    run = gh.api(f"actions/runs/{run_id}")
    jobs = gh.jobs(run_id, attempt)
    result = grade(jobs, args.pool, args.count, args.workload, args.max_queue, attempt)
    result.update(run_id=run_id, run_url=run["html_url"], run_attempt=attempt,
                  head_sha=run["head_sha"], workload={"pool": args.pool, "count": args.count, "pattern": args.workload},
                  raw_jobs=jobs, raw_run=run)
    # Baseline configuration must be verified from its original report rather
    # than inferred from the number of jobs or supplied candidate parameters.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", required=True, type=int)
    parser.add_argument("--attempt", type=int, default=1)
    parser.add_argument("--pool", choices=POOLS, required=True)
    parser.add_argument("--count", type=int, default=100)
    parser.add_argument("--workload", choices=("fast", "mixed"), default="mixed")
    parser.add_argument("--max-queue", type=float, default=600)
    parser.add_argument("--build-id", default="unlabelled")
    parser.add_argument("--baseline-run-id", type=int)
    parser.add_argument("--baseline-report", type=Path)
    parser.add_argument("--output", type=Path, default=Path(".allotment/report.json"))
    args = parser.parse_args()
    if not 1 <= args.count <= 100 or args.max_queue <= 0:
        parser.error("count must be 1..100 and max queue must be positive")
    gh = GitHub()
    report = {"schema": 2, "repository": REPO, "build_id": args.build_id,
              "metric": "GitHub eligible-to-start; differs from backend accepted-to-start dashboard",
              "comparison": None, "errors": [], "metrics": None}
    try:
        report.update(collect(gh, args.run_id, args, args.attempt))
        if args.baseline_report:
            baseline = json.loads(args.baseline_report.read_text())
        elif args.baseline_run_id:
            # Download the baseline's authoritative measurement artifact with
            # GitHub CLI; no cloud credentials are required by the workflow.
            import subprocess
            import tempfile
            with tempfile.TemporaryDirectory(prefix="allotment-baseline-") as tmp:
                subprocess.run(["gh", "run", "download", str(args.baseline_run_id), "--repo", REPO,
                                "--pattern", f"allotment-{args.baseline_run_id}-attempt-*", "--dir", tmp], check=True)
                reports = list(Path(tmp).rglob("report.json"))
                if len(reports) != 1:
                    raise ValueError("baseline requires exactly one measurement artifact; select an unretried run")
                baseline = json.loads(reports[0].read_text())
            if baseline["run_id"] != args.baseline_run_id or baseline["repository"] != REPO:
                raise ValueError("baseline artifact identity mismatch")
        else:
            baseline = None
        if baseline is not None:
            report["comparison"] = compare(report, baseline)
            report["baseline"] = {k: baseline[k] for k in ("run_id", "build_id", "head_sha", "metrics", "workload", "errors")}
            report["baseline"]["validity_errors"] = baseline_validity_errors(baseline)
    except Exception as exc:
        report["errors"].append(str(exc))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2) + "\n")
    summary = f"### Job allotment: {args.count} jobs / {args.pool} / {args.workload}\n\n"
    summary += f"Build: `{args.build_id}`\n\nMetrics (seconds):\n\n```json\n{json.dumps(report['metrics'], indent=2)}\n```\n\n"
    summary += "Comparison: " + json.dumps(report["comparison"]) + "\n\n"
    summary += "Errors: " + json.dumps(report["errors"]) + "\n\nRepeat matching runs at least three times; one burst is not a production performance proof.\n"
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as output:
            output.write(summary)
    print(summary)
    return int(bool(report["errors"]) or (report["comparison"] is not None and not report["comparison"]["accepted"]))


if __name__ == "__main__":
    sys.exit(main())
