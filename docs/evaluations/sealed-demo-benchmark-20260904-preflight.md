# Sealed demo benchmark preflight — 2026-09-07

## Scope

This is an **offline fixture-validity check**, not an accuracy result. No workbook from
this suite was sent to Claude during this check, and no model call was made.

The suite contains four workbooks with 12 problem blocks each and 36 automated keyed
corrections in total:

| Workbook | Keyed corrections | Input score | Deterministic-only score | Logical shadow calls |
|---|---:|---:|---:|---:|
| sealed-a-algebra | 8 | 0/8 | 3/8 | 17 |
| sealed-b-trig-logs | 8 | 0/8 | 3/8 | 17 |
| sealed-c-matrices-conics | 12 | 0/12 | 2/12 | 19 |
| sealed-d-sequences-probability | 8 | 0/8 | 3/8 | 16 |
| **Total** | **36** | **0/36** | **11/36** | **69** |

The input score establishes that every automated key item is actually defective in the
planted workbook. The deterministic-only score shows that 11 keyed corrections can be
made before asking a semantic model. It is not a model score.

## Safety checks

- Every source workbook hash was unchanged after its shadow run.
- Deterministic repair changed no cell outside the key's targets or allowed related cells.
- No clean control changed.
- The blocking/error deterministic findings were traced to planted defect rows. Some
  rules identify the symptom cell rather than the eventual repair cell — for example an
  answer-not-in-choices finding points at the Answer while the keyed repair changes the
  choice list — so row-level provenance and allowed related cells remain part of scoring.
- Each workbook completed every offline pipeline phase without a provider failure.

## What remains sealed

The semantic outcomes are still unknown. A live run must receive only the workbook, never
the answer-key JSON. The corrected workbook is scored against the key only after the
council finishes. Results must report defect recall, unexpected edits, changed clean
controls, human-escalation rate, physical calls, generated tokens, failures, and elapsed
time.

An authentication check from inside Codex's restricted command sandbox reported
`loggedIn: false` because that sandbox cannot read the macOS Keychain. The same command
outside the sandbox reported the real state: a `claude.ai` Pro subscription login. This
distinction matters operationally: the council launched normally from Terminal can use
the login, while an isolated diagnostic must not be treated as evidence that the user was
logged out. No API or different model was substituted.
