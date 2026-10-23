"""Per-run provenance of named installed dependencies (debt #2946).

The probe runs in the SCRIPT's interpreter, so these tests build a fake installed
distribution (a dist-info with RECORD/WHEEL/direct_url.json) on a PYTHONPATH site dir and
point `run_script` at `sys.executable`. The shadowing case puts a source copy of the package
next to the script, which is exactly what `sys.path[0]` prefers at run time.
"""

from __future__ import annotations

import base64
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import duckdb
import pytest

from bathos.catalog import read_runs
from bathos.dependency_provenance import (
    UNKNOWN,
    DependencyProvenanceConfigError,
    capture_dependency_provenance,
    key_prefix,
    probe_command,
    resolve_tracked_packages,
    shadowed_packages,
)
from bathos.runner import run_script

PY = sys.executable


def _record_line(site: Path, rel: str) -> str:
    data = (site / rel).read_bytes()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    return f"{rel},sha256={digest},{len(data)}"


def _install_fake_dist(
    site: Path,
    *,
    dist: str = "fakepkg",
    module: str = "fakepkg",
    version: str = "1.2.3",
    direct_url: dict | None = None,
    wheel_tag: str = "py3-none-any",
    package_files: bool = True,
    extra_record: list[str] | None = None,
) -> Path:
    """Write `<module>/__init__.py` plus `<dist>-<version>.dist-info` into `site`."""
    site.mkdir(parents=True, exist_ok=True)
    files: list[str] = []
    if package_files:
        (site / module).mkdir(exist_ok=True)
        (site / module / "__init__.py").write_text(f"__version__ = {version!r}\n")
        files.append(f"{module}/__init__.py")
    info = site / f"{dist.replace('-', '_')}-{version}.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {dist}\nVersion: {version}\n")
    (info / "WHEEL").write_text(f"Wheel-Version: 1.0\nRoot-Is-Purelib: true\nTag: {wheel_tag}\n")
    files += [f"{info.name}/METADATA", f"{info.name}/WHEEL"]
    if direct_url is not None:
        (info / "direct_url.json").write_text(json.dumps(direct_url))
        files.append(f"{info.name}/direct_url.json")
    for rel in extra_record or []:
        files.append(rel)
    lines = [_record_line(site, f) if (site / f).is_file() else f"{f},," for f in files]
    lines.append(f"{info.name}/RECORD,,")
    (info / "RECORD").write_text("\n".join(lines) + "\n")
    return info


@pytest.fixture
def site(tmp_path: Path, monkeypatch) -> Path:
    site = tmp_path / "site"
    site.mkdir()
    monkeypatch.setenv("PYTHONPATH", str(site))
    return site


@pytest.fixture
def script(tmp_path: Path) -> Path:
    scripts = tmp_path / "proj" / "scripts"
    scripts.mkdir(parents=True)
    s = scripts / "probe_me.py"
    s.write_text("print('hi')\n")
    return s


def _capture(entries: list[str], script: Path) -> dict:
    prov = capture_dependency_provenance(entries, [PY, str(script)], script.parent.parent)
    assert prov is not None
    assert prov["probe_status"] == "ok", prov
    return prov


# --- configuration -------------------------------------------------------------------------


def test_track_packages_is_the_union_of_sidecar_bth_toml_and_pyproject(tmp_path: Path):
    proj = tmp_path / "proj"
    (proj / "scripts").mkdir(parents=True)
    (proj / ".bth.toml").write_text('[provenance]\ntrack_packages = ["b", "a"]\n')
    (proj / "pyproject.toml").write_text('[tool.bathos]\ntrack_packages = ["c", "a"]\n')
    sidecar = proj / "scripts" / "x.bth.toml"
    sidecar.write_text('[experiment]\nhypothesis = "h"\n[provenance]\ntrack_packages = ["a"]\n')

    assert resolve_tracked_packages(sidecar, proj / "scripts") == ["a", "b", "c"]


