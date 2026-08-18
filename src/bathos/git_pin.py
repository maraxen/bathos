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


# Above this many bytes of newly-staged content, capture metadata instead of blobs. A snapshot is
# permanently reachable via its ref, so an unignored output directory would grow the repository
# without bound -- a worse failure than incomplete provenance. Calibrated against a real, messy
# working repo mid-audit: 306 dirty paths came to 1.33 MB, so this leaves ~40x headroom and fires
# only on a genuine "outputs/ is not ignored" mistake.
DEFAULT_MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024

SNAPSHOT_FULL = "full"
SNAPSHOT_METADATA_ONLY = "metadata_only"
SNAPSHOT_NONE = "none"


@dataclass
class PinResult:
    """What was durably recorded for one run. Empty strings mean "not done"."""

    run_ref: str = ""
    wip_ref: str = ""
    wip_commit: str = ""
    manifest_path: str = ""
    unpinned_reason: str = ""
    ignored_provenance_paths: tuple[str, ...] = ()
    # Set only after the ref has been read back. `run_ref` being non-empty means "we tried and
    # update-ref returned 0"; these mean "the ref resolves to an object that is really there".
    run_ref_ok: bool = False
    wip_ref_ok: bool = False
    snapshot_mode: str = SNAPSHOT_NONE
    skipped_bytes: int = 0
    skipped_paths: tuple[str, ...] = ()
    ignored_declared_paths: tuple[str, ...] = ()

    @property
    def complete(self) -> bool:
        """Whether this run's provenance is fully durable.

        The field downstream gates should read. Deliberately strict: a partially-captured record
        must not be able to pass for a complete one, which is the whole failure this module exists
        to stop.
        """
        return (
            self.run_ref_ok
            and self.snapshot_mode in (SNAPSHOT_FULL, SNAPSHOT_NONE)
            and not self.ignored_declared_paths
            and not self.unpinned_reason
        )


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


@dataclass
class SnapshotResult:
    """Outcome of trying to capture the working tree."""

    commit: str = ""
    mode: str = SNAPSHOT_NONE
    skipped_bytes: int = 0
    skipped_paths: tuple[str, ...] = ()


def snapshot_worktree_detailed(
    run_id: str, cwd: Path, max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES
) -> SnapshotResult:
    """Capture the working tree, degrading explicitly when it is too large to store.

    Over `max_bytes` of newly-staged content the blobs are NOT committed. A snapshot is permanently
    reachable through its ref, so capturing an unignored output directory would grow the repository
    without bound on every dirty run -- a worse outcome than incomplete provenance. Instead the
    result reports `metadata_only` with the byte count and the largest contributors, so the record
    states what it could not keep and the caller can name the paths that should be ignored.
    """
    root = repo_root(cwd)
    if root is None:
        return SnapshotResult()

    head = _git("rev-parse", "HEAD", cwd=root)
    if head.returncode != 0:
        return SnapshotResult()  # unborn branch: nothing to parent a snapshot onto
    parent = head.stdout.strip()

    with tempfile.TemporaryDirectory(prefix="bathos-index-") as tmpdir:
        index_env = {"GIT_INDEX_FILE": str(Path(tmpdir) / "index")}

        if _git("read-tree", parent, cwd=root, env_extra=index_env).returncode != 0:
            return SnapshotResult()
        if _git("add", "-A", cwd=root, env_extra=index_env).returncode != 0:
            return SnapshotResult()

        total, sized = _staged_bytes(cwd, index_env)
        if total > max_bytes:
            return SnapshotResult(
                mode=SNAPSHOT_METADATA_ONLY,
                skipped_bytes=total,
                skipped_paths=tuple(rel for _size, rel in sized[:10]),
            )

        tree = _git("write-tree", cwd=root, env_extra=index_env)
        if tree.returncode != 0:
            return SnapshotResult()
        tree_sha = tree.stdout.strip()

        # An unchanged tree means the worktree matched HEAD after all; no snapshot needed.
        if tree_sha == _git("rev-parse", f"{parent}^{{tree}}", cwd=root).stdout.strip():
            return SnapshotResult()

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
            return SnapshotResult()
        sha = commit.stdout.strip()
        if not sha:
            return SnapshotResult()
        return SnapshotResult(commit=sha, mode=SNAPSHOT_FULL)


