#!/usr/bin/env python3
"""gate - a merge gate for AI-agent-generated changes.

Many parallel agent sessions in, a few verified, provenance-tagged commits out.

  * one shared object store; per-session worktree (optionally sparse) or branch-only
    sessions for agents that run elsewhere and push
  * server-style merges with `git merge-tree` (no checkout of the target)
  * lanes: path-partitioned queues that build and test in parallel; only a short
    publish step is serialized, and a lane whose files didn't overlap with what
    landed meanwhile is replayed onto the new tip instead of re-tested
  * targeted test suites (by path), run in parallel, results cached by tree
  * batched testing with bisection; compare-and-swap ref updates (local or remote)
  * post-merge `verify`: tests the target, bisects a regression to the landed
    commit that caused it, and auto-reverts it (catches semantic conflicts)
  * risk tiers and size gate; secrets in a diff block the merge; traces are redacted
    (patterns + entropy) and an optional external scanner fails closed
  * provenance trailers; attempts and traces live outside Git refs and expire

Requires git >= 2.40 and Python >= 3.11. Standard library only.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import contextlib
import fnmatch
import gzip
import hashlib
import json
import math
import os
import re
import shutil
import signal
import socket
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import uuid
from collections.abc import Iterator
from pathlib import Path
from typing import Any

__version__ = "0.3.0"

CONFIG_FILE = "gate.toml"
LOCK_HEARTBEAT_S = 5.0   # a lock holder touches its lock file this often
LOCK_STALE_S = float(os.environ.get("GATE_LOCK_STALE_S", "60"))  # silent this long = abandoned
BREAK_STALE_S = 10.0     # a stale-lock breaker that stays put this long died mid-break
SCAN_TIMEOUT_S = 300.0   # external secret scanner time limit; a timeout counts as a finding
LEASE_REJECTED = re.compile(r"stale info|fetch first|non-fast-forward|cannot lock ref|but expected", re.I)
MIN_GIT = (2, 40)
SESSION_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
IN_FLIGHT = {"active", "too_large", "needs_approval", "queued", "rejected"}

# Exit codes (stable; documented in README and `gate --help`).
EXIT_OK = 0
EXIT_ERROR = 1          # usage, git or environment error; doctor found a failing check
EXIT_POLICY = 2         # submit bounced by policy: too large or secrets found
EXIT_NEEDS_HUMAN = 3    # verify: target is red and could not be fixed automatically
EXIT_TARGET_MOVED = 4   # verify: target moved while reverting; run again

DEFAULT_CONFIG = {
    "test_command": "",
    "tests": [],
    "test_parallelism": 4,
    "test_timeout_s": 1800,
    "remote": "",
    "max_batch": 4,
    "max_changed_lines": 400,
    "size_exempt": ["*.lock", "package-lock.json", "*/generated/*"],
    "approval_tier": 2,
    "lanes": {},
    "commute_disjoint": True,
    "secret_scan": True,
    "secret_scan_exempt": [],
    "secret_scan_command": "",
    "risk": {
        "high": [CONFIG_FILE, ".github/*", "auth/*", "*/auth/*", "infra/*", "*migrations/*"],
        "low": ["docs/*", "*.md", "tests/*", "*/tests/*"],
    },
    "retention": {
        "attempt_days": 14,
        "reject_grace_hours": 24,
        "trace_days_merged": 730,
        "trace_days_unmerged": 14,
    },
}

SAMPLE_CONFIG = """\
# gate policy. It is read from the TARGET BRANCH, not from a working tree,
# so an agent cannot loosen it in its own worktree. Editing it is high-risk.

# Always-run test command (empty = none). Add targeted suites with [[tests]] below.
test_command = 'python -m unittest discover -s tests'
test_timeout_s = 1800
test_parallelism = 4       # suites run concurrently, each in its own checkout

# Optional remote to publish to (compare-and-swap via --force-with-lease).
remote = ''

# Sessions tested together; a failing batch is bisected.
max_batch = 4

# Size gate on added + deleted lines per session, excluding size_exempt files.
max_changed_lines = 400
size_exempt = ['*.lock', 'package-lock.json', '*/generated/*']

# Sessions at or above this tier need `gate approve`.
approval_tier = 2

# If the target moved while a batch was testing, but none of the files that moved
# overlap the batch, replay the batch onto the new tip without re-testing.
# Cross-file semantic breakage is then caught by `gate verify`.
commute_disjoint = true

# Block merges whose added lines contain secrets. Optional external scanner:
# {file} is replaced by a temp file path; a nonzero exit means "secrets found".
secret_scan = true
secret_scan_exempt = []
secret_scan_command = ''

[risk]
# fnmatch patterns ("*" also matches "/"). Any high-risk file -> tier 2.
high = ['gate.toml', '.github/*', 'auth/*', '*/auth/*', 'infra/*', '*migrations/*']
# All files low-risk -> tier 0. Anything else -> tier 1.
low = ['docs/*', '*.md', 'tests/*', '*/tests/*']

[retention]
attempt_days = 14          # archived diffs of rejected/abandoned sessions
reject_grace_hours = 24    # a rejected session keeps its worktree this long for a fix
trace_days_merged = 730
trace_days_unmerged = 14

# Lanes: sessions whose files all fall in one lane are queued and tested in parallel
# with other lanes (`gate queue --parallel N`). Everything else uses "default".
[lanes]
# api = ['services/api/*']
# web = ['web/*']

# Targeted suites run only when a changed file matches `paths` (empty = always).
# [[tests]]
# name = 'api'
# command = 'pytest services/api'
# paths = ['services/api/*', 'libs/*']
"""

# ------------------------------------------------------------------ secret detection
SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S), "private-key"),
    (re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"), "aws-key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"), "github-token"),
    (re.compile(r"\bgithub_pat_[A-Za-z0-9_]{40,}\b"), "github-token"),
    (re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"), "gitlab-token"),
    (re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"), "api-key"),
    (re.compile(r"\b[rs]k_(?:live|test)_[A-Za-z0-9]{20,}\b"), "stripe-key"),
    (re.compile(r"\bAIza[0-9A-Za-z_-]{35}\b"), "google-api-key"),
    (re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"), "slack-token"),
    (re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"), "jwt"),
    (re.compile(r"(?i)\bAccountKey=[A-Za-z0-9+/=]{20,}"), "azure-key"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{20,}=*"), "bearer-token"),
]
URL_CREDENTIALS = re.compile(r"\b([a-z][a-z0-9+.-]*://)([^/\s:@]+):([^/\s@]{3,})@", re.I)
LOOSE_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|secret|token|api[_-]?key)(\"?\s*[:=]\s*\"?)([^\s\"',;]{6,})")
QUOTED_ASSIGNMENT = re.compile(
    r"(?i)\b\w*(password|passwd|secret|token|api[_-]?key)\w*\s*[:=]\s*[\"']([^\"'\s]{8,})[\"']")
TOKEN_CANDIDATE = re.compile(r"[A-Za-z0-9+/_\-=]{32,}")
PLACEHOLDER = re.compile(r"(?i)(x{4,}|\*{4,}|example|changeme|placeholder|dummy|redacted|<[^>]+>|\$\{)")


def shannon_entropy(token: str) -> float:
    counts = {char: token.count(char) for char in set(token)}
    return -sum(n / len(token) * math.log2(n / len(token)) for n in counts.values())


def looks_random(token: str) -> bool:
    mixed = (any(c.islower() for c in token) and any(c.isupper() for c in token)
             and any(c.isdigit() for c in token))
    return mixed and shannon_entropy(token) >= 4.2


def redact(text: str) -> tuple[str, int]:
    """Redact secrets from free text (traces, archived patches)."""
    total = 0
    for pattern, label in SECRET_PATTERNS:
        text, count = pattern.subn(f"[REDACTED:{label}]", text)
        total += count
    text, count = URL_CREDENTIALS.subn(r"\1\2:[REDACTED]@", text)
    total += count
    text, count = LOOSE_ASSIGNMENT.subn(r"\1\2[REDACTED]", text)
    total += count

    def entropy_sub(match: re.Match) -> str:
        nonlocal total
        if looks_random(match.group(0)):
            total += 1
            return "[REDACTED:high-entropy]"
        return match.group(0)

    text = TOKEN_CANDIDATE.sub(entropy_sub, text)
    return text, total


def secrets_in_diff(diff: str, exempt: list[str]) -> list[str]:
    """Findings (file: kind) on added lines of a unified diff. Never returns the secret."""
    findings: list[str] = []
    path = None
    for line in diff.splitlines():
        if line.startswith("+++ "):
            path = line[6:] if line.startswith("+++ b/") else None
            continue
        if not line.startswith("+") or path is None or matches(path, exempt):
            continue
        added = line[1:]
        kinds = [label for pattern, label in SECRET_PATTERNS if pattern.search(added)]
        if URL_CREDENTIALS.search(added):
            kinds.append("url-credentials")
        quoted = QUOTED_ASSIGNMENT.search(added)
        if quoted and not PLACEHOLDER.search(quoted.group(2)):
            kinds.append("hardcoded-credential")
        findings += [f"{path}: {kind}" for kind in kinds]
    return sorted(set(findings))


# ------------------------------------------------------------------ init
def detect_test_command(root: Path) -> tuple[str, str]:
    """Best guess at a repository's test command, with the evidence for it ('' if none found)."""
    def read(name: str) -> str:
        path = root / name
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    if (root / "pytest.ini").is_file() or any("pytest" in read(f) for f in
                                              ("pyproject.toml", "setup.cfg", "tox.ini", "requirements-dev.txt")):
        return "python -m pytest -q", "pytest is configured or listed"
    package = read("package.json")
    if package:
        with contextlib.suppress(json.JSONDecodeError):
            if json.loads(package).get("scripts", {}).get("test"):
                return "npm test", "package.json has a test script"
    if (root / "go.mod").is_file():
        return "go test ./...", "go.mod found"
    if (root / "Cargo.toml").is_file():
        return "cargo test", "Cargo.toml found"
    if any(root.glob("tests/test_*.py")):
        return "python -m unittest discover -s tests", "tests/test_*.py found"
    if any(root.glob("test_*.py")):
        return "python -m unittest", "test_*.py found"
    return "", "no test setup detected; set test_command yourself"


