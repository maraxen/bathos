"""Tests for the Review Coverage Gate (build-order step 3)."""

from __future__ import annotations

import duckdb
import pytest

from bathos.claim import review_coverage_check


class FakeClaim:
    def __init__(self, hyps=(), confs=()):
        self.hypotheses = [{"id": h} for h in hyps]
        self.confounds = [{"id": c} for c in confs]


SIDECAR = """
[experiment]
hypothesis = "h"
{review}
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


@pytest.fixture
def db():
    con = duckdb.connect(":memory:")
    con.execute("CREATE TABLE runs (id VARCHAR, sidecar_path VARCHAR)")
    con.execute("CREATE TABLE campaign_runs (campaign_id VARCHAR, run_id VARCHAR)")
    return con


def add_run(db, tmp_path, name, review_block):
    p = tmp_path / f"{name}.bth.toml"
    p.write_text(SIDECAR.format(review=review_block), encoding="utf-8")
    db.execute("INSERT INTO runs VALUES (?, ?)", [name, str(p)])
    db.execute("INSERT INTO campaign_runs VALUES (?, ?)", ["camp1", name])
    return p


def test_empty_slate_is_not_vacuously_covered(db):
    """§4: an empty required-set must be uncovered/error, never 'covered'."""
    res = review_coverage_check(db, "camp1", FakeClaim())
    assert res["verdict"] == "empty_slate"
    assert res["covered"] == []


def test_uncovered_when_no_review_entries(db, tmp_path):
    add_run(db, tmp_path, "r1", "")
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"
    assert res["uncovered"] == ["hypothesis:H1"]
    assert res["entries_seen"] == 0


def test_covered_when_every_id_is_named(db, tmp_path):
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.literature]]\nref = "10.1/x"\nclaim = "c"\nbears_on = "H1"\ndisposition = "supports"\n'
        '[[review.implementation]]\nsource = "u"\ncommit = "abc"\nwhat_was_checked = "w"\nbears_on = "C1"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"], confs=["C1"]))
    assert res["verdict"] == "covered"
    assert res["uncovered"] == []
    assert res["entries_seen"] == 2


def test_partial_coverage_reports_exactly_what_is_missing(db, tmp_path):
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.literature]]\nref = "10.1/x"\nclaim = "c"\nbears_on = "H1"\ndisposition = "supports"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1", "H2"], confs=["C1"]))
    assert res["verdict"] == "uncovered"
    assert set(res["uncovered"]) == {"hypothesis:H2", "confound:C1"}
    assert res["covered"] == ["hypothesis:H1"]


def test_coverage_accumulates_across_member_runs(db, tmp_path):
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.literature]]\nref = "a"\nclaim = "c"\nbears_on = "H1"\ndisposition = "supports"\n',
    )
    add_run(
        db,
        tmp_path,
        "r2",
        '[[review.literature]]\nref = "b"\nclaim = "c"\nbears_on = "H2"\ndisposition = "supports"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1", "H2"]))
    assert res["verdict"] == "covered"
    assert res["sidecars_read"] == 2


def test_an_entry_without_bears_on_covers_nothing(db, tmp_path):
    """A bare citation (C0) is not coverage — it names nothing to cover."""
    add_run(db, tmp_path, "r1", '[[review.literature]]\nref = "10.1/x"\nclaim = "c"\n')
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"
    assert res["entries_seen"] == 1  # seen, but it covers nothing


def test_a_bears_on_only_entry_is_not_coverage(db, tmp_path):
    """The gate's original defect: `bears_on` alone marked an id covered.

    An entry consisting of nothing but a binding asserts that *something* was reviewed without
    saying what, what it found, or where to check — so authoring empty placeholders was a way
    to satisfy the gate outright. Coverage now requires the C1 bar (`sidecar.covering_id`).
    """
    add_run(db, tmp_path, "r1", '[[review.literature]]\nbears_on = "H1"\n')
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"
    assert res["uncovered"] == ["hypothesis:H1"]
    assert res["entries_seen"] == 1  # counted as seen, but it covers nothing


def test_an_uncited_literature_entry_is_not_coverage(db, tmp_path):
    """`bears_on` + `disposition` but no `ref` — a verdict on an unnamed source.

    C0 is "DOI + claim", so without this an entry could grade C1 while failing C0's own content
    bar. Unfalsifiable in principle: there is nothing to go and check.
    """
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.literature]]\nclaim = "c"\nbears_on = "H1"\ndisposition = "supports"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"
    assert res["entries_seen"] == 1


def test_a_literature_entry_without_a_disposition_is_not_coverage(db, tmp_path):
    """A citation bound to an id but carrying no finding is C0 — it reports no result."""
    add_run(
        db, tmp_path, "r1", '[[review.literature]]\nref = "10.1/x"\nclaim = "c"\nbears_on = "H1"\n'
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"


def test_an_unpinned_implementation_entry_is_not_coverage(db, tmp_path):
    """No `commit` — nothing anchors which revision was actually read."""
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.implementation]]\nsource = "u"\nwhat_was_checked = "w"\nbears_on = "H1"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"


def test_a_pinned_implementation_read_that_names_no_id_covers_nothing(db, tmp_path):
    """C1 on its own merits, but it binds to nothing — a good entry is not automatically coverage.

    This is why `covering_id` exists separately from `implementation_entry_reviewed`: the tier
    bar and the coverage bar are related but distinct questions.
    """
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.implementation]]\nsource = "u"\ncommit = "abc"\nwhat_was_checked = "w"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["verdict"] == "uncovered"
    assert res["entries_seen"] == 1


def test_unreadable_sidecar_is_counted_not_silently_treated_as_absent(db, tmp_path):
    """An unreadable sidecar is not evidence review is missing — the caller must see both."""
    db.execute("INSERT INTO runs VALUES (?, ?)", ["gone", str(tmp_path / "missing.bth.toml")])
    db.execute("INSERT INTO campaign_runs VALUES (?, ?)", ["camp1", "gone"])
    add_run(
        db,
        tmp_path,
        "r1",
        '[[review.literature]]\nref = "a"\nclaim = "c"\nbears_on = "H1"\ndisposition = "supports"\n',
    )
    res = review_coverage_check(db, "camp1", FakeClaim(hyps=["H1"]))
    assert res["sidecars_unreadable"] == 1
    assert res["sidecars_read"] == 1
    assert res["verdict"] == "covered"


def test_gate_has_no_numeric_threshold():
    """The gate is binary by construction — §7 gated this step to avoid a guessed constant."""
    import inspect

    src = inspect.getsource(review_coverage_check)
    body = src.split('"""')[2]  # skip the docstring
    assert "0." not in body, "a float literal in the gate body suggests an uncalibrated threshold"


