"""Comprehensive tests for scientific validity gates (#2202, #2662, #2200, #2199)."""

from __future__ import annotations

import json
import sys
import textwrap
from datetime import datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import (
    CampaignError,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
)
from bathos.catalog import init_catalog, read_runs, write_run
from bathos.claim import (
    check_decision_scope,
    factor_coverage_check,
    parse_claim,
    register_claim,
    untested_assumptions,
    validate_claim,
)
from bathos.compact import compact
from bathos.linter import (
    IssueSeverity,
    check_invariants_declared,
    lint_project,
)
from bathos.runner import run_script
from bathos.schema import Run
from bathos.sidecar import (
    Sidecar,
    compute_evalue,
    evaluate_invariants,
    evaluate_sanity_assertions,
    parse_sidecar,
)
from bathos.validate import (
    check_factor_coverage,
    validate_result_against_sidecar,
    validate_sidecar,
)


def _setup_git_project(root: Path) -> None:
    """Create minimal .bth.toml and scripts/experiments layout."""
    (root / ".bth.toml").write_text(f'[project]\nslug = "testproj"\nroot = "{root}"\n')
    (root / "scripts" / "experiments").mkdir(parents=True, exist_ok=True)


# ---------------------------------------------------------------------------
# #2202 Part 1: [invariants] hard-fail assertions
# ---------------------------------------------------------------------------


