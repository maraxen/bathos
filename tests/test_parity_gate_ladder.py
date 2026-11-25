"""Tests for WP17 parity gate ladder kit: templates, corpus layout, isolation, stages, and legacy labeller."""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path

import numpy as np
import pytest
from cisternal.shadow.mode_state import check_mode_state, resolve_enforcer_symbol
from cisternal.telemetry.pipeline import shutdown_pipeline

from bathos.catalog import init_catalog, read_runs, write_run
from bathos.parity.declare import declare_parity_record
from bathos.parity.evidence import write_cell_evidence
from bathos.parity.gate_ladder import (
    GATE_FAMILIES,
    GateLadderConfigError,
    check_gate_writes_isolated,
    corpus_dirs,
    emit_gate_shadow_label,
    emit_gate_shadow_verdict,
    ensure_corpus_layout,
    flush_shadow_pipeline,
    label_legacy_runs,
    load_corpus_cases,
    load_gate_mode_state,
    load_parity_gate_stages,
    render_gate_mode_state_template,
    validate_gate_mode_state,
    verdict_column_hash,
)
from bathos.parity.grader import grade_parity_record
from bathos.parity.report import build_parity_report
from bathos.parity.schema import compute_cell_id, render_declaration_scaffold
from bathos.schema import Run

REPO_ROOT = Path(__file__).resolve().parents[1]
REPO_MODE_STATE_DIR = REPO_ROOT / ".praxia" / "mode_state"


def test_parity_gate_shadow_writes_isolated(tmp_path: Path) -> None:
    """Enforcer test proving a shadow parity gate writes nothing outside its shadow log dir."""
    cat = tmp_path / "catalog"
    init_catalog(cat)
    write_run(
        Run(
            project_slug="demo",
            command="bth run ev.py",
            argv=[],
            git_hash="0" * 40,
            git_branch="main",
            git_dirty=False,
            id="run-iso-1",
            exit_code=0,
            status="completed",
            outcome="pass",
        ),
        cat,
    )
    shadow_log = tmp_path / "shadow_log"

    def _baseline_eval() -> list[str]:
        return [r.id for r in read_runs(cat)]

    def _shadow_eval() -> list[str]:
        runs = read_runs(cat)
        emit_gate_shadow_verdict(
            feature="parity.g01",
            subject_key="record:demo:sha1",
            verdict=True,
            stage="shadow",
            log_dir=shadow_log,
        )
        shutdown_pipeline()
        return [r.id for r in runs]

    try:
        res = check_gate_writes_isolated(
            _baseline_eval,
            _shadow_eval,
            live_roots=[cat],
            shadow_log_dir=shadow_log,
            feature="parity.g01",
            enforced_by="tests/test_parity_gate_ladder.py::test_parity_gate_shadow_writes_isolated",
        )
    finally:
        shutdown_pipeline()
    assert res.passed is True
    assert res.live_output_identical is True
    assert res.leaked_paths == []


def test_nc_shadow_gate_mutating_outcome_fails_isolation(tmp_path: Path) -> None:
    """NC: a shadow gate that writes or alters a catalog run outcome fails isolation verification."""
    cat = tmp_path / "catalog"
    init_catalog(cat)
    run_file = cat / "runs" / "demo" / "run_1.json"
    run_file.parent.mkdir(parents=True, exist_ok=True)
    run_file.write_text(json.dumps({"id": "r1", "outcome": "pass"}), encoding="utf-8")
    shadow_log = tmp_path / "shadow_log"

    def _live_fn() -> str:
        return json.loads(run_file.read_text(encoding="utf-8"))["outcome"]

    def _leaky_shadow_fn() -> str:
        # Simulate a bug where a shadow check rewrites the live run outcome.
        run_file.write_text(json.dumps({"id": "r1", "outcome": "fail"}), encoding="utf-8")
        return json.loads(run_file.read_text(encoding="utf-8"))["outcome"]

    res = check_gate_writes_isolated(
        _live_fn,
        _leaky_shadow_fn,
        live_roots=[cat],
        shadow_log_dir=shadow_log,
    )
    assert res.passed is False
    assert res.live_output_identical is False


def test_repo_parity_mode_states_valid_and_enforcers_resolve() -> None:
    """All Ring 0 parity mode-state TOMLs in .praxia/mode_state pass WP17 validation & enforcer lookup."""
    stages = load_parity_gate_stages(REPO_MODE_STATE_DIR)
    assert stages == {"G01": "shadow", "G02": "shadow", "G04": "shadow"}
    for code in ("g01", "g02", "g04"):
        data = load_gate_mode_state(REPO_MODE_STATE_DIR / f"parity.{code}.toml")
        sym = data["shadow"]["writes_isolated_enforced_by"]
        fn = resolve_enforcer_symbol(sym, repo_root=REPO_ROOT)
        assert callable(fn)


