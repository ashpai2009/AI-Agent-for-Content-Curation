"""Instruction-document ingestion tests.

The central assertion, repeated in several shapes: a document that could not be read
never produces an empty instruction set. Those two outcomes are indistinguishable
downstream, and confusing them tells a curator who uploaded a scan that their workbook
is fine.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from documents import (
    SAMPLE_INSTRUCTIONS,
    write_docx,
    write_docx_with_table_only,
    write_image_only_pdf,
    write_markdown,
    write_text,
    write_text_pdf,
)
from oatutor_council.ingestion.instruction_documents import (
    MAX_SEGMENT_CHARACTERS,
    DocumentFormat,
    UnsupportedDocumentError,
    detect_format,
    read_instruction_document,
)


@pytest.mark.parametrize(
    ("writer", "suffix", "expected_format"),
    [
        (write_markdown, ".md", DocumentFormat.MARKDOWN),
        (write_text, ".txt", DocumentFormat.TEXT),
        (write_docx, ".docx", DocumentFormat.DOCX),
        (write_text_pdf, ".pdf", DocumentFormat.PDF),
    ],
)
def test_every_supported_format_yields_the_same_instructions(
    tmp_path, writer, suffix, expected_format
):
    document = read_instruction_document(writer(tmp_path / f"notes{suffix}"))
    assert document.format is expected_format
    rendered = document.render()
    # Compared on a distinctive fragment: PDF layout wraps lines, so exact equality
    # across four very different formats would be testing the writers, not the readers.
    for instruction in SAMPLE_INSTRUCTIONS:
        assert instruction.split()[2] in rendered


def test_every_segment_carries_provenance(tmp_path):
    """A claim has to be citable back to the document the curator wrote."""
    document = read_instruction_document(write_markdown(tmp_path / "notes.md"))
    assert all(segment.provenance for segment in document.segments)
    assert all("line" in segment.provenance for segment in document.segments)


def test_a_docx_written_entirely_as_a_table_is_read(tmp_path):
    """Errata are frequently a table of problem name against correction. A
    paragraphs-only reader would call this document empty."""
    document = read_instruction_document(
        write_docx_with_table_only(tmp_path / "notes.docx")
    )
    assert document.segments
    assert any("table" in segment.provenance for segment in document.segments)


# --------------------------------------------------------------------------------------
# Unreadable documents
# --------------------------------------------------------------------------------------


def test_an_image_only_pdf_is_a_clear_error_not_an_empty_result(tmp_path):
    """The fixture this whole module exists for. There is no OCR, and never will be, so
    the failure has to be loud and has to name the alternatives."""
    with pytest.raises(UnsupportedDocumentError) as caught:
        read_instruction_document(write_image_only_pdf(tmp_path / "scan.pdf"))

    message = caught.value.user_message
    assert "no readable text layer" in message
    assert "OCR" in message
    for alternative in ("DOCX", "TXT", "Markdown"):
        assert alternative in message


def test_an_empty_file_is_rejected_rather_than_read_as_no_instructions(tmp_path):
    """An empty upload cannot be told apart from a failed one."""
    path = tmp_path / "notes.txt"
    path.write_bytes(b"")
    with pytest.raises(UnsupportedDocumentError, match="empty"):
        read_instruction_document(path)


def test_a_whitespace_only_document_is_rejected(tmp_path):
    path = tmp_path / "notes.txt"
    path.write_text("   \n\n   \n", encoding="utf-8")
    with pytest.raises(UnsupportedDocumentError, match="no readable text"):
        read_instruction_document(path)


def test_a_corrupt_pdf_says_so(tmp_path):
    path = tmp_path / "notes.pdf"
    path.write_bytes(b"%PDF-1.4 this is not really a pdf")
    with pytest.raises(UnsupportedDocumentError, match="could not be opened as a PDF"):
        read_instruction_document(path)


def test_a_corrupt_docx_suggests_the_likely_cause(tmp_path):
    path = tmp_path / "notes.docx"
    path.write_bytes(b"not a zip archive")
    with pytest.raises(UnsupportedDocumentError, match=r"\.doc"):
        read_instruction_document(path)


def test_an_unsupported_extension_lists_what_is_supported(tmp_path):
    with pytest.raises(UnsupportedDocumentError) as caught:
        detect_format("errata.xlsx")
    assert ".docx" in caught.value.user_message
    assert ".pdf" in caught.value.user_message


def test_a_missing_file_is_reported(tmp_path):
    with pytest.raises(UnsupportedDocumentError, match="could not be found"):
        read_instruction_document(tmp_path / "absent.txt")


# --------------------------------------------------------------------------------------
# Bounds
# --------------------------------------------------------------------------------------


def test_an_oversized_passage_is_split_with_provenance_preserved(tmp_path):
    """"Somewhere in this 40,000-character block" tells a curator nothing about where a
    claim came from."""
    path = tmp_path / "notes.txt"
    path.write_text("x" * (MAX_SEGMENT_CHARACTERS * 2 + 10), encoding="utf-8")
    document = read_instruction_document(path)
    assert len(document.segments) == 3
    assert all(segment.provenance for segment in document.segments)
    assert sum(len(s.text) for s in document.segments) == MAX_SEGMENT_CHARACTERS * 2 + 10


def test_truncation_is_reported_rather_than_silent(tmp_path, monkeypatch):
    """Dropping the tail of an errata note would lose real instructions with no trace."""
    from oatutor_council.ingestion import instruction_documents as module

    monkeypatch.setattr(module, "MAX_DOCUMENT_CHARACTERS", 50)
    path = tmp_path / "notes.txt"
    path.write_text("\n\n".join(["a paragraph of instructions"] * 20), encoding="utf-8")
    document = module.read_instruction_document(path)
    assert document.truncated
    assert document.character_count <= 50


def test_latin_1_content_is_still_read(tmp_path):
    """A curator's document from Word may not be UTF-8. Refusing it would be a false
    negative about content that is perfectly readable."""
    path = tmp_path / "notes.txt"
    path.write_bytes("the angle is 45\xb0 not 90\xb0".encode("latin-1"))
    document = read_instruction_document(path)
    assert "45" in document.render()
