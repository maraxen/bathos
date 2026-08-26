"""Blast-radius ledger tests (Phase 1, backlog #4551, spec 260826).

Mirrors tests/test_trust_ledger.py's TestAppendAndFoldLatestWins shape, adapted for
the (entity_type, entity_id) composite key instead of content_hash.
"""

from __future__ import annotations

import pytest

from bathos.blast_radius import (
    BlastRadiusRecord,
    append_ledger_record,
    fold_blast_radius_state,
    latest_ledger_record,
    read_ledger_fragments,
)


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


class TestAppendAndFoldLatestWins:
    def test_fold_returns_clean_when_never_flagged(self, catalog_dir):
        assert fold_blast_radius_state(catalog_dir, "run", "run-nonexistent") == "clean"

    def test_single_append_is_visible_via_fold(self, catalog_dir):
        record = BlastRadiusRecord(
            entity_type="run",
            entity_id="run-001",
            from_state="clean",
            to_state="affected",
            match_reason="touches src/bathos/checker.py",
        )
        append_ledger_record(record, catalog_dir)

        assert fold_blast_radius_state(catalog_dir, "run", "run-001") == "affected"

    def test_supersede_resolves_to_latest_record(self, catalog_dir):
        first = BlastRadiusRecord(
            entity_type="run",
            entity_id="run-002",
            from_state="clean",
            to_state="affected",
            amended_at="2026-08-26T00:00:00+00:00",
        )
        second = BlastRadiusRecord(
            entity_type="run",
            entity_id="run-002",
            from_state="affected",
            to_state="cleared",
            reason="re-ran post-fix, identical output",
            amended_at="2026-08-26T01:00:00+00:00",
        )
        append_ledger_record(first, catalog_dir)
        append_ledger_record(second, catalog_dir)

        assert fold_blast_radius_state(catalog_dir, "run", "run-002") == "cleared"
        latest = latest_ledger_record(catalog_dir, "run", "run-002")
        assert latest is not None
        assert latest.reason == "re-ran post-fix, identical output"

    def test_composite_key_does_not_cross_entity_types(self, catalog_dir):
        """Same entity_id, different entity_type, must not collide (forward-compat
        with Phase 2's campaign/claim records sharing id-space with run ids)."""
        append_ledger_record(
            BlastRadiusRecord(entity_type="run", entity_id="shared-id", to_state="affected"),
            catalog_dir,
        )
        assert fold_blast_radius_state(catalog_dir, "campaign", "shared-id") == "clean"
        assert fold_blast_radius_state(catalog_dir, "run", "shared-id") == "affected"

    def test_ledger_is_append_only_both_records_present(self, catalog_dir):
        append_ledger_record(
            BlastRadiusRecord(
                entity_type="run", entity_id="run-003", to_state="affected",
                amended_at="2026-08-26T00:00:00+00:00",
            ),
            catalog_dir,
        )
        append_ledger_record(
            BlastRadiusRecord(
                entity_type="run", entity_id="run-003", to_state="cleared",
                amended_at="2026-08-26T01:00:00+00:00",
            ),
            catalog_dir,
        )

        all_records = read_ledger_fragments(catalog_dir)
        matching = [r for r in all_records if r.entity_id == "run-003"]
        assert len(matching) == 2, "append-only: both records must survive"


class TestSurvivesCompactForceRebuild:
    def test_survives_force_rebuild(self, catalog_dir):
        from bathos.compact import compact as compact_catalog

        append_ledger_record(
            BlastRadiusRecord(entity_type="run", entity_id="run-004", to_state="affected"),
            catalog_dir,
        )
        assert fold_blast_radius_state(catalog_dir, "run", "run-004") == "affected"

        result = compact_catalog(catalog_dir, force_rebuild=True)
        assert result is not None

        assert fold_blast_radius_state(catalog_dir, "run", "run-004") == "affected"


