# OATutor Curation Council

Autonomous curation of OATutor/OpenStax mathematics problem workbooks.

A curator uploads one `.xlsx`, optionally attaches a text instruction document, and starts
**one** job. They get back a corrected workbook, a complete issue ledger, a cell-level
change log, every reviewer decision and repair attempt, and a final validation report.

**The curator never names a problem, a cell, or a correction.** The system finds the
issues itself.

```
XLSX (required) + optional instruction document (.pdf/.docx/.txt/.md)
  → Initial Auditor          scans every problem block
  → Writer                   corrects identified issues
  → Known-Issue Reviewer     checks those corrections (fresh context)
  → repair loop              bounded at three attempts per issue
  → Independent Reviewer     sweeps problems nobody flagged (different prompt)
  → repair loop
  → final deterministic validation
  → corrected XLSX + reports
```

---

## The idea

**The LLM does semantic judgment. Deterministic Python does everything mechanical and
safety-critical.**

The model decides what is mathematically wrong, what the correction should be, whether a
correction is right, and what feedback to give. Python parses the workbook, applies exact
cell edits, enforces the rules, checks edit scope, compares source against output, manages
retries, and generates every report.

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

`web/README.md` has the rest.

---

## Architecture

```
src/oatutor_council/
  api.py           HTTP surface, upload validation, job runner
  council.py       the five-stage loop; one step = one model call
  orchestrator.py  crash-safe apply, recovery after a crash
  state_machine.py job and issue transition tables, attempt accounting
  persistence.py   SQLite: WAL, synchronous=FULL, BEGIN IMMEDIATE, epoch fencing
  models.py        the vocabulary every layer speaks
  uploads.py       magic bytes, archive inspection, path containment
  config.py        every setting, read from the environment in one place
  workbook/        reader · writer · diff · styles
  validation/      rules/ (60 rules) · mathematics · patch_gate · final_gate
  agents/          initial_auditor · writer · known_issue_reviewer ·
                   independent_reviewer · isolation · rendering · schemas · batching
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

**Reviewers never see the Writer's reasoning.** Enforced three ways: `PrivateText` is not a
`str` subclass (a subclass interpolates silently into an f-string); `ReviewerContext` is
checked at import time and cannot reference a private type anywhere in its field closure;
and a `TaintRegistry` compares every outgoing payload against registered private text by
exact match *and* 12-token shingle, catching the paraphrase that exact matching misses. A
violation fails the job — never a warning, never a retry. The registry is rebuilt from the
database on every resume: one that lived only in the worker would be a guarantee that ended
at the first crash, since the reasoning it was watching for is still on the patch.

**Every model call is written down.** One row per request — role, model, status, prompt
hash, pinned prompt version, latency, token usage, and the exact prompt text — including
the calls that failed, with the provider's own status. The recording is a wrapper around
the client rather than a call inside each agent, so no agent can forget it. Prompt versions
are pinned per job, so deploying a new prompt mid-job cannot mean one attempt ran under one
set of instructions and the next under another. The API key reaches the SDK directly from
settings and is not reachable from anything in that path.

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
impossible under `os.replace` and therefore means external corruption.

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

Settings are prefixed `COUNCIL_` because `CLAUDE_EFFORT` is a variable the CLI itself sets:
unprefixed, the service would inherit an effort level from whatever session launched it.

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

`SCAN_BATCH_SIZE` (default **1**) controls how many problem blocks one Initial Auditor or
Independent Reviewer call examines. At 1 the code delegates to the unchanged single-block
path — same prompt, same schema, same payload, same recorded prompt hash — so the default
is a genuine no-op rather than something that resembles one.

Above 1 the response is per block, keyed by an opaque id generated for that call. That is
not decoration: **a block the model omitted is indistinguishable from a block it examined
and found clean**, and marking the first done would report a workbook as reviewed when
nothing looked at it. So a block is marked done only when its own result is present and
valid; missing, duplicated and unknown ids all send the block back to the queue, and a
finding whose rows lie entirely outside the block it was filed under is discarded rather
than relocated.

**There is no `Conversation` object anywhere in the codebase**, and a test greps the whole
package to keep it that way. Context isolation is not a discipline anyone has to remember;
there is simply no message list that could carry reasoning forward.

See `docs/claude-cli-notes.md` before touching `llm/claude_cli.py`. It records which flags
were verified against the installed binary and — just as importantly — which were *not*:
the response envelope has not been observed, so the parser checks each plausible location
for structured output and fails loudly rather than guessing. `scripts/smoke_claude_cli.py`
is what closes that gap.

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

**`scripts/shadow_run.py`** runs the *whole council* over a **copy** of one real workbook
and prints what a real job would report. Offline by default — the scripted agents examine
and never edit — with `--live` as an explicit opt-in that says what it is about to send
before it sends it. `scripts/smoke_provider.py` makes one live call with invented
arithmetic and no workbook content at all.

---

## Testing

```bash
.venv/bin/python -m pytest tests/ -q                     # everything
.venv/bin/python -m pytest tests/test_rules.py -q        # one file
.venv/bin/python -m pytest tests/test_rules.py::test_x   # one test
```

Every fixture is generated into `tmp_path` with invented mathematics. No real educational
content is committed. The suite needs no credentials and touches no real file.