def test_nc_template_and_missing_mandatory_keys_rejected(tmp_path: Path) -> None:
    """NC: missing min_labels or lower bounds raises GateLadderConfigError."""
    rendered = render_gate_mode_state_template(
        "parity.g01",
        description="Schema validation gate",
        enforcer_symbol="tests/test_parity_gate_ladder.py::test_parity_gate_shadow_writes_isolated",
        threshold_basis="§7.7 D2 95% Clopper-Pearson >= 0.90",
    )
    parsed = tomllib.loads(rendered)
    assert validate_gate_mode_state(parsed)["promotion"]["min_labels"] == 60

    for drop_key in ("min_labels", "min_precision_lower_bound", "min_recall_lower_bound"):
        broken = tomllib.loads(rendered)
        broken["promotion"].pop(drop_key)
        with pytest.raises(GateLadderConfigError, match=drop_key):
            validate_gate_mode_state(broken, where=f"drop:{drop_key}")

    unisolated = tomllib.loads(rendered)
    unisolated["shadow"]["writes_isolated"] = False
    with pytest.raises(GateLadderConfigError, match="writes_isolated"):
        validate_gate_mode_state(unisolated)

    toml_p = tmp_path / "parity.bad.toml"
    toml_p.write_text(
        rendered.replace("min_labels = 60\n", ""),
        encoding="utf-8",
    )
    with pytest.raises(GateLadderConfigError, match="min_labels"):
        load_gate_mode_state(toml_p)


def test_corpus_layout_matches_six_families(tmp_path: Path) -> None:
    assert sum(len(v) for v in GATE_FAMILIES.values()) == 31
    assert set(GATE_FAMILIES) == {"PF1", "PF2", "PF3", "PF4", "PF5", "PF6"}
    pos, neg = ensure_corpus_layout(tmp_path, "pf1")
    assert pos.is_dir() and neg.is_dir()
    (pos / "case_01.toml").write_text("[parity]\n", encoding="utf-8")
    (neg / "case_02.json").write_text("{}\n", encoding="utf-8")
    cases = load_corpus_cases(tmp_path, "PF1")
    assert [p.name for p in cases["positive"]] == ["case_01.toml"]
    assert [p.name for p in cases["negative"]] == ["case_02.json"]
    with pytest.raises(GateLadderConfigError):
        corpus_dirs(tmp_path, "PF9")


