#!/usr/bin/env python3
"""Read-only staging reconciliation for a lifecycle_edges.py report.

Requires the documented staging tunnel (6443/16378/15432), kubectl, redis-cli,
Docker and gcloud. Credentials are read from Kubernetes, never written to evidence.
"""
import argparse
import base64
import json
import os
from pathlib import Path
import subprocess
import time

from lifecycle_edges import POOLS, REPO, utcnow

KUBECTL = ["kubectl", "--context", "gke_monkcidev_us-central1_monkci-non-prod-us-central1",
           "--server", "https://127.0.0.1:6443", "--tls-server-name", "172.16.0.2",
           "--namespace", "staging", "--request-timeout=20s"]

# One consistent, read-only Redis snapshot across all relevant job indexes.
# No SCAN of unrelated jobs and no mutation commands. Explicit keys work with
# the staging standalone Redis; EVAL_RO enforces the read-only contract (Redis 7).
SNAPSHOT_LUA = r"""
local result = {}
for _, item in ipairs(cjson.decode(ARGV[1])) do
  local marker = redis.call('GET', 'jobs:github_job_id:' .. tostring(item.job_id))
  local row = {job_id=item.job_id, marker=marker or cjson.null, memberships={}}
  if marker and string.sub(marker, 1, 10) ~= 'completed:' then
    local raw = redis.call('GET', 'jobs:details:' .. marker)
    if raw then row.job = cjson.decode(raw) end
    for _, pool in ipairs(cjson.decode(ARGV[2])) do
      local queue = 'jobs:queue:' .. pool
      if redis.call('ZSCORE', queue, marker) then table.insert(row.memberships, queue) end
      local pending = 'pending_allocation_requests:' .. pool
      if redis.call('SISMEMBER', pending, marker) == 1 then table.insert(row.memberships, pending) end
      for _, state in ipairs({'QUEUED','PENDING_ALLOCATION','VM_ALLOCATED','REGISTERING','ASSIGNED'}) do
        local key = 'jobs:by_schedule:' .. pool .. ':' .. state
        if redis.call('SISMEMBER', key, marker) == 1 then table.insert(row.memberships, key) end
      end
    end
    if row.job and row.job.assigned_vm_id and row.job.assigned_vm_id ~= '' then
      row.reverse_owner = redis.call('GET', 'jobs:by_vm:' .. row.job.assigned_vm_id) or cjson.null
    end
  end
  if item.runner_name and item.runner_name ~= '' then
    local raw = redis.call('GET', 'vm:' .. item.runner_name)
    if raw then row.actual_vm = cjson.decode(raw) end
    row.actual_vm_memberships = {}
    for _, pool in ipairs(cjson.decode(ARGV[2])) do
      for _, state in ipairs({'warm','busy','completed'}) do
        local key = 'pool:' .. string.gsub(pool, '^monkci%-', '') .. ':' .. state
        if redis.call('SISMEMBER', key, item.runner_name) == 1 then
          table.insert(row.actual_vm_memberships, key)
        end
      end
    end
  end
  table.insert(result, row)
end
return cjson.encode(result)
"""


def expected_jobs(report):
    if report.get("repository") != REPO or not report.get("passed") or not report.get("cases"):
        raise ValueError("require a passing, nonempty lifecycle report for the staging regression repository")
    result = {}
    for case in report["cases"]:
        for job in case.get("jobs", []):
            if job.get("name") != "probe":
                continue
            if job.get("status") != "completed" or not job.get("conclusion"):
                raise ValueError("report contains an unfinished job")
            job_id = job.get("id")
            if type(job_id) is not int or job_id <= 0:
                raise ValueError("invalid GitHub job ID")
            result[job_id] = {"job_id": job_id, "conclusion": job["conclusion"].upper(),
                              "runner_name": job.get("runner_name") or ""}
    if not result:
        raise ValueError("report has no probe jobs")
    return list(result.values())


def grade_snapshot(expected, redis_rows, pg_rows):
    errors = []
    redis = {r["job_id"]: r for r in redis_rows}
    postgres = {r["job_id"]: r for r in pg_rows}
    for item in expected:
        job_id, conclusion = item["job_id"], item["conclusion"]
        r, p = redis.get(job_id, {}), postgres.get(job_id, {})
        prefix = f"job {job_id}: "
        if str(p.get("status", "")).upper() != "COMPLETED" or str(p.get("conclusion", "")).upper() != conclusion:
            errors.append(prefix + "Postgres outcome missing or differs from GitHub")
        marker = r.get("marker") or ""
        if marker.startswith("completed:"):
            if marker[len("completed:"):].upper() != conclusion:
                errors.append(prefix + "completion tombstone differs from GitHub")
        else:
            job = r.get("job", {})
            if job.get("job_id") != job_id:
                errors.append(prefix + "Redis identity missing or mapped to a different GitHub job")
            if job.get("status") != "COMPLETED" or str(job.get("conclusion", "")).upper() != conclusion:
                errors.append(prefix + "Redis outcome missing or differs from GitHub")
            if job.get("assignment_receipt_pending") or job.get("recovery_exhausted"):
                errors.append(prefix + "completed job still has a pending receipt or remains parked")
            if marker and r.get("reverse_owner") == marker:
                errors.append(prefix + "completed job still owns its VM reverse mapping")
        if r.get("memberships"):
            errors.append(prefix + f"leftover scheduling demand/indexes: {r['memberships']}")
        # Check the runner GitHub actually used, not a VM merely intended for this
        # job: the latter may legitimately still be serving a different job.
        actual = r.get("actual_vm", {})
        if actual.get("state") in ("busy", "warm") or any(
                key.endswith((":busy", ":warm")) for key in r.get("actual_vm_memberships", [])):
            errors.append(prefix + "actual ephemeral runner VM was not retired")
    return errors


