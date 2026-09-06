# OATutor Curation Council

Autonomous curation of OATutor/OpenStax mathematics problem workbooks.

A curator uploads one `.xlsx`, optionally attaches a text instruction document, and starts
**one** job. They get back a corrected workbook, a complete issue ledger, a cell-level
change log, every reviewer decision and repair attempt, and a final validation report.

The system finds issues autonomously even when the curator supplies no problem name, cell,
or correction. Optional notes can also point it at a known defect or add workbook-specific
policy.

```
XLSX (required) + optional instruction document (.pdf/.docx/.txt/.md)
  → Initial Auditor          scans every block, accounting for every graded row
  → Claim Reviewer           audits the block again, blind to the claim
  → Adjudicator              reconciles two related findings on the same row
     or curator              receives a claim the blind audit did not reproduce
  → Writer                   corrects confirmed issues
  → Known-Issue Reviewer     checks simulated corrections before they are written
  → repair loop              bounded at three attempts per issue
  → Independent Reviewer     rechecks every current problem from scratch
  → repair loop
  → Final Semantic Verifier  re-solves graded rows in blocks whose content changed
  → final deterministic validation
  → corrected XLSX + reports
```

---

## The idea

**The LLM does semantic judgment. Deterministic Python does everything mechanical and
safety-critical.**

The model decides what is mathematically wrong, what the correction should be, whether a
correction is right, and what feedback to give. Python parses the workbook, simulates exact
cell edits, enforces the rules, checks edit scope, writes only reviewer-approved patches,
compares source against output, manages retries, and generates every report.

If you find yourself asking a model to do something a function could do exactly, that is a
design error. Nothing about a specific problem, cell, or correction is hardcoded anywhere.

---

## Quick start

```bash
# Environment (Python 3.12)
python3.12 -m venv .venv
uv pip install --python .venv -e ".[dev]"

# Full test suite: synthetic workbooks only, no credentials, no network
.venv/bin/python -m pytest tests/ -q

# See the whole pipeline run offline, with no API key
.venv/bin/python scripts/demo.py
```

The demo generates a synthetic workbook with four planted defects — a scaffold missing its
answer, a fraction Excel coerced into a date, a multiple-choice answer matching no option,
and a dangling dependency — then runs the complete council against a scripted model client
and prints every artefact.

### Running the service

```bash
claude auth login                    # once; this app never sees your password or token
.venv/bin/uvicorn "oatutor_council.api:create_app" --factory --app-dir src \
  --port 8000 --workers 1

# Verify the provider with one live call carrying no workbook content
.venv/bin/python scripts/smoke_claude_cli.py
```

**There is no API key.** The provider is the Claude Code CLI running on your Claude
subscription, invoked as a subprocess. The service checks at startup that the executable
exists, that the CLI is logged in, and that the login is a *subscription* rather than an
API credential — and refuses to start otherwise, rather than accepting uploads it can
never process. An inherited `ANTHROPIC_API_KEY` cannot silently switch it to metered
billing: the child environment is an allowlist and drops that variable on the way in.

`GET /health` is liveness; `GET /readyz` answers the different and more useful question of
whether a job submitted right now could actually run — provider configured, executable
present, CLI authenticated, database reachable, worker pool and poller up. It never reports
the account's email, organisation or any filesystem path.

Set `API_TOKEN` and every endpoint except `/health` requires `Authorization: Bearer …`.
Leaving it unset leaves the service open, which is a reasonable configuration behind a
proxy that authenticates on its behalf and a serious mistake anywhere else — so the
process says which it is at startup and `/readyz` reports it. Liveness stays open on
purpose: a probe that needs a secret reports the process as dead whenever the secret is
wrong.

Finished jobs are deleted after `RETENTION_DAYS` (30 by default), files first and then
rows. `RETENTION_DAYS=0` keeps everything, which should be a decision somebody makes
rather than a default they inherit.

