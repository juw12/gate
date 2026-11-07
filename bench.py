"""Throughput benchmark: how many merges/day can one gate host publish?

Creates a repo with L lanes, N branch-only agent sessions (spread over the lanes,
created in one `git fast-import`), a test suite that sleeps T seconds (a stand-in
for real CI), then drains the queue with --parallel 1 and --parallel L and reports
merges/hour, the serialized publish cost, and how many publishes were commuted.

With --test-seconds-max, every candidate's suite sleeps a different time, drawn uniformly
between --test-seconds and --test-seconds-max from a hash of the candidate commit (so
reruns are reproducible), which is closer to real CI than a fixed sleep.

    python bench.py --sessions 240 --lanes 8 --test-seconds 2 --batch 8
    python bench.py --sessions 400 --lanes 8 --test-seconds 1 --test-seconds-max 5 --batch 8 --modes 8
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import gate as gatelib


def run_git(*args: str, cwd: Path, input_bytes: bytes | None = None) -> str:
    proc = subprocess.run(["git", *args], cwd=cwd, input=input_bytes, capture_output=True)
    if proc.returncode != 0:
        raise SystemExit(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode()


def gate(repo: Path, *argv: str) -> str:
    out = io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
        code = gatelib.main(["-C", str(repo), *argv])
    if code not in (0, 2):
        raise SystemExit(f"gate {' '.join(argv)} failed:\n{out.getvalue()}")
    return out.getvalue()


SLOW_SUITE = """\
import hashlib
import subprocess
import sys
import time

