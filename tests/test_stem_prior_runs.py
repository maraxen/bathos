"""Prior-run lookup for exploration/calibration stems (spec 261015_exploratory-mode-contract §3.1).

The lookup matches a catalog run's stored `sidecar_path` (not its `command` argv) and counts only
pre-v17 tracked runs (`epistemic_class IS NULL` and a non-empty `sidecar_path`). Real catalog rows
are written with `write_run` and compacted, so the warm (DuckDB) path is what gets exercised.
"""

import textwrap
from datetime import UTC, datetime
from pathlib import Path

from bathos.catalog import init_catalog, write_run
from bathos.compact import compact
from bathos.prereg import _stem_has_prior_runs
from bathos.schema import Run

OLD_SIDECAR = "/other/worktree/scripts/experiments/old_style.bth.toml"

LEGACY_EXPLORATION = """
[experiment]
stage_name = "exploration"
hypothesis = "legacy-shaped exploration, no [observe] block"

[outcomes.hub]
condition = "top_hub_score > 0.5"
decision = "look at hub residues"
reasoning = "triage"
source = "lit:smith2020"

[outcomes.rest]
condition = "1 = 1"
decision = "nothing"
reasoning = "residual"
is_residual = true

[result_schema]
top_hub_score = "float"
"""


def _run(
    command: str,
    sidecar_path: str = "",
    epistemic_class: str | None = None,
    sidecar_mode: str = "",
) -> Run:
    return Run(
        project_slug="proj",
        command=command,
        argv=command.split(),
        git_hash="abc",
        git_branch="main",
        git_dirty=False,
        timestamp=datetime.now(UTC),
        status="completed",
        exit_code=0,
        sidecar_path=sidecar_path,
        epistemic_class=epistemic_class,
        sidecar_mode=sidecar_mode,
    )


def _catalog(tmp_path: Path, runs: list[Run], *, compacted: bool = True) -> Path:
    catalog = tmp_path / "catalog"
    init_catalog(catalog)
    for r in runs:
        write_run(r, catalog)
    if compacted:
        compact(catalog)
    return catalog


def _script(root: Path, subdir: str, stem: str) -> Path:
    return root / "scripts" / subdir / f"{stem}.py"


def test_pre_v17_row_matches_relative_command_under_another_root(tmp_path):
    catalog = _catalog(
        tmp_path, [_run("python scripts/experiments/old_style.py", sidecar_path=OLD_SIDECAR)]
    )
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, catalog) is True


def test_only_legacy_or_exploratory_rows_do_not_count(tmp_path):
    catalog = _catalog(
        tmp_path,
        [
            _run("python scripts/experiments/old_style.py", OLD_SIDECAR, epistemic_class="legacy"),
            _run(
                "python scripts/experiments/old_style.py",
                OLD_SIDECAR,
                epistemic_class="exploratory",
            ),
        ],
    )
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, catalog) is False


def test_row_with_empty_sidecar_path_does_not_count(tmp_path):
    # Mirrors `bth run --no-sidecar`: a throwaway run on a new stem must not unlock legacy.
    catalog = _catalog(tmp_path, [_run("python scripts/experiments/old_style.py", "")])
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, catalog) is False


def test_stem_does_not_match_longer_or_suffixed_names(tmp_path):
    catalog = _catalog(
        tmp_path,
        [
            _run(
                "python scripts/experiments/foo_bar.py",
                "/w/scripts/experiments/foo_bar.bth.toml",
            ),
            _run("python scripts/experiments/xfoo.py", "/w/scripts/experiments/xfoo.bth.toml"),
        ],
    )
    script = _script(tmp_path / "checkout", "experiments", "foo")
    assert _stem_has_prior_runs(script, catalog) is False

    # Positive control: the exact stem does match, so the negative above is not vacuous.
    catalog_exact = _catalog(
        tmp_path / "exact",
        [_run("python scripts/experiments/foo.py", "/w/scripts/experiments/foo.bth.toml")],
    )
    assert _stem_has_prior_runs(script, catalog_exact) is True


def test_same_stem_under_benchmarks_does_not_match_experiments(tmp_path):
    catalog = _catalog(
        tmp_path,
        [
            _run(
                "python scripts/benchmarks/old_style.py",
                "/other/worktree/scripts/benchmarks/old_style.bth.toml",
            )
        ],
    )
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, catalog) is False


def test_sidecar_path_input_gives_same_answer_as_script_path(tmp_path):
    catalog = _catalog(
        tmp_path, [_run("python scripts/experiments/old_style.py", sidecar_path=OLD_SIDECAR)]
    )
    root = tmp_path / "checkout"
    script = _script(root, "experiments", "old_style")
    sidecar = script.with_suffix(".bth.toml")
    assert _stem_has_prior_runs(script, catalog) is True
    assert _stem_has_prior_runs(sidecar, catalog) is True

    other = _script(root, "experiments", "new_stem")
    assert _stem_has_prior_runs(other, catalog) is False
    assert _stem_has_prior_runs(other.with_suffix(".bth.toml"), catalog) is False


