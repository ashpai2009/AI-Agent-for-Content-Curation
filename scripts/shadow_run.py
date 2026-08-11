"""A report-only run of the whole council over a **copy** of one real workbook.

`evaluate_workbooks.py` answers "does the deterministic core survive real input". This
answers the harder question: *would a real job on this file do the right thing* — the
council, the agents, the patch gate, the final gate, the reports. What it does not do is
touch the curator's file or, unless explicitly asked, spend money.

Three guarantees, in the order they matter:

1. **The original is never opened for writing.** The workbook is copied into a scratch job
   directory first, and everything after that point works on the copy. The original is
   hashed before and after and the run fails if the hash moved.
2. **Offline by default.** With no flag the council runs against a scripted client that
   proposes no repairs, which exercises every stage and every gate and produces the full
   report — a shadow run in the literal sense: what would have been examined, and what the
   deterministic layer says about it.
3. **`--live` is opt-in, one file, and prints what it is about to spend.** This is the only
   script that can send real workbook content to a model, and it says so before it does.

Usage:
    .venv/bin/python scripts/shadow_run.py ~/Documents/OATutor/7.3.xlsx
    .venv/bin/python scripts/shadow_run.py <workbook> --live      # real Gemini calls
    .venv/bin/python scripts/shadow_run.py <workbook> --keep      # keep the job directory
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from oatutor_council.agents.schemas import (  # noqa: E402
    AuditorResponse,
    IndependentReviewResponse,
    ReviewerResponse,
    WriterResponse,
)
from oatutor_council.config import load_settings  # noqa: E402
from oatutor_council.council import CurationCouncil  # noqa: E402
from oatutor_council.llm.base import AgentRole  # noqa: E402
from oatutor_council.llm.mock import ScriptedLLMClient  # noqa: E402
from oatutor_council.models import CurationJob, Severity, SourcePath  # noqa: E402
from oatutor_council.persistence import (  # noqa: E402
    Database,
    create_job,
    latest_findings,
    list_changes,
    list_llm_calls,
    load_ledger,
    token_usage,
)
from oatutor_council.workbook.writer import create_working_copy  # noqa: E402


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def observer_client() -> ScriptedLLMClient:
    """An agent set that looks and never touches.

    The auditor finds nothing, the reviewers accept, and the Writer escalates rather than
    editing. Every stage runs, every gate is evaluated, and no cell changes -- so the
    output is a report about the file rather than a correction of it.
    """
    replies = {
        AgentRole.INITIAL_AUDITOR: AuditorResponse(),
        AgentRole.INDEPENDENT_REVIEWER: IndependentReviewResponse(block_is_sound=True),
        AgentRole.WRITER: WriterResponse(
            needs_human_review=True,
            human_review_reason="shadow run: no repairs are proposed",
        ),
        AgentRole.KNOWN_ISSUE_REVIEWER: ReviewerResponse(decision="accept"),
    }
    client = ScriptedLLMClient()
    client.default = lambda request: replies[request.role]
    return client


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("workbook", type=Path, help="a workbook to shadow-run a copy of")
    parser.add_argument(
        "--live",
        action="store_true",
        help="make real Gemini calls with this workbook's content (costs money)",
    )
    parser.add_argument("--keep", action="store_true", help="keep the job directory")
    parser.add_argument("--max-steps", type=int, default=None)
    args = parser.parse_args()

    source = args.workbook.expanduser()
    if not source.is_file():
        print(f"no such workbook: {source}", file=sys.stderr)
        return 2

    original_hash = sha256(source)
    workspace = Path(tempfile.mkdtemp(prefix="shadow-"))

    try:
        # The copy happens before anything else touches the file, and everything after
        # this line works on `job_dir`. The curator's original is read once, here.
        job_dir = workspace / "job"
        stored = job_dir / "source" / f"workbook{source.suffix.lower()}"
        stored.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, stored)

        settings = load_settings()
        if args.live:
            settings.require_credentials()
            print(
                f"LIVE RUN: this will send the contents of {source.name} to "
                f"{settings.gemini_model}. Ctrl-C now if that is not what you want.\n"
            )

        db = Database(workspace / "council.db")
        copy = create_working_copy(SourcePath(str(stored)), job_dir)
        create_job(
            db,
            CurationJob(
                job_id="shadow",
                source_filename=stored.name,
                source_sha256=copy.source_sha256,
            ),
        )

        if args.live:
            from oatutor_council.llm.provider import GeminiClient

            client = GeminiClient(settings)
        else:
            client = observer_client()

        council = CurationCouncil(
            db=db,
            settings=settings,
            client=client,
            job_id="shadow",
            copy=copy,
            worker_id="shadow",
        )

        print(f"--- shadow run: {source.name} -----------------------------------")
        print(f"  mode: {'LIVE' if args.live else 'offline (no model calls billed)'}")
        parsed = council.current_workbook()
        print(f"  blocks: {len(parsed.blocks)}  notation: {parsed.conventions.notation.value}")

        job = council.run(max_steps=args.max_steps)

        ledger = load_ledger(db, "shadow")
        findings = latest_findings(db, "shadow", kind="content")
        integrity = latest_findings(db, "shadow", kind="integrity")
        usage = token_usage(db, "shadow")

        print("\n--- what a real job would report ---------------------------------")
        print(f"  final state: {job.state.value}")
        print(f"  issues opened: {len(ledger.issues)}")
        print(f"  cells changed: {len(list_changes(db, 'shadow'))}")
        print(f"  content findings at the end: {len(findings)}")
        print(f"  integrity findings: {len(integrity)}")
        print(f"  model calls: {usage['calls']} ({usage['failed_calls']} failed)")
        if usage["total_tokens"]:
            print(f"  tokens: {usage['total_tokens']}")

        by_severity: dict[str, int] = {}
        for finding in findings:
            by_severity[finding.severity.value] = by_severity.get(finding.severity.value, 0) + 1
        for severity in (Severity.BLOCKING, Severity.ERROR, Severity.WARNING):
            if by_severity.get(severity.value):
                print(f"    {severity.value}: {by_severity[severity.value]}")

        if args.live:
            print("\n--- calls made ---------------------------------------------------")
            for call in list_llm_calls(db, "shadow")[:20]:
                print(f"  {call['role']:<22} {call['status']:<12} {call['prompt_sha256'][:12]}")

        # The guarantee, checked rather than asserted in a comment.
        moved = sha256(source) != original_hash
        print(f"\n  original workbook unchanged: {not moved}")
        if moved:
            print("  THE ORIGINAL CHANGED. This is a bug; investigate before rerunning.")
            return 3

        if args.keep:
            print(f"\n  job directory kept at {workspace}")
        return 0
    finally:
        if not args.keep:
            shutil.rmtree(workspace, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
