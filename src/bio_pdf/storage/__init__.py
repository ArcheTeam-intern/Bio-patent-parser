"""Storage: atomic artifact writes and a small SQLite repository.

These helpers are deliberately tiny. The runner (Task 10) is the only caller
that opens SQLite connections; the helpers here own connection lifecycle and
schema migrations.
"""

from bio_pdf.storage.artifacts import (
    ArtifactRef,
    atomic_write_bytes,
    atomic_write_json,
    compute_sha256_bytes,
    read_verified_bytes,
    read_verified_json,
    make_cache_key,
    write_json_artifact,
)
from bio_pdf.storage.repository import (
    Repository,
    SCHEMA_SQL,
    init_repository,
)

__all__ = [
    "ArtifactRef",
    "atomic_write_bytes",
    "atomic_write_json",
    "compute_sha256_bytes",
    "read_verified_bytes",
    "read_verified_json",
    "make_cache_key",
    "write_json_artifact",
    "Repository",
    "SCHEMA_SQL",
    "init_repository",
]