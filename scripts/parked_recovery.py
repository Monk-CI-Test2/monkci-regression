#!/usr/bin/env python3
"""Force a job to be PARKED with no job_executions row, then prove it resumes.

Exercises the controller paths that ordinary traffic never reaches:

  1. allocation exhaustion -> park             (job_parked_after_scheduling_exhaustion)
  2. parked job has NO job_executions row       (row is only created on the first
                                                 RUNNER_REGISTERED; nothing ever
                                                 registered for this job)
  3. resume from the webhook-fed Postgres view  (parked_job_resumed, resume_count=1,
                                                 no GitHub REST call anywhere)
  4. the resumed job runs and completes; Redis and Postgres agree with GitHub;
     the total number of VM claims stays within the documented bound.

Two ways to force the park (--mode):

  allocation (default): the pool's existing warm VMs are deleted (the script
    waits until the controller no longer lists them warm) and a VPC firewall
    rule denies egress to NATS for the pool VMs' service account, so every VM
    custom-mig boots afterwards fails its NATS connect, never reports READY and
    is never claimable (no per-VM race: the rule precedes the boot). The job's
    allocation times out (assignment_timeout) max_retries times and it is parked by
    parkAfterSchedulingExhaustion.
  registration: one warm READY VM is starved of NATS while it stays READY, so the
    job is bound to it and the register-runner command is never answered. Each
    registration-lock timeout (assignment_timeout) is counted as a retry; after
    max_retries the job is parked and the silent VM retired (the bound added to
    recoverStalledRegistrationsForPool - before it, this looped for 24 hours).
    The same firewall rule keeps every new VM unclaimable until the park.

In both modes the parked job has no job_executions row (nothing registered). The
firewall rule is then deleted and the miglets restarted, VMs come up READY, and
the resume is watched. The rule affects every staging pool while it exists.

Staging only. Deletes the pool's warm VMs and holds the ones booted during the
~20 minute starvation silent (they are healed, not replaced). Runtime 25-35 minutes. Exit codes: 0 PASS, 1 FAIL, 3 INCONCLUSIVE
(the job got served before it could be parked - just re-run).

Requires the documented staging tunnel (6443/16378/15432), gcloud with IAP SSH to
pool VMs, kubectl, redis-cli, Docker and gh. Credentials are read from Kubernetes.
"""
import argparse
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode

from lifecycle_edges import REPO, GitHub, utcnow
from verify_lifecycle_state import KUBECTL, Staging, command

PROJECT = "monkcidev"
NS = "staging"
NATS_PORT = 4222
NATS_HOST = "10.10.0.10"                       # nats.nonprod.monkci.com
VPC_FALLBACK = "default"                       # pool VMs live in the project's default VPC
POOL_VM_SA = "miglet-sa-staging@monkcidev.iam.gserviceaccount.com"
FIREWALL_PREFIX = "monkci-regression-starve-nats-"
# Controller defaults on staging (no overrides in the chart). The grading uses
# them only to compute budgets and the claim bound; the pass/fail signals come
# from what the controller actually logs and stores.
MAX_RETRIES = 3                 # redis.Job.MaxRetries; park on the (MAX_RETRIES+1)th allocation timeout
ASSIGNMENT_TIMEOUT_S = 300      # scheduler.assignment_timeout
PARKED_RESUME_DELAY_S = 300
MAX_ASSIGNMENT_RECOVERIES = 3

STARVE_SH = r"""
set -e
sudo iptables -C INPUT  -p tcp --sport %(port)d -j DROP 2>/dev/null || sudo iptables -I INPUT  -p tcp --sport %(port)d -j DROP
sudo iptables -C OUTPUT -p tcp --dport %(port)d -j DROP 2>/dev/null || sudo iptables -I OUTPUT -p tcp --dport %(port)d -j DROP
echo starved
""" % {"port": NATS_PORT}

HEAL_SH = r"""
while sudo iptables -D INPUT  -p tcp --sport %(port)d -j DROP 2>/dev/null; do :; done
while sudo iptables -D OUTPUT -p tcp --dport %(port)d -j DROP 2>/dev/null; do :; done
sudo systemctl restart --no-block miglet
echo healed
""" % {"port": NATS_PORT}


