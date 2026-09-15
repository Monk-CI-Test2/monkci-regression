import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
from lifecycle_edges import GitHub, POOLS, REPO, Suite, grade, grade_identities
from verify_lifecycle_state import SNAPSHOT_LUA, expected_jobs, grade_snapshot
import verify_lifecycle_state


def fixture():
    case = {"attempt": 1, "pool": POOLS[1], "run_conclusions": ["success"], "job_conclusion": "success"}
    run = {"run_attempt": 1, "status": "completed", "conclusion": "success"}
    job = {"id": 123, "name": "probe", "run_attempt": 1, "status": "completed", "conclusion": "success",
           "created_at": "2026-09-15T00:00:00Z", "started_at": "2026-09-15T00:01:00Z",
           "completed_at": "2026-09-15T00:01:03Z", "runner_name": "monkci--ubuntu-24-04-4--abcd1234",
           "labels": [POOLS[1]]}
    return case, run, job


class GitHubVerdictTests(unittest.TestCase):
    def test_valid_success(self):
        c, r, j = fixture()
        self.assertEqual([], grade(c, r, [j], 600))

    def test_green_run_cannot_hide_bad_or_missing_job_evidence(self):
        for change in ({"conclusion": "failure"}, {"status": "queued"}, {"runner_name": ""},
                       {"runner_name": "monkci--ubuntu-24-04-2--other"}, {"labels": []},
                       {"run_attempt": 2}, {"created_at": None}, {"started_at": None},
                       {"completed_at": None}, {"started_at": "2026-09-15T00:20:00Z"},
                       {"started_at": "2026-09-14T23:00:00Z"}):
            with self.subTest(change=change):
                c, r, j = fixture()
                j.update(change)
                self.assertTrue(grade(c, r, [j], 600))
        c, r, j = fixture()
        self.assertTrue(grade(c, r, [], 600))
        self.assertTrue(grade(c, r, [j, j], 600))

    def test_wrong_run_attempt_or_outcome_fails(self):
        for change in ({"run_attempt": 2}, {"status": "in_progress"}, {"conclusion": "failure"}):
            c, r, j = fixture()
            r.update(change)
            self.assertTrue(grade(c, r, [j], 600))

    def test_timeout_requires_github_timeout_evidence_and_preserves_its_conclusion(self):
        c, r, j = fixture()
        c.update(scenario="timeout", job_conclusion="timed_out", run_conclusions=["failure", "timed_out", "cancelled"])
        r["conclusion"] = "failure"
        j["conclusion"] = "failure"
        self.assertTrue(grade(c, r, [j], 600))
        j["conclusion"] = "timed_out"
        self.assertTrue(grade(c, r, [j], 600))
        c["annotations"] = [{"message": "The job has exceeded the maximum execution time of 1m0s"}]
        self.assertEqual([], grade(c, r, [j], 600))
        r["conclusion"] = j["conclusion"] = "cancelled"
        self.assertEqual([], grade(c, r, [j], 600))
        c["annotations"] = [{"message": "The operation was canceled."}]
        self.assertTrue(grade(c, r, [j], 600))

    def test_queued_cancel_that_raced_into_assignment_is_not_a_pass(self):
        c, r, j = fixture()
        c.update(job_conclusion="cancelled", run_conclusions=["cancelled"], cancel_phase="queued", cancel_observed={"at": "now"})
        r["conclusion"] = j["conclusion"] = "cancelled"
        self.assertTrue(grade(c, r, [j], 600))
        j["runner_name"] = ""
        self.assertEqual([], grade(c, r, [j], 600))
        c.pop("cancel_observed")
        self.assertTrue(grade(c, r, [j], 600))

    def test_running_cancel_must_have_been_observed(self):
        c, r, j = fixture()
        c.update(job_conclusion="cancelled", run_conclusions=["cancelled"], cancel_phase="running")
        r["conclusion"] = j["conclusion"] = "cancelled"
        self.assertTrue(grade(c, r, [j], 600))
        c["cancel_observed"] = {"at": "now"}
        self.assertEqual([], grade(c, r, [j], 600))

    def test_rerun_requires_new_job_and_new_runner(self):
        c, r, j = fixture()
        c["previous_job"] = dict(j)
        self.assertEqual(2, len(grade(c, r, [j], 600)))
        j.update(id=124, runner_name="monkci--ubuntu-24-04-4--new")
        self.assertEqual([], grade(c, r, [j], 600))

    def test_ephemeral_runner_cannot_serve_two_independent_cases(self):
        _, _, job = fixture()
        cases = [{"name": "a", "jobs": [job]}, {"name": "b", "jobs": [{**job, "id": 124}]}]
        self.assertTrue(grade_identities(cases))
        cases[1]["jobs"][0]["runner_name"] = "monkci--ubuntu-24-04-4--new"
        self.assertEqual([], grade_identities(cases))

    def test_duplicate_job_evidence_cannot_count_as_two_completed_cases(self):
        _, _, job = fixture()
        self.assertTrue(grade_identities([{"name": "a", "jobs": [job]}, {"name": "b", "jobs": [job]}]))

    def test_long_running_case_must_actually_outlive_recovery_interval(self):
        c, r, j = fixture()
        c["min_duration"] = 175
        self.assertTrue(grade(c, r, [j], 600))
        j["completed_at"] = "2026-09-15T00:04:00Z"
        self.assertEqual([], grade(c, r, [j], 600))

    def test_attempt_specific_jobs_are_paginated(self):
        gh = GitHub()
        with patch.object(gh, "api", side_effect=[{"jobs": [{}] * 100}, {"jobs": [{"id": 123}]}]) as api:
            self.assertEqual(101, len(gh.jobs(42, 2)))
        self.assertIn("/42/attempts/2/jobs?per_page=100&page=2", api.call_args.args[0])

    def test_dispatch_is_never_retried_on_ambiguous_failure(self):
        result = subprocess.CompletedProcess([], 1, "", "connection reset")
        with patch("lifecycle_edges.subprocess.run", return_value=result) as run:
            with self.assertRaises(RuntimeError):
                GitHub().api("actions/workflows/regression-target.yml/dispatches", "POST", {})
            self.assertEqual(1, run.call_count)

    def test_run_discovery_ignores_other_titles_and_branches(self):
        suite = Suite.__new__(Suite)
        suite.args = argparse_namespace(ref="test/ref")
        suite.gh = GitHub()
        runs = [{"display_title": "target/ours", "head_branch": "main", "id": 1},
                {"display_title": "target/other", "head_branch": "test/ref", "id": 2},
                {"display_title": "target/ours", "head_branch": "test/ref", "id": 3}]
        with patch.object(suite.gh, "api", return_value={"workflow_runs": runs}):
            self.assertEqual(3, suite.find_run("target/ours"))