def test_no_setting_anywhere_tracks_nothing(tmp_path: Path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname = 'x'\n")
    assert resolve_tracked_packages(None, tmp_path) == []
    assert capture_dependency_provenance([], [PY, "x.py"], tmp_path) is None


@pytest.mark.parametrize("value", ['"aminx"', "[1]", '["bad name"]', '["a:1mod"]'])
def test_malformed_track_packages_is_refused_not_ignored(tmp_path: Path, value: str):
    (tmp_path / ".bth.toml").write_text(f"[provenance]\ntrack_packages = {value}\n")
    with pytest.raises(DependencyProvenanceConfigError):
        resolve_tracked_packages(None, tmp_path)


def test_unparseable_config_that_mentions_track_packages_is_refused(tmp_path: Path):
    (tmp_path / ".bth.toml").write_text("[provenance\ntrack_packages = [\n")
    with pytest.raises(DependencyProvenanceConfigError):
        resolve_tracked_packages(None, tmp_path)


def test_key_prefix_is_a_json_path_safe_normalised_name():
    assert key_prefix("Scikit-Learn") == "scikit_learn"
    assert key_prefix("zope.interface") == "zope_interface"


# --- interpreter / command reconstruction --------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "expected_prefix", "path0"),
    [
        (["python", "scripts/x.py", "--n", "3"], ["python"], "scripts"),
        (["python3", "-X", "dev", "-u", "scripts/x.py"], ["python3", "-X", "dev", "-u"], "scripts"),
        (["python", "-m", "pkg.mod"], ["python"], "."),
        (
            ["uv", "run", "--extra", "dev", "--frozen", "python", "scripts/x.py"],
            ["uv", "run", "--extra", "dev", "--frozen", "python"],
            "scripts",
        ),
        (
            ["uv", "run", "--no-sync", "scripts/x.py"],
            ["uv", "run", "--no-sync", "python"],
            "scripts",
        ),
        (
            ["env", "FOO=1", "timeout", "900", "python", "scripts/x.py"],
            ["env", "FOO=1", "timeout", "900", "python"],
            "scripts",
        ),
    ],
)
def test_probe_command_mirrors_the_launcher(tmp_path: Path, argv, expected_prefix, path0):
    (tmp_path / "scripts").mkdir()
    (tmp_path / "scripts" / "x.py").write_text("")
    got = probe_command(argv, tmp_path)
    assert got is not None
    prefix, got_path0 = got
    assert prefix == expected_prefix
    assert Path(got_path0) == (tmp_path / path0).resolve()


@pytest.mark.parametrize("argv", [["python", "-c", "print(1)"], ["bash", "run.sh"], ["uv", "sync"]])
def test_probe_command_declines_what_it_cannot_mirror(tmp_path: Path, argv):
    assert probe_command(argv, tmp_path) is None


def test_probe_command_reads_a_python_shebang(tmp_path: Path):
    s = tmp_path / "x.py"
    s.write_text("#!/usr/bin/env python3\nprint(1)\n")
    assert probe_command([str(s)], tmp_path) == (["python3"], str(tmp_path.resolve()))


def test_non_python_command_records_unknown_with_a_reason(tmp_path: Path):
    prov = capture_dependency_provenance(["fakepkg"], ["bash", "run.sh"], tmp_path)
    assert prov is not None
    assert prov["probe_status"] == "no_python_interpreter_in_command"
    assert prov["fakepkg_version"] == UNKNOWN
    assert prov["fakepkg_module_in_dist"] == UNKNOWN


# --- the probe -----------------------------------------------------------------------------


def test_installed_vcs_dist_records_version_commit_hash_and_containment(site: Path, script: Path):
    _install_fake_dist(
        site,
        direct_url={
            "url": "https://example.invalid/fakepkg-1.2.3-py3-none-any.whl",
            "archive_info": {"hashes": {"sha256": "ab" * 32}},
            "vcs_info": {"vcs": "git", "commit_id": "c0ffee" * 6 + "abcd"},
        },
    )
    prov = _capture(["fakepkg"], script)

    assert prov["fakepkg_version"] == "1.2.3"
    assert prov["fakepkg_wheel_sha256"] == "ab" * 32
    assert prov["fakepkg_wheel_sha256_source"] == "direct_url"
    assert prov["fakepkg_source_commit"] == "c0ffee" * 6 + "abcd"
    assert prov["fakepkg_module_in_dist"] is True
    assert prov["fakepkg_module_path"] == str((site / "fakepkg" / "__init__.py").resolve())
    assert prov["probe_python"]
    assert shadowed_packages(prov, ["fakepkg"]) == []


def test_source_copy_next_to_the_script_is_detected_as_shadowing(site: Path, script: Path):
    """The reference failure: metadata says 1.2.3, but `import fakepkg` loads a local copy."""
    _install_fake_dist(site)
    (script.parent / "fakepkg").mkdir()
    (script.parent / "fakepkg" / "__init__.py").write_text("__version__ = 'vendored'\n")

    prov = _capture(["fakepkg"], script)

    assert prov["fakepkg_version"] == "1.2.3"  # what importlib.metadata alone would report
    assert prov["fakepkg_module_in_dist"] is False
    assert prov["fakepkg_module_path"] == str((script.parent / "fakepkg" / "__init__.py").resolve())
    assert shadowed_packages(prov, ["fakepkg"]) == ["fakepkg"]


def test_importable_module_with_no_distribution_is_not_in_dist(site: Path, script: Path):
    (site / "loosepkg").mkdir()
    (site / "loosepkg" / "__init__.py").write_text("")
    prov = _capture(["loosepkg"], script)
    assert prov["loosepkg_version"] == UNKNOWN
    assert prov["loosepkg_module_in_dist"] is False


