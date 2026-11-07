# gate provenance trailers, version 1

Every change that gate merges is one squash commit on the target branch. Its message is the session title, a
blank line, then [git trailers](https://git-scm.com/docs/git-interpret-trailers) in the fixed order below. A
trailer is written only when it has a value. Revert commits made by `gate verify` and commits replayed onto a
moved target carry the extra trailers at the end of the table.

| Trailer | Value | Source | Always present |
|---|---|---|---|
| `Agent-Session` | session id, `[A-Za-z0-9._-]+` | `gate spawn <id>` | yes |
| `Agent-Model` | model name, one line | `submit --model` | no |
| `Agent-Tool` | agent tool or harness, one line | `submit --tool` | no |
| `Spec` | ticket, spec or issue reference, one line | `submit --spec` | no |
| `Prompt-SHA256` | lowercase hex SHA-256 of the prompt file (the prompt itself is never stored) | `submit --prompt-file` | no |
| `Trace-SHA256` | lowercase hex SHA-256 of the stored, redacted agent trace | `gate trace <id> <file>` | no |
| `Risk-Tier` | integer from `gate.toml` `[[tiers]]` (0 = lowest) | computed from the changed paths | yes |
| `Approved-By` | approver name, one line | `gate approve <id> --by <name>` | only for tier >= `approval_tier` |
| `Agent-Lane` | lane name | computed from the changed paths | when lanes are configured |
| `Agent-Base` | full SHA of the target tip the agent started from | `spawn` | yes |
| `Agent-Head` | full SHA of the agent's last commit (before squashing) | `submit` | yes |
| `Commuted-From` | full SHA of the tested commit this one replays | queue, when the target moved but files did not overlap | no |
| `Reverts` | full SHA of the gate commit that `verify` reverted | `gate verify` | revert commits only |

Other fields of a gate commit:

- **author** = the agent's identity from its own last commit (name, e-mail, date);
- **committer** = `gate <gate@localhost>`;
- **parent** = exactly one, the previous target tip (gate never writes merge commits).

## Guarantees

- Values supplied by the agent (`--model`, `--tool`, `--spec`, `--title`) must be a single line; `submit` refuses
  multi-line values and every value is flattened again when the commit is written, so a value cannot add a
  trailer of its own.
- Gate reads only the first occurrence of each trailer key.
- The format is additive: later versions may add trailers, never rename or remove these.

## What it does not prove

Trailers are claims written by whoever writes the commit. Anyone who can push to the target can write a commit
with committer `gate` and plausible trailers. The evidence is only as strong as the branch protection around it:

1. allow only the gate's credentials to push to the target (branch protection or a server-side hook);
2. run `gate audit --strict` in CI, so a commit on the target that gate did not make fails the build;
3. for tamper evidence, sign the gate's commits (git `commit.gpgsign` with an SSH or GPG key that only the gate
   host holds) and check signatures with `git log --show-signature`.

`gate audit` builds its report from these trailers plus the gate's local session records under
`<git-common-dir>/gate/`. Refused attempts exist only in those local records; keep that directory if refused
attempts must be part of the evidence.