def argparse_namespace(**kwargs):
    from argparse import Namespace
    return Namespace(**kwargs)


def state_fixture():
    expected = [{"job_id": 123, "conclusion": "SUCCESS", "runner_name": "vm-used"}]
    redis = [{"job_id": 123, "marker": "uuid", "job": {"job_id": 123, "status": "COMPLETED", "conclusion": "SUCCESS"},
              "memberships": [], "actual_vm": {"state": "completed"}, "actual_vm_memberships": []}]
    pg = [{"job_id": 123, "status": "COMPLETED", "conclusion": "success"}]
    return expected, redis, pg


class StateVerdictTests(unittest.TestCase):
    def test_reconciled_result_accepts_lowercase_webhook_conclusion(self):
        self.assertEqual([], grade_snapshot(*state_fixture()))

    def test_absent_rows_fail_closed(self):
        e, r, p = state_fixture()
        self.assertTrue(grade_snapshot(e, [], p))
        self.assertTrue(grade_snapshot(e, r, []))

    def test_completed_job_must_leave_every_index_including_other_pools(self):
        for key in (f"jobs:queue:{POOLS[0]}", f"pending_allocation_requests:{POOLS[1]}",
                    *(f"jobs:by_schedule:{POOLS[2]}:{s}" for s in
                      ("QUEUED", "PENDING_ALLOCATION", "VM_ALLOCATED", "REGISTERING", "ASSIGNED"))):
            with self.subTest(key=key):
                e, r, p = state_fixture()
                r[0]["memberships"] = [key]
                self.assertTrue(grade_snapshot(e, r, p))

    def test_wrong_conclusion_and_parked_terminal_state_fail(self):
        for change in ({"status": "RUNNING"}, {"conclusion": "FAILURE"}, {"job_id": 124},
                       {"assignment_receipt_pending": True}, {"recovery_exhausted": True}):
            with self.subTest(change=change):
                e, r, p = state_fixture()
                r[0]["job"].update(change)
                self.assertTrue(grade_snapshot(e, r, p))
        e, r, p = state_fixture()
        p[0]["conclusion"] = "failure"
        self.assertTrue(grade_snapshot(e, r, p))

    def test_matching_tombstone_is_valid_without_job_record(self):
        e, r, p = state_fixture()
        r[0].pop("job")
        r[0]["marker"] = "completed:SUCCESS"
        self.assertEqual([], grade_snapshot(e, r, p))
        r[0]["marker"] = "completed:CANCELLED"
        self.assertTrue(grade_snapshot(e, r, p))

    def test_only_owned_reverse_mapping_is_a_leak(self):
        e, r, p = state_fixture()
        r[0]["reverse_owner"] = "uuid"
        self.assertTrue(grade_snapshot(e, r, p))
        r[0]["reverse_owner"] = "another-job"
        self.assertEqual([], grade_snapshot(e, r, p))

    def test_actual_runner_busy_membership_is_checked_even_after_vm_deletion(self):
        e, r, p = state_fixture()
        r[0].pop("actual_vm")
        r[0]["actual_vm_memberships"] = [f"pool:ubuntu-24.04-4:busy"]
        self.assertTrue(grade_snapshot(e, r, p))
        r[0]["actual_vm_memberships"] = []
        self.assertEqual([], grade_snapshot(e, r, p))

    def test_verifier_rejects_empty_failed_foreign_or_malformed_reports(self):
        c, run, job = fixture()
        valid = {"repository": REPO, "passed": True, "cases": [{"jobs": [job]}]}
        self.assertEqual(123, expected_jobs(valid)[0]["job_id"])
        for change in ({"repository": "other/repo"}, {"passed": False}, {"cases": []}):
            with self.assertRaises(ValueError):
                expected_jobs({**valid, **change})
        for invalid in ("123; DELETE", -1, True):
            report = copy.deepcopy(valid)
            report["cases"][0]["jobs"][0]["id"] = invalid
            with self.assertRaises(ValueError):
                expected_jobs(report)