@pytest.mark.usefixtures("site")
def test_absent_package_is_unknown_everywhere(script: Path):
    prov = _capture(["nosuchpkg"], script)
    for suffix in ("version", "wheel_sha256", "source_commit", "module_path", "module_in_dist"):
        assert prov[f"nosuchpkg_{suffix}"] == UNKNOWN


def test_dist_and_module_names_may_differ(site: Path, script: Path):
    _install_fake_dist(site, dist="fake-dist", module="fakemod")
    (site / "fake_dist-1.2.3.dist-info" / "top_level.txt").write_text("fakemod\n")
    prov = _capture(["fake-dist"], script)
    assert prov["fake_dist_module_in_dist"] is True
    assert prov["fake_dist_module_path"].endswith("fakemod/__init__.py")
    # explicit dist:module form
    prov2 = _capture(["fake-dist:fakemod"], script)
    assert prov2["fake_dist_module_in_dist"] is True


def _build_wheel(path: Path, init_text: str) -> None:
    """A minimal wheel whose payload RECORD row hashes `fakepkg/__init__.py` = init_text."""
    import zipfile

    data = init_text.encode()
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("fakepkg/__init__.py", data)
        zf.writestr(
            "fakepkg-1.2.3.dist-info/RECORD",
            f"fakepkg/__init__.py,sha256={digest},{len(data)}\nfakepkg-1.2.3.dist-info/RECORD,,\n",
        )


def test_local_wheel_file_is_hashed_when_it_matches_the_install(
    site: Path, script: Path, tmp_path: Path
):
    wheel = tmp_path / "wheels" / "fakepkg-1.2.3-py3-none-any.whl"
    _build_wheel(wheel, "__version__ = '1.2.3'\n")  # == what _install_fake_dist writes
    _install_fake_dist(site, direct_url={"url": wheel.as_uri(), "archive_info": {}})
    prov = _capture(["fakepkg"], script)
    assert prov["fakepkg_wheel_sha256"] == hashlib.sha256(wheel.read_bytes()).hexdigest()
    assert prov["fakepkg_wheel_sha256_source"] == "wheel_file"
    assert prov["fakepkg_source_commit"] == UNKNOWN


def test_local_wheel_rebuilt_after_install_is_not_trusted(site: Path, script: Path, tmp_path: Path):
    wheel = tmp_path / "wheels" / "fakepkg-1.2.3-py3-none-any.whl"
    _build_wheel(wheel, "__version__ = 'rebuilt'\n")
    _install_fake_dist(site, direct_url={"url": wheel.as_uri(), "archive_info": {}})
    prov = _capture(["fakepkg"], script)
    assert prov["fakepkg_wheel_sha256"] == UNKNOWN
    assert prov["fakepkg_wheel_sha256_source"] == "wheel_file_changed_since_install"


def test_local_file_that_is_not_a_readable_wheel_is_labelled_unverified(
    site: Path, script: Path, tmp_path: Path
):
    wheel = tmp_path / "wheels" / "fakepkg-1.2.3-py3-none-any.whl"
    wheel.parent.mkdir()
    wheel.write_bytes(b"not a zip")
    _install_fake_dist(site, direct_url={"url": wheel.as_uri(), "archive_info": {}})
    prov = _capture(["fakepkg"], script)
    assert prov["fakepkg_wheel_sha256_source"] == "wheel_file_unverified"


def test_uv_lock_supplies_the_wheel_hash_matching_the_installed_tags(site: Path, script: Path):
    _install_fake_dist(site, wheel_tag="cp313-cp313-manylinux_2_17_x86_64")
    (script.parent.parent / "uv.lock").write_text(
        "version = 1\n"
        "[[package]]\n"
        'name = "fakepkg"\n'
        'version = "1.2.3"\n'
        "wheels = [\n"
        '  { url = "https://x.invalid/fakepkg-1.2.3-cp313-cp313-macosx_11_0_arm64.whl", hash = "sha256:'
        + "11"
        * 32
        + '" },\n'
        '  { url = "https://x.invalid/fakepkg-1.2.3-cp313-cp313-manylinux_2_17_x86_64.whl", hash = "sha256:'
        + "22"
        * 32
        + '" },\n'
        "]\n"
    )
    # The lock is only consulted for the project's own interpreter.
    venv_py = script.parent.parent / ".venv" / "bin" / "python"
    venv_py.parent.mkdir(parents=True)
    venv_py.symlink_to(PY)
    prov = capture_dependency_provenance(
        ["fakepkg"], [str(venv_py), str(script)], script.parent.parent
    )
    assert prov is not None and prov["probe_status"] == "ok", prov
    assert prov["fakepkg_wheel_sha256"] == "22" * 32
    assert prov["fakepkg_wheel_sha256_source"] == "uv.lock"


