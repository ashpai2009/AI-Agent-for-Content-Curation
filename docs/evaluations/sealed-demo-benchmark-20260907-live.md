# Sealed demo benchmark — live result, 2026-09-07

## Result

The frozen council was run once on each of the four sealed workbooks. Only the `.xlsx`
files were supplied to the council; the answer-key JSON files were not included in any
prompt or job directory. No code, prompt, setting, or key changed between the four live
runs.

| Workbook | Machine checks | Manual judgments | Substantive result | Unexpected edits | Clean controls changed | Physical calls | Final state |
|---|---:|---:|---:|---:|---:|---:|---|
| A — algebra | 7/7 | 0/1 | 7/8 | 0 | 0 | 31 | needs human attention |
| B — trig/logs | 7/7 | 1/1 | 8/8 | 0 | 0 | 30 | succeeded |
| C — matrices/conics | 12/12 | 0 | 12/12 | 0 | 0 | 46 | succeeded |
| D — sequences/probability | 7/7 | 1/1 | 8/8 | 0 | 0 | 33 | succeeded |
| **Total** | **33/33** | **2/3** | **35/36 (97.2%)** | **0** | **0** | **140** | — |

Every source hash remained unchanged. The run produced four refusals or authentication
refresh outcomes among the 140 physical invocations; the bounded retry path recovered
from them. Total wall-clock pipeline time was about 35 minutes. The four completed-call
usage totals were 460 uncached input tokens, 172,198 generated output tokens, 1,493,395
cache-read tokens, and 458,487 cache-creation tokens. Subscription usage is not an API
cost measurement.

## Manual judgments

- **A, D46 — fail.** The hint still says to add 5 to both sides of `2*x+5=17`; it must
  say subtract. The Initial Auditor did find this defect. The claim-blind second audit did
  not reproduce it, so the efficiency policy correctly left it unchanged and sent it to a
  curator as unconfirmed. This is a safe escalation, but it is not an autonomous repair.
- **B, D25 — pass.** The output explains the correct unit-circle point `(0,-1)` and derives
  the sine from its y-coordinate. It is substantively correct even though it does not copy
  the fixture author's preferred sentence.
- **D, D25 — pass.** The output says the range of `2**x` is strictly positive and excludes
  zero. That is the required mathematical correction, phrased differently from the key.

The scorer was corrected only after all four jobs finished. An `expected` prose sentence
in an instructional-quality key is now queued for manual judgment unless the fixture
explicitly opts into `comparison: exact`. Structural prose moves remain exact machine
checks. This changes scoring only; it did not alter any live output.

## Interpretation

This suite contains 48 synthetic problems. The measured result clears a 90% demo target
on this suite and shows zero unsafe edits, but it is not proof of 97.2% performance across
all OpenStax chapters or real curator work. It should be presented as a controlled pilot
result with one safe human escalation, not as perfection or a production SLA.

The preserved local evidence is under
`outputs/sealed-demo-benchmark-20260907-live/`: each workbook has its corrected file,
human-readable report, and SQLite call/job audit. The sealed keys remain only in the
separate original key directory.