def snapshot_worktree(run_id: str, cwd: Path) -> str | None:
    """Backwards-compatible wrapper: the snapshot commit sha, or None.

    Callers that need to know WHY nothing was captured -- clean tree versus too large to store --
    should use `snapshot_worktree_detailed`, since both cases return None here.
    """
    return snapshot_worktree_detailed(run_id, cwd).commit or None



def update_ref(ref: str, sha: str, cwd: Path) -> bool:
    """Point `ref` at `sha`, then READ IT BACK. Returns whether the ref really resolves.

    Verifying rather than trusting `update-ref`'s exit code is the difference between recording that
    something is durable and knowing it is. A ref that failed to be written -- unwritable ref
    directory, full disk, lock contention -- would otherwise be reported as pinned while its object
    is collectable, which is precisely the false attestation this module exists to eliminate.
    """
    root = repo_root(cwd)
    if root is None or not sha:
        return False
    if _git("update-ref", ref, sha, cwd=root).returncode != 0:
        return False
    verify = _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=root)
    return verify.returncode == 0 and verify.stdout.strip() == sha


def ref_resolves(ref: str, cwd: Path) -> bool:
    """Whether `ref` currently resolves to a present commit object."""
    root = repo_root(cwd)
    if root is None:
        return False
    return _git("rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", cwd=root).returncode == 0


def _staged_bytes(cwd: Path, index_env: dict[str, str]) -> tuple[int, list[tuple[int, str]]]:
    """Total size of paths differing from HEAD in the temp index, plus the largest contributors."""
    root = repo_root(cwd)
    if root is None:
        return 0, []
    listing = _git("diff", "--cached", "--name-only", "HEAD", cwd=root, env_extra=index_env)
    if listing.returncode != 0:
        return 0, []
    sized: list[tuple[int, str]] = []
    total = 0
    for rel in listing.stdout.splitlines():
        rel = rel.strip()
        if not rel:
            continue
        try:
            size = (root / rel).stat().st_size
        except OSError:
            continue  # deleted paths contribute nothing to snapshot size
        total += size
        sized.append((size, rel))
    sized.sort(reverse=True)
    return total, sized


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


def ignored_declared_paths(paths: list[str] | tuple[str, ...], cwd: Path) -> tuple[str, ...]:
    """Which of the caller's DECLARED load-bearing paths this repo ignores.

    The inverse hazard to an oversized snapshot: `git add -A` respects `.gitignore`, so a file that
    matters but is ignored is omitted from the snapshot silently. bathos cannot discover undeclared
    inputs -- a config read at runtime that nobody registered is beyond what any of this can see --
    but it can enforce that what WAS declared is capturable, which is the part a tool can own.
    """
    root = repo_root(cwd)
    if root is None or not paths:
        return ()
    ignored = []
    for raw in paths:
        if not raw:
            continue
        result = _git("check-ignore", "-q", str(raw), cwd=root)
        if result.returncode == 0:
            ignored.append(str(raw))
    return tuple(ignored)


