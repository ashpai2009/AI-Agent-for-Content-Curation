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
export GEMINI_API_KEY=...            # see .env.example for every setting
.venv/bin/uvicorn "oatutor_council.api:create_app" --factory --app-dir src --port 8000
```

```bash
# Submit
curl -s -X POST localhost:8000/jobs \
  -F workbook=@section-7-4.xlsx \
  -F instructions=@errata.md
# {"job_id":"9f2c...","state":"created","seed_claims":3}

# Poll — never blocks on an in-flight write
curl -s localhost:8000/jobs/9f2c...

# Artefacts
curl -s  localhost:8000/jobs/9f2c.../issues
curl -s  localhost:8000/jobs/9f2c.../changes
curl -s  localhost:8000/jobs/9f2c.../reviews
curl -s  localhost:8000/jobs/9f2c.../report
curl -sO localhost:8000/jobs/9f2c.../download

# Resume an interrupted job (409 if finished or already running)
curl -s -X POST localhost:8000/jobs/9f2c.../resume
```

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
  validation/      rules/ (38 rules) · mathematics · patch_gate · final_gate
  agents/          initial_auditor · writer · known_issue_reviewer ·
                   independent_reviewer · isolation · rendering · schemas
  llm/             base · provider (Gemini) · mock · context · prompts
  ingestion/       instruction_documents
  reporting/       ledger · reports
prompts/           versioned agent prompts, plus the shared untrusted-data policy
scripts/           demo.py · evaluate_workbooks.py · recon_workbooks.py
```

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
violation fails the job — never a warning, never a retry.

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

Google Gemini through `google-genai`, default model `gemini-3.6-flash`, overridable with
`GEMINI_MODEL`. Structured outputs with Pydantic schemas for every agent response,
exponential backoff on 429, and a scripted mock client that drives the entire council
offline.

**There is no `Conversation` object anywhere in the codebase**, and a test greps the whole
package to keep it that way. Context isolation is not a discipline anyone has to remember;
there is simply no message list that could carry reasoning forward.

See `docs/gemini-sdk-notes.md` before touching `llm/provider.py`. The API differs from
every plausible guess — it is `client.interactions.create`, not `models.generate_content`;
the parameter is `input=`, not `contents=`; and `status` must be checked before
`output_text` is read, because an `incomplete` interaction returns truncated JSON that
fails schema validation with a confusing error rather than the real cause.

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

## Testing

```bash
.venv/bin/python -m pytest tests/ -q                     # everything
.venv/bin/python -m pytest tests/test_rules.py -q        # one file
.venv/bin/python -m pytest tests/test_rules.py::test_x   # one test
```

Every fixture is generated into `tmp_path` with invented mathematics. No real educational
content is committed. The suite needs no credentials and touches no real file.
