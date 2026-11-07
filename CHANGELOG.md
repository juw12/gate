# Changelog

All notable changes. Versions follow [semantic versioning](https://semver.org/); the JSON output and exit
codes are part of the public interface.

## [0.4.0] - 2026-11-07

Every finding of the round-2 review was checked in the code first; all were real and are fixed here.

### Fixed after the round-3 review (each reproduced by a failing test first)
- **Malformed policy.** A `gate.toml` with a TOML syntax error, a value of the wrong type, or a `[[tests]]`
  entry without `command` crashed with a traceback (the missing command only when `queue` reached it). The
  policy is now validated when it is read; the error names the file, the line or key, and how to fix it.
- **Forged provenance trailers.** `--model`, `--tool`, `--spec` and `--title` went into the squash commit
  message verbatim, so a value containing a newline could add a fake `Agent-Head:` trailer and make crash
  recovery mark another queued session as merged. `submit` now rejects multi-line values, and every value is
  flattened to one line when the commit is written.
- **verify and merge commits.** A merge commit on the target that carried an `Agent-Session` trailer could be
  picked as the culprit and auto-reverted against its first parent, undoing a whole branch. verify now stops
  with exit 3 for any merge-commit culprit.
- **Kill while testing.** The crash-safety row "killed while testing a batch" is now proven with a real process
  kill between the green test run and publication (`GATE_FAULT=after-tests`): nothing is published, the next
  run re-lands the batch once.

### Fixed during the round-2 review
- **Locks across hosts.** Staleness compared a lock file's mtime with the local clock, so clock skew between
  hosts (or NFS attribute caching) could make a live lock look abandoned. Liveness is now judged by observation:
  a lock is taken over only after this process has watched it stay unchanged for `LOCK_STALE_S` on its own
  monotonic clock (or its owner is a dead pid on this host, or it is older than 10x `LOCK_STALE_S`). Lock files
  are read with open() + fstat() so NFS close-to-open consistency applies. Proven on a real NFS export with two
  hosts and a 120 s clock skew (`nfs` workflow).
- **Protected branches.** A push refused by branch protection or a hook was treated as contention: the batch
  was re-tested five times and the run ended with a misleading "target kept moving". Only lease failures count
  as contention now; any other refusal stops at once with the remote's message and what to do, sessions stay
  queued, tests run once.
- **Checked-out target.** Only `queue` refused to run while the target branch was checked out; `sync_tip` and
  `advance` (remote fetch, verify revert) could still move it under a worktree. Both now refuse.
- **Crash during verify.** A verify killed after publishing a revert never marked the session `reverted`. The
  next verify reconciles from the `Reverts:` / `Agent-Session:` trailers (`result.recovered`).
- **gc vs resubmit.** gc abandoned rejected sessions from a snapshot taken without the state lock, so a session
  fixed and resubmitted meanwhile could lose its branch. Abandon is now a compare-and-set on head and status.
- **Atomic writes.** Traces and archived patches are written via temp file + rename like all other state.
- **External scanner.** A scanner that hangs or cannot start is a finding (fail closed) instead of a crash;
  limit `SCAN_TIMEOUT_S` = 300 s.
- **merge-tree errors.** A merge-tree failure on a bad head was reported as an empty conflict; it is now an
  error naming the session.
- **Windows lock breaking.** Watching a stale-lock breaker file kept a handle open that stopped its owner
  deleting it (found by CI). The breaker is observed with stat(), and unlinks retry briefly.
- README encoding: the 0.3.0 README had a byte-order mark and mojibake from an editor round trip.

### Added
- `--json` is accepted after the subcommand as well (`gate queue --json`).
- `gate completion bash|zsh|fish`, generated from the parser.
- `gate doctor --fix` detaches a clean main checkout that has the target checked out.
- `GATE_LOCK_STALE_S` environment variable (default 60).
- Tests that kill the real process (SIGKILL / TerminateProcess) after a publish and after a revert publish,
  instead of raising an exception in-process; tests for skewed and silent foreign locks, a protected remote,
  the checked-out guard, gc racing a resubmit, atomic writes, scanner timeout, merge-tree failure, verify with no
  green baseline, completion scripts, doctor --fix, and clean UTF-8 in every tracked text file.
- `nfs_e2e.py` + `nfs` workflow: two docker hosts, each its own NFS client, one clock 120 s ahead; live lock
  kept, dead host's lock taken over after `LOCK_STALE_S`, both hosts draining at once land every session once.
- `e2e_github.py`: the crash scenario now kills the real process; new `protected` (branch protection with
  admins enforced, then the documented bypass) and `throughput` (ten publishes, lock-hold median/p95) scenarios.
- `bench.py --test-seconds-max` (per-candidate suite time drawn from a seeded uniform range) and `--modes`.

## [0.3.0] - 2026-11-07

### Added
- Heartbeat locks: holders refresh their lock every 5 s; a lock silent for 60 s is taken over. This also covers
  reused pids, frozen processes and owners on other hosts sharing the repository. A holder never deletes a lock
  that no longer carries its token.
- `gate init` detects the test command (pytest, npm, go, cargo, unittest) and says why.
- `examples/agent_loop.py`: reference agent client using only `--json`, exercised by the test suite.
- `e2e_github.py` and a weekly/on-demand `e2e` workflow: batch, crash-after-push recovery and concurrent-push
  scenarios against a real GitHub remote on a throwaway branch.
- Error messages say what to do next (unknown session, nothing committed, dirty worktree).
- mypy now runs with `disallow_untyped_defs`.

### Fixed
- In remote mode the policy was read before fetching, so a `gate.toml` change pushed to the remote applied one run
  late. A fetch that moves the target now reloads the policy, and `submit` fetches first.

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