class TestInvariantsGate:
    def test_parse_and_declaration_roundtrip(self, tmp_path: Path):
        p = tmp_path / "run_inv.bth.toml"
        p.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "Cells differ"
            [outcomes.pass]
            condition = "temp_std < 5"
            decision = "proceed"
            reasoning = "stable"
            [outcomes.fail]
            condition = "temp_std >= 5"
            decision = "stop"
            reasoning = "unstable"
            is_residual = true
            [invariants.cells_differ]
            condition = "cell_E != cell_F AND cell_F != cell_G"
            reasoning = "Must not collapse sweep cells"
            [result_schema]
            temp_std = "float"
            cell_E = "float"
            cell_F = "float"
            cell_G = "float"
        """)
        )
        sc = parse_sidecar(p)
        assert "cells_differ" in sc.invariants
        assert sc.invariants["cells_differ"].condition == "cell_E != cell_F AND cell_F != cell_G"
        assert sc.invariants["cells_differ"].reasoning == "Must not collapse sweep cells"

        rebuilt = Sidecar.from_declaration(sc.to_declaration())
        assert rebuilt.invariants == sc.invariants

    def test_evaluate_invariants_checks_all_no_short_circuit(self, tmp_path: Path):
        p = tmp_path / "run_inv.bth.toml"
        p.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "x > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [invariants.inv_a]
            condition = "a > 0"
            reasoning = "a must be positive"
            [invariants.inv_b]
            condition = "b != c"
            reasoning = "b and c must differ"
            [result_schema]
            x = "float"
            a = "float"
            b = "float"
            c = "float"
        """)
        )
        sc = parse_sidecar(p)
        assert evaluate_invariants(sc, {"x": 1.0, "a": 1.0, "b": 1.0, "c": 2.0}) == []
        assert evaluate_invariants(sc, {"x": 1.0, "a": -1.0, "b": 1.0, "c": 1.0}) == [
            "inv_a",
            "inv_b",
        ]
        # Empty result fails all declared invariants
        assert evaluate_invariants(sc, {}) == ["inv_a", "inv_b"]

    def test_validate_sidecar_invariants_rules_and_reserved_label(self, tmp_path: Path):
        # Reserved outcome label [outcomes.invariant_fail] is rejected
        reserved = tmp_path / "reserved.bth.toml"
        reserved.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.invariant_fail]
            condition = "x < 0"
            decision = "bad"
            reasoning = "r"
            is_residual = true
            [result_schema]
            x = "float"
        """)
        )
        res = validate_sidecar(parse_sidecar(reserved), sidecar_path=reserved)
        assert not res.ok
        assert any("reserved outcome label" in e.message for e in res.errors)

        # Missing reasoning & bad SQL in [invariants] fail validation
        bad_inv = tmp_path / "bad_inv.bth.toml"
        bad_inv.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "x > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [invariants.missing_reason]
            condition = "x = NULL"
            [invariants.bad_sql]
            condition = "SELECT FROM WHERE"
            reasoning = "bad"
            [invariants.no_schema_ref]
            condition = "1 = 1"
            reasoning = "warns when referencing no result_schema field"
            [result_schema]
            x = "float"
        """)
        )
        res2 = validate_sidecar(parse_sidecar(bad_inv), sidecar_path=bad_inv)
        assert not res2.ok
        assert any("Invalid SQL NULL comparison" in e.message for e in res2.errors)
        assert any("Missing 'reasoning'" in e.message for e in res2.errors)
        assert any("DuckDB parse error" in e.message for e in res2.errors)
        assert any("invariants.no_schema_ref.condition" in w for w in res2.warnings)

    def test_validate_result_against_sidecar_flags_violated_invariant(self, tmp_path: Path):
        p = tmp_path / "inv.bth.toml"
        p.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "x > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [invariants.nonzero_delta]
            condition = "delta > 0"
            reasoning = "must be nonzero"
            [result_schema]
            x = "float"
            delta = "float"
        """)
        )
        sc = parse_sidecar(p)
        errs, matched = validate_result_against_sidecar(sc, {"x": 2.0, "delta": 0.0})
        assert matched == "pass"
        assert any(e.field == "invariants.nonzero_delta" for e in errs)

    def test_runner_hard_fails_green_run_on_invariant_violation(self, tmp_path: Path):
        _setup_git_project(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        init_catalog(catalog_dir)

        script = tmp_path / "scripts" / "experiments" / "run_collapse.py"
        script.write_text(
            textwrap.dedent("""
            import json, os, sys
            val = float(sys.argv[1])
            json.dump({"temp_std": 1.2, "cell_E": 0.5, "cell_F": val}, open(os.environ["BTH_RESULTS_PATH"], "w"))
        """)
        )
        sidecar_path = script.with_suffix(".bth.toml")
        sidecar_path.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "Sweep cells remain distinct"
            stage_name = "validation"
            [outcomes.pass]
            condition = "temp_std < 5"
            decision = "proceed"
            reasoning = "within threshold"
            [outcomes.fail]
            condition = "temp_std >= 5"
            decision = "debug"
            reasoning = "too noisy"
            is_residual = true
            [invariants.cells_differ]
            condition = "cell_E != cell_F"
            reasoning = "collapsed cells indicate silent caching bug"
            [result_schema]
            temp_std = "float"
            cell_E = "float"
            cell_F = "float"
        """)
        )

        # Collapsed cell_F == cell_E -> outcome forced to invariant_fail despite temp_std < 5
        rc = run_script(
            argv=[sys.executable, str(script), "0.5"],
            project_slug="testproj",
            catalog_dir=catalog_dir,
            output_paths=[],
            tags=[],
            cwd=tmp_path,
        )
        assert rc == 0
        runs = read_runs(catalog_dir)
        assert len(runs) == 1
        assert runs[0].outcome == "invariant_fail"
        reason = json.loads(runs[0].outcome_error_reason)
        assert reason == {"violated_invariants": ["cells_differ"], "would_have_been": "pass"}

        # Distinct cell_F != cell_E -> outcome is pass
        rc2 = run_script(
            argv=[sys.executable, str(script), "0.9"],
            project_slug="testproj",
            catalog_dir=catalog_dir,
            output_paths=[],
            tags=[],
            cwd=tmp_path,
        )
        assert rc2 == 0
        runs2 = [r for r in read_runs(catalog_dir) if r.id != runs[0].id]
        assert runs2[0].outcome == "pass"

    def test_popper_evalue_neutral_on_invariant_fail(self, tmp_path: Path):
        catalog_dir = tmp_path / "catalog"
        init_catalog(catalog_dir)
        sidecar_path = tmp_path / "run_seq.bth.toml"
        sidecar_path.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "x > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "x <= 0"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [result_schema]
            x = "float"
            [popper]
            null_pass_rate = 0.25
            alt_pass_rate = 0.75
            stopping_threshold = 5.0
        """)
        )
        sc = parse_sidecar(sidecar_path)
        assert compute_evalue(sc, "invariant_fail") == 1.0
        assert compute_evalue(sc, "pass") == pytest.approx(3.0)

        r_inv = Run(
            project_slug="testproj",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            outcome="invariant_fail",
            sidecar_path=str(sidecar_path),
        )
        r_pass = Run(
            project_slug="testproj",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            status="completed",
            exit_code=0,
            outcome="pass",
            sidecar_path=str(sidecar_path),
        )
        write_run(r_inv, catalog_dir)
        write_run(r_pass, catalog_dir)
        compact(catalog_dir)

        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        try:
            camp = create_campaign(db, name="SeqInv", project_slug="testproj", mode="sequential")
            add_run_to_campaign(db, camp.id, r_inv.id, catalog_dir=catalog_dir)
            # invariant_fail is neutral: threshold not locked and evalue == 1.0
            row_inv = db.execute(
                "SELECT evalue FROM campaign_runs WHERE campaign_id=? AND run_id=?",
                [camp.id, r_inv.id],
            ).fetchone()
            assert row_inv is not None
            assert row_inv[0] == 1.0
            thresh_row = db.execute(
                "SELECT stopping_threshold FROM campaigns WHERE id=?", [camp.id]
            ).fetchone()
            assert thresh_row is not None
            assert thresh_row[0] is None

            add_run_to_campaign(db, camp.id, r_pass.id, catalog_dir=catalog_dir)
            rows = db.execute(
                "SELECT evalue FROM campaign_runs WHERE campaign_id=?", [camp.id]
            ).fetchall()
            product = 1.0
            for (ev,) in rows:
                product *= ev
            assert product == pytest.approx(3.0)
        finally:
            db.close()

    def test_linter_check_invariants_declared_advisory(self, tmp_path: Path):
        _setup_git_project(tmp_path)
        s = tmp_path / "scripts" / "experiments" / "run_plain.bth.toml"
        s.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            stage_name = "validation"
            [outcomes.pass]
            condition = "x > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [result_schema]
            x = "float"
        """)
        )
        issues = check_invariants_declared(tmp_path)
        assert len(issues) == 1
        assert issues[0].issue == "missing_invariants"
        assert issues[0].severity == IssueSeverity.WARNING