def test_scm_local_version_label_supplies_the_commit(site: Path, script: Path):
    _install_fake_dist(site, version="0.1.0a25.dev3+g1a2b3c4d")
    prov = _capture(["fakepkg"], script)
    assert prov["fakepkg_source_commit"] == "1a2b3c4d"


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_editable_install_records_source_head_and_counts_as_in_dist(
    site: Path, script: Path, tmp_path: Path, monkeypatch
):
    src = tmp_path / "checkout"
    (src / "fakepkg").mkdir(parents=True)
    (src / "fakepkg" / "__init__.py").write_text("")
    _git(src, "init", "-q")
    _git(src, "add", ".")
    _git(src, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "init")
    head = _git(src, "rev-parse", "HEAD")
    # PEP 660: dist-info in site-packages, a .pth puts the source tree on sys.path. A .pth is
    # only processed in a real site dir, not a PYTHONPATH entry, so add the tree directly.
    (site / "_fakepkg_editable.pth").write_text(str(src) + "\n")
    monkeypatch.setenv("PYTHONPATH", f"{site}:{src}")
    _install_fake_dist(
        site,
        package_files=False,
        direct_url={"url": src.as_uri(), "dir_info": {"editable": True}},
        extra_record=["_fakepkg_editable.pth"],
    )

    prov = _capture(["fakepkg"], script)
    assert prov["fakepkg_source_commit"] == head
    assert prov["fakepkg_module_in_dist"] is True

    (src / "fakepkg" / "__init__.py").write_text("x = 1\n")
    assert _capture(["fakepkg"], script)["fakepkg_source_commit"] == f"{head}-dirty"


# --- end to end through bth run ------------------------------------------------------------


def test_run_records_dependency_provenance_queryable_after_compact(
    site: Path, script: Path, tmp_path: Path
):
    from bathos.compact import compact

    _install_fake_dist(
        site, direct_url={"url": "https://x.invalid/f.whl", "vcs_info": {"commit_id": "abc123"}}
    )
    proj = script.parent.parent
    (proj / ".bth.toml").write_text('[provenance]\ntrack_packages = ["fakepkg"]\n')
    catalog = tmp_path / "catalog"

    assert (
        run_script([PY, str(script)], "testproj", catalog, [], [], cwd=proj, no_sidecar=True) == 0
    )

    (run,) = read_runs(catalog)
    prov = json.loads(run.dependency_provenance)
    assert prov["fakepkg_version"] == "1.2.3"
    assert prov["fakepkg_source_commit"] == "abc123"
    assert prov["fakepkg_module_in_dist"] is True

    compact(catalog)
    con = duckdb.connect(str(catalog / "bathos.db"), read_only=True)
    try:
        row = con.execute(
            "SELECT json_extract_string(dependency_provenance, '$.fakepkg_source_commit'), "
            "json_extract_string(dependency_provenance, '$.fakepkg_module_in_dist') FROM runs"
        ).fetchone()
    finally:
        con.close()
    assert row == ("abc123", "true")


def test_run_without_track_packages_records_null(tmp_path: Path, script: Path):
    proj = script.parent.parent
    catalog = tmp_path / "catalog"
    assert (
        run_script([PY, str(script)], "testproj", catalog, [], [], cwd=proj, no_sidecar=True) == 0
    )
    (run,) = read_runs(catalog)
    assert run.dependency_provenance is None


def test_run_warns_loudly_on_a_shadowed_package(site: Path, script: Path, tmp_path: Path, capsys):
    _install_fake_dist(site)
    (script.parent / "fakepkg").mkdir()
    (script.parent / "fakepkg" / "__init__.py").write_text("")
    proj = script.parent.parent
    (proj / ".bth.toml").write_text('[provenance]\ntrack_packages = ["fakepkg"]\n')
    catalog = tmp_path / "catalog"

    assert (
        run_script([PY, str(script)], "testproj", catalog, [], [], cwd=proj, no_sidecar=True) == 0
    )

    assert "NOT the installed distribution" in capsys.readouterr().err
    (run,) = read_runs(catalog)
    assert json.loads(run.dependency_provenance)["fakepkg_module_in_dist"] is False


def test_run_is_refused_on_malformed_track_packages(tmp_path: Path, script: Path, capsys):
    proj = script.parent.parent
    (proj / ".bth.toml").write_text('[provenance]\ntrack_packages = "fakepkg"\n')
    catalog = tmp_path / "catalog"
    assert (
        run_script([PY, str(script)], "testproj", catalog, [], [], cwd=proj, no_sidecar=True) == 1
    )
    assert '"error_code": "invalid_param"' in capsys.readouterr().err
    assert not (catalog / "runs").exists() or read_runs(catalog) == []