# ------------------------------------------------------------------ helpers
class GateError(Exception):
    pass


class Contention(Exception):
    """The target ref moved between reading the tip and publishing."""


class Output:
    """Human-readable lines by default; with --json, one JSON document on stdout instead."""

    def __init__(self) -> None:
        self.json = False
        self.events: list[dict] = []
        self.result: dict = {}
        self._lock = threading.Lock()

    def reset(self, json_mode: bool = False) -> None:
        self.json = json_mode
        self.events = []
        self.result = {}

    def say(self, text: str) -> None:
        if not self.json:
            print(text, flush=True)

    def event(self, kind: str, text: str, **fields: Any) -> None:
        with self._lock:
            self.events.append({"event": kind, **fields})
        self.say(text)


OUT = Output()


def fault_point(name: str) -> None:
    """Test hook: with GATE_FAULT=<name>, kill this process abruptly here (no cleanup, no finally blocks,
    locks left behind), the way a crash, OOM kill or power loss would."""
    if os.environ.get("GATE_FAULT") == name:
        sys.stdout.flush()
        os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))


def write_atomic(path: Path, data: bytes) -> None:
    """Write via a temp file + rename, so readers and crash recovery never see a partial file."""
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def pid_alive(pid: int) -> bool:
    """True if a process with this id is running on this host."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        process_query_limited_information = 0x1000
        still_active = 259
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if not handle:
            return False
        exit_code = ctypes.c_ulong()
        ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code))
        kernel32.CloseHandle(handle)
        return bool(ok) and exit_code.value == still_active
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def git_run(*args: str, cwd: Path, env: dict | None = None,
            input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=env,
        input=input_text,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


# On Windows, a git process that reads a file while another one replaces it (lockfile +
# rename) gets "Permission denied". These failures happen before git does any work, so
# retrying is safe. Seen in CI with parallel lanes.
TRANSIENT_GIT_ERRORS = ("unable to access", "Permission denied")


def git(*args: str, cwd: Path, env: dict | None = None) -> str:
    for attempt in range(6):
        proc = git_run(*args, cwd=cwd, env=env)
        if proc.returncode == 0:
            return proc.stdout
        if not any(marker in proc.stderr for marker in TRANSIENT_GIT_ERRORS):
            break
        time.sleep(0.05 * (2 ** attempt))
    raise GateError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def git_version() -> tuple[int, int]:
    out = subprocess.run(["git", "--version"], capture_output=True, text=True).stdout
    numbers = re.search(r"(\d+)\.(\d+)", out)
    return (int(numbers.group(1)), int(numbers.group(2))) if numbers else (0, 0)


def matches(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def merged_config(overrides: dict) -> dict:
    config = json.loads(json.dumps(DEFAULT_CONFIG))
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(config.get(key), dict) and key != "lanes":
            config[key].update(value)
        else:
            config[key] = value
    return config


class Gate:
    def __init__(self, path: Path):
        worktrees = git("worktree", "list", "--porcelain", cwd=path)
        self.root = Path(worktrees.splitlines()[0].removeprefix("worktree ").strip())
        common = Path(git("rev-parse", "--git-common-dir", cwd=self.root).strip())
        if not common.is_absolute():
            common = (self.root / common).resolve()
        self.state = common / "gate"
        for sub in ("sessions", "attempts", "traces"):
            (self.state / sub).mkdir(parents=True, exist_ok=True)
        self.agents_dir = self.root.parent / f"{self.root.name}.agents"
        configured = git_run("config", "gate.target", cwd=self.root).stdout.strip()
        self.target = configured or "main"
        self.config = self._load_config()
        # Lock liveness is judged by observed change on this process's own clock (see _unchanged_for).
        self._observed: dict[str, tuple[tuple[str, int], float]] = {}
        self._observed_lock = threading.Lock()

    # ---------------------------------------------------------------- config / state
    def _load_config(self) -> dict:
        proc = git_run("show", f"refs/heads/{self.target}:{CONFIG_FILE}", cwd=self.root)
        if proc.returncode != 0:
            return merged_config({})
        return merged_config(tomllib.loads(proc.stdout))

    def session_path(self, sid: str) -> Path:
        return self.state / "sessions" / f"{sid}.json"

    def load(self, sid: str) -> dict:
        path = self.session_path(sid)
        if not path.exists():
            raise GateError(f"unknown session {sid!r}; `gate status --all` lists sessions")
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, session: dict) -> None:
        self.write_json(self.session_path(session["id"]), session)

    def write_json(self, path: Path, data: Any) -> None:
        write_atomic(path, json.dumps(data, indent=2).encode("utf-8"))

    def read_json(self, name: str, default: Any) -> Any:
        path = self.state / name
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else default

    def sessions(self) -> list[dict]:
        files = sorted((self.state / "sessions").glob("*.json"))
        return [json.loads(f.read_text(encoding="utf-8")) for f in files]

    def bump_counter(self, name: str, amount: float = 1) -> None:
        with self.lock("state"):
            counters = self.read_json("counters.json", {})
            counters[name] = counters.get(name, 0) + amount
            self.write_json(self.state / "counters.json", counters)

    def counters(self) -> dict:
        return self.read_json("counters.json", {})

    @contextlib.contextmanager
    def lock(self, name: str, timeout_s: float = 30.0) -> Iterator[None]:
        """Exclusive lock file recording `pid host token`. While it is held, a heartbeat thread touches
        it every LOCK_HEARTBEAT_S. Another process may break it only if
        - the owner is a dead process on this host, or
        - this process has watched the lock file stay unchanged for LOCK_STALE_S on its own monotonic
          clock (owner crashed, frozen, its pid was reused, or it ran on another host), or
        - its timestamp is older than 10 x LOCK_STALE_S (abandoned long ago).
        Liveness is judged by change, not by comparing timestamps with this host's clock, so clock skew
        between hosts and NFS attribute caching cannot make a live lock look stale."""
        path = self.state / f"{name}.lock"
        token = f"{os.getpid()} {socket.gethostname()} {uuid.uuid4().hex}"
        deadline = time.monotonic() + timeout_s
        waiting_since = time.monotonic()
        while True:
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, token.encode())
                os.close(fd)
                break
            except (FileExistsError, PermissionError):
                if self._lock_is_stale(path):
                    self._break_stale_lock(path)
                    continue
                if time.monotonic() > deadline and not self._silent_since(path, waiting_since):
                    raise GateError(f"{name} lock is held by another gate process ({path}); it is taken "
                                    f"over automatically once that process exits or stops refreshing it "
                                    f"for {LOCK_STALE_S:.0f} s") from None
                time.sleep(0.05)
        self._forget(path)
        stop = threading.Event()
        heartbeat = threading.Thread(target=self._heartbeat, args=(path, token, stop), daemon=True)
        heartbeat.start()
        try:
            yield
        finally:
            stop.set()
            heartbeat.join()
            self._release_lock(path, token)

    @staticmethod
    def _heartbeat(path: Path, token: str, stop: threading.Event) -> None:
        while not stop.wait(LOCK_HEARTBEAT_S):
            try:
                if path.read_text(encoding="utf-8") != token:
                    return  # broken while we were frozen: stop claiming it
                os.utime(path)
            except OSError:
                continue  # transient sharing violation on Windows; retry on the next beat

    @staticmethod
    def _release_lock(path: Path, token: str) -> None:
        # Delete the lock only while it is still ours. On Windows a concurrent reader (another process
        # checking staleness) briefly blocks deletion, so retry.
        for attempt in range(50):
            try:
                if path.read_text(encoding="utf-8") != token:
                    return
                path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.01 * min(attempt + 1, 10))
        raise GateError(f"could not release lock {path}")

    @staticmethod
    def _read_lock(path: Path) -> tuple[str, float] | None:
        """Content and mtime. open() + fstat() rather than stat(): on NFS, open revalidates cached
        attributes (close-to-open consistency), so a heartbeat from another host is seen."""
        try:
            with open(path, "rb") as handle:
                content = handle.read().decode("utf-8", "replace")
                return content, os.fstat(handle.fileno()).st_mtime
        except OSError:
            return None

    def _unchanged_for(self, path: Path, content: str, mtime: float) -> float:
        """Seconds (on this process's monotonic clock) for which this file has shown the same content
        and mtime. Any change - a heartbeat, a new owner - restarts the count."""
        signature = (content, int(mtime * 1e9))
        now = time.monotonic()
        with self._observed_lock:
            seen = self._observed.get(str(path))
            if seen is None or seen[0] != signature:
                self._observed[str(path)] = (signature, now)
                return 0.0
            return now - seen[1]

    def _silent_since(self, path: Path, since: float) -> bool:
        """True if the lock has not changed since `since`: its owner may be dead, so keep waiting until
        the observation window decides, even past the caller's timeout."""
        with self._observed_lock:
            seen = self._observed.get(str(path))
        return seen is not None and seen[1] <= since + LOCK_HEARTBEAT_S / 2

    def _forget(self, path: Path) -> None:
        with self._observed_lock:
            self._observed.pop(str(path), None)

    def _break_stale_lock(self, path: Path) -> None:
        """Remove a stale lock without racing other breakers: only the holder of `<lock>.break` may
        delete it, after re-checking. A live owner never loses its lock, because a fresh lock file can
        only appear after the stale one is gone, and only the breaker removes that."""
        breaker = path.with_name(path.name + ".break")
        try:
            fd = os.open(breaker, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except (FileExistsError, PermissionError):
            # stat(), not open(): on Windows an open handle would stop the breaker's owner deleting it.
            try:
                info = os.stat(breaker)
            except OSError:
                info = None
            if info and self._unchanged_for(breaker, str(info.st_size), info.st_mtime) > BREAK_STALE_S:
                self._unlink_retrying(breaker)  # a breaker that died mid-break
                self._forget(breaker)
            time.sleep(0.01)
            return
        try:
            os.close(fd)
            if self._lock_is_stale(path):
                self._unlink_retrying(path)
                self._forget(path)
        finally:
            self._unlink_retrying(breaker)

    @staticmethod
    def _unlink_retrying(path: Path) -> None:
        """Delete a file, retrying briefly while another process holds it open (Windows refuses then)."""
        for attempt in range(50):
            try:
                path.unlink()
                return
            except FileNotFoundError:
                return
            except PermissionError:
                time.sleep(0.005 * min(attempt + 1, 10))

    def _lock_is_stale(self, path: Path) -> bool:
        observed = self._read_lock(path)
        if observed is None:
            return False
        content, mtime = observed
        owner = content.split()
        same_host = len(owner) >= 2 and owner[1] == socket.gethostname() and owner[0].isdigit()
        if same_host and not pid_alive(int(owner[0])):
            return True
        age = time.time() - mtime
        if same_host and age > LOCK_STALE_S:
            return True  # same clock, so the timestamp can be trusted (frozen holder or reused pid)
        if age > 10 * LOCK_STALE_S:
            return True  # abandoned long ago; tolerates up to 9 x LOCK_STALE_S of clock skew
        return self._unchanged_for(path, content, mtime) > LOCK_STALE_S

    def update(self, sid: str, expected_head: str | None = None, expected_status: str | None = None,
               **changes: Any) -> bool:
        """Read-modify-write a session under the state lock; skip (return False) if it was resubmitted
        with a new head or has moved on from the status the caller saw."""
        with self.lock("state"):
            session = self.load(sid)
            if expected_head is not None and session.get("head") != expected_head:
                return False
            if expected_status is not None and session.get("status") != expected_status:
                return False
            session.update(changes)
            self.save(session)
            return True

    # ---------------------------------------------------------------- git helpers
    def rev(self, ref: str) -> str:
        return git("rev-parse", "--verify", f"{ref}^{{commit}}", cwd=self.root).strip()

    def tree_of(self, commit: str) -> str:
        return git("rev-parse", f"{commit}^{{tree}}", cwd=self.root).strip()

    def changed_files(self, old: str, new: str) -> list[str]:
        out = git("diff", "--name-only", "--no-renames", old, new, cwd=self.root)
        return [line for line in out.splitlines() if line]

    def sync_tip(self) -> str:
        remote = self.config["remote"]
        if not remote:
            return self.rev(f"refs/heads/{self.target}")
        tracking = f"refs/remotes/{remote}/{self.target}"
        git("fetch", "--quiet", remote, f"+refs/heads/{self.target}:{tracking}", cwd=self.root)
        tip = self.rev(tracking)
        previous = git_run("rev-parse", "--verify", "--quiet", f"refs/heads/{self.target}", cwd=self.root)
        if previous.stdout.strip() != tip:
            self.ensure_target_free()
        git("update-ref", f"refs/heads/{self.target}", tip, cwd=self.root)
        if previous.stdout.strip() != tip:
            self.config = self._load_config()  # the policy lives on the target; it may have just changed
        return tip

    def advance(self, old: str, new: str) -> None:
        """Compare-and-swap the target from old to new. Contention means the target moved (rebuild and
        retry); any other refusal - a protected branch, missing permission, a network or auth error - is
        an error a retry cannot fix, so it is reported at once instead of re-testing the batch."""
        self.ensure_target_free()
        remote = self.config["remote"]
        ref = f"refs/heads/{self.target}"
        if remote:
            proc = git_run("push", "--quiet", "--porcelain", remote, f"{new}:{ref}",
                           f"--force-with-lease={ref}:{old}", cwd=self.root)
            if proc.returncode != 0:
                output = (proc.stdout + proc.stderr).strip()
                if LEASE_REJECTED.search(output):
                    raise Contention(output)
                raise GateError(f"remote '{remote}' rejected the push to {self.target}: {output[-400:]}\n"
                                f"Nothing was lost: the sessions stay queued. If the branch is protected, give "
                                f"the gate's account a bypass (README: Publishing to GitHub); if this is a "
                                f"network or authentication error, fix it and run the queue again.")
            git("update-ref", ref, new, cwd=self.root)
            return
        proc = git_run("update-ref", ref, new, old, cwd=self.root)
        if proc.returncode != 0:
            raise Contention(proc.stderr.strip())

    def ensure_target_free(self) -> None:
        """Moving a branch that a worktree has checked out would leave that checkout looking as if it
        had reverted every new commit, so refuse."""
        if self.target_checked_out():
            raise GateError(f"'{self.target}' is checked out in a worktree, and gate would move it under that "
                            f"checkout; run gate from a detached HEAD or another branch (`gate doctor --fix` "
                            f"detaches the main checkout)")

    def target_checked_out(self) -> bool:
        listing = git("worktree", "list", "--porcelain", cwd=self.root)
        return f"branch refs/heads/{self.target}" in listing.splitlines()

    def diff_stats(self, base: str, head: str) -> tuple[list[str], int, int]:
        out = git("diff", "--numstat", "--no-renames", base, head, cwd=self.root)
        files, sized, changed = [], 0, 0
        for line in out.splitlines():
            added, deleted, path = line.split("\t", 2)
            files.append(path)
            lines = 0 if added == "-" else int(added) + int(deleted)
            changed += lines
            if not matches(path, self.config["size_exempt"]):
                sized += lines
        return files, changed, sized

    def risk_tier(self, files: list[str]) -> int:
        risk = self.config["risk"]
        if any(matches(f, risk["high"]) for f in files):
            return 2
        if files and all(matches(f, risk["low"]) for f in files):
            return 0
        return 1

    def lane_of(self, files: list[str]) -> str:
        for name, patterns in self.config["lanes"].items():
            if files and all(matches(f, patterns) for f in files):
                return name
        return "default"

    def external_scan(self, text: str) -> str | None:
        """Run the configured external scanner; return findings text, or None if clean."""
        command = self.config["secret_scan_command"]
        if not command:
            return None
        scratch = Path(tempfile.mkdtemp(prefix="gate-scan-"))
        try:
            target = scratch / "content.txt"
            target.write_text(text, encoding="utf-8")
            try:
                proc = subprocess.run(command.replace("{file}", str(target)), shell=True,
                                      capture_output=True, text=True, errors="replace", timeout=SCAN_TIMEOUT_S)
            except subprocess.TimeoutExpired:
                return f"external scanner timed out after {SCAN_TIMEOUT_S:.0f} s; treated as a finding (fail closed)"
            except OSError as error:
                return f"external scanner could not run ({error}); treated as a finding (fail closed)"
            if proc.returncode == 0:
                return None
            return (proc.stdout + proc.stderr).strip()[-500:] or "external scanner reported findings"
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

    # ---------------------------------------------------------------- tests
    def suites(self) -> list[dict]:
        suites = []
        if self.config["test_command"]:
            suites.append({"name": "default", "command": self.config["test_command"], "paths": []})
        for index, suite in enumerate(self.config["tests"]):
            suites.append({
                "name": suite.get("name") or f"suite{index}",
                "command": suite["command"],
                "paths": suite.get("paths", []),
            })
        return suites

    def run_tests(self, commit: str, changed: list[str] | None = None) -> tuple[bool, str]:
        """Run the suites selected by `changed` (None = all) against commit."""
        self.bump_counter("ci_runs")
        selected = [s for s in self.suites()
                    if changed is None or not s["paths"] or any(matches(f, s["paths"]) for f in changed)]
        if not selected:
            return True, "no test suites selected"
        tree = self.tree_of(commit)
        cache = self.read_json("test-cache.json", {})
        key = {s["name"]: f"{tree}:{hashlib.sha256(s['command'].encode()).hexdigest()[:16]}"
               for s in selected}
        pending = [s for s in selected if key[s["name"]] not in cache]
        self.bump_counter("suite_cache_hits", len(selected) - len(pending))
        if not pending:
            return True, "all selected suites passed earlier on this exact tree"
        workers = max(1, int(self.config["test_parallelism"]))
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(lambda s: self._run_suite(commit, s), pending))
        passed = [s for s, (ok, _) in zip(pending, results, strict=True) if ok]
        if passed:
            with self.lock("state"):
                cache = self.read_json("test-cache.json", {})
                for suite in passed:
                    cache[key[suite["name"]]] = time.time()
                self.write_json(self.state / "test-cache.json", cache)
        failures = [f"[{s['name']}]\n{log}" for s, (ok, log) in zip(pending, results, strict=True) if not ok]
        return not failures, "\n".join(failures)[-4000:]

    def _run_suite(self, commit: str, suite: dict) -> tuple[bool, str]:
        self.bump_counter("suite_runs")
        scratch = Path(tempfile.mkdtemp(prefix="gate-ci-"))
        worktree = scratch / f"ci-{uuid.uuid4().hex[:12]}"
        git("worktree", "add", "--quiet", "--detach", str(worktree), commit, cwd=self.root)
        try:
            proc = subprocess.run(suite["command"], shell=True, cwd=worktree, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=self.config["test_timeout_s"])
            return proc.returncode == 0, (proc.stdout + proc.stderr)[-4000:]
        except subprocess.TimeoutExpired:
            return False, f"timed out after {self.config['test_timeout_s']} s"
        finally:
            git_run("worktree", "remove", "--force", str(worktree), cwd=self.root)
            shutil.rmtree(scratch, ignore_errors=True)

    # ---------------------------------------------------------------- commits
    def commit_env(self, author_of: str) -> dict:
        author = git("log", "-1", "--format=%an%x00%ae%x00%ad", "--date=raw", author_of, cwd=self.root)
        name, email, date = author.strip().split("\x00")
        return {
            **os.environ,
            "GIT_AUTHOR_NAME": name,
            "GIT_AUTHOR_EMAIL": email,
            "GIT_AUTHOR_DATE": date,
            "GIT_COMMITTER_NAME": "gate",
            "GIT_COMMITTER_EMAIL": "gate@localhost",
        }

    def squash_commit(self, session: dict, tree: str, parent: str) -> str:
        current = self.load(session["id"])
        trailers = [
            ("Agent-Session", session["id"]),
            ("Agent-Model", session.get("model")),
            ("Agent-Tool", session.get("tool")),
            ("Spec", session.get("spec")),
            ("Prompt-SHA256", session.get("prompt_sha256")),
            ("Trace-SHA256", (current.get("trace") or {}).get("sha256")),
            ("Risk-Tier", str(session["tier"])),
            ("Approved-By", session.get("approved_by")),
            ("Agent-Lane", session.get("lane")),
            ("Agent-Base", session["base"]),
            ("Agent-Head", session["head"]),
        ]
        body = "\n".join(f"{key}: {value}" for key, value in trailers if value)
        message = f"{session['title']}\n\n{body}\n"
        return git("commit-tree", tree, "-p", parent, "-m", message, cwd=self.root,
                   env=self.commit_env(session["head"])).strip()

    def replay(self, commits: list[str], onto: str) -> list[str] | None:
        """Cherry-pick tested commits onto a new tip with merge-tree; None on conflict."""
        parent = onto
        replayed = []
        for commit in commits:
            proc = git_run("merge-tree", "--write-tree", f"--merge-base={commit}^", parent, commit,
                           cwd=self.root)
            if proc.returncode != 0:
                return None
            tree = proc.stdout.split()[0]
            message = git("log", "-1", "--format=%B", commit, cwd=self.root).rstrip()
            message += f"\nCommuted-From: {commit}\n"
            parent = git("commit-tree", tree, "-p", parent, "-m", message, cwd=self.root,
                         env=self.commit_env(commit)).strip()
            replayed.append(parent)
        return replayed

    def publish(self, tested_tip: str, commits: list[str], files: list[str]) -> list[str] | None:
        """Serialize publication. Returns published commits, or None if a re-test is needed."""
        started = time.perf_counter()
        held_since = None
        try:
            with self.lock("publish", timeout_s=600):
                held_since = time.perf_counter()
                for _ in range(5):
                    current = self.sync_tip()
                    to_publish = commits
                    if current != tested_tip:
                        moved = set(self.changed_files(tested_tip, current))
                        if not self.config["commute_disjoint"] or moved & set(files):
                            return None
                        replayed = self.replay(commits, current)
                        if replayed is None:
                            return None
                        to_publish = replayed
                        self.bump_counter("commuted_publishes")
                    try:
                        self.advance(current, to_publish[-1])
                        return to_publish
                    except Contention:
                        continue
                return None
        finally:
            finished = time.perf_counter()
            self.bump_counter("publish_ms", round((finished - started) * 1000, 1))
            if held_since is not None:
                self.bump_counter("publish_hold_ms", round((finished - held_since) * 1000, 1))
            self.bump_counter("publishes")

    def cleanup(self, session: dict) -> None:
        if session.get("worktree") and Path(session["worktree"]).exists():
            git_run("worktree", "remove", "--force", session["worktree"], cwd=self.root)
        # update-ref -d, not `branch -D`: branch -D rewrites .git/config, which races with
        # concurrent git readers in other lanes on Windows.
        ref = f"refs/heads/{session['branch']}"
        for _ in range(5):
            exists = git_run("rev-parse", "--verify", "--quiet", ref, cwd=self.root).returncode == 0
            if not exists or git_run("update-ref", "-d", ref, cwd=self.root).returncode == 0:
                break
            time.sleep(0.1)  # packed-refs lock held by a concurrent lane
        if session.get("worktree"):
            git_run("worktree", "prune", cwd=self.root)

    def archive(self, session: dict) -> Path:
        head = session.get("head")
        if not head:
            proc = git_run("rev-parse", "--verify", session["branch"], cwd=self.root)
            head = proc.stdout.strip() if proc.returncode == 0 else None
        patch = git("diff", session["base"], head, cwd=self.root) if head else ""
        patch, _ = redact(patch)
        path = self.state / "attempts" / f"{session['id']}.json.gz"
        record = {"session": session, "head": head, "patch": patch}
        write_atomic(path, gzip.compress(json.dumps(record).encode("utf-8")))
        return path

    # ---------------------------------------------------------------- landing
    def still_queued(self, session: dict) -> bool:
        current = self.load(session["id"])
        return current["status"] == "queued" and current.get("head") == session["head"]

    def land(self, batch: list[dict]) -> list[dict]:
        """Merge, test and publish a batch. Returns sessions deferred to the next round."""
        for _ in range(5):
            batch = [s for s in batch if self.still_queued(s)]
            tip = self.sync_tip()
            candidate = tip
            landed: list[tuple[dict, str]] = []
            deferred: list[dict] = []
            for session in batch:
                proc = git_run("merge-tree", "--write-tree", "--name-only", "--no-messages",
                               candidate, session["head"], cwd=self.root)
                if proc.returncode == 0:
                    tree = proc.stdout.split()[0]
                    if tree == self.tree_of(candidate):
                        self.reject(session, "empty", "no changes relative to target")
                        continue
                    candidate = self.squash_commit(session, tree, candidate)
                    landed.append((session, candidate))
                elif proc.returncode == 1 and proc.stdout.strip():  # a tree id first, then conflicted paths
                    conflicted = sorted({line for line in proc.stdout.splitlines()[1:] if line})
                    if candidate != tip and git_run("merge-tree", "--write-tree", tip,
                                                    session["head"], cwd=self.root).returncode == 0:
                        deferred.append(session)  # conflicts only with an untested batch peer
                    else:
                        self.reject(session, "conflict", ", ".join(conflicted))
                else:
                    raise GateError(f"merge-tree failed for session {session['id']}: {proc.stderr.strip()}")
            if not landed:
                return deferred

            files = self.changed_files(tip, candidate)
            ok, log = self.run_tests(candidate, files)
            if ok:
                published = self.publish(tip, [commit for _, commit in landed], files)
                if published is None:
                    continue  # overlapping change landed meanwhile: rebuild and re-test
                fault_point("after-publish")
                now = time.time()
                for (session, _), commit in zip(landed, published, strict=True):
                    if self.update(session["id"], session["head"], status="merged",
                                   merged_commit=commit, finished=now, reason=None):
                        self.cleanup(session)
                    lane = session.get("lane", "default")
                    OUT.event("merged", f"merged   {session['id']:<24} {commit[:10]}  [{lane}]",
                              session=session["id"], commit=commit, lane=lane)
                return deferred
            if len(landed) == 1:
                self.reject(landed[0][0], "tests", log)
                return deferred
            sessions = [session for session, _ in landed]
            middle = len(sessions) // 2
            deferred += self.land(sessions[:middle])
            deferred += self.land(sessions[middle:])
            return deferred
        raise GateError("target kept moving during publish; gave up after 5 attempts")

    def reconcile_reverts(self, tip: str, since: str | None) -> list[str]:
        """Crash recovery for verify: a revert published by a run that died before recording it carries
        `Reverts:` and `Agent-Session:` trailers, so mark that session reverted now."""
        if since and git_run("merge-base", "--is-ancestor", since, tip, cwd=self.root).returncode == 0:
            span = [f"{since}..{tip}"]
        else:
            span = ["-n", "200", tip]
        log = git("log", "--format=%H%x1f%(trailers:key=Reverts,valueonly,separator=%x2C)%x1f"
                  "%(trailers:key=Agent-Session,valueonly,separator=%x2C)", *span, cwd=self.root)
        recovered = []
        for line in log.splitlines():
            commit, reverts, sid = (line.split("\x1f") + ["", ""])[:3]
            sid = sid.strip()
            if not reverts.strip() or not sid or not self.session_path(sid).exists():
                continue
            if self.load(sid)["status"] == "reverted":
                continue
            if self.update(sid, status="reverted", reason="post-merge verification failed",
                           reverted_by=commit, finished=time.time(), recovered=True):
                recovered.append(sid)
                OUT.event("recovered", f"recovered revert of {sid} ({commit[:10]}, published by an interrupted "
                          f"verify)", session=sid, commit=commit)
        return recovered

    def reconcile(self, lane: str | None = None) -> list[str]:
        """Crash recovery. A process killed after publishing but before recording the result
        leaves sessions 'queued' although their squash commit is on the target. Every gate commit
        carries an Agent-Head trailer, so find those sessions in target history and mark them
        merged instead of landing them twice."""
        queued = [s for s in self.sessions() if s["status"] == "queued"
                  and (lane is None or s.get("lane", "default") == lane)]
        if not queued:
            return []
        tip = self.sync_tip()
        landed_heads: dict[str, dict[str, str]] = {}
        recovered = []
        for session in queued:
            base = session["base"]
            if base not in landed_heads:
                log = git("log", "--format=%H%x00%(trailers:key=Agent-Head,valueonly,separator=%x2C)",
                          tip, f"^{base}", cwd=self.root)
                heads: dict[str, str] = {}
                for line in log.splitlines():
                    commit, _, trailer = line.partition("\x00")
                    for head in filter(None, trailer.strip().split(",")):
                        heads.setdefault(head.strip(), commit)
                landed_heads[base] = heads
            landed = landed_heads[base].get(session["head"])
            if landed and self.update(session["id"], session["head"], status="merged", merged_commit=landed,
                                      finished=time.time(), reason=None, recovered=True):
                self.cleanup(session)
                recovered.append(session["id"])
                OUT.event("recovered", f"recovered {session['id']:<23} {landed[:10]}  (published by an "
                          f"interrupted run)", session=session["id"], commit=landed)
        return recovered

    def drain_lane(self, lane: str, drain: bool, wait: float) -> None:
        with self.lock(f"lane-{lane}", timeout_s=wait):
            self.reconcile(lane)
            while True:
                queued = sorted((s for s in self.sessions()
                                 if s["status"] == "queued" and s.get("lane", "default") == lane),
                                key=lambda s: s["submitted"])
                if not queued:
                    return
                batch = queued[: self.config["max_batch"]]
                deferred = self.land(batch)
                if deferred and {s["id"] for s in deferred} == {s["id"] for s in batch}:
                    for session in deferred:
                        self.reject(session, "conflict", "conflicts with other queued sessions")
                if not drain:
                    return

    def reject(self, session: dict, reason: str, detail: str) -> None:
        self.update(session["id"], session["head"], status="rejected", reason=reason,
                    detail=detail[-4000:], finished=time.time())
        last_line = detail.strip().splitlines()[-1] if detail.strip() else ""
        OUT.event("rejected", f"rejected {session['id']:<24} {reason}: {last_line[:100]}",
                  session=session["id"], reason=reason, detail=last_line[:300])


# -------------------------------------------------------------------- commands
def cmd_init(gate: Gate, args: argparse.Namespace) -> int:
    path = gate.root / CONFIG_FILE
    created = not path.exists()
    command, evidence = detect_test_command(gate.root)
    if created:
        sample = SAMPLE_CONFIG.replace("test_command = 'python -m unittest discover -s tests'",
                                       f"test_command = {json.dumps(command)}  # detected: {evidence}")
        path.write_text(sample, encoding="utf-8")
        OUT.say(f"wrote {path}")
        OUT.say(f"test_command = {command!r} ({evidence})")
    else:
        OUT.say(f"{path} already exists (left unchanged)")
    git("config", "extensions.worktreeConfig", "true", cwd=gate.root)
    OUT.say(f"next: check test_command, commit {CONFIG_FILE} to '{gate.target}', run `gate doctor`,")
    OUT.say(f"      and run the queue from a checkout that is not on '{gate.target}' (git switch --detach).")
    OUT.result = {"config": str(path), "created": created, "target": gate.target,
                  "test_command": command if created else None, "detected_from": evidence if created else None}
    return 0


def cmd_spawn(gate: Gate, args: argparse.Namespace) -> int:
    sid = args.session
    if not SESSION_ID.match(sid):
        raise GateError("session id must match [A-Za-z0-9][A-Za-z0-9._-]{0,63}")
    if gate.session_path(sid).exists():
        raise GateError(f"session {sid!r} already exists")
    base = gate.sync_tip()
    branch = f"agent/{sid}"
    worktree = gate.agents_dir / sid
    if args.no_worktree:
        git("branch", branch, base, cwd=gate.root)
        worktree = None
    elif args.sparse:
        git("config", "extensions.worktreeConfig", "true", cwd=gate.root)
        git("worktree", "add", "--quiet", "--no-checkout", "-b", branch, str(worktree), base, cwd=gate.root)
        git("sparse-checkout", "set", "--cone", *args.sparse, cwd=worktree)
        git("read-tree", "-mu", "HEAD", cwd=worktree)
    else:
        git("worktree", "add", "--quiet", "-b", branch, str(worktree), base, cwd=gate.root)
    gate.save({
        "id": sid,
        "status": "active",
        "branch": branch,
        "worktree": str(worktree) if worktree else "",
        "base": base,
        "created": time.time(),
        "sparse": args.sparse or [],
    })
    OUT.say(str(worktree) if worktree else f"refs/heads/{branch} @ {base}")
    OUT.result = {"session": sid, "branch": branch, "worktree": str(worktree) if worktree else None,
                  "base": base}
    return 0


def cmd_submit(gate: Gate, args: argparse.Namespace) -> int:
    session = gate.load(args.session)
    if session["status"] in {"queued", "needs_approval"} and session.get("head") == gate.rev(session["branch"]):
        # Idempotent: an agent that retries after a timeout gets the same answer, not an error.
        OUT.say(f"{args.session}: already {session['status']} at {session['head'][:10]}")
        OUT.result = {"session": args.session, "status": session["status"], "head": session["head"],
                      "unchanged": True}
        return 0
    if session["status"] not in {"active", "rejected", "too_large"}:
        raise GateError(f"session is {session['status']}; only active/rejected/too_large can be submitted")
    worktree = session.get("worktree")
    if worktree and Path(worktree).exists() and git("status", "--porcelain", cwd=Path(worktree)).strip():
        raise GateError(f"{worktree} has uncommitted changes; commit (or stash) them, then submit")
    head = gate.rev(session["branch"])
    gate.sync_tip()  # judge the change against the current target and its current policy
    base = git("merge-base", f"refs/heads/{gate.target}", head, cwd=gate.root).strip()
    if head == base:
        raise GateError(f"no commits on {session['branch']} yet; commit the change"
                        + (f" in {worktree}" if worktree else " and push the branch") + ", then submit")
    files, changed, sized = gate.diff_stats(base, head)
    tier = gate.risk_tier(files)
    prompt_sha = None
    if args.prompt_file:
        prompt_sha = hashlib.sha256(Path(args.prompt_file).read_bytes()).hexdigest()
    title = args.title or git("log", "-1", "--format=%s", head, cwd=gate.root).strip()

    findings: list[str] = []
    if gate.config["secret_scan"]:
        diff = git("diff", "--no-renames", base, head, cwd=gate.root)
        findings = secrets_in_diff(diff, gate.config["secret_scan_exempt"])
        added = "\n".join(line[1:] for line in diff.splitlines()
                          if line.startswith("+") and not line.startswith("+++"))
        external = gate.external_scan(added)
        if external:
            findings.append(f"external scanner: {external}")

    limit = gate.config["max_changed_lines"]
    detail = None
    if findings:
        status, reason = "rejected", "secret"
        detail = "possible secrets on added lines (values not shown):\n" + "\n".join(findings)
    elif sized > limit:
        status, reason = "too_large", f"{sized} changed lines > {limit}; split the change"
    elif tier >= gate.config["approval_tier"]:
        status, reason = "needs_approval", None
    else:
        status, reason = "queued", None
    with gate.lock("state"):
        session = gate.load(args.session)
        session.update({
            "status": status,
            "reason": reason,
            "detail": detail,
            "head": head,
            "base": base,
            "title": title,
            "model": args.model,
            "tool": args.tool,
            "spec": args.spec,
            "prompt_sha256": prompt_sha,
            "files": files,
            "changed_lines": changed,
            "sized_lines": sized,
            "tier": tier,
            "lane": gate.lane_of(files),
            "approved_by": None,
            "submitted": time.time(),
            "finished": time.time() if status == "rejected" else None,
            "submissions": session.get("submissions", 0) + 1,
        })
        gate.save(session)
    OUT.say(f"{args.session}: {status} (tier {tier}, lane {session['lane']}, {sized} lines, "
            f"{len(files)} files)" + (f" - {reason}" if reason else ""))
    if detail:
        OUT.say(detail)
    OUT.result = {"session": args.session, "status": status, "reason": reason, "head": head, "tier": tier,
                  "lane": session["lane"], "sized_lines": sized, "files": files, "findings": findings}
    return EXIT_POLICY if status in {"too_large", "rejected"} else 0


def cmd_approve(gate: Gate, args: argparse.Namespace) -> int:
    session = gate.load(args.session)
    already = session["status"] == "queued" and session.get("approved_by") == args.by
    if session["status"] != "needs_approval" and not already:
        raise GateError(f"session is {session['status']}, not needs_approval")
    if not already:
        gate.update(args.session, session["head"], status="queued", approved_by=args.by)
    OUT.say(f"{args.session}: approved by {args.by}, queued at {session['head'][:10]}")
    OUT.result = {"session": args.session, "status": "queued", "approved_by": args.by, "head": session["head"]}
    return 0


def cmd_queue(gate: Gate, args: argparse.Namespace) -> int:
    if gate.target_checked_out():
        raise GateError(f"'{gate.target}' is checked out in a worktree; run the gate from a "
                        f"checkout on another branch or detached HEAD")
    queued_lanes = sorted({s.get("lane", "default") for s in gate.sessions() if s["status"] == "queued"})
    lanes = [lane for lane in queued_lanes if not args.lane or lane in args.lane]
    if args.parallel <= 1 or len(lanes) <= 1:
        for lane in lanes:
            gate.drain_lane(lane, args.drain, args.wait)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=args.parallel) as pool:
            futures = [pool.submit(gate.drain_lane, lane, args.drain, args.wait) for lane in lanes]
            for future in futures:
                future.result()
    if not lanes:
        OUT.say("queue: nothing queued")
    by_kind: dict[str, list] = {"merged": [], "rejected": [], "recovered": []}
    for event in OUT.events:
        if event["event"] in by_kind:
            by_kind[event["event"]].append({k: v for k, v in event.items() if k != "event"})
    remaining = sum(1 for s in gate.sessions() if s["status"] == "queued")
    OUT.result = {"lanes": lanes, **by_kind, "still_queued": remaining}
    return 0


