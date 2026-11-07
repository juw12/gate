# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org/); the JSON output and exit
codes are part of the public interface.

## [0.2.0] - 2026-11-07

### Added
- `--json` on every subcommand: one JSON document `{command, exit_code, result, events}` (or `{error}`) on stdout.
- `--version`; `--help` now documents the agent workflow and exit codes; every argument has help text.
- Crash recovery: `queue` and `gc` reconcile sessions whose `Agent-Head` is already on the target (a run killed
  after publishing), so work is never landed twice. `gc` also removes branches/worktrees left by a crash.
- Locks record `pid host`; a lock left by a dead process on this host is taken over immediately.
- Idempotent `submit` (unchanged queued session) and `approve` (same approver).
- LICENSE (MIT), CHANGELOG, CONTRIBUTING, architecture diagram, crash-safety and agent-integration docs.
- CI lint job: `ruff`, `mypy`, coverage ≥ 85 %.

### Fixed
- `publish()` could pass `None` to the ref update when a commuted replay conflicted (caught by mypy).

## [0.1.0] - 2026-11-06

First public version: lanes with parallel queues, commute-without-retest publish, targeted and cached suites,
batched testing with bisection, `verify` with bisect + auto-revert, secret blocking and trace redaction,
branch-only sessions, `doctor --write-test`, benchmark, CI on Linux/macOS/Windows.
