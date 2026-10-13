"""Annotation schema tests."""

from __future__ import annotations

import pytest

from bio_pdf.evaluation.annotation import Annotation


def test_annotation_requires_document_id() -> None:
    with pytest.raises(Exception):
        Annotation(  # type: ignore[call-arg]
            annotation_id="a1",
            document_id="",
            page_number=1,
            kind="sequence",
        )


def test_annotation_default_is_draft_not_verified() -> None:
    a = Annotation(
        annotation_id="a1",
        document_id="sha256:doc",
        page_number=1,
        kind="sequence",
        seq_id_no="1",
        sequence="MKTG",
    )
    assert a.status == "draft"
    assert a.human_verified is False


def test_annotation_verified_flag() -> None:
    a = Annotation(
        annotation_id="a1",
        document_id="sha256:doc",
        page_number=1,
        kind="sequence",
        human_verified=True,
        status="verified",
        seq_id_no="1",
        sequence="MKTG",
    )
    assert a.human_verified is True
    assert a.status == "verified"


def test_annotation_measurement_fields() -> None:
    a = Annotation(
        annotation_id="m1",
        document_id="sha256:doc",
        page_number=2,
        kind="measurement",
        human_verified=True,
        status="verified",
        metric="MIC",
        entity_raw="A",
        target_raw="E. coli",
        value_raw=">128",
        unit_raw="µg/mL",
    )
    assert a.metric == "MIC"
    assert a.entity_raw == "A"


def test_annotation_unreadable_kind() -> None:
    a = Annotation(
        annotation_id="u1",
        document_id="sha256:doc",
        page_number=1,
        kind="unreadable",
        human_verified=True,
        status="verified",
    )
    assert a.kind == "unreadable"


def test_annotation_rejects_unknown_field() -> None:
    with pytest.raises(Exception):
        Annotation(  # type: ignore[call-arg]
            annotation_id="a1",
            document_id="sha256:doc",
            page_number=1,
            kind="sequence",
            bogus_field="nope",
        )