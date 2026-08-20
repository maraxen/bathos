"""Archive/restore script+output bundles without losing them (bathos artifact archival).

Replaces a bundle's tracked file content with a small self-describing stub (never
deletion, never an in-tree "archived/" directory -- that shape was shown to still be
grepped/read by an agent during the design pass this module implements). Untracked
output paths are packed into a ``git bundle`` instead of a raw tarball, giving the same
content-addressed integrity properties git already provides everywhere else in bathos.

Durability layers, each independently sufficient to recover from:
  1. The stub itself embeds ``pre_archive_sha`` -- ``bth restore`` needs nothing but git
     and the stub file to recover original bytes, even with no local catalog at all.
  2. A ``git notes`` entry on the stubbing commit carries the same metadata, so it
     travels with every ``git clone`` -- unlike the DuckDB catalog, which lives under
     ``~/.bth/catalog/`` and does NOT travel with a clone.
  3. The ``archived_items`` ledger (``bathos.archived_items``) is the queryable
     system-of-record, built on the same cool-fragment/warm-fold substrate as
     ``bathos.trust_ledger``.
  4. ``.bth/ARCHIVE_INDEX.md`` is a generated, git-tracked flat index -- greppable by an
     agent with no bathos tooling at all, regenerated on every archive/restore.

Lint (``bathos.linter.check_archival_candidates``) only ever PROPOSES a candidate; only
this module's ``archive_experiment_bundle`` actually mutates the tree, and only when
called explicitly.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bathos.archived_items import (
    ArchivedItemRecord,
    append_archived_item_record,
    latest_status,
)
from bathos.git import capture_git_state
from bathos.sidecar import find_sidecar
from bathos.telemetry import event

_STUB_MARKER = "ARCHIVED by bathos artifact_archive"
_BUNDLES_SUBDIR = "archive_bundles"


class ArchiveError(Exception):
    pass


class DirtyTreeError(ArchiveError):
    """Raised when a bundle path has uncommitted changes -- refuse to overwrite work."""


class ArtifactNotFoundError(ArchiveError):
    """Raised when a script path doesn't exist, or an item id has no archived_items ledger
    record AND no stub_path was given to recover it from a stub file's own embedded
    metadata (the fresh-clone / no-local-catalog durability path)."""


class BundleNotFoundError(ArchiveError):
    """Raised when a ledger record references an untracked-output git bundle file that is
    missing or unreadable on this machine."""


@dataclass
class ArchivedItem:
    id: str
    project_slug: str
    kind: str
    paths: list[str]
    pre_archive_sha: str
    stub_commit_sha: str
    verdict: str
    reason: str
    superseded_by: str
    bundle_sha256: str = ""
    bundle_path: str = ""
    archived_at: str = field(default_factory=lambda: datetime.now(UTC).isoformat())


@dataclass
class RestoreResult:
    id: str
    restored_paths: list[str]
    restore_commit_sha: str


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _run_git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        text=True,
        capture_output=True,
        check=check,
    )


def _tracked_paths(project_root: Path, paths: list[Path]) -> list[Path]:
    """Which of `paths` are tracked by git (vs. never added)."""
    if not paths:
        return []
    result = _run_git(
        ["ls-files", "--error-unmatch", *[str(p) for p in paths]],
        cwd=project_root,
        check=False,
    )
    tracked_rel = set(result.stdout.splitlines())
    tracked = []
    for p in paths:
        try:
            rel = str(p.resolve().relative_to(project_root.resolve()))
        except ValueError:
            continue
        if rel in tracked_rel:
            tracked.append(p)
    return tracked


def _refuse_if_dirty(project_root: Path, paths: list[Path]) -> None:
    if not paths:
        return
    result = _run_git(
        ["status", "--porcelain", "--", *[str(p) for p in paths]],
        cwd=project_root,
        check=False,
    )
    # `??` (untracked) is expected and fine -- output paths that were never `git add`ed are
    # exactly the ones this module packs into a bundle rather than stubbing in place. Only a
    # TRACKED path with uncommitted modifications is a genuine "would clobber unsaved work"
    # refusal; treating untracked-ness itself as dirty would make the whole bundle path
    # (packing legitimately-untracked outputs) permanently unreachable.
    dirty_lines = [line for line in result.stdout.splitlines() if not line.startswith("??")]
    if dirty_lines:
        raise DirtyTreeError(
            "Refusing to archive: uncommitted changes on bundle path(s):\n"
            + "\n".join(dirty_lines)
        )


def _tracked_output_paths_for_script(
    catalog_dir: Path, project_slug: str, script_sha256: str
) -> list[str]:
    """Output paths recorded on any run whose script_sha256 matches, deduplicated."""
    import duckdb

    db_path = catalog_dir / "bathos.db"
    if not db_path.exists() or not script_sha256:
        return []
    try:
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT output_paths FROM runs WHERE script_sha256 = ? AND project_slug = ?",
                [script_sha256, project_slug],
            ).fetchall()
        finally:
            con.close()
    except Exception:
        return []
    seen: set[str] = set()
    for (paths,) in rows:
        for p in paths or []:
            seen.add(p)
    return sorted(seen)


def _stub_content(
    item_id: str,
    project_slug: str,
    verdict: str,
    reason: str,
    superseded_by: str,
    pre_archive_sha: str,
) -> str:
    """A short, self-describing comment-block stub.

    Deliberately a plain '#'-comment block for EVERY text file kind (.py, .bth.toml,
    .slurm, ...) rather than a kind-specific format (e.g. a parseable [archived] TOML
    table for sidecars) -- a stubbed .bth.toml that still parsed as a valid sidecar table
    would risk being picked up by bathos.sidecar.parse_sidecar as if it were live, which
    is exactly the "wrong live default" danger this whole design exists to avoid.

    Embeds project_slug (not just pre_archive_sha) so restore_archived_item's stub_path
    fallback can record the real project_slug on its "restored" ledger event instead of a
    placeholder -- a wrong project_slug written to the shared archived_items ledger would
    misattribute the item under check_archival_candidates's per-project scoping.
    """
    lines = [
        f"# {_STUB_MARKER} (item_id={item_id})",
        f"# project_slug: {project_slug}",
        f"# verdict: {verdict}",
        f"# reason: {reason}",
        f"# superseded_by: {superseded_by or '(unset)'}",
        f"# pre_archive_sha: {pre_archive_sha}",
        f"# restore: bth restore {item_id}",
    ]
    return "\n".join(lines) + "\n"


def parse_stub(path: Path) -> dict[str, str] | None:
    """Parse a stub file's embedded metadata, or None if `path` is not a stub.

    This is the durability fallback restore_archived_item uses when the warm catalog is
    unavailable (fresh clone, no local ~/.bth/catalog/) -- the stub is self-describing by
    design, not just a pointer to a database row.
    """
    try:
        text = path.read_text()
    except OSError:
        return None
    if _STUB_MARKER not in text:
        return None
    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.lstrip("#").strip()
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        fields[key.strip()] = value.strip()
    marker_line = next((l for l in text.splitlines() if _STUB_MARKER in l), "")
    if "item_id=" in marker_line:
        fields["item_id"] = marker_line.split("item_id=", 1)[1].rstrip(")").strip()
    return fields


def archive_experiment_bundle(
    project_root: Path,
    script_path: Path,
    catalog_dir: Path,
    project_slug: str,
    verdict: str,
    reason: str,
    superseded_by: str = "",
    archived_by: str = "agent",
    dry_run: bool = False,
) -> ArchivedItem:
    """Archive a script + its sidecar + its tracked output paths.

    Refuses if any bundle path has uncommitted changes. Tracked paths are overwritten with
    a stub and committed; untracked paths are packed into a git bundle. See module
    docstring for the four independent durability layers this writes.
    """
    project_root = project_root.resolve()
    script_path = script_path.resolve()
    if not script_path.exists():
        raise ArtifactNotFoundError(f"Script not found: {script_path}")

    item_id = str(uuid.uuid4())
    sidecar_path = find_sidecar(script_path)
    script_sha256 = _sha256_file(script_path)

    bundle_paths: list[Path] = [script_path]
    if sidecar_path is not None:
        bundle_paths.append(sidecar_path)

    output_rel_paths = _tracked_output_paths_for_script(catalog_dir, project_slug, script_sha256)
    output_paths = [
        (project_root / rel).resolve() if not Path(rel).is_absolute() else Path(rel).resolve()
        for rel in output_rel_paths
    ]
    existing_output_paths = [p for p in output_paths if p.exists()]
    bundle_paths.extend(existing_output_paths)

    _refuse_if_dirty(project_root, bundle_paths)

    tracked = _tracked_paths(project_root, bundle_paths)
    untracked = [p for p in bundle_paths if p not in tracked]

    if dry_run:
        return ArchivedItem(
            id=item_id,
            project_slug=project_slug,
            kind="experiment",
            paths=[str(p.relative_to(project_root)) for p in bundle_paths],
            pre_archive_sha=capture_git_state(project_root).hash,
            stub_commit_sha="",
            verdict=verdict,
            reason=reason,
            superseded_by=superseded_by,
        )

    pre_archive_sha = capture_git_state(project_root).hash

    for p in tracked:
        stub = _stub_content(item_id, project_slug, verdict, reason, superseded_by, pre_archive_sha)
        p.write_text(stub)

    bundle_sha256 = ""
    bundle_rel_path = ""
    if untracked:
        bundles_dir = catalog_dir / _BUNDLES_SUBDIR / project_slug
        bundles_dir.mkdir(parents=True, exist_ok=True)
        bundle_target = bundles_dir / f"{item_id}.bundle"
        _build_untracked_bundle(untracked, project_root, bundle_target)
        bundle_sha256 = _sha256_file(bundle_target)
        bundle_rel_path = str(bundle_target)
        for p in untracked:
            p.unlink()

    stub_commit_sha = ""
    if tracked:
        _run_git(["add", *[str(p) for p in tracked]], cwd=project_root)
        commit_msg = (
            f"archive: stub {script_path.name} (item_id={item_id})\n\n"
            f"verdict: {verdict}\nreason: {reason}\nsuperseded_by: {superseded_by or '(unset)'}"
        )
        _run_git(["commit", "-m", commit_msg], cwd=project_root)
        stub_commit_sha = _run_git(["rev-parse", "HEAD"], cwd=project_root).stdout.strip()

        notes_payload = json.dumps(
            {
                "item_id": item_id,
                "verdict": verdict,
                "reason": reason,
                "superseded_by": superseded_by,
                "pre_archive_sha": pre_archive_sha,
                "paths": [str(p.relative_to(project_root)) for p in tracked],
            }
        )
        _run_git(
            ["notes", "--ref=bathos-archive", "add", "-m", notes_payload, stub_commit_sha],
            cwd=project_root,
            check=False,  # non-fatal: notes are a durability bonus, not the source of truth
        )

    all_rel_paths = [str(p.relative_to(project_root)) for p in bundle_paths]

    record = ArchivedItemRecord(
        id=item_id,
        project_slug=project_slug,
        event="archived",
        kind="experiment",
        paths=all_rel_paths,
        pre_archive_sha=pre_archive_sha,
        stub_commit_sha=stub_commit_sha,
        verdict=verdict,
        reason=reason,
        superseded_by=superseded_by,
        bundle_sha256=bundle_sha256,
        bundle_path=bundle_rel_path,
        archived_by=archived_by,
    )
    append_archived_item_record(record, catalog_dir)
    _regenerate_index(project_root, catalog_dir)

    event(
        "archive.artifact_archived",
        id=item_id,
        project_slug=project_slug,
        paths=all_rel_paths,
        verdict=verdict,
    )

    return ArchivedItem(
        id=item_id,
        project_slug=project_slug,
        kind="experiment",
        paths=all_rel_paths,
        pre_archive_sha=pre_archive_sha,
        stub_commit_sha=stub_commit_sha,
        verdict=verdict,
        reason=reason,
        superseded_by=superseded_by,
        bundle_sha256=bundle_sha256,
        bundle_path=bundle_rel_path,
    )


def _build_untracked_bundle(paths: list[Path], project_root: Path, bundle_target: Path) -> None:
    """Pack untracked paths into a git bundle: init a throwaway repo scoped to just these
    files, commit, and bundle it. Gives the same content-addressed integrity as everything
    else in bathos, instead of a raw tarball + separate checksum scheme."""
    import shutil
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        scratch = Path(tmp) / "scratch_repo"
        scratch.mkdir()
        for p in paths:
            rel = p.relative_to(project_root)
            dest = scratch / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(p, dest)

        _run_git(["init", "-q"], cwd=scratch)
        _run_git(["add", "-A"], cwd=scratch)
        _run_git(["commit", "-q", "-m", "archived output bundle"], cwd=scratch)
        _run_git(["bundle", "create", str(bundle_target), "--all"], cwd=scratch)


def _regenerate_index(project_root: Path, catalog_dir: Path) -> None:
    """Regenerate the git-tracked, greppable flat index at .bth/ARCHIVE_INDEX.md.

    Generated artifact, like .praxia/docs/INDEX.md elsewhere -- never hand-edited,
    rebuilt in full from the archived_items ledger on every archive/restore.
    """
    import duckdb

    index_dir = project_root / ".bth"
    index_dir.mkdir(parents=True, exist_ok=True)
    index_path = index_dir / "ARCHIVE_INDEX.md"

    db_path = catalog_dir / "bathos.db"
    rows: list[tuple] = []
    if db_path.exists():
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT id, event, kind, paths, verdict, reason, superseded_by, recorded_at "
                "FROM archived_items ORDER BY recorded_at ASC"
            ).fetchall()
        finally:
            con.close()

    latest_by_id: dict[str, tuple] = {}
    for row in rows:
        latest_by_id[row[0]] = row  # ORDER BY recorded_at ASC -> last write wins per id

    lines = ["# Archived items (generated -- do not hand-edit; regenerated by bth archive-artifact/restore)", ""]
    for item_id, event_name, kind, paths_json, verdict, reason, superseded_by, recorded_at in sorted(
        latest_by_id.values(), key=lambda r: r[7]
    ):
        try:
            paths = json.loads(paths_json) if paths_json else []
        except (TypeError, ValueError):
            paths = []
        status = "RESTORED" if event_name == "restored" else "ARCHIVED"
        lines.append(
            f"- [{status}] {recorded_at[:10]} `{item_id}` ({kind}) {', '.join(paths)} — "
            f"verdict: {verdict}; reason: {reason}; superseded_by: {superseded_by or '(unset)'}; "
            f"restore: `bth restore {item_id}`"
        )
    index_path.write_text("\n".join(lines) + "\n")


def restore_archived_item(
    project_root: Path,
    item_id: str,
    catalog_dir: Path,
    restored_by: str = "agent",
    dry_run: bool = False,
    stub_path: Path | None = None,
) -> RestoreResult:
    """Restore a previously archived bundle: recover tracked paths from git history,
    unbundle untracked ones, commit the restoration, and append a 'restored' ledger record.

    Falls back to parsing `stub_path`'s own embedded pre_archive_sha when the warm catalog
    has no record for item_id (fresh clone, no local ~/.bth/catalog/) -- this is why the SHA
    lives in the stub itself and not only in the ledger. Without the ledger only the single
    stub file pointed at can be recovered (there is no bundle-wide manifest without it) --
    restore each stub in a multi-file bundle individually in that case.
    """
    project_root = project_root.resolve()
    record = latest_status(catalog_dir, item_id)

    if record is not None and record.event == "restored":
        raise ArchiveError(f"Item {item_id} was already restored at {record.recorded_at}")

    if record is not None:
        pre_archive_sha = record.pre_archive_sha
        paths = record.paths
        bundle_path = record.bundle_path
        project_slug = record.project_slug
        kind = record.kind
        verdict = record.verdict
        reason = record.reason
        superseded_by = record.superseded_by
        bundle_sha256 = record.bundle_sha256
    elif stub_path is not None:
        stub_path = stub_path.resolve()
        parsed = parse_stub(stub_path)
        if parsed is None:
            raise ArtifactNotFoundError(f"{stub_path} is not a bathos archive stub")
        if parsed.get("item_id") != item_id:
            raise ArtifactNotFoundError(
                f"Stub at {stub_path} has item_id={parsed.get('item_id')!r}, expected {item_id!r}"
            )
        pre_archive_sha = parsed["pre_archive_sha"]
        paths = [str(stub_path.relative_to(project_root))]
        bundle_path = ""
        # Older stubs (written before project_slug was added to _stub_content) fall back
        # to "unknown" -- new stubs carry the real slug so the "restored" ledger event
        # below doesn't misattribute the item under check_archival_candidates's
        # project_slug-scoped query.
        project_slug = parsed.get("project_slug") or "unknown"
        kind = "experiment"
        verdict = parsed.get("verdict", "")
        reason = parsed.get("reason", "")
        superseded_by = parsed.get("superseded_by", "")
        if superseded_by == "(unset)":
            superseded_by = ""
        bundle_sha256 = ""
    else:
        raise ArtifactNotFoundError(
            f"No archived_items record for {item_id} in {catalog_dir} and no stub_path given "
            "-- pass the path to one of the stub files bth archive-artifact wrote (its own "
            "embedded pre_archive_sha recovers it without the ledger)"
        )

    if dry_run:
        return RestoreResult(id=item_id, restored_paths=paths, restore_commit_sha="")

    # Bundle restore FIRST, tracked-path git-show restore SECOND -- deliberately, so a
    # missing/corrupt bundle raises before any bytes are written to project_root at all.
    # The tracked-path loop below cannot itself fail (a non-zero `git show` just means
    # "this path was untracked", not an error), so ordering the fallible step first means
    # this function has no partial-write failure mode: either nothing on disk changes, or
    # everything below this point does.
    bundle_restored: list[Path] = []
    if bundle_path:
        bundle_file = Path(bundle_path)
        if not bundle_file.exists():
            raise BundleNotFoundError(f"Archive bundle not found on disk: {bundle_path}")
        import tempfile

        # Clone into a scratch dir, not project_root -- `git clone` refuses a non-empty
        # destination (the live working tree always is one), which previously made this
        # silently no-op while the restore still reported success (data never came back).
        with tempfile.TemporaryDirectory() as tmp:
            clone_dir = Path(tmp) / "bundle_clone"
            clone_result = _run_git(
                ["clone", "-q", str(bundle_file), str(clone_dir)], cwd=project_root, check=False
            )
            if clone_result.returncode != 0:
                raise BundleNotFoundError(
                    f"Failed to clone archive bundle {bundle_path}: {clone_result.stderr}"
                )
            for item in sorted(clone_dir.rglob("*")):
                if not item.is_file():
                    continue
                rel = item.relative_to(clone_dir)
                if rel.parts and rel.parts[0] == ".git":
                    continue
                dest = project_root / rel
                dest.parent.mkdir(parents=True, exist_ok=True)
                dest.write_bytes(item.read_bytes())
                bundle_restored.append(dest)

    restored: list[Path] = []
    for rel in paths:
        abs_path = project_root / rel
        # Raw-bytes subprocess call, deliberately NOT `_run_git` (text=True) -- output
        # files are frequently binary (plots, .npz, ...) and text-mode decoding would
        # corrupt them via locale-dependent encoding/decoding.
        show = subprocess.run(
            ["git", "show", f"{pre_archive_sha}:{rel}"],
            cwd=project_root,
            capture_output=True,
            check=False,
        )
        if show.returncode != 0:
            continue  # this path was untracked -> comes from the bundle instead
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        abs_path.write_bytes(show.stdout)
        restored.append(abs_path)

    restore_commit_sha = ""
    if restored:
        _run_git(["add", *[str(p) for p in restored]], cwd=project_root)
        _run_git(
            ["commit", "-m", f"restore: {item_id} (from {pre_archive_sha[:8]})"],
            cwd=project_root,
        )
        restore_commit_sha = _run_git(["rev-parse", "HEAD"], cwd=project_root).stdout.strip()

    append_archived_item_record(
        ArchivedItemRecord(
            id=item_id,
            project_slug=project_slug,
            event="restored",
            kind=kind,
            paths=paths,
            pre_archive_sha=pre_archive_sha,
            stub_commit_sha=restore_commit_sha,
            verdict=verdict,
            reason=reason,
            superseded_by=superseded_by,
            bundle_sha256=bundle_sha256,
            bundle_path=bundle_path,
            archived_by=restored_by,
        ),
        catalog_dir,
    )
    _regenerate_index(project_root, catalog_dir)

    all_restored = restored + bundle_restored
    event("archive.artifact_restored", id=item_id, paths=[str(p) for p in all_restored])

    return RestoreResult(
        id=item_id,
        restored_paths=[str(p) for p in all_restored],
        restore_commit_sha=restore_commit_sha,
    )
