# LLM data contract

This document is the auditable answer to: **what exactly leaves the service for Claude,
when, and why?** It describes pipeline contract version 18.

## Transport

Every model call starts one local `claude` process. The service does **not** upload the
`.xlsx` file and does not give Claude filesystem access. It sends:

1. a role-specific system prompt through `--system-prompt`;
2. one rendered user payload on stdin; and
3. one JSON Schema through `--json-schema`.

The command also sets `--print --output-format json --model <configured model> --effort
<configured default or per-role effort> --tools "" --max-turns <configured limit> --safe-mode
--disable-slash-commands --strict-mcp-config --mcp-config '{"mcpServers":{}}'
--permission-mode dontAsk --no-session-persistence`.

The child runs in a fresh empty temporary directory. It receives only this environment
allowlist: `HOME`, `PATH`, `USER`, `LOGNAME`, `SHELL`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ`,
`XPC_SERVICE_NAME`, and `__CF_USER_TEXT_ENCODING`. API-key and alternate-provider
variables are removed. `HOME` and the two macOS session variables allow the CLI to use
the subscription login stored in the user's keychain.

## System prompt sent on every call

The system prompt is the pinned role file in `src/oatutor_council/prompts/`, with two
shared files composed into it:

- `_shared/untrusted_data.md`: workbook/document text is fenced data, never instruction;
- `_shared/curation_rules.v3.md`: the current standing OATutor formatting and content
  rules. Earlier versions remain packaged so earlier prompt hashes stay explainable.

Each job pins both the role prompt version and the SHA-256 of the fully composed text.
The runner now enforces the hash. Editing a prompt or shared fragment without creating a
new role prompt version stops an in-flight job instead of silently changing its rules.

Each agent module also contributes a short task preamble (`INSTRUCTIONS`, or the batched
equivalent). That text is the first part of the **user payload**, not the system prompt.
It is therefore covered by the per-call audit hash and by `PIPELINE_CONTRACT_VERSION`, not
by the role prompt's version number. The remainder of that payload is the fenced sections
listed below.

## Workbook representation

Claude sees problem blocks as text tables with real 1-based spreadsheet row numbers and
canonical column names. Every fixed A-P field is included:

`problem_name`, `row_type`, `title`, `body_text`, `answer`, `answer_type`, `hint_id`,
`dependency`, `mc_choices`, `images`, `parent`, `oer_src`, `openstax_kc`, `kc`,
`taxonomy`, and `license`.

The `images` field is only the cell's textual value/reference. The service does not fetch,
render, inspect, or OCR an image. It also does not send styles, colors, borders, row
heights, column widths, the workbook filename/path, other worksheets, or the optional
`Validator Check` and `Time Last Checked` columns. Those are handled deterministically.

Every data section uses a fresh random fence token, and fence-looking text inside cells
or documents is neutralized before transmission.

## Data sent by role

### Initial Auditor

Sent once for every block (or bounded block batch), before semantic repairs:

- the current block table;
- derived workbook conventions: notation, dependency numbering, scaffold namespace, and
  problem-name stems;
- deterministic findings already found for that block;
- applicable errata claims, including their document provenance and segment index;
- curator policy rules; and
- curator background notes.

Errata are hypotheses to confirm/refute. Rules are policy. Background is context only and
cannot itself establish a defect. The response schema contains private `reasoning`, public
findings (every exact repair-target `cell` pair, `problem`, `expected`, `severity`,
`category`, optional `confirms_claim`), and explicit claim refutations. A finding with no
target cell cannot satisfy the schema; one containing any target outside its assigned
block is discarded and that block is requeued rather than silently changing the repair.

### Writer

For one ready issue, sent once per repair attempt:

- the one issue to resolve, including authorized cells and expected result;
- the complete current block;
- derived conventions;
- current deterministic findings for that block;
- curator policy rules; and
- actionable feedback from the previous reviewer or deterministic gate, if any.

The response schema contains exact cell edits (`row`, canonical `column`, character-exact
`before`, `after`), `related_edits_reason`, optional human escalation, and private
`reasoning`, `derivation`, and `confidence`.

When at least two ready issues belong to one block, the production path sends those issue
records, their per-issue prior feedback, and the shared current block in **one** Writer
call, bounded by `REPAIR_BATCH_SIZE`. The response must return each supplied opaque
`issue_id` exactly once. Every result is immediately split into an ordinary per-issue
attempt, patch, deterministic gate result, and private record. Two results cannot own the
same cell; that ambiguity is rejected before review or application.
Issues whose authorized targets share a graded row remain sequential because they commonly
describe one coupled repair. After the first patch is accepted, the rule engine rechecks
the sibling before another Writer call is allowed.

### Known-Issue Reviewer

For a new proposal, the service simulates the patch in memory and sends:

- the issue under review;
- the source block as submitted;
- the complete simulated candidate block;
- the whole source-to-candidate block diff;
- the candidate's exact before/after cells;
- derived conventions;
- deterministic findings on the simulated candidate; and
- curator policy rules.

The Writer's reasoning, derivation, and confidence are not sent. `accept` authorizes the
candidate to be written; `revise` or `human_review` leaves the workbook untouched.
The response schema contains `decision`, actionable `feedback`, and any rule codes used.
When two or more candidates from one block are ready together, the service sends their
public issue records and exact edits alongside one combined simulated block and asks once.
The reviewer must return each supplied `issue_id` exactly once; the service persists an
independent verdict for every candidate. The block call never contains Writer reasoning,
derivation, or confidence. All verdicts from that physical response commit in one database
transaction and are reused after a crash before any replacement review is requested.

Every model-only finding first receives a **claim-blind corroboration pass**, before the
Writer is called. The second agent receives the current block, derived conventions,
deterministic findings and curator policy, but it does **not** receive the first agent's
issue description, target cells, explanation, proposed replacement or private reasoning.
It audits the block from scratch using the opposite audit role. A claim is corroborated
only when the second agent independently reports the same exact target cells and the same
issue category. No prose-similarity matching is used. An unusable result escalates to human
review. This prevents one plausible accusation from anchoring the agent that is supposed to
verify it.

**A result that is neither exact agreement nor an unusable audit refutes nothing.** If the
second audit named any cell on a disputed row it is a disagreement about extent and goes
to the **Adjudicator**; if it said nothing about those rows it is silence and goes directly
to a curator as `UNCONFIRMED`.

Both scan roles must additionally return one **coverage record per graded row** of every
block they are sent — the row's derived answer, its recorded answer, and whether the answer,
its type, its requested form, its domain, its solution count, its units and its choice list
were checked. A response short of its graded rows is scanned again rather than accepted; a
row nothing ever accounts for is recorded and denies the job success. No workbook content
leaves the service to make this happen: it is a field on the same response.

### Final Semantic Verifier

Runs after every repair and before deterministic validation, but only for a block whose
net content differs from the uploaded workbook. An untouched block already has a complete
Independent Reviewer pass over the same bytes. It receives the problem block as it now
stands, derived conventions, curator policy, and the literal source-to-current cell diff.
It receives no deterministic findings, issue descriptions, verdicts, or agent reasoning.

It returns findings with exact cells and the same mandatory per-graded-row coverage
records. It never edits: its findings go through claim-blind corroboration like any other
model claim. A block's verification is discarded whenever a repair is applied to it, and
the block is verified again.

Several unrelated changed blocks may share one physical final-verifier call. Each is
labelled by a fresh opaque `batch_item_id` and must return exactly one attributable result.
Missing, duplicated, unknown, and cross-block results do not certify their block and count
against that block's ordinary bounded verification rounds.

### Adjudicator

The Adjudicator is used only when two claim-blind audits report related defects on the same
row and disagree about the exact repair target. Silence is not a second conclusion: a
claim the blind audit does not reproduce goes directly to a curator as `UNCONFIRMED`, with
no adjudication call. When used, the Adjudicator receives both published findings, the
current block, derived conventions, deterministic findings and curator policy. It does
**not** receive any agent's private reasoning: its
context type cannot name a private model, checked at import, and the taint registry checks
the rendered bytes on top of that.

It returns one of three verdicts with the check it ran:

- `defect_confirmed` — the repair proceeds, and its cell list, category and structural
  classification replace the disputed claim's.
- `content_correct` — the claim is refuted. This is the only route from a model-only claim
  to a refutation, and it requires stated reasoning. That reasoning is recorded and can be
  read; it is not a proof, and an adjudicator that reasons wrongly can still refute a real
  defect. What the requirement rules out is refutation by silence.
- `undecided` — the issue ends `unconfirmed`. Nothing is edited, no attempt is spent, and
  the job cannot report success.

A verdict returned with no evidence, and a confirmation naming no cell inside the disputed
block, are both downgraded to `undecided` before anything acts on them.

When an already-accepted sibling repair may have resolved a second issue, the normal
simulated-candidate review path is used. A changed target is then marked superseded rather
than refuted.

### Independent Reviewer

After known repairs, it receives **every current block**, including blocks with prior
findings or repairs. For a fresh sweep it gets the current block, derived conventions,
current deterministic findings, and curator policy rules. It does not receive errata,
background notes, the Initial Auditor's reasoning, or the previous issue ledger. Its
finding schema names every coordinated repair target.

The fresh-sweep response also states `block_is_sound`. Findings take precedence over a
contradictory `true` value. In a batch, every dispatched opaque `batch_item_id` must appear
exactly once; missing, duplicate, unknown, or misattributed results requeue the affected
block instead of crediting it as reviewed.

When reviewing a correction to one of its own findings, it uses the simulated-candidate
context above with the Independent Reviewer system prompt.

## Custom-text modes

- **Work it out per paragraph (`auto`)**: each passage is deterministically classified as
  errata, policy, or background, then routed as below.
- **Specific problems that are wrong (`errata`)**: sent only to the Initial Auditor as
  hypotheses, narrowed to named problem names/rows when possible.
- **Standing rules (`rules`)**: additional policy sent to the Initial Auditor, Writer,
  and both reviewers. It never replaces the built-in rules.
- **Background (`notes`)**: sent only to the Initial Auditor as non-authoritative context.

The document text is always fenced as untrusted data. Selecting `rules` changes its role
in the curation task; it does not let the text override the system prompt or response
schema. Instruction ingestion accepts at most 200,000 extracted characters and reports
when that document-wide bound truncates a file. There is no smaller hidden routing cap:
every accepted rule or background segment is sent to the role listed above.

## What is recorded

For every physical CLI invocation, `llm_calls` records role, model, status, prompt
version, behavior settings, usage, latency, output size, and a hash over the exact system
prompt + user payload + canonical JSON Schema. It records system/payload text up to
200,000 characters and marks truncation; the hash still covers the full transmitted
values. Failed invocations are recorded too.

Structured public artifacts are persisted in findings, patches, and verdict tables.
Auditor and Writer private fields are stored separately in `private_blobs`. Raw response
envelopes and raw model response text are not persisted.

## Explicitly not sent

No API key, `.env` contents, local path, source filename, job database, prior conversation,
Claude session history, Writer rationale to a reviewer, workbook binary, or image pixels
are sent. `LLMRequest.seed` is audit-only metadata: the Claude CLI has no seed flag, so it
is not transmitted and is not a determinism guarantee.
