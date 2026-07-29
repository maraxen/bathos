"""#2276 verification: claim_discriminates/claim_isolates survive ingest and gate consumers read them.

Findings (post-B1 INSERT fix):
- AC-04/AC-05 (validate_claim): read the claim TOML discriminability matrix only — NOT warm columns.
- AC-06 (check_single_cell_gate): reads runs.metadata via campaign_runs — NOT claim_discriminates.
  metadata is warm-only (excluded from COOL_SCHEMA); compact from cool fragments writes metadata='{}'.
  AC-06 was therefore NOT affected by the claim_discriminates NULL-on-ingest bug.
- Signal 12 (sprint_audit): checks campaigns.claim_path — NOT claim_discriminates.
- Union Gate (run_union_gate / conclude): DOES read warm runs.claim_discriminates — was the
  production path broken by the pre-B1 NULL-on-ingest bug; fixed by compact.py INSERT columns.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import duckdb

from bathos.catalog import init_catalog, write_run
from bathos.claim import parse_claim, run_union_gate, validate_claim
from bathos.compact import compact
from bathos.schema import Run

_UNION_GATE_CLAIM = """[claim]
headline = "Discriminability ingest check"
kill_condition = "fail"
kill_condition_satisfiable_by_null = false

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[assumptions]]
id = "A1"
label = "Assumption"

[[confounds]]
id = "C1"
label = "Confound"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
"""


def test_union_gate_reads_claim_discriminates_after_compact(
    tmp_catalog: Path, sample_run: Run, tmp_path: Path
):
    """Union Gate must see claim_discriminates after cool→warm compact (B1 fix regression)."""
    init_catalog(tmp_catalog)
    campaign_id = "camp-2276-union-gate-test"

    run = dataclasses.replace(
        sample_run,
        campaign_id=campaign_id,
        claim_discriminates=json.dumps(["H_primary", "H_null"]),
        claim_isolates=json.dumps(["C1"]),
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        row = db.execute(
            "SELECT claim_discriminates, claim_isolates FROM runs WHERE id = ?",
            [run.id],
        ).fetchone()
        assert row is not None
        assert row[0] == json.dumps(["H_primary", "H_null"])
        assert row[1] == json.dumps(["C1"])

        (tmp_path / "claim.toml").write_text(_UNION_GATE_CLAIM)
        claim = parse_claim(tmp_path / "claim.toml")

        verdict, uncovered = run_union_gate(db, campaign_id, claim)
        assert verdict == "covered"
        assert uncovered == []
    finally:
        db.close()


def test_union_gate_confounded_when_discriminates_null_after_compact(
    tmp_catalog: Path, sample_run: Run, tmp_path: Path
):
    """Document pre-B1 failure mode: NULL claim_discriminates → Union Gate confounded."""
    init_catalog(tmp_catalog)
    campaign_id = "camp-2276-null-discriminates"

    run = dataclasses.replace(
        sample_run,
        campaign_id=campaign_id,
        claim_discriminates=None,
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        row = db.execute(
            "SELECT claim_discriminates FROM runs WHERE id = ?", [run.id]
        ).fetchone()
        assert row[0] is None

        (tmp_path / "claim.toml").write_text(_UNION_GATE_CLAIM)
        claim = parse_claim(tmp_path / "claim.toml")

        verdict, uncovered = run_union_gate(db, campaign_id, claim)
        assert verdict == "confounded"
        assert "C_main" in uncovered
    finally:
        db.close()


def test_ac04_ac05_read_claim_file_not_warm_discriminates_column(tmp_path: Path):
    """AC-04/05 fire from claim TOML alone; no warm DB or claim_discriminates column needed."""
    claim_path = tmp_path / "bias.claim.toml"
    claim_path.write_text(
        """[claim]
headline = "Bias check"
kill_condition = "fail"
kill_condition_satisfiable_by_null = false

[[hypotheses]]
id = "H_primary"
label = "Primary"

[[hypotheses]]
id = "H_null"
label = "Null"

[[assumptions]]
id = "A1"
label = "Assumption"

