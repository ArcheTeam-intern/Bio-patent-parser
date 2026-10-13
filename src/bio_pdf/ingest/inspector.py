"""PDF ingestion: signature check, page geometry, page selection.

The module deliberately keeps the surface tiny so we never read raw bytes
elsewhere. The hash of the original file is used as ``document_id`` so
identical inputs produce identical IDs across runs.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from pathlib import Path

import pypdfium2 as pdfium

from bio_pdf.contracts import (
    DocumentInspection,
    PageGeometry,
)
from bio_pdf.errors import (
    InputCorruptError,
    InputEncryptedError,
    InputNotPdfError,
    PageRangeInvalidError,
)

#: Standard PDF header that every conforming file starts with. We accept the
#: optional second line in case some producers emit a comment.
_PDF_HEADER_RE = re.compile(rb"%PDF-\d\.\d\s*(?:%[\x21-\x7e]+)?")


def compute_sha256(path: Path) -> str:
    """Return the lowercase hex SHA-256 of ``path`` contents."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _looks_like_pdf(data: bytes) -> bool:
    """Return True if ``data`` starts with the PDF signature. The header may
    be preceded by at most a small ASCII whitespace run.
    """

    stripped = data.lstrip()[: 1024]
    return bool(_PDF_HEADER_RE.match(stripped))


def _page_geometry(pdf: pdfium.PdfDocument, page_index: int) -> PageGeometry:
    """Return the visible-page geometry for a 0-based ``page_index``."""

    page = pdf[page_index]
    width, height = page.get_size()
    rotation = page.get_rotation()
    # ``pdfium`` exposes rotation as 0/90/180/270. Clamp to the design range.
    rotation_deg = int(round(rotation)) % 360
    return PageGeometry(
        page_number=page_index + 1,
        width_pt=float(width),
        height_pt=float(height),
        rotation_deg=rotation_deg,
    )


def inspect_pdf(path: Path) -> DocumentInspection:
    """Verify ``path`` is a readable PDF and return a :class:`DocumentInspection`.

    Raises:
        InputNotPdfError: If the file does not start with the PDF signature.
        InputCorruptError: If pypdfium2 cannot open the file (object table
            missing, broken cross-ref, truncated file, …).
        InputEncryptedError: If the PDF requires a password we don't have.
    """

    if not path.exists() or not path.is_file():
        raise InputNotPdfError(f"input is not a regular file: {path}")

    data = path.read_bytes()
    sha256 = compute_sha256(path)
    document_id = f"sha256:{sha256}"

    if not _looks_like_pdf(data):
        raise InputNotPdfError(
            f"file does not start with the PDF signature: {path}",
            details={"path": str(path)},
        )

    try:
        pdf = pdfium.PdfDocument(str(path))
    except pdfium.PdfiumError as exc:
        # pdfium raises different error types depending on the failure mode.
        message = str(exc).lower()
        if "password" in message or "encrypted" in message:
            raise InputEncryptedError(
                f"PDF is encrypted and requires a password: {path}",
                details={"path": str(path)},
            ) from exc
        raise InputCorruptError(
            f"PDF cannot be opened by pypdfium: {exc}",
            details={"path": str(path)},
        ) from exc

    try:
        # pypdfium2 4.30 does not expose ``is_encrypted`` directly. We try to
        # access the version metadata which forces a full decrypt; a failed
        # decrypt surfaces as ``PdfiumError`` and we map it to
        # :class:`InputEncryptedError`.
        try:
            _ = pdf.get_version()
        except pdfium.PdfiumError as exc:
            message = str(exc).lower()
            if "password" in message or "encrypted" in message:
                raise InputEncryptedError(
                    f"PDF is encrypted and requires a password: {path}",
                    details={"path": str(path)},
                ) from exc
            raise InputCorruptError(
                f"PDF cannot be read by pypdfium: {exc}",
                details={"path": str(path)},
            ) from exc

        page_count = len(pdf)
        pages = [_page_geometry(pdf, idx) for idx in range(page_count)]
    finally:
        pdf.close()

    return DocumentInspection(
        document_id=document_id,
        sha256=sha256,
        bytes=path.stat().st_size,
        page_count=page_count,
        is_encrypted=False,
        pages=pages,
    )


# ---------------------------------------------------------------------------
# Page selection
# ---------------------------------------------------------------------------