**One worker process.** The lease protocol itself is safe across processes — epoch fencing
and `BEGIN IMMEDIATE` are not in-process locks — but SQLite in WAL mode over a single file
wants one writer, and the working copies live on a local filesystem. Concurrency within the
process is `MAX_CONCURRENT_JOBS`. Scaling horizontally means a shared database and shared
storage, which is a different design rather than a flag.

Jobs survive the process that started them. A worker renews its lease from a background
thread, so a model call that outlasts the lease does not lose the job; if the process dies,
the lease expires and a poller in the next process picks the job up — on startup and then
every `POLL_INTERVAL_SECONDS`. Shutdown asks running jobs to stop at a step boundary and
releases their leases, so a restart resumes immediately instead of waiting the lease out.

```bash
# Submit
curl -s -X POST localhost:8000/jobs \
  -F workbook=@section-7-4.xlsx \
  -F instructions=@errata.md
# {"job_id":"9f2c...","state":"created","seed_claims":3}

# Poll — never blocks on an in-flight write
curl -s localhost:8000/jobs/9f2c...

# Artefacts. `/report` is rebuilt from the same rows the job wrote, so it always
# agrees with the report.md in the download.
curl -s  localhost:8000/jobs/9f2c.../issues
curl -s  localhost:8000/jobs/9f2c.../changes
curl -s  localhost:8000/jobs/9f2c.../reviews
curl -s  localhost:8000/jobs/9f2c.../report
curl -sO localhost:8000/jobs/9f2c.../download

# Resume an interrupted job (409 if finished or already running)
curl -s -X POST localhost:8000/jobs/9f2c.../resume
```

---

## Web interface

`web/` is a Next.js page for curators who would rather not use `curl`: choose a workbook,
add anything specific about *this* one, watch the phases, download the result.

```bash
./scripts/serve.sh          # council on :8000, page on :3000, browser opened
./scripts/serve.sh --api-only   # just the service
```

**It runs on your machine, and that is the architecture rather than a limitation.** The
council shells out to the Claude Code CLI under your subscription login, kept in this
machine's keychain; there is no API key anywhere in this application, deliberately. Hosting
the work elsewhere would mean introducing one and converting a subscription into metered
billing. The service also wants a writable filesystem and runs one job for minutes under a
renewed lease, so the compute has to be where the login is — and once it is, a remote host
has nothing left to do.

The browser still never talks to the service directly: every call goes through a Next.js
route handler, so `COUNCIL_API_TOKEN` stays out of the JavaScript bundle and there is no
CORS to configure. Typed instructions are uploaded as an **instruction document**, so they
go through the same `RULES | ERRATA | NOTES` classification and the same untrusted-data
fencing as an attached file. The agents' own prompts stay in the service and are never
served to the page.

What is left over is reported in two figures, never one: **open issues** (anything still
repairable, plus non-repairable errors that need a person) and **observations**
(non-repairable warnings/observations, like a correct answer that happens to be listed
first, a valid house-style namespace, or an optional column this workbook does not use).
They are counted apart
because a single total makes a finished workbook look unfinished, and a curator who cannot
trust the summary has to re-check the file by hand, which is the whole job they came here to
avoid.

`web/README.md` has the rest.

---

## Architecture

```
src/oatutor_council/
  api.py           HTTP surface, upload validation, job runner
  council.py       the five-stage loop; one step = one bounded durable unit
  orchestrator.py  crash-safe apply, recovery after a crash
  state_machine.py job and issue transition tables, attempt accounting
  persistence.py   SQLite: WAL, synchronous=FULL, BEGIN IMMEDIATE, epoch fencing
  models.py        the vocabulary every layer speaks
  uploads.py       magic bytes, archive inspection, path containment
  config.py        every setting, read from the environment in one place
  workbook/        reader · writer · diff · styles
  validation/      rules/ (63 rules) · mathematics · patch_gate · final_gate
  agents/          initial_auditor · writer · known_issue_reviewer ·
                   independent_reviewer · adjudicator · final_verifier ·
                   coverage · isolation · rendering · schemas · batching
  llm/             base · claude_cli · mock · context · prompts · audit
  ingestion/       instruction_documents
  reporting/       ledger · reports
  workers.py       leases, heartbeats, the worker pool, the poller, retention
  prompts/         versioned agent prompts, plus the shared policies (package data)
scripts/           demo.py · evaluate_workbooks.py · shadow_run.py ·
                   smoke_claude_cli.py · recon_workbooks.py
```

