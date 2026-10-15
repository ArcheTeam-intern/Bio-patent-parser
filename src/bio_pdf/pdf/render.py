"""Page and region PNG rendering using pypdfium2."""

from __future__ import annotations

import threading
from pathlib import Path

import pypdfium2 as pdfium
from PIL import Image

from bio_pdf.contracts import BoundingBox, EvidenceImage
from bio_pdf.errors import ArtifactWriteFailedError
from bio_pdf.pdf.geometry import (
    PageTransform,
    identity_transform,
    top_left_to_pdfium_bbox,
)

#: pypdfium2's library init/close is process-global and not thread-safe:
#: concurrent first-uses (e.g. a page of figure thumbnails firing parallel
#: requests) corrupt the global state and every later open fails with
#: "Data format error". Serialize all PDFium work in this process.
_PDFIUM_LOCK = threading.RLock()


def _scale_for_dpi(dpi: int) -> float:
    return dpi / 72.0


def render_page_png(
    pdf_path: Path,
    page_index: int,
    *,
    dpi: int = 300,
    output_path: Path | None = None,
) -> tuple[Image.Image, PageTransform]:
    """Render a single page to a PIL Image.

    Returns the image and the :class:`PageTransform` that maps PDF top-left
    points to image pixels.
    """

    if dpi < 72:
        raise ValueError(f"dpi must be at least 72, got {dpi}")
    scale = _scale_for_dpi(dpi)

    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page = pdf[page_index]
            width_pt, height_pt = page.get_size()
            bitmap = page.render(scale=scale)
            pil_image = bitmap.to_pil()
        finally:
            pdf.close()

    transform = PageTransform(
        scale_x=scale,
        scale_y=scale,
        offset_x=0.0,
        offset_y=0.0,
        width_pt=float(width_pt),
        height_pt=float(height_pt),
    )

    if output_path is not None:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        pil_image.save(str(output_path))

    return pil_image, transform


def render_evidence_crop(
    pdf_path: Path,
    *,
    page_number: int,
    bbox: BoundingBox,
    dpi: int = 300,
    output_path: Path,
    document_id: str,
) -> EvidenceImage:
    """Render a top-left-origin ``bbox`` to a PNG.

    Returns an :class:`EvidenceImage` with the affine used to map pixels
    back to page coordinates.
    """

    if page_number < 1:
        raise ValueError(f"page_number must be 1-based, got {page_number}")

    with _PDFIUM_LOCK:
        pdf = pdfium.PdfDocument(str(pdf_path))
        try:
            page = pdf[page_number - 1]
            width_pt, height_pt = page.get_size()
            scale = _scale_for_dpi(dpi)

            # pypdfium2 ``crop`` is the amount to cut off each side
            # ``(left, bottom, right, top)`` in PDF user space. We convert the
            # top-left bbox to bottom-left and express the crop that way.
            bl_bbox = top_left_to_pdfium_bbox(bbox, page_height_pt=float(height_pt))
            crop = (
                float(bl_bbox[0]),
                float(bl_bbox[1]),
                float(width_pt - bl_bbox[2]),
                float(height_pt - bl_bbox[3]),
            )
            bitmap = page.render(scale=scale, crop=crop)
            pil_image = bitmap.to_pil()
        finally:
            pdf.close()

    # Build the affine mapping page top-left points to image pixels.
    pixel_x0 = (bbox[0]) * scale
    pixel_y0 = (bbox[1]) * scale
    transform = PageTransform(
        scale_x=scale,
        scale_y=scale,
        offset_x=pixel_x0,
        offset_y=pixel_y0,
        width_pt=float(width_pt),
        height_pt=float(height_pt),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        pil_image.save(str(output_path))
    except OSError as exc:
        raise ArtifactWriteFailedError(
            f"failed to write crop image: {exc}",
            details={"path": str(output_path)},
        ) from exc

    return EvidenceImage(
        artifact_ref=str(output_path),
        document_id=document_id,
        page_number=page_number,
        bbox=bbox,
        dpi=dpi,
        pixel_width=pil_image.width,
        pixel_height=pil_image.height,
        transform_to_page=list(transform.to_matrix()),
    )