# Controlled-pilot failure analysis

The old corrected workbooks are evidence about different failure classes. Calling every
miss a “prompt issue” hides defects that no wording could fix; calling every miss an
“architecture issue” promises deterministic guarantees for mathematical judgment the
software does not have.

## How the classification is made

A failure is **architecture/deterministic** when the correct information never reaches the
responsible model, a valid output cannot pass the gate, rejected bytes remain in the file,
coverage is skipped, state/reporting is wrong, or an exact rule can decide the result.

A failure is **prompt/model judgment** when the model had the relevant current cells,
rules, task and authority, but failed to identify the mathematics or proposed/accepted a
bad semantic correction. Prompt improvements can reduce this class; only labelled live
evaluation can measure it.

## Historical output scores

`scripts/evaluate_controlled_run.py` scored the saved source/output/key triples without a
model call. These are exact-key scores, so a manually acceptable equivalent can still
need review; unexpected changed cells are source-to-output changes outside the keyed
targets.

| Saved run | Exact targets | Unexpected changed cells |
| --- | ---: | ---: |
| hard 15 | 7 / 11 | 1 |
| realistic 24 | 15 / 17 | 5 |
| adversarial 24 | 21 / 31 | 4 |
| adversarial 24 interrupted rerun (working copy) | 24 / 31 | 3 |

The screenshots' “finished” state therefore cannot be used as accuracy evidence. It said
the internal ledger and deterministic final gate were satisfied, not that the hidden
semantic answer key was fully matched.

## Findings from the controlled runs

| Observed failure | Classification | Why | Repair now in the system |
|---|---|---|---|
| A block with one known issue could hide another defect | Architecture | The independent sweep skipped every block with a surviving ledger entry | The independent reviewer now sweeps every current block |
| Rejected/revision patches appeared in the downloaded workbook | Architecture | The service wrote before review and tried to undo afterward | Proposals are simulated; only accepted candidates are written |
| `answerType` fixes were rejected after the auditor cited only `Answer` | Schema/prompt boundary | The finding discarded coordinated target cells, so the gate correctly refused the complete patch | Findings retain every row×column target; prompts and schema require the column that must change |
| Valid reset-per-step `h1` rows caused repeated gate rejection | Deterministic architecture | A duplicate dependency implementation used block scope and absolute validity | The duplicate check was removed; the registered convention-aware delta rules are authoritative |
| Trailing-space and duplicated-metadata repairs repeated wrong `before` values | Architecture | Exact, lossless cleanup was delegated to a probabilistic Writer | High-confidence mechanical repairs bypass the model and pass the same gate |
| A final clean validation displayed old findings | Persistence/reporting | “No rows” was mistaken for “no validation round” | Empty validation rounds have durable markers |
| UI counts reported edit operations rather than output differences | Reporting | Retries/rollbacks inflated “cells changed” | User-facing counts use the net source-to-output diff |
| A wrong inverse operation or incomplete solution set was missed | Prompt/model judgment plus coverage | The first auditor missed semantic mathematics; the second reviewer was structurally prevented from checking that block | Both math prompts enumerate form/domain/solution-count checks, and the independent sweep now covers the block |
| A correct exact fraction/LaTeX expression was rewritten to another representation | Prompt/model judgment | The edit was semantically plausible but unnecessary | Auditor/Writer/reviewer prompts now require preserving correct exact form, delimiters and representation |
| The UI's Background mode did nothing | Architecture | Notes were stored but routed to no agent | Notes now reach only the Initial Auditor as non-authoritative context |
| Prompt pins did not enforce their stored hash | Architecture/audit | A shared fragment could change while the same role version remained pinned | Composed prompt hashes are checked on resume |
| The audit hash omitted the response schema | Architecture/audit | The schema changes model behavior and is sent to the CLI | Canonical schema bytes are now part of `prompt_sha256` |
| Multi-row findings could authorize unintended cells | Architecture/schema | Separate row and column arrays were expanded as a Cartesian product | Findings now return explicit row-and-column cell pairs; out-of-block targets requeue the block |
| A long custom rules document was accepted but mostly ignored | Architecture/context | A second, silent 4,000-character cap existed only when model context was built | The hidden cap is removed; the documented instruction-ingestion bound is the only bound |
| A candidate copied the `before` value from a different cell and crashed during review | Architecture | Review moved before apply, but the exact live-cell check stayed only in the later writer | `validate_patch` now rejects an impossible `before` before simulation; the live-file check remains as the race barrier |
| A clean control's correct expression was replaced by an equivalent simplification | Model judgment plus review architecture | The reviewer first saw the claim together with a plausible replacement, which anchors the review on the edit rather than whether the source was wrong | Every model-only claim gets a claim-blind, from-scratch audit by the other role; exact target-and-category agreement is required, and provably equivalent Answer restatements are refused deterministically |
| An exact fractional answer was changed to a decimal so it matched an existing choice | Model judgment plus deterministic form | Both values are equivalent, but the task asked for an exact fraction and the wrong side of the mismatch moved | The gate preserves a fractional Answer when the block requests exact/fraction form; prompts direct the Writer to repair the choice list |
| A valid identifier was renumbered to close a numeric gap | Incorrect rule/prompt assumption | The standing rules require identifiers to be unique and dependencies to resolve, not consecutive labels | The obsolete numbering-gap rule is removed; prompts state that gaps are valid; a behavior-preserving model-only rename is rejected |
| A displaced row generated many symptom repairs before its root repair | Orchestration | Findings were processed by location rather than causal priority | Row/column-shift and block-structure roots enter each block's queue before answer, choice, and dependency symptoms |
| A hint said “undo subtraction” but instructed another subtraction | Prompt/model judgment with deterministic signal | Repeating the named operation is suspicious, but signed quantities mean text alone is not always proof | A non-repairing rule surfaces all four operation pairs to the agents; both auditor prompts explicitly require applying the operation to the equation |
| A repairable warning appeared under “Observations” while also holding the job back | Reporting/semantics | Severity was incorrectly used as a proxy for whether the council acts | Open work now mirrors the backend predicate: repairable findings plus all errors; observations are non-repairable warnings/observations. A consistent `h#` house style is explicitly non-repairable |

The interrupted rerun ended in `FAILED`, so its 24/31 working-copy score is diagnostic,
not a completed-pipeline accuracy result. Its screenshot's 29 open errors were also a
mid-pipeline snapshot: several were symptoms already corrected by later root patches but
never reconciled because the crash prevented final validation.

## What remains empirical

No offline test can prove that Sonnet will solve every new trigonometry problem correctly.
The deterministic layer can prove scope, before-values, rule regressions, atomic writes,
review coverage, provenance, and truthful reporting. Detection and correction of novel
semantic mathematics remain model-quality questions. They must be measured on held-out,
labelled workbooks with the controlled-run evaluator, at the exact model/effort/batch
settings intended for deployment.
