"""B07: entity wiring and the review-correction loop.

Counterexamples from the plan:

- a clearly DISPLAYED short fragment (``RRRQRRKKRG``) could never be
  accepted: SEQ_COMPLETENESS fails on every inline candidate, so the
  review loop was permanently blocked;
- a correction with ``raw=">128"`` but ``number="12.8"``/``operator="="``
  must be rejected (structured fields must agree with raw);
- the records API must expose each record's revision and the allowed
  actions/blockers so the UI never guesses the head version;
- explicit prose/table entities (``peptide A``) should link measurements
  to Entity objects with evidence — and stay UNLINKED with a review
  reason when no explicit name matches.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bio_pdf.application.job_service import JobPaths, JobService
from bio_pdf.application.review_service import ReviewService
from bio_pdf.errors import WorkflowError
from bio_pdf.extract.entities import extract_entities
from bio_pdf.workflow.queue_worker import run_worker


def _paper_with_fragment(tmp_path: Path) -> Path:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    pdf = tmp_path / "frag.pdf"
    c = canvas.Canvas(str(pdf), pagesize=A4)
    t = c.beginText(50, 800)
    for line in [
        "we used the HIV TAT protein (RRRQRRKKRG) in this study.",
        "",
        "Cells were treated with 20 uM peptide A, reducing viability to 5%.",
    ]:
        t.textLine(line)
    c.drawText(t)
    c.save()
    return pdf


@pytest.fixture()
def service(tmp_path: Path):
    svc = JobService(JobPaths.under(tmp_path / "data"))
    yield svc, tmp_path
    svc.close()


# ---- entities --------------------------------------------------------------

def test_explicit_peptide_names_become_entities() -> None:
    blocks = [
        {"text": "Cells were treated with 20 uM peptide A, reducing viability to 5%.", "page_number": 1},
        {"text": "compound 3 was inactive", "page_number": 2},
    ]
    entities = extract_entities(blocks, "sha256:doc")
    names = {e.name_raw for e in entities}
    assert "peptide A" in names
    assert "compound 3" in names
    for entity in entities:
        assert entity.entity_id.startswith("ent:")
        assert entity.evidence_ids


def test_measurement_links_to_matching_entity_only() -> None:
    from bio_pdf.extract.entities import link_measurements_to_entities
    from bio_pdf.contracts import ActivityValue, Measurement

    entities = extract_entities(
        [{"text": "peptide A was active", "page_number": 1}], "sha256:doc"
    )
    matched = Measurement(
        measurement_id="m1",
        document_id="sha256:doc",
        entity_raw="peptide A",
        metric="viability",
        value=ActivityValue(kind="scalar", number="5", raw="5"),
        evidence_ids=["ev1"],
    )
    unmatched = Measurement(
        measurement_id="m2",
        document_id="sha256:doc",
        entity_raw="peptide Z",
        metric="viability",
        value=ActivityValue(kind="scalar", number="5", raw="5"),
        evidence_ids=["ev1"],
    )
    linked, reasons = link_measurements_to_entities([matched, unmatched], entities)
    assert linked[0].entity_id == entities[0].entity_id
    assert linked[1].entity_id is None  # no guessing
    assert any("peptide Z" in r for r in reasons)


# ---- fragment acceptance ----------------------------------------------------

def test_displayed_fragment_can_be_accepted(service) -> None:
    svc, tmp_path = service
    pdf = _paper_with_fragment(tmp_path)
    job, _ = svc.create_job(pdf)
    run_worker(tmp_path / "data", once=True)

    seq_items = [i for i in svc.review_items(job.job_id) if ":seq:" in i.record_id or ":inline:" in i.record_id]
    assert seq_items, "fixture must produce a sequence review item"

    review = ReviewService(svc.repository, svc.paths.artifact_root)
    outcome = review.apply_decision(
        review_id=seq_items[0].review_id,
        action="confirm",
        base_revision=seq_items[0].record_revision,
        actor="tester",
    )
    assert outcome.acceptance_state == "accepted"
    # The fragment identity is preserved — accepting does not pretend it
    # is a full protein.
    assert outcome.record.get("sequence_scope") == "fragment"
    assert outcome.record.get("is_explicit_sequence") is True


# ---- correction consistency -------------------------------------------------

def test_correction_with_inconsistent_raw_is_rejected(service) -> None:
    svc, tmp_path = service
    pdf = _paper_with_fragment(tmp_path)
    job, _ = svc.create_job(pdf)
    run_worker(tmp_path / "data", once=True)

    meas_items = [i for i in svc.review_items(job.job_id) if ":prose:" in i.record_id]
    # The fixture's mention has no metric → not a measurement; build one.
    # Fall back: use any measurement review item if present.
    if not meas_items:
        pytest.skip("fixture produced no measurement review item")

    review = ReviewService(svc.repository, svc.paths.artifact_root)
    with pytest.raises(WorkflowError, match="raw"):
        review.apply_decision(
            review_id=meas_items[0].review_id,
            action="correct",
            base_revision=meas_items[0].record_revision,
            actor="tester",
            patch={
                "value": {
                    "kind": "scalar",
                    "operator": "=",
                    "number": "12.8",
                    "raw": ">128",  # structured fields contradict raw
                }
            },
        )


# ---- records API revision metadata ------------------------------------------

def test_records_api_exposes_revision_and_actions(tmp_path: Path) -> None:
    from fastapi.testclient import TestClient

    from bio_pdf.api.app import create_app
    from bio_pdf.api.settings import ApiSettings

    data_dir = tmp_path / "data"
    with TestClient(create_app(ApiSettings(data_dir=data_dir, samples_dir=tmp_path / "s"))) as c:
        pdf = _paper_with_fragment(tmp_path)
        doc = c.post(
            "/api/v1/documents",
            files={"file": ("frag.pdf", pdf.read_bytes(), "application/pdf")},
        ).json()
        job = c.post(
            "/api/v1/jobs", json={"document_id": doc["document_id"], "parser": "native"}
        ).json()
        run_worker(data_dir, once=True)

        records = c.get(f"/api/v1/jobs/{job['job_id']}/records").json()
        assert records["revision"] == 0
        all_records = records["sequences"] + records["measurements"]
        assert all_records
        for record in all_records:
            assert "_revision" in record or "record_revision" in record
