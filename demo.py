"""Simulate a day of parallel agent sessions through the gate CLI.

Creates ./demo-workspace/repo, spawns 26 agent sessions with a realistic mix
(good changes, broken changes, conflicting pairs, a high-risk change, an oversized
change, an abandoned attempt with a trace), runs the queue, and prints the result.
"""
from __future__ import annotations

import shutil
import stat
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE / "demo-workspace"
REPO = WORKSPACE / "repo"

CALC = "def add(a, b):\n    return a + b\n\n\ndef sub(a, b):\n    return a - b\n"
CALC_TEST = (
    "import unittest\nfrom calc import add, sub\n\n\n"
    "class CalcTest(unittest.TestCase):\n"
    "    def test_add(self):\n        self.assertEqual(add(2, 3), 5)\n\n"
    "    def test_sub(self):\n        self.assertEqual(sub(5, 3), 2)\n"
)


def sh(*args: str, cwd: Path = REPO, check: bool = True) -> str:
    proc = subprocess.run(args, cwd=cwd, capture_output=True, text=True)
    if check and proc.returncode not in (0, 2):
        raise SystemExit(f"{' '.join(args)}\n{proc.stdout}{proc.stderr}")
    return proc.stdout + proc.stderr


def gate(*args: str) -> str:
    return sh(sys.executable, str(HERE / "gate.py"), "-C", str(REPO), *args)


def write_and_commit(worktree: Path, files: dict[str, str], message: str) -> None:
    for name, content in files.items():
        path = worktree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    sh("git", "add", "-A", cwd=worktree)
    sh("git", "-c", "user.name=Agent Bot", "-c", "user.email=agent@example.com",
       "commit", "-qm", message, cwd=worktree)


def feature(n: int) -> dict[str, str]:
    return {
        f"lib/feature_{n}.py": f"def feature_{n}():\n    return {n}\n",
        f"tests/test_feature_{n}.py": (
            f"import unittest\nfrom lib.feature_{n} import feature_{n}\n\n\n"
            f"class T(unittest.TestCase):\n    def test(self):\n"
            f"        self.assertEqual(feature_{n}(), {n})\n"
        ),
    }


def force_remove(func, path, _exc):
    Path(path).chmod(stat.S_IWRITE)
    func(path)


def setup_repo() -> None:
    if WORKSPACE.exists():
        if sys.version_info >= (3, 12):
            shutil.rmtree(WORKSPACE, onexc=force_remove)
        else:
            shutil.rmtree(WORKSPACE, onerror=force_remove)
    REPO.mkdir(parents=True)
    sh("git", "init", "-q", "-b", "main")
    sh("git", "config", "user.name", "Demo Human")
    sh("git", "config", "user.email", "human@example.com")
    files = {
        "calc.py": CALC,
        "tests/test_calc.py": CALC_TEST,
        "auth/policy.py": "ALLOW_ALL = False\n",
        "lib/__init__.py": "",
    }
    for name, content in files.items():
        (REPO / name).parent.mkdir(parents=True, exist_ok=True)
        (REPO / name).write_text(content)
    gate("init")
    config = (REPO / "gate.toml").read_text().replace(
        "test_command = 'python -m unittest discover -s tests'",
        f"test_command = '\"{sys.executable}\" -m unittest discover -s tests -q'",
    ).replace("[lanes]\n", "[lanes]\nlib = ['lib/*']\n")
    (REPO / "gate.toml").write_text(config)
    sh("git", "add", ".")
    sh("git", "commit", "-qm", "initial")
    sh("git", "switch", "-q", "--detach")


def agent(sid: str, files: dict[str, str], model: str) -> str:
    worktree = Path(gate("spawn", sid).strip().splitlines()[-1])
    write_and_commit(worktree, files, f"{sid}: {next(iter(files))}")
    return gate("submit", sid, "--model", model, "--tool", "demo-agent/1.0", "--spec", f"TASK-{sid}")


def main() -> None:
    setup_repo()
    models = ["model-alpha", "model-beta"]
    print("== 26 agent sessions submit ==")
    for n in range(1, 17):
        print(agent(f"feat-{n:02d}", feature(n), models[n % 2]).strip())
    for n in range(1, 4):
        broken = CALC.replace("return a - b", f"return a - b + {n}")
        print(agent(f"broken-{n}", {"calc.py": broken, f"lib/b{n}.py": "x = 1\n"}, models[n % 2]).strip())
    for pair in ("x", "y"):
        print(agent(f"race-{pair}1", {f"lib/shared_{pair}.py": "VALUE = 1\n"}, "model-alpha").strip())
        print(agent(f"race-{pair}2", {f"lib/shared_{pair}.py": "VALUE = 2\n"}, "model-beta").strip())
    print(agent("authz", {"auth/policy.py": "ALLOW_ALL = False\nAUDIT = True\n"}, "model-alpha").strip())
    big = "".join(f"CONST_{i} = {i}\n" for i in range(900))
    print(agent("megachange", {"lib/mega.py": big}, "model-beta").strip())
    print(agent("leaky", {"lib/client.py": 'API_TOKEN = "' + "ghp_" + "Ab3" * 12 + '"\n'}, "model-beta").strip())

    worktree = Path(gate("spawn", "dead-end").strip().splitlines()[-1])
    write_and_commit(worktree, {"lib/idea.py": "IDEA = 'nope'\n"}, "dead-end attempt")
    trace = WORKSPACE / "dead-end.jsonl"
    trace.write_text('{"step": 1, "env": "OPENAI_API_KEY=sk-demo1234567890abcdefghij"}\n' * 200)
    print(gate("trace", "dead-end", str(trace)).strip())

    print("\n== review queue (what a human actually looks at) ==")
    print(gate("status"))

    print("== gate runs ==")
    print(gate("queue", "--drain", "--parallel", "2"))
    print(gate("approve", "authz", "--by", "juwen").strip())
    print(gate("queue", "--drain"))
    print(gate("abandon", "dead-end").strip())
    print("\n== post-merge verification ==")
    print(gate("verify").strip())

    print("\n== remaining work for humans/agents ==")
    print(gate("status"))
    print("== stats ==")
    print(gate("stats"))
    print("== last landed commit ==")
    print(sh("git", "log", "-1", "--format=%an <%ae> / committer %cn%n%n%B", "main"))
    print("== broken code ever on main? ==")
    bad = [c for c in sh("git", "rev-list", "main").split()
           if "a - b +" in sh("git", "show", f"{c}:calc.py")]
    print("NO" if not bad else f"YES: {bad}")


if __name__ == "__main__":
    main()
