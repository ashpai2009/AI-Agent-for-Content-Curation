# Deployment readiness

## Current status

The repository is a **local evaluation prototype**, not yet a centrally hosted Berkeley
service. Its production client starts the local Claude Code CLI and authenticates through
the subscription session in that machine's keychain. There is deliberately no API-key or
SDK fallback.

That boundary is appropriate for the present controlled pilots: it keeps the workbook on
one curator's machine except for the exact text sent to Claude, and it prevents a web host
from holding a personal credential. It is not the architecture that would consume API
credits purchased by an organization.

Anthropic documents three relevant authentication paths for Claude Code: Anthropic Console
with active billing, a Claude App Pro/Max login, and enterprise platforms such as Amazon
Bedrock or Google Vertex AI. It also documents print mode as the programmatic CLI path.

- <https://docs.anthropic.com/en/docs/claude-code/getting-started>
- <https://docs.anthropic.com/en/docs/claude-code/cli-usage>
- <https://docs.anthropic.com/en/docs/claude-code/llm-gateway>

## What is mechanically covered now

- The uploaded source file is read-only and hash-checked; output is written to a separate
  working copy.
- Every graded row is accounted for by the Initial Auditor and again by the independent
  sweep. After repairs, a blind Final Semantic Verifier re-solves every current graded row;
  edits invalidate that block's final marker until it is checked again.
- The two full audit passes use different attention order without adding a call: the
  Initial Auditor derives mathematics before consulting recorded answers, while the
  Independent Reviewer reconstructs the instructional contract and hint sequence first.
  Both retain the complete semantic checklist. This is a general anti-correlation design,
  not evidence of higher accuracy until a sealed live evaluation measures it.
- Every unsupported model finding is audited from scratch by the other audit role, without
  exposing the original accusation. Exact agreement proceeds directly; related or silent
  results go to a separate adjudicator, and an undecided claim cannot be reported as fixed.
  Reviewers then judge the simulated candidate separately.
- A Writer proposal is simulated and reviewed before it can change workbook bytes.
- Exact target cells, before-values, allowed scope, deterministic regressions, and final
  source-to-output differences are gated in Python.
- Prompt versions, composed prompt hashes, JSON schemas, provider behavior, and the
  Python-side pipeline contract (currently version 18) are pinned per job.
- Each physical model invocation has a durable audit row and a pre-call budget charge.
- Physical calls and generated output tokens have separate, size-aware, per-workbook
  ceilings. Both are pinned and shown in the local interface; cache traffic is reported
  but does not masquerade as newly generated output.
- Worker replacement runs recovery before resuming any durable phase.
- Upload size, job steps, repair attempts, model calls, provider failures, process output,
  process time, run time, concurrency, and retention are bounded.
- The local API supports bearer authentication; the browser proxy keeps that token out of
  client JavaScript.
- A clean installed wheel is checked for current prompts and retired provider code.
- Correct mathematical restatements in an Answer cell are protected from normalization:
  equation-to-value and named-evaluation-to-value rewrites are rejected when they are
  provably equivalent, while genuinely different answers remain repairable.
- Exact, lossless defects no longer consume Writer or reviewer calls: boundary and
  irregular whitespace, known Unicode spellings, doubled LaTeX command escapes, ASCII
  exponent markers, repeated block names, redundant metadata, supported answer-type
  corrections, scaffold namespaces, and dependency-chain values derived uniquely from
  the row/step structure are repaired in Python through the same patch gate. Dependency
  automation refuses rows carrying independent shift/corruption evidence.
- Workbook notation detection uses positive notation evidence. Plain numbers and bare
  graded equations are neutral, so a LaTeX workbook with ordinary numeric answers is not
  misclassified as mixed.

The exact model disclosure is in `docs/llm-data-contract.md`.

The four-workbook demo benchmark's no-call fixture check is recorded in
`docs/evaluations/sealed-demo-benchmark-20260904-preflight.md`. Its subsequent frozen live
run is recorded in `docs/evaluations/sealed-demo-benchmark-20260907-live.md`: 33/33
machine checks passed, manual review accepted two of three instructional alternatives,
and the combined substantive score was 35/36 (97.2%) with no unexpected edit or changed
clean control. That is a controlled 48-problem synthetic result, not a production SLA.

## What is not established yet

- **Real-workbook generalization.** The new sealed synthetic suite provides a frozen live
  accuracy result, but 48 synthetic problems cannot establish performance across the
  diversity of real OpenStax chapters. Historical corrected files contain misses and
  unexpected edits, and the older `20260819` suite is regression material because its
  failures shaped the system. A deployment claim still needs a sealed set sampled from
  real curator work and judged by curators.