# ---------------------------------------------------------------------------
# #2202 Part 2: Confirmatory [sanity] pre-flight block
# ---------------------------------------------------------------------------


class TestConfirmatorySanityGate:
    def test_parse_and_validate_sanity_block(self, tmp_path: Path):
        p = tmp_path / "run_sanity.bth.toml"
        p.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            stage_name = "validation"
            [outcomes.pass]
            condition = "max_prob > 0.5"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [sanity]
            argv_suffix = ["--sanity-check"]
            [sanity.assertions.temperature_sharpens]
            condition = "max_prob > 0.9"
            reasoning = "catches inverted temperature convention"
            [result_schema]
            max_prob = "float"
        """)
        )
        sc = parse_sidecar(p)
        assert sc.sanity is not None
        assert sc.sanity.argv_suffix == ["--sanity-check"]
        assert "temperature_sharpens" in sc.sanity.assertions
        assert validate_sidecar(sc, sidecar_path=p).ok
        assert evaluate_sanity_assertions(sc.sanity, {"max_prob": 0.95}) == []
        assert evaluate_sanity_assertions(sc.sanity, {"max_prob": 0.2}) == ["temperature_sharpens"]

        rebuilt = Sidecar.from_declaration(sc.to_declaration())
        assert rebuilt.sanity == sc.sanity

    def test_validate_rejects_empty_sanity_assertions(self, tmp_path: Path):
        p = tmp_path / "bad_sanity.bth.toml"
        p.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "x > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [sanity]
            argv_suffix = []
            [result_schema]
            x = "float"
        """)
        )
        res = validate_sidecar(parse_sidecar(p), sidecar_path=p)
        assert not res.ok
        assert any(e.field == "sanity.argv_suffix" for e in res.errors)
        assert any(e.field == "sanity.assertions" for e in res.errors)

    def test_runner_sanity_failure_blocks_main_run_and_caches_pass(self, tmp_path: Path):
        _setup_git_project(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        init_catalog(catalog_dir)

        main_marker = tmp_path / "main_ran.txt"
        sanity_count = tmp_path / "sanity_count.txt"
        script = tmp_path / "scripts" / "experiments" / "run_temp.py"
        script.write_text(
            textwrap.dedent(f"""
            import json, os, sys
            from pathlib import Path
            if "--sanity-check" in sys.argv:
                c = Path({str(sanity_count)!r})
                n = int(c.read_text()) if c.exists() else 0
                c.write_text(str(n + 1))
                prob = 0.2 if os.environ.get("BREAK_SANITY") == "1" else 0.99
                json.dump({{"max_prob": prob}}, open(os.environ["BTH_RESULTS_PATH"], "w"))
            else:
                Path({str(main_marker)!r}).write_text("ran")
                json.dump({{"max_prob": 0.85}}, open(os.environ["BTH_RESULTS_PATH"], "w"))
        """)
        )
        script.with_suffix(".bth.toml").write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "Temperature sharpens softmax"
            stage_name = "validation"
            [outcomes.pass]
            condition = "max_prob > 0.7"
            decision = "go"
            reasoning = "sharp"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "flat"
            is_residual = true
            [sanity]
            argv_suffix = ["--sanity-check"]
            [sanity.assertions.sharp_one_hot]
            condition = "max_prob > 0.9"
            reasoning = "one-hot synthetic ground truth must sharpen"
            [result_schema]
            max_prob = "float"
        """)
        )

        # 1) Failing sanity pre-flight blocks main subprocess
        import os

        old_val = os.environ.get("BREAK_SANITY")
        os.environ["BREAK_SANITY"] = "1"
        try:
            rc = run_script(
                argv=[sys.executable, str(script)],
                project_slug="testproj",
                catalog_dir=catalog_dir,
                output_paths=[],
                tags=[],
                cwd=tmp_path,
            )
        finally:
            if old_val is None:
                os.environ.pop("BREAK_SANITY", None)
            else:
                os.environ["BREAK_SANITY"] = old_val

        assert rc == 1
        assert not main_marker.exists(), "Main script must NOT execute when [sanity] fails"
        runs = read_runs(catalog_dir)
        assert runs[0].outcome == "invalid_measurement"
        assert "sanity_check_failed" in runs[0].outcome_error_reason

        # 2) Passing sanity pre-flight executes main script and records status='passed'
        rc2 = run_script(
            argv=[sys.executable, str(script)],
            project_slug="testproj",
            catalog_dir=catalog_dir,
            output_paths=[],
            tags=[],
            cwd=tmp_path,
        )
        assert rc2 == 0
        assert main_marker.exists()
        assert sanity_count.read_text() == "2"
        pass_runs = [r for r in read_runs(catalog_dir) if r.outcome == "pass"]
        assert len(pass_runs) == 1
        assert json.loads(pass_runs[0].metadata)["sanity_check"]["status"] == "passed"

        # 3) Third run with unchanged script+sidecar hits cache and does NOT re-run sanity subprocess
        rc3 = run_script(
            argv=[sys.executable, str(script)],
            project_slug="testproj",
            catalog_dir=catalog_dir,
            output_paths=[],
            tags=[],
            cwd=tmp_path,
        )
        assert rc3 == 0
        assert sanity_count.read_text() == "2"
        cached_runs = [
            r
            for r in read_runs(catalog_dir)
            if r.outcome == "pass" and r.id != pass_runs[0].id
        ]
        assert json.loads(cached_runs[0].metadata)["sanity_check"]["status"] == "cached"


# ---------------------------------------------------------------------------
# #2202 Part 3: conclude_campaign assumption-status gate
# ---------------------------------------------------------------------------


class TestAssumptionStatusConcludeGate:
    def _setup_campaign_with_claim(self, tmp_path: Path, assumptions_toml: str, mode: str = "confirmation"):
        catalog_dir = tmp_path / "catalog"
        init_catalog(catalog_dir)
        compact(catalog_dir)
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        camp = create_campaign(db, name="AssumptionGate", project_slug="testproj", mode=mode)

        claim_path = tmp_path / "claim.bth.toml"
        claim_path.write_text(
            textwrap.dedent(f"""
            [claim]
            headline = "Substates differ"
            kill_condition = "No difference"
            kill_condition_satisfiable_by_null = false
            regime = "catalytic_triad"

            [[hypotheses]]
            id = "H_primary"
            label = "Substate sensitivity"

            [[hypotheses]]
            id = "H_null"
            label = "Null model"

            {assumptions_toml}

            [claim.union_gate]
        """)
        )
        register_claim(claim_path, camp.id, db, tmp_path)

        run_time = datetime.fromisoformat(camp.started_at) + timedelta(minutes=1)
        run = Run(
            project_slug="testproj",
            command="python x.py",
            argv=["python", "x.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=run_time,
            status="completed",
            exit_code=0,
            outcome="pass",
        )
        db.close()
        write_run(run, catalog_dir)
        compact(catalog_dir)
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        add_run_to_campaign(db, camp.id, run.id, catalog_dir=catalog_dir)
        return db, camp, claim_path

    def test_blocks_when_assumption_status_untested(self, tmp_path: Path):
        db, camp, claim_path = self._setup_campaign_with_claim(
            tmp_path,
            textwrap.dedent("""
            [[assumptions]]
            id = "A_adequate_power"
            label = "MDE below decision threshold"
            status = "untested"
            halt_if = "MDE exceeds decision-changing effect size"
        """),
        )
        try:
            claim = parse_claim(claim_path)
            assert len(untested_assumptions(claim)) == 1
            val_res = validate_claim(claim)
            assert any("A_adequate_power" in w for w in val_res.warnings)

            with pytest.raises(CampaignError, match="A_adequate_power"):
                conclude_campaign(db, camp.id, "pass", "done", workspace_root=tmp_path)
        finally:
            db.close()

    def test_allows_when_tested_or_waived_or_forced(self, tmp_path: Path):
        # 1) status = "tested" succeeds
        t1 = tmp_path / "t1"
        t1.mkdir()
        db, camp, _ = self._setup_campaign_with_claim(
            t1,
            '[[assumptions]]\nid = "A1"\nlabel = "Power"\nstatus = "tested"\n',
        )
        try:
            conclude_campaign(db, camp.id, "pass", "done", workspace_root=t1)
            assert (
                db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [camp.id]).fetchone()[
                    0
                ]
                == "pass"
            )
        finally:
            db.close()

        # 2) Explicit waive_reason on assumption succeeds
        t2 = tmp_path / "t2"
        t2.mkdir()
        db, camp, _ = self._setup_campaign_with_claim(
            t2,
            '[[assumptions]]\nid = "A1"\nlabel = "Power"\nstatus = "untested"\nwaive_reason = "Verified externally in pilot"\n',
        )
        try:
            conclude_campaign(db, camp.id, "pass", "done", workspace_root=t2)
            assert (
                db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [camp.id]).fetchone()[
                    0
                ]
                == "pass"
            )
        finally:
            db.close()

        # 3) force_verdict=True overrides with claim_mode='bypassed'
        t3 = tmp_path / "t3"
        t3.mkdir()
        db, camp, _ = self._setup_campaign_with_claim(
            t3,
            '[[assumptions]]\nid = "A1"\nlabel = "Power"\nstatus = "untested"\n',
        )
        try:
            conclude_campaign(db, camp.id, "pass", "done", workspace_root=t3, force_verdict=True)
            row = db.execute(
                "SELECT outcome_label, claim_mode FROM campaigns WHERE id=?", [camp.id]
            ).fetchone()
            assert row == ("pass", "bypassed")
        finally:
            db.close()


# ---------------------------------------------------------------------------
# #2662 (P1): Unmeasured prose data-quality / filtering claims
# ---------------------------------------------------------------------------


class TestProseDataQualityClaims:
    def test_x_free_prose_without_measured_count_and_invariant_fails(self, tmp_path: Path):
        _setup_git_project(tmp_path)
        s = tmp_path / "scripts" / "experiments" / "score_esmc.bth.toml"
        (tmp_path / "scripts" / "experiments" / "score_esmc.py").write_text("# script\n")
        s.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "215 unique X-free queries improve ESM-C scoring"
            stage_name = "validation"
            novel = true
            [outcomes.pass]
            condition = "auroc > 0.8"
            decision = "ship"
            reasoning = "strong signal"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "weak"
            is_residual = true
            [result_schema]
            auroc = "float"
        """)
        )
        res = validate_sidecar(parse_sidecar(s), sidecar_path=s)
        assert not res.ok
        assert any(e.field == "prose_data_quality" for e in res.errors)

        issues = lint_project(tmp_path)
        assert any(
            i.issue == "unmeasured_data_quality_claim" and i.severity == IssueSeverity.ERROR
            for i in issues
        )

    def test_count_in_schema_without_invariant_still_fails(self, tmp_path: Path):
        s = tmp_path / "score_esmc.bth.toml"
        s.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "Filtered dataset with no unknown tokens"
            [outcomes.pass]
            condition = "auroc > 0.8"
            decision = "ship"
            reasoning = "strong"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "weak"
            is_residual = true
            [result_schema]
            auroc = "float"
            n_unknown = "int"
        """)
        )
        res = validate_sidecar(parse_sidecar(s), sidecar_path=s)
        assert not res.ok
        assert any(e.field == "prose_data_quality" for e in res.errors)

    def test_measured_count_with_invariant_passes_and_no_false_positives(self, tmp_path: Path):
        _setup_git_project(tmp_path)
        (tmp_path / "scripts" / "experiments" / "score_esmc.py").write_text("# script\n")
        s = tmp_path / "scripts" / "experiments" / "score_esmc.bth.toml"
        s.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "215 unique X-free queries improve ESM-C scoring"
            stage_name = "validation"
            novel = true
            [outcomes.pass]
            condition = "auroc > 0.8"
            decision = "ship"
            reasoning = "strong signal"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "weak"
            is_residual = true
            [invariants.x_free]
            condition = "n_x == 0"
            reasoning = "fail closed if any query contains unknown token X"
            [result_schema]
            auroc = "float"
            n_x = "int"
        """)
        )
        res = validate_sidecar(parse_sidecar(s), sidecar_path=s)
        assert res.ok, res.errors
        issues = lint_project(tmp_path)
        assert not any(i.issue == "unmeasured_data_quality_claim" for i in issues)

        # Scientific phrases like "free energy", "scale-free", "zero-shot", "no effect" must NOT false-positive
        clean = tmp_path / "clean_sci.bth.toml"
        clean.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "Scale-free free energy landscape shows zero-shot transfer with no effect from noise"
            [outcomes.pass]
            condition = "delta > 0"
            decision = "go"
            reasoning = "model-free estimator works"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "no difference"
            is_residual = true
            [result_schema]
            delta = "float"
        """)
        )
        assert validate_sidecar(parse_sidecar(clean), sidecar_path=clean).ok


# ---------------------------------------------------------------------------
# #2200: Declared-vs-analyzed factor/arm coverage & n_units_informative
# ---------------------------------------------------------------------------


class TestFactorCoverageGate:
    def test_dropped_arm_caught_and_excluded_levels_reason_enforced(self, tmp_path: Path):
        p = tmp_path / "run_arms.bth.toml"
        p.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "Conditioning arms sensitivity"
            [factors.declared_levels]
            arm = ["unconditioned", "ligand_only", "ligand_and_sc", "sc_only"]
            [outcomes.pass]
            condition = "score > 0.5"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [result_schema]
            score = "float"
            analyzed_levels = "dict"
        """)
        )
        sc = parse_sidecar(p)
        assert validate_sidecar(sc, sidecar_path=p).ok

        # Negative: sc_only dropped without excluded_levels
        errs, _ = validate_result_against_sidecar(
            sc,
            {
                "score": 0.9,
                "analyzed_levels": {"arm": ["unconditioned", "ligand_only", "ligand_and_sc"]},
            },
        )
        assert any(e.field == "factor_coverage" and "sc_only" in e.message for e in errs)

        # Positive: sc_only explicitly excluded with reason
        p_exc = tmp_path / "run_arms_exc.bth.toml"
        p_exc.write_text(
            p.read_text()
            + '\n[factors.excluded_levels]\nsc_only = "intentionally omitted in pilot"\n'
        )
        sc_exc = parse_sidecar(p_exc)
        errs_ok, _ = validate_result_against_sidecar(
            sc_exc,
            {
                "score": 0.9,
                "analyzed_levels": {"arm": ["unconditioned", "ligand_only", "ligand_and_sc"]},
            },
        )
        assert errs_ok == []

        # Negative: blank excluded_levels reason fails validation
        p_blank = tmp_path / "run_arms_blank.bth.toml"
        p_blank.write_text(p.read_text() + '\n[factors.excluded_levels]\nsc_only = "   "\n')
        res_blank = validate_sidecar(parse_sidecar(p_blank), sidecar_path=p_blank)
        assert not res_blank.ok
        assert any("excluded_levels" in e.field for e in res_blank.errors)

    def test_n_units_informative_checks(self):
        errs, warns = check_factor_coverage({}, {"n_units": 512})
        assert any(e.field == "n_units_informative" for e in errs)

        errs2, warns2 = check_factor_coverage({}, {"n_units": 512, "n_units_informative": 100})
        assert errs2 == []
        assert any("low informative fraction" in w for w in warns2)

        errs3, _ = check_factor_coverage({}, {"n_units": 512, "n_units_informative": 600})
        assert any(e.field == "n_units_informative" for e in errs3)

    def test_linter_and_campaign_factor_coverage_check(self, tmp_path: Path):
        _setup_git_project(tmp_path)
        (tmp_path / "scripts" / "experiments" / "run_cov.py").write_text("# script\n")
        s = tmp_path / "scripts" / "experiments" / "run_cov.bth.toml"
        s.write_text(
            textwrap.dedent("""
            [experiment]
            hypothesis = "h"
            [outcomes.pass]
            condition = "n_units > 0"
            decision = "go"
            reasoning = "r"
            [outcomes.fail]
            condition = "1 = 1"
            decision = "stop"
            reasoning = "r"
            is_residual = true
            [result_schema]
            n_units = "int"
        """)
        )
        issues = lint_project(tmp_path)
        assert any(
            i.issue == "missing_n_units_informative" and i.severity == IssueSeverity.WARNING
            for i in issues
        )

        # Campaign conclude gate refuses when member run omits n_units_informative
        catalog_dir = tmp_path / ".bth" / "catalog"
        init_catalog(catalog_dir)
        compact(catalog_dir)
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        try:
            camp = create_campaign(db, name="FactorCov", project_slug="testproj", mode="confirmation")
            claim_path = tmp_path / "claim.bth.toml"
            claim_path.write_text(
                textwrap.dedent("""
                [claim]
                headline = "Test"
                kill_condition = "No diff"
                kill_condition_satisfiable_by_null = false
                [[hypotheses]]
                id = "H_main"
                label = "Main"
                [[hypotheses]]
                id = "H_null"
                label = "Null"
                [claim.union_gate]
            """)
            )
            register_claim(claim_path, camp.id, db, tmp_path, catalog_dir=catalog_dir)
            run_time = datetime.fromisoformat(camp.started_at) + timedelta(minutes=1)
            run = Run(
                project_slug="testproj",
                command="python x.py",
                argv=["python", "x.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=run_time,
                status="completed",
                exit_code=0,
                outcome="pass",
                metadata=json.dumps({"n_units": 512}),
            )
            db.close()
            write_run(run, catalog_dir)
            compact(catalog_dir)
            db = duckdb.connect(str(catalog_dir / "bathos.db"))
            add_run_to_campaign(db, camp.id, run.id, catalog_dir=catalog_dir)

            claim = parse_claim(claim_path)
            fc = factor_coverage_check(db, camp.id, claim, workspace_root=tmp_path, catalog_dir=catalog_dir)
            assert fc["verdict"] == "uncovered"
            with pytest.raises(CampaignError, match="factor/arm coverage"):
                conclude_campaign(db, camp.id, "pass", "done", workspace_root=tmp_path, catalog_dir=catalog_dir)
        finally:
            db.close()


# ---------------------------------------------------------------------------
# #2199: decision_scope vs regime + check_single_cell_gate in lint_project
# ---------------------------------------------------------------------------


class TestDecisionScopeAndSingleCellLint:
    def test_parse_validate_and_conclude_decision_scope(self, tmp_path: Path):
        catalog_dir = tmp_path / "catalog"
        init_catalog(catalog_dir)
        compact(catalog_dir)
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        try:
            camp = create_campaign(db, name="ScopeGate", project_slug="testproj", mode="confirmation")
            claim_path = tmp_path / "scope.claim.toml"
            claim_path.write_text(
                textwrap.dedent("""
                [claim]
                headline = "No substate sensitivity across fixed policies"
                kill_condition = "Delta > 0.1"
                kill_condition_satisfiable_by_null = false
                regime = "catalytic_triad and triad_shell10a"

                [claim.decision_scope]
                fixed_policy = ["catalytic_triad", "triad_shell10a"]

                [[hypotheses]]
                id = "H_primary"
                label = "Primary"

                [[hypotheses]]
                id = "H_null"
                label = "Null"

                [claim.union_gate]
            """)
            )
            claim = parse_claim(claim_path)
            assert claim.decision_scope == {"fixed_policy": ["catalytic_triad", "triad_shell10a"]}
            assert validate_claim(claim).ok

            register_claim(claim_path, camp.id, db, tmp_path, catalog_dir=catalog_dir)

            # Run 1 tests only catalytic_triad -> triad_shell10a is missing
            t0 = datetime.fromisoformat(camp.started_at)
            r1 = Run(
                project_slug="testproj",
                command="python r1.py",
                argv=["python", "r1.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=t0 + timedelta(minutes=1),
                status="completed",
                exit_code=0,
                outcome="pass",
                metadata=json.dumps({"fixed_policy": "catalytic_triad"}),
            )
            db.close()
            write_run(r1, catalog_dir)
            compact(catalog_dir)
            db = duckdb.connect(str(catalog_dir / "bathos.db"))
            add_run_to_campaign(db, camp.id, r1.id, catalog_dir=catalog_dir)

            res = check_decision_scope(db, camp.id, claim)
            assert res["verdict"] == "uncovered"
            assert res["uncovered"][0]["missing_levels"] == ["triad_shell10a"]

            with pytest.raises(CampaignError, match="decision_scope"):
                conclude_campaign(db, camp.id, "pass", "done", workspace_root=tmp_path, catalog_dir=catalog_dir)

            # Run 2 covers triad_shell10a -> conclude succeeds
            r2 = Run(
                project_slug="testproj",
                command="python r2.py",
                argv=["python", "r2.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=t0 + timedelta(minutes=2),
                status="completed",
                exit_code=0,
                outcome="pass",
                metadata=json.dumps({"fixed_policy": "triad_shell10a"}),
            )
            db.close()
            write_run(r2, catalog_dir)
            compact(catalog_dir)
            db = duckdb.connect(str(catalog_dir / "bathos.db"))
            add_run_to_campaign(db, camp.id, r2.id, catalog_dir=catalog_dir)

            assert check_decision_scope(db, camp.id, claim)["verdict"] == "covered"
            conclude_campaign(db, camp.id, "pass", "done", workspace_root=tmp_path, catalog_dir=catalog_dir)
            out_row = db.execute(
                "SELECT outcome_label FROM campaigns WHERE id=?", [camp.id]
            ).fetchone()
            assert out_row is not None
            assert out_row[0] == "pass"
        finally:
            db.close()

    def test_decision_scope_flags_position_set_hash_conflict(self, tmp_path: Path):
        catalog_dir = tmp_path / "catalog"
        init_catalog(catalog_dir)
        compact(catalog_dir)
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        try:
            camp = create_campaign(db, name="HashConflict", project_slug="testproj", mode="confirmation")
            claim_path = tmp_path / "scope.claim.toml"
            claim_path.write_text(
                textwrap.dedent("""
                [claim]
                headline = "h"
                kill_condition = "k"
                kill_condition_satisfiable_by_null = false
                regime = "shell"
                [claim.decision_scope]
                fixed_policy = ["shell"]
                [[hypotheses]]
                id = "H_a"
                label = "A"
                [[hypotheses]]
                id = "H_null"
                label = "Null"
                [claim.union_gate]
            """)
            )
            claim = parse_claim(claim_path)
            register_claim(claim_path, camp.id, db, tmp_path, catalog_dir=catalog_dir)
            t0 = datetime.fromisoformat(camp.started_at)
            r1 = Run(
                project_slug="testproj",
                command="python r1.py",
                argv=["python", "r1.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=t0 + timedelta(minutes=1),
                status="completed",
                exit_code=0,
                outcome="pass",
                metadata=json.dumps(
                    {"fixed_policy": "shell", "position_set_hashes": {"shell_10a": "hash_81res"}}
                ),
            )
            r2 = Run(
                project_slug="testproj",
                command="python r2.py",
                argv=["python", "r2.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=t0 + timedelta(minutes=2),
                status="completed",
                exit_code=0,
                outcome="pass",
                metadata=json.dumps(
                    {"fixed_policy": "shell", "position_set_hashes": {"shell_10a": "hash_25res"}}
                ),
            )
            db.close()
            write_run(r1, catalog_dir)
            write_run(r2, catalog_dir)
            compact(catalog_dir)
            db = duckdb.connect(str(catalog_dir / "bathos.db"))
            add_run_to_campaign(db, camp.id, r1.id, catalog_dir=catalog_dir)
            add_run_to_campaign(db, camp.id, r2.id, catalog_dir=catalog_dir)

            res = check_decision_scope(db, camp.id, claim)
            assert res["verdict"] == "uncovered"
            assert res["hash_conflicts"][0]["label"] == "shell_10a"
            with pytest.raises(CampaignError, match="multiple hashes"):
                conclude_campaign(db, camp.id, "pass", "done", workspace_root=tmp_path, catalog_dir=catalog_dir)
        finally:
            db.close()

    def test_lint_project_wires_check_single_cell_gate(self, tmp_path: Path, monkeypatch):
        monkeypatch.delenv("BTH_CATALOG_DIR", raising=False)
        _setup_git_project(tmp_path)
        catalog_dir = tmp_path / ".bth" / "catalog"
        init_catalog(catalog_dir)
        compact(catalog_dir)
        db = duckdb.connect(str(catalog_dir / "bathos.db"))
        try:
            camp = create_campaign(db, name="SingleCell", project_slug="testproj", mode="confirmation")
            t0 = datetime.fromisoformat(camp.started_at)
            r1 = Run(
                project_slug="testproj",
                command="python r1.py",
                argv=["python", "r1.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=t0 + timedelta(minutes=1),
                status="completed",
                exit_code=0,
                metadata=json.dumps({"temperature": "300K"}),
            )
            r2 = Run(
                project_slug="testproj",
                command="python r2.py",
                argv=["python", "r2.py"],
                git_hash="abc",
                git_branch="main",
                git_dirty=False,
                timestamp=t0 + timedelta(minutes=2),
                status="completed",
                exit_code=0,
                metadata=json.dumps({"temperature": "300K"}),
            )
            db.close()
            write_run(r1, catalog_dir)
            write_run(r2, catalog_dir)
            compact(catalog_dir)
            db = duckdb.connect(str(catalog_dir / "bathos.db"))
            add_run_to_campaign(db, camp.id, r1.id, catalog_dir=catalog_dir)
            add_run_to_campaign(db, camp.id, r2.id, catalog_dir=catalog_dir)
        finally:
            db.close()

        issues = lint_project(tmp_path)
        assert any(
            i.issue == "single-cell-gate smell" and i.severity == IssueSeverity.WARNING
            for i in issues
        )
