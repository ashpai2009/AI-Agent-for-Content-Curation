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
- **hint** — carries `Title` and `Body Text`, and an identifier in the `h#` namespace,
  such as `h1`, `h2` or `h3`. It explains rather than grades, so it carries no `Answer`,
  `answerType` or `mcChoices`.
- **scaffold** — a graded sub-question: `Title`, `Body Text`, `Answer`, `answerType` and
  an identifier in the distinct `s#` namespace, such as `s1`, `s2` or `s3`.

`answerType` is exactly one of `numeric`, `algebra`, `mc`. The rules do not otherwise
define a complete classifier between `numeric` and `algebra`. In particular, an unchanged
plain fraction or constant is not a defect merely because one agent would label it
differently. Report or relabel an existing `numeric`/`algebra` value only when the mismatch
is unambiguous — for example, `numeric` on an expression with a genuine free variable —
or when an uploaded curator instruction explicitly defines the workbook's convention.

## Dependencies

Everything here is scoped to a **step**, not to a block.

- Hints form a chain within their step: the first depends on nothing, and each one after
  it depends on the hint immediately before it.
- A scaffold depends on the nearest hint above it in the same step. Several scaffolds
  following one hint all name that hint — they are not a chain.
- The chain restarts at every step boundary under both numbering conventions.
- Identifier numbering either restarts at each step or continues across them. Both are
  valid; a workbook uses one consistently, and the convention is detected rather than
  assumed. This flexibility applies to the numeric suffix, not to the namespace: hints
  use `h#` and scaffolds use `s#`.
- A legacy workbook that uses `h#` for **every** scaffold is reported as a house-style
  observation and is not rewritten automatically. A workbook that mixes `h#` and `s#`
  for scaffolds has no coherent alternative convention; its `h#` scaffold IDs are
  defects and must be renamed to `s#` together with any dependencies that reference them.
- A `Dependency` cell names exactly one identifier. Never a list.
- A dependency never points at a row below itself, and never reaches into another step.

## Mathematical content and requested precision

Solve each graded row and make its wording, Answer and requested form agree. Preserve a
correct value rather than making it less accurate merely to match an inconsistent prompt.
When an Answer is a correct rounding to a coherent *greater* precision than the wording
requests, and those extra digits are useful to later work, repair the wording to request
that precision. When the recorded digits are not the correctly rounded value at either
precision, repair the Answer instead. An uploaded curator instruction can require a
specific precision and overrides this preference.

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
their operands in `Answer` and `mcChoices`. Greek letters are spelled out: `alpha`,
`beta`, `theta`, `pi`. Degrees use a separating space: `45 degrees`, never `45degrees`.

**LaTeX.** Complete expressions live inside `$$…$$`, with no whitespace immediately
inside the delimiters and no empty containers. Commands never appear outside a container
— they are printed literally there. Explanatory prose belongs outside a container, or
inside `\text{…}`. Manual spacing commands (`\,` `\;` `\!` `\quad` `\qquad`) are not used.
A graded value that contains no LaTeX is written plainly, without delimiters.

**Both conventions are ASCII-only text.** No character above U+007F appears in any cell,
in either convention: LaTeX renders `\theta`, not a literal θ. No tabs, no line breaks,
no double spaces, and no leading or trailing whitespace.

## Appearance

Rows the council edits are set to height 15 with wrap text off. Rows it does not touch
are left exactly as they are and reported as observations — restyling a workbook nobody
asked to have restyled would bury the corrections in a change log full of formatting.
