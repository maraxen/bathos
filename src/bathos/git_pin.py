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
import subprocess
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

# The authored-document mutation ledger, kept separate from the run manifest above: they
# answer different questions (which tree did this RUN execute against, versus how did this
# DOCUMENT come to have these bytes) and are appended by different code paths at different
# times. Sharing one file would interleave them for no benefit and complicate both readers.
AUTHORING_RELPATH = Path(".bth") / "refs" / "authoring.jsonl"

# Paths whose contents a run's provenance may point at, and which are therefore useless if the
# repository is configured to ignore them. `.bth/claims/` holds claim-tier pre-registrations, whose
# sha256 is the tamper anchor the Union Gate evaluates against at `campaign conclude`.
PROVENANCE_PATHS = (".bth/claims", ".bth/refs")

# Debt #1943: `.bth/refs/manifest.jsonl` is the run manifest -- `pin_run` (below) appends to
# it on literally every `bth run`, before the script under provenance ever executes. Unlike
# `.bth/refs/authoring.jsonl` (deliberately KEPT in PROVENANCE_PATHS/tracked -- its git
# history is the tamper-evidence mechanism `bathos.authoring.ledger` depends on; see that
# module's docstring and `test_the_ledger_file_lands_where_provenance_paths_expects_it`),
# nothing in this module or cisternal.provenance.durable ever commits the manifest's
# appends. So treating it as "tracked" the way the durable.py module docstring frames the
# manifest mechanism ("reviewable, diffable, recoverable from any clone") means, in
# practice, "modified-and-uncommitted starting with the second run, forever" -- which
# poisons the exact `git_dirty` signal this whole mechanism exists to make trustworthy. The
# manifest's own durability need is already covered by the per-run ref
# (`refs/bathos/runs/<id>`), which lives in `.git` and cares nothing about `.gitignore`.
# `.bth/claims` is unaffected: it holds authored, reviewed content (claim-tier
# pre-registrations) with its own PROVENANCE_PATHS warning path, unchanged here.
MANIFEST_GITIGNORE_LINES = ("/.bth/refs/manifest.jsonl", "/.bth/refs/.manifest.lock")

EXPORT_DIRNAME = Path("outputs") / "provenance"

__all__ = [
    "DEFAULT_MAX_SNAPSHOT_BYTES",
    "EXPORT_DIRNAME",
    "AUTHORING_RELPATH",
    "MANIFEST_GITIGNORE_LINES",
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
    "append_authoring_manifest",
    "append_manifest",
    "ensure_manifest_ignored",
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


def ensure_manifest_ignored(cwd: Path) -> bool:
    """Best-effort: make sure the run manifest is ignored before `pin_run` writes to it.

    Covers projects initialized before `bth init` added the manifest to `.gitignore`
    (`bathos.init._ensure_gitignore_entries`). `bth run` must not edit the tracked
    `.gitignore` itself -- that edit would dirty the very tree whose cleanliness it is
    protecting -- so the patterns go into the repository's local, untracked exclude file
    (`git rev-parse --git-path info/exclude`, shared by every linked worktree). Then git is
    asked whether the manifest is actually ignored, since a `!` negation in `.gitignore`
    (which outranks info/exclude) can defeat it.

    Never raises -- provenance bookkeeping must not be able to fail a run (see this
    module's docstring). Returns whether the manifest is (now) ignored; `False` means
    "manifest hygiene is degraded" (the caller should warn, not abort).
    """
    root = repo_root(cwd)
    if root is None:
        return True  # nothing to ignore: pin_run is a no-op outside a repo anyway

    manifest_rel = MANIFEST_RELPATH.as_posix()

    def _is_ignored() -> bool | None:
        try:
            result = subprocess.run(
                ["git", "check-ignore", "-q", "--no-index", manifest_rel],
                cwd=root,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        return result.returncode == 0

    if _is_ignored():
        return True

    try:
        git_path = subprocess.run(
            ["git", "rev-parse", "--git-path", "info/exclude"],
            cwd=root,
            capture_output=True,
            text=True,
        )
    except OSError:
        return False
    if git_path.returncode != 0 or not git_path.stdout.strip():
        return False
    exclude = Path(git_path.stdout.strip())
    if not exclude.is_absolute():
        exclude = root / exclude
    try:
        existing = exclude.read_text(encoding="utf-8") if exclude.exists() else ""
        missing = [line for line in MANIFEST_GITIGNORE_LINES if line not in existing]
        if missing:
            new_text = existing
            if new_text and not new_text.endswith("\n"):
                new_text += "\n"
            new_text += "".join(f"{line}\n" for line in missing)
            exclude.parent.mkdir(parents=True, exist_ok=True)
            exclude.write_text(new_text, encoding="utf-8")
    except OSError:
        pass  # best-effort; the authoritative check below still runs

    return bool(_is_ignored())


def ignored_declared_paths(paths: list[str] | tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    return _ignored_declared_paths(paths, cwd)


def append_manifest(entry: dict, cwd: Path) -> Path | None:
    return _append_manifest(entry, cwd, MANIFEST_RELPATH)


def append_authoring_manifest(entry: dict, cwd: Path) -> Path | None:
    """Append one entry to the authored-document ledger.

    Returns None both when the append failed AND when *cwd* is not in a git repository --
    cisternal conflates the two. bathos.authoring.ledger disambiguates them; callers
    should go through that rather than reading a None here as "no repo".
    """
    return _append_manifest(entry, cwd, AUTHORING_RELPATH)


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
    if export_dir is not None:
        target_dir = export_dir
    else:
        root = repo_root(cwd)
        target_dir = root / EXPORT_DIRNAME if root else None
    if target_dir is None:
        return None
    return _export_bundle(
        run_id, pinned_sha, head_sha, cwd, target_dir, RUN_REF_PREFIX, WIP_REF_PREFIX
    )


def import_bundles(cwd: Path, import_dir: Path | None = None) -> ImportReport:
    if import_dir is not None:
        source_dir = import_dir
    else:
        root = repo_root(cwd)
        source_dir = root / EXPORT_DIRNAME if root else None
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
    is_remote = bool(
        os.environ.get("SLURM_JOB_ID") or os.environ.get("BTH_FORCE_PROVENANCE_EXPORT")
    )
    effective_export_dir = export_dir
    if effective_export_dir is None and is_remote:
        root = repo_root(cwd)
        if root is not None:
            effective_export_dir = root / EXPORT_DIRNAME

    return _pin_run(
        run_id,
        git_hash,
        git_branch,
        dirty,
        cwd,
        declared_paths=declared_paths,
        max_snapshot_bytes=max_snapshot_bytes,
        export_dir=effective_export_dir,
        provenance_paths=PROVENANCE_PATHS,
        run_ref_prefix=RUN_REF_PREFIX,
        wip_ref_prefix=WIP_REF_PREFIX,
        manifest_relpath=MANIFEST_RELPATH,
        identity_name="bathos",
        identity_email="bathos@localhost",
        commit_message_template="bathos worktree snapshot for run {run_id}",
    )