def test_warm_tier_answers_directly_when_compacted(tmp_path):
    # Guards against the warm query failing silently and every answer coming from the cool tier.
    from bathos.prereg import _warm_stem_has_prior_runs

    catalog = _catalog(
        tmp_path,
        [
            _run("python scripts/experiments/old_style.py", OLD_SIDECAR),
            _run(
                "python scripts/experiments/v17.py",
                "/w/scripts/experiments/v17.bth.toml",
                "exploratory",
            ),
        ],
    )
    assert _warm_stem_has_prior_runs(catalog, "/experiments/old_style.bth.toml", "") is True
    assert _warm_stem_has_prior_runs(catalog, "/experiments/v17.bth.toml", "") is False
    assert _warm_stem_has_prior_runs(catalog, "/experiments/absent.bth.toml", "") is False


def test_cool_tier_fallback_matches_without_compaction(tmp_path):
    # No bathos.db: the warm tier is not readable, so the cool Parquet scan answers.
    catalog = _catalog(
        tmp_path,
        [_run("python scripts/experiments/old_style.py", OLD_SIDECAR)],
        compacted=False,
    )
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, catalog) is True


def test_corrupt_warm_db_falls_back_to_cool_tier(tmp_path):
    catalog = _catalog(tmp_path, [_run("python scripts/experiments/old_style.py", OLD_SIDECAR)])
    (catalog / "bathos.db").write_bytes(b"not a duckdb file")
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, catalog) is True


def test_missing_catalog_means_no_prior_runs(tmp_path):
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, tmp_path / "does-not-exist") is False
    assert _stem_has_prior_runs(script, None) is False


_LEGACY_SIDECAR_TEXT = textwrap.dedent(LEGACY_EXPLORATION)


def test_validate_sidecar_tool_accepts_legacy_shape_with_prior_run(tmp_path, monkeypatch):
    from bathos.mcp import validate_sidecar_tool

    catalog = _catalog(tmp_path, [_run("python scripts/experiments/old_style.py", OLD_SIDECAR)])
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    sidecar = tmp_path / "checkout" / "scripts" / "experiments" / "old_style.bth.toml"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(_LEGACY_SIDECAR_TEXT)

    result = validate_sidecar_tool(path=str(sidecar))
    stage_errors = [e for e in result.get("errors", []) if e.startswith("stage_name")]
    assert stage_errors == []


def test_validate_sidecar_tool_rejects_legacy_shape_on_new_stem(tmp_path, monkeypatch):
    # Control for the test above: with no prior run, the same sidecar is a stage_name error.
    from bathos.mcp import validate_sidecar_tool

    catalog = _catalog(tmp_path, [])
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog))

    sidecar = tmp_path / "checkout" / "scripts" / "experiments" / "old_style.bth.toml"
    sidecar.parent.mkdir(parents=True)
    sidecar.write_text(_LEGACY_SIDECAR_TEXT)

    result = validate_sidecar_tool(path=str(sidecar))
    assert result["validation_ok"] is False
    assert any(e.startswith("stage_name") for e in result["errors"])


def test_bypassed_no_sidecar_row_does_not_count(tmp_path):
    """`bth run --no-sidecar` stores the skipped sidecar_path with sidecar_mode='bypassed' and no
    class; such a row must not unlock the legacy path (warm and cool)."""
    row = _run(
        "python scripts/experiments/old_style.py",
        sidecar_path=OLD_SIDECAR,
        sidecar_mode="bypassed",
    )
    script = _script(tmp_path / "checkout", "experiments", "old_style")
    assert _stem_has_prior_runs(script, _catalog(tmp_path / "w", [row])) is False
    assert _stem_has_prior_runs(script, _catalog(tmp_path / "c", [row], compacted=False)) is False


def test_no_sidecar_run_through_the_runner_does_not_unlock_legacy(tmp_path, tmp_catalog):
    """End to end: the row is produced by run_script(no_sidecar=True), not hand-built."""
    import sys

    from bathos.runner import run_script

    init_catalog(tmp_catalog)
    exp = tmp_path / "scripts" / "experiments"
    exp.mkdir(parents=True)
    script = exp / "old_style.py"
    script.write_text("print('{}')\n")
    (exp / "old_style.bth.toml").write_text(textwrap.dedent(LEGACY_EXPLORATION))
    run_script(
        argv=[sys.executable, str(script)],
        project_slug="proj",
        catalog_dir=tmp_catalog,
        output_paths=[],
        tags=[],
        cwd=tmp_path,
        no_sidecar=True,
    )
    compact(tmp_catalog)
    assert _stem_has_prior_runs(script, tmp_catalog) is False
