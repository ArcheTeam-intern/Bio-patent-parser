"""Parser adapters: native, Docling, MinerU.

Each adapter implements :class:`ParserAdapter`. The adapter owns all SDK/CLI
specifics; downstream code only sees a :class:`ParseArtifacts` triple and the
produced :class:`NormalizedDocument`.
"""

from bio_pdf.parsers.base import (
    ParseArtifacts,
    ParseRequest,
    ParserAdapter,
)
from bio_pdf.parsers.native_adapter import NativeAdapter
from bio_pdf.parsers.normalize_docling import (
    NormalizationLossError,
    normalize_docling_document,
)
from bio_pdf.parsers.docling_serve_adapter import (
    DEFAULT_BASE_URL,
    DoclingServeAdapter,
)
from bio_pdf.parsers.mineru_adapter import (
    DEFAULT_BASE_URL as MINERU_DEFAULT_BASE_URL,
    DEFAULT_MODEL_VERSION as MINERU_DEFAULT_MODEL_VERSION,
    MinerUAdapter,
)

__all__ = [
    "ParseArtifacts",
    "ParseRequest",
    "ParserAdapter",
    "NativeAdapter",
    "DoclingServeAdapter",
    "MinerUAdapter",
    "DEFAULT_BASE_URL",
    "MINERU_DEFAULT_BASE_URL",
    "MINERU_DEFAULT_MODEL_VERSION",
    "normalize_docling_document",
    "NormalizationLossError",
]