"""Domain record contracts: sequences, measurements, relations, checks."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints

from bio_pdf.contracts.common import (
    BoundingBox,
    EvidenceSpan,
    NonEmptyStr,
    PageNumber,
    _StrictModel,
)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

MoleculeType = Literal["DNA", "RNA", "protein", "unknown"]
Representation = Literal["one_letter", "three_letter", "modified", "unresolved"]

# Evidence-kind labels per design §6.5. ``undetermined`` is the catch-all for
# evidence types we cannot classify yet (e.g. cited-but-not-reproduced
# numbers in the prose).
EvidenceKind = Literal[
    "measured",
    "claim_threshold",
    "general_statement",
    "cited_result",
    "undetermined",
]

ValidationState = Literal[
    "unchecked",
    "checks_passed",
    "conflict",
    "verified_reference",
    "verified_human",
]

AcceptanceState = Literal[
    "candidate",
    "needs_review",
    "accepted",
    "rejected",
]

# Sequence references come from ST.25 / SEQ ID mentions. We keep the value as
# a string so different patent styles (``SEQ ID NO: 7`` vs. ``SEQ ID NOs: 7-9``)
# both parse, but downstream consumers must validate the format.
SeqIdNo = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=64),
]

RelationPredicate = Literal[
    "has_sequence",
    "heavy_chain",
    "light_chain",
    "has_cdr",
    "variant_of",
    "assayed_entity",
]

CheckOutcome = Literal["pass", "fail", "not_applicable", "unknown"]


# ---------------------------------------------------------------------------
# Check + Reference helpers
# ---------------------------------------------------------------------------


class CheckResult(_StrictModel):
    """A single structured check on a record.

    The ``check_id`` mirrors the IDs in design §6.6. ``outcome`` is the
    discrete result and ``evidence_ids`` points to the evidence that
    triggered the decision. We deliberately keep ``details`` as a free-form
    string dict so future checks can attach operator-specific notes without
    schema churn.
    """

    check_id: NonEmptyStr
    outcome: CheckOutcome
    details: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)


class EvidenceRef(_StrictModel):
    """A reference to an :class:`EvidenceSpan` from a record.

    Records may share evidence (e.g. a measurement and its row header).
    """

    detail: NonEmptyStr
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)


# ---------------------------------------------------------------------------
# Sequences
# ---------------------------------------------------------------------------


class SequenceRecord(_StrictModel):
    """A sequence candidate extracted from a page.

    The record carries both the original raw form (e.g. ST.25 three-letter
    blocks) and the normalized one-letter form. ``declared_length`` and
    ``observed_length`` are stored independently so incomplete trailing
    segments remain honest.
    """

    sequence_id: NonEmptyStr
    document_id: NonEmptyStr
    seq_id_no: SeqIdNo | None = None
    molecule_type: MoleculeType
    representation: Representation

    declared_length: int | None = Field(default=None, ge=0)
    observed_length: int | None = Field(default=None, ge=0)
    raw_sequence: NonEmptyStr
    normalized_sequence: NonEmptyStr

    residues: list[dict[str, str]] = Field(default_factory=list)
    organism_raw: NonEmptyStr | None = None
    feature_notes: list[NonEmptyStr] = Field(default_factory=list)

    is_complete: bool = False
    is_explicit_sequence: bool = True

    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    checks: list[CheckResult] = Field(default_factory=list)

    validation_state: ValidationState = "unchecked"
    acceptance_state: AcceptanceState = "candidate"


class SequenceReference(_StrictModel):
    """A SEQ ID mention that has not been resolved to a sequence.

    Resolved references keep the same shape but set ``resolved_sequence_id``
    to the linked :class:`SequenceRecord`.
    """

    document_id: NonEmptyStr
    seq_id_no: SeqIdNo
    mention_raw: NonEmptyStr
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    resolved_sequence_id: NonEmptyStr | None = None


class SequenceRelation(_StrictModel):
    """An explicit relation between two records or entities.

    ``predicate`` controls the semantic; cross-language consumers must
    accept the exact set in :data:`RelationPredicate`.
    """

    relation_id: NonEmptyStr
    document_id: NonEmptyStr
    subject_id: NonEmptyStr
    predicate: RelationPredicate
    object_id: NonEmptyStr
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    validation_state: ValidationState = "unchecked"


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


class ActivityValue(_StrictModel):
    """The structured representation of an activity value.

    ``kind`` discriminates how ``number``/``lower``/``upper``/``uncertainty``
    are interpreted. ``raw`` always retains the original string from the
    source so reviewers can audit the parse.
    """

    kind: Literal["scalar", "range", "plus_minus", "categorical", "unparsed"]
    operator: Literal["<", "<=", ">", ">=", "=", "~"] | None = None
    number: str | None = Field(default=None, description="Decimal string for scalar/operator.")
    lower: str | None = None
    upper: str | None = None
    uncertainty: str | None = None
    qualifier: NonEmptyStr | None = None
    raw: NonEmptyStr


class Measurement(_StrictModel):
    """A measurement linking an entity to a value with evidence.

    Field-level evidence is required for ``value``/``unit``/``entity``/
    ``target`` so that reviewers can audit which cell or paragraph
    contributed each component.
    """

    measurement_id: NonEmptyStr
    document_id: NonEmptyStr
    entity_id: NonEmptyStr | None = None
    sequence_ids: list[NonEmptyStr] = Field(default_factory=list)
    assay_id: NonEmptyStr | None = None
    metric: NonEmptyStr
    value: ActivityValue

    unit_raw: NonEmptyStr | None = None
    unit_normalized: NonEmptyStr | None = None

    target_raw: NonEmptyStr | None = None
    conditions: list[dict[str, str]] = Field(default_factory=list)

    evidence_kind: EvidenceKind = "undetermined"
    field_evidence: dict[str, list[NonEmptyStr]] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    checks: list[CheckResult] = Field(default_factory=list)

    validation_state: ValidationState = "unchecked"
    acceptance_state: AcceptanceState = "candidate"


# ---------------------------------------------------------------------------
# Evidence convenience aggregator
# ---------------------------------------------------------------------------


class EvidenceBundle(_StrictModel):
    """A collection of :class:`EvidenceSpan` records.

    Centralises the per-document evidence index so domain rules and
    validators can pull all spans for a job without re-reading artifact
    files.
    """

    spans: list[EvidenceSpan] = Field(default_factory=list)

    def by_id(self, evidence_id: str) -> EvidenceSpan | None:
        for span in self.spans:
            if span.evidence_id == evidence_id:
                return span
        return None