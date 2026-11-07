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

    def heartbeat(self, lock: Path, owner: str, skew_s: float = 0.0) -> threading.Event:
        """Play a live lock holder whose clock is off by skew_s: rewrite the lock's mtime every 50 ms."""
        lock.write_text(owner)
        stop = threading.Event()

        def beat() -> None:
            while not stop.wait(0.05):
                stamp = time.time() + skew_s
                with contextlib.suppress(OSError):
                    os.utime(lock, (stamp, stamp))

        threading.Thread(target=beat, daemon=True).start()
        self.addCleanup(stop.set)
        return stop

    def test_lock_held_by_live_or_remote_owner_blocks(self):
        """A heartbeating holder keeps its lock whatever its clock says: liveness is judged by change."""
        self.make_repo()
        state = self.repo / ".git" / "gate"
        state.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.agent("l1", self.feature(1))[0], 0)
        lock = state / "lane-default.lock"
        holders = {
            "live process on this host": (f"{os.getpid()} {gate.socket.gethostname()}", 0.0),
            "other host, clock 5 min behind": ("4242 some-other-host", -300.0),
            "other host, clock 5 min ahead": ("4242 some-other-host", 300.0),
        }
        for label, (owner, skew) in holders.items():
            with self.subTest(label):
                stop = self.heartbeat(lock, owner, skew)
                code, out = self.gate("--json", "queue", "--wait", "0.3")
                stop.set()
                self.assertEqual(code, gate.EXIT_ERROR, out)
                self.assertIn("lock is held", json.loads(out)["error"])
                self.assertEqual(self.session("l1")["status"], "queued")
                self.assertEqual(lock.read_text(), owner)
        lock.unlink()

    def test_silent_foreign_lock_is_taken_over_after_observation(self):
        """A lock from another host that stops changing is taken over after LOCK_STALE_S of watching,
        even if its timestamp is in the future (skewed clock) and even with a shorter --wait."""
        self.make_repo()
        state = self.repo / ".git" / "gate"
        state.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.agent("s1", self.feature(1))[0], 0)
        lock = state / "lane-default.lock"
        lock.write_text("4242 crashed-host")
        future = time.time() + 300
        os.utime(lock, (future, future))
        started = time.monotonic()
        with mock.patch.object(gate, "LOCK_STALE_S", 0.5):
            code, out = self.gate("queue", "--wait", "0.1")
        elapsed = time.monotonic() - started
        self.assertEqual(code, 0, out)
        self.assertEqual(self.session("s1")["status"], "merged")
        self.assertGreaterEqual(elapsed, 0.5, "must watch the lock for LOCK_STALE_S before breaking it")

    def test_lock_abandoned_long_ago_is_taken_over_at_once(self):
        self.make_repo()
        state = self.repo / ".git" / "gate"
        state.mkdir(parents=True, exist_ok=True)
        self.assertEqual(self.agent("a1", self.feature(1))[0], 0)
        lock = state / "lane-default.lock"
        lock.write_text("4242 decommissioned-host")
        old = time.time() - 11 * gate.LOCK_STALE_S
        os.utime(lock, (old, old))
        started = time.monotonic()
        code, out = self.gate("queue", "--wait", "0")
        self.assertEqual(code, 0, out)
        self.assertLess(time.monotonic() - started, gate.LOCK_STALE_S / 2)
        self.assertEqual(self.session("a1")["status"], "merged")

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

    # ------------------------------------------------------------ lock liveness (heartbeat)
    def test_heartbeat_keeps_a_long_running_holder_safe(self):
        self.make_repo()
        instance = gate.Gate(self.repo)
        acquired = threading.Event()

        def hold() -> None:
            with instance.lock("long"):
                acquired.set()
                time.sleep(1.2)

        with mock.patch.object(gate, "LOCK_HEARTBEAT_S", 0.05), mock.patch.object(gate, "LOCK_STALE_S", 0.4):
            holder = threading.Thread(target=hold)
            holder.start()
            acquired.wait(5)
            with self.assertRaises(gate.GateError):
                with instance.lock("long", timeout_s=0.8):
                    pass  # pragma: no cover - must not be reached while the holder is alive
            holder.join()
            with instance.lock("long", timeout_s=1):
                pass
        self.assertFalse((instance.state / "long.lock").exists())

    def test_silent_lock_is_taken_over_even_if_its_pid_was_reused(self):
        self.make_repo()
        instance = gate.Gate(self.repo)
        lock = instance.state / "reused.lock"
        lock.write_text(f"{os.getpid()} {gate.socket.gethostname()} old-token")  # live pid, wrong process
        old = time.time() - 10 * gate.LOCK_STALE_S
        os.utime(lock, (old, old))
        with instance.lock("reused", timeout_s=2):
            self.assertNotIn("old-token", lock.read_text())

    def test_release_never_deletes_a_lock_that_changed_hands(self):
        self.make_repo()
        instance = gate.Gate(self.repo)
        lock = instance.state / "handover.lock"
        with instance.lock("handover"):
            lock.write_text("4242 other-host new-owner")  # e.g. broken while we were frozen
        self.assertEqual(lock.read_text(), "4242 other-host new-owner")

    # ------------------------------------------------------------ guided init
    def test_init_detects_the_test_command(self):
        cases = {
            "pytest": ({"pyproject.toml": "[tool.pytest.ini_options]\n"}, "python -m pytest -q"),
            "npm": ({"package.json": json.dumps({"scripts": {"test": "jest"}})}, "npm test"),
            "go": ({"go.mod": "module x\n"}, "go test ./..."),
            "cargo": ({"Cargo.toml": "[package]\n"}, "cargo test"),
            "unittest": ({"tests/test_x.py": ""}, "python -m unittest discover -s tests"),
            "root-tests": ({"test_x.py": ""}, "python -m unittest"),
            "none": ({"README.md": "hi"}, ""),
        }
        for label, (files, expected) in cases.items():
            with self.subTest(label):
                root = self.tmp / label
                root.mkdir()
                for name, content in files.items():
                    (root / name).parent.mkdir(parents=True, exist_ok=True)
                    (root / name).write_text(content)
                self.assertEqual(gate.detect_test_command(root)[0], expected)
        self.make_repo()
        (self.repo / "gate.toml").unlink()
        code, out = self.gate("--json", "init")
        result = json.loads(out)["result"]
        self.assertEqual(result["test_command"], "python -m unittest discover -s tests")
        config = gate.tomllib.loads((self.repo / "gate.toml").read_text())
        self.assertEqual(config["test_command"], "python -m unittest discover -s tests")

    def test_errors_tell_the_user_what_to_do(self):
        self.make_repo()
        code, out = self.gate("show", "ghost")
        self.assertIn("gate status --all", out)
        worktree = self.spawn("empty")
        code, out = self.gate("submit", "empty", "--model", "m")
        self.assertEqual(code, gate.EXIT_ERROR)
        self.assertIn(f"commit the change in {worktree}", out)
        (worktree / "dirty.txt").write_text("x")
        run_git("add", "dirty.txt", cwd=worktree)
        code, out = self.gate("submit", "empty", "--model", "m")
        self.assertIn("uncommitted changes", out)

    # ------------------------------------------------------------ reference agent
    def test_reference_agent_example_merges_through_the_json_interface(self):
        self.make_repo()
        example = Path(__file__).resolve().parent / "examples" / "agent_loop.py"
        proc = subprocess.run(
            [PY, str(example), "--repo", str(self.repo), "--session", "ex1", "--model", "example-model",
             "--file", "docs/example.md", "# written by the example agent\n", "--drive-queue",
             "--timeout", "120"],
            capture_output=True, text=True, timeout=300,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("final: merged", proc.stdout)
        self.assertIn("Agent-Model: example-model", self.main_log())
        self.assertEqual(self.main_file("docs/example.md"), "# written by the example agent\n")

    # ------------------------------------------------------------ reporting
    def test_reports_show_history_and_reasons(self):
        self.make_repo()
        self.assertEqual(self.agent("good", self.feature(1))[0], 0)
        self.agent("bad", {"calc.py": CALC.replace("return a + b", "return a - b")}, model="model-b")
        self.assertEqual(self.gate("queue", "--drain")[0], 0)
        code, out = self.gate("stats")
        self.assertIn("not-merged reasons:  tests=1", out)
        self.assertIn("model model-b", out)
        self.assertIn("submit->merge median", out)
        code, out = self.gate("status", "--all")
        self.assertIn("good", out)
        self.assertIn("bad", out)
        code, out = self.gate("show", "good", "--patch")
        self.assertIn("+def feature_1():", out)
        code, out = self.gate("show", "bad")
        self.assertIn("--- detail ---", out)

    def test_policy_pushed_to_the_remote_applies_to_the_next_submit(self):
        self.make_repo(remote=True)
        human = self.tmp / "human"
        run_git("clone", "-q", str(self.origin), str(human), cwd=self.tmp)
        config = (human / "gate.toml").read_text().replace("max_changed_lines = 400", "max_changed_lines = 5")
        (human / "gate.toml").write_text(config)
        run_git("-c", "user.name=H", "-c", "user.email=h@x", "commit", "-qam", "tighten size gate", cwd=human)
        run_git("push", "-q", "origin", "HEAD:main", cwd=human)
        worktree = self.spawn("big2")
        self.commit(worktree, {"lib/b.py": "".join(f"v{i} = {i}\n" for i in range(20))}, "20 lines")
        code, out = self.gate("--json", "submit", "big2", "--model", "m")
        self.assertEqual(code, gate.EXIT_POLICY, out)
        self.assertEqual(json.loads(out)["result"]["status"], "too_large")


    # ------------------------------------------------------------ publish failures
    def test_push_rejected_by_the_remote_is_reported_once_not_retried(self):
        """A protected branch (here: a pre-receive hook) is not contention: report it at once, keep the
        session queued, run the tests once, and leave the remote untouched."""
        self.make_repo(remote=True)
        hook = self.origin / "hooks" / "pre-receive"
        hook.write_bytes(b"#!/bin/sh\necho 'GH006: Protected branch update failed for refs/heads/main.' >&2\nexit 1\n")
        hook.chmod(0o755)
        before = run_git("--git-dir", str(self.origin), "rev-parse", "main", cwd=self.tmp).strip()
        self.assertEqual(self.agent("p1", self.feature(1))[0], 0)
        original = gate.Gate.run_tests
        calls = {"n": 0}

        def counting_run_tests(gate_self, commit, changed=None):
            calls["n"] += 1
            return original(gate_self, commit, changed)

        with mock.patch.object(gate.Gate, "run_tests", counting_run_tests):
            code, out = self.gate("--json", "queue", "--drain")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        error = json.loads(out)["error"]
        self.assertIn("rejected the push", error)
        self.assertIn("Protected branch", error)
        self.assertNotIn("kept moving", error)
        self.assertEqual(calls["n"], 1, "a refusal that a retry cannot fix must not re-test the batch")
        self.assertEqual(self.session("p1")["status"], "queued")
        self.assertEqual(run_git("--git-dir", str(self.origin), "rev-parse", "main", cwd=self.tmp).strip(), before)

    def test_target_checked_out_elsewhere_is_never_moved(self):
        self.make_repo(remote=True)
        checkout = self.tmp / "human-checkout"
        run_git("worktree", "add", "-q", str(checkout), "main", cwd=self.repo)
        instance = gate.Gate(self.repo)
        tip = instance.rev("refs/heads/main")
        with self.assertRaises(gate.GateError) as caught:
            instance.advance(tip, tip)
        self.assertIn("checked out", str(caught.exception))
        outsider = self.tmp / "outsider"
        run_git("clone", "-q", str(self.origin), str(outsider), cwd=self.tmp)
        (outsider / "docs" / "new.md").write_text("new\n")
        run_git("add", ".", cwd=outsider)
        run_git("-c", "user.name=H", "-c", "user.email=h@x", "commit", "-qm", "remote move", cwd=outsider)
        run_git("push", "-q", "origin", "main", cwd=outsider)
        with self.assertRaises(gate.GateError):
            instance.sync_tip()
        self.assertEqual(instance.rev("refs/heads/main"), tip, "checked-out branch must not move")

    def test_merge_tree_failure_is_reported(self):
        self.make_repo()
        self.assertEqual(self.agent("m1", self.feature(1))[0], 0)
        instance = gate.Gate(self.repo)
        instance.update("m1", head="0" * 40)  # e.g. objects lost from a corrupted repository
        code, out = self.gate("queue")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        self.assertIn("merge-tree failed", out)

    # ------------------------------------------------------------ retention and durability
    def test_gc_never_abandons_a_session_resubmitted_meanwhile(self):
        self.make_repo(overrides={"reject_grace_hours": "0"})
        self.assertEqual(self.agent("r1", {"calc.py": CALC.replace("return a - b", "return a + b")})[0], 0)
        self.gate("queue")
        self.assertEqual(self.session("r1")["status"], "rejected")
        real_archive = gate.Gate.archive

        def archive_then_agent_resubmits(gate_self, session):
            path = real_archive(gate_self, session)
            gate_self.update(session["id"], status="queued", head="f" * 40)  # fixed and resubmitted
            return path

        with mock.patch.object(gate.Gate, "archive", archive_then_agent_resubmits):
            code, out = self.gate("--json", "gc")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.session("r1")["status"], "queued")
        self.assertIn("agent/r1", run_git("for-each-ref", "refs/heads/agent/", cwd=self.repo))
        code, out = self.gate("--json", "gc")  # unchanged rejected sessions are still collected
        self.assertEqual(code, 0, out)

    def test_atomic_write_never_leaves_a_partial_file(self):
        target = self.tmp / "state.json"
        target.write_text("old")
        with mock.patch.object(gate.os, "replace", side_effect=OSError("disk full")), \
                self.assertRaises(OSError):
            gate.write_atomic(target, b"new")
        self.assertEqual(target.read_text(), "old")
        self.assertEqual(sorted(p.name for p in self.tmp.iterdir()), ["state.json"])
        gate.write_atomic(target, b"new")
        self.assertEqual(target.read_text(), "new")

    def test_scanner_timeout_fails_closed(self):
        scanner = self.tmp / "slow_scanner.py"
        scanner.write_text("import time\ntime.sleep(30)\n")
        self.make_repo(overrides={"secret_scan_command": f"'\"{PY}\" \"{scanner}\" {{file}}'"})
        with mock.patch.object(gate, "SCAN_TIMEOUT_S", 0.5):
            finding = gate.Gate(self.repo).external_scan("hello\n")
        self.assertIsNotNone(finding)
        self.assertIn("timed out", finding)

    def test_verify_without_green_baseline_needs_a_human(self):
        self.make_repo()
        self.commit_on_main({"calc.py": CALC.replace("return a - b", "return a + b")}, "break main")
        code, out = self.gate("--json", "verify")
        self.assertEqual(code, gate.EXIT_NEEDS_HUMAN, out)
        doc = json.loads(out)
        self.assertEqual(doc["result"]["status"], "red")
        self.assertIsNone(doc["result"]["last_green"])

    # ------------------------------------------------------------ real process kills
    def run_killed(self, fault: str, *argv: str) -> subprocess.CompletedProcess:
        """Run gate in a child process that kills itself (SIGKILL / TerminateProcess) at a fault point."""
        env = {**os.environ, "GATE_FAULT": fault}
        proc = subprocess.run([PY, gate.__file__, "-C", str(self.repo), *argv], env=env,
                              capture_output=True, text=True, timeout=300)
        self.assertNotEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return proc

    def test_real_kill_after_publish_recovers_without_duplicates(self):
        self.make_repo()
        for n in (1, 2):
            self.assertEqual(self.agent(f"k{n}", self.feature(n))[0], 0)
        self.run_killed("after-publish", "queue", "--drain")
        self.assertEqual(self.session("k1")["status"], "queued")  # published, never recorded
        self.assertTrue((self.repo / ".git" / "gate" / "lane-default.lock").exists(), "lock left behind")
        published = run_git("log", "--format=%H", "main", cwd=self.repo).split()
        self.assertIn("Agent-Session: k1", self.main_log())

        code, out = self.gate("--json", "queue", "--drain", "--wait", "1")
        self.assertEqual(code, 0, out)
        doc = json.loads(out)
        self.assertEqual(sorted(item["session"] for item in doc["result"]["recovered"]), ["k1", "k2"])
        self.assertEqual(run_git("log", "--format=%H", "main", cwd=self.repo).split(), published)
        for n in (1, 2):
            self.assertEqual(self.main_log().count(f"Agent-Session: k{n}\n"), 1)
            self.assertTrue(self.session(f"k{n}")["recovered"])

    def test_real_kill_after_revert_publish_is_recorded_by_the_next_verify(self):
        self.make_repo()
        self.assertEqual(self.gate("verify")[0], 0)
        self.agent("evil", {"calc.py": CALC.replace("return a - b", "return a + b")})
        with mock.patch.object(gate.Gate, "run_tests", lambda *a, **k: (True, "pretend")):
            self.gate("queue", "--drain")
        self.assertEqual(self.session("evil")["status"], "merged")
        self.run_killed("after-revert-publish", "verify")
        self.assertIn("Reverts:", run_git("log", "-1", "--format=%B", "main", cwd=self.repo))
        self.assertEqual(self.session("evil")["status"], "merged")  # revert published, never recorded

        code, out = self.gate("--json", "verify", "--wait", "1")
        self.assertEqual(code, 0, out)
        doc = json.loads(out)
        self.assertEqual(doc["result"]["recovered"], ["evil"])
        self.assertEqual(doc["result"]["status"], "green")
        self.assertEqual(self.session("evil")["status"], "reverted")
        self.assertEqual(run_git("log", "--format=%B", "main", cwd=self.repo).count("Reverts:"), 1)


    # ------------------------------------------------------------ round-3 council findings
    def test_broken_config_is_reported_not_a_traceback(self):
        self.make_repo(append="\n[[tests]]\nname = 'api'\npaths = ['api/*']\n")  # no command
        code, out = self.gate("status")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        self.assertIn("gate.toml", out)
        self.assertIn("command", out)
        self.assertNotIn("Traceback", out)

        self.commit_on_main({"gate.toml": "max_batch = = 4\n"}, "typo in policy")
        code, out = self.gate("--json", "status")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        error = json.loads(out)["error"]
        self.assertIn("gate.toml", error)
        self.assertIn("line 1", error)

        self.commit_on_main({"gate.toml": "max_batch = 'four'\n"}, "wrong type in policy")
        code, out = self.gate("status")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        self.assertIn("max_batch", out)

    def test_policy_typos_and_duplicate_suite_names_are_rejected(self):
        # A misspelled key would silently leave its default in force (e.g. no test command at all).
        self.make_repo(append="test_comand = 'pytest'\n")
        code, out = self.gate("status")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        self.assertIn("test_comand", out)

        # Two suites with one name would share a test-cache key, so one could be skipped on the other's pass.
        duplicate = ("\n[[tests]]\nname = 'api'\ncommand = 'a'\n"
                     "\n[[tests]]\nname = 'api'\ncommand = 'b'\n")
        fixed = self.main_file("gate.toml").replace("test_comand = 'pytest'\n", "")
        self.commit_on_main({"gate.toml": fixed + duplicate}, "two suites named api")
        code, out = self.gate("status")
        self.assertEqual(code, gate.EXIT_ERROR, out)
        self.assertIn("api", out)
        self.assertIn("more than once", out)

    def test_provenance_fields_cannot_forge_trailers(self):
        self.make_repo()
        victim = self.spawn("victim")
        self.commit(victim, self.feature(1), "victim: change")
        self.assertEqual(self.gate("submit", "victim", "--model", "m")[0], 0)
        victim_head = self.session("victim")["head"]

        worktree = self.spawn("forger")
        self.commit(worktree, self.feature(2), "forger: change")
        for flag in ("--model", "--tool", "--spec", "--title"):
            code, out = self.gate("submit", "forger", "--model", "m", flag, f"x\nAgent-Head: {victim_head}")
            self.assertEqual(code, gate.EXIT_ERROR, f"{flag}: {out}")
            self.assertIn("single line", out)
        self.assertEqual(self.session("forger")["status"], "active")

        # A session stored before this check (or edited by hand) is still flattened at commit time.
        squashed = gate.Gate(self.repo).squash_commit(
            {**self.session("victim"), "id": "victim", "tier": 0, "title": "t",
             "model": f"m\nAgent-Head: {'f' * 40}"},
            run_git("rev-parse", "main^{tree}", cwd=self.repo).strip(),
            run_git("rev-parse", "main", cwd=self.repo).strip())
        heads = run_git("log", "-1", "--format=%(trailers:key=Agent-Head,valueonly)", squashed,
                        cwd=self.repo).split()
        self.assertEqual(heads, [victim_head])

    def test_real_kill_while_testing_a_batch_relands_once(self):
        self.make_repo()
        for n in (1, 2):
            self.assertEqual(self.agent(f"t{n}", self.feature(n))[0], 0)
        before = run_git("rev-parse", "main", cwd=self.repo).strip()
        self.run_killed("after-tests", "queue", "--drain")
        self.assertEqual(run_git("rev-parse", "main", cwd=self.repo).strip(), before, "nothing published")
        self.assertEqual(self.session("t1")["status"], "queued")
        self.assertTrue((self.repo / ".git" / "gate" / "lane-default.lock").exists(), "lock left behind")

        code, out = self.gate("--json", "queue", "--drain", "--wait", "1")
        self.assertEqual(code, 0, out)
        for n in (1, 2):
            self.assertEqual(self.session(f"t{n}")["status"], "merged")
            self.assertEqual(self.main_log().count(f"Agent-Session: t{n}\n"), 1)

    def test_verify_never_auto_reverts_a_merge_commit(self):
        self.make_repo()
        self.assertEqual(self.gate("verify")[0], 0)
        main = run_git("rev-parse", "main", cwd=self.repo).strip()
        broken = self.commit_on_main({"calc.py": CALC.replace("return a - b", "return a + b")}, "side work")
        run_git("update-ref", "refs/heads/main", main, cwd=self.repo)
        side = self.commit_on_main({"docs/other.md": "x\n"}, "mainline work")
        tree = run_git("merge-tree", "--write-tree", side, broken, cwd=self.repo).split()[0]
        env = {**os.environ, "GIT_AUTHOR_NAME": "Human", "GIT_AUTHOR_EMAIL": "h@x",
               "GIT_COMMITTER_NAME": "Human", "GIT_COMMITTER_EMAIL": "h@x"}
        merge = run_git("commit-tree", tree, "-p", side, "-p", broken, "-m",
                        "Merge side\n\nAgent-Session: forged\n", cwd=self.repo, env=env).strip()
        run_git("update-ref", "refs/heads/main", merge, cwd=self.repo)

        code, out = self.gate("--json", "verify")
        self.assertEqual(code, gate.EXIT_NEEDS_HUMAN, out)
        doc = json.loads(out)
        self.assertEqual(doc["result"]["culprit"], merge)
        self.assertIn("merge commit", doc["result"]["reason"])
        self.assertEqual(run_git("rev-parse", "main", cwd=self.repo).strip(), merge, "main untouched")

    # ------------------------------------------------------------ ergonomics
    def test_json_flag_works_after_the_subcommand(self):
        self.make_repo()
        code, out = self.gate("status", "--json")
        self.assertEqual(code, 0, out)
        self.assertEqual(json.loads(out)["command"], "status")
        code, out = self.gate("--json", "status")
        self.assertEqual(json.loads(out)["command"], "status")

    def test_completion_scripts_cover_every_command(self):
        for shell in ("bash", "zsh", "fish"):
            with self.subTest(shell):
                out = io.StringIO()
                with contextlib.redirect_stdout(out):
                    self.assertEqual(gate.main(["completion", shell]), 0)
                script = out.getvalue()
                for command in gate.COMMANDS:
                    self.assertIn(command, script)
                self.assertIn("drain", script)
                if shell == "bash" and sys.platform != "win32" and shutil.which("bash"):
                    checked = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True)
                    self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_doctor_fix_detaches_a_clean_main_checkout(self):
        self.make_repo()
        run_git("switch", "-q", "main", cwd=self.repo)
        code, out = self.gate("doctor")
        self.assertNotEqual(code, 0, out)
        self.assertIn("doctor --fix", out)
        code, out = self.gate("doctor", "--fix")
        self.assertEqual(code, 0, out)
        self.assertIn("detached", out)
        detached = subprocess.run(["git", "symbolic-ref", "-q", "HEAD"], cwd=self.repo, capture_output=True)
        self.assertNotEqual(detached.returncode, 0)

    def test_tracked_text_files_are_clean_utf8(self):
        """No byte-order marks and no mojibake (UTF-8 decoded as cp1252 and re-encoded)."""
        here = Path(__file__).resolve().parent
        listing = subprocess.run(["git", "ls-files"], cwd=here, capture_output=True, text=True)
        if listing.returncode != 0:
            self.skipTest("not a git checkout")
        markers = ["\u00e2\u20ac", "\u00c3", "\u00e2\u2030", "\u00c2\u00a0", "\ufffd"]
        suffixes = {".md", ".py", ".toml", ".yml", ".yaml", ".html", ".txt", ".cfg"}
        for name in listing.stdout.split():
            path = here / name
            if path.suffix not in suffixes or not path.exists():
                continue
            data = path.read_bytes()
            with self.subTest(name):
                self.assertFalse(data.startswith(b"\xef\xbb\xbf"), "UTF-8 byte-order mark")
                text = data.decode("utf-8")
                for marker in markers:
                    self.assertNotIn(marker, text, f"mojibake {marker!r}")


if __name__ == "__main__":
    unittest.main()