def cmd_verify(gate: Gate, args: argparse.Namespace) -> int:
    """Test the target; on failure bisect landed commits and auto-revert the culprit."""
    with gate.lock("verify", timeout_s=args.wait):
        tip = gate.sync_tip()
        state = gate.read_json("verify.json", {})
        last_green = state.get("last_green")
        OUT.result = {"target": gate.target, "tip": tip, "last_green": last_green,
                      "recovered": gate.reconcile_reverts(tip, last_green)}
        ok, log = gate.run_tests(tip, None)
        if ok:
            gate.write_json(gate.state / "verify.json", {"last_green": tip, "checked": time.time()})
            OUT.say(f"green    {gate.target} @ {tip[:10]}")
            OUT.result["status"] = "green"
            return 0
        OUT.say(f"red      {gate.target} @ {tip[:10]}")
        OUT.result.update(status="red", log=log[-1500:])
        if not last_green or git_run("merge-base", "--is-ancestor", last_green, tip,
                                     cwd=gate.root).returncode != 0:
            OUT.say("no known-green baseline to bisect from; fix manually, then run verify again")
            OUT.say(log[-1500:])
            return EXIT_NEEDS_HUMAN
        commits = git("rev-list", "--reverse", "--first-parent", f"{last_green}..{tip}",
                      cwd=gate.root).split()
        low, high = 0, len(commits) - 1
        while low < high:
            middle = (low + high) // 2
            if gate.run_tests(commits[middle], None)[0]:
                low = middle + 1
            else:
                high = middle
        culprit = commits[low]
        sid = git("log", "-1", "--format=%(trailers:key=Agent-Session,valueonly)", culprit,
                  cwd=gate.root).strip()
        title = git("log", "-1", "--format=%s", culprit, cwd=gate.root).strip()
        OUT.say(f"culprit  {culprit[:10]} {title!r}" + (f" (session {sid})" if sid else " (not from gate)"))
        OUT.result.update(culprit=culprit, culprit_session=sid or None, culprit_title=title)
        if not sid or args.no_revert:
            return EXIT_NEEDS_HUMAN
        proc = git_run("merge-tree", "--write-tree", f"--merge-base={culprit}", tip, f"{culprit}^",
                       cwd=gate.root)
        if proc.returncode != 0:
            OUT.say("automatic revert conflicts with later commits; revert manually")
            return EXIT_NEEDS_HUMAN
        message = (f'Revert "{title}"\n\nPost-merge verification failed on {gate.target} @ {tip[:12]};\n'
                   f"bisected to this agent change.\n\nReverts: {culprit}\nAgent-Session: {sid}\n")
        env = {**os.environ, "GIT_AUTHOR_NAME": "gate", "GIT_AUTHOR_EMAIL": "gate@localhost",
               "GIT_COMMITTER_NAME": "gate", "GIT_COMMITTER_EMAIL": "gate@localhost"}
        revert = git("commit-tree", proc.stdout.split()[0], "-p", tip, "-m", message,
                     cwd=gate.root, env=env).strip()
        if not gate.run_tests(revert, None)[0]:
            OUT.say("revert did not restore green; leaving target unchanged")
            return EXIT_NEEDS_HUMAN
        published = gate.publish(tip, [revert], gate.changed_files(tip, revert))
        if published is None:
            OUT.say(f"{gate.target} moved during verification; run verify again")
            return EXIT_TARGET_MOVED
        fault_point("after-revert-publish")
        with contextlib.suppress(GateError):
            gate.update(sid, status="reverted", reason="post-merge verification failed",
                        detail=log, reverted_by=published[-1], finished=time.time())
        gate.write_json(gate.state / "verify.json", {"last_green": published[-1], "checked": time.time()})
        OUT.say(f"reverted {sid} with {published[-1][:10]}; {gate.target} is green again")
        OUT.result.update(status="reverted", revert_commit=published[-1])
        return 0