The prompts ship **inside** the package. They started outside it, on the reasoning that
they are content rather than code — which does not survive a wheel install, where the
directory beside the source tree is site-packages and every agent raises on its first
call. `OATUTOR_PROMPT_ROOT` overrides the location for anyone iterating on wording.
`scripts/verify_wheel.py dist/*.whl` also refuses a release artifact that omits the newest
prompts, declares a retired SDK, or resurrects the deleted provider module from a stale
local `build/` directory; CI runs it before installing the wheel.

### The guarantees, and how each is enforced

**The source workbook is never modified.** Four independent defences: a `SourcePath`
NewType no write-capable function accepts, `chmod 0444`, an assertion that the target is
not the source *by resolved path and by inode* (a hard link defeats a path comparison),
and hash re-verification at every resume and at finalisation.

**Success is never falsely reported.** `SUCCEEDED` is set in exactly one place, guarded by
every gate — and the state machine table has exactly one inbound edge to it, which a test
asserts by enumeration. Finalisation writes the outputs *before* evaluating the gates, so a
job that needs a person still hands over the corrected workbook and a report saying plainly
what is unresolved.

**Every change is accounted for.** The source-to-output diff compares cell values,
formulas and types, number formats, fonts, fills, borders, alignment, row heights, column
widths, merged ranges, hidden rows and columns, sheet names, order and visibility, freeze
panes, data validations, hyperlinks, and images. Any difference not traceable to an
accepted edit or to the approved edited-row rule **fails the gate**. There is no blanket
"formatting normalisation" category, because that bucket would absorb real damage.

**Rejected proposals cannot leak into the download.** A Writer patch is simulated in
memory. The reviewer sees the full simulated block, source-to-candidate diff, and exact
candidate cells before deciding. `revise` and `human_review` write nothing; only `accept`
creates an approved patch that the next crash-safe step may apply. A rollback path exists
only to resume jobs created under the former apply-before-review lifecycle. The UI's
**cells changed** metric counts distinct source-to-output cell differences.

**Unsupported findings do not authorize edits by themselves.** A deterministic finding
is backed by its registered rule. For a model-only finding, the other audit role examines
the current block from scratch without seeing the original accusation, target cells,
explanation, or proposed replacement. Only an independently rediscovered defect at the
same exact cells and in the same category reaches the Writer. This separates “is the source
wrong?” from the later, independently recorded question “is this candidate correction
right?” and prevents a plausible accusation from anchoring both judgments.

**Mechanical cleanup does not spend model calls.** Boundary whitespace is trimmed exactly,
duplicate metadata on non-problem rows is cleared when the problem row already carries it,
and an explicit variable equation labelled `numeric` is relabelled `algebra`. These narrow
repairs still pass the ordinary patch gate and are recorded like any other change.

**Reviewers never see the Writer's reasoning.** Enforced three ways: `PrivateText` is not a
`str` subclass (a subclass interpolates silently into an f-string); `ReviewerContext` is
checked at import time and cannot reference a private type anywhere in its field closure;
and a `TaintRegistry` compares every outgoing payload against registered private text by
exact match *and* 12-token shingle, catching the paraphrase that exact matching misses. A
violation fails the job — never a warning, never a retry. The registry is rebuilt from the
database on every resume: one that lived only in the worker would be a guarantee that ended
at the first crash, since the reasoning it was watching for is still on the patch.

