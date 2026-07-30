"""Tests for the [review] sidecar block (build-order step 2): parse + validate, advisory."""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from bathos.sidecar import (
    ImplementationReview,
    LiteratureReview,
    ReviewBlock,
    parse_sidecar,
    review_tier,
)
from bathos.validate import validate_sidecar

BASE_OUTCOMES = """
[outcomes.pass]
condition = "x < 5"
decision = "go"
reasoning = "r"

[outcomes.fail]
condition = "x >= 5"
decision = "stop"
reasoning = "r"
is_residual = true

[result_schema]
x = "float"
"""


def make(tmp_path: Path, review: str) -> Path:
    p = tmp_path / "s.bth.toml"
    p.write_text(
        textwrap.dedent('[experiment]\nhypothesis = "h"\n') + review + BASE_OUTCOMES,
        encoding="utf-8",
    )
    return p


# ── parsing ──────────────────────────────────────────────────────────────────


def test_absent_review_block_is_none(tmp_path):
    assert parse_sidecar(make(tmp_path, "")).review is None


def test_parses_literature_and_implementation_entries(tmp_path):
    sc = parse_sidecar(
        make(
            tmp_path,
            """
[[review.literature]]
ref = "10.1038/s41586-021-03819-2"
claim = "reports 87% GDT-TS on CASP14"
bears_on = "H1"
disposition = "supports"
checked = "2026-07-30"

[[review.implementation]]
source = "https://github.com/org/repo"
commit = "a1b2c3d"
what_was_checked = "loss reduction over batch dim"
bears_on = "C2"
disposition = "diverges"
""",
        )
    )
    assert len(sc.review.literature) == 1
    assert len(sc.review.implementation) == 1
    assert sc.review.literature[0].ref.startswith("10.1038")
    assert sc.review.literature[0].disposition == "supports"
    assert sc.review.implementation[0].commit == "a1b2c3d"
    assert sc.review.implementation[0].bears_on == "C2"


# ── tier derivation (mechanically graded, never declared) ────────────────────


def test_tier_is_empty_when_no_review():
    assert review_tier(None) == ""
    assert review_tier(ReviewBlock()) == ""


def test_bare_citation_grades_c0():
    r = ReviewBlock(literature=[LiteratureReview(ref="10.1/x", claim="says x")])
    assert review_tier(r) == "C0"


def test_bears_on_plus_disposition_grades_c1():
    r = ReviewBlock(
        literature=[
            LiteratureReview(ref="10.1/x", claim="c", bears_on="H1", disposition="supports")
        ]
    )
    assert review_tier(r) == "C1"


def test_pinned_implementation_read_grades_c1():
    r = ReviewBlock(
        implementation=[ImplementationReview(source="u", commit="abc", what_was_checked="the loss")]
    )
    assert review_tier(r) == "C1"


def test_implementation_without_a_pinned_commit_stays_c0():
    """An unpinned read is a citation, not a review — nothing anchors what was actually read."""
    r = ReviewBlock(implementation=[ImplementationReview(source="u", what_was_checked="the loss")])
    assert review_tier(r) == "C0"


def test_tier_cannot_be_declared_by_the_author(tmp_path):
    """A sidecar asserting its own tier must not be able to inflate it."""
    sc = parse_sidecar(
        make(
            tmp_path,
            '\n[review]\ntier = "C2"\n\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n',
        )
    )
    assert review_tier(sc.review) == "C0"  # graded from content, not from the declared field


# ── validation ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "block,expect_field,expect_fragment",
    [
        (
            '\n[[review.literature]]\nclaim = "c"\n',
            "review.literature[0]",
            "needs a 'ref'",
        ),
        (
            '\n[[review.literature]]\nref = "10.1/x"\n',
            "review.literature[0]",
            "needs a 'claim'",
        ),
        (
            '\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\ndisposition = "nope"\n',
            "review.literature[0]",
            "is not one of",
        ),
        (
            '\n[[review.implementation]]\nwhat_was_checked = "w"\n',
            "review.implementation[0]",
            "needs a 'source'",
        ),
        (
            '\n[[review.implementation]]\nsource = "u"\n',
            "review.implementation[0]",
            "needs 'what_was_checked'",
        ),
        (
            '\n[[review.implementation]]\nsource = "u"\nwhat_was_checked = "w"\ndisposition = "nope"\n',
            "review.implementation[0]",
            "is not one of",
        ),
    ],
)
def test_structural_validation_errors(tmp_path, block, expect_field, expect_fragment):
    res = validate_sidecar(parse_sidecar(make(tmp_path, block)))
    hits = [e for e in res.errors if e.field == expect_field and expect_fragment in e.message]
    assert hits, (
        f"expected {expect_fragment!r} on {expect_field}; got {[(e.field, e.message) for e in res.errors]}"
    )


