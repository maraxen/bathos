"""The myxcel boundary: what the installed `myxcel` CLI/API actually
supports, and that bathos never calls a myxcel flag/positional shape that
doesn't exist.

Context (260927, task 260927_bathos-next-sprint item 4): CLAUDE.md's cluster
rules ("Bathos Sync Delegates to Myxcel") require cluster file transfers to
go through myxcel rather than raw `rsync`/`ssh`. Two spots needed checking:

1. `bathos.cluster.pull_path` used to shell out to
   `myxcel pull <remote> <remote_path> --dest <local_dest>` -- a shape that
   does not exist on the installed `myxcel` CLI (`myxcel pull --help` is
   `myxcel pull [OPTIONS] {remote} {project}`, no `--dest`). It now calls
   `myxcel pull-path <remote> <path> --dest <dest>` (myxcel backlog #5880);
   an installed myxcel that predates that subcommand raises
   `bathos.cluster.MyxcelCapabilityGapError` (see that function's docstring).
2. `bathos.runlog.migrate.rsync_full_mirror` calls `rsync` directly on
   purpose, because myxcel has no capability that mirrors an arbitrary
   remote directory into an arbitrary local destination with `--checksum`
   comparison and no deletion -- `myxcel pull` is bound to a
   myxcel-registered project's configured `pull_paths`. This is verified
   below by pinning the installed myxcel signature (skipped gracefully if
   myxcel is not installed/importable on the machine running the tests).
"""

from __future__ import annotations

import glob
import importlib.util
import inspect
import sys
from unittest.mock import MagicMock, patch

import pytest

from bathos.cluster import pull_path
from bathos.runlog.migrate import rsync_full_mirror