class TestReadLedgerFragmentsRobustness:
    """Regression (code-review finding, PR #54): a leftover .tmp.parquet file
    (write_ledger_fragment interrupted between the tmp-write and the atomic
    rename) must not be read as a real fragment, and a genuinely corrupt final
    fragment must not crash the whole read (which bathos.compact.compact()
    calls unconditionally for every project's data)."""

    def test_leftover_tmp_fragment_is_ignored(self, catalog_dir):
        append_ledger_record(
            BlastRadiusRecord(entity_type="run", entity_id="run-005", to_state="affected"),
            catalog_dir,
        )
        frag_dir = catalog_dir / "blast_radius"
        (frag_dir / "blast_radius_deadbeef.tmp.parquet").write_bytes(b"not a real parquet file")

        # Must not raise, and must not pick up the tmp file as a phantom record.
        records = read_ledger_fragments(catalog_dir)
        assert all(r.entity_id != "" for r in records)
        assert len(records) == 1
        assert records[0].entity_id == "run-005"

    def test_corrupt_final_fragment_is_skipped_not_fatal(self, catalog_dir):
        append_ledger_record(
            BlastRadiusRecord(entity_type="run", entity_id="run-006", to_state="affected"),
            catalog_dir,
        )
        frag_dir = catalog_dir / "blast_radius"
        (frag_dir / "blast_radius_corrupt123.parquet").write_bytes(b"garbage, not parquet")

        # Must not raise despite the corrupt fragment; the good record still reads.
        records = read_ledger_fragments(catalog_dir)
        assert any(r.entity_id == "run-006" for r in records)


class TestNewPhase2Columns:
    """Phase 2a (backlog #4552): matched_clauses/shadow_verdict columns, additive
    on the same table -- proven forward-compatible by Phase 1's own
    test_composite_key_does_not_cross_entity_types (entity_type="claim" was
    always a legal value, just unused until now)."""

    def test_matched_clauses_and_shadow_verdict_round_trip(self, catalog_dir):
        record = BlastRadiusRecord(
            entity_type="claim",
            entity_id="camp-001",
            to_state="affected",
            matched_clauses='["clause-a", "clause-b"]',
            shadow_verdict='{"kind": "output_sha_still_matches", "verdict": "clean"}',
        )
        append_ledger_record(record, catalog_dir)

        latest = latest_ledger_record(catalog_dir, "claim", "camp-001")
        assert latest is not None
        assert latest.matched_clauses == '["clause-a", "clause-b"]'
        assert latest.shadow_verdict == '{"kind": "output_sha_still_matches", "verdict": "clean"}'

    def test_existing_warm_table_migrates_via_compact(self, catalog_dir):
        """A blast_radius_ledger table created by Phase-1-era code (no
        matched_clauses/shadow_verdict columns) must gain them via compact(),
        the same ALTER TABLE ADD COLUMN IF NOT EXISTS pattern used elsewhere in
        compact.py for campaigns/etc."""
        import duckdb

        con = duckdb.connect(str(catalog_dir / "bathos.db"))
        con.execute("""
            CREATE TABLE blast_radius_ledger (
                id TEXT PRIMARY KEY, entity_type TEXT NOT NULL, entity_id TEXT NOT NULL,
                from_state TEXT, to_state TEXT NOT NULL, anchor_kind TEXT, anchor_value TEXT,
                matched_files TEXT, match_reason TEXT, reason TEXT, amended_at TEXT NOT NULL
            )
        """)
        con.close()

        from bathos.compact import compact as compact_catalog

        compact_catalog(catalog_dir)  # must not raise on the pre-existing short-column table

        append_ledger_record(
            BlastRadiusRecord(
                entity_type="claim", entity_id="camp-002", to_state="affected",
                matched_clauses='["x"]',
            ),
            catalog_dir,
        )
        assert fold_blast_radius_state(catalog_dir, "claim", "camp-002") == "affected"
