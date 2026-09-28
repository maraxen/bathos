"""CLI-level coverage for `bth migrate --consolidate-catalog` (debt #1998, PR #74
review fix 11): exit-code behavior on clean vs. `needs_review`, mutual exclusion
with `--to-log`, and `--source-catalog` (fix 9's race-window closure).

Uses the same isolation as the rest of `tests/runlog/` (`isolated_home`, autouse
from `conftest.py`). `explicit_catalog_dir()` reads `.bth.toml` directly off the
working file, so these tests build a plain root directory with a `.bth.toml` --
no git repo or registry entry is required for `--consolidate-catalog` itself.
"""

from __future__ import annotations

from pathlib import Path

from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


def _write_bth_toml(root: Path, *, catalog_dir: Path | None) -> None:
    root.mkdir(parents=True, exist_ok=True)
    lines = ["[project]", 'slug = "proj"', f'root = "{root}"']
    if catalog_dir is not None:
        lines.append(f'catalog_dir = "{catalog_dir}"')
    (root / ".bth.toml").write_text("\n".join(lines) + "\n")


def test_cli_consolidate_catalog_exit_zero_on_clean(tmp_path: Path, monkeypatch):
    from bathos.cli_cyclopts import app

    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=source)

    (source / "campaigns").mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text('{"id": "c"}')

    monkeypatch.setenv("BTH_CATALOG_DIR", str(dest))

    result = runner.invoke(app, ["migrate", "--consolidate-catalog", str(root)])

    assert result.exit_code == 0, result.output
    assert '"status": "ok"' in result.output
    assert (dest / "campaigns" / "c.json").is_file()


def test_cli_consolidate_catalog_exit_nonzero_on_needs_review(tmp_path: Path, monkeypatch):
    from bathos.cli_cyclopts import app

    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    (source / "campaigns").mkdir(parents=True)
    (dest / "campaigns").mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=source)

    (source / "campaigns" / "c.json").write_text("source-version")
    (dest / "campaigns" / "c.json").write_text("dest-version")

    monkeypatch.setenv("BTH_CATALOG_DIR", str(dest))

    result = runner.invoke(app, ["migrate", "--consolidate-catalog", str(root)])

    assert result.exit_code != 0
    assert '"status": "needs_review"' in result.output
    # Dest's own file survives untouched -- the additive copy never clobbers.
    assert (dest / "campaigns" / "c.json").read_text() == "dest-version"


def test_cli_consolidate_catalog_mutual_exclusion_with_to_log(tmp_path: Path, monkeypatch):
    from bathos.cli_cyclopts import app

    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=source)

    monkeypatch.setenv("BTH_CATALOG_DIR", str(dest))

    result = runner.invoke(app, ["migrate", "--consolidate-catalog", str(root), "--to-log"])

    assert result.exit_code != 0
    assert "mutually exclusive" in result.output


def test_cli_consolidate_catalog_missing_key_without_source_catalog_errors(
    tmp_path: Path, monkeypatch
):
    from bathos.cli_cyclopts import app

    root = tmp_path / "root"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=None)  # no explicit catalog_dir key

    monkeypatch.setenv("BTH_CATALOG_DIR", str(dest))

    result = runner.invoke(app, ["migrate", "--consolidate-catalog", str(root)])

    assert result.exit_code != 0
    assert "nothing to consolidate" in result.output


def test_cli_consolidate_catalog_source_catalog_override_works(tmp_path: Path, monkeypatch):
    """The second pass of the documented race-window procedure: the key is
    gone from `.bth.toml`, so `--source-catalog` supplies the path instead."""
    from bathos.cli_cyclopts import app

    root = tmp_path / "root"
    source = tmp_path / "source-cat"
    dest = tmp_path / "dest-cat"
    dest.mkdir(parents=True)
    _write_bth_toml(root, catalog_dir=None)  # key already removed + committed

    (source / "campaigns").mkdir(parents=True)
    (source / "campaigns" / "c.json").write_text('{"id": "c"}')

    monkeypatch.setenv("BTH_CATALOG_DIR", str(dest))

    result = runner.invoke(
        app,
        ["migrate", "--consolidate-catalog", str(root), "--source-catalog", str(source)],
    )

    assert result.exit_code == 0, result.output
    assert '"status": "ok"' in result.output
    assert (dest / "campaigns" / "c.json").is_file()

    # A second run afterward (nothing left to copy) still reports clean 0-copy.
    result2 = runner.invoke(
        app,
        ["migrate", "--consolidate-catalog", str(root), "--source-catalog", str(source)],
    )
    assert result2.exit_code == 0, result2.output
    assert '"copied": 0' in result2.output
