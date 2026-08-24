"""Tests for the post-conclude hook-discovery mechanism (bathos.campaign_conclude_hooks).

Covers discovery + invocation + argument-shape correctness via a fake entry
point (no real package installation needed), the non-propagation guarantee
when a hook raises, and the zero-hooks-registered no-op case.
"""

import importlib.metadata
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import (
    CampaignConcludeEvent,
    ConcludedRunInfo,
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
)
from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.schema import Run

HOOK_GROUP = "bathos.campaign_conclude_hooks"


class _FakeEntryPoint:
    """Stand-in for importlib.metadata.EntryPoint that skips real package install."""

    def __init__(self, name, func):
        self.name = name
        self._func = func

    def load(self):
        return self._func


def _patch_entry_points(monkeypatch, entry_points_by_group):
    """Route importlib.metadata.entry_points(group=...) to a fixed mapping for the test."""

    def fake_entry_points(**kwargs):
        group = kwargs.get("group")
        return list(entry_points_by_group.get(group, []))

    monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)


@pytest.fixture
def catalog_with_two_runs(tmp_catalog: Path) -> Path:
    init_catalog(tmp_catalog)
    base = datetime(2026, 8, 24, 12, 0, 0, tzinfo=UTC)
    for i in range(2):
        r = Run(
            project_slug="prolix",
            command=f"python run_{i}.py",
            argv=["python", f"run_{i}.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=base + timedelta(hours=i),
            status="completed",
            exit_code=0,
        )
        write_run(r, tmp_catalog)
    compact(tmp_catalog)
    return tmp_catalog


def test_hook_discovered_and_invoked_with_correct_argument_shape(
    catalog_with_two_runs: Path, monkeypatch
):
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")
        run_ids = [
            row[0]
            for row in db.execute(
                "SELECT id FROM runs WHERE project_slug = 'prolix' ORDER BY id"
            ).fetchall()
        ]
        for rid in run_ids:
            add_run_to_campaign(db, campaign.id, rid)

        # Stamp one member run with resolvable output/sidecar/content-hash data,
        # bypassing real file creation -- we're testing the read path, not compaction.
        db.execute(
            "UPDATE runs SET output_paths = ?, sidecar_path = ?, output_metadata = ? WHERE id = ?",
            [
                ["/tmp/out/result.json"],
                "/tmp/out/result.bth.toml",
                json.dumps([{"path": "/tmp/out/result.json", "sha256": "deadbeef"}]),
                run_ids[0],
            ],
        )
        db.commit()

        captured = []
        _patch_entry_points(
            monkeypatch,
            {HOOK_GROUP: [_FakeEntryPoint("affigit-wire-test", captured.append)]},
        )

        conclude_campaign(db, campaign.id, "pass", "worked")

        assert len(captured) == 1
        event = captured[0]
        assert isinstance(event, CampaignConcludeEvent)
        assert event.campaign_id == campaign.id
        assert event.outcome_label == "pass"
        assert isinstance(event.members, tuple)
        assert {m.run_id for m in event.members} == set(run_ids)

        stamped = next(m for m in event.members if m.run_id == run_ids[0])
        assert isinstance(stamped, ConcludedRunInfo)
        assert stamped.output_path == "/tmp/out/result.json"
        assert stamped.sidecar_path == "/tmp/out/result.bth.toml"
        assert stamped.content_hash == "deadbeef"

        unstamped = next(m for m in event.members if m.run_id == run_ids[1])
        assert unstamped.content_hash is None
    finally:
        db.close()


def test_content_hash_matched_by_path_not_first_hash_found(
    catalog_with_two_runs: Path, monkeypatch
):
    """Regression: content_hash must be the entry keyed by output_path, not "the first
    entry in output_metadata with any hash" -- a multi-output run whose FIRST output has
    no recorded hash (e.g. >100MB, per bathos.compact._collect_output_metadata) but whose
    SECOND output does must not have that second hash misattributed to the first path."""
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")
        run_id = db.execute(
            "SELECT id FROM runs WHERE project_slug = 'prolix' ORDER BY id LIMIT 1"
        ).fetchone()[0]
        add_run_to_campaign(db, campaign.id, run_id)

        db.execute(
            "UPDATE runs SET output_paths = ?, output_metadata = ? WHERE id = ?",
            [
                ["/big/no-hash.bin", "/small/hashed.json"],
                json.dumps(
                    [
                        {"path": "/big/no-hash.bin"},  # too large to hash -- no sha256 key
                        {"path": "/small/hashed.json", "sha256": "should-not-be-used"},
                    ]
                ),
                run_id,
            ],
        )
        db.commit()

        captured = []
        _patch_entry_points(monkeypatch, {HOOK_GROUP: [_FakeEntryPoint("hook", captured.append)]})

        conclude_campaign(db, campaign.id, "pass", "worked")

        member = next(m for m in captured[0].members if m.run_id == run_id)
        assert member.output_path == "/big/no-hash.bin"
        assert member.content_hash is None
    finally:
        db.close()


def test_hook_that_raises_does_not_propagate_or_block_conclusion(
    catalog_with_two_runs: Path, monkeypatch, capsys
):
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")

        def boom(_event):
            raise RuntimeError("simulated hook failure")

        _patch_entry_points(monkeypatch, {HOOK_GROUP: [_FakeEntryPoint("broken-hook", boom)]})

        # Must complete normally -- no exception, no changed return value.
        result = conclude_campaign(db, campaign.id, "pass", "worked")
        assert result is None

        row = db.execute(
            "SELECT status, outcome_label FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchone()
        assert row[0] == "concluded"
        assert row[1] == "pass"

        out = capsys.readouterr().out
        assert "WARNING" in out
        assert "broken-hook" in out
        assert "simulated hook failure" in out
    finally:
        db.close()


def test_zero_hooks_registered_is_a_noop(catalog_with_two_runs: Path, monkeypatch, capsys):
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")

        calls = []

        def fake_entry_points(**kwargs):
            calls.append(kwargs.get("group"))
            return []

        monkeypatch.setattr(importlib.metadata, "entry_points", fake_entry_points)

        result = conclude_campaign(db, campaign.id, "pass", "worked")
        assert result is None

        row = db.execute("SELECT status FROM campaigns WHERE id = ?", [campaign.id]).fetchone()
        assert row[0] == "concluded"

        # Discovery must actually run (proving the call site wasn't dropped/reverted),
        # even though it found nothing to invoke.
        assert calls == [HOOK_GROUP]

        out = capsys.readouterr().out
        assert "campaign_conclude_hook" not in out
    finally:
        db.close()


def test_zero_member_runs_still_invokes_hook(catalog_with_two_runs: Path, monkeypatch):
    """A campaign with no member runs at all must still invoke a registered hook, with
    an empty members tuple -- not be silently skipped."""
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        # Deliberately never call add_run_to_campaign -- membership stays empty.
        campaign = create_campaign(db, name="Empty", project_slug="prolix", mode="exploration")

        captured = []
        _patch_entry_points(monkeypatch, {HOOK_GROUP: [_FakeEntryPoint("hook", captured.append)]})

        conclude_campaign(db, campaign.id, "pass", "worked")

        assert len(captured) == 1
        assert captured[0].members == ()
    finally:
        db.close()


def test_second_hook_still_runs_after_first_hook_raises(catalog_with_two_runs: Path, monkeypatch):
    """Per-hook isolation: one broken entry point must not prevent a second, well-behaved
    entry point from receiving the same payload."""
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")

        def boom(_event):
            raise RuntimeError("first hook is broken")

        captured = []
        _patch_entry_points(
            monkeypatch,
            {
                HOOK_GROUP: [
                    _FakeEntryPoint("broken-hook", boom),
                    _FakeEntryPoint("good-hook", captured.append),
                ]
            },
        )

        conclude_campaign(db, campaign.id, "pass", "worked")

        assert len(captured) == 1
        assert captured[0].campaign_id == campaign.id
    finally:
        db.close()


def test_hooks_survive_member_resolution_failure(catalog_with_two_runs: Path, monkeypatch, capsys):
    """Regression for a gap found in adversarial review: union_campaign_member_ids()
    (and, transitively, _resolve_member_run_infos()) originally sat OUTSIDE the
    try/except in _run_campaign_conclude_hooks() -- a raise there (e.g. a corrupt
    cool-tier parquet fragment inside union_campaign_member_ids's read_runs() call)
    would have propagated straight out of conclude_campaign(), after the campaign was
    already durably committed as concluded. Isolate that exact code path by making
    membership resolution itself raise, independent of any real catalog I/O."""
    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(db, name="Test", project_slug="prolix", mode="exploration")

        def boom_union(*_args, **_kwargs):
            raise RuntimeError("simulated corrupt cool-tier fragment")

        monkeypatch.setattr("bathos.campaigns.union_campaign_member_ids", boom_union)

        captured = []
        _patch_entry_points(monkeypatch, {HOOK_GROUP: [_FakeEntryPoint("hook", captured.append)]})

        # No claim registered (mode=exploration) -- the only call to
        # union_campaign_member_ids in this run is the one inside the hook dispatch
        # path, so this isolates that call without a real catalog/parquet fixture.
        result = conclude_campaign(db, campaign.id, "pass", "worked")
        assert result is None

        row = db.execute("SELECT status FROM campaigns WHERE id = ?", [campaign.id]).fetchone()
        assert row[0] == "concluded"

        # Member resolution failed, so no hook was invoked with a broken/partial payload.
        assert captured == []

        out = capsys.readouterr().out
        assert "WARNING" in out
    finally:
        db.close()


def test_hook_receives_final_post_downgrade_outcome_label(
    catalog_with_two_runs: Path, tmp_path, monkeypatch
):
    """The hook must see the FINAL outcome_label (post Union-Gate downgrade), not the
    researcher's original argument -- same setup as
    test_conclude_campaign_union_gate_downgrades_with_real_registered_claim in
    test_campaigns.py, with a hook attached to observe what it actually receives."""
    import json as _json

    from bathos.claim import register_claim

    # emit_claim_coverage_report hardcodes Path.home() / ".bth" / "catalog" -- redirect
    # so this test doesn't write into the real user home directory.
    fake_home = tmp_path / "fake_home"
    monkeypatch.setattr(Path, "home", lambda: fake_home)

    db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
    try:
        campaign = create_campaign(
            db, name="Hook Downgrade Test", project_slug="prolix", mode="confirmation"
        )

        claim_path = tmp_path / "test.claim.toml"
        claim_path.write_text("""[claim]
headline = "Test claim"
kill_condition = "Outcome != expected"

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
""")
        register_claim(claim_path, campaign.id, db, tmp_path)

        # Only H_primary is discriminated -- H_null is missing, so C_main is uncovered
        # and the Union Gate downgrades the verdict to 'confounded'.
        campaign_time = datetime.fromisoformat(campaign.started_at)
        run = Run(
            project_slug="prolix",
            command="python test.py",
            argv=["python", "test.py"],
            git_hash="abc",
            git_branch="main",
            git_dirty=False,
            timestamp=campaign_time + timedelta(minutes=1),
            status="completed",
            exit_code=0,
            claim_discriminates=_json.dumps(["H_primary"]),
        )
        db.close()
        write_run(run, catalog_with_two_runs)
        compact(catalog_with_two_runs)
        db = duckdb.connect(str(catalog_with_two_runs / "bathos.db"))
        add_run_to_campaign(db, campaign.id, run.id)

        captured = []
        _patch_entry_points(monkeypatch, {HOOK_GROUP: [_FakeEntryPoint("hook", captured.append)]})

        conclude_campaign(
            db, campaign.id, "pass", "Should be downgraded", workspace_root=tmp_path
        )

        row = db.execute(
            "SELECT outcome_label FROM campaigns WHERE id = ?", [campaign.id]
        ).fetchone()
        assert row[0] == "confounded"

        assert len(captured) == 1
        # The hook must see the same final label that was persisted -- not the
        # researcher's original "pass" argument.
        assert captured[0].outcome_label == "confounded"
    finally:
        db.close()