def pin_run(
    run_id: str,
    git_hash: str,
    git_branch: str,
    dirty: bool,
    cwd: Path,
    declared_paths: list[str] | tuple[str, ...] = (),
    max_snapshot_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> PinResult:
    """Durably record one run's provenance. Never raises; degrades to a partial result.

    On a dirty tree the run ref points at the SNAPSHOT rather than at HEAD, because the snapshot is
    the tree that actually ran. `git_hash` is still recorded in the manifest so the relationship to
    the committed history stays visible.

    Every degradation is recorded rather than swallowed: a ref that did not take, a snapshot too
    large to store, a declared path the repo ignores. The manifest entry carries all of it, so
    "which runs have complete provenance?" is a query instead of an excavation.
    """
    result = PinResult(
        ignored_provenance_paths=ignored_provenance_paths(cwd),
        ignored_declared_paths=ignored_declared_paths(declared_paths, cwd),
    )

    if repo_root(cwd) is None:
        result.unpinned_reason = "not a git repository"
        return result
    if not git_hash or git_hash == "unknown":
        result.unpinned_reason = "no resolvable HEAD to pin"
        return result

    pinned_sha = git_hash
    if dirty:
        snap = snapshot_worktree_detailed(run_id, cwd, max_bytes=max_snapshot_bytes)
        result.snapshot_mode = snap.mode
        result.skipped_bytes = snap.skipped_bytes
        result.skipped_paths = snap.skipped_paths
        if snap.commit:
            result.wip_commit = snap.commit
            wip_ref = f"{WIP_REF_PREFIX}/{run_id}"
            if update_ref(wip_ref, snap.commit, cwd):
                result.wip_ref = wip_ref
                result.wip_ref_ok = True
            else:
                result.unpinned_reason = "could not create wip ref"
            pinned_sha = snap.commit
        elif snap.mode == SNAPSHOT_METADATA_ONLY:
            result.unpinned_reason = (
                f"working tree too large to snapshot ({snap.skipped_bytes:,} bytes)"
            )

    run_ref = f"{RUN_REF_PREFIX}/{run_id}"
    if update_ref(run_ref, pinned_sha, cwd):
        result.run_ref = run_ref
        result.run_ref_ok = True
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
            # Recorded so a reader can tell a durable record from a partial one WITHOUT re-deriving
            # it. Omitting these is what let the original version claim a run was pinned when its
            # ref had failed to be written and its object was already collectable.
            "run_ref_ok": result.run_ref_ok,
            "wip_ref_ok": result.wip_ref_ok,
            "snapshot_mode": result.snapshot_mode,
            "skipped_bytes": result.skipped_bytes,
            "skipped_paths": list(result.skipped_paths),
            "ignored_declared_paths": list(result.ignored_declared_paths),
            "unpinned_reason": result.unpinned_reason,
            "complete": result.complete,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
        },
        cwd,
    )
    if manifest is not None:
        result.manifest_path = str(manifest)

    return result


def manifest_candidates(cwd: Path) -> list[Path]:
    """Every manifest that could describe a run in THIS repository.

    Refs are shared across linked worktrees but the manifest is an ordinary file in one checkout, so
    a run pinned inside `git worktree add`-ed tree is absent from the main worktree's manifest even
    though its ref and object are fully present there. Since worktree-per-task is a common workflow,
    that would make the manifest miss the majority of runs -- so lookups consult the current
    worktree AND the main one.
    """
    root = repo_root(cwd)
    if root is None:
        return []
    candidates = [root]

    # Enumerate EVERY linked worktree, not just the main one. Looking only "upward" via
    # --git-common-dir finds main from a linked tree but not a linked tree from main, which leaves
    # the more common direction broken: work happens in the worktree, and it is read from main.
    listing = _git("worktree", "list", "--porcelain", cwd=root)
    if listing.returncode == 0:
        for line in listing.stdout.splitlines():
            if not line.startswith("worktree "):
                continue
            other = Path(line[len("worktree ") :].strip())
            if other != root and other not in candidates:
                candidates.append(other)

    return [c / MANIFEST_RELPATH for c in candidates]


def manifest_entry(run_id: str, cwd: Path) -> dict | None:
    """The most recent manifest record for `run_id`, from any manifest in this repository."""
    found = None
    for path in manifest_candidates(cwd):
        if not path.exists():
            continue
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
            continue
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