def cmd_abandon(gate: Gate, args: argparse.Namespace) -> int:
    session = gate.load(args.session)
    if session["status"] == "merged":
        raise GateError("session is already merged")
    path = gate.archive(session)
    gate.cleanup(session)
    gate.update(args.session, status="abandoned", finished=time.time(),
                reason=session.get("reason") or "abandoned")
    OUT.say(f"{args.session}: abandoned, diff archived to {path}")
    OUT.result = {"session": args.session, "status": "abandoned", "archive": str(path)}
    return 0


def cmd_trace(gate: Gate, args: argparse.Namespace) -> int:
    gate.load(args.session)
    raw = Path(args.file).read_text(encoding="utf-8", errors="replace")
    clean, redactions = redact(raw)
    external = gate.external_scan(clean)
    if external:
        raise GateError(f"trace NOT stored: external scanner still finds secrets after redaction: {external}")
    data = clean.encode("utf-8")
    path = gate.state / "traces" / f"{args.session}.jsonl.gz"
    write_atomic(path, gzip.compress(data))
    record = {
        "sha256": hashlib.sha256(data).hexdigest(),
        "raw_bytes": len(data),
        "stored_bytes": path.stat().st_size,
        "redactions": redactions,
        "stored": time.time(),
    }
    gate.update(args.session, trace=record)
    OUT.say(f"{args.session}: trace stored ({len(data)} B -> {path.stat().st_size} B gz, "
            f"{redactions} redactions)")
    OUT.result = {"session": args.session, **record}
    return 0


