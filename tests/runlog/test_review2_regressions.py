"""Regressions for the step-2b review: one mode read per batch; no legacy ingest with the flag on."""

from __future__ import annotations

from pathlib import Path

import duckdb

import bathos.runlog.emit as emit_mod
from bathos.blast_radius import BlastRadiusMatch, BlastRadiusReport, flag_blast_radius


def _match(run_id: str) -> BlastRadiusMatch:
    return BlastRadiusMatch(
        run_id=run_id,
        git_hash="abc123",
        command="src/foo.py",
        matched_files=["src/foo.py"],
        reason="touches src/foo.py",
    )


def test_flag_blast_radius_reads_mode_once_per_batch(tmp_path: Path, monkeypatch):
    """A cut-over between two records must not split one assess (spec "Mode")."""
    cat = tmp_path / "catalog"
    cat.mkdir()
    reads: list[int] = []
    real = emit_mod.is_log_mode

    def counting(catalog_dir=None):
        reads.append(1)
        return real(catalog_dir)

    monkeypatch.setattr(emit_mod, "is_log_mode", counting)
    report = BlastRadiusReport(
        anchor_kind="commit",
        anchor_value="deadbeef",
        changed_files=["src/foo.py"],
        affected=[_match("run-a"), _match("run-b")],
        unverifiable=[_match("run-c")],
        unaffected_run_ids=[],
    )
    records = flag_blast_radius(report, cat)
    assert len(records) == 3
    assert len(reads) == 1


def test_add_run_to_campaign_skips_legacy_ingest_with_flag_on(tmp_path: Path, monkeypatch):
    """Flag on: events only -- the cool->warm campaign ingest must not run."""
    import bathos.campaigns as campaigns

    calls: list[str] = []
    monkeypatch.setattr(campaigns, "ingest_cool_campaigns", lambda _db, _cd: calls.append("ingest"))
    monkeypatch.setattr(campaigns, "_resolve_campaign_id", lambda _db, cid, catalog_dir=None: cid)  # noqa: ARG005
    cat = tmp_path / "catalog"
    cat.mkdir()
    db = duckdb.connect(":memory:")
    db.execute(
        "CREATE TABLE campaigns (mode TEXT, started_at TIMESTAMP, stopping_threshold DOUBLE, id TEXT)"
    )
    for flag, expected in (("1", []), (None, ["ingest"])):
        calls.clear()
        if flag:
            monkeypatch.setenv("BTH_LOG_MODE", flag)
            monkeypatch.setenv("BTH_CATALOG_DIR", str(cat))
        else:
            monkeypatch.delenv("BTH_LOG_MODE", raising=False)
        try:
            with emit_mod.unit_of_work(cat):
                campaigns._add_run_to_campaign_impl(db, "c1", "r1", catalog_dir=cat)
        except Exception:
            pass  # only the ingest decision is under test; the missing campaign row is expected
        assert calls == expected, f"flag={flag}"
