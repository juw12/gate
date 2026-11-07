"""End-to-end tests: real git repos, real worktrees, real test runs."""
from __future__ import annotations

import contextlib
import gzip
import io
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

import gate

PY = sys.executable

CALC = """\
def add(a, b):
    return a + b


def sub(a, b):
    return a - b
"""

CALC_TEST = """\
import unittest

from calc import add, sub


class CalcTest(unittest.TestCase):
    def test_add(self):
        self.assertEqual(add(2, 3), 5)

    def test_sub(self):
        self.assertEqual(sub(5, 3), 2)
"""


def run_git(*args: str, cwd: Path, env: dict | None = None, input_text: str | None = None) -> str:
    # Bytes in/out: text-mode stdin on Windows would turn "\n" into "\r\n" inside blobs.
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, env=env,
                          input=input_text.encode() if input_text is not None else None)
    if proc.returncode != 0:
        raise AssertionError(f"git {' '.join(args)}: {proc.stderr.decode(errors='replace')}")
    return proc.stdout.decode(errors="replace").replace("\r\n", "\n")


def remove_tree(path: Path) -> None:
    def force(func, target, _exc):
        os.chmod(target, stat.S_IWRITE)
        func(target)

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=force)
    else:
        shutil.rmtree(path, onerror=force)


class GateTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="gate-test-"))
        self.repo = self.tmp / "repo"

    def tearDown(self):
        remove_tree(self.tmp)

    # ------------------------------------------------------------ fixtures
    def make_repo(self, overrides: dict[str, str] | None = None, lanes: dict[str, list[str]] | None = None,
                  append: str = "", remote: bool = False) -> None:
        self.repo.mkdir()
        run_git("init", "-q", "-b", "main", cwd=self.repo)
        run_git("config", "user.name", "Test Human", cwd=self.repo)
        run_git("config", "user.email", "human@example.com", cwd=self.repo)
        files = {
            "calc.py": CALC,
            "tests/test_calc.py": CALC_TEST,
            "auth/policy.py": "ALLOW_ALL = False\n",
            "docs/guide.md": "# Guide\n",
            "lib/__init__.py": "",
        }
        for name, content in files.items():
            path = self.repo / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        settings = {"test_command": f"'\"{PY}\" -m unittest discover -s tests -q'"}
        if remote:
            settings["remote"] = "'origin'"
        settings.update(overrides or {})
        lines = gate.SAMPLE_CONFIG.splitlines()
        for key, value in settings.items():
            index = next(i for i, line in enumerate(lines) if line.startswith(f"{key} ="))
            lines[index] = f"{key} = {value}"
        config = "\n".join(lines) + "\n"
        if lanes:
            entries = "".join(f"{name} = {json.dumps(patterns)}\n" for name, patterns in lanes.items())
            config = config.replace("[lanes]\n", "[lanes]\n" + entries)
        (self.repo / "gate.toml").write_text(config + append)
        run_git("add", ".", cwd=self.repo)
        run_git("commit", "-qm", "initial", cwd=self.repo)
        if remote:
            self.origin = self.tmp / "origin.git"
            run_git("clone", "-q", "--bare", str(self.repo), str(self.origin), cwd=self.tmp)
            run_git("remote", "add", "origin", str(self.origin), cwd=self.repo)
        run_git("switch", "-q", "--detach", cwd=self.repo)

    def gate(self, *argv: str) -> tuple[int, str]:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = gate.main(["-C", str(self.repo), *argv])
        return code, out.getvalue()

    def spawn(self, sid: str, *extra: str) -> Path:
        code, out = self.gate("spawn", sid, *extra)
        self.assertEqual(code, 0, out)
        return Path(out.strip().splitlines()[-1])

    def commit(self, worktree: Path, files: dict[str, str], message: str) -> None:
        for name, content in files.items():
            path = worktree / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        run_git("add", "-A", cwd=worktree)
        run_git("-c", "user.name=Agent Bot", "-c", "user.email=agent@example.com",
                "commit", "-qm", message, cwd=worktree)

    def commit_on_main(self, files: dict[str, str], message: str = "outside change") -> str:
        """Advance main directly (as another lane or a human would), without a checkout."""
        env = {**os.environ, "GIT_INDEX_FILE": str(self.tmp / "side.index"),
               "GIT_AUTHOR_NAME": "Other", "GIT_AUTHOR_EMAIL": "o@x",
               "GIT_COMMITTER_NAME": "Other", "GIT_COMMITTER_EMAIL": "o@x"}
        run_git("read-tree", "main", cwd=self.repo, env=env)
        for name, content in files.items():
            blob = run_git("hash-object", "-w", "--stdin", cwd=self.repo, input_text=content).strip()
            run_git("update-index", "--add", "--cacheinfo", f"100644,{blob},{name}", cwd=self.repo, env=env)
        tree = run_git("write-tree", cwd=self.repo, env=env).strip()
        commit = run_git("commit-tree", tree, "-p", "main", "-m", message, cwd=self.repo, env=env).strip()
        run_git("update-ref", "refs/heads/main", commit, cwd=self.repo)
        return commit

    def agent(self, sid: str, files: dict[str, str], model: str = "model-a") -> tuple[int, str]:
        worktree = self.spawn(sid)
        self.commit(worktree, files, f"{sid}: change")
        return self.gate("submit", sid, "--model", model, "--spec", f"SPEC-{sid}")

    def feature(self, n: int) -> dict[str, str]:
        return {
            f"lib/feature_{n}.py": f"def feature_{n}():\n    return {n}\n",
            f"tests/test_feature_{n}.py": (
                f"import unittest\nfrom lib.feature_{n} import feature_{n}\n\n\n"
                f"class T(unittest.TestCase):\n    def test(self):\n"
                f"        self.assertEqual(feature_{n}(), {n})\n"
            ),
        }

    def session(self, sid: str) -> dict:
        path = self.repo / ".git" / "gate" / "sessions" / f"{sid}.json"
        return json.loads(path.read_text())

    def main_log(self) -> str:
        return run_git("log", "--format=%B%x00", "main", cwd=self.repo)

    def main_file(self, name: str) -> str:
        return run_git("show", f"main:{name}", cwd=self.repo)

    def counter(self, name: str) -> float:
        path = self.repo / ".git" / "gate" / "counters.json"
        return json.loads(path.read_text()).get(name, 0) if path.exists() else 0

    # ------------------------------------------------------------ core gate
    def test_batch_lands_with_provenance_in_one_ci_run(self):
        self.make_repo()
        for n in (1, 2, 3):
            code, out = self.agent(f"a{n}", self.feature(n))
            self.assertEqual(code, 0, out)
            self.assertIn("queued (tier 1", out)
        code, out = self.gate("queue", "--drain")
        self.assertEqual(code, 0, out)
        for n in (1, 2, 3):
            session = self.session(f"a{n}")
            self.assertEqual(session["status"], "merged")
            self.assertFalse(Path(session["worktree"]).exists())
        self.assertEqual(self.counter("ci_runs"), 1)
        log = self.main_log()
        for n in (1, 2, 3):
            self.assertIn(f"Agent-Session: a{n}", log)
            self.assertIn(f"Spec: SPEC-a{n}", log)
        self.assertIn("Agent-Model: model-a", log)
        author = run_git("log", "-1", "--format=%an|%cn", "main", cwd=self.repo).strip()
        self.assertEqual(author, "Agent Bot|gate")
        self.assertEqual(run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo), "")
        code, out = self.gate("stats")
        self.assertIn("CI runs: 1", out)
        self.assertIn("merged    3 / 3", out)

    def test_conflict_rejected_then_fixed_and_resubmitted(self):
        self.make_repo()
        self.agent("left", {"calc.py": CALC.replace("return a + b", "return b + a")})
        self.agent("right", {"calc.py": CALC.replace("return a + b", "return (a + b)")})
        self.gate("queue", "--drain")
        self.assertEqual(self.session("left")["status"], "merged")
        right = self.session("right")
        self.assertEqual(right["status"], "rejected")
        self.assertEqual(right["reason"], "conflict")
        self.assertIn("calc.py", right["detail"])
        worktree = Path(right["worktree"])
        self.assertTrue(worktree.exists(), "rejected session keeps its worktree for a fix")
        run_git("reset", "-q", "--hard", "main", cwd=worktree)
        self.commit(worktree, self.feature(9), "right: rework")
        code, out = self.gate("submit", "right", "--model", "model-a")
        self.assertEqual(code, 0, out)
        self.gate("queue", "--drain")
        self.assertEqual(self.session("right")["status"], "merged")

    def test_failing_session_is_bisected_out_and_never_lands(self):
        self.make_repo()
        self.agent("good1", self.feature(1))
        self.agent("bad", {"calc.py": CALC.replace("return a - b", "return a + b")})
        self.agent("good2", self.feature(2))
        self.agent("good3", self.feature(3))
        self.gate("queue", "--drain")
        statuses = {sid: self.session(sid)["status"] for sid in ("good1", "bad", "good2", "good3")}
        self.assertEqual(statuses, {"good1": "merged", "bad": "rejected",
                                    "good2": "merged", "good3": "merged"})
        self.assertEqual(self.session("bad")["reason"], "tests")
        for commit in run_git("rev-list", "main", cwd=self.repo).split():
            calc = run_git("show", f"{commit}:calc.py", cwd=self.repo)
            self.assertIn("return a - b", calc, f"broken code reached main at {commit}")
        self.assertLessEqual(self.counter("ci_runs"), 5)

    def test_peer_conflict_is_deferred_not_rejected(self):
        self.make_repo()
        broken = CALC.replace("return a + b", "return a + b + 0").replace("return a - b", "return 0")
        self.agent("breaker", {"calc.py": broken})
        self.agent("innocent", {"calc.py": CALC.replace("return a + b", "return b + a")})
        self.gate("queue", "--drain")
        self.assertEqual(self.session("breaker")["status"], "rejected")
        self.assertEqual(self.session("breaker")["reason"], "tests")
        self.assertEqual(self.session("innocent")["status"], "merged")

    def test_high_risk_waits_for_named_approval(self):
        self.make_repo()
        code, out = self.agent("authz", {"auth/policy.py": "ALLOW_ALL = True\n"})
        self.assertIn("needs_approval (tier 2", out)
        self.gate("queue", "--drain")
        self.assertEqual(self.session("authz")["status"], "needs_approval")
        code, out = self.gate("approve", "authz", "--by", "alice")
        self.assertEqual(code, 0, out)
        self.gate("queue", "--drain")
        self.assertEqual(self.session("authz")["status"], "merged")
        self.assertIn("Approved-By: alice", self.main_log())
        self.assertIn("Risk-Tier: 2", self.main_log())

    def test_docs_and_tests_only_change_is_tier_zero(self):
        self.make_repo()
        code, out = self.agent("docs", {"docs/guide.md": "# Guide\n\nMore.\n"})
        self.assertIn("queued (tier 0", out)

    def test_policy_comes_from_target_not_agent_worktree(self):
        self.make_repo()
        worktree = self.spawn("sneaky")
        policy = (worktree / "gate.toml").read_text()
        policy = policy.replace("max_changed_lines = 400", "max_changed_lines = 100000")
        big = "".join(f"x{i} = {i}\n" for i in range(600))
        self.commit(worktree, {"gate.toml": policy, "lib/big.py": big}, "loosen policy")
        code, out = self.gate("submit", "sneaky", "--model", "model-a")
        self.assertEqual(code, 2, out)
        self.assertEqual(self.session("sneaky")["status"], "too_large")
        self.assertEqual(self.session("sneaky")["tier"], 2)

    def test_size_gate_and_exempt_files(self):
        self.make_repo()
        big = "".join(f"x{i} = {i}\n" for i in range(500))
        code, out = self.agent("huge", {"lib/huge.py": big})
        self.assertEqual(code, 2, out)
        self.assertIn("split the change", out)
        lock = "".join(f"dep{i}==1.0\n" for i in range(5000))
        code, out = self.agent("deps", {"deps.lock": lock, "lib/small.py": "y = 1\n"})
        self.assertEqual(code, 0, out)
        self.assertIn("queued", out)

    def test_merges_exactly_the_submitted_head(self):
        self.make_repo()
        worktree = self.spawn("pinned")
        self.commit(worktree, self.feature(1), "feature")
        self.gate("submit", "pinned", "--model", "model-a")
        self.commit(worktree, {"evil.txt": "not reviewed\n"}, "sneak in after submit")
        self.gate("queue", "--drain")
        self.assertEqual(self.session("pinned")["status"], "merged")
        files = run_git("ls-tree", "-r", "--name-only", "main", cwd=self.repo).split()
        self.assertIn("lib/feature_1.py", files)
        self.assertNotIn("evil.txt", files)

    def test_trace_redaction_abandon_archive_and_gc(self):
        self.make_repo(overrides={"attempt_days": "0", "reject_grace_hours": "0", "trace_days_unmerged": "0"})
        worktree = self.spawn("tried")
        self.commit(worktree, self.feature(4), "attempt")
        trace = self.tmp / "trace.jsonl"
        trace.write_text(
            '{"tool": "bash", "out": "export AWS=AKIAABCDEFGHIJKLMNOP"}\n'
            '{"cfg": {"password": "hunter2xyz"}}\n'
            '{"tok": "ghp_' + "a" * 36 + '"}\n'
        )
        code, out = self.gate("trace", "tried", str(trace))
        self.assertEqual(code, 0, out)
        stored = self.repo / ".git" / "gate" / "traces" / "tried.jsonl.gz"
        with gzip.open(stored, "rt") as handle:
            content = handle.read()
        for secret in ("AKIAABCDEFGHIJKLMNOP", "hunter2xyz", "ghp_aaaa"):
            self.assertNotIn(secret, content)
        self.assertEqual(self.session("tried")["trace"]["redactions"], 3)

        code, out = self.gate("abandon", "tried")
        self.assertEqual(code, 0, out)
        archive = self.repo / ".git" / "gate" / "attempts" / "tried.json.gz"
        with gzip.open(archive, "rt") as handle:
            self.assertIn("feature_4", json.load(handle)["patch"])
        self.assertEqual(run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo), "")

        code, out = self.gate("gc")
        self.assertEqual(code, 0, out)
        self.assertFalse(stored.exists())
        self.assertFalse(archive.exists())

    def test_sparse_spawn(self):
        self.make_repo()
        worktree = self.spawn("narrow", "--sparse", "lib")
        self.assertTrue((worktree / "calc.py").exists())
        self.assertTrue((worktree / "lib" / "__init__.py").exists())
        self.assertFalse((worktree / "auth").exists())
        self.assertTrue((self.repo / "auth" / "policy.py").exists(), "main checkout stays full")

    def test_refuses_when_target_checked_out(self):
        self.make_repo()
        run_git("switch", "-q", "main", cwd=self.repo)
        code, out = self.gate("queue")
        self.assertEqual(code, 1)
        self.assertIn("checked out", out)

    # ------------------------------------------------------------ concurrency / scale
    def test_remote_race_with_disjoint_push_commutes_without_retest(self):
        self.make_repo(remote=True)
        self.agent("a1", self.feature(1))
        outsider = self.tmp / "outsider"
        run_git("clone", "-q", str(self.origin), str(outsider), cwd=self.tmp)
        original = gate.Gate.run_tests
        calls = {"n": 0}

        def run_tests_with_race(gate_self, commit, changed=None):
            calls["n"] += 1
            if calls["n"] == 1:
                (outsider / "docs" / "human.md").write_text("human edit\n")
                run_git("add", ".", cwd=outsider)
                run_git("-c", "user.name=H", "-c", "user.email=h@x", "commit", "-qm", "human", cwd=outsider)
                run_git("push", "-q", "origin", "main", cwd=outsider)
            return original(gate_self, commit, changed)

        with mock.patch.object(gate.Gate, "run_tests", run_tests_with_race):
            code, out = self.gate("queue", "--drain")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.session("a1")["status"], "merged")
        remote_main = run_git("--git-dir", str(self.origin), "rev-parse", "main", cwd=self.tmp).strip()
        self.assertEqual(remote_main, self.session("a1")["merged_commit"])
        files = run_git("--git-dir", str(self.origin), "ls-tree", "-r", "--name-only", "main",
                        cwd=self.tmp).split()
        self.assertIn("docs/human.md", files, "external commit must be preserved")
        self.assertIn("lib/feature_1.py", files)
        self.assertEqual(calls["n"], 1, "disjoint move must not trigger a re-test")
        self.assertIn("Commuted-From:", self.main_log())

    def test_overlapping_move_forces_retest(self):
        self.make_repo()
        self.agent("a1", {"calc.py": CALC.replace("return a + b", "return b + a")})
        original = gate.Gate.run_tests
        calls = {"n": 0}

        def run_tests_with_race(gate_self, commit, changed=None):
            calls["n"] += 1
            if calls["n"] == 1:
                self.commit_on_main({"calc.py": CALC.replace("return a - b", "return -(b - a)")})
            return original(gate_self, commit, changed)

        with mock.patch.object(gate.Gate, "run_tests", run_tests_with_race):
            code, out = self.gate("queue", "--drain")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.session("a1")["status"], "merged")
        self.assertEqual(calls["n"], 2, "overlapping move must be re-tested")
        calc = self.main_file("calc.py")
        self.assertIn("return b + a", calc)
        self.assertIn("return -(b - a)", calc)

    def test_parallel_lanes_land_everything(self):
        self.make_repo(lanes={"a": ["lib/a/*"], "b": ["lib/b/*"]})
        for lane in ("a", "b"):
            for n in (1, 2, 3):
                code, out = self.agent(f"{lane}{n}", {f"lib/{lane}/m{n}.py": f"V = {n}\n"})
                self.assertIn(f"lane {lane}", out)
        code, out = self.gate("queue", "--drain", "--parallel", "2")
        self.assertEqual(code, 0, out)
        for lane in ("a", "b"):
            for n in (1, 2, 3):
                self.assertEqual(self.session(f"{lane}{n}")["status"], "merged", out)
        files = run_git("ls-tree", "-r", "--name-only", "main", cwd=self.repo).split()
        for lane in ("a", "b"):
            for n in (1, 2, 3):
                self.assertIn(f"lib/{lane}/m{n}.py", files)
        self.assertIn("Agent-Lane: a", self.main_log())
        self.assertEqual(run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo), "")

    def test_branch_only_session_from_remote_agent(self):
        self.make_repo()
        code, out = self.gate("spawn", "cloud1", "--no-worktree")
        self.assertEqual(code, 0, out)
        sandbox = self.tmp / "sandbox"
        run_git("clone", "-q", "--branch", "agent/cloud1", str(self.repo), str(sandbox), cwd=self.tmp)
        self.commit(sandbox, self.feature(5), "work done in a remote sandbox")
        run_git("push", "-q", "origin", "HEAD:agent/cloud1", cwd=sandbox)
        code, out = self.gate("submit", "cloud1", "--model", "model-a")
        self.assertEqual(code, 0, out)
        self.gate("queue", "--drain")
        self.assertEqual(self.session("cloud1")["status"], "merged")
        self.assertIn("feature_5", self.main_file("lib/feature_5.py"))

    # ------------------------------------------------------------ tests: selection, cache, verify
    def test_targeted_suites_and_tree_cache(self):
        marker = self.tmp / "marker.py"
        log = self.tmp / "suites.log"
        marker.write_text(f"import sys\nopen(r'{log}', 'a').write(sys.argv[1] + '\\n')\n")
        suites = (
            f"\n[[tests]]\nname = 'calc'\ncommand = '\"{PY}\" \"{marker}\" calc'\npaths = ['calc.py']\n"
            f"\n[[tests]]\nname = 'lib'\ncommand = '\"{PY}\" \"{marker}\" lib'\npaths = ['lib/*']\n"
        )
        self.make_repo(overrides={"test_command": "''"}, append=suites)
        self.agent("libonly", {"lib/x.py": "X = 1\n"})
        self.gate("queue", "--drain")
        self.assertEqual(self.session("libonly")["status"], "merged")
        self.assertEqual(log.read_text().split(), ["lib"], "only the lib suite should run")
        code, out = self.gate("verify")
        self.assertEqual(code, 0, out)
        self.assertEqual(sorted(log.read_text().split()), ["calc", "lib"],
                         "verify runs all suites; lib result on the same tree comes from cache")
        self.assertGreaterEqual(self.counter("suite_cache_hits"), 1)

    def test_verify_bisects_and_reverts_culprit(self):
        self.make_repo()
        code, out = self.gate("verify")
        self.assertEqual(code, 0, out)
        self.agent("good1", self.feature(1))
        self.gate("queue", "--drain")
        self.agent("evil", {"calc.py": CALC.replace("return a - b", "return a + b")})
        self.agent("good2", self.feature(2))
        with mock.patch.object(gate.Gate, "run_tests", lambda *a, **k: (True, "pretend")):
            self.gate("queue", "--drain")
        self.assertEqual(self.session("evil")["status"], "merged")
        self.assertNotIn("return a - b", self.main_file("calc.py"), "main is red before verify")

        code, out = self.gate("verify")
        self.assertEqual(code, 0, out)
        self.assertIn("culprit", out)
        self.assertIn("session evil", out)
        self.assertEqual(self.session("evil")["status"], "reverted")
        self.assertEqual(self.session("good2")["status"], "merged")
        self.assertIn("return a - b", self.main_file("calc.py"))
        self.assertIn("feature_2", self.main_file("lib/feature_2.py"))
        self.assertIn("Reverts:", run_git("log", "-1", "--format=%B", "main", cwd=self.repo))
        code, out = self.gate("verify")
        self.assertIn("green", out)

    # ------------------------------------------------------------ secrets
    def test_secret_in_diff_blocks_merge_without_echoing_it(self):
        self.make_repo()
        code, out = self.agent("leaky", {"lib/config.py": (
            'DB_PASSWORD = "hunter2hunter2"\n'
            'AWS_KEY = "AKIAABCDEFGHIJKLMNOP"\n'
        )})
        self.assertEqual(code, 2, out)
        session = self.session("leaky")
        self.assertEqual((session["status"], session["reason"]), ("rejected", "secret"))
        self.assertIn("lib/config.py: aws-key", session["detail"])
        self.assertIn("lib/config.py: hardcoded-credential", session["detail"])
        self.assertNotIn("hunter2hunter2", out + session["detail"])
        code, out = self.agent("placeholder", {"lib/settings.py": 'PASSWORD = "<your-password-here>"\n'})
        self.assertEqual(code, 0, out)

    def test_trace_redaction_extended_and_scanner_fails_closed(self):
        scanner = self.tmp / "scanner.py"
        scanner.write_text("import sys\nsys.exit(1 if 'LEAK-' in open(sys.argv[1]).read() else 0)\n")
        self.make_repo(overrides={"secret_scan_command": f"'\"{PY}\" \"{scanner}\" {{file}}'"})
        self.spawn("t1")
        secrets = {
            "jwt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV",
            "url": "postgres://admin:s3cretpw@db.internal:5432/app",
            "stripe": "sk_live_" + "4eC39HqLyjWDarjtT1zdp7dc",
            "entropy": "Zx9Qm2Lp8Vt4Rk7Wn3Hs6Yb1Jd5Fg0Ac",
        }
        trace = self.tmp / "t1.jsonl"
        trace.write_text("\n".join(json.dumps({"k": key, "v": value}) for key, value in secrets.items()))
        code, out = self.gate("trace", "t1", str(trace))
        self.assertEqual(code, 0, out)
        with gzip.open(self.repo / ".git" / "gate" / "traces" / "t1.jsonl.gz", "rt") as handle:
            stored = handle.read()
        for value in ("SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV", "s3cretpw", "4eC39HqLyjWDarjtT1zdp7dc",
                      "Zx9Qm2Lp8Vt4Rk7Wn3Hs6Yb1Jd5Fg0Ac"):
            self.assertNotIn(value, stored)
        self.assertIn("postgres://admin:[REDACTED]@", stored)

        unknown = self.tmp / "t2.jsonl"
        unknown.write_text('{"note": "internal LEAK-77 format the patterns do not know"}\n')
        self.spawn("t2")
        code, out = self.gate("trace", "t2", str(unknown))
        self.assertEqual(code, 1, out)
        self.assertIn("NOT stored", out)
        self.assertFalse((self.repo / ".git" / "gate" / "traces" / "t2.jsonl.gz").exists())

    def test_redaction_leaves_git_hashes_and_prose_alone(self):
        text = "commit 3f9a2c7e5b1d4f6a8c0e2b4d6f8a0c2e4b6d8f0a merged; see docs/guide.md"
        self.assertEqual(gate.redact(text), (text, 0))

    # ------------------------------------------------------------ operability
    def test_doctor_checks_remote_write_access(self):
        self.make_repo(remote=True)
        code, out = self.gate("doctor", "--write-test")
        self.assertEqual(code, 0, out)
        self.assertIn("push access ok", out)
        refs = run_git("--git-dir", str(self.origin), "for-each-ref", "refs/heads/", cwd=self.tmp)
        self.assertNotIn("gate-doctor", refs)

    # ------------------------------------------------------------ crash safety
    def test_crash_after_publish_is_recovered_without_duplicates(self):
        """A process killed between publishing and recording the merge must not land work twice."""
        self.make_repo()
        for n in (1, 2):
            self.assertEqual(self.agent(f"c{n}", self.feature(n))[0], 0)
        real_update = gate.Gate.update

        def crash_on_merge(instance, sid, expected_head=None, **changes):
            if changes.get("status") == "merged":
                raise KeyboardInterrupt("simulated kill after publish")
            return real_update(instance, sid, expected_head, **changes)

        with mock.patch.object(gate.Gate, "update", crash_on_merge), self.assertRaises(KeyboardInterrupt):
            self.gate("queue", "--drain")
        self.assertEqual(self.session("c1")["status"], "queued")  # published, but not recorded
        published = run_git("log", "--format=%H", "main", cwd=self.repo).split()

        code, out = self.gate("--json", "queue", "--drain")
        self.assertEqual(code, 0, out)
        doc = json.loads(out)
        self.assertEqual(sorted(item["session"] for item in doc["result"]["recovered"]), ["c1", "c2"])
        self.assertEqual(doc["result"]["merged"], [])
        self.assertEqual(run_git("log", "--format=%H", "main", cwd=self.repo).split(), published)
        log = self.main_log()
        for n in (1, 2):
            self.assertEqual(log.count(f"Agent-Session: c{n}\n"), 1)
            session = self.session(f"c{n}")
            self.assertEqual(session["status"], "merged")
            self.assertTrue(session["recovered"])
        self.assertEqual(run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo), "")

    def test_gc_finishes_cleanup_interrupted_after_merge(self):
        self.make_repo()
        self.assertEqual(self.agent("g1", self.feature(1))[0], 0)
        with mock.patch.object(gate.Gate, "cleanup"):
            self.assertEqual(self.gate("queue")[0], 0)
        self.assertEqual(self.session("g1")["status"], "merged")
        self.assertIn("agent/g1", run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo))
        code, out = self.gate("--json", "gc")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["result"]["branches_cleaned"], 1)
        self.assertEqual(run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo), "")

    def test_lock_left_by_dead_process_is_taken_over(self):
        self.make_repo()
        state = self.repo / ".git" / "gate"
        state.mkdir(parents=True, exist_ok=True)
        dead_pid = 2 ** 22 + 12345  # far above any live pid on CI runners
        (state / "lane-default.lock").write_text(f"{dead_pid} {gate.socket.gethostname()}")
        self.assertEqual(self.agent("d1", self.feature(1))[0], 0)
        code, out = self.gate("queue", "--wait", "0.2")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.session("d1")["status"], "merged")
        self.assertFalse((state / "lane-default.lock").exists())

    def test_stale_lock_takeover_is_exclusive_under_contention(self):
        """Many contenders breaking the same dead lock must still enter one at a time."""
        self.make_repo()
        instance = gate.Gate(self.repo)
        dead = f"{2 ** 22 + 12345} {gate.socket.gethostname()}"
        intervals: list[tuple[float, float]] = []
        record = threading.Lock()

        def contender() -> None:
            with instance.lock("contended", timeout_s=30):
                entered = time.perf_counter()
                time.sleep(0.02)
                left = time.perf_counter()
            with record:
                intervals.append((entered, left))

        for _ in range(3):
            (instance.state / "contended.lock").write_text(dead)
            threads = [threading.Thread(target=contender) for _ in range(8)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()
        intervals.sort()
        self.assertEqual(len(intervals), 24)
        for (_, left), (entered, _) in zip(intervals, intervals[1:], strict=False):
            self.assertLessEqual(left, entered)

    def test_lock_held_by_live_or_remote_owner_blocks(self):
        self.make_repo()
        state = self.repo / ".git" / "gate"
        state.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.agent("l1", self.feature(1))[0], 0)
        lock = state / "lane-default.lock"
        for owner in (f"{os.getpid()} {gate.socket.gethostname()}", "4242 some-other-host"):
            lock.write_text(owner)
            code, out = self.gate("--json", "queue", "--wait", "0.2")
            self.assertEqual(code, gate.EXIT_ERROR, out)
            self.assertIn("lock is held", json.loads(out)["error"])
            self.assertEqual(self.session("l1")["status"], "queued")
        lock.unlink()

    def test_pid_alive(self):
        self.assertTrue(gate.pid_alive(os.getpid()))
        self.assertFalse(gate.pid_alive(0))
        self.assertFalse(gate.pid_alive(2 ** 22 + 12345))

    # ------------------------------------------------------------ automation interface
    def test_json_output_and_idempotent_submit(self):
        self.make_repo()
        code, out = self.gate("--json", "spawn", "j1")
        self.assertEqual(code, 0, out)
        spawned = json.loads(out)["result"]
        self.assertEqual(spawned["branch"], "agent/j1")
        self.commit(Path(spawned["worktree"]), self.feature(1), "j1: change")

        code, out = self.gate("--json", "submit", "j1", "--model", "m")
        doc = json.loads(out)
        self.assertEqual((code, doc["exit_code"], doc["result"]["status"]), (0, 0, "queued"))
        code, out = self.gate("--json", "submit", "j1", "--model", "m")  # agent retry after a timeout
        self.assertEqual(code, 0, out)
        self.assertTrue(json.loads(out)["result"]["unchanged"])

        code, out = self.gate("--json", "status")
        self.assertEqual([s["id"] for s in json.loads(out)["result"]["sessions"]], ["j1"])
        code, out = self.gate("--json", "queue")
        doc = json.loads(out)
        self.assertEqual([m["session"] for m in doc["result"]["merged"]], ["j1"])
        self.assertEqual(doc["events"][0]["event"], "merged")
        code, out = self.gate("--json", "stats")
        self.assertEqual(json.loads(out)["result"]["by_status"], {"merged": 1})
        code, out = self.gate("--json", "show", "j1")
        self.assertEqual(json.loads(out)["result"]["session"]["status"], "merged")
        code, out = self.gate("--json", "verify")
        self.assertEqual(json.loads(out)["result"]["status"], "green")
        code, out = self.gate("--json", "doctor")
        self.assertEqual(json.loads(out)["result"]["failures"], 0)

    def test_json_errors_and_policy_exit_code(self):
        self.make_repo()
        code, out = self.gate("--json", "show", "nope")
        self.assertEqual(code, gate.EXIT_ERROR)
        self.assertIn("unknown session", json.loads(out)["error"])
        big = {"lib/big.py": "".join(f"x{i} = {i}\n" for i in range(500))}
        worktree = self.spawn("big")
        self.commit(worktree, big, "big change")
        code, out = self.gate("--json", "submit", "big", "--model", "m")
        self.assertEqual(code, gate.EXIT_POLICY)
        self.assertEqual(json.loads(out)["result"]["status"], "too_large")

    def test_version_and_help(self):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            gate.main(["--version"])
        self.assertIn(gate.__version__, out.getvalue())
        out = io.StringIO()
        with contextlib.redirect_stdout(out), self.assertRaises(SystemExit):
            gate.main(["--help"])
        self.assertIn("exit codes:", out.getvalue())
        self.assertIn("--json", out.getvalue())

    def test_init_is_idempotent(self):
        self.make_repo()
        (self.repo / "gate.toml").unlink()
        code, out = self.gate("--json", "init")
        self.assertTrue(json.loads(out)["result"]["created"])
        code, out = self.gate("--json", "init")
        self.assertFalse(json.loads(out)["result"]["created"])


if __name__ == "__main__":
    unittest.main()
