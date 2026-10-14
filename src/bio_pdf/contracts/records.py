"""Domain record contracts: sequences, measurements, relations, checks."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import Field, StringConstraints, model_validator

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

EntityKind = Literal[
    "peptide",
    "protein",
    "antibody",
    "antibody_chain",
    "variant",
    "compound",
    "control",
    "unknown",
]

ChainName = Literal["VH", "VL", "heavy", "light", "CDR1", "CDR2", "CDR3", "other"]


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

    Provenance: ``source_kind`` says where the candidate came from — a
    listing block, a prose fragment, a table cell or an attachment — and
    ``sequence_scope`` says whether it claims to be a full listing entry or
    a fragment of a natural moiety. Residue-level notes (D/L, modifications,
    linkers, retro-inverso) live in ``residue_annotations`` and never alter
    ``normalized_sequence`` silently.
    """

    sequence_id: NonEmptyStr
    document_id: NonEmptyStr
    seq_id_no: SeqIdNo | None = None
    molecule_type: MoleculeType
    representation: Representation

    entity_id: NonEmptyStr | None = None
    source_kind: Literal[
        "listing", "inline_fragment", "table", "reference_attachment"
    ] = "inline_fragment"
    sequence_scope: Literal[
        "fragment", "full_listing", "undetermined"
    ] = "undetermined"
    residue_annotations: list[dict[str, str]] = Field(default_factory=list)
    source_refs: list[NonEmptyStr] = Field(default_factory=list)

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


class Entity(_StrictModel):
    """A named entity (peptide, antibody, compound, …) in a document.

    ``chain_map`` carries explicit chain → seq_id_no assignments for
    antibody entities (e.g. ``{"VH": "1", "VL": "2"}``). This is the only
    signal :func:`link_explicit_relations` uses to pair chains — it never
    infers a pairing from name similarity.
    """

    entity_id: NonEmptyStr
    document_id: NonEmptyStr
    name_raw: NonEmptyStr
    kind: EntityKind = "unknown"
    aliases: list[NonEmptyStr] = Field(default_factory=list)
    chain_map: dict[str, str] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyStr] = Field(default_factory=list)


class RelationResult(_StrictModel):
    """The result of relation linking.

    ``relations`` holds confirmed candidates; ``unresolved_references``
    keeps the references whose target sequence was not found (never
    fabricated); ``review_reasons`` explains what needs human attention.
    """

    relations: list[SequenceRelation] = Field(default_factory=list)
    unresolved_references: list[SequenceReference] = Field(default_factory=list)
    review_reasons: list[NonEmptyStr] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Activities
# ---------------------------------------------------------------------------


class ActivityValue(_StrictModel):
    """The structured representation of an activity value.

    ``kind`` discriminates how ``number``/``lower``/``upper``/``uncertainty``
    are interpreted. ``raw`` always retains the original string from the
    source so reviewers can audit the parse.

    Structural invariants (plan B05) are enforced on construction:
    numbers are finite decimals, a range has ``lower <= upper``,
    ``uncertainty >= 0``, and fields from other branches must stay empty
    (no scalar carrying range fields, no categorical with a number).
    """

    kind: Literal["scalar", "range", "plus_minus", "categorical", "unparsed"]
    operator: Literal["<", "<=", ">", ">=", "=", "~"] | None = None
    number: str | None = Field(default=None, description="Decimal string for scalar/operator.")
    lower: str | None = None
    upper: str | None = None
    uncertainty: str | None = None
    qualifier: NonEmptyStr | None = None
    raw: NonEmptyStr

    @model_validator(mode="after")
    def _check_invariants(self) -> "ActivityValue":
        from decimal import Decimal, InvalidOperation

        def dec(name: str, text: str | None) -> Decimal | None:
            if text is None:
                return None
            try:
                value = Decimal(text)
            except InvalidOperation as exc:
                raise ValueError(f"{name}={text!r} is not a decimal") from exc
            if not value.is_finite():
                raise ValueError(f"{name}={text!r} is not finite")
            return value

        number = dec("number", self.number)
        lower = dec("lower", self.lower)
        upper = dec("upper", self.upper)
        uncertainty = dec("uncertainty", self.uncertainty)

        if self.kind == "range":
            if lower is None or upper is None:
                raise ValueError("range requires lower and upper")
            if lower > upper:
                raise ValueError(f"range lower {self.lower} > upper {self.upper}")
            if self.number is not None or uncertainty is not None:
                raise ValueError("range must not carry number/uncertainty")
        elif self.kind == "plus_minus":
            if number is None or uncertainty is None:
                raise ValueError("plus_minus requires number and uncertainty")
            if uncertainty < 0:
                raise ValueError("uncertainty must be >= 0")
            if lower is not None or upper is not None:
                raise ValueError("plus_minus must not carry range fields")
        elif self.kind == "scalar":
            if number is None:
                raise ValueError("scalar requires number")
            if lower is not None or upper is not None or uncertainty is not None:
                raise ValueError("scalar must not carry range/uncertainty fields")
        elif self.kind == "categorical":
            if self.qualifier is None:
                raise ValueError("categorical requires qualifier")
            if number is not None or lower is not None or upper is not None:
                raise ValueError("categorical must not carry numeric fields")
        return self


