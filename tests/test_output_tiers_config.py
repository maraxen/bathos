"""Output-tier profile resolution (spec 261023_output-tiering section 5.1, step 1)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bathos.output_tiers.config import (
    OutputTiersConfigError,
    load_profile,
    resolve_profile,
    user_config_path,
)


@pytest.fixture
def layers(tmp_path, monkeypatch):
    """A project dir with no pyproject yet, an empty XDG config home, and no profile env vars."""
    monkeypatch.delenv("BTH_COLD_PROFILE", raising=False)
    monkeypatch.delenv("BTH_MIRROR_PROFILE", raising=False)
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg))
    project = tmp_path / "proj"
    (project / "sub").mkdir(parents=True)

    class L:
        root = project
        start = project / "sub"

        @staticmethod
        def write_project(text: str) -> Path:
            p = project / "pyproject.toml"
            p.write_text(text)
            return p

        @staticmethod
        def write_user(text: str) -> Path:
            p = xdg / "bathos" / "config.toml"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            return p

    return L


def test_no_layer_set_is_off_and_lists_layers(layers):
    choice = resolve_profile("cold", start=layers.start)
    assert choice.name is None
    assert choice.layer.startswith("off")
    assert "$BTH_COLD_PROFILE" in choice.layer
    assert str(user_config_path()) in choice.layer


def test_user_layer_used_when_nothing_above(layers):
    user = layers.write_user('[outputs]\ncold_profile = "engaging"\nmirror_profile = "titanix"\n')
    assert resolve_profile("cold", start=layers.start).name == "engaging"
    mirror = resolve_profile("mirror", start=layers.start)
    assert (mirror.name, mirror.layer) == ("titanix", f"user config {user}")


def test_project_wins_over_user(layers):
    layers.write_user('[outputs]\ncold_profile = "engaging"\n')
    pyproject = layers.write_project('[tool.bathos.outputs]\ncold_profile = "titanix"\n')
    choice = resolve_profile("cold", start=layers.start)
    assert (choice.name, choice.layer) == ("titanix", f"project config {pyproject.resolve()}")


def test_env_wins_over_project(layers, monkeypatch):
    layers.write_project('[tool.bathos.outputs]\ncold_profile = "titanix"\n')
    monkeypatch.setenv("BTH_COLD_PROFILE", "engaging")
    choice = resolve_profile("cold", start=layers.start)
    assert (choice.name, choice.layer) == ("engaging", "$BTH_COLD_PROFILE")


def test_explicit_wins_over_env(layers, monkeypatch):
    monkeypatch.setenv("BTH_COLD_PROFILE", "engaging")
    choice = resolve_profile("cold", explicit="titanix", start=layers.start)
    assert (choice.name, choice.layer) == ("titanix", "explicit --profile")


@pytest.mark.parametrize("value", ["none", "NONE", "", "  "])
def test_env_none_or_empty_disables_and_stops(layers, monkeypatch, value):
    layers.write_project('[tool.bathos.outputs]\ncold_profile = "titanix"\n')
    monkeypatch.setenv("BTH_COLD_PROFILE", value)
    choice = resolve_profile("cold", start=layers.start)
    assert choice.name is None
    assert choice.layer == "$BTH_COLD_PROFILE (disabled)"


def test_explicit_none_disables(layers, monkeypatch):
    monkeypatch.setenv("BTH_MIRROR_PROFILE", "titanix")
    choice = resolve_profile("mirror", explicit="none", start=layers.start)
    assert (choice.name, choice.layer) == (None, "explicit --mirror (disabled)")


def test_project_none_disables_without_falling_to_user(layers):
    layers.write_user('[outputs]\nmirror_profile = "titanix"\n')
    layers.write_project('[tool.bathos.outputs]\nmirror_profile = "none"\n')
    choice = resolve_profile("mirror", start=layers.start)
    assert choice.name is None
    assert choice.layer.endswith("(disabled)")


def test_project_without_key_falls_through_to_user(layers):
    layers.write_user('[outputs]\ncold_profile = "engaging"\n')
    layers.write_project('[tool.bathos.outputs]\nmirror_profile = "titanix"\n')
    assert resolve_profile("cold", start=layers.start).name == "engaging"


def test_nearest_pyproject_only(layers):
    """An inner pyproject without the key hides an outer one: the outer project is not this project."""
    layers.write_project('[tool.bathos.outputs]\ncold_profile = "titanix"\n')
    (layers.start / "pyproject.toml").write_text('[project]\nname = "inner"\n')
    assert resolve_profile("cold", start=layers.start).name is None


def test_legacy_bth_config_is_not_read(layers):
    legacy = Path.home() / ".bth" / "config.toml"
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text('[outputs]\ncold_profile = "engaging"\n')
    assert resolve_profile("cold", start=layers.start).name is None


def test_malformed_project_toml_raises_naming_file(layers):
    pyproject = layers.write_project("[tool.bathos.outputs\ncold_profile = 1\n")
    with pytest.raises(OutputTiersConfigError, match=str(pyproject.resolve())):
        resolve_profile("cold", start=layers.start)


def test_malformed_user_toml_raises_naming_file(layers):
    user = layers.write_user("[outputs]\ncold_profile = \n")
    with pytest.raises(OutputTiersConfigError, match=str(user)):
        resolve_profile("cold", start=layers.start)


def test_unknown_key_raises(layers):
    layers.write_user('[outputs]\ncold_profle = "engaging"\n')
    with pytest.raises(OutputTiersConfigError, match="unknown key"):
        resolve_profile("cold", start=layers.start)


def test_non_string_value_raises(layers):
    layers.write_project("[tool.bathos.outputs]\ncold_profile = 3\n")
    with pytest.raises(OutputTiersConfigError, match="must be a string"):
        resolve_profile("cold", start=layers.start)


@pytest.mark.parametrize("name", ["../engaging", "a/b", ".hidden"])
def test_profile_name_with_path_refused(layers, name):
    with pytest.raises(OutputTiersConfigError, match="profile name"):
        resolve_profile("cold", explicit=name, start=layers.start)


def _profile(tmp_path: Path, name: str, text: str) -> Path:
    d = tmp_path / "myxcel"
    d.mkdir(exist_ok=True)
    (d / f"{name}.toml").write_text(text)
    return d


def test_load_profile_reads_contract_keys(tmp_path):
    d = _profile(
        tmp_path,
        "titanix",
        'schema_version = 1\nhost = "titanix"\ncold_root = "/home/solab/marielle"\n',
    )
    profile = load_profile("titanix", config_dir=d)
    assert (profile.host, profile.cold_root) == ("titanix", "/home/solab/marielle")
    assert (
        profile.destination("denxity", "1e4596c6-7a55")
        == "/home/solab/marielle/bathos-cold/denxity/1e4596c6-7a55"
    )


def test_load_profile_without_cold_root_refuses(tmp_path):
    d = _profile(tmp_path, "home-gpu", 'host = "home-gpu"\n')
    with pytest.raises(OutputTiersConfigError, match="home-gpu.toml") as exc:
        load_profile("home-gpu", config_dir=d)
    assert exc.value.error_code == "no_cold_root"


def test_load_profile_relative_cold_root_refuses(tmp_path):
    d = _profile(tmp_path, "eng", 'host = "engaging"\ncold_root = "~/cold"\n')
    with pytest.raises(OutputTiersConfigError, match="absolute") as exc:
        load_profile("eng", config_dir=d)
    assert exc.value.error_code == "no_cold_root"


def test_load_profile_missing_file_and_malformed(tmp_path):
    with pytest.raises(OutputTiersConfigError, match="not found"):
        load_profile("absent", config_dir=tmp_path)
    d = _profile(tmp_path, "bad", "host = \n")
    with pytest.raises(OutputTiersConfigError, match="bad.toml"):
        load_profile("bad", config_dir=d)


def test_destination_rejects_path_components(tmp_path):
    d = _profile(tmp_path, "eng", 'host = "engaging"\ncold_root = "/orcd/data"\n')
    profile = load_profile("eng", config_dir=d)
    with pytest.raises(OutputTiersConfigError):
        profile.destination("denxity", "../x")
