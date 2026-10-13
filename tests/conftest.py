"""Shared pytest configuration and fixtures.

The fixtures here are intentionally tiny. Larger domain fixtures are
co-located with the tests that consume them under tests/fixtures/.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# Make ``src`` importable without installing the package.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC = PROJECT_ROOT / "src"
if str(SRC) not in {str(p) for p in __import__("sys").path}:
    __import__("sys").path.insert(0, str(SRC))


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path to the repository root."""

    return PROJECT_ROOT


@pytest.fixture(scope="session")
def reports_dir(project_root: Path) -> Path:
    """Directory where ad-hoc reports are written during development."""

    path = project_root / "reports"
    path.mkdir(parents=True, exist_ok=True)
    return path