class Measurement(_StrictModel):
    """A measurement linking an entity to a value with evidence.

    Field-level evidence is required for ``value``/``unit``/``entity``/
    ``target`` so that reviewers can audit which cell or paragraph
    contributed each component.

    Entity identity: ``entity_id`` is the id of an Entity object (resolvable
    in the job's entity list) or None. The *original surface text* lives in
    ``entity_raw`` — legacy table rows that stuffed a row label into
    ``entity_id`` are mapped by compat to ``entity_raw`` and are NOT
    considered linked. ``metric_raw`` keeps the source spelling (``IC₅₀``,
    ``MIC50``) while ``metric`` holds the canonical registry name.
    """

    measurement_id: NonEmptyStr
    document_id: NonEmptyStr
    entity_id: NonEmptyStr | None = None
    entity_raw: NonEmptyStr | None = None
    sequence_ids: list[NonEmptyStr] = Field(default_factory=list)
    assay_id: NonEmptyStr | None = None
    metric: NonEmptyStr
    metric_raw: NonEmptyStr | None = None
    value: ActivityValue

    unit_raw: NonEmptyStr | None = None
    unit_normalized: NonEmptyStr | None = None

    target_raw: NonEmptyStr | None = None
    target_id: NonEmptyStr | None = None
    conditions: list[dict[str, str]] = Field(default_factory=list)

    evidence_kind: EvidenceKind = "undetermined"
    field_evidence: dict[str, list[NonEmptyStr]] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)
    checks: list[CheckResult] = Field(default_factory=list)

    validation_state: ValidationState = "unchecked"
    acceptance_state: AcceptanceState = "candidate"


class AssayRecord(_StrictModel):
    """An experimental method that produced measurements.

    Extracted from methods sections, captions and table legends. Every
    condition field must point back to its source evidence — a method
    paragraph never silently becomes THE assay for the whole document.
    """

    assay_id: NonEmptyStr
    document_id: NonEmptyStr
    method_raw: NonEmptyStr
    target_raw: NonEmptyStr | None = None
    conditions: list[dict[str, str]] = Field(default_factory=list)
    condition_evidence: dict[str, list[NonEmptyStr]] = Field(default_factory=dict)
    evidence_ids: list[NonEmptyStr] = Field(min_length=1)


class MetricDefinition(_StrictModel):
    """Registry entry describing what a metric means and requires.

    ``value_kind`` constrains which ActivityValue shapes are acceptable;
    the requirement flags drive validation (an S/R/I susceptibility result
    has no concentration unit; MIC without a unit is incomplete). Registry
    names are canonical; the source spelling stays on the record as
    ``metric_raw``.
    """

    name: NonEmptyStr
    value_kind: Literal[
        "numeric", "categorical", "numeric_or_categorical"
    ] = "numeric"
    unit_requirement: Literal["required", "optional", "not_applicable"] = "required"
    target_requirement: Literal["required", "optional", "not_applicable"] = "optional"
    entity_requirement: Literal["required", "optional", "not_applicable"] = "required"
    allowed_units: list[NonEmptyStr] = Field(default_factory=list)
    description: NonEmptyStr | None = None


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