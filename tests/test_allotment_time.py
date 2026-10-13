import copy
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sys
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from allotment_time import compare, grade

POOL = "monkci-ubuntu-24.04-4"


def fixture():
    origin = datetime(2026, 10, 13, tzinfo=timezone.utc)
    stamp = lambda delta: (origin + timedelta(seconds=delta)).isoformat()
    jobs = [{"name": "plan", "conclusion": "success", "completed_at": stamp(10)}]
    for i in range(1, 101):
        jobs.append({"id": i, "name": f"fast-{i}", "status": "completed", "conclusion": "success",
                     "run_attempt": 1, "runner_name": f"monkci--ubuntu-24-04-4--vm{i}", "labels": [POOL],
                     "created_at": stamp(0), "started_at": stamp(10 + i), "completed_at": stamp(70 + i)})
    return jobs


class AllotmentTests(unittest.TestCase):
    def test_hundred_jobs_and_dependency_eligibility(self):
        result = grade(fixture(), POOL, 100, "mixed", 600)
        self.assertEqual(result["errors"], [])
        self.assertEqual(result["metrics"]["n"], 100)
        self.assertEqual(result["metrics"]["mean"], 50.5)
        self.assertAlmostEqual(result["metrics"]["p95"], 95.05)

    def test_missing_duplicate_failed_and_unstarted_jobs_fail(self):
        for mutation in ("missing", "duplicate", "failure", "unstarted", "missing_plan"):
            with self.subTest(mutation=mutation):
                jobs = fixture()
                if mutation == "missing": jobs.pop()
                elif mutation == "duplicate": jobs[-1] = copy.deepcopy(jobs[1])
                elif mutation == "failure": jobs[1]["conclusion"] = "failure"
                elif mutation == "unstarted": jobs[1]["started_at"] = None
                else: jobs.pop(0)
                self.assertTrue(grade(jobs, POOL, 100, "mixed", 600)["errors"])

    def test_runner_pool_attempt_and_execution_contract(self):
        for change in ({"runner_name": ""}, {"runner_name": "monkci--ubuntu-24-04-4--vm2"},
                       {"labels": ["monkci-ubuntu-24.04-2"]}, {"run_attempt": 2},
                       {"id": True}, {"completed_at": "2026-10-13T00:00:00Z"}):
            with self.subTest(change=change):
                jobs = fixture(); jobs[1].update(change)
                self.assertTrue(grade(jobs, POOL, 100, "mixed", 600)["errors"])

    def test_slo_and_negative_times_fail(self):
        self.assertTrue(grade(fixture(), POOL, 100, "mixed", 90)["errors"])
        jobs = fixture(); jobs[1]["started_at"] = jobs[1]["created_at"]
        self.assertTrue(grade(jobs, POOL, 100, "mixed", 600)["errors"])

    def test_comparison_requires_coverage_matched_workload_and_lower_tails(self):
        baseline = dict(grade(fixture(), POOL, 100, "mixed", 600), run_id=1,
                        workload={"pool": POOL, "count": 100, "pattern": "mixed"})
        candidate = copy.deepcopy(baseline); candidate["run_id"] = 2
        candidate["metrics"] = {k: v / 2 if k in ("mean", "p50", "p90", "p95", "p99", "max") else v
                                for k, v in baseline["metrics"].items()}
        self.assertTrue(compare(candidate, baseline)["accepted"])
        for mutation in ("missing", "pool", "pattern", "same_run", "mean", "p99"):
            with self.subTest(mutation=mutation):
                c = copy.deepcopy(candidate)
                if mutation == "missing": c["errors"] = ["missing job"]
                elif mutation in ("pool", "pattern"): c["workload"][mutation] = "different"
                elif mutation == "same_run": c["run_id"] = 1
                else: c["metrics"][mutation] = baseline["metrics"][mutation] + 1
                self.assertFalse(compare(c, baseline)["accepted"])

    def test_slow_legacy_baseline_requires_valid_original_evidence(self):
        jobs = fixture()
        baseline = dict(grade(jobs, POOL, 100, "mixed", 90), run_id=1, run_attempt=1,
                        raw_jobs=jobs, workload={"pool": POOL, "count": 100, "pattern": "mixed"})
        del baseline["validity_errors"]
        del baseline["slo_errors"]
        candidate = dict(grade(jobs, POOL, 100, "mixed", 600), run_id=2,
                         workload=copy.deepcopy(baseline["workload"]))
        for key in ("mean", "p50", "p90", "p95", "p99", "max"):
            candidate["metrics"][key] /= 2
        self.assertTrue(baseline["errors"])
        self.assertTrue(compare(candidate, baseline)["accepted"])
        self.assertTrue(baseline["errors"], "original failed SLO must be preserved")
        for mutation in ("missing", "failure", "metrics", "missing_evidence"):
            with self.subTest(mutation=mutation):
                invalid = copy.deepcopy(baseline)
                if mutation == "missing": invalid["raw_jobs"].pop()
                elif mutation == "failure": invalid["raw_jobs"][1]["conclusion"] = "failure"
                elif mutation == "metrics": invalid["metrics"]["mean"] += 1
                else: del invalid["raw_jobs"]
                self.assertFalse(compare(candidate, invalid)["accepted"])
        candidate["errors"] = ["candidate SLO failed"]
        self.assertFalse(compare(candidate, baseline)["accepted"])
