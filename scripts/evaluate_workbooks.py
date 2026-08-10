"""Read-only evaluation of the deterministic core across every real workbook.

Runs the reader, the convention detector, the rules engine and the diff over each file
and reports what happens. **No model is called and nothing is written to the corpus.**
Every file is hashed before and after, and the run aborts if any hash moves.

Two questions are being answered:

1. *Does the deterministic core survive real input?* A parser that only handles fixtures
   proves nothing, and several of the corrections in this codebase exist because this
   script disagreed with the fixtures.

2. *Is the source-to-output diff clean on a zero-edit round trip?* If openpyxl perturbs a
   real workbook, every job on that file fails its own integrity gate. That number has to
   be zero, and it is checked here rather than assumed.

Usage:  .venv/bin/python scripts/evaluate_workbooks.py [corpus_dir] [--json out.json]
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from openpyxl import load_workbook  # noqa: E402

from oatutor_council.models import Severity  # noqa: E402
from oatutor_council.validation.rules import describe_rules, run_rules  # noqa: E402
from oatutor_council.workbook.diff import compare_workbooks  # noqa: E402
from oatutor_council.workbook.reader import WorkbookReadError, read_workbook  # noqa: E402

DEFAULT_CORPUS = Path.home() / "Documents" / "OATutor"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def evaluate(path: Path, scratch: Path) -> dict:
    """Everything this file tells us, without changing a byte of it."""
    before = sha256(path)
    report: dict = {"file": path.name, "sha256": before}

    try:
        parsed = read_workbook(path)
    except WorkbookReadError as error:
        report["read_error"] = str(error)
        if sha256(path) != before:
            raise SystemExit(f"ABORT: {path.name} was modified")
        return report

    findings = tuple(run_rules(parsed)) + parsed.all_findings
    severities = collections.Counter(f.severity.value for f in findings)

    report.update(
        {
            "header_row": parsed.header_row,
            "first_data_row": parsed.first_data_row,
            "blocks": len(parsed.blocks),
            "orphan_rows": len(parsed.orphan_rows),
            "notation": parsed.conventions.notation.value,
            "dependency_convention": parsed.conventions.dependency_convention.value,
            "scaffold_namespace": parsed.conventions.dominant_scaffold_namespace,
            "namespace_consistent": parsed.conventions.scaffold_namespace_is_consistent,
            "naming_stems": list(parsed.conventions.naming_stems),
            "findings_total": len(findings),
            "findings_by_severity": dict(severities),
            "findings_by_code": dict(
                collections.Counter(f.code for f in findings).most_common()
            ),
            "blocking": [
                {"row": f.row, "code": f.code, "message": f.message[:160]}
                for f in findings
                if f.severity is Severity.BLOCKING
            ][:20],
        }
    )

    # The zero-edit round trip. A copy is made into scratch so the corpus is untouched.
    working = scratch / f"roundtrip-{path.stem}.xlsx"
    shutil.copy2(path, working)
    workbook = load_workbook(working, data_only=False)
    workbook.save(working)
    workbook.close()

    differences = compare_workbooks(path, working)
    report["roundtrip_differences"] = len(differences)
    report["roundtrip_sample"] = [d.describe()[:160] for d in differences[:5]]
    working.unlink(missing_ok=True)

    if sha256(path) != before:
        raise SystemExit(f"ABORT: {path.name} was modified during evaluation")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("corpus", nargs="?", default=str(DEFAULT_CORPUS))
    parser.add_argument("--json", dest="json_out")
    args = parser.parse_args()

    corpus = Path(args.corpus).expanduser()
    workbooks = sorted(corpus.glob("*.xlsx"))
    if not workbooks:
        print(f"no workbooks found in {corpus}")
        return 1

    scratch = Path(tempfile.mkdtemp(prefix="oatutor-eval-"))
    reports = []
    try:
        print(f"{len(workbooks)} workbook(s) in {corpus}")
        print(f"{len(describe_rules())} rules registered\n")
        header = (
            f"{'file':16s} {'blocks':>6s} {'notation':8s} {'ns':4s} "
            f"{'findings':>8s} {'block':>5s} {'err':>5s} {'diff':>4s}"
        )
        print(header)
        print("-" * len(header))

        for path in workbooks:
            report = evaluate(path, scratch)
            reports.append(report)
            if "read_error" in report:
                print(f"{path.name:16s} READ ERROR: {report['read_error'][:60]}")
                continue
            severities = report["findings_by_severity"]
            print(
                f"{path.name:16s} {report['blocks']:6d} "
                f"{report['notation']:8s} "
                f"{str(report['scaffold_namespace'] or '-'):4s} "
                f"{report['findings_total']:8d} "
                f"{severities.get('blocking', 0):5d} "
                f"{severities.get('error', 0):5d} "
                f"{report['roundtrip_differences']:4d}"
            )
    finally:
        shutil.rmtree(scratch, ignore_errors=True)

    readable = [r for r in reports if "read_error" not in r]
    drift = [r for r in readable if r["roundtrip_differences"]]

    print(f"\nread successfully: {len(readable)}/{len(reports)}")
    print(f"zero-edit round trips with no differences: {len(readable) - len(drift)}/{len(readable)}")
    if drift:
        print("\nROUND-TRIP DRIFT — every job on these files would fail its own gate:")
        for report in drift:
            print(f"  {report['file']}: {report['roundtrip_differences']} difference(s)")
            for line in report["roundtrip_sample"]:
                print(f"      {line}")

    total_blocking = sum(
        r["findings_by_severity"].get("blocking", 0) for r in readable
    )
    print(f"\nblocking findings across the corpus: {total_blocking}")
    codes = collections.Counter()
    for report in readable:
        codes.update(report["findings_by_code"])
    print("\nmost common findings:")
    for code, count in codes.most_common(12):
        print(f"  {code:34s} {count}")

    print("\nall source hashes unchanged")

    if args.json_out:
        Path(args.json_out).write_text(json.dumps(reports, indent=2), encoding="utf-8")
        print(f"wrote {args.json_out}")

    return 0 if not drift else 2


if __name__ == "__main__":
    raise SystemExit(main())