Text that is public **by provenance** — the workbook, the curator's document, the
deterministic findings — is subtracted before that comparison, and the check is worthless
without it. A derivation quotes the cells it reasons about; the reviewer is shown those same
cells because they are what it is judging; twelve consecutive tokens of shared mathematics is
the ordinary case rather than evidence of a leak. Reading it as one killed a live job whose
first repair was correct. What is never public ground is the outgoing payload itself, which
would subtract everything from everything and leave a check that cannot fail.

**Every model call is written down.** One row per physical invocation — role, model,
status, a hash over the complete system prompt + user payload + JSON response schema,
pinned prompt version, latency, token usage, and prompt text up to the documented audit
cap — including failed calls with the provider's own status. The recording is a wrapper around
the client rather than a call inside each agent, so no agent can forget it. Prompt versions
and composed prompt hashes are enforced per job, and a separate pipeline-contract pin
covers Python-side payload and orchestration changes. Deploying mid-job therefore cannot
silently run later attempts under a different contract. Production launches one isolated
Claude Code CLI process per physical call through the local subscription login; the
application has no API key or SDK fallback.

**Workbook cells are hostile input.** They reach every agent inside fenced, labelled data
sections with a **per-call random delimiter**, and anything resembling a fence is
neutralised. Every system prompt states that fenced text is content to analyse and never
instruction to follow — composed in from one shared file, so it cannot drift out of one
prompt while staying in the other three.

**A crash loses nothing and applies nothing twice.** Intent → file → commit: a durable
intent row before any byte is written, an atomic `os.replace` from a temp file inside the
job directory, then the commit. Recovery decides roll-forward versus re-apply by **reading
the target cells**, never by hashing the file — openpyxl output is not byte-reproducible,
so the expected hash could never be computed in advance. A partially-applied patch is
impossible under `os.replace` and therefore means external corruption. Recovery runs on
the first step of every replacement worker, regardless of which durable phase it resumes.

**The job always terminates.** Three independent brakes: three attempts per issue
(reserved *before* the model call, so a crash loop cannot burn unbounded spend against a
counter that never moves), a validation-round budget on the single cyclic edge in the
state machine, and global step and model-call fuses. Plus fingerprint dedup backed by a
unique database index: a rediscovered defect cannot open a second issue with a second
budget, however many code paths try.

---

## Workbook contract

Single sheet. Columns A–T:

| A | B | C | D | E | F | G | H | I | J |
|---|---|---|---|---|---|---|---|---|---|
| Problem Name | Row Type | Title | Body Text | Answer | answerType | HintID / Scaffold ID | Dependency | mcChoices | Images |

`K` Parent · `L` OER src · `M` openstax KC · `N` KC · `O` Taxonomy · `P` License ·
`S` Validator Check · `T` Time Last Checked.

Row types: `problem`, `step`, `hint`, `scaffold`. `answerType`: `numeric`, `algebra`, `mc`.
`mcChoices` is pipe-delimited, 2–5 choices, and `Answer` must match one **exactly**.

**Block boundaries are driven by `Row Type == "problem"`, not by runs of identical
`Problem Name`.** A block starts at a problem row and runs to the next problem row.
`Problem Name` is then a *validation* signal, and when the two disagree the reader emits a
structural finding rather than silently picking the convenient reading — because that is
exactly what would hide a column-shift corruption.

### What real workbooks actually do

The contract above is what the specification says. Eleven real workbooks say more, and
several corrections in this codebase exist because of it:

- **The A–T contract is incomplete.** Every real file carries undocumented tooling columns
  out to column 25 (`Debug Link`, `Problem ID`, `Lesson ID`, `Image Checksum`). Nothing
  curates them; everything preserves them.
- **Trailing columns move.** `Validator Check` appears at column 18, 19 or 20, duplicated
  in two files, and one workbook pushes `Time Last Checked` to column 21. They are resolved
  by header name across the full width, never by index.
