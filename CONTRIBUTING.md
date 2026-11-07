# Contributing

gate is one standard-library module (`gate.py`) plus its tests. Keep it that way: no runtime dependencies.

## Setup

```bash
git clone https://github.com/juw12/gate && cd gate
python -m pip install -e . ruff mypy coverage
git config --global user.name >/dev/null || git config --global user.name you   # tests create commits
```

Requires Python ≥ 3.11 and Git ≥ 2.40.

## Before you push

```bash
ruff check .
mypy gate.py
coverage run -m unittest test_gate && coverage report     # must stay ≥ 85 %
python demo.py                                            # last line must be "... NO"
```

CI runs the same checks plus the test suite on Linux, macOS and Windows × Python 3.11/3.13.

## Guidelines

- Tests run against real Git repositories in temp dirs; add one for every behaviour change, and a failure-path
  test (crash, race, conflict) for anything touching `land`, `publish`, locks or session state.
- Session state must stay crash-safe: write with `write_json` (temp file + rename), mutate under
  `lock("state")`, and make sure a kill at any line leaves something `reconcile()` or `gc` can finish.
- The JSON output (`OUT.result`, `OUT.event`) and exit codes are a public interface; note changes in
  `CHANGELOG.md`.
- Never print secret values; findings name the file and kind only.
