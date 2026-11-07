"""Two hosts, one repository on a real NFS export, one clock skewed: do gate's locks and queue hold up?

Host A and host B are privileged docker containers with different hostnames, each in its own network
namespace and each mounting the export itself, so the server sees two separate NFSv4 clients (own client
id, own attribute cache, own delegations), as it would see two machines. Host B's clock runs SKEW_S ahead
of A's (libfaketime). The checks:

1. live lock:  A holds a lock and keeps heartbeating. B tries to take it for longer than LOCK_STALE_S and
               must fail, although by B's clock the lock file looks SKEW_S old (a timestamp-comparison
               rule would break it).
2. dead lock:  A is killed (`docker kill`, no cleanup). B takes the lock over after LOCK_STALE_S of silence.
3. queue:      `gate queue --drain` runs on A and B at the same time over shared lanes; every session lands
               exactly once.

Needs Linux, docker, and an NFS server exporting NFS_EXPORT to the docker bridge network
(see .github/workflows/nfs.yml). Writes a Markdown summary to $GITHUB_STEP_SUMMARY when set.
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

IMAGE = "gate-nfs-test"
LOCK_STALE_S = 20
SKEW_S = 120
SESSIONS = 8
SRC = Path(__file__).resolve().parent
FAKETIME_LIB = "/usr/lib/x86_64-linux-gnu/faketime/libfaketime.so.1"
NFS_EXPORT = os.environ.get("NFS_EXPORT", "172.17.0.1:/srv/nfs/shared")
HOSTS = {
    "host-a": {},
    "host-b": {"LD_PRELOAD": FAKETIME_LIB, "FAKETIME": f"+{SKEW_S}s", "NO_FAKE_STAT": "1"},
}
DOCKERFILE = """\
FROM python:3.12-slim
RUN apt-get update -q && apt-get install -y -q --no-install-recommends git faketime procps nfs-common \\
 && rm -rf /var/lib/apt/lists/*
RUN git config --global user.name gate-nfs && git config --global user.email nfs@example.com \\
 && git config --global init.defaultBranch main && git config --global --add safe.directory '*'
"""

HOLD = """\
import sys, time
sys.path.insert(0, "/src")
from pathlib import Path
import gate
with gate.Gate(Path("/work/repo")).lock("probe"):
    print("held", flush=True)
    time.sleep(float(sys.argv[1]))
"""

CONTEND = """\
import json, os, sys, time
sys.path.insert(0, "/src")
from pathlib import Path
import gate
instance = gate.Gate(Path("/work/repo"))
lock = instance.state / "probe.lock"
naive_age = time.time() - os.stat(lock).st_mtime if lock.exists() else None
started = time.monotonic()
try:
    with instance.lock("probe", timeout_s=float(sys.argv[1])):
        result = "acquired"
except gate.GateError:
    result = "blocked"
print(json.dumps({"result": result, "elapsed": round(time.monotonic() - started, 1),
                  "naive_age": None if naive_age is None else round(naive_age, 1)}))
"""


def run(*cmd: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if check and proc.returncode != 0:
        raise SystemExit(f"FAILED: {' '.join(cmd)}\n{proc.stdout}\n{proc.stderr}")
    return proc


def on(host: str, script: str, check: bool = True, timeout: float = 600) -> subprocess.CompletedProcess:
    return run("docker", "exec", host, "bash", "-c", script, check=check, timeout=timeout)


def start(host: str) -> None:
    env = {"GATE_LOCK_STALE_S": str(LOCK_STALE_S), **HOSTS[host]}
    flags = [arg for key, value in env.items() for arg in ("-e", f"{key}={value}")]
    # The container mounts the export itself (its own NFS client) and only then idles. LD_PRELOAD is
    # cleared for mount so the faked clock cannot interfere with it.
    boot = f"mkdir -p /work && env -u LD_PRELOAD mount -t nfs -o vers=4.2 {NFS_EXPORT} /work && exec sleep infinity"
    run("docker", "run", "-d", "--privileged", "--name", host, "--hostname", host,
        "-v", f"{SRC}:/src:ro", *flags, IMAGE, "bash", "-c", boot)
    wait_for_mount(host)


def wait_for_mount(host: str) -> None:
    deadline = time.monotonic() + 60
    while on(host, "mountpoint -q /work", check=False).returncode != 0:
        if time.monotonic() > deadline:
            logs = run("docker", "logs", host, check=False)
            raise SystemExit(f"{host} could not mount {NFS_EXPORT}:\n{logs.stdout}{logs.stderr}")
        time.sleep(0.5)


def check(condition: bool, message: str) -> None:
    print(("ok    " if condition else "FAIL  ") + message, flush=True)
    if not condition:
        raise SystemExit(f"check failed: {message}")


def setup_repo() -> None:
    on("host-a", "rm -rf /work/repo /work/repo.agents /work/scripts && mkdir -p /work/scripts")
    with tempfile.TemporaryDirectory() as scratch:
        project = Path(scratch, "repo")
        files = {
            "tests/test_ok.py": "import time\nimport unittest\n\n\nclass T(unittest.TestCase):\n"
                                "    def test(self):\n        time.sleep(1)\n",
            "a/.keep": "",
            "b/.keep": "",
        }
        for name, body in files.items():
            Path(project, name).parent.mkdir(parents=True, exist_ok=True)
            Path(project, name).write_text(body)
        run("docker", "cp", str(project), "host-a:/work/repo")
        for name, body in {"hold.py": HOLD, "contend.py": CONTEND}.items():
            Path(scratch, name).write_text(body)
            run("docker", "cp", str(Path(scratch, name)), f"host-a:/work/scripts/{name}")
    on("host-a", """
        set -e
        cd /work/repo && git init -q && git add -A && git commit -qm project
        python /src/gate.py init
        python - <<'EOF'
from pathlib import Path
config = Path("gate.toml").read_text().replace("[lanes]\\n", '[lanes]\\na = ["a/**"]\\nb = ["b/**"]\\n', 1)
Path("gate.toml").write_text(config)
EOF
        git add -A && git commit -qm 'gate policy' && git switch -q --detach
    """)


def lock_checks(results: dict) -> None:
    on("host-a", "nohup python /work/scripts/hold.py 600 > /work/scripts/hold.log 2>&1 &")
    deadline = time.monotonic() + 60
    while on("host-b", "test -e /work/repo/.git/gate/probe.lock", check=False).returncode != 0:
        check(time.monotonic() < deadline, "host A acquired the probe lock")
        time.sleep(0.5)
    watch = on("host-b", "python - <<'EOF'\n"
                         "import os, time\n"
                         "seen = []\n"
                         "for _ in range(12):\n"
                         "    with open('/work/repo/.git/gate/probe.lock', 'rb') as handle:\n"
                         "        seen.append(os.fstat(handle.fileno()).st_mtime_ns)\n"
                         "    time.sleep(1)\n"
                         "print(len(set(seen)))\n"
                         "EOF").stdout.strip()
    results["heartbeats_seen_by_b_in_12s"] = int(watch)
    check(int(watch) >= 2, f"host B sees host A's heartbeats ({watch} distinct mtimes in 12 s)")
    timeout = LOCK_STALE_S * 2.5
    live = json.loads(on("host-b", f"python /work/scripts/contend.py {timeout}").stdout)
    results["live"] = live
    check(live["naive_age"] is not None and live["naive_age"] > LOCK_STALE_S,
          f"by host B's clock the live lock looks {live['naive_age']} s old (> {LOCK_STALE_S} s): skew is in effect")
    check(live["result"] == "blocked",
          f"host B did not take a heartbeating lock in {live['elapsed']} s of trying ({timeout:.0f} s timeout)")

    run("docker", "kill", "host-a")
    dead = json.loads(on("host-b", "python /work/scripts/contend.py 5").stdout)
    results["dead"] = dead
    check(dead["result"] == "acquired", f"host B took over the dead host's lock after {dead['elapsed']} s")
    check(LOCK_STALE_S - 1 <= dead["elapsed"] <= LOCK_STALE_S * 3,
          f"takeover waited for about LOCK_STALE_S ({LOCK_STALE_S} s) of silence")
    run("docker", "start", "host-a")
    wait_for_mount("host-a")


def queue_check(results: dict) -> None:
    for n in range(SESSIONS):
        host = "host-a" if n % 2 == 0 else "host-b"
        lane = "a" if n < SESSIONS // 2 else "b"
        sid = f"s{n}"
        worktree = on(host, f"python /src/gate.py -C /work/repo spawn {sid}").stdout.strip().splitlines()[-1]
        on(host, f"""
            set -e
            cd {worktree}
            echo 'change {n}' > {lane}/f{n}.txt
            git add -A && git commit -qm '{sid}: change'
            python /src/gate.py -C /work/repo submit {sid} --model nfs-test
        """)

    def drain(host: str) -> subprocess.CompletedProcess:
        return on(host, "python /src/gate.py -C /work/repo --json queue --drain --parallel 2 --wait 300",
                  check=False, timeout=900)

    started = time.monotonic()
    with concurrent.futures.ThreadPoolExecutor(2) as pool:
        procs = dict(zip(HOSTS, pool.map(drain, HOSTS), strict=True))
    results["drain_seconds"] = round(time.monotonic() - started, 1)
    merged_by = {}
    for host, proc in procs.items():
        check(proc.returncode == 0, f"queue --drain on {host} exited 0 ({proc.stderr.strip()[-300:]})")
        merged_by[host] = [item["session"] for item in json.loads(proc.stdout)["result"]["merged"]]
    results["merged_by"] = merged_by
    log = on("host-a", "git -C /work/repo log --format=%B%x00 main").stdout
    for n in range(SESSIONS):
        check(log.count(f"Agent-Session: s{n}\n") == 1, f"session s{n} is on main exactly once")
    statuses = json.loads(on("host-b", "python /src/gate.py -C /work/repo --json status --all").stdout)
    check(all(s["status"] == "merged" for s in statuses["result"]["sessions"]) and
          len(statuses["result"]["sessions"]) == SESSIONS, "every session is recorded as merged")
    check(sum(len(v) for v in merged_by.values()) == SESSIONS,
          f"each merge reported once across hosts: {merged_by}")


def main() -> int:
    with tempfile.TemporaryDirectory() as context:
        Path(context, "Dockerfile").write_text(DOCKERFILE)
        run("docker", "build", "-q", "-t", IMAGE, context)
    for host in HOSTS:
        run("docker", "rm", "-f", host, check=False)
        start(host)
    results: dict = {"lock_stale_s": LOCK_STALE_S, "skew_s": SKEW_S}
    try:
        skew = float(on("host-b", "date +%s").stdout) - float(on("host-a", "date +%s").stdout)
        check(abs(skew - SKEW_S) < 10, f"host B's clock is {skew:.0f} s ahead of host A's")
        setup_repo()
        lock_checks(results)
        queue_check(results)
    finally:
        for host in HOSTS:
            run("docker", "rm", "-f", host, check=False)
    print(json.dumps(results, indent=2))
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as handle:
            handle.write(
                "## gate on NFS: two hosts, skewed clock\n\n"
                f"| check | result |\n|---|---|\n"
                f"| live lock, B's clock +{SKEW_S} s | blocked for {results['live']['elapsed']} s; "
                f"naive age seen by B {results['live']['naive_age']} s |\n"
                f"| dead lock (docker kill A) | taken over after {results['dead']['elapsed']} s "
                f"(LOCK_STALE_S = {LOCK_STALE_S}) |\n"
                f"| concurrent drain, 2 hosts x 2 lanes | {SESSIONS} sessions, each on main once, "
                f"{results['drain_seconds']} s; merged by host: {results['merged_by']} |\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
