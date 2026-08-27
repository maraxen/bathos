"""Durable git provenance -- thin shim over cisternal.provenance.durable.

The worktree-snapshot / durable-ref / tracked-manifest mechanism (design
rationale in the original module's docstring, preserved verbatim below)
moved to `cisternal.provenance.durable`, generalized so `provenance_paths`,
ref prefixes, and the manifest path are caller-supplied rather than
hardcoded. This module supplies bathos's own values for those and
re-exports everything with the exact same public names/signatures bathos's
own callers (`runner.py`, `cli.py`, this module's test suite) already use --
no behavior change.

---

Make a run's git provenance durable, not merely recorded.

`bathos.git` captures WHAT the repo looked like. This module makes that capture survive, which is a
separate problem and the one that actually fails in practice.

Motivating measurement (tev_design catalog, 2026-08-18): `git_hash` was populated on 345/345 runs --
capture is not the gap -- but only 40.6% of those hashes still resolved to a commit, and 92.2% of
runs executed on a DIRTY tree. So the median run recorded a clean-looking hash describing a tree that
never existed, and two runs in five cited a commit that is simply gone.
"""

from __future__ import annotations

import os
from pathlib import Path

from cisternal.provenance.durable import (
    DEFAULT_MAX_SNAPSHOT_BYTES,
    SNAPSHOT_FULL,
    SNAPSHOT_METADATA_ONLY,
    SNAPSHOT_NONE,
    ImportReport,
    PinResult,
    SnapshotResult,
    pin_result_as_dict,  # re-exported unchanged
    ref_resolves,  # re-exported unchanged
    repo_root,  # re-exported unchanged
    snapshot_worktree,  # re-exported unchanged
    snapshot_worktree_detailed,  # re-exported unchanged
    update_ref,  # re-exported unchanged
)
from cisternal.provenance.durable import append_manifest as _append_manifest
from cisternal.provenance.durable import export_bundle as _export_bundle
from cisternal.provenance.durable import ignored_declared_paths as _ignored_declared_paths
from cisternal.provenance.durable import ignored_provenance_paths as _ignored_provenance_paths
from cisternal.provenance.durable import import_bundles as _import_bundles
from cisternal.provenance.durable import manifest_candidates as _manifest_candidates
from cisternal.provenance.durable import manifest_entry as _manifest_entry
from cisternal.provenance.durable import pin_run as _pin_run
from cisternal.provenance.durable import uncommitted_diff_for_run as _uncommitted_diff_for_run

RUN_REF_PREFIX = "refs/bathos/runs"
WIP_REF_PREFIX = "refs/bathos/wip"
MANIFEST_RELPATH = Path(".bth") / "refs" / "manifest.jsonl"

# Paths whose contents a run's provenance may point at, and which are therefore useless if the
# repository is configured to ignore them. `.bth/claims/` holds claim-tier pre-registrations, whose
# sha256 is the tamper anchor the Union Gate evaluates against at `campaign conclude`.
PROVENANCE_PATHS = (".bth/claims", ".bth/refs")

EXPORT_DIRNAME = Path("outputs") / "provenance"

__all__ = [
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "EXPORT_DIRNAME",
    "MANIFEST_RELPATH",
    "PROVENANCE_PATHS",
    "RUN_REF_PREFIX",
    "SNAPSHOT_FULL",
    "SNAPSHOT_METADATA_ONLY",
    "SNAPSHOT_NONE",
    "WIP_REF_PREFIX",
    "ImportReport",
    "PinResult",
    "SnapshotResult",
    "append_manifest",
    "export_bundle",
    "ignored_declared_paths",
    "ignored_provenance_paths",
    "import_bundles",
    "manifest_candidates",
    "manifest_entry",
    "pin_result_as_dict",
    "pin_run",
    "ref_resolves",
    "repo_root",
    "snapshot_worktree",
    "snapshot_worktree_detailed",
    "uncommitted_diff_for_run",
    "update_ref",
]


def ignored_provenance_paths(cwd: Path) -> tuple[str, ...]:
    return _ignored_provenance_paths(cwd, PROVENANCE_PATHS)


def ignored_declared_paths(paths: list[str] | tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    return _ignored_declared_paths(paths, cwd)


def append_manifest(entry: dict, cwd: Path) -> Path | None:
    return _append_manifest(entry, cwd, MANIFEST_RELPATH)


def manifest_candidates(cwd: Path) -> list[Path]:
    return _manifest_candidates(cwd, MANIFEST_RELPATH)


def manifest_entry(run_id: str, cwd: Path) -> dict | None:
    return _manifest_entry(run_id, cwd, MANIFEST_RELPATH)


def uncommitted_diff_for_run(run_id: str, cwd: Path, name_only: bool = False) -> str | None:
    return _uncommitted_diff_for_run(run_id, cwd, name_only, WIP_REF_PREFIX, MANIFEST_RELPATH)


def export_bundle(
    run_id: str, pinned_sha: str, head_sha: str, cwd: Path, export_dir: Path | None = None
) -> Path | None:
    """See cisternal.provenance.durable.export_bundle. `export_dir=None` here
    (bathos's original default) resolves to `<repo_root>/outputs/provenance`."""
    root = repo_root(cwd)
    target_dir = export_dir if export_dir is not None else (root / EXPORT_DIRNAME if root else None)
    if target_dir is None:
        return None
    return _export_bundle(run_id, pinned_sha, head_sha, cwd, target_dir, RUN_REF_PREFIX, WIP_REF_PREFIX)


def import_bundles(cwd: Path, import_dir: Path | None = None) -> ImportReport:
    root = repo_root(cwd)
    source_dir = import_dir if import_dir is not None else (root / EXPORT_DIRNAME if root else None)
    if source_dir is None:
        return ImportReport()
    return _import_bundles(cwd, source_dir, RUN_REF_PREFIX, WIP_REF_PREFIX, MANIFEST_RELPATH)


def pin_run(
    run_id: str,
    git_hash: str,
    git_branch: str,
    dirty: bool,
    cwd: Path,
    declared_paths: list[str] | tuple[str, ...] = (),
    max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
    export_dir: Path | None = None,
) -> PinResult:
    """Durably record one run's provenance. See cisternal.provenance.durable.pin_run.

    Export triggers when `export_dir` is given, OR (bathos's original
    behavior, preserved here) when running as a SLURM job / with
    `BTH_FORCE_PROVENANCE_EXPORT` set -- in which case it defaults to
    `<repo_root>/outputs/provenance`.
    """
    is_remote = bool(os.environ.get("SLURM_JOB_ID") or os.environ.get("BTH_FORCE_PROVENANCE_EXPORT"))
    effective_export_dir = export_dir
    if effective_export_dir is None and is_remote:
        root = repo_root(cwd)
        if root is not None:
            effective_export_dir = root / EXPORT_DIRNAME

    return _pin_run(
        run_id, git_hash, git_branch, dirty, cwd,
        declared_paths=declared_paths, max_snapshot_bytes=max_snapshot_bytes,
        export_dir=effective_export_dir, provenance_paths=PROVENANCE_PATHS,
        run_ref_prefix=RUN_REF_PREFIX, wip_ref_prefix=WIP_REF_PREFIX,
        manifest_relpath=MANIFEST_RELPATH,
    )
