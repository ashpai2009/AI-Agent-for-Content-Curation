"""Validating what a curator uploads.

Everything here treats the upload as hostile, because it is the one part of the system a
stranger controls completely.

**The client's filename is never used to build a path.** The job directory is named by a
generated UUID and the file inside it has a fixed name; the submitted name is stored for
display only. That single decision removes the entire class of traversal attacks, and the
checks below are defence behind it rather than the defence itself.

**Type is decided by magic bytes, not by `Content-Type`.** The header is attacker
controlled and the first four bytes are what every reader will actually act on.

**Both the compressed and decompressed sizes are capped.** An `.xlsx` is a zip archive, so
a small upload can expand to fill a disk. Checking only the upload size defends against
the wrong thing.

**The size cap is enforced while the bytes arrive, not after.** `stream_upload` writes in
chunks and stops at the limit. Reading the whole body first and then comparing its length
means a client who wants to exhaust memory just sends more than the limit.
"""

from __future__ import annotations

import re
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

#: Extensions accepted for each role. The instruction-document set matches what
#: `ingestion.instruction_documents` can actually read.
WORKBOOK_EXTENSIONS = frozenset({".xlsx"})
INSTRUCTION_EXTENSIONS = frozenset({".pdf", ".docx", ".txt", ".md", ".markdown"})

#: First bytes of each accepted format. `.xlsx` and `.docx` are both zip archives.
MAGIC_BYTES = {
    ".xlsx": b"PK\x03\x04",
    ".docx": b"PK\x03\x04",
    ".pdf": b"%PDF-",
}

#: Ratio of decompressed to compressed size above which an archive is a decompression
#: bomb rather than a spreadsheet. Real workbooks in the corpus sit well under 20.
MAX_COMPRESSION_RATIO = 200

#: Absolute ceiling on decompressed content, independent of ratio.
MAX_DECOMPRESSED_BYTES = 512 * 1024 * 1024

_UNSAFE = re.compile(r"[/\\\x00]")