class ObservationTests(unittest.TestCase):
    def observe(self, snapshots, clock):
        _, _, job = fixture()
        report = {"repository": REPO, "passed": True, "cases": [{"jobs": [job]}]}
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "report.json"
            path.write_text(json.dumps(report))
            with patch.object(sys, "argv", ["verify", str(path), "--observe-seconds", "60"]), \
                    patch.object(verify_lifecycle_state, "Staging") as staging, \
                    patch("verify_lifecycle_state.time.monotonic", side_effect=clock), \
                    patch("verify_lifecycle_state.time.sleep"), patch("builtins.print"):
                staging.return_value.deployment.return_value = [{"image_id": "sha256:test"}]
                staging.return_value.snapshot.side_effect = snapshots
                rc = verify_lifecycle_state.main()
            verdict = json.loads((Path(directory) / "state-verdict.json").read_text())
            return rc, verdict

    def test_convergence_is_not_a_pass_until_observation_finishes(self):
        _, redis, pg = state_fixture()
        rc, verdict = self.observe([(redis, pg), (redis, pg)], [0, 0, 61])
        self.assertEqual(0, rc)
        self.assertTrue(verdict["passed"])
        self.assertEqual(2, len(verdict["samples"]))

    def test_returning_demand_after_convergence_fails_immediately(self):
        _, redis, pg = state_fixture()
        dirty = copy.deepcopy(redis)
        dirty[0]["memberships"] = [f"pending_allocation_requests:{POOLS[1]}"]
        rc, verdict = self.observe([(redis, pg), (dirty, pg)], [0, 0, 30])
        self.assertEqual(1, rc)
        self.assertFalse(verdict["passed"])

    def test_access_failure_after_clean_sample_cannot_be_green(self):
        _, redis, pg = state_fixture()
        rc, verdict = self.observe([(redis, pg), RuntimeError("read failed")], [0, 0])
        self.assertEqual(1, rc)
        self.assertFalse(verdict["passed"])
        self.assertEqual(["read failed"], verdict["errors"])


@unittest.skipUnless(os.environ.get("LIFECYCLE_TEST_REDIS_PORT"), "requires throwaway local Redis")
class RedisSnapshotTests(unittest.TestCase):
    """Run the actual production verifier Lua on a throwaway localhost Redis DB."""
    def redis(self, *args):
        return subprocess.check_output(["redis-cli", "-h", "127.0.0.1", "-p",
                                        os.environ["LIFECYCLE_TEST_REDIS_PORT"], "-n", "15", "--raw", *args], text=True)

    def test_snapshot_finds_cross_pool_demand_without_writing(self):
        import uuid
        uid = "lifecycle-test-" + uuid.uuid4().hex
        jid = int(uuid.uuid4().int % 1000000000000)
        mapping, detail = f"jobs:github_job_id:{jid}", f"jobs:details:{uid}"
        index = f"jobs:by_schedule:{POOLS[0]}:ASSIGNED"
        try:
            self.redis("SET", mapping, uid, "EX", "60")
            self.redis("SET", detail, json.dumps({"job_id": jid, "status": "COMPLETED", "conclusion": "SUCCESS"}), "EX", "60")
            self.redis("SADD", index, uid)
            before = self.redis("GET", detail)
            rows = json.loads(self.redis("EVAL_RO", SNAPSHOT_LUA, "0", json.dumps([{"job_id": jid}]), json.dumps(POOLS)))
            self.assertEqual([index], rows[0]["memberships"])
            self.assertEqual(before, self.redis("GET", detail))
            self.assertGreater(int(self.redis("TTL", detail)), 0)
        finally:
            self.redis("DEL", mapping, detail)
            self.redis("SREM", index, uid)


if __name__ == "__main__":
    unittest.main()
