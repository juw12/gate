# gate design system

The site at <https://juw12.github.io/gate/> (`docs/index.html`) follows this file. Agents editing the page read
it first. Chosen in a judged four-direction bracket (ledger, departures board, docs split-pane, Swiss signal);
the ledger won its semi-final 2-0 on council votes and beat its own previous version 3-0 after adding the
session strip.

## Idea

A printed audit ledger. gate's job is evidence: what merged, what was refused, who approved it. The page reads
like a report an auditor would file, with real numbers from the recorded demo run and nothing invented.

References: accounting ledgers and audit reports (ruled rows, dotted leaders, double rules), Stripe docs
(code next to prose), Trunk merge queue (near-monochrome with one green signal), Bors (text-first).

## Palette (one accent)

| Token | Hex | Use |
|---|---|---|
| `--paper` | `#f7f5ef` | page background |
| `--ink` | `#1c1d21` | text, rules, borders |
| `--soft` | `#55575e` | secondary text (6.6:1 on paper) |
| `--rule` | `#d6d0c2` | hairlines |
| `--panel` | `#efece3` | code blocks |
| `--accent` | `#0f6b3a` | primary button, merged status (6.1:1 on paper; white on it 6.6:1) |
| `--accent-ink` | `#0b5c2f` | links, hover |
| `--t-bad` | `#a3321f` | rejected status only (6.4:1) |
| `--t-warn` | `#8a5a00` | too-large status only (5.4:1) |
| `--t-mute` | `#5f6167` | muted text in code blocks (5.3:1 on panel) |

Red and amber appear only as status colours. No purple, indigo or violet anywhere, no gradients.

## Type

- Display: Source Serif 4 (600), headings only.
- Body: IBM Plex Sans (400/600).
- Data, code, labels: IBM Plex Mono (400/500/600), tabular numbers.
- Fonts are self-hosted in `docs/fonts/` (OFL), latin subset, `font-display: swap`. No third-party requests.

## Layout and spacing

- Sheet max width 1040 px, 28 px gutters (18 px on phones).
- Section rhythm 52 px; a 150 px left margin carries the section reference (`01 / The run`).
- Spacing steps: 4, 8, 12, 16, 20, 28, 36, 48, 56 px.
- Radius 2 px on the button, 0 elsewhere. No shadows. No motion beyond hover colour.
- All grid columns use `minmax(0, 1fr)`; code blocks scroll inside themselves so the page never scrolls sideways.

## Signature

The ledger card in the hero: six outcome rows with dotted leaders, a strip with one block per agent session in
run order (green merged, red rejected, amber too large), and a double-ruled stamp "Broken code ever on main: None".

## Voice

Plain and verifiable. Every number must come from the recorded run, the README or a release.
Banned words: supercharge, seamless, unlock, next-level, blazing, revolutionary, effortless, magic.

## Banned defaults

- Emoji anywhere (headings, buttons, lists, links).
- Purple, indigo or violet; purple-to-blue gradients; gradient text; aurora or glow blobs; glassmorphism.
- Inter or any single default sans as the only face.
- Rows of identical rounded icon cards; cliche icons (sparkles, rocket, lightning); decorative status dots.
- Centred-everything layouts; fake testimonials or logos; stock 3D shapes.
- "Get started" as the only call to action. The primary action is "Install gate".

## Checks before shipping a change to the page

1. `slop_check.py docs/index.html`: 0 blockers, at most 2 warnings.
2. Screenshots at 390 px and 1440 px: no horizontal scroll, nothing clipped.
3. Contrast pairs above stay at AA or better.