def cmd_gc(gate: Gate, args: argparse.Namespace) -> int:
    retention = gate.config["retention"]
    now = time.time()
    archived = traces_deleted = attempts_deleted = 0
    recovered = gate.reconcile()
    branches_cleaned = 0
    for session in gate.sessions():
        if session["status"] in {"merged", "abandoned"}:
            ref = f"refs/heads/{session['branch']}"
            leftover = git_run("rev-parse", "--verify", "--quiet", ref, cwd=gate.root).returncode == 0
            if leftover or (session.get("worktree") and Path(session["worktree"]).exists()):
                gate.cleanup(session)  # a crash between recording the result and cleaning up
                branches_cleaned += 1
        finished = session.get("finished") or session["created"]
        grace = retention["reject_grace_hours"] * 3600
        if session["status"] == "rejected" and now - finished >= grace:
            gate.archive(session)
            # Only if nobody resubmitted it meanwhile: an agent may have fixed and resubmitted it after
            # this loop read the session list, and then its branch must stay.
            if gate.update(session["id"], session.get("head"), "rejected", status="abandoned"):
                gate.cleanup(session)
                session["status"] = "abandoned"
                archived += 1
        trace = session.get("trace")
        if trace and not trace.get("deleted") and session["status"] not in IN_FLIGHT:
            days = retention["trace_days_merged"] if session["status"] == "merged" \
                else retention["trace_days_unmerged"]
            if now - finished >= days * 86400:
                (gate.state / "traces" / f"{session['id']}.jsonl.gz").unlink(missing_ok=True)
                gate.update(session["id"], trace={**trace, "deleted": now})
                traces_deleted += 1
    for path in (gate.state / "attempts").glob("*.json.gz"):
        if now - path.stat().st_mtime >= retention["attempt_days"] * 86400:
            path.unlink()
            attempts_deleted += 1
    git_run("worktree", "prune", cwd=gate.root)
    OUT.say(f"gc: archived {archived} rejected sessions, deleted {traces_deleted} traces, "
            f"{attempts_deleted} attempt archives, cleaned {branches_cleaned} leftover branches, "
            f"recovered {len(recovered)} interrupted merges")
    OUT.result = {"archived": archived, "traces_deleted": traces_deleted, "attempts_deleted": attempts_deleted,
                  "branches_cleaned": branches_cleaned, "recovered": recovered}
    return 0


