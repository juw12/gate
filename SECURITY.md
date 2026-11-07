# Security

## Reporting a vulnerability

Use GitHub's private vulnerability reporting on this repository (Security tab, "Report a vulnerability"). Please
do not open a public issue for a security problem. Expect a first answer within 7 days; gate is maintained by one
person, so say so in your own risk review.

## Answers for a security questionnaire

Short, checkable answers to what a vendor review usually asks. Each answer names the place in the code or docs
where it can be verified.

| Question | Answer |
|---|---|
| What does it run as? | A CLI started by your own user, cron, systemd unit or CI job. No daemon, no listening port. |
| Network access? | None of its own. It runs `git` (fetch/push to the remote you configured) and your test commands. No telemetry, no update check. |
| Third-party dependencies? | None at runtime: one file, `gate.py`, Python standard library only (`pyproject.toml` has no dependencies). |
| Where is data stored? | In the repository: session records, locks, logs and traces under `<git-common-dir>/gate/`, plus one git worktree per agent session. Nothing leaves the machine except what `git push` sends to your remote. |
| Does it store prompts or traces? | Prompts: no, `submit --prompt-file` records only its SHA-256. Traces: only if you run `gate trace <session> <file>`, which redacts secrets, stores the trace gzipped under `<git-common-dir>/gate/traces/` and records its SHA-256. |
| Secrets in agent changes? | Added lines are scanned for private keys, AWS, GitHub, GitLab, Stripe, Slack, Google and Azure keys, JWTs, bearer tokens, URL credentials, password-style assignments and high-entropy strings; an external scanner can be added and fails closed. A hit bounces the submit with exit 2 and the finding is redacted in output. It is a pattern scanner, not a guarantee. |
| Can an agent bypass review? | Not through gate: a change at or above `approval_tier` waits in `needs_approval` until `gate approve --by <name>`. Pushing to the target directly is outside gate; block it with branch protection and detect it with `gate audit --strict`. |
| Can an agent forge provenance? | Not through `submit`: agent-supplied values must be one line and are flattened again when written (test: `test_provenance_fields_cannot_forge_trailers`). Anyone with push rights can still write any commit; see [`docs/provenance.md`](docs/provenance.md). |
| What runs agent code? | Only your configured test commands, in a separate worktree, with a timeout. gate does not sandbox them; run gate as a user or container with the least rights your tests need. |
| Audit trail? | Every merged change is one commit with trailers ([`docs/provenance.md`](docs/provenance.md)); `gate audit` produces a period report of merges, reverts, refusals and bypasses. |
| Crash safety? | Any process may be killed at any point without losing or duplicating a merge; proven by killing the real process in the test suite (README, "Crash safety"). |
| Licence? | MIT. |
| Release integrity? | Releases are tagged on GitHub and the CI runs the full suite on Linux, macOS and Windows for every commit. Release artefacts are not signed yet. |
| Supported versions? | The latest minor release. |
