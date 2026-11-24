"""Tests for HTML visualization E_n sequence chart (deferred:viz-en-chart)."""

from __future__ import annotations

import ast
import json
import uuid
from pathlib import Path

import duckdb
import pytest

pytest.importorskip("jinja2")

from bathos.campaigns import Campaign, add_run_to_campaign, create_campaign
from bathos.catalog import init_catalog, write_run
from bathos.schema import Run
from bathos.viz.data import compute_en_trajectory, project_campaign, render_en_chart_svg
from bathos.viz.html import export_html, render_html_report


def test_viz_data_respects_architecture_import_boundary():
    """viz/data.py must only import query.py + campaigns.py (never catalog.py or compact.py)."""
    data_path = Path(__file__).resolve().parents[1] / "src" / "bathos" / "viz" / "data.py"
    tree = ast.parse(data_path.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    for forbidden in ("bathos.catalog", "bathos.compact", "bathos.index"):
        assert forbidden not in imported, f"viz/data.py must not import {forbidden}"


def test_compute_en_trajectory_and_svg_rendering():
    pts = [
        {"seq_position": 1, "run_id": "aaaa1111-0000", "outcome": "pass", "evalue": 2.5},
        {"seq_position": 2, "run_id": "bbbb2222-0000", "outcome": "error", "evalue": 9.0},
        {"seq_position": 3, "run_id": "cccc3333-0000", "outcome": "pass", "evalue": 4.0},
        {"seq_position": 4, "run_id": "dddd4444-0000", "outcome": "pass", "evalue": 3.0},
    ]
    traj = compute_en_trajectory(pts)
    assert len(traj) == 4
    assert traj[0]["cumulative_en"] == pytest.approx(2.5)
    # 'error' is neutral so effective multiplier is 1.0 and cumulative stays 2.5
    assert traj[1]["evalue"] == pytest.approx(1.0)
    assert traj[1]["cumulative_en"] == pytest.approx(2.5)
    assert traj[2]["cumulative_en"] == pytest.approx(10.0)
    assert traj[3]["cumulative_en"] == pytest.approx(30.0)

    svg = render_en_chart_svg(traj, stopping_threshold=20.0)
    assert "<svg " in svg
    assert "Null (E=1.0)" in svg
    assert "Threshold (20)" in svg
    assert "<polyline " in svg
    assert svg.count("<circle ") == 4
    assert "aaaa1111" in svg

    assert render_en_chart_svg([]) == ""


def test_project_campaign_with_trajectory():
    campaign = Campaign(
        id="seq-1234-5678",
        project_slug="proj",
        name="Seq Chart",
        mode="sequential",
        stopping_threshold=20.0,
    )
    display = project_campaign(
        campaign,
        {
            "run_count": 2,
            "outcome_distribution": {"pass": 2},
            "residual_rate": 0.0,
            "bypass_rate": 0.0,
            "unknown_rate": 0.0,
            "anomalies": [],
            "evalue_trajectory": [
                {"seq_position": 1, "run_id": "r1111111", "outcome": "pass", "evalue": 5.0},
                {"seq_position": 2, "run_id": "r2222222", "outcome": "pass", "evalue": 5.0},
            ],
        },
    )
    assert display["stopping_threshold"] == 20.0
    assert display["final_en"] == pytest.approx(25.0)
    assert len(display["evalue_trajectory"]) == 2
    assert "Threshold (20)" in display["en_chart_svg"]
    assert "Null (E=1.0)" in display["en_chart_svg"]


def test_render_html_report_sequential_campaign_en_chart(tmp_path: Path):
    """End-to-end: catalog sequential campaign renders inline SVG E_n chart & table in HTML."""
    sidecar = tmp_path / "exp.bth.toml"
    sidecar.write_text(
        """
[experiment]
hypothesis = "E_n chart test"
[outcomes.pass]
condition = "x > 0"
decision = "go"
reasoning = "ok"
[outcomes.fail]
condition = "x <= 0"
decision = "stop"
reasoning = "no"
is_residual = true
[result_schema]
x = "float"
[popper]
null_pass_rate = 0.25
alt_pass_rate = 0.75
stopping_threshold = 20.0
""",
        encoding="utf-8",
    )

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    from bathos.compact import compact

    r0 = Run(
        project_slug="proj",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
    )
    write_run(r0, cat_dir)
    compact(cat_dir)

    db = duckdb.connect(str(cat_dir / "bathos.db"))
    runs: list[Run] = []
    try:
        seq_campaign = create_campaign(db, name="Seq Viz", project_slug="proj", mode="sequential")
        exp_campaign = create_campaign(db, name="Exp Viz", project_slug="proj", mode="exploration")
        for _ in range(3):
            rid = str(uuid.uuid4())
            db.execute(
                """
                INSERT INTO runs (id, project_slug, command, argv, git_hash, git_branch, git_dirty,
                                  timestamp, duration_s, exit_code, status, output_paths, tags,
                                  schema_version, outcome, sidecar_path, script_sha256)
                VALUES (?, 'proj', 'python x.py', ['python', 'x.py'], 'abc', 'main', false,
                        current_timestamp, 1.0, 0, 'completed', [], [], '20', 'pass', ?, 'sha1')
                """,
                [rid, str(sidecar)],
            )
            add_run_to_campaign(db, seq_campaign.id, rid)
            runs.append(
                Run(
                    id=rid,
                    project_slug="proj",
                    command="python x.py",
                    argv=["python", "x.py"],
                    git_hash="abc",
                    git_branch="main",
                    git_dirty=False,
                    status="completed",
                    exit_code=0,
                    outcome="pass",
                    campaign_id=seq_campaign.id,
                )
            )
    finally:
        db.close()

    out_file = tmp_path / "out.html"
    out_path, warned = export_html(
        runs,
        campaigns=[seq_campaign, exp_campaign],
        output_path=str(out_file),
        catalog_dir=cat_dir,
    )
    assert warned is False
    html_text = Path(out_path).read_text(encoding="utf-8")

    assert "POPPER E<sub>n</sub> Sequence Trajectory" in html_text
    assert "Null (E=1.0)" in html_text
    assert "Threshold (20)" in html_text
    assert "Threshold Met" in html_text
    assert "<polyline " in html_text

    # Verify JSON data blob carries trajectory & final_en
    marker = "window.__BATHOS_DATA__ = "
    start = html_text.find(marker)
    end = html_text.find("};\n", start)
    blob = json.loads(html_text[start + len(marker) : end + 1])
    seq_blob = next(c for c in blob["campaigns"] if c["id"] == seq_campaign.id)
    exp_blob = next(c for c in blob["campaigns"] if c["id"] == exp_campaign.id)
    assert seq_blob["final_en"] == pytest.approx(27.0)
    assert len(seq_blob["evalue_trajectory"]) == 3
    assert exp_blob["evalue_trajectory"] == []
    assert exp_blob["en_chart_svg"] == ""

    # render_html_report also works cleanly directly
    rendered = render_html_report(runs, campaigns=[seq_campaign], catalog_dir=cat_dir)
    assert "Null (E=1.0)" in rendered
