# Reconnaissance findings — real workbook corpus

Produced by `scripts/recon_workbooks.py` over 11 workbooks in `~/Documents/OATutor/`. Every file was
SHA-256 hashed before and after inspection; **no hash moved**. Raw output: `recon/workbooks.json`
(gitignored).

**This document drives fixture design.** Synthetic fixtures reproduce these *structures* with invented
mathematics. Real content is never copied into a committed test. The written rules remain authoritative
wherever the corpus disagrees with them.

---

## 1. Layout is not uniform — detect, don't assume

| Fact | Reality |
| --- | --- |
| Sheets | 1 per workbook, always the active sheet |
| Header row | Row 1 in all 11 |
| First problem row | Row 2 in 8 files; **row 3 in `7.2`, `7.4`, `7.5`**, which put tool output on row 2 |
| Columns A–P | Stable across all 11 — safe to address by index |
| Columns Q–T | **Drift.** `Validator Check` sits at col 18 (`7.2`), 19 (9 files), 20 (`7.5`) — and is **duplicated** in `7.2` (18 *and* 19) and `7.5` (19 *and* 20) |
| Columns U–Y | **The contract is incomplete.** All 11 carry undocumented tooling columns: `Debug Link`, `Problem ID`, `Lesson ID`, `Image Checksum`, out to column 25 |

**Consequence:** the reader locates the header row by scanning for `Problem Name`, and resolves the
**trailing validator/metadata columns by header name, not fixed index**. Hardcoding `S`/`T` would
silently read the wrong cells on two of eleven workbooks. Name resolution also has to look **past
column T**: `7.5` duplicates `Validator Check` into column 20 and pushes `Time Last Checked` out to
column 21, so a lookup bounded at the documented contract would report it missing.

**Row 2 is not blank in `7.2`, `7.4` and `7.5`** — it holds the OATutor validator's own output and a
`Lesson ID`, i.e. content in columns the curation contract does not describe. Two consequences: a row
counts as blank only when **every** column is empty, not merely the mapped ones, or a row like this is
trimmed off a block or dropped from segmentation; and the extra columns must be preserved untouched
through the write-and-diff cycle even though nothing curates them.

The `Validator Check` text is a prior tool's findings embedded in the file (`"Hint ID is missing"`,
`"Scaffold ID is missing"`). It is **evidence, not ground truth**, and the council must reach its own
conclusions rather than trusting it.

## 2. Conventions vary per workbook

**Naming stems** — 11 distinct, one per workbook, counter suffixed, no zero padding:
`angles`, `Unitcirc` (capitalised), `othertrig`, `righttrig`, `triggraphs`, `real`, `trig` (×2),
`angle`, `sumprod`, `trigonometric`. Never assume lowercase and never assume a section prefix.

**Scaffold ID namespace — the `h` namespace is the majority, not the exception:**

| Namespace | Files |
| --- | --- |
| `h` only | `5.1`, `5.2`, `5.3`, `6.1`, `6.2`, `7.1` (6 files) |
| `s` only | `7.4` |
| mixed `s` + `h` | `7.3` (166 `s` / 12 `h`), `7.5` (229 `s` / 3 `h`) |
| no scaffold IDs | `5.4`, `7.2` |

The planning assumption that `5.1` was an outlier was wrong. This validates the decision to **detect
and preserve a workbook-wide consistent namespace, downgrading the deviation to a warning** rather than
flagging every scaffold in six workbooks as broken. The mixed files are genuinely inconsistent and do
warrant findings.

**Dependency convention** — where decidable it is always **reset-per-step**; `continuous` appears
nowhere. Most blocks are undecided simply because they contain a single step, which gives no evidence
either way. The detector must treat single-step blocks as *no evidence* and fall back to a
workbook-level verdict, never guess per block.

**Notation** — 10 workbooks pure ASCII; `7.5` alone is LaTeX. Both conventions must be supported.

## 3. Defects actually present

| Code | Count | Where |
| --- | ---: | --- |
| `ROW_SHIFT_RIGHT` | 32 | `7.2` |
| `DATE_COERCION` | 18 | `6.1` (14), `7.5` (4) |
| `COLUMN_SHIFT` | 12 | `7.3` |
| `PROBLEM_NAME_MISMATCH_IN_BLOCK` | 5 | `7.4` |
| `DOUBLE_ESCAPED_BACKSLASH` | 4 | `7.5` |
| `ORPHAN_ROW_BEFORE_FIRST_PROBLEM` | 1 | `7.4` |
| `MC_LATEX_PIPE_CORRUPTION` | 1 | `7.5` |

### Two distinct shift shapes — not one defect

The corpus contains **two structurally different corruptions**, and conflating them would produce a
wrong repair:

- **`7.3` — partial left shift.** In 12 rows the `G`–`I` block moved one column left, so `h1`/`h2`
  land in `answerType` (F) and the dependency in `HintID` (G).
- **`7.2` — whole-row right shift by 2.** In 32 rows every value moved two columns right:
  `Problem Name`→`Title`, `Row Type`→`Body Text`, `Body Text`→`answerType`, `Answer`→`HintID`.
  A naive check reads these as "missing Problem Name"; they are nothing of the sort.

The detector now distinguishes them. A repair for one applied to the other would destroy data.

### Date coercion — the year is noise

Excel turned fractions into datetimes: `1/2` → `2025-01-02`, `1/3` → `2025-01-03`. **The year differs
per workbook** (2025 in `6.1`, 2026 in `7.5`) because it reflects when the file was edited. Recovery
must read **month/day only and ignore the year entirely**. Column `T` (`Time Last Checked`) is a
legitimate datetime and is exempt.

### The `\middle|` pipe corruption is real

`7.5` row 23 `mcChoices`:

```
$$$$\pm \frac{\sqrt{2}}{2} \;\middle$$|$$\; \frac{\sqrt{2}}{2} \;\middle$$|$$\; \pm \frac{1}{2} \;\middle$$|$$\; \pm \sqrt{2}$$$$
```

A `\middle|` was split on the pipe, destroying both the `$$` delimiters and the choice boundaries.
**The whole cell still has an even `$$` count**, so this is only detectable by checking each
pipe-separated choice individually — a whole-cell balance check misses it entirely.

### Invalid `answerType` values

Beyond the shift-induced garbage in `7.2` (188 distinct values, all Body Text content) and `7.3`
(`h1`/`h2`), two genuine content defects:

- `6.2` uses `string` — outside the valid set `{numeric, algebra, mc}`.
- `7.1` has `cos((θ))**2=1-sin((θ))**2` in the `answerType` column — both a misplaced value **and** a
  Unicode `θ`, which the ASCII notation rule forbids.

### Block-boundary disagreement is real

`7.4` rows 38–42 carry `Problem Name = sumprod3` while sitting inside a block whose problem row says
`sumprod4`, and row 2 holds content before the first problem row. Either reading — trusting the problem
row or trusting the names — is a guess. This is exactly why the reader **emits a structural finding
instead of silently resolving it**.

## 4. Appearance surface is narrow but not empty

| Property | Reality |
| --- | --- |
| `wrap_text` | Off everywhere except 2 rows each in `7.1` and `7.2` |
| Row heights | Unset (default) in 10 files; `6.1` sets `15.75` on 276 rows |
| Column widths | Unset in 10 files; `6.1` sets exactly 1 |
| Merged ranges | None — except **`7.4`, which has 6** |
| Freeze panes / data validations / images | None anywhere |
| Fonts | Arial throughout |

Ten of eleven workbooks are almost featureless, which makes the diff cheap in practice — but `6.1`
and `7.4` prove the exceptions exist. **This is the argument for enumerating every dimension rather
than assuming a narrow surface**: a merged-range or row-height regression would be invisible to a
value-only diff, and `7.4` is the file where it would happen.

## 5. "Correct answer first" is file-dependent, not universal

| High | Low |
| --- | --- |
| `7.4` 67/68 · `7.5` 197/219 · `7.3` 56/69 · `6.1` 11/17 · `7.1` 8/11 | `5.1` 11/69 · `5.3` 6/41 · `6.2` 5/26 |

Real enough to report, too inconsistent to treat as a convention, and shuffling is not something the
written rules require. It stays a **low-severity observation, never auto-corrected**.

---

## Fixtures this implies

Synthetic workbooks (invented mathematics) must cover:

1. Header at row 1 with data at row 2 **and** at row 3, the latter with tool output on row 2.
2. `Validator Check` at column 18, 19, and 20; duplicated; and `Time Last Checked` displaced past
   column T, plus a workbook missing it entirely.
2b. Tooling columns beyond the contract, and a row whose only content lives in one of them.
3. Scaffold namespaces: `s`-only, `h`-only, mixed, and absent.
4. Single-step blocks (no dependency evidence) alongside multi-step reset-convention blocks.
5. ASCII and LaTeX workbooks.
6. Both shift shapes — partial `G`–`I` left shift and whole-row `+2` right shift.
7. Date coercion with **two different years**, plus a legitimate datetime in `Time Last Checked`.
8. A `\middle|` choice list whose whole-cell `$$` count is even but whose parts are unbalanced.
9. Invalid `answerType`: an out-of-set word, and a value containing a Unicode `θ`.
10. Block-boundary disagreement: interior name mismatch, and content before the first problem row.
11. Appearance: a workbook with merged ranges, explicit row heights, and a set column width, to prove
    the diff catches regressions in each.
