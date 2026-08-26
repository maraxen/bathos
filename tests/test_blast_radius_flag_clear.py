"""Blast-radius flag/clear tests (AC-6, AC-9, backlog #4551)."""

from __future__ import annotations

import json

import pytest

from bathos.blast_radius import (
    BlastRadiusMatch,
    BlastRadiusReport,
    clear_blast_radius_flag,
    compute_shadow_auto_clear_verdict,
    flag_blast_radius,
    fold_blast_radius_state,
)
from bathos.catalog import write_run
from bathos.schema import Run


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


def _report(affected=(), unverifiable=()):
    return BlastRadiusReport(
        anchor_kind="commit",
        anchor_value="deadbeef",
        changed_files=["src/foo.py"],
        affected=list(affected),
        unverifiable=list(unverifiable),
        unaffected_run_ids=[],
    )


class TestFlagBlastRadius:
    def test_flags_affected_runs(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-a", git_hash="abc123", command="src/foo.py",
            matched_files=["src/foo.py"], reason="touches src/foo.py",
        )
        records = flag_blast_radius(_report(affected=[match]), catalog_dir)

        assert len(records) == 1
        assert records[0].entity_type == "run"
        assert records[0].entity_id == "run-a"
        assert records[0].to_state == "affected"
        assert json.loads(records[0].matched_files) == ["src/foo.py"]
        assert fold_blast_radius_state(catalog_dir, "run", "run-a") == "affected"

    def test_flags_unverifiable_runs_distinctly(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-b", git_hash="abc123", command="src/foo.py",
            matched_files=["src/foo.py"], reason="DIRTY_RUN, cannot verify",
        )
        records = flag_blast_radius(_report(unverifiable=[match]), catalog_dir)

        assert records[0].to_state == "unverifiable"
        assert fold_blast_radius_state(catalog_dir, "run", "run-b") == "unverifiable"

    def test_records_from_state_from_prior_flag(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-c", git_hash="abc", command="c", matched_files=["c"], reason="r",
        )
        flag_blast_radius(_report(unverifiable=[match]), catalog_dir)
        records = flag_blast_radius(_report(affected=[match]), catalog_dir)

        assert records[0].from_state == "unverifiable"
        assert records[0].to_state == "affected"


class TestClearBlastRadiusFlag:
    def test_clear_requires_nonempty_reason(self, catalog_dir):
        with pytest.raises(ValueError):
            clear_blast_radius_flag(catalog_dir, "run", "run-d", reason="")
        with pytest.raises(ValueError):
            clear_blast_radius_flag(catalog_dir, "run", "run-d", reason="   ")

    def test_clear_transitions_to_cleared(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-e", git_hash="abc", command="c", matched_files=["c"], reason="r",
        )
        flag_blast_radius(_report(affected=[match]), catalog_dir)

        record = clear_blast_radius_flag(
            catalog_dir, "run", "run-e", reason="re-ran post-fix, identical output"
        )

        assert record.to_state == "cleared"
        assert record.from_state == "affected"
        assert fold_blast_radius_state(catalog_dir, "run", "run-e") == "cleared"

    def test_clear_on_never_flagged_entity_still_works(self, catalog_dir):
        """Clearing something that was never flagged is a legal no-op-ish action --
        from_state is 'clean', to_state becomes 'cleared'. Not an error: the caller
        may be pre-emptively documenting that they checked."""
        record = clear_blast_radius_flag(
            catalog_dir, "run", "run-f", reason="manually verified, was never affected"
        )
        assert record.from_state == "clean"
        assert record.to_state == "cleared"


class TestShadowAutoClear:
    """AC-10 (Phase 2a, #4552): the shadow auto-clear verdict is computed and
    stored, but NEVER changes to_state -- it is observational only, calibrated
    over time before ever being trusted to act (spec Decision Log #5)."""

    def test_shadow_verdict_clean_when_output_sha_matches(self, tmp_path):
        import hashlib

        out_file = tmp_path / "out.json"
        out_file.write_text("result data")
        sha256 = hashlib.sha256(out_file.read_bytes()).hexdigest()

        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False,
            output_metadata=json.dumps([{"path": str(out_file), "sha256": sha256}]),
        )
        verdict = compute_shadow_auto_clear_verdict(run)
        assert verdict["kind"] == "output_sha_still_matches"
        assert verdict["verdict"] == "clean"

    def test_shadow_verdict_drifted_when_output_sha_mismatches(self, tmp_path):
        out_file = tmp_path / "out.json"
        out_file.write_text("changed data")

        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False,
            output_metadata=json.dumps([{"path": str(out_file), "sha256": "f" * 64}]),
        )
        verdict = compute_shadow_auto_clear_verdict(run)
        assert verdict["verdict"] == "drifted"

    def test_shadow_verdict_no_outputs_recorded(self):
        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False,
        )
        verdict = compute_shadow_auto_clear_verdict(run)
        assert verdict["verdict"] == "no_outputs_recorded"

    def test_flag_blast_radius_records_shadow_verdict_without_applying_it(self, catalog_dir):
        run = Run(
            project_slug="p", command="x", argv=["x"], git_hash="abc", git_branch="main",
            git_dirty=False,
        )
        write_run(run, catalog_dir)

        match = BlastRadiusMatch(
            run_id=run.id, git_hash="abc", command="x", campaign_id="",
            matched_files=["x"], reason="r",
        )
        records = flag_blast_radius(_report(affected=[match]), catalog_dir)

        assert records[0].to_state == "affected"  # NOT auto-cleared
        assert records[0].shadow_verdict is not None
        assert json.loads(records[0].shadow_verdict)["verdict"] == "no_outputs_recorded"

    def test_unverifiable_matches_get_no_shadow_verdict(self, catalog_dir):
        match = BlastRadiusMatch(
            run_id="run-unverifiable", git_hash="abc", command="x", campaign_id="",
            matched_files=["x"], reason="r",
        )
        records = flag_blast_radius(_report(unverifiable=[match]), catalog_dir)
        assert records[0].shadow_verdict is None
