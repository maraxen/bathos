"""AC-18 review finding 3, post-4727b872: `bathos.viz.html._project_campaigns`
had no existence guard at all before its `connect_read` call -- it relied on
`connect_read`'s flag-off `missing="empty"` fallback plus a broad
`except Exception` to degrade gracefully, which happened to work but wasn't
guarded the way every other migrated reader in this wave was (and the
`connect_read` docstring incorrectly claimed every site guarded this way).

This is not a behaviour regression on 4727b872 (the broad except already
covered a missing bathos.db) -- it's a consistency/robustness fix: the site
now uses `catalog_readable`, like the other ~12 readers, so it degrades the
same explicit way rather than through an incidental try/except, and works
identically for a real folded-index read post-cutover.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("jinja2")

import bathos.viz.html as html_mod
from bathos.campaigns import Campaign
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.viz.html import _project_campaigns
from tests.runlog.conftest import make_git_repo, write_bth_toml
from tests.runlog.test_ac18_flag_on_reads import _envelope_line, _sidecar_decl, _write_lines


def test_project_campaigns_gates_through_catalog_readable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """The actual regression test for finding 3: `_project_campaigns` must
    call `bathos.index.catalog_readable` (imported into `bathos.viz.html`'s
    own namespace) to decide whether to read, not skip the check entirely.

    Fails on 4727b872 with an `AttributeError` from `monkeypatch.setattr`:
    that revision never imports `catalog_readable` into `bathos.viz.html` at
    all (there was no guard, explicit or otherwise, before the `connect_read`
    call -- just a bare `if catalog_dir is not None:`).
    """
    calls: list[Path | None] = []

    def fake_catalog_readable(catalog_dir: Path | None) -> bool:
        calls.append(catalog_dir)
        return False

    monkeypatch.setattr(html_mod, "catalog_readable", fake_catalog_readable)

    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    campaign = Campaign(id="x", project_slug="repo", name="n", mode="sequential")

    results = html_mod._project_campaigns([campaign], catalog_dir)

    assert calls == [catalog_dir], "catalog_readable(catalog_dir) must be called exactly once"
    assert results[0]["run_count"] == 0
    assert results[0]["outcome_distribution"] == {}


def enable_log_mode(catalog_dir: Path) -> None:
    catalog_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(catalog_dir).write_text(
        json.dumps({"at": "2026-01-01T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []})
    )


def setup_project(tmp_path: Path, name: str = "repo") -> tuple[Path, str | None]:
    repo = make_git_repo(tmp_path / name)
    write_bth_toml(repo, slug=name)
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    register_main_root(repo)
    return repo, pid


def test_project_campaigns_flag_on_no_bathos_db_reads_folded_aggregates(tmp_path: Path):
    from bathos.runlog.ingest import run_ingest

    repo, pid = setup_project(tmp_path)
    catalog_dir = tmp_path / "catalog"
    enable_log_mode(catalog_dir)

    campaign_id = "44444444-4444-4444-4444-444444444444"
    run_id = "55555555-5555-5555-5555-555555555555"
    common = dict(main_root=repo, worktree_root=repo, project="repo", project_id=pid, writer="w1")

    lines = [
        _envelope_line(
            kind="campaign.created",
            entity=[campaign_id],
            data={
                "id": campaign_id,
                "project_slug": "repo",
                "name": "viz-smoke",
                "mode": "sequential",
                "question": None,
                "hypothesis": None,
                "status": "open",
                "started_at": "2026-01-01T00:00:00Z",
                "concluded_at": None,
                "conclusion": None,
                "outcome_label": None,
                "parent_campaign_id": None,
                "stopping_threshold": None,
                "negative_check": None,
                "claim_path": None,
                "claim_sha256": None,
                "claim_mode": None,
            },
            seq=1,
            ts="2026-01-01T00:00:00.000000Z",
            eid="e-viz-created",
            **common,
        ),
        _envelope_line(
            kind="run.started",
            entity=[run_id],
            data={
                "id": run_id,
                "project_slug": "repo",
                "command": "python x.py",
                "argv": ["python", "x.py"],
                "git_hash": "aaa",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": "2026-01-01T00:01:00.000000Z",
                "campaign_id": campaign_id,
                "agent_mode": "manual",
                "sidecar": _sidecar_decl(),
            },
            seq=2,
            ts="2026-01-01T00:01:00.000000Z",
            eid="e-viz-started",
            **common,
        ),
        _envelope_line(
            kind="run.finished",
            entity=[run_id],
            data={
                "id": run_id,
                "status": "completed",
                "exit_code": 0,
                "duration_s": 1.0,
                "output_paths": [],
                "outcome": "pass",
                "outcome_error_reason": "",
                "outcome_is_residual": False,
                "adversarial_check_status": "",
                "timestamp": "2026-01-01T00:02:00.000000Z",
            },
            seq=3,
            ts="2026-01-01T00:02:00.000000Z",
            eid="e-viz-finished",
            **common,
        ),
    ]
    _write_lines(repo, *lines)

    report = run_ingest(catalog_dir)
    assert report.new_events == 3
    assert not (catalog_dir / "bathos.db").exists()

    campaign = Campaign(id=campaign_id, project_slug="repo", name="viz-smoke", mode="sequential")
    results = _project_campaigns([campaign], catalog_dir)

    assert len(results) == 1
    aggregates = results[0]
    assert aggregates["run_count"] == 1, (
        "expected the folded index's one 'pass' run for this campaign, not the "
        "zeroed default aggregates"
    )
    assert aggregates["outcome_distribution"] == {"pass": 1}