# ── §8b objection 2: the contradicted / indeterminate truth table ───────────


class DiscClaim:
    hypotheses = [{"id": "H1"}, {"id": "H2"}]
    confounds: list = []
    discriminability = [
        {
            "hypothesis_a": "H1",
            "hypothesis_b": "H2",
            "planned_run_label": "fail",
            "predicted_outcome": "H2",
        },
        {
            "hypothesis_a": "H1",
            "hypothesis_b": "H2",
            "planned_run_label": "pass",
            "predicted_outcome": "H1",
        },
    ]


def test_supports_citation_contradicted_by_the_observed_outcome():
    from bathos.claim import citation_contradicted

    assert citation_contradicted(DiscClaim(), "H1", "fail") == "contradicted"


def test_supports_citation_consistent_with_the_observed_outcome():
    from bathos.claim import citation_contradicted

    assert citation_contradicted(DiscClaim(), "H1", "pass") == "consistent"


def test_uncovered_label_is_indeterminate_not_consistent():
    """§8b: silence must read as neither confirmation nor refutation."""
    from bathos.claim import citation_contradicted

    assert citation_contradicted(DiscClaim(), "H1", "marginal") == "indeterminate"


def test_empty_discriminability_makes_everything_indeterminate():
    """discriminability is optional, so a confirmatory claim may carry an empty map."""
    from bathos.claim import citation_contradicted

    class Empty:
        discriminability: list = []

    assert citation_contradicted(Empty(), "H1", "fail") == "indeterminate"


def test_missing_inputs_are_indeterminate():
    from bathos.claim import citation_contradicted

    assert citation_contradicted(DiscClaim(), "", "fail") == "indeterminate"
    assert citation_contradicted(DiscClaim(), "H1", "") == "indeterminate"


def test_contradicted_citations_reports_evaluable_separately(db, tmp_path):
    """A trigger that CANNOT fire must be distinguishable from one that found nothing."""
    from bathos.claim import contradicted_citations

    db.execute("ALTER TABLE runs ADD COLUMN outcome VARCHAR")
    p = tmp_path / "r1.bth.toml"
    p.write_text(
        '[experiment]\nhypothesis = "h"\n'
        '[[review.literature]]\nref = "10.1/x"\nclaim = "c"\nbears_on = "H1"\n'
        'disposition = "supports"\n'
        '[outcomes.pass]\ncondition = "x < 5"\ndecision = "go"\nreasoning = "r"\n'
        '[outcomes.fail]\ncondition = "x >= 5"\ndecision = "s"\nreasoning = "r"\nis_residual = true\n'
        '[result_schema]\nx = "float"\n',
        encoding="utf-8",
    )
    db.execute("INSERT INTO runs VALUES (?, ?, ?)", ["r1", str(p), "fail"])
    db.execute("INSERT INTO campaign_runs VALUES (?, ?)", ["camp1", "r1"])

    res = contradicted_citations(db, "camp1", DiscClaim())
    assert res["supports_seen"] == 1
    assert len(res["contradicted"]) == 1
    assert res["contradicted"][0]["bears_on"] == "H1"
    assert res["evaluable"] == 1
