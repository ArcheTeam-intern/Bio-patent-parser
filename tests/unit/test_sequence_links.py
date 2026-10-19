"""Candidate entity → SEQ ID links (never a silent hard link).

A patent's sequence listing numbers declarations (<210> 4) while its
activity tables name compounds ("Chain 104"). The prose is the only place
they meet:

    "Met10"-peptide (Chain 100) defined by SEQ ID NO: 4

The extractor turns those statements into CANDIDATES and never writes
``Measurement.sequence_ids``: a wrong link is worse than none, so each
candidate carries its source sentence and stays unconfirmed.
"""

from __future__ import annotations

from bio_pdf.extract.sequences.links import extract_sequence_links

_PAGE4 = [
    {
        "text": (
            'Figure 4a shows an alignment of 25 variants of "Met10"-peptide '
            "(Chain 100) defined by SEQ ID NO: 4. Figure 4b shows an alignment "
            'of 23 variants of "Met11"-peptide (Chain 200) defined by SEQ ID NO: 5.'
        ),
        "page_number": 4,
        "block_id": "p4:b3",
    }
]


def test_family_statement_links_the_whole_hundred_block() -> None:
    links = extract_sequence_links(
        _PAGE4, [{"entity_raw": "104"}, {"entity_raw": "105"}, {"entity_raw": "201"}]
    )
    by_entity = {c["entity_raw"]: c["seq_id_no"] for c in links["candidates"]}
    assert by_entity == {"104": "4", "105": "4", "201": "5"}


def test_exact_statement_links_only_that_entity() -> None:
    blocks = [
        {
            "text": "Variant Chain 312 is encoded by SEQ ID NO: 9.",
            "page_number": 7,
            "block_id": "p7:b1",
        }
    ]
    links = extract_sequence_links(
        blocks, [{"entity_raw": "312"}, {"entity_raw": "313"}, {"entity_raw": "412"}]
    )
    assert [c["entity_raw"] for c in links["candidates"]] == ["312"]
    assert links["candidates"][0]["match_kind"] == "exact"


def test_every_candidate_carries_its_source() -> None:
    links = extract_sequence_links(_PAGE4, [{"entity_raw": "104"}])
    candidate = links["candidates"][0]
    assert candidate["page_number"] == 4
    assert candidate["block_id"] == "p4:b3"
    assert "SEQ ID NO: 4" in candidate["source_text"]
    assert candidate["status"] == "candidate"
    assert "human must confirm" in links["note"]


def test_no_statement_no_candidate() -> None:
    """Proximity alone is never evidence: an entity with no linking
    sentence in the SAME block produces nothing."""

    blocks = [
        {"text": "Chain 104 showed strong activity.", "page_number": 23, "block_id": "p23:b1"},
        {"text": "The listing is defined by SEQ ID NO: 4.", "page_number": 29, "block_id": "p29:b9"},
    ]
    links = extract_sequence_links(blocks, [{"entity_raw": "104"}])
    assert links["candidates"] == []


def test_ambiguous_entity_keeps_both_candidates() -> None:
    blocks = [
        {
            "text": "Chain 104 is defined by SEQ ID NO: 4.",
            "page_number": 4,
            "block_id": "p4:b1",
        },
        {
            "text": "In another embodiment Chain 104 corresponds to SEQ ID NO: 7.",
            "page_number": 9,
            "block_id": "p9:b2",
        },
    ]
    links = extract_sequence_links(blocks, [{"entity_raw": "104"}])
    seq_ids = sorted(c["seq_id_no"] for c in links["candidates"])
    assert seq_ids == ["4", "7"]


def test_chain_id_and_seq_spellings_are_tolerated() -> None:
    blocks = [
        {
            "text": "The peptide of Chain ID 205 is given in SEQ ID No. 6.",
            "page_number": 5,
            "block_id": "p5:b4",
        }
    ]
    links = extract_sequence_links(blocks, [{"entity_raw": "Chain205"}])
    assert links["candidates"][0]["seq_id_no"] == "6"


def test_non_numeric_entities_are_not_linked() -> None:
    links = extract_sequence_links(
        _PAGE4, [{"entity_raw": "Escherichia coli"}, {"entity_raw": None}]
    )
    assert links["candidates"] == []
