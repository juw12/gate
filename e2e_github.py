#!/usr/bin/env python3
"""End-to-end check of gate against a real hosted remote (e.g. a GitHub repository you can push to).

Everything happens on a throwaway target branch `gate-e2e-<id>`, which is deleted at the end:

  1. batch      - three agent sessions are submitted and published in one queue run
  2. crash      - the gate process is killed (SIGKILL / TerminateProcess) after its push reached the
                  remote but before it recorded the merge; the next run recovers both sessions and the
                  remote gets no duplicate commits
  3. race       - a human pushes an unrelated commit while the gate is testing; the gate keeps it and
                  replays its own commit on top without re-testing
  4. protected  - the branch is protected (pull request required, admins included): the push is refused,
                  gate says so at once, runs the tests once, keeps the session queued and leaves the
                  remote untouched; after the gate's account gets a bypass (admins exempted) the same
                  session publishes. Needs a token with admin rights on the repository in
                  GATE_E2E_ADMIN_TOKEN; skipped otherwise.
  5. throughput - ten publishes one after another; reports the median and p95 time the publish lock is
                  held (fetch + push round trips dominate)

    python e2e_github.py https://github.com/OWNER/REPO.git [--keep]

Prints a JSON summary and exits 0 only if every scenario passed.
"""
from __future__ import annotations

import argparse
import contextlib
import io
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gate  # noqa: E402

PY = sys.executable
IDENTITY = ["-c", "user.name=gate-e2e", "-c", "user.email=gate-e2e@example.invalid"]


def git(cwd: Path, *args: str) -> str:
    proc = subprocess.run(["git", *IDENTITY, *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)}: {proc.stderr.strip()}")
    return proc.stdout


def run_gate(repo: Path, *argv: str) -> tuple[int, dict[str, Any]]:
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = gate.main(["-C", str(repo), "--json", *argv])
    return code, json.loads(out.getvalue())


def agent(repo: Path, sid: str, files: dict[str, str]) -> None:
    code, doc = run_gate(repo, "spawn", sid)
    assert code == 0, doc
    worktree = Path(doc["result"]["worktree"])
    for name, content in files.items():
        (worktree / name).parent.mkdir(parents=True, exist_ok=True)
        (worktree / name).write_text(content, encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "commit", "-qm", f"{sid}: change")
    code, doc = run_gate(repo, "submit", sid, "--model", "e2e-model")
    assert code == 0 and doc["result"]["status"] == "queued", doc


def remote_log(repo: Path, branch: str) -> str:
    git(repo, "fetch", "-q", "origin", f"+refs/heads/{branch}:refs/remotes/origin/{branch}")
    return git(repo, "log", "--format=%H%x00%B%x00", f"origin/{branch}")