def cmd_status(gate: Gate, args: argparse.Namespace) -> int:
    rows = gate.sessions()
    if not args.all:
        rows = [s for s in rows if s["status"] in IN_FLIGHT | {"reverted"}]
    order = {"needs_approval": 0, "reverted": 1, "too_large": 2, "rejected": 3, "queued": 4, "active": 5}
    rows.sort(key=lambda s: (order.get(s["status"], 9), s.get("submitted") or s["created"]))
    OUT.say(f"{'SESSION':<24} {'STATUS':<15} {'TIER':>4} {'LANE':<10} {'LINES':>6} {'FILES':>5}  "
            f"{'MODEL':<18} REASON")
    for s in rows:
        OUT.say(f"{s['id']:<24} {s['status']:<15} {s.get('tier', '-')!s:>4} {s.get('lane', '-'):<10} "
                f"{s.get('sized_lines', '-')!s:>6} {len(s.get('files', [])):>5}  "
                f"{(s.get('model') or '-'):<18} {s.get('reason') or ''}")
    fields = ("id", "status", "reason", "tier", "lane", "sized_lines", "model", "head", "branch", "worktree",
              "submitted", "finished", "merged_commit")
    OUT.result = {"sessions": [{key: s.get(key) for key in fields} for s in rows]}
    return 0