def test_acceptance_cisternal_shadow_check_end_to_end(tmp_path: Path) -> None:
    """WP17 Acceptance: cisternal shadow check evaluates a sample gate end-to-end (library + CLI)."""
    toml_text = render_gate_mode_state_template(
        "parity.g_sample",
        description="Sample parity gate for WP17 acceptance",
        enforcer_symbol="tests/test_parity_gate_ladder.py::test_parity_gate_shadow_writes_isolated",
        threshold_basis="WP17 synthetic sample acceptance",
        min_labels=30,
        min_verdicts=30,
        min_precision_lower_bound=0.80,
        min_recall_lower_bound=0.80,
    )
    ms_dir = tmp_path / "mode_state"
    ms_dir.mkdir()
    ms_path = ms_dir / "parity.g_sample.toml"
    ms_path.write_text(toml_text, encoding="utf-8")

    records_dir = tmp_path / "shadow_records"
    live_dir = tmp_path / "live"
    live_dir.mkdir()

    def _emit_init() -> str:
        emit_gate_shadow_verdict(
            feature="parity.g_sample",
            subject_key="case:init",
            verdict=True,
            log_dir=records_dir,
        )
        return "ok"

    try:
        # Prove isolation in the same telemetry log_dir so shadow.isolation sits alongside verdicts/labels
        iso = check_gate_writes_isolated(
            lambda: "ok",
            _emit_init,
            live_roots=[live_dir],
            shadow_log_dir=records_dir,
            feature="parity.g_sample",
            enforced_by="tests/test_parity_gate_ladder.py::test_parity_gate_shadow_writes_isolated",
        )
        assert iso.passed is True
        # Emit 20 positives + 15 negatives matching cleanly (Wilson 95% lower bound on 20/20 ~= 0.839 > 0.80)
        for idx in range(35):
            truth = idx < 20
            subj = f"case:{idx}"
            assert emit_gate_shadow_verdict(
                feature="parity.g_sample",
                subject_key=subj,
                verdict=truth,
                stage="shadow",
                log_dir=None,
            )
            assert emit_gate_shadow_label(
                feature="parity.g_sample",
                subject_key=subj,
                label=truth,
                label_source="owner_confirmed",
                log_dir=None,
            )
    finally:
        flush_shadow_pipeline()

    api_res = check_mode_state(ms_path, records_dir=records_dir, repo_root=REPO_ROOT)
    assert api_res.promotion_permitted is True
    assert api_res.blocking_reasons == []
    assert api_res.metrics["tp"] == 20
    assert api_res.metrics["tn"] == 15

    proc = subprocess.run(
        [
            "cisternal",
            "shadow",
            "check",
            str(ms_path),
            "--records-dir",
            str(records_dir),
            "--json",
        ],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr + proc.stdout
    cli_payload = json.loads(proc.stdout)
    assert cli_payload["promotion_permitted"] is True
    assert cli_payload["blocking_reasons"] == []


def test_legacy_labeller_preserves_verdict_column_hash(tmp_path: Path) -> None:
    """NC: legacy labeller marks existing runs 'legacy' without altering verdict-column hash."""
    cat = tmp_path / "catalog"
    init_catalog(cat)
    runs = [
        Run(
            project_slug="demo",
            command="bth run legacy.py",
            argv=[],
            git_hash="0" * 40,
            git_branch="main",
            git_dirty=False,
            id=f"r-leg-{i}",
            exit_code=0 if i % 2 == 0 else 1,
            status="completed",
            outcome="pass" if i % 2 == 0 else "fail",
        )
        for i in range(4)
    ]
    for r in runs:
        write_run(r, cat)

    before_runs = read_runs(cat)
    before_hash = verdict_column_hash(before_runs)
    frag_dir = cat / "runs" / "demo"
    before_bytes = {p.name: p.read_bytes() for p in sorted(frag_dir.glob("*.parquet"))}

    labeled = label_legacy_runs(before_runs)
    assert len(labeled) == 4
    assert all(item["label"] == "legacy" for item in labeled)
    assert all(item["verdict_column_sha256"] == before_hash for item in labeled)

    after_runs = read_runs(cat)
    after_hash = verdict_column_hash(after_runs)
    after_bytes = {p.name: p.read_bytes() for p in sorted(frag_dir.glob("*.parquet"))}
    assert after_hash == before_hash
    assert after_bytes == before_bytes


def test_grader_reads_stage_records_without_changing_verdicts(tmp_path: Path) -> None:
    """Grader prints real stage records from mode_state_dir while keeping cell verdicts identical."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / ".bth.toml").write_text('[project]\nslug = "demo"\n', encoding="utf-8")
    subprocess.run(["git", "init"], cwd=str(repo), check=True, capture_output=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=str(repo), check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=str(repo), check=True)
    (repo / "a.txt").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=str(repo), check=True)
    subprocess.run(["git", "commit", "--no-verify", "-m", "init"], cwd=str(repo), check=True, capture_output=True)

    store = tmp_path / "store"
    cat = tmp_path / "catalog"
    init_catalog(cat)

    decl_path = repo / "demo.parity.bth.toml"
    raw = render_declaration_scaffold("wp17-stage-v1").replace(
        'kind = "sealed_dump"\n'
        'identity = { dump_sha256 = "0000000000000000000000000000000000000000000000000000000000000000", producer_run_id = "run_0000", upstream_commit = "0000000000000000000000000000000000000000" }',
        'kind = "upstream_live"\nidentity = { repo = "https://example.com/u", commit = "' + "a" * 40 + '" }',
    )
    decl_path.write_text(raw, encoding="utf-8")
    decl = tomllib.loads(raw)
    c = decl["cell_plan"][0]
    cid = compute_cell_id(c["component_id"], c["use_id"], c["knob_setting"], c["axis_value"], c["data_tier"], c["pairing"])
    declare_parity_record(decl_path, parity_store=store)
    write_run(
        Run(
            project_slug="demo",
            command="bth run ev.py",
            argv=[],
            git_hash="0" * 40,
            git_branch="main",
            git_dirty=False,
            id="r-wp17",
            exit_code=0,
            status="completed",
            outcome="pass",
        ),
        cat,
    )
    write_cell_evidence(
        store=store,
        record_id="wp17-stage-v1",
        cell_id=cid,
        run_id="r-wp17",
        ours_arrays={"scores": np.array([1.0], dtype=np.float32)},
        reference_arrays={"scores": np.array([1.0], dtype=np.float32)},
        input_sha256={"ours": ["a" * 64], "reference": ["b" * 64]},
    )

    ms_dir = tmp_path / "mode_state"
    ms_dir.mkdir()
    (ms_dir / "parity.g01.toml").write_text(
        render_gate_mode_state_template(
            "parity.g01",
            description="G01 advisory test",
            enforcer_symbol="tests/test_parity_gate_ladder.py::test_parity_gate_shadow_writes_isolated",
            threshold_basis="WP17 stage record test",
            current_stage="advisory",
            target_stage="enforce",
        ),
        encoding="utf-8",
    )

    g_shadow = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, mode_state_dir=REPO_MODE_STATE_DIR, track_run=False)
    g_adv = grade_parity_record(decl_path, parity_store=store, catalog_dir=cat, mode_state_dir=ms_dir, track_run=False)
    assert g_adv.result["cells"] == g_shadow.result["cells"]
    assert g_adv.result["grade_inputs"]["gate_stages"]["G01"] == "advisory"
    assert g_adv.result["grade_inputs"]["gate_stages"]["G02"] == "shadow"
    assert g_adv.result["grade_inputs"]["gate_stages"]["G04"] == "shadow"
    assert len(g_adv.result["grade_inputs"]["gate_stages"]) == 26

    _, rep = build_parity_report(g_adv.result_path, decl_path=decl_path, parity_store=store, catalog_dir=cat, mode_state_dir=ms_dir)
    assert "gates: 0 enforce, 1 advisory, 2 shadow, 23 not active" in rep["badge"]["t1"]
