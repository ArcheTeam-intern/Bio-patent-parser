"""Engine preference chain: ``auto`` → MinerU → Docling → native.

The 2026-10-19 evaluation put MinerU ahead on sequence recovery in both
languages once the paragraph-split fix landed, so ``auto`` prefers it.
The chain is resolved ONCE at job creation and the concrete engine is
persisted — a run never switches parser mid-flight.

These tests pin the ordering and the audit trail; they do NOT call any
cloud API.
"""

from __future__ import annotations

import pytest

from bio_pdf.application.job_service import resolve_engine
from bio_pdf.runtime_config import RuntimeConfig, set_runtime_config


@pytest.fixture(autouse=True)
def _clean_runtime():
    set_runtime_config(RuntimeConfig())
    yield
    set_runtime_config(RuntimeConfig())


def test_explicit_engine_is_never_rewritten() -> None:
    for name in ("native", "docling_sdk", "mineru_http", "docling_serve"):
        assert resolve_engine(name) == (name, [f"explicit:{name}"])


def test_auto_prefers_mineru_when_configured(monkeypatch) -> None:
    monkeypatch.setenv("MINERU_TOKEN", "token")
    set_runtime_config(RuntimeConfig(mineru_token="token"))
    resolved, chain = resolve_engine("auto")
    assert resolved == "mineru_http"
    assert chain == ["mineru_http:configured"]


def test_auto_falls_back_to_docling_without_a_token(monkeypatch) -> None:
    monkeypatch.delenv("MINERU_TOKEN", raising=False)
    set_runtime_config(RuntimeConfig(mineru_token=""))
    resolved, chain = resolve_engine("auto")
    # Docling availability is environment-dependent (SDK installed here),
    # so the assertion accepts either the docling step or the native
    # fallback — what matters is that MinerU is rejected WITH A REASON
    # and the next candidate is tried in order.
    assert resolved in ("docling_sdk", "native")
    assert chain[0].startswith("mineru_http:missing")
    if resolved == "docling_sdk":
        assert chain[1] == "docling_sdk:available"
    else:
        assert chain[-1] == "native:always-available"


def test_unresolved_auto_is_a_programming_error() -> None:
    """``_require_engine_available`` must refuse to run with the literal
    'auto' — reaching it means resolution was skipped."""

    from bio_pdf.application.job_service import _require_engine_available
    from bio_pdf.config import WorkflowConfig
    from bio_pdf.errors import EngineUnavailableError

    with pytest.raises(EngineUnavailableError) as excinfo:
        _require_engine_available(WorkflowConfig(parser="auto"))
    assert excinfo.value.details.get("code") == "PARSE_ENGINE_UNRESOLVED"


def test_job_creation_persists_the_resolved_engine(tmp_path) -> None:
    """A job created with ``auto`` records the concrete engine, so the job
    view shows what actually ran."""

    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    from bio_pdf.application.job_service import JobPaths, JobService
    from bio_pdf.runtime_config import RuntimeConfig, set_runtime_config

    set_runtime_config(RuntimeConfig(mineru_token="token"))
    pdf = tmp_path / "in.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    t = c.beginText(50, 800)
    for line in ["<210> 1", "<211> 4", "<212> PRT", "<400> 1", "Ala Gly Leu Lys"]:
        t.textLine(line)
    c.drawText(t)
    c.save()

    svc = JobService(JobPaths.under(tmp_path / "data"))
    try:
        job, _ = svc.create_job(pdf, engine="auto")
        config = svc.job_config(job.job_id)
        assert config["effective_config"]["parser"] == "mineru_http"
    finally:
        svc.close()
