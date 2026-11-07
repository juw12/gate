# agentgate

A merge gate for AI-agent-generated code. Many parallel agent sessions go in; a small number of
tested, risk-tiered, provenance-tagged commits come out. Humans review only what policy says
needs a human.

| Bottleneck | What agentgate does |
|---|---|
| Merge contention | Server-style merges with `git merge-tree` (no checkout), batched, published with compare-and-swap (`update-ref old new` locally, `--force-with-lease` to a remote). |
| Throughput | **Lanes**: path-partitioned queues that merge and test in parallel (`queue --parallel N`). Only a short publish step is serialized. If the target moved while a lane was testing but none of the moved files overlap the lane's files, the lane's commits are replayed onto the new tip (`Commuted-From:` trailer) instead of re-tested. |
| CI cost / slow suites | Targeted suites (`[[tests]]` with `paths`) run only when relevant files change. They run in parallel, each in its own checkout, and pass results are cached by tree, so bisection, retries and `verify` never re-run a suite on a tree that already passed. Batches are bisected on failure. |
| Semantic conflicts | `agentgate verify` tests the target. If it's red, it bisects the commits since the last green point, finds the agent commit that broke it, tests a revert, and publishes the revert (session marked `reverted`). |
| Review load | Risk tiers from path policy. Tier 2 (auth, infra, CI, migrations, the policy file) waits for `approve --by NAME`. Changes over `max_changed_lines` bounce back to the agent. |
| Secrets | Submit-time scan of added lines blocks the merge (`reason: secret`) without echoing the value. Traces and archived patches are redacted (known token formats, URL credentials, `key = value`, high-entropy tokens). An optional external scanner (e.g. gitleaks) fails closed: a trace it still flags is not stored. |
| Integrity | Policy is read from the target branch; the exact submitted/approved head SHA is what merges. |
| Provenance | `Agent-Session`, `Agent-Model`, `Agent-Tool`, `Spec`, `Prompt-SHA256`, `Trace-SHA256`, `Risk-Tier`, `Approved-By`, `Agent-Lane`, `Agent-Base`, `Agent-Head` trailers; author = agent, committer = `agentgate`. |
| Retention / ref clutter | Merged sessions delete their branch at once; rejected ones keep it for a grace period, then become a gzipped, redacted diff outside Git refs. Traces expire by outcome. |
| Checkout cost / remote agents | Shared object store, per-agent worktree with optional sparse cone, or `spawn --no-worktree` for agents running in remote sandboxes that push `agent/<id>`. |

Standard library only. Python ≥ 3.11, Git ≥ 2.40.

## Quick start

```bash
pip install -e .                      # or: python agentgate.py ...
cd your-repo
agentgate init                        # writes agentgate.toml (lanes, suites, secrets, retention)
git add agentgate.toml && git commit -m "agentgate policy"
git switch --detach                   # the gate must not have the target branch checked out
agentgate doctor                      # add --write-test when `remote` is set

# per agent session
WT=$(agentgate spawn fix-1234 --sparse services/api)   # or: --no-worktree for remote sandboxes
#   ... agent works and commits ...
agentgate trace  fix-1234 ./session.jsonl              # optional; redacted, gzipped
agentgate submit fix-1234 --model <exact-model-id> --tool claude-code/2.x --spec ISSUE-1234

# gate (cron/systemd/CI loop)
agentgate queue --drain --parallel 8
agentgate verify                      # every N minutes: bisect + auto-revert a red target
agentgate status                      # needs_approval / reverted / too_large / rejected first
agentgate approve fix-1234 --by alice
agentgate stats
agentgate gc                          # daily
```

Exit codes: `0` ok, `1` error, `2` policy bounce (`too_large`, `secret`), `3` verify needs a human, `4` target moved during verify.

## Measured

Unit/e2e: `python -m unittest -v test_agentgate`, 22 tests on real repos.

Throughput, from `python bench.py --sessions 160 --lanes 8 --test-seconds 2 --batch 8` on one Windows laptop
(Git 2.55, Python 3.13), with a 2 s sleep standing in for CI:

| `--parallel` | wall | merges/day (extrapolated) | commuted publishes | publish-lock hold | ceiling of the serialized step |
|---|---|---|---|---|---|
| 1 | 109.5 s | 126 k | 0 / 24 | 87 ms | — |
| 8 | 45.9 s | 301 k | 22 / 24 | 1.4 s per 6.7 merges | ≈ 408 k/day |

The serialized publish step stops being the limit at about 400k merges/day on this hardware.
Above that, throughput is set by CI capacity: about \(\text{lanes} \times \text{batch} / \text{test time}\).
With 10-minute suites and batches of 8, 30k merges/day needs about 26 lanes' worth of CI workers.

## Publishing to GitHub

Set `remote = 'origin'`. The gate fetches, merges and tests locally, then pushes with
`--force-with-lease`. Give the gate's bot account or deploy key a ruleset bypass on the target
branch. `agentgate doctor --write-test` proves reachability and write access by pushing and deleting a scratch ref.
It cannot prove the bypass on the protected branch itself; only a real publish does.
Agents never open PRs, so `refs/pull/*` doesn't grow.

## What is still out of scope

- **Semantic conflicts are caught after the fact, not prevented.** Batches are tested together, and
  `verify` reverts a regression that a commuted publish (or a human) introduced. A bug no test detects is
  invisible to any merge gate.
- **Redaction is best-effort.** Patterns plus entropy catch common formats; plug in gitleaks/trufflehog via
  `secret_scan_command` for fail-closed coverage, and still treat traces as sensitive.
- **One host holds the state.** Lanes run in parallel, and several gate processes on the same host can share a
  repo (file locks), but state lives in `.git/agentgate`. Running gates on several hosts needs a shared store.
  A Scion-class system would replace the file locks with a database.
- **Verified on Windows only.** `.github/workflows/ci.yml` runs the tests, demo and benchmark on
  Linux/macOS/Windows × Python 3.11/3.13 once this is pushed to GitHub.
