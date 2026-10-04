#!/usr/bin/env python3
"""Run central contracts and existing Go package tests against exact source trees.

Always owns a disposable Redis container on a random loopback port. Overlays
redirect only test files' localhost:6379 references. Never uses a live Redis.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parents[1]
PACKAGES = {
    "controller": ["internal/redis", "internal/scheduler", "internal/handlers", "internal/pubsub", "internal/token", "internal/twirp"],
    "custom-mig": ["internal/state", "internal/reconciler", "internal/events", "internal/allocator"],
    "miglet": ["internal/handler", "internal/agent", "internal/nats", "internal/twirp", "internal/state", "internal/runner"],
}
RACE_PACKAGES = {"controller": ["internal/redis", "internal/handlers", "internal/scheduler"],
                 "custom-mig": ["internal/state", "internal/reconciler"], "miglet": ["internal/handler"]}


def command(args, **kwargs):
    return subprocess.check_output(args, text=True, timeout=60, **kwargs).strip()


def redirect_test_redis(text, port):
    text = text.replace("localhost:6379", f"localhost:{port}").replace("127.0.0.1:6379", f"127.0.0.1:{port}")
    return re.sub(r"(\bPort:\s*)6379\b", lambda m: m[1] + str(port), text)


def source_snapshot(source, dest):
    """Include local changes, but keep the original checkout untouched."""
    source = source.resolve(strict=True)
    files = command(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=source).split("\0")
    digest = hashlib.sha256()
    for relative in sorted(set(filter(None, files))):
        p = source / relative
        if not p.is_file():
            continue
        if p.is_symlink():
            raise ValueError(f"source symlink needs explicit review: {relative}")
        data = p.read_bytes()
        digest.update(relative.encode() + b"\0" + data + b"\0")
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        target.chmod(p.stat().st_mode & 0o777)
    return {"head": command(["git", "rev-parse", "HEAD"], cwd=source),
            "dirty": bool(command(["git", "status", "--porcelain"], cwd=source)),
            "tree_sha256": digest.hexdigest()}


def overlay_for(name, source, scratch, port):
    replacements = {}
    for original in source.rglob("*_test.go"):
        old = original.read_text()
        new = redirect_test_redis(old, port)
        if new != old:
            target = scratch / "ports" / original.relative_to(source)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(new)
            replacements[str(original)] = str(target)
    for fixture in (ROOT / "contracts" / name).rglob("*_test.go"):
        target = scratch / "contracts" / fixture.relative_to(ROOT / "contracts" / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(redirect_test_redis(fixture.read_text(), port))
        original = source / fixture.relative_to(ROOT / "contracts" / name)
        if original.exists():
            raise ValueError(f"contract path collides with source: {original}")
        replacements[str(original)] = str(target)
    path = scratch / "overlay.json"
    path.write_text(json.dumps({"Replace": replacements}))
    return path


def test_result(path, returncode):
    outcomes = {}
    package_results = []
    for line in path.read_text().splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        action = event.get("Action")
        if action in ("pass", "fail", "skip"):
            key = (event.get("Package"), event.get("Test"))
            if key[1]:
                outcomes[key] = action
            else:
                package_results.append(action)
    # Count leaves, rather than inflating totals with parent table-test names.
    leaves = {key: value for key, value in outcomes.items()
              if not any(other[0] == key[0] and other[1].startswith(key[1] + "/") for other in outcomes)}
    central = {key: value for key, value in leaves.items() if key[1].startswith("TestRegression")}
    return {"returncode": returncode, "passed": returncode == 0 and bool(central) and bool(package_results)
            and all(x == "pass" for x in package_results) and all(x == "pass" for x in central.values()),
            "central_cases": len(central), "central_passes": sum(v == "pass" for v in central.values()),
            "existing_cases": len(leaves) - len(central), "skips": sum(v == "skip" for v in leaves.values()),
            "failures": [f"{p}/{t}" for (p, t), v in leaves.items() if v == "fail"]}


def execute(args):
    args.output.mkdir(parents=True, exist_ok=False)
    report = {"started_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "runs": [], "sources": {}, "passed": False}
    container = "regression-redis-" + uuid.uuid4().hex[:12]
    started = False
    try:
        command(["docker", "run", "-d", "--name", container, "-p", "127.0.0.1::6379", "redis:7-alpine"])
        started = True
        port = int(json.loads(command(["docker", "inspect", container]))[0]["NetworkSettings"]["Ports"]["6379/tcp"][0]["HostPort"])
        if port in (6378, 16378):
            raise RuntimeError("disposable Redis collided with a reserved tunnel port")
        for _ in range(30):
            if command(["docker", "exec", container, "redis-cli", "ping"]) == "PONG":
                break
            time.sleep(.1)
        else:
            raise RuntimeError("disposable Redis did not become ready")
        with tempfile.TemporaryDirectory(prefix="monkci-contracts-") as temp:
            for name in args.component or PACKAGES:
                scratch = Path(temp) / name
                source = scratch / "source"
                report["sources"][name] = source_snapshot(getattr(args, name.replace("-", "_")), source)
                overlay = overlay_for(name, source, scratch, port)
                modes = [("normal", PACKAGES[name])]
                if args.race:
                    modes.append(("race", RACE_PACKAGES[name]))
                for mode, packages in modes:
                    path = args.output / f"{name}-{mode}.jsonl"
                    cmd = ["go", "test", "-json", "-count=1", "-p=1", "-timeout=5m", "-overlay", str(overlay)]
                    if mode == "race":
                        cmd.append("-race")
                    cmd.extend("./" + package for package in packages)
                    print(f"Testing {name} ({mode}) against {report['sources'][name]['head'][:12]}", flush=True)
                    # Unit test credentials are literals in test files. Do not forward live secrets.
                    env = {key: value for key, value in os.environ.items()
                           if key in ("PATH", "HOME", "TMPDIR", "GOPATH", "GOCACHE", "GOMODCACHE", "GOTOOLCHAIN", "SSL_CERT_FILE", "SSL_CERT_DIR")}
                    env["GOMAXPROCS"] = "2"
                    with path.open("w") as output:
                        try:
                            result = subprocess.run(cmd, cwd=source, env=env, stdout=output, stderr=subprocess.STDOUT, timeout=900)
                            code = result.returncode
                        except subprocess.TimeoutExpired:
                            code = 124
                    item = {"component": name, "mode": mode, **test_result(path, code)}
                    report["runs"].append(item)
                    print(json.dumps(item), flush=True)
        report["passed"] = len(report["runs"]) == len(args.component or PACKAGES) * (2 if args.race else 1) and all(r["passed"] for r in report["runs"])
    finally:
        if started:
            subprocess.run(["docker", "rm", "-f", container], check=True, stdout=subprocess.DEVNULL, timeout=30)
        (args.output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return 0 if report["passed"] else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in PACKAGES:
        parser.add_argument("--" + name, required=True, type=Path)
    parser.add_argument("--component", choices=tuple(PACKAGES), action="append", help="Run only selected components (default: all three)")
    parser.add_argument("--race", action="store_true", help="Also race-test the changed concurrency paths")
    parser.add_argument("--output", type=Path, default=Path(".source-contracts/run"))
    args = parser.parse_args()
    def interrupted(*_):
        raise RuntimeError("source contract runner interrupted")
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(execute(args))


if __name__ == "__main__":
    main()