def set_test_command(repo: Path, branch: str, command: str) -> None:
    """Commit a new policy to the remote target branch (as a human would)."""
    side = repo.parent / f"side-{uuid.uuid4().hex[:6]}"
    git(repo.parent, "clone", "-q", "--branch", branch, str(git(repo, "remote", "get-url", "origin").strip()),
        str(side))
    config = (side / "gate.toml").read_text(encoding="utf-8").splitlines()
    config = [f"test_command = {json.dumps(command)}" if line.startswith("test_command =") else line
              for line in config]
    (side / "gate.toml").write_text("\n".join(config) + "\n", encoding="utf-8")
    git(side, "commit", "-qam", "policy: change test command")
    git(side, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
    shutil.rmtree(side, ignore_errors=True)


def scenario_batch(repo: Path, branch: str) -> dict[str, Any]:
    for n in (1, 2, 3):
        agent(repo, f"b{n}", {f"e2e/batch_{n}.txt": f"batch {n}\n"})
    started = time.perf_counter()
    code, doc = run_gate(repo, "queue", "--drain")
    elapsed = time.perf_counter() - started
    merged = sorted(item["session"] for item in doc["result"]["merged"])
    log = remote_log(repo, branch)
    remote_head = git(repo, "rev-parse", f"origin/{branch}").strip()
    local_head = git(repo, "rev-parse", f"refs/heads/{branch}").strip()
    ok = (code == 0 and merged == ["b1", "b2", "b3"] and remote_head == local_head
          and all(log.count(f"Agent-Session: b{n}\n") == 1 for n in (1, 2, 3)))
    return {"ok": ok, "merged": merged, "queue_seconds": round(elapsed, 1), "remote_head": remote_head[:10]}


def scenario_crash(repo: Path, branch: str) -> dict[str, Any]:
    for n in (1, 2):
        agent(repo, f"c{n}", {f"e2e/crash_{n}.txt": f"crash {n}\n"})
    killed = subprocess.run([PY, gate.__file__, "-C", str(repo), "queue", "--drain"], capture_output=True,
                            text=True, env={**os.environ, "GATE_FAULT": "after-publish"})
    pushed = remote_log(repo, branch)
    published_before = pushed.count("\x00") // 2
    code, doc = run_gate(repo, "queue", "--drain")
    recovered = sorted(item["session"] for item in doc["result"]["recovered"])
    after = remote_log(repo, branch)
    ok = (killed.returncode != 0 and code == 0 and recovered == ["c1", "c2"]
          and after.count("\x00") // 2 == published_before
          and all(after.count(f"Agent-Session: c{n}\n") == 1 for n in (1, 2)))
    return {"ok": ok, "killed_exit_code": killed.returncode, "recovered": recovered,
            "remote_commits_unchanged": after == pushed}


def scenario_race(repo: Path, branch: str) -> dict[str, Any]:
    set_test_command(repo, branch, f'"{PY}" -c "import time; time.sleep(12)"')
    agent(repo, "r1", {"e2e/race_agent.txt": "agent\n"})
    url = git(repo, "remote", "get-url", "origin").strip()
    human = repo.parent / "human"
    git(repo.parent, "clone", "-q", "--branch", branch, url, str(human))

    def human_push() -> None:
        time.sleep(4)
        (human / "e2e" / "race_human.txt").parent.mkdir(parents=True, exist_ok=True)
        (human / "e2e" / "race_human.txt").write_text("human\n", encoding="utf-8")
        git(human, "add", "-A")
        git(human, "commit", "-qm", "human: unrelated change")
        git(human, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")

    pusher = threading.Thread(target=human_push)
    pusher.start()
    code, doc = run_gate(repo, "queue")
    pusher.join()
    log = remote_log(repo, branch)
    subjects = git(repo, "log", "--format=%s", "-2", f"origin/{branch}").splitlines()
    ok = (code == 0 and [m["session"] for m in doc["result"]["merged"]] == ["r1"]
          and "Commuted-From:" in log and "human: unrelated change" in subjects)
    return {"ok": ok, "top_two_commits": subjects, "commuted": "Commuted-From:" in log}


def github_api(token: str, method: str, path: str, body: dict | None = None) -> tuple[int, Any]:
    request = urllib.request.Request(
        f"https://api.github.com{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
            return response.status, json.loads(data) if data else None
    except urllib.error.HTTPError as error:
        return error.code, error.read().decode(errors="replace")[:300]


def scenario_protected(repo: Path, branch: str, slug: str | None) -> dict[str, Any]:
    token = os.environ.get("GATE_E2E_ADMIN_TOKEN", "")
    if not token or not slug:
        return {"ok": None, "skipped": "set GATE_E2E_ADMIN_TOKEN (admin on the repository) to run"}
    protection = f"/repos/{slug}/branches/{urllib.parse.quote(branch, safe='')}/protection"
    status, detail = github_api(token, "PUT", protection, {
        "required_status_checks": None, "enforce_admins": True, "restrictions": None,
        "required_pull_request_reviews": {"required_approving_review_count": 1}})
    if status != 200:
        return {"ok": False, "error": f"could not protect the branch: HTTP {status} {detail}"}
    try:
        agent(repo, "p1", {"e2e/protected.txt": "protected\n"})
        before = remote_log(repo, branch)
        calls = {"n": 0}
        original = gate.Gate.run_tests

        def counting(instance: gate.Gate, commit: str, changed: list[str] | None = None) -> tuple[bool, str]:
            calls["n"] += 1
            return original(instance, commit, changed)

        started = time.perf_counter()
        with mock.patch.object(gate.Gate, "run_tests", counting):
            code, doc = run_gate(repo, "queue", "--drain")
        refused_seconds = time.perf_counter() - started
        error = doc.get("error", "")
        status_doc = run_gate(repo, "show", "p1")[1]
        refused = (code == gate.EXIT_ERROR and "rejected the push" in error and calls["n"] == 1
                   and status_doc["result"]["session"]["status"] == "queued"
                   and remote_log(repo, branch) == before)

        github_api(token, "DELETE", f"{protection}/enforce_admins")  # the documented remedy: a bypass
        code, doc = run_gate(repo, "queue", "--drain")
        bypassed = code == 0 and [m["session"] for m in doc.get("result", {}).get("merged", [])] == ["p1"]
        return {"ok": refused and bypassed, "refused_at_once": refused, "tests_run": calls["n"],
                "refused_seconds": round(refused_seconds, 1), "error_excerpt": error[:160],
                "published_after_bypass": bypassed}
    finally:
        github_api(token, "DELETE", protection)


def scenario_throughput(repo: Path, branch: str, publishes: int = 10) -> dict[str, Any]:
    instance = gate.Gate(repo)
    holds, totals = [], []
    for n in range(publishes):
        agent(repo, f"t{n}", {f"e2e/throughput_{n}.txt": f"{n}\n"})
        before = instance.counters()
        started = time.perf_counter()
        code, doc = run_gate(repo, "queue")
        totals.append(time.perf_counter() - started)
        after = instance.counters()
        if code != 0 or after.get("publishes", 0) - before.get("publishes", 0) != 1:
            return {"ok": False, "error": f"publish {n} failed: {doc}"}
        holds.append((after.get("publish_hold_ms", 0) - before.get("publish_hold_ms", 0)) / 1000)
    holds.sort()
    p95 = holds[min(len(holds) - 1, round(0.95 * (len(holds) - 1)))]
    median = statistics.median(holds)
    return {"ok": True, "publishes": publishes, "lock_hold_median_s": round(median, 2),
            "lock_hold_p95_s": round(p95, 2), "queue_run_median_s": round(statistics.median(totals), 2),
            "ceiling_merges_per_day_at_batch_8": int(86400 / p95 * 8)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("remote_url", help="push-able remote, e.g. https://github.com/OWNER/REPO.git")
    parser.add_argument("--keep", action="store_true", help="keep the remote branch for inspection")
    args = parser.parse_args()
    branch = f"gate-e2e-{uuid.uuid4().hex[:8]}"
    work = Path(tempfile.mkdtemp(prefix="gate-e2e-"))
    repo = work / "repo"
    shown_url = re.sub(r"//[^/@]+@", "//***@", args.remote_url)  # never print credentials
    summary: dict[str, Any] = {"remote": shown_url, "branch": branch, "gate": gate.__version__}
    try:
        git(work, "clone", "-q", args.remote_url, str(repo))
        git(repo, "switch", "-q", "--orphan", branch)
        (repo / "gate.toml").write_text(
            gate.SAMPLE_CONFIG.replace("test_command = 'python -m unittest discover -s tests'",
                                       f'test_command = {json.dumps(f"{PY} -c pass")}')
            .replace("remote = ''", "remote = 'origin'"),
            encoding="utf-8")
        git(repo, "add", "gate.toml")
        git(repo, "commit", "-qm", "gate e2e policy")
        git(repo, "push", "-q", "origin", f"HEAD:refs/heads/{branch}")
        git(repo, "config", "gate.target", branch)
        git(repo, "switch", "-q", "--detach")
        started = time.perf_counter()
        for name, scenario in (("batch", scenario_batch), ("crash", scenario_crash), ("race", scenario_race)):
            summary[name] = scenario(repo, branch)
        set_test_command(repo, branch, f"{PY} -c pass")
        slug_match = re.search(r"github\.com[/:]([^/]+/[^/.]+?)(?:\.git)?/?$", args.remote_url)
        summary["protected"] = scenario_protected(repo, branch, slug_match.group(1) if slug_match else None)
        summary["throughput"] = scenario_throughput(repo, branch)
        summary["total_seconds"] = round(time.perf_counter() - started, 1)
    except Exception as error:  # report, then clean up
        summary["error"] = re.sub(r"//[^/@]+@", "//***@", f"{type(error).__name__}: {error}")
    finally:
        if not args.keep:
            with contextlib.suppress(Exception):
                git(repo, "push", "-q", "origin", f":refs/heads/{branch}")
                summary["remote_branch_deleted"] = True
        shutil.rmtree(work, ignore_errors=True)
    passed = "error" not in summary and all(
        summary[name]["ok"] is not False for name in ("batch", "crash", "race", "protected", "throughput"))
    summary["passed"] = passed
    print(json.dumps(summary, indent=2))
    return 0 if passed else 1


if __name__ == "__main__":
    os.environ.setdefault("GIT_TERMINAL_PROMPT", "0")
    sys.exit(main())
