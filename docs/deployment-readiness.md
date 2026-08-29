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
- Every problem is inspected by the Initial Auditor and again by the independent sweep.
- Every unsupported model finding is audited from scratch by the other audit role, without
  exposing the original accusation; exact target-and-category agreement is required before
  the Writer may edit. Reviewers then judge the simulated candidate separately.
- A Writer proposal is simulated and reviewed before it can change workbook bytes.
- Exact target cells, before-values, allowed scope, deterministic regressions, and final
  source-to-output differences are gated in Python.
- Prompt versions, composed prompt hashes, JSON schemas, provider behavior, and the
  Python-side pipeline contract (currently version 3) are pinned per job.
- Each physical model invocation has a durable audit row and a pre-call budget charge.
- Worker replacement runs recovery before resuming any durable phase.
- Upload size, job steps, repair attempts, model calls, provider failures, process output,
  process time, run time, concurrency, and retention are bounded.
- The local API supports bearer authentication; the browser proxy keeps that token out of
  client JavaScript.
- A clean installed wheel is checked for current prompts and retired provider code.
- Correct mathematical restatements in an Answer cell are protected from normalization:
  equation-to-value and named-evaluation-to-value rewrites are rejected when they are
  provably equivalent, while genuinely different answers remain repairable.
- Workbook notation detection uses positive notation evidence. Plain numbers and bare
  graded equations are neutral, so a LaTeX workbook with ordinary numeric answers is not
  misclassified as mixed.

The exact model disclosure is in `docs/llm-data-contract.md`.

## What is not established yet

- **Held-out semantic accuracy.** Historical corrected files contain missed keyed cells
  and unexpected edits. Architecture fixes address several causes, but old files cannot
  prove the new pipeline's accuracy. Four fresh, unused workbooks now provide 60 problems,
  38 defect groups, 52 automated checks and 22 clean controls; they have not been sent to a
  live model, so they are evaluation material rather than an accuracy result.
- **Organization provider choice.** Console API, Bedrock, Vertex AI, or an approved gateway
  is a Berkeley decision involving billing, identity, retention, and procurement.
- **Multi-user boundaries.** The current SQLite/data-directory design, shared bearer token,
  and latest-job-only page are suitable for one trusted local curator, not an organization.
- **API cost.** Subscription calls do not establish an API budget. Cost needs token usage
  from a run made through the provider and model the organization intends to purchase.
- **Operational controls.** Central deployment still needs institutional authentication,
  per-user authorization, managed secrets, durable database/object storage, backups,
  monitoring, incident handling, and an approved data-retention policy.

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