_PAGE_RANGE_RE = re.compile(r"^\s*(\d+)\s*-\s*(\d+)\s*$")
_SINGLE_PAGE_RE = re.compile(r"^\s*(\d+)\s*$")


def parse_page_selection(selection: str, *, page_count: int) -> tuple[int, ...]:
    """Parse a CLI page selection into a sorted, deduplicated page tuple.

    Supported forms:
        - ``all`` (or empty): every page 1..page_count.
        - ``N`` sequence: comma-separated single pages, e.g. ``1,3,7``.
        - ``A-B`` sequence: comma-separated ranges, e.g. ``1-3,7``.

    Returns:
        A 1-based, deduplicated, inclusive page list.

    Raises:
        PageRangeInvalidError: If a token is malformed, points to a page
            outside ``[1, page_count]``, or describes an inverted range.
    """

    if page_count <= 0:
        raise PageRangeInvalidError(
            "page_count must be positive",
            details={"page_count": page_count},
        )

    selection = (selection or "").strip()
    if selection == "" or selection.lower() == "all":
        return tuple(range(1, page_count + 1))

    pages: list[int] = []
    for token in selection.split(","):
        token = token.strip()
        if not token:
            raise PageRangeInvalidError(
                f"empty page token in selection: {selection!r}",
                details={"selection": selection},
            )

        if (single := _SINGLE_PAGE_RE.match(token)):
            pages.append(int(single.group(1)))
            continue
        if (rng := _PAGE_RANGE_RE.match(token)):
            start = int(rng.group(1))
            end = int(rng.group(2))
            if start > end:
                raise PageRangeInvalidError(
                    f"inverted range: {start}-{end}",
                    details={"token": token},
                )
            pages.extend(range(start, end + 1))
            continue

        raise PageRangeInvalidError(
            f"unrecognised page token: {token!r}",
            details={"token": token},
        )

    # Validate the resulting set against the page count.
    out_of_range = [p for p in pages if p < 1 or p > page_count]
    if out_of_range:
        raise PageRangeInvalidError(
            f"page numbers out of range {out_of_range} (have {page_count})",
            details={"pages": out_of_range, "page_count": page_count},
        )

    # Deduplicate while keeping the natural order, then return a tuple.
    return tuple(dict.fromkeys(sorted(pages)))


# ---------------------------------------------------------------------------
# Inventory over a directory
# ---------------------------------------------------------------------------


def _walk_pdfs(root: Path) -> Iterable[Path]:
    """Yield PDF files under ``root`` in stable, sorted order."""

    yield from sorted(root.rglob("*.pdf"))


def inspect_directory(
    root: Path,
) -> list[dict[str, object]]:
    """Inspect every PDF under ``root`` and return one record per file.

    The original files are not renamed, moved or modified. Failed
    inspections are recorded with the matching error code so the caller
    can report a mixed-batch summary.
    """

    records: list[dict[str, object]] = []
    for path in _walk_pdfs(root):
        record: dict[str, object] = {
            "path": str(path),
            "bytes": path.stat().st_size,
            "sha256": compute_sha256(path),
        }
        try:
            inspection = inspect_pdf(path)
            record.update(
                {
                    "status": "ok",
                    "document_id": inspection.document_id,
                    "page_count": inspection.page_count,
                    "is_encrypted": inspection.is_encrypted,
                }
            )
        except InputNotPdfError as exc:
            record.update({"status": "error", "error_code": exc.code, "message": exc.message})
        except InputEncryptedError as exc:
            record.update({"status": "error", "error_code": exc.code, "message": exc.message})
        except InputCorruptError as exc:
            record.update({"status": "error", "error_code": exc.code, "message": exc.message})
        records.append(record)
    return records


def build_inventory_report(records: list[dict[str, object]]) -> dict[str, object]:
    """Aggregate per-file records into a top-level summary.

    The summary counts each outcome so the caller can report
    ``signature_audit_matches`` style numbers without re-parsing the rows.
    """

    summary = {
        "ok": 0,
        "INPUT_NOT_PDF": 0,
        "INPUT_CORRUPT": 0,
        "INPUT_ENCRYPTED": 0,
        "other_errors": 0,
    }
    for record in records:
        status = record.get("status")
        if status == "ok":
            summary["ok"] += 1
        else:
            code = str(record.get("error_code", ""))
            if code in summary:
                summary[code] += 1
            else:
                summary["other_errors"] += 1

    return {
        "scope": "directory_inventory",
        "count": len(records),
        "summary": summary,
        "entries": records,
    }