[[confounds]]
id = "C1"
label = "Confound"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "same"

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_alt"
planned_run_label = "main"
predicted_outcome = "same"

[claim.union_gate]
"""
    )
    claim = parse_claim(claim_path)
    result = validate_claim(claim, db=None)

    assert result.ok is True
    assert any("zero discriminative power" in w for w in result.warnings)
    assert any("positive-testing bias" in w for w in result.warnings)


def test_metadata_not_preserved_from_cool_fragments(tmp_catalog: Path, sample_run: Run):
    """metadata is warm-only; AC-06 was never affected by claim_discriminates ingest bug."""
    init_catalog(tmp_catalog)
    run = dataclasses.replace(
        sample_run,
        metadata=json.dumps({"temperature": "300K"}),
    )
    write_run(run, tmp_catalog)
    compact(tmp_catalog)

    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    try:
        row = db.execute("SELECT metadata FROM runs WHERE id = ?", [run.id]).fetchone()
        assert row[0] == "{}"
    finally:
        db.close()


# ---------------------------------------------------------------------------
# #3717: the ORIGIN link -- sidecar [experiment] -> Sidecar -> Run.
#
# The tests above cover cool->warm (the #2276 "B1" compact fix). They all build a
# Run with claim_discriminates set BY HAND, so they could not and did not catch
# that NOTHING EVER SET IT in the production path: `SELECT count(*),
# count(claim_discriminates) FROM runs` returned 1418 and 0 against the live asr
# catalog. The Union Gate maps a run onto a claim clause via this field, so an
# always-NULL column left every clause unmapped and downgraded every
# confirmation-mode campaign to `confounded` regardless of design -- a gate that
# returns the same verdict for every possible input. Only surfaced when asr ran
# its first confirmation campaign; exploration mode is warn-only.
# ---------------------------------------------------------------------------


def _experiment_sidecar(claim_lines: str) -> str:
    import textwrap

    return textwrap.dedent(f"""
        [experiment]
        hypothesis = "test hypothesis"
{claim_lines}
        [outcomes.pass]
        condition = "x == 1"
        decision = "good"
        reasoning = "expected behavior"
        [outcomes.fallback]
        condition = "1==1"
        decision = "other"
        reasoning = "catch-all"
        is_residual = true
        [result_schema]
        x = "float"
    """)


def _run_with_sidecar(tmp_path: Path, sidecar_text: str):
    import sys

    from bathos.catalog import read_runs
    from bathos.runner import run_script

    (tmp_path / "catalog").mkdir()
    enforced = tmp_path / "scripts" / "experiments"
    enforced.mkdir(parents=True)
    script = enforced / "run_x.py"
    script.write_text("print('hi')")
    (enforced / "run_x.bth.toml").write_text(sidecar_text)

    rc = run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_path / "catalog",
        output_paths=[],
        tags=[],
        cwd=tmp_path,
    )
    assert rc == 0
    return read_runs(tmp_path / "catalog")


def test_claim_discriminates_reaches_run_record_from_sidecar(tmp_path: Path):
    """A sidecar declaring claim_discriminates/claim_isolates must populate the run record."""
    runs = _run_with_sidecar(
        tmp_path,
        _experiment_sidecar(
            '        claim_discriminates = ["H_primary", "H_null"]\n'
            '        claim_isolates = ["C_batch_effect"]'
        ),
    )
    assert len(runs) == 1
    assert json.loads(runs[0].claim_discriminates) == ["H_primary", "H_null"]
    assert json.loads(runs[0].claim_isolates) == ["C_batch_effect"]


def test_claim_discriminates_absent_stays_null(tmp_path: Path):
    """A sidecar declaring nothing leaves both columns NULL.

    Absent must stay absent: an empty list would read as "this run declares coverage of no
    clauses", which is the same thing, but NULL is what the warm schema and every existing
    consumer expect. This also pins that the fix did not start writing '[]' everywhere.
    """
    runs = _run_with_sidecar(tmp_path, _experiment_sidecar(""))
    assert len(runs) == 1
    assert runs[0].claim_discriminates is None
    assert runs[0].claim_isolates is None
