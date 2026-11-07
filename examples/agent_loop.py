#!/usr/bin/env python3
"""Reference agent integration for gate, using only its JSON interface.

An agent (or the harness around it) needs three calls: spawn, submit, then poll show. This script plays
the agent: it writes files into its worktree, commits, submits, and waits for a final status. With
--drive-queue it also runs `gate queue` itself so the example is self-contained; in production one
gate process runs the queue for everyone.

    python examples/agent_loop.py --repo PATH --session fix-1 --model my-model \\
        --file docs/note.md "hello" --drive-queue

Exit code: 0 merged, 2 bounced by policy or rejected (reason printed), 1 anything else.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

GATE = [sys.executable, str(Path(__file__).resolve().parent.parent / "gate.py")]
FINAL = {"merged", "rejected", "reverted", "abandoned"}


def gate(repo: str, *args: str) -> tuple[int, dict]:
    """Run one gate command and return (exit code, parsed JSON document)."""
    proc = subprocess.run([*GATE, "-C", repo, "--json", *args], capture_output=True, text=True)
    return proc.returncode, json.loads(proc.stdout)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def run(repo: str, session: str, model: str, files: dict[str, str], message: str,
        drive_queue: bool, timeout_s: float) -> int:
    code, doc = gate(repo, "spawn", session)
    if code != 0:
        print(f"spawn failed: {doc['error']}")
        return 1
    worktree = Path(doc["result"]["worktree"])

    for name, content in files.items():
        path = worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    git(worktree, "add", "-A")
    git(worktree, "-c", "user.name=example-agent", "-c", "user.email=agent@example.invalid",
        "commit", "-qm", message)

    code, doc = gate(repo, "submit", session, "--model", model)
    if code == 2:
        result = doc["result"]
        print(f"bounced: {result['status']} - {result['reason']} {result['findings']}")
        return 2
    if code != 0:
        print(f"submit failed: {doc.get('error')}")
        return 1
    print(f"submitted: {doc['result']['status']} (tier {doc['result']['tier']}, lane {doc['result']['lane']})")

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        if drive_queue:
            gate(repo, "queue", "--drain")
        code, doc = gate(repo, "show", session)
        status = doc["result"]["session"]["status"]
        if status in FINAL:
            detail = doc["result"]["session"].get("detail") or ""
            commit = doc["result"]["session"].get("merged_commit") or ""
            print(f"final: {status} {commit[:10]} {detail[-200:]}".rstrip())
            return 0 if status == "merged" else 2
        if status == "needs_approval":
            print("waiting for a human: gate approve", session, "--by NAME")
        time.sleep(1)
    print("timed out waiting for a final status")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--repo", required=True, help="repository managed by gate")
    parser.add_argument("--session", required=True, help="session id")
    parser.add_argument("--model", required=True, help="model id recorded on the commit")
    parser.add_argument("--file", nargs=2, action="append", metavar=("PATH", "CONTENT"), default=[],
                        help="file to write in the worktree (repeatable)")
    parser.add_argument("--message", default="agent change", help="commit message")
    parser.add_argument("--drive-queue", action="store_true", help="also run `gate queue` (demo only)")
    parser.add_argument("--timeout", type=float, default=600, help="seconds to wait for a final status")
    args = parser.parse_args()
    files = {path: content for path, content in args.file}
    return run(args.repo, args.session, args.model, files, args.message, args.drive_queue, args.timeout)


if __name__ == "__main__":
    sys.exit(main())
