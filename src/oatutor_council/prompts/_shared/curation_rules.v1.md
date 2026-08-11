# The workbook curation rules

These are the standing rules for OATutor problem workbooks. They are versioned in this
repository so a curator does not have to attach them to every job — a document uploaded
with a job carries *additional* policy for that job, and never replaces this.

Where a real workbook disagrees with a rule here, the rule wins. The files are evidence
about what workbooks contain, not about what they should contain.

## Row types

A block starts at a `problem` row and runs to the next one. Every populated row inside it
repeats the block's `Problem Name`.

- **problem** — carries `Title` (the directions a student reads) and the block's
  metadata. It carries no `Answer`, `answerType`, `HintID`, `Dependency` or `mcChoices`:
  the problem row introduces the question and the rows beneath it hold the work.
- **step** — carries the question in `Title`, plus `Answer` and `answerType`. It carries
  no identifier and no `Dependency`; steps are ordered by position.
- **hint** — carries `Title` and `Body Text`, and an identifier. It explains rather than
  grades, so it carries no `Answer`, `answerType` or `mcChoices`.
- **scaffold** — a graded sub-question: `Title`, `Body Text`, `Answer`, `answerType` and
  an identifier.

`answerType` is exactly one of `numeric`, `algebra`, `mc`.

## Dependencies

Everything here is scoped to a **step**, not to a block.

- Hints form a chain within their step: the first depends on nothing, and each one after
  it depends on the hint immediately before it.
- A scaffold depends on the nearest hint above it in the same step. Several scaffolds
  following one hint all name that hint — they are not a chain.
- The chain restarts at every step boundary under both numbering conventions.
- Identifier numbering either restarts at each step or continues across them. Both are
  valid; a workbook uses one consistently, and the convention is detected rather than
  assumed.
- A `Dependency` cell names exactly one identifier. Never a list.
- A dependency never points at a row below itself, and never reaches into another step.

## Multiple choice

`mcChoices` is pipe-delimited with between two and five choices. `Answer` must match one
of them **character for character** — an equivalent value written differently is a wrong
answer to the grader. No two choices are identical, no choice is empty, and no distractor
is mathematically equal to the answer.

Only rows typed `mc` carry choices.

## Notation

A workbook is written either in ASCII or in LaTeX, consistently.

**ASCII.** `**` for exponents, never `^`. `sqrt(x)` with parentheses. `arcsin` rather
than `sin^-1` or `asin`. `<=` and `>=`, never `=<` or `=>`. Operators sit tight against
their operands in `Answer` and `mcChoices`. Greek letters are spelled out: `theta`, `pi`.

**LaTeX.** Complete expressions live inside `$$…$$`, with no whitespace immediately
inside the delimiters and no empty containers. Commands never appear outside a container
— they are printed literally there. Explanatory prose belongs outside the container, or
inside `\text{…}`. Manual spacing commands (`\,` `\;` `\!` `\quad` `\qquad`) are not used.
A graded value that contains no LaTeX is written plainly, without delimiters.

**Both conventions are ASCII-only text.** No character above U+007F appears in any cell,
in either convention: LaTeX renders `\theta`, not a literal θ. No tabs, no line breaks,
no double spaces, and no leading or trailing whitespace.

## Appearance

Rows the council edits are set to height 15 with wrap text off. Rows it does not touch
are left exactly as they are and reported as observations — restyling a workbook nobody
asked to have restyled would bury the corrections in a change log full of formatting.
