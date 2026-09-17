#!/usr/bin/env python3
"""Reconcile GitHub, Redis and Postgres for every pool job of a time window.

For each workflow run of this repository created since --since, every job that
carried a MonkCI pool label is looked up in the controller's Redis (record, index
memberships, VM ownership) and in job_executions, and graded:

  ok               GitHub, Redis and Postgres all terminal and agree
  redis_tombstone  GitHub finished a job the controller never enqueued
                   (skipped, or cancelled before the queued webhook) - expected
  bad              anything else, with the reasons

Run it after any suite; the suites print their UTC window. Read-only.

Requires the documented staging tunnel (6443/16378/15432), kubectl, redis-cli,
Docker and gh. Credentials are read from Kubernetes.
"""
import argparse
import collections
import json
import subprocess
import sys

from lifecycle_edges import REPO
from verify_lifecycle_state import Staging, command

LUA = r"""
local out = {}
for _, id in ipairs(ARGV) do
  local row = {gh = id}
  local idx = redis.call('GET', 'jobs:github_job_id:' .. id)
  row.index = idx
  if idx and string.sub(idx, 1, 10) ~= 'completed:' then
    local data = redis.call('GET', 'jobs:details:' .. idx)
    if data then
      local j = cjson.decode(data)
      row.status = j.status; row.conclusion = j.conclusion; row.sched = j.schedule_status
      row.receipt = j.assignment_receipt_pending; row.exhausted = j.recovery_exhausted
      row.vm = j.assigned_vm_id; row.label = j.label
      row.in_assigned = redis.call('SISMEMBER', 'jobs:by_schedule:' .. j.label .. ':ASSIGNED', idx)
      if type(j.assigned_vm_id) == 'string' and j.assigned_vm_id ~= '' then
        row.byvm = redis.call('GET', 'jobs:by_vm:' .. j.assigned_vm_id)
      end
    else
      row.status = 'NO_DETAILS'
    end
  end
  table.insert(out, row)
end
return cjson.encode(out)
"""

CONCLUSION_MAP = {"success": "SUCCESS", "failure": "FAILURE", "cancelled": "CANCELLED",
                  "timed_out": "TIMED_OUT", "skipped": "SKIPPED"}


def gh(args):
    return json.loads(subprocess.check_output(["gh", *args], text=True))


def pool_jobs(since, limit):
    runs = gh(["run", "list", "--repo", REPO, "--limit", str(limit), "--json", "databaseId,name,createdAt"])
    runs = [r for r in runs if r["createdAt"] >= since]
    jobs = []
    for r in runs:
        page = gh(["api", f"repos/{REPO}/actions/runs/{r['databaseId']}/jobs?per_page=100"])
        for j in page["jobs"]:
            if any(str(l).startswith("monkci-") for l in (j.get("labels") or [])):
                j["_run"] = r["name"]
                jobs.append(j)
    return runs, jobs


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--since", required=True, help="RFC3339 UTC, e.g. 2026-09-16T15:39:40Z")
    parser.add_argument("--limit", type=int, default=100, help="how many recent runs to scan")
    parser.add_argument("--json", action="store_true", help="print the per-job rows as JSON")
    args = parser.parse_args()

    runs, jobs = pool_jobs(args.since, args.limit)
    print(f"runs={len(runs)} pool_jobs={len(jobs)} since {args.since}")
    if not jobs:
        return 0
    print("GitHub conclusions:", dict(collections.Counter(j.get("conclusion") for j in jobs)))

    staging = Staging()
    ids = [str(int(j["id"])) for j in jobs]
    raw = command(["redis-cli", "--tls", "--insecure", "--no-auth-warning", "-h", "127.0.0.1", "-p", "16378",
                   "--raw", "EVAL", LUA, "0", *ids], env=staging.redis_env)
    redis_rows = {r["gh"]: r for r in json.loads(raw)}
    sql = ("SELECT COALESCE(json_agg(t), '[]'::json) FROM (SELECT job_id, status, conclusion, completed_at "
           f"FROM job_executions WHERE job_id IN ({','.join(ids)})) t;")
    pg_rows = {str(p["job_id"]): p for p in json.loads(command(
        ["docker", "run", "--rm", "--network", "host", "-e", "PGPASSWORD",
         "-e", "PGOPTIONS=-c default_transaction_read_only=on", "postgres:16-alpine",
         "psql", "host=127.0.0.1 port=15432 dbname=staging_control_plane "
         "user=staging-mig-controller-rw sslmode=require", "-X", "-A", "-t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        env=staging.pg_env))}

    summary = collections.Counter()
    bad = []
    rows = []
    for j in jobs:
        gid = str(int(j["id"]))
        r, p = redis_rows.get(gid, {}), pg_rows.get(gid)
        problems = []
        if j.get("status") != "completed":
            problems.append(f"github_{j.get('status')}")
        if (r.get("index") or "").startswith("completed:"):
            grade = "redis_tombstone"
        else:
            if r.get("status") != "COMPLETED":
                problems.append(f"redis_status={r.get('status')}")
            want = CONCLUSION_MAP.get(j.get("conclusion"))
            if want and r.get("conclusion") != want:
                problems.append(f"redis_conclusion={r.get('conclusion')} (gh={j.get('conclusion')})")
            if r.get("receipt"):
                problems.append("receipt_still_pending")
            if r.get("exhausted"):
                problems.append("still_parked")
            if r.get("in_assigned") == 1:
                problems.append("still_in_ASSIGNED_index")
            if r.get("byvm") and r.get("byvm") == r.get("index"):
                problems.append("jobs:by_vm_still_points_here")
            if p is None:
                problems.append("no_postgres_row")
            else:
                if p.get("status") != "COMPLETED":
                    problems.append(f"pg_status={p.get('status')}")
                if p.get("conclusion") != j.get("conclusion"):
                    problems.append(f"pg_conclusion={p.get('conclusion')}")
            grade = "ok" if not problems else "bad"
        summary[grade] += 1
        row = {"github_job_id": gid, "run": j["_run"], "job": j.get("name"), "github": j.get("conclusion"),
               "grade": grade, "problems": problems}
        rows.append(row)
        if grade == "bad":
            bad.append(row)

    print("Audit:", dict(summary))
    for b in bad:
        print(f"  BAD {b['github_job_id']} {b['run'][:40]} {b['job']} gh={b['github']} {b['problems']}")
    if args.json:
        print(json.dumps(rows, indent=2))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