- **Organization provider choice.** Console API, Bedrock, Vertex AI, or an approved gateway
  is a Berkeley decision involving billing, identity, retention, and procurement.
- **Multi-user boundaries.** The current SQLite/data-directory design, shared bearer token,
  and local recent-job history are suitable for one trusted curator, not an organization.
- **API cost.** Subscription calls do not establish an API budget. Cost needs token usage
  from a run made through the provider and model the organization intends to purchase.
- **Operational controls.** Central deployment still needs institutional authentication,
  per-user authorization, managed secrets, durable database/object storage, backups,
  monitoring, incident handling, and an approved data-retention policy.

## 2026-08-30 spent-regression result

Workbooks 1 and 3 were rerun as fresh jobs after the corroboration, row-coverage and final
semantic-verification redesigns. They are spent regression material, not new held-out
evidence, but they answer whether the failures that motivated the redesign still occur.

| | Workbook 1 | Workbook 3 |
|---|---:|---:|
| Keyed corrections | 13/13 | 10/10 |
| Unauthorized changed cells | 0 | 0 |
| Changed clean controls | 0 | 0 |
| Physical CLI calls | 76 (10 failed/retried) | 92 (11 failed/retried) |
| Final graded-row markers | 15/15 | 15/15 |
| Source hash changed | no | no |

The combined saved outputs therefore score **23/23 keyed corrections with no unexpected
edit**. Workbook 1 finished `succeeded`. Workbook 3 finished
`needs_human_attention` because two agents independently invented the same unsupported
policy that a plain exact fraction must be typed `numeric`; the patch gate rejected both
proposals and the clean cell was unchanged. The standing prompt rules now state that plain
fractions and constants are not defects merely because an agent prefers another label.

One warning in each saved report was also diagnosed as coverage-parser noise rather than a
workbook defect: a hint-row commentary record in Workbook 1, and a full probability
calculation ending in `=5/18` in Workbook 3. The parser now limits answer contradictions to
graded rows and compares a calculation's final result before stripping explanatory text.

This is strong regression evidence for the local prototype, but it does **not** establish an
unbiased 100% accuracy rate. These workbooks have shaped the system repeatedly. It also
exposes the operational blocker clearly: 21 of 168 physical CLI invocations failed and had
to be retried, and an earlier Workbook 1 attempt exhausted the Pro allowance after 96
calls. A personal subscription CLI is not a production capacity or reliability result.

## Evidence required before a deployment claim

1. Freeze the exact model identifier, effort, batch size, prompt versions, and pipeline
   contract intended for the evaluation. Do not use a moving alias in the results table.
2. Run the previously failing controlled workbook once as a regression check.
3. Run fresh labelled workbooks that were not used to design prompts or rules. Keep their
   answer keys hidden from the council.
4. Report, separately: defect recall, repair precision, unexpected changed cells, clean
   problems changed, human-escalation rate, calls, tokens, elapsed time, and provider cost.
5. Have a curator manually judge mathematically equivalent alternatives and instructional
   quality; an exact-cell key alone cannot decide whether a hint teaches well.
6. Preserve the source, output, key, settings, reports, and call audit for every scored run.
7. Only after the accuracy bar is agreed and met, implement the organization-authorized
   provider behind the existing `LLMClient` boundary and repeat the same evaluation.

The fresh suite is under `outputs/deployment-heldout-suite-20260819/`. Upload only each
`.xlsx`; do not place its file from `evaluation-keys/` in the UI or custom prompt. Score a
downloaded result with:

```bash
.venv/bin/python scripts/evaluate_controlled_run.py SOURCE.xlsx CORRECTED.xlsx KEY.json --summary
```

The evaluator accepts explicit alternatives, mathematical equivalence, and narrow
invariants such as “the exact multiple-choice answer appears once with no equivalent
distractor.” It does not force one arbitrary wording when several repairs are valid.

The first regression run tests whether known failures were repaired. The fresh held-out run
is the evidence for a pitch; repeatedly running a workbook whose failures shaped the system
is not an unbiased accuracy measurement.

**As of 2026-08-28 the `20260819` suite is spent as held-out evidence.** Three of its four
workbooks have been run live (five jobs, recorded in `jobs/council.db`), and their failures
drove architecture changes, so they are now regression tests by the paragraph above. Only
`heldout-04` remains unseen. A pitch needs a set built after the architecture stops moving —
building it before then just spends the next one the same way.