- **A row is blank only when every column is empty.** Three workbooks put the OATutor
  validator's own output on row 2, in columns the contract never describes.
- **The `h` scaffold namespace is the majority**, not an outlier — six of eleven files.
  A workbook-wide consistent alternative is a house style and downgrades to a warning; a
  workbook that *mixes* namespaces keeps the error.
- **Two structurally different shift corruptions exist**, and a repair for one applied to
  the other would destroy data.

`.venv/bin/python scripts/evaluate_workbooks.py` runs the whole deterministic core over a
corpus read-only, hashing every file before and after, and reports what it finds.

---

## Provider

The **Claude Code CLI** on your subscription, one `claude --print` process per call, model
`sonnet` by default. Structured outputs with Pydantic schemas for every agent response, and
a scripted mock client that drives the entire council offline.

`docs/claude-cli-notes.md` records the exact flags, verified against the installed binary.
The properties that matter:

- **The payload goes on stdin**, never as an argument (arguments are visible in `ps` to
  every user on the machine) and never through the environment or a temporary file.
- **`--tools ""`** disables every built-in tool. The empty string is a real argument;
  omitting it enables the lot, and an agent that can read the filesystem is not an agent
  that reads the block it was given.
- **A fresh process in a fresh empty directory per call**, with no session persistence — so
  context isolation between agents is structural rather than disciplinary.
- **A restricted child environment** built by allowlist, dropping every credential variable.
- **A timeout kills the whole process group**, because the CLI spawns helpers that would
  otherwise outlive it.
- **`--max-turns 4`** (`COUNCIL_CLAUDE_MAX_TURNS`), a ceiling the CLI enforces rather than one
  inferred from having no tools. It rose from 1 to 2 after a live run lost four calls at turn 1,
  then from 2 to 3 after a 30-block real workbook lost eight more calls at turn 2. In both cases
  the model had produced another turn but the adapter discarded it and paid to retry the whole
  prompt, so the lower ceiling increased rather than bounded spend. A fresh contract-11 run
  then lost two of its first four completed audit invocations at turn 3, so the measured
  ceiling is now 4.

Settings are prefixed `COUNCIL_` because `CLAUDE_EFFORT` is a variable the CLI itself sets:
unprefixed, the service would inherit an effort level from whatever session launched it.
Optional per-role overrides use `COUNCIL_INITIAL_AUDITOR_EFFORT`,
`COUNCIL_WRITER_EFFORT`, `COUNCIL_KNOWN_ISSUE_REVIEWER_EFFORT`, and
`COUNCIL_INDEPENDENT_REVIEWER_EFFORT`; they are validated at startup and pinned per job.

**The adapter is versioned, and the version is pinned per job.** `CLI_ADAPTER_VERSION` decides
what flags every call carries, so a job that started under one set and finished under another
finished under instructions its first half never saw — and its own record would say otherwise.
A job whose pin disagrees with the running process fails as `FAILED(CONFIG)` on resume rather
than continuing. Two practical consequences: changing anything in `build_command` means bumping
the constant, and bumping it means in-flight jobs must be resubmitted rather than resumed.

Failures are classified rather than lumped together, because they need opposite handling:

| Failure | Handling |
| --- | --- |
| 429, 5xx, timeout, transport | Retried with exponential backoff, honouring `Retry-After` where the server sends one and capping it where it exceeds our patience |
| Not logged in, unknown model, malformed request | Never retried — it fails identically until something changes. `FAILED(CONFIG)`, non-resumable, and the message says to run `claude auth login` |
| Subscription usage limit | Never retried: an allowance is not a burst. Fails the job **immediately** rather than walking the failure budget down, and stays resumable. A reset time is kept only if the provider states one, never guessed |
| Safety block, recitation | Never retried either: the same cell trips the same filter every time. That one issue goes to a person and the job carries on |
| Anything unrecognised | Treated as transient — the conservative reading, since a bounded few retries costs less than failing a job that would have worked |