def command(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=60, **kwargs)
    if result.returncode:
        # Do not echo arguments, environments or raw stderr from credential-bearing clients.
        raise RuntimeError(f"{args[0]} failed (exit {result.returncode}); check staging access/tunnel")
    return result.stdout


class Staging:
    def __init__(self):
        command(["gcloud", "auth", "print-access-token"])
        raw = json.loads(command(KUBECTL + ["get", "secret", "mig-controller-secrets", "-o", "json"]))["data"]
        self.redis_env = dict(os.environ, REDISCLI_AUTH=base64.b64decode(raw["REDIS_JOBS_PASSWORD"]).decode())
        self.pg_env = dict(os.environ, PGPASSWORD=base64.b64decode(raw["POSTGRES_PASSWORD"]).decode())

    def deployment(self):
        pods = json.loads(command(KUBECTL + ["get", "pods", "-o", "json"]))
        return [{"pod": p["metadata"]["name"], "image": c.get("image"), "image_id": c.get("imageID")}
                for p in pods["items"] for c in p.get("status", {}).get("containerStatuses", [])
                if c["name"] == "mig-controller"]

    def snapshot(self, jobs):
        redis = command(["redis-cli", "--tls", "--insecure", "-h", "127.0.0.1", "-p", "16378",
                         "-n", "0", "--raw", "EVAL_RO", SNAPSHOT_LUA, "0", json.dumps(jobs), json.dumps(POOLS)],
                        env=self.redis_env)
        ids = ",".join(str(j["job_id"]) for j in jobs)  # validated positive integers
        sql = ("SELECT COALESCE(json_agg(t), '[]'::json) FROM "
               f"(SELECT job_id, status, conclusion FROM job_executions WHERE job_id IN ({ids})) t;")
        pg = command(["docker", "run", "--rm", "--network", "host", "-e", "PGPASSWORD",
                      "-e", "PGOPTIONS=-c default_transaction_read_only=on", "postgres:16-alpine",
                      "psql", "host=127.0.0.1 port=15432 dbname=staging_control_plane "
                      "user=staging-mig-controller-rw sslmode=require", "-X", "-A", "-t",
                      "-v", "ON_ERROR_STOP=1", "-c", sql], env=self.pg_env)
        return json.loads(redis), json.loads(pg)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("report", type=Path)
    parser.add_argument("--settle-seconds", type=int, default=180)
    parser.add_argument("--observe-seconds", type=int, default=660,
                        help="Continue after convergence to catch demand returning (default 11 minutes)")
    parser.add_argument("--interval", type=int, default=30)
    args = parser.parse_args()
    if args.settle_seconds < 0 or args.observe_seconds < 60 or not 5 <= args.interval <= 60:
        parser.error("settle must be nonnegative, observe >=60, interval 5..60")
    expected = expected_jobs(json.loads(args.report.read_text()))
    staging = Staging()
    evidence = {"started_at": utcnow(), "deployment": staging.deployment(), "samples": [], "passed": False}
    if not evidence["deployment"]:
        raise RuntimeError("no staging mig-controller pod image found")
    path = args.report.parent / "state-verdict.json"
    started = time.monotonic()
    converged = None
    try:
        while True:
            redis, pg = staging.snapshot(expected)
            errors = grade_snapshot(expected, redis, pg)
            now = time.monotonic()
            evidence["samples"].append({"at": utcnow(), "errors": errors, "redis": redis, "postgres": pg})
            path.write_text(json.dumps(evidence, indent=2) + "\n")
            print(f"{utcnow()}: {len(expected)} jobs; {len(errors)} reconciliation errors", flush=True)
            if errors:
                if converged is not None or now - started >= args.settle_seconds:
                    evidence["errors"] = errors
                    print("\n".join(errors), flush=True)
                    break
            elif converged is None:
                converged = now
                print("Converged; observing for returning demand and delayed VM cleanup failures", flush=True)
            elif now - converged >= args.observe_seconds:
                evidence["passed"] = True
                break
            time.sleep(args.interval)
    except Exception as exc:
        evidence["errors"] = [str(exc)]
    finally:
        evidence["ended_at"] = utcnow()
        path.write_text(json.dumps(evidence, indent=2) + "\n")
    return 0 if evidence["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