def cmd_show(gate: Gate, args: argparse.Namespace) -> int:
    session = gate.load(args.session)
    OUT.result = {"session": session}
    OUT.say(json.dumps({k: v for k, v in session.items() if k != "detail"}, indent=2))
    if session.get("detail"):
        OUT.say("\n--- detail ---\n" + session["detail"])
    if session.get("head"):
        diffstat = git("diff", "--stat", session["base"], session["head"], cwd=gate.root)
        OUT.say(diffstat)
        OUT.result["diffstat"] = diffstat
        if args.patch:
            patch = git("diff", session["base"], session["head"], cwd=gate.root)
            OUT.say(patch)
            OUT.result["patch"] = patch
    return 0


def cmd_stats(gate: Gate, args: argparse.Namespace) -> int:
    rows = gate.sessions()
    by_status: dict[str, int] = {}
    by_model: dict[str, dict[str, int]] = {}
    reasons: dict[str, int] = {}
    latencies = []
    for s in rows:
        by_status[s["status"]] = by_status.get(s["status"], 0) + 1
        if not s.get("model"):
            continue
        model = by_model.setdefault(s["model"], {"merged": 0, "not_merged": 0})
        if s["status"] == "merged":
            model["merged"] += 1
            latencies.append((s["finished"] - s["submitted"]) / 60)
        elif s["status"] in {"rejected", "abandoned", "too_large", "reverted"}:
            model["not_merged"] += 1
            if s["status"] in {"too_large", "reverted"}:
                reason = s["status"]
            else:
                reason = s.get("reason") or "abandoned"
            reasons[reason] = reasons.get(reason, 0) + 1
    counters = gate.counters()
    ci_runs = counters.get("ci_runs", 0)
    live_refs = len(git("for-each-ref", "--format=%(refname)", "refs/heads/agent/",
                        cwd=gate.root).splitlines())
    attempt_bytes = sum(p.stat().st_size for p in (gate.state / "attempts").glob("*"))
    trace_bytes = sum(p.stat().st_size for p in (gate.state / "traces").glob("*"))
    OUT.say("sessions by status:  " + ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())))
    OUT.say("not-merged reasons:  " + (", ".join(f"{k}={v}" for k, v in sorted(reasons.items())) or "-"))
    for name, counts in sorted(by_model.items()):
        total = counts["merged"] + counts["not_merged"]
        rate = f"{100 * counts['merged'] / total:.0f}%" if total else "-"
        OUT.say(f"model {name:<18} merged {counts['merged']:>4} / {total:<4} ({rate})")
    merged = by_status.get("merged", 0)
    if ci_runs:
        OUT.say(f"CI runs: {ci_runs}  (merged sessions per CI run: {merged / ci_runs:.2f})")
    else:
        OUT.say("CI runs: 0")
    OUT.say(f"suite runs: {counters.get('suite_runs', 0)}  suite cache hits: {counters.get('suite_cache_hits', 0)}")
    publishes = counters.get("publishes", 0)
    if publishes:
        OUT.say(f"publishes: {publishes}  commuted without re-test: {counters.get('commuted_publishes', 0)}  "
              f"mean lock hold: {counters.get('publish_hold_ms', 0) / publishes:.0f} ms  "
              f"(incl. wait: {counters.get('publish_ms', 0) / publishes:.0f} ms)")
    if latencies:
        OUT.say(f"submit->merge median: {statistics.median(latencies):.1f} min")
    OUT.say(f"live agent branches: {live_refs}   attempt archives: {attempt_bytes} B   traces: {trace_bytes} B")
    OUT.result = {
        "by_status": by_status,
        "not_merged_reasons": reasons,
        "by_model": by_model,
        "counters": counters,
        "merged_per_ci_run": merged / ci_runs if ci_runs else None,
        "submit_to_merge_median_min": statistics.median(latencies) if latencies else None,
        "live_agent_branches": live_refs,
        "attempt_bytes": attempt_bytes,
        "trace_bytes": trace_bytes,
    }
    return 0


def cmd_doctor(gate: Gate, args: argparse.Namespace) -> int:
    failures = 0
    checks: list[dict] = []

    def report(ok: bool | None, name: str, detail: str = "") -> None:
        nonlocal failures
        mark = {True: "ok  ", False: "FAIL", None: "warn"}[ok]
        failures += ok is False
        checks.append({"check": name, "status": mark.strip().lower(), "detail": detail})
        OUT.say(f"[{mark}] {name}" + (f" - {detail}" if detail else ""))

    version = git_version()
    report(version >= MIN_GIT, "git version", f"{version[0]}.{version[1]} (need >= {MIN_GIT[0]}.{MIN_GIT[1]})")
    report(sys.version_info >= (3, 11), "python version", sys.version.split()[0])
    target_ok = git_run("rev-parse", "--verify", f"refs/heads/{gate.target}", cwd=gate.root).returncode == 0
    report(target_ok, f"target branch '{gate.target}' exists")
    has_policy = git_run("cat-file", "-e", f"refs/heads/{gate.target}:{CONFIG_FILE}",
                         cwd=gate.root).returncode == 0
    report(has_policy, f"{CONFIG_FILE} committed on target", "" if has_policy else "using built-in defaults")
    suites = gate.suites()
    report(bool(suites) or None, "test suites", ", ".join(s["name"] for s in suites) or "none: nothing is tested")
    checked_out = gate.target_checked_out()
    if checked_out and args.fix:
        current = git_run("symbolic-ref", "--quiet", "HEAD", cwd=gate.root).stdout.strip()
        main_clean = not git("status", "--porcelain", "--untracked-files=no", cwd=gate.root).strip()
        if current == f"refs/heads/{gate.target}" and main_clean:
            git("switch", "--quiet", "--detach", cwd=gate.root)
            OUT.say(f"fixed: detached {gate.root} from '{gate.target}'")
            checked_out = gate.target_checked_out()
    report(not checked_out, "target not checked out in any worktree",
           "run `gate doctor --fix` (main checkout) or switch that worktree away" if checked_out else "")
    lanes = gate.config["lanes"]
    report(True if lanes else None, "lanes", ", ".join(lanes) or "none: all sessions share one queue")
    if gate.config["secret_scan_command"]:
        dirty = gate.external_scan("aws_secret = 'AKIAABCDEFGHIJKLMNOP'\n")
        clean = gate.external_scan("hello world\n")
        report(dirty is not None and clean is None, "external secret scanner",
               "flags a known fake key and passes clean text" if dirty and not clean
               else "did not behave as expected on known inputs")
    remote = gate.config["remote"]
    if remote:
        proc = git_run("ls-remote", "--exit-code", remote, f"refs/heads/{gate.target}", cwd=gate.root)
        report(proc.returncode == 0, f"remote '{remote}' reachable, has {gate.target}",
               proc.stderr.strip()[-200:])
        if args.write_test and proc.returncode == 0:
            probe = f"refs/heads/gate-doctor-{uuid.uuid4().hex[:8]}"
            tip = gate.rev(f"refs/heads/{gate.target}")
            pushed = git_run("push", "--quiet", remote, f"{tip}:{probe}", cwd=gate.root)
            deleted = git_run("push", "--quiet", remote, f":{probe}", cwd=gate.root)
            report(pushed.returncode == 0 and deleted.returncode == 0, "push access",
                   "push access ok (scratch ref created and deleted)" if pushed.returncode == 0
                   else pushed.stderr.strip()[-300:])
            OUT.say("       note: branch protection on the target itself is only exercised by a real publish;")
            OUT.say("       give the gate's bot a ruleset bypass for the target branch.")
    OUT.say("doctor: " + ("all checks passed" if not failures else f"{failures} failing check(s)"))
    OUT.result = {"checks": checks, "failures": failures}
    return 0 if not failures else EXIT_ERROR