def test_a_well_formed_review_block_adds_no_errors(tmp_path):
    res = validate_sidecar(
        parse_sidecar(
            make(
                tmp_path,
                '\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n'
                'bears_on = "H1"\ndisposition = "supports"\n',
            )
        )
    )
    assert [e for e in res.errors if e.field.startswith("review.")] == []


# ── D2: bears_on is optional until confirmatory ──────────────────────────────


def test_bears_on_is_not_required_without_a_claim(tmp_path):
    """D2: a sidecar authored before any claim exists must still validate."""
    res = validate_sidecar(
        parse_sidecar(make(tmp_path, '\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n'))
    )
    assert [e for e in res.errors if e.field.startswith("review.")] == []


def test_bears_on_must_resolve_when_a_claim_is_supplied(tmp_path):
    class FakeClaim:
        hypotheses = [{"id": "H1"}]
        confounds = [{"id": "C1"}]

    sc = parse_sidecar(
        make(
            tmp_path,
            '\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n'
            'bears_on = "H_NOPE"\ndisposition = "supports"\n',
        )
    )
    res = validate_sidecar(sc, claim=FakeClaim())
    assert any("is not a hypothesis or confound id" in e.message for e in res.errors)

    sc_ok = parse_sidecar(
        make(
            tmp_path,
            '\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n'
            'bears_on = "C1"\ndisposition = "supports"\n',
        )
    )
    res_ok = validate_sidecar(sc_ok, claim=FakeClaim())
    assert [e for e in res_ok.errors if e.field.startswith("review.")] == []


# ── regression: [review] must work on every sidecar kind ─────────────────────


@pytest.mark.parametrize(
    "header",
    [
        '[experiment]\nhypothesis = "h"\n',
        '[benchmark]\nbaseline_ref = "abc"\nmetric = "ns_per_day"\n',
        '[validation]\nproperty = "p"\nreference = "r"\ntolerance = "1%"\n',
        '[debug]\nsymptom = "s"\nsuspected_cause = "c"\nverification = "v"\n',
    ],
)
def test_review_parses_for_every_sidecar_kind(tmp_path, header):
    """Regression: [review] parsing was nested inside the experiment-only branch, so a
    benchmark/validation/debug sidecar silently parsed it to None — contradicting the design
    ("a benchmark or validation script can equally rest on prior work")."""
    p = tmp_path / "s.bth.toml"
    p.write_text(
        header + '\n[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n' + BASE_OUTCOMES,
        encoding="utf-8",
    )
    sc = parse_sidecar(p)
    assert sc.review is not None, "review block was dropped for this sidecar kind"
    assert len(sc.review.literature) == 1


def test_non_table_review_is_ignored_not_a_crash(tmp_path):
    """`review = "text"` is legal TOML; calling .get() on it raised AttributeError."""
    p = tmp_path / "s.bth.toml"
    p.write_text(
        '[experiment]\nhypothesis = "h"\nreview = "not a table"\n' + BASE_OUTCOMES,
        encoding="utf-8",
    )
    sc = parse_sidecar(p)  # must not raise
    assert sc.review is None or sc.review.is_empty()


def test_review_as_array_is_ignored_not_a_crash(tmp_path):
    p = tmp_path / "s.bth.toml"
    p.write_text(
        '[experiment]\nhypothesis = "h"\nreview = [1, 2]\n' + BASE_OUTCOMES, encoding="utf-8"
    )
    sc = parse_sidecar(p)
    assert sc.review is None or sc.review.is_empty()
