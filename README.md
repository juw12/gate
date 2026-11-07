# gate

[![ci](https://github.com/juw12/gate/actions/workflows/ci.yml/badge.svg)](https://github.com/juw12/gate/actions/workflows/ci.yml)
[![release](https://img.shields.io/github/v/release/juw12/gate)](https://github.com/juw12/gate/releases)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)

A merge gate for AI-agent-generated code. Many parallel agent sessions go in; a small number of
tested, risk-tiered, provenance-tagged commits come out. Humans review only what policy says
needs a human.

| Bottleneck | What gate does |
|---|---|
| Merge contention | Server-style merges with `git merge-tree` (no checkout), batched, published with compare-and-swap (`update-ref old new` locally, `--force-with-lease` to a remote). |
| Throughput | **Lanes**: path-partitioned queues that merge and test in parallel (`queue --parallel N`). Only a short publish step is serialized. If the target moved while a lane was testing but none of the moved files overlap the lane's files, the lane's commits are replayed onto the new tip (`Commuted-From:` trailer) instead of re-tested. |
| CI cost / slow suites | Targeted suites (`[[tests]]` with `paths`) run only when relevant files change. They run in parallel, each in its own checkout, and pass results are cached by tree, so bisection, retries and `verify` never re-run a suite on a tree that already passed. Batches are bisected on failure. |
| Semantic conflicts | `gate verify` tests the target. If it's red, it bisects the commits since the last green point, finds the agent commit that broke it, tests a revert, and publishes the revert (session marked `reverted`). |
| Review load | Risk tiers from path policy. Tier 2 (auth, infra, CI, migrations, the policy file) waits for `approve --by NAME`. Changes over `max_changed_lines` bounce back to the agent. |
| Secrets | Submit-time scan of added lines blocks the merge (`reason: secret`) without echoing the value. Traces and archived patches are redacted (known token formats, URL credentials, `key = value`, high-entropy tokens). An optional external scanner (e.g. gitleaks) fails closed: a trace it still flags is not stored. |
| Integrity | Policy is read from the target branch; the exact submitted/approved head SHA is what merges. |
| Provenance | `Agent-Session`, `Agent-Model`, `Agent-Tool`, `Spec`, `Prompt-SHA256`, `Trace-SHA256`, `Risk-Tier`, `Approved-By`, `Agent-Lane`, `Agent-Base`, `Agent-Head` trailers; author = agent, committer = `gate`. |
| Retention / ref clutter | Merged sessions delete their branch at once; rejected ones keep it for a grace period, then become a gzipped, redacted diff outside Git refs. Traces expire by outcome. |
| Checkout cost / remote agents | Shared object store, per-agent worktree with optional sparse cone, or `spawn --no-worktree` for agents running in remote sandboxes that push `agent/<id>`. |

Demo page with a recorded run: https://juw12.github.io/gate/

Standard library only. Python ≥ 3.11, Git ≥ 2.40.

## How it works

```mermaid
flowchart LR
  A1[agent session] -- spawn / commit --> B[agent/&lt;id&gt; branch]
  A2[agent in remote sandbox] -- push --> B
  B -- submit --> P{policy<br/>secrets · size · risk tier}
  P -- bounced, exit 2 --> A1
  P -- tier 2 --> H[human: gate approve]
  P -- queued --> L[lane queue]
  H --> L
  L -- merge-tree batch --> T[targeted suites<br/>parallel, cached by tree]
  T -- red: bisect --> R[rejected session]
  T -- green --> PUB[publish<br/>serialized, compare-and-swap]
  PUB -- target moved, disjoint files --> C[replay without re-test]
  C --> PUB
  PUB --> M[(target branch)]
  M -- gate verify --> V{green?}
  V -- red: bisect + revert --> M
```

State lives in `.git/gate/` (session JSON, counters, gzipped traces and archived diffs) and never in Git refs,
so agent attempts don't bloat clones. Every write, traces and archives included, is a temp file + atomic rename.
Cross-process exclusion uses lock files that record `pid host token` and are kept alive by a heartbeat (see
[Crash safety](#crash-safety)); they work across hosts sharing the repository over NFS.

## Install

```bash
pip install git+https://github.com/juw12/gate      # puts `gate` on PATH; or copy gate.py, it has no dependencies
gate --version
```

## Quick start

```bash
cd your-repo
gate init                        # writes gate.toml; detects your test command (pytest, npm, go, cargo, unittest)
git add gate.toml && git commit -m "gate policy"
git switch --detach              # the gate never moves a checked-out target (`gate doctor --fix` does this)
gate doctor                      # add --write-test when `remote` is set
gate completion bash >> ~/.bashrc   # optional: zsh and fish too

# per agent session
WT=$(gate spawn fix-1234 --sparse services/api)   # or: --no-worktree for remote sandboxes
#   ... agent works and commits ...
gate trace  fix-1234 ./session.jsonl              # optional; redacted, gzipped
gate submit fix-1234 --model <exact-model-id> --tool claude-code/2.x --spec ISSUE-1234

# gate (cron/systemd/CI loop)
gate queue --drain --parallel 8
gate verify                      # every N minutes: bisect + auto-revert a red target
gate status                      # needs_approval / reverted / too_large / rejected first
gate approve fix-1234 --by alice
gate stats
gate gc                          # daily
```

## For agents and automation

Add `--json` (before or after the subcommand) to get exactly one JSON document on stdout and nothing else:

```console
$ gate --json submit fix-1234 --model claude-x
{"command": "submit", "exit_code": 0, "result": {"session": "fix-1234", "status": "queued", "tier": 1,
 "lane": "api", "sized_lines": 42, "files": ["services/api/x.py"], "reason": null, "findings": [], ...},
 "events": []}

$ gate --json queue --drain
{"command": "queue", "exit_code": 0, "result": {"lanes": ["api"], "merged": [{"session": "fix-1234",
 "commit": "9f1c...", "lane": "api"}], "rejected": [], "recovered": [], "still_queued": 0}, "events": [...]}

$ gate --json show nope
{"command": "show", "exit_code": 1, "error": "unknown session 'nope'", "events": []}
```

An agent loop needs three calls: `spawn` (read `result.worktree`), `submit` (read `result.status`; on exit 2
read `result.reason` / `result.findings`, fix and resubmit), then poll `gate --json show <id>` until
`result.session.status` is `merged`, `rejected` (read `detail`) or `reverted`. `submit` is idempotent: retrying
an unchanged queued session returns the same answer instead of an error.

[`examples/agent_loop.py`](examples/agent_loop.py) is a complete reference client (about 100 lines, JSON interface
only, no imports from gate). The test suite runs it end to end, so it cannot drift from the real interface:

```bash
python examples/agent_loop.py --repo . --session fix-1 --model my-model \
    --file docs/note.md "hello" --drive-queue
```

| Exit code | Meaning |
|---|---|
| `0` | ok |
| `1` | error: usage, git or environment problem; the remote refused the push (e.g. branch protection); a lock held by a live process; `doctor` found a failing check |
| `2` | `submit` bounced by policy: `too_large`, or secrets on added lines (`rejected`, reason `secret`) |
| `3` | `verify`: target is red and needs a human (no green baseline, revert conflicts, revert still red, or `--no-revert`) |
| `4` | `verify`: target moved while publishing the revert; run `verify` again |

## Crash safety

Any gate process can be killed at any point without losing or duplicating work. The tests kill the real process
(SIGKILL on Linux/macOS, TerminateProcess on Windows) at these points, not an exception raised in-process:

| Killed while... | Next run |
|---|---|
| testing a batch | Nothing was published. The lane lock names a dead pid, so the next `queue` takes it over at once and re-lands the batch (passing suites are cached by tree, so they aren't re-run). |
| publishing | The ref update is a single compare-and-swap (`update-ref old new`, or `push --force-with-lease`): it either happened or it didn't. |
| after publishing, before recording `merged` | `queue` (and `gc`) first reconciles: every gate commit carries an `Agent-Head` trailer, so sessions whose head is already on the target are marked `merged` (`recovered: true`) instead of being landed twice. |
| after `verify` published a revert, before recording it | The next `verify` reads the `Reverts:` and `Agent-Session:` trailers and marks the session `reverted` (`result.recovered`). |
| after recording `merged`, before deleting the branch | `gc` removes leftover branches and worktrees of merged/abandoned sessions. |

**Locks** are kept alive by a heartbeat: the holder touches its lock file every 5 s, however long its tests run.
Whether a lock is abandoned is decided by watching it, never by comparing its timestamp with this machine's
clock, because hosts sharing a repository over NFS can disagree by minutes:

- owner is a dead process on this host: taken over at once;
- this process has watched the lock stay unchanged for `LOCK_STALE_S` (default 60 s, env `GATE_LOCK_STALE_S`) on
  its own monotonic clock: taken over (owner crashed, froze, lost its host, or its pid was reused). A waiter whose
  `--wait` is shorter keeps watching a silent lock until this decides, so a dead foreign lock never blocks forever;
- the timestamp is more than 10 x `LOCK_STALE_S` old: taken over (tolerates 9 x `LOCK_STALE_S` of clock skew).

Lock files are read with open() + fstat(), so NFS close-to-open consistency shows another host's heartbeat.
Breaking a stale lock goes through a separate breaker file and a re-check, so two processes never both take it
over, and a holder only deletes a lock that still carries its own token. The
[`nfs` workflow](.github/workflows/nfs.yml) proves this on a real NFS export: two docker hosts, each its own NFS
client, host B's clock 120 s ahead (so by B's clock A's live lock looks two minutes old). B tried for 50 s and
did not take A's heartbeating lock; after `docker kill` of A, B took the lock over after 20.0 s with
`GATE_LOCK_STALE_S=20`; and both hosts draining the same queue at once (first one lane each, racing on the
publish compare-and-swap, then sharing all lanes) landed every session exactly once.

**Refusals are not contention.** If the remote refuses the push for any reason other than a moved target (a
protected branch, a hook, missing permission, a network error), gate stops at once with the remote's message and
what to do, runs the tests only once, and leaves the sessions queued. It also refuses to move a target branch
that is checked out in any worktree, whether in `queue`, a remote fetch or a `verify` revert.

## Measured

Unit/e2e: `python -m unittest -v test_gate`, 55 tests on real repos (real-process kills, skewed and silent
locks, contended locks, protected remote, JSON interface, remote races, verify/revert, secrets, clean UTF-8 docs).
CI runs them on Linux, macOS and Windows x Python 3.11/3.13 and also enforces `ruff`, `mypy` and ≥ 85 % line
coverage of `gate.py`. The `nfs` workflow runs the two-host NFS test on every change to `gate.py`.

Throughput, from `python bench.py --sessions 160 --lanes 8 --test-seconds 2 --batch 8` on one Windows laptop
(Git 2.55, Python 3.13), with a 2 s sleep standing in for CI:

| `--parallel` | wall | merges/day (extrapolated) | commuted publishes | publish-lock hold | ceiling of the serialized step |
|---|---|---|---|---|---|
| 1 | 109.5 s | 126 k | 0 / 24 | 87 ms | — |
| 8 | 45.9 s | 301 k | 22 / 24 | 1.4 s per 6.7 merges | ≈ 408 k/day |

The serialized publish step stops being the limit at about 400k merges/day on this hardware.
Above that, throughput is set by CI capacity: about \(\text{lanes} \times \text{batch} / \text{test time}\).
With 10-minute suites and batches of 8, 30k merges/day needs about 26 lanes' worth of CI workers.

With `remote` set, each publish also does a fetch and a `--force-with-lease` push. The e2e `throughput`
scenario measured ten publishes in a row against github.com through a corporate HTTP proxy, from a loaded
laptop: the publish lock was held 6.4 s at the median and 17.6 s at p95. Sized on the p95, that is a ceiling of
\(86400 / 17.6 \times 8 \approx 39\text{k}\) merges/day at batch 8 (about 108k on the median). Raise
`max_batch` or run the gate next to the remote if you need more headroom.

## Publishing to GitHub

Set `remote = 'origin'`. The gate fetches, merges and tests locally, then pushes with
`--force-with-lease`. Give the gate's bot account or deploy key a ruleset bypass on the target
branch. `gate doctor --write-test` proves reachability and write access by pushing and deleting a scratch ref.
It cannot prove the bypass on the protected branch itself; only a real publish does.
Agents never open PRs, so `refs/pull/*` doesn't grow.

[`e2e_github.py`](e2e_github.py) checks all of this against a real remote, on a throwaway `gate-e2e-<id>` branch
that it deletes afterwards, and the [`e2e` workflow](.github/workflows/e2e.yml) runs it against this repository
weekly and on demand:

| Scenario | What must happen |
|---|---|
| batch | three sessions publish in one queue run; GitHub's branch head equals the gate's merged commit |
| crash | the gate process is killed (TerminateProcess / SIGKILL) after the push reached GitHub but before recording it; the next run recovers both sessions and GitHub gets no duplicate commits |
| race | a human pushes an unrelated commit while the gate is testing; the gate keeps it and replays its own commit on top (`Commuted-From:`) without re-testing |
| protected | branch protection requires a PR, admins included: the push is refused, gate reports the remote's message at once, the tests ran once, the session stays queued, GitHub is unchanged; after exempting admins (the documented bypass) the same session publishes. Needs `GATE_E2E_ADMIN_TOKEN`; skipped in the weekly workflow, whose token can't change branch protection |
| throughput | ten publishes in a row; reports the median and p95 publish-lock hold |

Policy is read from the remote target: a stricter `gate.toml` pushed to the remote applies to the very next
`submit`.

## What is still out of scope

- **Semantic conflicts are caught after the fact, not prevented.** Batches are tested together, and
  `verify` reverts a regression that a commuted publish (or a human) introduced. A bug no test detects is
  invisible to any merge gate.
- **Redaction is best-effort.** Patterns plus entropy catch common formats; plug in gitleaks/trufflehog via
  `secret_scan_command` for fail-closed coverage, and still treat traces as sensitive.
- **State is files, not a database.** Several gate processes can share a repository, on one host or across hosts
  through a network filesystem with working `O_EXCL` and close-to-open consistency (NFSv3/v4). That is tested on
  a real NFS export with two clients and a skewed clock, but on one CI machine, not a production NFS cluster
  under load, and SMB/CIFS shares are untested. A lock's takeover waits up to `LOCK_STALE_S` after a crash. A
  Scion-class deployment would put session state in a database.
- **Platforms:** CI (`.github/workflows/ci.yml`) runs tests, demo and benchmark on Linux, macOS and
  Windows × Python 3.11/3.13.

See [CHANGELOG.md](CHANGELOG.md) for releases and [CONTRIBUTING.md](CONTRIBUTING.md) to work on gate.
MIT licensed.
