"""Make a run's git provenance durable, not merely recorded.

`bathos.git` captures WHAT the repo looked like. This module makes that capture survive, which is a
separate problem and the one that actually fails in practice.

Motivating measurement (tev_design catalog, 2026-08-18): `git_hash` was populated on 345/345 runs --
capture is not the gap -- but only 40.6% of those hashes still resolved to a commit, and 92.2% of
runs executed on a DIRTY tree. So the median run recorded a clean-looking hash describing a tree that
never existed, and two runs in five cited a commit that is simply gone. A recorded hash that cannot
be resolved, or that describes a different tree than the one that ran, is a false attestation: the
field reads as a reproducibility guarantee it cannot back.

Three mechanisms, in order of how much they buy:

1. **Worktree snapshot.** When the tree is dirty, commit its actual contents to a real object and
   record THAT, so the provenance describes what ran rather than what happened to be committed.
   Built through a temporary index, so the caller's index, worktree and branches are untouched.

2. **Durable per-run ref** (`refs/bathos/runs/<run_id>`). A ref is a reachability root, so the cited
   commit cannot be garbage-collected and survives deletion of the branch it was made on -- the
   dominant loss mode when work happens in short-lived worktrees.

3. **Tracked manifest** (`.bth/refs/manifest.jsonl`). Refs live in `.git/` and therefore do not
   travel with a normal clone or survive a re-rooted history. The manifest is an ordinary tracked
   file: reviewable, diffable, and recoverable from any clone. The two are complements, not
   alternatives -- the ref protects the objects, the manifest preserves the mapping.

Everything here is best-effort by construction. Provenance capture must never be able to fail a run,
so every function degrades to `None`/empty rather than raising.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

RUN_REF_PREFIX = "refs/bathos/runs"
WIP_REF_PREFIX = "refs/bathos/wip"
MANIFEST_RELPATH = Path(".bth") / "refs" / "manifest.jsonl"

# Paths whose contents a run's provenance may point at, and which are therefore useless if the
# repository is configured to ignore them. `.bth/claims/` holds claim-tier pre-registrations, whose
# sha256 is the tamper anchor the Union Gate evaluates against at `campaign conclude`.
PROVENANCE_PATHS = (".bth/claims", ".bth/refs")

# commit-tree refuses to run without an identity, and a run must not fail because the environment
# has no git user configured (CI containers, cluster nodes).
_IDENTITY_ENV = {
    "GIT_AUTHOR_NAME": "bathos",
    "GIT_AUTHOR_EMAIL": "bathos@localhost",
    "GIT_COMMITTER_NAME": "bathos",
    "GIT_COMMITTER_EMAIL": "bathos@localhost",
}


@dataclass
class PinResult:
    """What was durably recorded for one run. Empty strings mean "not done"."""

    run_ref: str = ""
    wip_ref: str = ""
    wip_commit: str = ""
    manifest_path: str = ""
    unpinned_reason: str = ""
    ignored_provenance_paths: tuple[str, ...] = ()


def _git(
    *args: str, cwd: Path, env_extra: dict[str, str] | None = None, check: bool = False
) -> subprocess.CompletedProcess[str]:
    import os

    env = {**os.environ, **(env_extra or {})}
    return subprocess.run(
        ["git", *args], cwd=cwd, text=True, capture_output=True, env=env, check=check
    )


def repo_root(cwd: Path) -> Path | None:
    """Absolute worktree root, or None if `cwd` is not inside a git repository."""
    try:
        result = _git("rev-parse", "--show-toplevel", cwd=cwd)
    except (OSError, FileNotFoundError):
        return None
    if result.returncode != 0:
        return None
    root = result.stdout.strip()
    return Path(root) if root else None


def ignored_provenance_paths(cwd: Path) -> tuple[str, ...]:
    """Which provenance-bearing paths this repo is configured to IGNORE.

    A non-empty result is a configuration bug worth surfacing loudly: it means a claim or a ref
    manifest written there will never be committed, so the artifact a run's provenance points at is
    silently discarded. Observed in the wild -- a bare `.bth/` ignore rule left 3 of 4 claim-tier
    pre-registrations untracked, including one whose post-hoc amendment later became an audit
    finding.
    """
    root = repo_root(cwd)
    if root is None:
        return ()
    ignored = []
    for rel in PROVENANCE_PATHS:
        # check-ignore exits 0 when the path IS ignored. Probe a child, since an ignore rule on a
        # directory is what actually bites, and `check-ignore` skips already-tracked paths.
        probe = f"{rel}/.bathos-probe"
        result = _git("check-ignore", "-q", "--no-index", probe, cwd=root)
        if result.returncode == 0:
            ignored.append(rel)
    return tuple(ignored)


def snapshot_worktree(run_id: str, cwd: Path) -> str | None:
    """Commit the working tree's ACTUAL contents to a dangling object; return its sha.

    Uses a throwaway index so the caller's index, worktree and branches are never touched. `git add
    -A` respects `.gitignore`, so ignored bulk (virtualenvs, caches, large scratch outputs) is not
    swept in, while untracked-but-tracked-able files -- frequently the script that actually ran --
    are captured.

    Storage is a delta against HEAD: unchanged blobs are shared with objects the repository already
    has, so a snapshot costs roughly the changed files and nothing more. This is why a plain object
    plus a ref is preferable to writing a bundle per run -- same bytes, no dependency on a base
    commit still being present at read time, and git's own GC protection applies.
    """
    root = repo_root(cwd)
    if root is None:
        return None

    head = _git("rev-parse", "HEAD", cwd=root)
    if head.returncode != 0:
        return None  # unborn branch: nothing to parent a snapshot onto
    parent = head.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="bathos-index-") as tmpdir:
        index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}

        if _git("read-tree", parent, cwd=root, env_extra=index_env).returncode != 0:
            return None
        if _git("add", "-A", cwd=root, env_extra=index_env).returncode != 0:
            return None

        tree = _git("write-tree", cwd=root, env_extra=index_env)
        if tree.returncode != 0:
            return None
        tree_sha = tree.stdout.strip()

        # An unchanged tree means the worktree matched HEAD after all; no snapshot needed.
        if tree_sha == _git("rev-parse", f"{parent}^{{tree}}", cwd=root).stdout.strip():
            return None

        commit = _git(
            "commit-tree",
            tree_sha,
            "-p",
            parent,
            "-m",
            f"bathos worktree snapshot for run {run_id}",
            cwd=root,
            env_extra={**index_env, **_IDENTITY_ENV},
        )
        if commit.returncode != 0:
            return None
        return commit.stdout.strip() or None


def update_ref(ref: str, sha: str, cwd: Path) -> bool:
    """Point `ref` at `sha`. Returns whether it took."""
    root = repo_root(cwd)
    if root is None or not sha:
        return False
    return _git("update-ref", ref, sha, cwd=root).returncode == 0


def append_manifest(entry: dict, cwd: Path) -> Path | None:
    """Append one JSONL record to the tracked ref manifest, creating it if needed."""
    root = repo_root(cwd)
    if root is None:
        return None
    path = root / MANIFEST_RELPATH
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, sort_keys=True) + "\n")
    except OSError:
        return None
    return path


def pin_run(run_id: str, git_hash: str, git_branch: str, dirty: bool, cwd: Path) -> PinResult:
    """Durably record one run's provenance. Never raises; degrades to a partial result.

    On a dirty tree the run ref points at the SNAPSHOT rather than at HEAD, because the snapshot is
    the tree that actually ran. `git_hash` is still recorded in the manifest so the relationship to
    the committed history stays visible.
    """
    result = PinResult(ignored_provenance_paths=ignored_provenance_paths(cwd))

    if repo_root(cwd) is None:
        result.unpinned_reason = "not a git repository"
        return result
    if not git_hash or git_hash == "unknown":
        result.unpinned_reason = "no resolvable HEAD to pin"
        return result

    pinned_sha = git_hash
    if dirty:
        wip = snapshot_worktree(run_id, cwd)
        if wip:
            result.wip_commit = wip
            wip_ref = f"{WIP_REF_PREFIX}/{run_id}"
            if update_ref(wip_ref, wip, cwd):
                result.wip_ref = wip_ref
            pinned_sha = wip

    run_ref = f"{RUN_REF_PREFIX}/{run_id}"
    if update_ref(run_ref, pinned_sha, cwd):
        result.run_ref = run_ref
    else:
        result.unpinned_reason = result.unpinned_reason or "could not create run ref"

    manifest = append_manifest(
        {
            "run_id": run_id,
            "head_sha": git_hash,
            "pinned_sha": pinned_sha,
            "branch": git_branch,
            "dirty": bool(dirty),
            "wip_commit": result.wip_commit,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
        cwd,
    )
    if manifest is not None:
        result.manifest_path = str(manifest)

    return result


def manifest_entry(run_id: str, cwd: Path) -> dict | None:
    """The most recent manifest record for `run_id`, or None."""
    root = repo_root(cwd)
    if root is None:
        return None
    path = root / MANIFEST_RELPATH
    if not path.exists():
        return None
    found = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue  # a corrupt line must not hide the rest of the manifest
            if entry.get("run_id") == run_id:
                found = entry
    except OSError:
        return None
    return found


def uncommitted_diff_for_run(run_id: str, cwd: Path, name_only: bool = False) -> str | None:
    """The uncommitted changes that were live when `run_id` executed.

    This is the payoff of parenting the snapshot on HEAD: the delta between the recorded `head_sha`
    and the snapshot IS the dirty state at run time, recoverable long after the working tree moved
    on. Returns "" when the run was clean, None when it cannot be reconstructed.

    Prefers the ref (`refs/bathos/wip/<run_id>`) and falls back to the manifest's recorded sha, so a
    clone that has the objects but not the refs can still answer.
    """
    root = repo_root(cwd)
    if root is None:
        return None

    entry = manifest_entry(run_id, cwd)
    wip_ref = f"{WIP_REF_PREFIX}/{run_id}"
    have_ref = _git("rev-parse", "--verify", "--quiet", wip_ref, cwd=root).returncode == 0

    if have_ref:
        wip_sha = _git("rev-parse", wip_ref, cwd=root).stdout.strip()
    elif entry and entry.get("wip_commit"):
        wip_sha = str(entry["wip_commit"])
    else:
        # No snapshot recorded. Either the run was clean, or it predates pinning -- distinguish,
        # rather than reporting "no changes" for a run whose changes were simply never captured.
        if entry is not None and not entry.get("dirty"):
            return ""
        return None

    head_sha = str(entry["head_sha"]) if entry and entry.get("head_sha") else f"{wip_sha}^"
    args = ["diff", head_sha, wip_sha]
    if name_only:
        args.insert(1, "--name-only")
    result = _git(*args, cwd=root)
    if result.returncode != 0:
        return None
    return result.stdout


def pin_result_as_dict(result: PinResult) -> dict:
    """Flat dict for telemetry, with tuples rendered as lists."""
    payload = asdict(result)
    payload["ignored_provenance_paths"] = list(result.ignored_provenance_paths)
    return payload
