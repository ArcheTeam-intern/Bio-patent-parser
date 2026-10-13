"""API settings resolved from the environment.

Secrets never leave the backend: the browser only sees whether an engine
is configured (capabilities), never a token.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class ApiSettings:
    """Process-wide API configuration."""

    data_dir: Path = Path("data/demo")
    samples_dir: Path = Path("pdf/samples")
    web_dist_dir: Path | None = None
    max_upload_bytes: int = 100 * 1024 * 1024
    cors_origins: list[str] = field(default_factory=list)

    @classmethod
    def from_env(cls) -> "ApiSettings":
        web_dist = os.environ.get("BIO_PDF_WEB_DIST")
        origins = [
            o.strip()
            for o in os.environ.get("BIO_PDF_CORS_ORIGINS", "").split(",")
            if o.strip()
        ]
        return cls(
            data_dir=Path(os.environ.get("BIO_PDF_DATA_DIR", "data/demo")),
            samples_dir=Path(os.environ.get("BIO_PDF_SAMPLES_DIR", "pdf/samples")),
            web_dist_dir=Path(web_dist) if web_dist else None,
            max_upload_bytes=_env_int("BIO_PDF_MAX_UPLOAD_BYTES", 100 * 1024 * 1024),
            cors_origins=origins,
        )


__all__ = ["ApiSettings"]