Every call carries a timeout. Lost calls are counted against a per-job budget stored in the
database rather than in the worker, because a provider outage routinely takes the worker
with it and an in-memory counter would reset exactly when it mattered. Persisted error
messages are bounded at both ends and stripped of anything credential-shaped: a provider
can quote your workbook back at you in an error, and errors end up in logs.

### Scan batching

`SCAN_BATCH_SIZE` (default **2**) controls how many problem blocks one Initial Auditor or
Independent Reviewer call examines. At 1 the code delegates to the unchanged single-block
path — same prompt, same schema, same payload, same recorded prompt hash — which remains
useful for diagnostics and A/B evaluation. The production default is 2; a four-problem
live batch on realistic OpenStax material exceeded the former 120-second CLI ceiling, so
the process timeout is five minutes; the rendered-size ceiling can still split an
unusually large pair earlier.

Above 1 the response is per block, keyed by an opaque id generated for that call. That is
not decoration: **a block the model omitted is indistinguishable from a block it examined
and found clean**, and marking the first done would report a workbook as reviewed when
nothing looked at it. So a block is marked done only when its own result is present and
valid; missing, duplicated and unknown ids all send the block back to the queue. Each
semantic finding names exact `(row, column)` target pairs—never separate arrays whose
Cartesian product can authorize unintended cells—and a finding with any target outside
its assigned block is discarded and that block is requeued rather than credited.

The same bounded envelope is reused for claim-blind corroboration: one Independent
Reviewer call may examine one unresolved model claim in each of several unrelated blocks.
The accusations are still absent from the payload. At most one claim per block is
prefetched, and a block with another live issue stays sequential so an accepted edit can
never make a cached blind verdict describe stale bytes.

Final semantic verification is batched by the same count and rendered-size limits. Every
changed block retains its own coverage, findings, round counter, and completion marker.
An omitted, duplicated, or cross-block result consumes that block's bounded round and is
requeued; it is never treated as a clean certification.

### Block repair and review batching

`REPAIR_BATCH_SIZE` (default **8**) controls how many confirmed issues from one problem
block the Writer handles in one coordinated response. Each issue still receives its own
attempt, patch, deterministic gate result, reviewer verdict, and terminal ledger state.
The optimisation removes repeated model envelopes; it does not merge accountability.

When at least two candidate patches are ready in a block, the reviewer receives one
simulated whole-block diff and returns one decision per issue in a single call. Omitted,
duplicated, or invented issue identifiers invalidate the response. Two proposals may not
claim the same cell, and workbook bytes are still written only after review acceptance.
Set the value to 1 to retain the legacy one-issue call path.

Issues naming the same graded row are kept sequential. Answer/choice and Answer/type
findings often describe two views of one physical repair; forcing both into independent
proposals makes them compete for the same cell. The first accepted correction is applied,
then deterministic rules decide whether the sibling was resolved for free. Coordinated
review verdicts are committed atomically, so a crash cannot preserve half of one paid
review response and force the replacement worker to guess the missing half.

**There is no `Conversation` object anywhere in the codebase**, and a test greps the whole
package to keep it that way. Context isolation is not a discipline anyone has to remember;
there is simply no message list that could carry reasoning forward.

See `docs/claude-cli-notes.md` before touching `llm/claude_cli.py`. It records the flags
verified against the installed binary and the live smoke result that confirmed structured
output. The parser still fails loudly on an unknown envelope rather than guessing.

[`docs/llm-data-contract.md`](docs/llm-data-contract.md) enumerates every system prompt,
payload, schema, workbook field and environment value sent to the backend, plus everything
that is deliberately excluded. [`docs/pilot-failure-analysis.md`](docs/pilot-failure-analysis.md)
classifies the controlled-run failures as architecture versus prompt/model judgment.
[`docs/deployment-readiness.md`](docs/deployment-readiness.md) separates what the local
prototype proves from the work required for an organization-funded deployment.