def _load_installed_myxcel_module(dotted_name: str):
    """Import a module from the installed `myxcel` uv-tool's site-packages,
    without relying on it being on `sys.path` for bathos's own venv (it
    generally is not -- myxcel is installed as its own isolated uv tool).
    Returns None if the installed myxcel package (or the requested
    submodule) cannot be found, so callers can skip gracefully rather than
    fail on a machine/CI image without myxcel installed."""
    candidates = glob.glob(
        "/home/marielle/.local/share/uv/tools/myxcel/lib/python*/site-packages/myxcel"
    )
    if not candidates:
        return None
    pkg_dir = candidates[0]
    rel = dotted_name.split(".")[1:]  # drop leading "myxcel"
    file_path = f"{pkg_dir}/__init__.py" if not rel else f"{pkg_dir}/{'/'.join(rel)}.py"
    spec = importlib.util.spec_from_file_location(dotted_name, file_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    try:
        # Register in sys.modules under its real dotted name so relative
        # imports inside the myxcel package (e.g. `from myxcel.config import
        # ...`) resolve, and so we don't collide with a same-named bathos
        # module.
        if "myxcel" not in sys.modules:
            pkg_spec = importlib.util.spec_from_file_location(
                "myxcel", f"{pkg_dir}/__init__.py", submodule_search_locations=[pkg_dir]
            )
            pkg_module = importlib.util.module_from_spec(pkg_spec)
            sys.modules["myxcel"] = pkg_module
            pkg_spec.loader.exec_module(pkg_module)
        sys.modules[dotted_name] = module
        spec.loader.exec_module(module)
    except ImportError:
        # A dependency of the installed myxcel tool (e.g. `typer`) isn't
        # installed in *this* project's venv -- myxcel is an isolated uv
        # tool, not a bathos dependency, so this is expected on most
        # machines. Treat exactly like "not importable" and let the caller
        # skip.
        sys.modules.pop(dotted_name, None)
        return None
    return module


def test_installed_myxcel_rsync_pull_is_project_bound_not_arbitrary_path():
    """Pin the assumption `pull_path`'s docstring and `MyxcelCapabilityGapError`
    are built on: `myxcel.rsync.pull()` takes a `project` name (and resolves
    the destination from `profile`/`worktree`), never an explicit remote
    path + local destination pair. If myxcel ever grows `remote_path`/
    `local_dest`/`dest` parameters here, `pull_path` should be re-enabled
    through them instead of refusing."""
    myxcel_rsync = _load_installed_myxcel_module("myxcel.rsync")
    if myxcel_rsync is None:
        pytest.skip("myxcel not installed/importable on this machine")

    sig = inspect.signature(myxcel_rsync.pull)
    params = set(sig.parameters)
    assert "project" in params
    assert not ({"remote_path", "local_dest", "dest"} & params), (
        "myxcel.rsync.pull() gained an arbitrary-path parameter -- "
        "bathos.cluster.pull_path can likely be re-enabled through it now"
    )


def test_installed_myxcel_cli_pull_path_matches_bathos_argv():
    """`bathos.cluster.pull_path` shells out to
    `myxcel pull-path <remote> <remote_path> --dest <local_dest>` (myxcel
    backlog #5880). If the installed myxcel predates it, skip (the wrapper
    then raises `MyxcelCapabilityGapError`); if it has it, pin the signature
    so an argv drift between the two repos fails here, not on a real pull.
    This doubles as the spec's "grep the installed package" gate before L3."""
    myxcel_cli = _load_installed_myxcel_module("myxcel.cli")
    if myxcel_cli is None:
        pytest.skip("myxcel not installed/importable on this machine")
    if "def pull_path(" not in inspect.getsource(myxcel_cli):
        pytest.skip("installed myxcel predates `pull-path` (myxcel backlog #5880)")

    params = list(inspect.signature(myxcel_cli.pull_path).parameters)
    assert params[:2] == ["remote", "remote_path"]
    assert "dest" in params


def test_pull_path_never_shells_out_to_old_pull_dest_shape():
    """`pull_path` must use the `myxcel pull-path ... --dest` subcommand
    (backlog #5880), never the old `myxcel pull <remote> <path> --dest`
    shape, which the installed `myxcel pull` does not accept. Gap handling
    for an installed myxcel that predates `pull-path` is covered in
    tests/test_cluster.py."""
    with patch("bathos.cluster.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
        pull_path("engaging", "~/projects/testproj/.bth/log/", "/local/dest/log/")
    argv = mock_run.call_args[0][0]
    assert argv[:2] == ["myxcel", "pull-path"]
    assert "--dest" in argv


def test_rsync_full_mirror_uses_checksum_no_delete_no_ignore_existing():
    """`rsync_full_mirror` (the seam `mirror_remote_runs_full` calls for
    migration step 1's full remote `runs/` mirror) must compare by checksum,
    never delete locally, and never skip existing files -- spec semantics:
    a full copy of `<remote_catalog>/runs/` into
    `catalog/remote-runs/<root id>/<remote>/`."""
    with patch("bathos.runlog.migrate.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
        rsync_full_mirror("engaging:/remote/runs/", "/local/dest/runs/")

    argv = mock_run.call_args[0][0]
    assert argv[0] == "rsync"
    assert "--checksum" in argv
    assert "--delete" not in argv
    assert "--ignore-existing" not in argv
    assert "engaging:/remote/runs/" in argv
    assert "/local/dest/runs/" in argv
    # Goes over ssh, not myxcel -- documented as an intentional gap, not an
    # oversight; see rsync_full_mirror's docstring.
    assert "-e" in argv


def test_rsync_full_mirror_does_not_raise_on_nonzero_exit():
    """Best-effort seam (per `pull_and_mirror_all_remotes`'s docstring: one
    remote's failure must not abort every other root/remote) -- confirms
    `rsync_full_mirror` itself stays a thin, non-raising subprocess call so
    the `contextlib.suppress(Exception)` wrapping in its caller isn't the
    only thing standing between a bad remote and an aborted migration."""
    with patch("bathos.runlog.migrate.subprocess.run") as mock_run:
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="connection refused")
        rsync_full_mirror("badhost:/remote/runs/", "/local/dest/runs/")  # must not raise
    mock_run.assert_called_once()
