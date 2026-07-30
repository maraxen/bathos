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
