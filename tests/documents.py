"""Synthetic instruction documents for the test suite.

Authored here, not derived from the real errata corpus. The prose is invented and the
problem names are the same fictional stems the workbook fixtures use, so nothing a
curator wrote is ever committed.

The image-only PDF is the important one: it is the fixture that proves an unreadable
document produces a clear error rather than an empty instruction set.
"""

from __future__ import annotations

from pathlib import Path

#: The same instructions rendered in every format, so the parsers can be compared against
#: one another rather than each against its own expectations.
SAMPLE_INSTRUCTIONS = [
    "The answer for angles1 step 2 shows a date instead of the fraction it should be.",
    "In unitcirc3 the multiple-choice list has two options that are the same value "
    "written differently.",
    "Several scaffolds in othertrig1 point at a dependency that does not exist.",
]


def write_markdown(path: Path) -> Path:
    body = "# Curation notes\n\n" + "\n\n".join(
        f"- {line}" for line in SAMPLE_INSTRUCTIONS
    )
    path.write_text(body, encoding="utf-8")
    return path


def write_text(path: Path) -> Path:
    path.write_text("\n\n".join(SAMPLE_INSTRUCTIONS), encoding="utf-8")
    return path


def write_docx(path: Path) -> Path:
    import docx

    document = docx.Document()
    document.add_heading("Curation notes", level=1)
    for line in SAMPLE_INSTRUCTIONS:
        document.add_paragraph(line)
    document.save(path)
    return path


def write_docx_with_table_only(path: Path) -> Path:
    """Errata are often written as a table of problem name against correction.

    A paragraphs-only reader reports this document as empty, which is exactly the silent
    failure the ingestion layer forbids.
    """
    import docx

    document = docx.Document()
    table = document.add_table(rows=1 + len(SAMPLE_INSTRUCTIONS), cols=2)
    table.rows[0].cells[0].text = "Problem"
    table.rows[0].cells[1].text = "Correction"
    for index, line in enumerate(SAMPLE_INSTRUCTIONS, start=1):
        table.rows[index].cells[0].text = f"problem{index}"
        table.rows[index].cells[1].text = line
    document.save(path)
    return path


def write_text_pdf(path: Path) -> Path:
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    page = canvas.Canvas(str(path), pagesize=LETTER)
    page.setFont("Helvetica", 11)
    y = 720
    page.drawString(72, y, "Curation notes")
    for line in SAMPLE_INSTRUCTIONS:
        y -= 24
        page.drawString(72, y, line[:90])
    page.showPage()
    page.save()
    return path


def write_image_only_pdf(path: Path) -> Path:
    """A PDF whose pages carry no text layer at all.

    This is what a scan or a screenshot export looks like to the parser. It must produce
    a clear validation error naming the supported alternatives -- never an empty
    instruction set, which would tell the curator their workbook had no issues.
    """
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas

    page = canvas.Canvas(str(path), pagesize=LETTER)
    page.setFillColorRGB(0.8, 0.8, 0.8)
    page.rect(72, 500, 400, 200, fill=1, stroke=0)
    page.setFillColorRGB(0.4, 0.4, 0.4)
    page.circle(200, 600, 40, fill=1, stroke=0)
    page.showPage()
    page.save()
    return path
