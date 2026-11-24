"""Tests for WP01: parity-store location resolver, content-addressed store, and bth parity where."""

from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import pytest

from bathos.cli_cyclopts import app
from bathos.parity.store import (
    LAYER_CLI,
    LAYER_ENV,
    LAYER_OFF,
    LAYER_PYPROJECT,
    LAYER_XDG,
    ParityStore,
    ParityStoreConfigError,
    ParityStoreUnavailableError,
    lint_no_default_store_path,
    require_parity_store,
    resolve_parity_store,
)


def _isolated_env(tmp_path: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
    xdg_empty = tmp_path / "empty_xdg"
    xdg_empty.mkdir(exist_ok=True)
    env: dict[str, str] = {"XDG_CONFIG_HOME": str(xdg_empty)}
    if extra:
        env.update(extra)
    return env


class TestParityStoreResolution:
    def test_precedence_all_five_layers(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        nested = proj / "scripts" / "parity"
        nested.mkdir(parents=True)
        (proj / "pyproject.toml").write_text(
            '[project]\nname = "demo"\n[tool.bathos.parity]\nstore = ".parity_from_pyproject"\n',
            encoding="utf-8",
        )
        xdg = tmp_path / "xdg"
        (xdg / "bathos").mkdir(parents=True)
        (xdg / "bathos" / "config.toml").write_text(
            '[parity]\nstore = "' + str(tmp_path / "from_xdg") + '"\n',
            encoding="utf-8",
        )
        explicit_dir = tmp_path / "explicit_store"
        env_dir = tmp_path / "env_store"

        # 1. explicit beats env, pyproject, xdg
        r1 = resolve_parity_store(
            explicit=explicit_dir,
            start_dir=nested,
            env={"BTH_PARITY_STORE": str(env_dir), "XDG_CONFIG_HOME": str(xdg)},
        )
        assert r1.source == LAYER_CLI
        assert r1.path == explicit_dir.resolve()

        # 2. BTH_PARITY_STORE beats pyproject and xdg
        r2 = resolve_parity_store(
            start_dir=nested,
            env={"BTH_PARITY_STORE": str(env_dir), "XDG_CONFIG_HOME": str(xdg)},
        )
        assert r2.source == LAYER_ENV
        assert r2.path == env_dir.resolve()

        # 2b. BTH_PARITY_STORE="" or "none" disables lower layers
        r2_off = resolve_parity_store(
            start_dir=nested,
            env={"BTH_PARITY_STORE": "none", "XDG_CONFIG_HOME": str(xdg)},
        )
        assert r2_off.path is None
        assert r2_off.source == f"{LAYER_ENV}:disabled"

        r2_empty = resolve_parity_store(
            start_dir=nested,
            env={"BTH_PARITY_STORE": "", "XDG_CONFIG_HOME": str(xdg)},
        )
        assert r2_empty.path is None
        assert r2_empty.source == f"{LAYER_ENV}:disabled"

        # 3. pyproject [tool.bathos.parity] beats xdg
        r3 = resolve_parity_store(start_dir=nested, env={"XDG_CONFIG_HOME": str(xdg)})
        assert r3.source == LAYER_PYPROJECT
        assert r3.path == (proj / ".parity_from_pyproject").resolve()

        # 4. xdg config beats off
        outside = tmp_path / "outside"
        outside.mkdir()
        r4 = resolve_parity_store(start_dir=outside, env={"XDG_CONFIG_HOME": str(xdg)})
        assert r4.source == LAYER_XDG
        assert r4.path == (tmp_path / "from_xdg").resolve()

        # 5. off when nothing configured
        r5 = resolve_parity_store(start_dir=outside, env=_isolated_env(tmp_path))
        assert r5.source == LAYER_OFF
        assert r5.path is None

    def test_malformed_pyproject_raises(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text("[tool.bathos.parity\nbroken =", encoding="utf-8")
        with pytest.raises(ParityStoreConfigError, match="Malformed pyproject.toml"):
            resolve_parity_store(start_dir=tmp_path, env=_isolated_env(tmp_path))

    def test_malformed_xdg_config_raises(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        xdg = tmp_path / "xdg"
        (xdg / "bathos").mkdir(parents=True)
        (xdg / "bathos" / "config.toml").write_text("[parity]\nstore = 123\n", encoding="utf-8")
        with pytest.raises(ParityStoreConfigError, match="must be a string"):
            resolve_parity_store(start_dir=proj, env={"XDG_CONFIG_HOME": str(xdg)})

    def test_nc_unconfigured_refuses_with_full_resolution_trace(self, tmp_path: Path) -> None:
        isolated = tmp_path / "no_cfg"
        isolated.mkdir()
        with pytest.raises(ParityStoreUnavailableError) as exc_info:
            require_parity_store(start_dir=isolated, env=_isolated_env(tmp_path))
        msg = str(exc_info.value)
        for layer_name in (LAYER_CLI, LAYER_ENV, LAYER_PYPROJECT, LAYER_XDG, LAYER_OFF):
            assert layer_name in msg

    def test_nc_no_default_store_path_lint_passes_and_catches_violation(self, tmp_path: Path) -> None:
        parity_pkg = Path(__file__).resolve().parent.parent / "src" / "bathos" / "parity"
        assert lint_no_default_store_path(parity_pkg) == []

        bad_src = tmp_path / "bad_pkg"
        bad_src.mkdir()
        (bad_src / "store.py").write_text(
            'def fallback():\n    return "~/.bth/parity"\n', encoding="utf-8"
        )
        violations = lint_no_default_store_path(bad_src)
        assert len(violations) == 1
        assert ".bth/parity" in violations[0]


class TestParityStoreOperations:
    def test_put_get_prefix_and_atomic_dir(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        sha = store.put_bytes(b"hello parity")
        assert store.blob_path(sha) == store.root / sha[:2] / sha
        assert store.has(sha)
        assert store.get_bytes(sha) == b"hello parity"
        assert store.resolve_prefix(sha[:12]) == sha

        def _populate(d: Path) -> None:
            (d / "marker.json").write_text('{"ok":true}\n', encoding="utf-8")

        unit = store.write_unit_dir("units/cell-1", _populate)
        assert (unit / "marker.json").is_file()

    def test_ambiguous_prefix_refused(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store")
        shard = store.root / "aa"
        shard.mkdir(parents=True)
        (shard / ("a" * 64)).write_bytes(b"1")
        (shard / ("a" * 63 + "b")).write_bytes(b"2")
        with pytest.raises(ValueError, match="Ambiguous"):
            store.resolve_prefix("aaaa")

    def test_quota_warnings(self, tmp_path: Path) -> None:
        store = ParityStore(tmp_path / "store", max_files=1, max_bytes=10)
        store.put_bytes(b"12345")
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            msgs = store.check_quota(additional_files=1, additional_bytes=20)
        assert len(msgs) == 2
        assert len(caught) == 2


class TestCliParityWhere:
    def test_bth_parity_where(self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> None:
        store_dir = tmp_path / "my_store"
        monkeypatch.setenv("BTH_PARITY_STORE", str(store_dir))
        app(["parity", "where", "--json"], exit_on_error=False)
        out = json.loads(capsys.readouterr().out)
        assert out["source"] == LAYER_ENV
        assert Path(out["path"]) == store_dir.resolve()

    def test_bth_parity_where_off_exit_zero_and_prints_off(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        isolated = tmp_path / "isolated"
        isolated.mkdir()
        xdg_empty = tmp_path / "xdg_empty"
        xdg_empty.mkdir()
        monkeypatch.delenv("BTH_PARITY_STORE", raising=False)
        monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_empty))
        old_cwd = os.getcwd()
        try:
            os.chdir(isolated)
            app(["parity", "where"], exit_on_error=False)
        finally:
            os.chdir(old_cwd)
        stdout = capsys.readouterr().out
        assert "off" in stdout