class UploadRejected(Exception):
    """The upload cannot be accepted, with a reason safe to return to the client.

    The message never contains a filesystem path. A curator does not need one, and an
    attacker probing for the storage layout should learn nothing from an error.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


@dataclass(frozen=True)
class UploadedFile:
    """A validated upload. `display_name` is for humans and never for paths."""

    display_name: str
    extension: str
    size: int


def safe_display_name(filename: str) -> str:
    """Sanitise a client filename for display.

    Reduced to its last component and stripped of separators and NULs. Even so it is only
    ever shown, never joined to a directory -- the sanitising is so a report cannot be
    made to display something misleading, not so the name can be trusted.
    """
    if not filename or not filename.strip():
        raise UploadRejected("the upload has no filename")
    if "\x00" in filename:
        raise UploadRejected("the filename contains a NUL byte")

    candidate = PurePosixPath(filename.replace("\\", "/")).name
    candidate = _UNSAFE.sub("", candidate).strip()
    if not candidate or candidate in {".", ".."}:
        raise UploadRejected("the filename is not usable")
    return candidate[:200]


def check_extension(name: str, allowed: frozenset[str], *, kind: str) -> str:
    extension = Path(name).suffix.lower()
    if extension not in allowed:
        raise UploadRejected(
            f"{kind} files must be one of: {', '.join(sorted(allowed))}. "
            f"This upload is a {extension or 'file with no extension'}."
        )
    return extension


def check_magic_bytes(data: bytes, extension: str) -> None:
    """Confirm the bytes match the claimed format.

    `Content-Type` is attacker controlled; the first four bytes are what openpyxl, pypdf
    and python-docx will act on.
    """
    expected = MAGIC_BYTES.get(extension)
    if expected is None:
        return  # Plain text has no signature to check.
    if not data.startswith(expected):
        raise UploadRejected(
            f"the file does not look like a {extension} file. Its contents do not match "
            "the format its name claims."
        )


def check_archive(path: Path, extension: str) -> None:
    """Inspect a zip-backed upload before anything opens it properly.

    Two dangers, both cheap to rule out. A member with an absolute or `..` path escapes
    the extraction directory in any library that extracts naively. And a small archive
    that decompresses enormously fills a disk -- checking the upload size alone defends
    against the wrong number.
    """
    if MAGIC_BYTES.get(extension) != b"PK\x03\x04":
        return

    try:
        with zipfile.ZipFile(path) as archive:
            total = 0
            compressed = 0
            for member in archive.infolist():
                name = member.filename
                if name.startswith("/") or PurePosixPath(name).is_absolute():
                    raise UploadRejected(
                        "the archive contains an entry with an absolute path"
                    )
                if ".." in PurePosixPath(name).parts:
                    raise UploadRejected(
                        "the archive contains an entry that points outside itself"
                    )
                total += member.file_size
                compressed += member.compress_size

            if total > MAX_DECOMPRESSED_BYTES:
                raise UploadRejected(
                    "the archive expands to more content than this service will process"
                )
            if compressed and total / compressed > MAX_COMPRESSION_RATIO:
                raise UploadRejected(
                    "the archive expands far beyond its uploaded size and will not be "
                    "processed"
                )
    except zipfile.BadZipFile as error:
        raise UploadRejected(
            "the file is not a readable archive; it may be corrupted or incomplete"
        ) from error


def validate_upload(
    *,
    filename: str,
    data: bytes,
    allowed: frozenset[str],
    kind: str,
    max_bytes: int,
) -> UploadedFile:
    """Run every check in order, cheapest first."""
    if not data:
        raise UploadRejected(f"the {kind} file is empty")
    if len(data) > max_bytes:
        raise UploadRejected(
            f"the {kind} file is larger than the {max_bytes // (1024 * 1024)} MB limit"
        )

    display = safe_display_name(filename)
    extension = check_extension(display, allowed, kind=kind)
    check_magic_bytes(data, extension)
    return UploadedFile(display_name=display, extension=extension, size=len(data))


#: How much of an upload is read at a time. Large enough that the syscall overhead is
#: irrelevant, small enough that a hundred concurrent uploads cannot be a memory problem.
UPLOAD_CHUNK_BYTES = 1024 * 1024


async def stream_upload(
    upload: Any,
    destination: Path,
    *,
    filename: str,
    allowed: frozenset[str],
    kind: str,
    max_bytes: int,
) -> UploadedFile:
    """Write an upload to disk in chunks, enforcing the cap **as it arrives**.

    The previous version read the whole body into memory and *then* compared its length to
    the limit -- which means a client wanting to exhaust the service's memory simply sends
    more than the limit, and the check that was supposed to stop them runs after the damage
    is done. Concurrency makes it worse: the cap was per upload, and nothing bounded the
    sum.

    Reading in chunks also lets the format check happen on the first chunk, before the rest
    of a file that was never going to be accepted is written to disk at all.
    """
    display = safe_display_name(filename)
    extension = check_extension(display, allowed, kind=kind)

    destination.parent.mkdir(parents=True, exist_ok=True)
    total = 0
    first = True
    try:
        with destination.open("wb") as handle:
            while chunk := await upload.read(UPLOAD_CHUNK_BYTES):
                if first:
                    check_magic_bytes(chunk, extension)
                    first = False
                total += len(chunk)
                if total > max_bytes:
                    raise UploadRejected(
                        f"the {kind} file is larger than the "
                        f"{max_bytes // (1024 * 1024)} MB limit"
                    )
                handle.write(chunk)
    except UploadRejected:
        # A rejected upload leaves nothing behind. Otherwise a stranger can fill the disk
        # with the leading megabytes of files the service refused.
        destination.unlink(missing_ok=True)
        raise

    if total == 0:
        destination.unlink(missing_ok=True)
        raise UploadRejected(f"the {kind} file is empty")

    return UploadedFile(display_name=display, extension=extension, size=total)


def assert_contained(path: Path, root: Path) -> Path:
    """Prove a resolved path really sits under the job root.

    The last line of defence for artefact serving. Artefacts are addressed by
    `(job_id, kind)` rather than by path, so this should be unreachable -- which is
    exactly why it is worth asserting rather than assuming.
    """
    resolved = path.resolve()
    if not resolved.is_relative_to(root.resolve()):
        raise UploadRejected("the requested artefact is not available")
    return resolved