low, high = float(sys.argv[1]), float(sys.argv[2])
head = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
fraction = int(hashlib.sha1(head.encode()).hexdigest()[:8], 16) / 0xFFFFFFFF
time.sleep(low + (high - low) * fraction)
"""


def build_repo(root: Path, sessions: int, lanes: int, test_seconds: float, batch: int,
               test_seconds_max: float | None = None) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    run_git("init", "-q", "-b", "main", cwd=repo)
    run_git("config", "user.name", "Bench", cwd=repo)
    run_git("config", "user.email", "bench@example.com", cwd=repo)
    for lane in range(lanes):
        (repo / f"svc{lane}").mkdir()
        (repo / f"svc{lane}" / "README.md").write_text(f"service {lane}\n")
    lane_table = "".join(f"svc{lane} = ['svc{lane}/*']\n" for lane in range(lanes))
    config = gatelib.SAMPLE_CONFIG
    if test_seconds_max is None:
        command = f"\"{sys.executable}\" -c \"import time; time.sleep({test_seconds})\""
    else:
        (repo / "slow_suite.py").write_text(SLOW_SUITE)
        command = f"\"{sys.executable}\" slow_suite.py {test_seconds} {test_seconds_max}"
    config = config.replace("test_command = 'python -m unittest discover -s tests'", f"test_command = '{command}'")
    config = config.replace("max_batch = 4", f"max_batch = {batch}")
    config = config.replace("[lanes]\n", "[lanes]\n" + lane_table)
    (repo / "gate.toml").write_text(config)
    run_git("add", ".", cwd=repo)
    run_git("commit", "-qm", "initial", cwd=repo)
    run_git("switch", "-q", "--detach", cwd=repo)
    base = run_git("rev-parse", "main", cwd=repo).strip()

    for index in range(sessions):
        gate(repo, "spawn", f"s{index:04d}", "--no-worktree")
    stream = []
    for index in range(sessions):
        lane = index % lanes
        content = f"value = {index}\n".encode()
        message = f"s{index:04d}: add svc{lane}/f{index}.py".encode()
        stream += [
            f"commit refs/heads/agent/s{index:04d}\n".encode(),
            b"committer Agent <agent@example.com> 1700000000 +0000\n",
            f"data {len(message)}\n".encode(), message, b"\n",
            f"from {base}\n".encode(),
            f"M 100644 inline svc{lane}/f{index}.py\n".encode(),
            f"data {len(content)}\n".encode(), content, b"\n",
        ]
    run_git("fast-import", "--quiet", cwd=repo, input_bytes=b"".join(stream))
    for index in range(sessions):
        gate(repo, "submit", f"s{index:04d}", "--model", "bench-model")
    return repo


def measure(sessions: int, lanes: int, test_seconds: float, batch: int, parallel: int,
            test_seconds_max: float | None = None) -> dict:
    root = Path(tempfile.mkdtemp(prefix="gate-bench-"))
    try:
        repo = build_repo(root, sessions, lanes, test_seconds, batch, test_seconds_max)
        started = time.perf_counter()
        gate(repo, "queue", "--drain", "--parallel", str(parallel))
        elapsed = time.perf_counter() - started
        state = repo / ".git" / "gate"
        statuses = [json.loads(p.read_text())["status"] for p in (state / "sessions").glob("*.json")]
        counters = json.loads((state / "counters.json").read_text())
        merged = statuses.count("merged")
        landed_files = run_git("ls-tree", "-r", "--name-only", "main", cwd=repo).split()
        assert merged == sessions, f"only {merged}/{sessions} merged"
        assert sum(f.startswith("svc") and "/f" in f for f in landed_files) == sessions
        return {
            "parallel": parallel,
            "merged": merged,
            "seconds": elapsed,
            "merges_per_hour": merged / elapsed * 3600,
            "ci_runs": counters.get("ci_runs", 0),
            "publishes": counters.get("publishes", 0),
            "commuted": counters.get("commuted_publishes", 0),
            "publish_ms_mean": counters.get("publish_ms", 0) / max(1, counters.get("publishes", 1)),
            "hold_ms_mean": counters.get("publish_hold_ms", 0) / max(1, counters.get("publishes", 1)),
            "merges_per_publish": merged / max(1, counters.get("publishes", 1)),
        }
    finally:
        def force(func, target, _exc):
            os.chmod(target, stat.S_IWRITE)
            func(target)
        if sys.version_info >= (3, 12):
            shutil.rmtree(root, onexc=force)
        else:
            shutil.rmtree(root, onerror=force)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sessions", type=int, default=240)
    parser.add_argument("--lanes", type=int, default=8)
    parser.add_argument("--test-seconds", type=float, default=2.0)
    parser.add_argument("--test-seconds-max", type=float, default=None,
                        help="vary each candidate's test time uniformly in [--test-seconds, this]")
    parser.add_argument("--batch", type=int, default=8)
    parser.add_argument("--modes", default=None, help="comma-separated --parallel values (default: 1,LANES)")
    args = parser.parse_args()
    modes = [int(m) for m in args.modes.split(",")] if args.modes else [1, args.lanes]
    timing = (f"{args.test_seconds} s" if args.test_seconds_max is None
              else f"{args.test_seconds}-{args.test_seconds_max} s (uniform, seeded by commit)")
    print(f"{args.sessions} sessions, {args.lanes} lanes, batch {args.batch}, "
          f"simulated test time {timing} per candidate\n")
    print(f"{'parallel':>8} {'merged':>7} {'wall s':>8} {'merges/h':>9} {'CI runs':>8} "
          f"{'commuted':>9} {'hold ms':>8} {'wait+hold':>10} {'merges/day':>11} {'serial ceiling/day':>19}")
    for parallel in modes:
        r = measure(args.sessions, args.lanes, args.test_seconds, args.batch, parallel, args.test_seconds_max)
        # The only serialized step is the publish lock; its hold time bounds throughput
        # no matter how many lanes/CI workers run in parallel.
        ceiling = 86400 / (r["hold_ms_mean"] / 1000) * r["merges_per_publish"]
        print(f"{r['parallel']:>8} {r['merged']:>7} {r['seconds']:>8.1f} {r['merges_per_hour']:>9.0f} "
              f"{r['ci_runs']:>8} {r['commuted']:>4}/{r['publishes']:<4} {r['hold_ms_mean']:>8.0f} "
              f"{r['publish_ms_mean']:>10.0f} {r['merges_per_hour'] * 24:>11.0f} {ceiling:>19.0f}")


if __name__ == "__main__":
    main()
