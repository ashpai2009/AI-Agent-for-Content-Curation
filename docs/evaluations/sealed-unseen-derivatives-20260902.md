# Sealed unseen-chapter evaluation — 2026-09-02

## Outcome

`HARNESS_FAILED`; no accuracy score was computed and the workbook will not be rerun.

The council completed 20 live Claude calls, then stopped in `repairing_known`. One result
inside a coordinated Writer response proposed a cell edit whose before and after values
were identical. The internal no-op invariant correctly rejected the edit, but its
validation exception escaped the issue boundary and terminated the whole job. No
corrected workbook was produced and the sealed input remained unchanged.

The run also showed that the selected derivative excerpt was not a valid accuracy
fixture. Its source content already contained deterministic `NON_ASCII_MATH` findings
outside the planted answer key. Legitimate cleanup would therefore have been counted as
unexpected edits.

## Follow-up

The general runtime defect was fixed without changing any agent prompt: invalid proposals
are now issue-local rejections, so valid sibling proposals from the same paid Writer call
survive. Unit and council-level regressions reproduce the exact failure shape.

A future sealed score must use a different unseen chapter, export and validate a clean
baseline before planting errors, and run the planted workbook exactly once. This result
must remain recorded as a harness failure rather than being replaced by a retry.