HELP_EPILOG = """\
workflow (one agent session):
  gate spawn S1                      branch + worktree from the target tip; prints the worktree path
  ...agent commits in that worktree (or pushes refs/heads/agent/S1 with --no-worktree)...
  gate submit S1 --model <id>        policy checks; status queued | needs_approval | too_large | rejected
  gate queue --drain                 merge, test and publish everything queued (run by one gate process)
  gate verify                        post-merge check of the target; bisects and reverts a regression

automation: add --json before the subcommand for one JSON document on stdout:
  {"command": ..., "exit_code": ..., "result": {...}, "events": [...]}   or   {"error": ...}

exit codes:
  0  ok
  1  error (usage, git, environment; doctor found a failing check)
  2  submit bounced by policy (too large, or secrets on added lines)
  3  verify: target is red and needs a human (no baseline, revert conflicts, or --no-revert)
  4  verify: target moved during the revert; run verify again
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="gate",
        description=__doc__.split("\n\n")[0] if __doc__ else "gate",
        epilog=HELP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("-C", dest="repo", default=".", metavar="PATH", help="repository path (default: cwd)")
    parser.add_argument("--json", action="store_true",
                        help="print one machine-readable JSON document instead of text")
    parser.add_argument("--version", action="version", version=f"gate {__version__}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    sub.add_parser("init", help="write a sample gate.toml and enable per-worktree config",
                   description="Write a commented sample gate.toml in the repository root (never overwrites).")

    spawn = sub.add_parser("spawn", help="create a branch (+ worktree) for an agent session",
                           description="Create refs/heads/agent/<session> at the target tip, plus a worktree "
                                       "next to the repository unless --no-worktree.")
    spawn.add_argument("session", help="session id: letters, digits, . _ - (max 64)")
    spawn.add_argument("--sparse", nargs="+", metavar="DIR", help="sparse-checkout cone directories")
    spawn.add_argument("--no-worktree", action="store_true",
                       help="branch only; the agent runs elsewhere and pushes refs/heads/agent/<id>")

    submit = sub.add_parser("submit", help="submit a session's commits to the gate",
                            description="Run policy checks (secrets, size, risk tier) and queue the session. "
                                        "Re-submitting an unchanged queued session is a no-op. Exit 2 if bounced.")
    submit.add_argument("session", help="session id")
    submit.add_argument("--model", required=True, help="exact model id (recorded as a commit trailer)")
    submit.add_argument("--tool", help="agent tool and version")
    submit.add_argument("--spec", help="spec/issue reference")
    submit.add_argument("--prompt-file", help="prompt file (only its SHA-256 is recorded)")
    submit.add_argument("--title", help="commit title (default: last commit subject)")

    approve = sub.add_parser("approve", help="human approval for a high-risk session",
                             description="Queue a needs_approval session; the approver is recorded as a trailer.")
    approve.add_argument("session", help="session id")
    approve.add_argument("--by", required=True, help="approver name or handle")

    queue = sub.add_parser("queue", help="merge, test and publish queued sessions",
                           description="Process queued sessions lane by lane: merge-tree, test (batched, "
                                       "bisected on failure), compare-and-swap publish. Also recovers merges "
                                       "published by an interrupted run.")
    queue.add_argument("--drain", action="store_true", help="process until the queue is empty")
    queue.add_argument("--parallel", type=int, default=1, help="lanes processed concurrently (default 1)")
    queue.add_argument("--lane", action="append", help="only these lanes (repeatable)")
    queue.add_argument("--wait", type=float, default=5.0, help="seconds to wait for a lane lock (default 5)")

    verify = sub.add_parser("verify", help="test the target; bisect and auto-revert a regression",
                            description="Run all suites on the target tip. If red, bisect commits since the last "
                                        "green tip and revert the culprit if it came from gate. Exit 3/4, see below.",
                            epilog=HELP_EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    verify.add_argument("--no-revert", action="store_true", help="report the culprit only")
    verify.add_argument("--wait", type=float, default=5.0, help="seconds to wait for the verify lock")

    abandon = sub.add_parser("abandon", help="archive a session's diff and delete its branch")
    abandon.add_argument("session", help="session id")

    trace = sub.add_parser("trace", help="store a redacted, compressed agent trace",
                           description="Redact secrets from an agent trace, gzip it under .git/gate/traces and "
                                       "record its SHA-256 (added to the commit as Trace-SHA256).")
    trace.add_argument("session", help="session id")
    trace.add_argument("file", help="trace file (any text, e.g. JSONL)")

    sub.add_parser("gc", help="apply retention; finish cleanup interrupted by a crash")

    status = sub.add_parser("status", help="in-flight sessions, review items first")
    status.add_argument("--all", action="store_true", help="include merged and abandoned sessions")

    show = sub.add_parser("show", help="session details and diffstat")
    show.add_argument("session", help="session id")
    show.add_argument("--patch", action="store_true", help="include the full diff")

    sub.add_parser("stats", help="throughput, merge rate by model, retention footprint")

    doctor = sub.add_parser("doctor", help="check git, policy, tests, scanner and remote readiness")
    doctor.add_argument("--write-test", action="store_true",
                        help="push and delete a scratch ref on the remote to prove write access")
    doctor.add_argument("--fix", action="store_true",
                        help="detach the main checkout if it has the target branch checked out (and is clean)")

    completion = sub.add_parser("completion", help="print a shell completion script (bash, zsh, fish)",
                                description="Print a completion script, e.g. `gate completion bash >> ~/.bashrc` "
                                            "or `gate completion fish > ~/.config/fish/completions/gate.fish`.")
    completion.add_argument("shell", choices=["bash", "zsh", "fish"])

    # --json is accepted before or after the subcommand (`gate --json queue` or `gate queue --json`).
    for subparser in sub.choices.values():
        subparser.add_argument("--json", action="store_true", default=argparse.SUPPRESS,
                               help="print one machine-readable JSON document instead of text")
    return parser


def completion_script(shell: str) -> str:
    """Completion generated from the parser itself, so it can't drift from the real options."""
    parser = build_parser()
    subactions = [a for a in parser._actions if isinstance(a, argparse._SubParsersAction)]
    commands = dict(subactions[0].choices) if subactions else {}
    global_opts = sorted(o for a in parser._actions for o in a.option_strings if o.startswith("--"))
    options = {name: sorted(o for a in sub_parser._actions for o in a.option_strings if o.startswith("--"))
               for name, sub_parser in commands.items()}
    if shell == "fish":
        lines = [f"complete -c gate -f -n __fish_use_subcommand -a {name!r}" for name in commands]
        for name, opts in options.items():
            lines += [f"complete -c gate -f -n '__fish_seen_subcommand_from {name}' -l {opt[2:]}" for opt in opts]
        lines += [f"complete -c gate -f -n __fish_use_subcommand -l {opt[2:]}" for opt in global_opts]
        return "\n".join(lines) + "\n"
    cases = "\n".join(f'        {name}) opts="{" ".join(opts)}" ;;' for name, opts in options.items())
    script = f"""_gate() {{
    local cur="${{COMP_WORDS[COMP_CWORD]}}" cmd="" word opts
    for word in "${{COMP_WORDS[@]:1:COMP_CWORD-1}}"; do
        case "$word" in -*) ;; *) cmd="$word"; break ;; esac
    done
    if [ -z "$cmd" ]; then
        COMPREPLY=($(compgen -W "{' '.join(commands)} {' '.join(global_opts)}" -- "$cur"))
        return
    fi
    case "$cmd" in
{cases}
        *) opts="" ;;
    esac
    COMPREPLY=($(compgen -W "$opts" -- "$cur"))
}}
complete -o default -F _gate gate
"""
    if shell == "zsh":
        script = "autoload -U +X bashcompinit && bashcompinit\n" + script
    return script


def cmd_completion(gate: Gate, args: argparse.Namespace) -> int:
    script = completion_script(args.shell)
    OUT.say(script.rstrip("\n"))
    OUT.result = {"shell": args.shell, "script": script}
    return 0


COMMANDS = {
    "init": cmd_init,
    "spawn": cmd_spawn,
    "submit": cmd_submit,
    "approve": cmd_approve,
    "queue": cmd_queue,
    "verify": cmd_verify,
    "abandon": cmd_abandon,
    "trace": cmd_trace,
    "gc": cmd_gc,
    "status": cmd_status,
    "show": cmd_show,
    "stats": cmd_stats,
    "doctor": cmd_doctor,
    "completion": cmd_completion,
}


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    OUT.reset(json_mode=args.json)
    try:
        gate = Gate(Path(args.repo).resolve())
        code = COMMANDS[args.command](gate, args)
    except GateError as error:
        if args.json:
            print(json.dumps({"command": args.command, "exit_code": EXIT_ERROR, "error": str(error),
                              "events": OUT.events}))
        else:
            print(f"gate: {error}", file=sys.stderr)
        return EXIT_ERROR
    if args.json:
        print(json.dumps({"command": args.command, "exit_code": code, "result": OUT.result,
                          "events": OUT.events}, default=str))
    return code


if __name__ == "__main__":
    sys.exit(main())