def log(msg):
    print(f"{datetime.now(timezone.utc).strftime('%H:%M:%S')} {msg}", flush=True)


def parse_ts(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


class Pool:
    def __init__(self, label):
        if not label.startswith("monkci-ubuntu-24.04-"):
            raise ValueError("only monkci-ubuntu-24.04-<n> pools are supported")
        self.label = label                                   # GitHub label / jobs:* keys
        self.id = label[len("monkci-"):]                     # pool:<id>:warm etc.
        self.prefix = label.replace("monkci-", "monkci--", 1).replace("24.04", "24-04") + "--"


class Redis:
    def __init__(self, staging):
        self.env = staging.redis_env

    def __call__(self, *args):
        return command(["redis-cli", "--tls", "--insecure", "--no-auth-warning", "-h", "127.0.0.1",
                        "-p", "16378", "--raw", *args], env=self.env).rstrip("\n")

    def job(self, internal_id):
        raw = self("GET", f"jobs:details:{internal_id}")
        return json.loads(raw) if raw else None

    def warm(self, pool):
        raw = self("SMEMBERS", f"pool:{pool.id}:warm")
        return set(raw.split("\n")) - {""} if raw else set()

    def vm(self, vm_id):
        raw = self("GET", f"vm:{vm_id}")
        return json.loads(raw) if raw else None


class Postgres:
    def __init__(self, staging):
        self.env = staging.pg_env

    def execution(self, github_job_id):
        sql = ("SELECT COALESCE(json_agg(t), '[]'::json) FROM (SELECT job_id, status, conclusion, "
               "assigned_vm_id, started_at, completed_at FROM job_executions "
               f"WHERE job_id = {int(github_job_id)}) t;")
        rows = json.loads(command(
            ["docker", "run", "--rm", "--network", "host", "-e", "PGPASSWORD",
             "-e", "PGOPTIONS=-c default_transaction_read_only=on", "postgres:16-alpine",
             "psql", "host=127.0.0.1 port=15432 dbname=staging_control_plane "
             "user=staging-mig-controller-rw sslmode=require", "-X", "-A", "-t",
             "-v", "ON_ERROR_STOP=1", "-c", sql], env=self.env))
        return rows[0] if rows else None


class Vms:
    """Pool VMs over the GCE API and IAP SSH."""

    def __init__(self, pool):
        self.pool = pool
        self.lock = threading.Lock()
        self.touched = {}      # vm -> zone, every VM we armed (healed on exit)

    def running(self):
        out = command(["gcloud", "compute", "instances", "list", "--project", PROJECT, "--format", "json",
                       "--filter", f"name~'^{self.pool.prefix}' AND status=RUNNING AND "
                                   f"metadata.items.environment={NS}"])
        return {i["name"]: i["zone"].rsplit("/", 1)[-1] for i in json.loads(out)}

    def ssh(self, vm, zone, script, timeout=90):
        result = subprocess.run(
            ["gcloud", "compute", "ssh", vm, "--project", PROJECT, "--zone", zone, "--tunnel-through-iap",
             "--quiet", "--ssh-flag=-o ConnectTimeout=20", "--ssh-flag=-o ServerAliveInterval=15",
             "--command", script], text=True, capture_output=True, timeout=timeout)
        return result.returncode, (result.stdout or "").strip()

    def starve(self, vm, zone, attempts=8):
        for attempt in range(attempts):
            try:
                rc, out = self.ssh(vm, zone, STARVE_SH)
            except subprocess.TimeoutExpired:
                rc, out = 1, "timeout"
            if rc == 0 and "starved" in out:
                with self.lock:
                    self.touched[vm] = zone
                return True
            time.sleep(8)   # sshd is not up for the first ~20 s of a boot
        return False

    def heal(self, vm, zone):
        try:
            rc, out = self.ssh(vm, zone, HEAL_SH)
            return rc == 0 and "healed" in out
        except subprocess.TimeoutExpired:
            return False

    def delete(self, vm, zone):
        subprocess.run(["gcloud", "compute", "instances", "delete", vm, "--project", PROJECT, "--zone", zone,
                        "--quiet"], text=True, capture_output=True, timeout=180)

    def network(self):
        """The VPC the pool VMs actually use, read from a live VM (fallback: default)."""
        out = command(["gcloud", "compute", "instances", "list", "--project", PROJECT, "--limit", "1",
                       "--filter", f"name~'^{self.pool.prefix}' AND metadata.items.environment={NS}",
                       "--format", "value(networkInterfaces[0].network.basename())"]).strip()
        return out or VPC_FALLBACK

    def starve_network(self, name):
        vpc = self.network()
        log(f"  pool VMs use VPC {vpc}")
        subprocess.run(["gcloud", "compute", "firewall-rules", "create", name, "--project", PROJECT,
                        "--network", vpc, "--direction", "EGRESS", "--action", "DENY", "--rules", f"tcp:{NATS_PORT}",
                        "--destination-ranges", f"{NATS_HOST}/32", "--target-service-accounts", POOL_VM_SA,
                        "--priority", "100", "--quiet"], text=True, capture_output=True, timeout=120, check=True)

    def unstarve_network(self, prefix=FIREWALL_PREFIX):
        out = command(["gcloud", "compute", "firewall-rules", "list", "--project", PROJECT,
                       "--filter", f"name~'^{prefix}'", "--format", "value(name)"])
        deleted = []
        for name in out.split():
            subprocess.run(["gcloud", "compute", "firewall-rules", "delete", name, "--project", PROJECT, "--quiet"],
                           text=True, capture_output=True, timeout=120)
            deleted.append(name)
        return deleted

    def restart_miglet(self, vm, zone):
        try:
            rc, out = self.ssh(vm, zone, HEAL_SH)
            return rc == 0 and "healed" in out
        except subprocess.TimeoutExpired:
            return False

    def heal_all(self):
        rules = self.unstarve_network()
        with self.lock:
            targets = dict(self.touched)
        alive = self.running()
        healed = []
        for vm, zone in {**alive, **{v: z for v, z in targets.items() if v in alive}}.items():
            if self.heal(vm, zone):
                healed.append(vm)
        return healed, [vm for vm in targets if vm not in alive], rules


class Watcher(threading.Thread):
    """Records every pool VM that boots during the experiment (for the report)."""

    def __init__(self, vms, skip=(), interval=15):
        super().__init__(daemon=True)
        self.vms, self.interval = vms, interval
        self.stop_event = threading.Event()
        self.seen = set()
        self.skip = set(skip)

    def run(self):
        while not self.stop_event.is_set():
            try:
                for vm in self.vms.running():
                    if vm not in self.seen and vm not in self.skip:
                        self.seen.add(vm)
                        log(f"  booted {vm}")
            except Exception as exc:
                log(f"  watcher: {exc}")
            self.stop_event.wait(self.interval)


class Controller:
    def __init__(self, since):
        self.since = since

    def lines(self, internal_id):
        flt = (f'resource.labels.namespace_name="{NS}" AND resource.labels.container_name="mig-controller" '
               f'AND jsonPayload.job_id="{internal_id}" AND timestamp>="{self.since}"')
        out = command(["gcloud", "logging", "read", "--project", PROJECT, "--order", "asc", "--limit", "500",
                       "--format", "json", flt])
        rows = []
        for entry in json.loads(out or "[]"):
            p = entry.get("jsonPayload", {})
            rows.append({"time": entry.get("timestamp"), "message": p.get("message"), "event": p.get("event"),
                         "vm_id": p.get("vm_id"), "recovery_count": p.get("recovery_count"),
                         "resume_count": p.get("resume_count")})
        return rows


class Experiment:
    def __init__(self, args):
        self.args = args
        self.pool = Pool(args.pool)
        self.gh = GitHub()
        self.staging = Staging()
        self.redis = Redis(self.staging)
        self.pg = Postgres(self.staging)
        self.vms = Vms(self.pool)
        self.watcher = None
        self.started = utcnow()
        self.ctl = Controller(self.started)
        self.bait = None
        self.suite = f"park-{args.mode[:5]}-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}"
        self.rule = FIREWALL_PREFIX + self.suite.replace("park-", "")
        self.report = {"suite_id": self.suite, "mode": args.mode, "pool": self.pool.label, "started": self.started,
                       "controller": self.staging.deployment(), "checks": [], "events": []}
        self.fails = 0

    # ---- reporting --------------------------------------------------------
    def check(self, ok, what):
        self.report["checks"].append({"ok": bool(ok), "what": what})
        log(f"  {'PASS' if ok else 'FAIL'}: {what}")
        if not ok:
            self.fails += 1

    def save(self):
        self.report["updated"] = utcnow()
        self.args.report.write_text(json.dumps(self.report, indent=2))

    # ---- GitHub -----------------------------------------------------------
    def find_run(self, title):
        for page in range(1, 4):
            query = urlencode({"event": "workflow_dispatch", "branch": self.args.ref, "per_page": 100, "page": page})
            runs = self.gh.api(f"actions/workflows/regression-target.yml/runs?{query}")["workflow_runs"]
            matches = [r for r in runs if r["display_title"] == title and r["head_branch"] == self.args.ref]
            if len(matches) > 1:
                raise RuntimeError(f"ambiguous dispatch: {title}")
            if matches:
                return matches[0]["id"]
            if len(runs) < 100:
                break
        return None

    def dispatch(self):
        title = f"target/{self.suite}/probe/success"
        self.gh.api("actions/workflows/regression-target.yml/dispatches", "POST", {
            "ref": self.args.ref,
            "inputs": {"suite_id": self.suite, "case_id": "probe", "scenario": "success",
                       "runner_label": self.pool.label, "hold_seconds": "0"}})
        for _ in range(24):
            run_id = self.find_run(title)
            if run_id:
                break
            time.sleep(5)
        else:
            raise TimeoutError("could not resolve the dispatched run")
        for _ in range(24):
            jobs = [j for j in self.gh.jobs(run_id, 1) if j.get("name") == "probe"]
            if jobs:
                return run_id, jobs[0]["id"]
            time.sleep(5)
        raise TimeoutError("probe job never appeared on the run")

    def github_job(self, job_id):
        return self.gh.api(f"actions/jobs/{job_id}")

    # ---- phases -----------------------------------------------------------
    def run(self):
        log(f"suite {self.suite} pool {self.pool.label} controller {self.report['controller']}")
        self.check(self.redis("PING") == "PONG", "staging Redis reachable through the tunnel")
        stale = self.vms.unstarve_network()
        if stale:
            log(f"  removed stale starvation rules from an earlier run: {stale}")

        existing = self.vms.running()
        if self.args.mode == "registration":
            log("phase 1: starve one READY warm VM (it stays claimable); starve every VM that boots from now on")
            warm = [vm for vm in self.redis.warm(self.pool) if vm in existing
                    and (self.redis.vm(vm) or {}).get("migletState") == "ready"]
            self.check(len(warm) >= 1, f"a READY warm VM exists to bind the job to ({warm})")
            if not warm:
                return self.finish()
            self.bait = warm[0]
            for vm in warm[1:]:
                log(f"  deleting surplus warm VM {vm}")
                self.vms.delete(vm, existing[vm])
            self.check(self.vms.starve(self.bait, existing[self.bait]), f"bait VM {self.bait} starved of NATS while READY")
            self.vms.starve_network(self.rule)
            log(f"  firewall rule {self.rule} denies NATS egress for {POOL_VM_SA}")
            self.watcher = Watcher(self.vms, skip=set(existing))
            self.watcher.start()
        else:
            log("phase 1: deny NATS egress for the pool VMs' service account; delete the pool's warm VMs")
            self.vms.starve_network(self.rule)
            log(f"  firewall rule {self.rule} denies NATS egress for {POOL_VM_SA}")
            self.watcher = Watcher(self.vms, skip=set(existing))
            self.watcher.start()
            for vm, zone in existing.items():
                log(f"  deleting {vm} ({zone})")
                self.vms.delete(vm, zone)
            deadline = time.time() + 6 * 60
            while time.time() < deadline:
                left = self.redis.warm(self.pool) & set(existing)
                alive = set(self.vms.running()) & set(existing)
                if not left and not alive:
                    break
                time.sleep(10)
            self.check(not left and not alive, f"controller lists none of the deleted VMs as warm ({sorted(left | alive)} left)")

        log("phase 2: dispatch the probe")
        run_id, gh_job_id = self.dispatch()
        self.report.update({"run_id": run_id, "github_job_id": gh_job_id,
                            "run_url": f"https://github.com/{REPO}/actions/runs/{run_id}"})
        log(f"  {self.report['run_url']} job {gh_job_id}")
        internal = None
        for _ in range(36):
            internal = self.redis("GET", f"jobs:github_job_id:{gh_job_id}")
            if internal and not internal.startswith("completed:"):
                break
            time.sleep(5)
        self.check(bool(internal) and not internal.startswith("completed:"), f"controller enqueued the job ({internal})")
        self.report["internal_id"] = internal
        self.save()
        if not internal or internal.startswith("completed:"):
            return self.finish()

        log(f"phase 3: wait for the park (up to {self.args.park_budget}s; expect ~{(MAX_RETRIES+1)*ASSIGNMENT_TIMEOUT_S//60} min)")
        parked_at, job = self.wait(self.args.park_budget, 15, lambda j: j.get("recovery_exhausted") is True, internal)
        if job and job.get("status") == "COMPLETED":
            log("  the job completed before it could be parked: a healthy VM won a claim")
            self.report["verdict"] = "INCONCLUSIVE"
            self.save()
            return 3
        self.check(parked_at is not None, "job parked after scheduling exhaustion (recovery_exhausted=true)")
        if parked_at is None:
            return self.finish(internal)
        self.report["parked_at"] = parked_at
        self.check(job.get("retry_count") == MAX_RETRIES,
                   f"retry_count == max_retries ({job.get('retry_count')} == {MAX_RETRIES})")
        if self.args.mode == "registration":
            self.check(job.get("schedule_status") == "ASSIGNED", "parked job sits in the ASSIGNED index")
            bait_vm = self.redis.vm(self.bait) or {}
            self.check(bait_vm.get("state") in (None, "completed"),
                       f"the silent bait VM was retired ({self.bait}: {bait_vm.get('state')})")
        else:
            self.check(job.get("schedule_status") == "ASSIGNED" and not job.get("assigned_vm_id"),
                       "parked job sits in the ASSIGNED index with no VM bound (never allocated)")
        row = self.pg.execution(gh_job_id)
        self.check(row is None, f"no job_executions row exists for the parked job (nothing ever registered): {row}")
        gh = self.github_job(gh_job_id)
        self.check(gh.get("status") == "queued", f"GitHub still shows the job queued ({gh.get('status')})")
        self.save()

        log("phase 4: lift the starvation, then wait for the resume")
        self.watcher.stop_event.set()
        self.watcher.join(timeout=60)
        healed, gone, rules = self.vms.heal_all()
        log(f"  deleted {rules}; restarted miglet on {healed}; already retired {gone}")
        resumed_at, job = self.wait(self.args.resume_budget, 15,
                                    lambda j: (j.get("resume_count") or 0) >= 1 or j.get("status") == "COMPLETED", internal)
        self.check(resumed_at is not None and (job.get("resume_count") or 0) >= 1,
                   f"parked job resumed from the Postgres view (resume_count={job.get('resume_count') if job else None})")
        if resumed_at:
            waited = (parse_ts(resumed_at) - parse_ts(parked_at)).total_seconds()
            self.check(waited >= PARKED_RESUME_DELAY_S - 30,
                       f"resume honoured the {PARKED_RESUME_DELAY_S}s delay (parked->resumed {waited:.0f}s)")
            self.report["resumed_at"] = resumed_at

        log("phase 5: the resumed job must run and complete")
        done_at, job = self.wait(self.args.complete_budget, 15, lambda j: j.get("status") == "COMPLETED", internal)
        gh = self.github_job(gh_job_id)
        self.check(gh.get("status") == "completed" and gh.get("conclusion") == "success",
                   f"GitHub job completed/success ({gh.get('status')}/{gh.get('conclusion')} on {gh.get('runner_name')})")
        self.check(job is not None and job.get("status") == "COMPLETED" and job.get("conclusion") == "SUCCESS",
                   f"Redis record COMPLETED/SUCCESS ({(job or {}).get('status')}/{(job or {}).get('conclusion')})")
        self.check(job is not None and not job.get("recovery_exhausted"), "Redis record is no longer parked")
        row = self.pg.execution(gh_job_id)
        self.check(row is not None and row.get("status") == "COMPLETED" and row.get("conclusion") == "success",
                   f"Postgres job_executions COMPLETED/success ({row})")
        runner = gh.get("runner_name") or ""
        self.check(runner.startswith(self.pool.prefix), f"ran on a pool VM ({runner})")
        return self.finish(internal)

    def wait(self, budget, step, predicate, internal):
        deadline = time.time() + budget
        job = None
        while time.time() < deadline:
            job = self.redis.job(internal)
            if job and predicate(job):
                return utcnow(), job
            if job and job.get("status") == "COMPLETED":
                return None, job
            time.sleep(step)
        return None, job

    def finish(self, internal=None):
        if internal:
            lines = self.ctl.lines(internal)
            self.report["events"] = lines
            claims = [l for l in lines if l["message"] == "Claimed MIGlet-ready VM and assigned to job"]
            events = [l["event"] for l in lines if l["event"]]
            bound = (MAX_ASSIGNMENT_RECOVERIES + 1) * 2
            self.check(0 < len(claims) <= bound, f"VM claims within bound ({len(claims)} <= {bound})")
            if self.args.mode == "registration":
                unlocks = [l for l in lines if l["message"] == "Registration lock timed out; returned job to VM_ALLOCATED for retry"]
                self.check(len(unlocks) == MAX_RETRIES, f"registration lock timed out max_retries times before parking ({len(unlocks)} == {MAX_RETRIES})")
                # The silent VM is retired either by the registration bound itself or,
                # if the 10-minute watchdog fires first, by the busy-VM release.
                retired = any(l["message"] == "Retired VM whose runner registration never completed" for l in lines)
                watchdog = any(l["event"] == "busy_vm_released_oom_restart" and l["vm_id"] == self.bait for l in lines)
                self.check(retired or watchdog,
                           f"controller retired the silent VM (registration bound={retired}, watchdog={watchdog})")
            else:
                requeues = [l for l in lines if l["message"] == "VM allocation timed out, requeued"]
                self.check(len(requeues) == MAX_RETRIES, f"allocation retried max_retries times before parking ({len(requeues)} == {MAX_RETRIES})")
            self.check("job_parked_after_scheduling_exhaustion" in events, "controller logged job_parked_after_scheduling_exhaustion")
            self.check("parked_job_resumed" in events, "controller logged parked_job_resumed")
            self.check("job_parked_final" not in events, "controller never gave up on the job (no job_parked_final)")
            log("  controller timeline:")
            for l in lines:
                if l["message"] in ("Refusing GetConfig for completed VM pending deletion",):
                    continue
                log(f"    {l['time'][11:19]} {l['message']} {l['event'] or ''} {l['vm_id'] or ''}")
        verdict = "PASS" if self.fails == 0 else "FAIL"
        self.report["verdict"] = verdict
        self.report["finished"] = utcnow()
        self.save()
        log(f"{verdict}: {sum(c['ok'] for c in self.report['checks'])}/{len(self.report['checks'])} checks; "
            f"report {self.args.report}")
        summary = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary:
            with open(summary, "a") as fh:
                fh.write(f"## parked-recovery {self.suite}: {verdict}\n\n| check | result |\n|---|---|\n")
                for c in self.report["checks"]:
                    fh.write(f"| {c['what']} | {'PASS' if c['ok'] else 'FAIL'} |\n")
        return 0 if verdict == "PASS" else 1

    def cleanup(self):
        if self.watcher:
            self.watcher.stop_event.set()
        healed, gone, rules = self.vms.heal_all()
        log(f"cleanup: deleted {rules}; restarted miglet on {healed}; retired {gone}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--pool", default="monkci-ubuntu-24.04-4")
    parser.add_argument("--mode", choices=("allocation", "registration"), default="allocation")
    parser.add_argument("--ref", default="main", help="branch of this repo whose regression-target.yml is dispatched")
    parser.add_argument("--park-budget", type=int, default=32 * 60)
    parser.add_argument("--resume-budget", type=int, default=9 * 60)
    parser.add_argument("--complete-budget", type=int, default=10 * 60)
    parser.add_argument("--report", type=Path, default=Path("parked-recovery-report.json"))
    args = parser.parse_args()

    exp = Experiment(args)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(130))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
    try:
        return exp.run()
    finally:
        exp.cleanup()


if __name__ == "__main__":
    sys.exit(main())