---

## Out of scope

OCR, image recognition, PDF-page rendering, and screenshot interpretation. An optional PDF
has its **embedded text** extracted; a PDF with no text layer produces a clear validation
error naming the supported alternatives.

That last point is a requirement rather than a limitation: an unreadable document must
never be reported as a document containing no instructions. Those two outcomes are
indistinguishable downstream, and confusing them tells a curator who uploaded a scan that
their workbook is fine.

---

## Evaluation

Three layers, answering three different questions.

**The golden collection** (`tests/golden.py`) states, for each workbook, the verdict a
curator would give it — in a sentence — and asserts the engine reaches it. Half the cases
are *correct* material that must come back quiet, because a rule that flags good
mathematics is worse than a missing rule: it buries every true finding beside it. Several
of them encode measurements that changed the rules, such as `5*pi/6` being ordinary
notation rather than a missing parenthesis.

**`scripts/evaluate_workbooks.py`** runs the deterministic core over the real corpus,
read-only, hashing every file before and after.

**`scripts/evaluate_controlled_run.py`** compares a corrected synthetic pilot with its
source and hidden key. Exact keyed cells and unexpected changes are machine-scored;
expectations stated only in prose are surfaced for manual review rather than guessed into
a pass.

**`scripts/shadow_run.py`** runs the *whole council* over a **copy** of one real workbook
and prints what a real job would report. Offline by default — the scripted agents examine
and never edit — with `--live` as an explicit opt-in that says what it is about to send
before it sends it. `scripts/smoke_claude_cli.py` makes one live call with invented
arithmetic and no workbook content at all.

### What the controlled live pilot established (15 problems, 2026-08-15)

A 15-problem workbook — ten planted defects, five deliberately correct problems — run end to
end against live Claude on the subscription CLI. Every number below is from that run:

| | |
| --- | --- |
| Planted defects detected | **10 / 10** |
| Planted defects repaired | **10 / 10** under the evaluation key |
| Clean problems left untouched | **5 / 5** |
| Cells changed | **exactly 10**, all in expected locations |
| False-positive corrections | **none** |
| Downloaded workbook vs job output | identical |

The report showed **12 findings against 10 defects**: two defects each raised a pair of
related findings, and in both cases a single repair resolved both. That is the intended
behaviour rather than double-counting — the finding is what a rule saw, the issue is what
gets repaired, and the two are deliberately not the same number.

This run cleared the bar the previous pilot missed. That one found 10/10 and repaired only
5/10, because deterministic gate rejections were never returned to the Writer and semantic
duplicates could stay open after a sibling repair had already fixed them. Both are fixed,
and this pilot is the evidence that the fixes work against live Claude rather than only
against the scripted client.

**Two editorial reservations the evaluation key did not capture** were later promoted into
regression requirements. Both repairs passed the old deterministic gate and reviewer:

- A "Which fraction…" problem had its `Answer` changed from `3/4` to `0.75`. The mathematics
  is equivalent and the choice now matches exactly, which is all the gates and the key ask
  for. But the question asks for a *fraction*, so the correct repair was the other direction:
  keep `3/4` and fix the choice that read `0.75`. The gate now preserves fractional answers
  when the task explicitly requests an exact value or fraction.
- A new hint gave away the final answer (`5+5=10`) for a `2*5` problem. A hint that states the
  answer is a hint that does no work; "rewrite `2*5` as adding 5 two times" is the same repair
  done properly.

The first has a safe deterministic boundary; the second remains instructional-quality
judgment and is now explicit in the Writer and reviewer prompts. They remain recorded here
because a pilot that only reports its score stops being evidence.

### What the later controlled files established (2026-08-16 to 2026-08-17)

The later outputs do **not** support a claim of 100% correctness. Scoring the saved files
directly with `scripts/evaluate_controlled_run.py` produced these exact-key results:

| Workbook | Exact target cells | Unexpected changed cells |
| --- | ---: | ---: |
| hard 15 | 7 / 11 | 1 |
| realistic 24 | 15 / 17 | 5 |
| adversarial 24 | 21 / 31 | 4 |
| adversarial interrupted rerun (working copy) | 24 / 31 | 3 |

Some key expectations may admit a manually acceptable alternative, so this is an exact-key
score rather than a universal mathematical verdict. It is still enough to reject the old
readiness claim: unexpected edits and missed exact targets are not a clean pass.

Those files exposed architecture faults that prompt wording could never repair. The second
reviewer skipped every block with an earlier ledger entry; rejected edits were written before
review and then imperfectly rolled back; agents could not see all A–P fields; custom background
was stored but sent nowhere; semantic target locations were represented ambiguously; and a
second hidden cap silently omitted most of a long custom rules document. Those paths are now
reworked. The Writer's proposal is simulated and independently accepted before any workbook
byte changes, the second reviewer examines every current block, findings name exact cell pairs,
and all accepted instructions reach their documented roles.

The interrupted rerun is not a new accuracy claim: the job failed before final validation.
It exposed an architecture crash where a Writer copied the `before` value from a neighboring
cell into a candidate. Candidate review had moved before file application, but the exact-cell
check had not moved with it. The gate now checks `before` against the parsed current block
before simulation and again against the live file at apply time. The same run also led to a
claim-blind cross-agent audit, root-defect queue priority, removal of the invalid
“identifier numbers must be consecutive” assumption, an exact/fraction form guard, and a
non-editing inverse-operation hint signal backed by explicit semantic prompt checks.

Two semantic misses remain model-quality questions: answering with the smaller solution when
the question asks for the larger, and selecting a real solution when the question asks for the
non-solution. The prompts now require requested-form, domain and solution-count checks, but only
a fresh held-out live evaluation at the intended model/effort/batch settings can measure that
improvement. The historical files are regression evidence; they are not deployment proof.

The next evaluation set is deliberately fresh: four plain workbooks in
`outputs/deployment-heldout-suite-20260819/` contain 60 problems, 38 defect groups,
52 automated checks and 22 clean controls. Their keys support valid alternatives,
mathematical equivalence and multiple-choice invariants rather than requiring one arbitrary
string.

**They have now been run, and they are no longer held out.** Five live jobs against three of
the four workbooks are recorded in `jobs/council.db`: two succeeded, one needed a person, and
two were killed by the isolation false positive since fixed. Their failures went on to shape
the architecture — the isolation demotion, the auditor/reviewer corroboration questions — so
scoring against them now measures how well the system was fitted to them. **Reclassify the
first three as regression tests**: they can prove a known failure no longer recurs, which
is worth having, and they cannot measure accuracy on unseen material.

The 2026-08-30 fresh regression jobs for Workbooks 1 and 3 are preserved under
`outputs/live-regressions-20260830/`. Their corrected files score 13/13 and 10/10 against
the sealed keys, with no unauthorized changed cell or changed clean control. That is
23/23 spent-regression recall, not a fresh accuracy estimate. Workbook 1 completed in 76
physical CLI calls (10 failed/retried); Workbook 3 completed in 92 (11 failed/retried).
The personal Pro CLI also exhausted its allowance during an earlier 96-call attempt, so
these runs support the repair architecture while arguing against treating the subscription
CLI as organization-grade capacity.

The fourth workbook (`heldout-04`) has never been run and is the only genuinely unseen
material left. Deployment evidence needs a new set built after the architecture stops moving.

Do not expose the `evaluation-keys/` files in an upload or custom prompt; score the corrected
downloads only after every run finishes.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q                     # everything
.venv/bin/python -m pytest tests/test_rules.py -q        # one file
.venv/bin/python -m pytest tests/test_rules.py::test_x   # one test
```

Every fixture is generated into `tmp_path` with invented mathematics. No real educational
content is committed. The suite needs no credentials and touches no real file.
