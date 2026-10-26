"""Backfill a run's claim-tier declaration from its own pre-registered sidecar.

`bth run` only began copying `claim_discriminates` / `claim_isolates` from the
sidecar into the run record in #3717 (2065fd50, 260729). Every run made before
that whose sidecar declared them carries NULL, and the Union Gate -- which maps
a run onto a claim clause through `runs.claim_discriminates` -- reports those
clauses as unmapped and downgrades the campaign to `confounded`.

The correction is a `run.declaration_backfilled` event, written only when the
declaration can be proven to be the one the run was launched under: the bytes
it is parsed from must hash to the run's own recorded `sidecar_sha256`. The
sidecar is looked for, in order, at the recorded `sidecar_path`, at the same
project-relative path under each registered local root (cluster runs record a
remote absolute path, debt #2225), and in the git history of that path. A
sidecar edited after the run hashes differently and is never used, so a
backfill cannot introduce a declaration that was not pre-registered.

The fold applies the event only to a field that is still NULL; it never
overrides a value the run recorded itself.

Log mode only: the legacy warm tier is rebuilt from cool fragments, so an
UPDATE there is silently lost on the next rebuild (which is how the 260829
asr backfill disappeared).
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bathos.index import connect_read
from bathos.runlog.emit import current_mode, emit_event, unit_of_work
from bathos.runlog.project_id import list_registered_roots

EVENT_KIND = "run.declaration_backfilled"


class BackfillError(RuntimeError):
    """The backfill cannot run in this catalog (e.g. not in log mode)."""


@dataclass
class BackfillCandidate:
    run_id: str
    project_slug: str
    sidecar_sha256: str
    sidecar_path: str
    status: str
    """`backfillable` | `no_declaration` | `unresolved` | `parse_error`."""
    claim_discriminates: list[str] = field(default_factory=list)
    claim_isolates: list[str] = field(default_factory=list)
    resolved_from: str = ""
    """`path` | `registered_root` | `git` (empty unless resolved)."""
    resolved_locator: str = ""
    """The file path, or `<repo>@<commit>:<relpath>` for a git blob."""
    detail: str = ""


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _git(args: list[str], cwd: Path) -> bytes | None:
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _git_toplevel(path: Path) -> Path | None:
    probe = path if path.is_dir() else path.parent
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    out = _git(["rev-parse", "--show-toplevel"], probe)
    return Path(out.decode().strip()) if out else None


class _SidecarResolver:
    """Find sidecar bytes whose sha256 equals the run's recorded one.

    Caches file hashes and per-path git history, since thousands of runs share
    a few hundred sidecars.
    """

    def __init__(self, roots: list[Path]):
        self.roots = roots
        self._file_sha: dict[Path, str | None] = {}
        self._git_blobs: dict[tuple[Path, str], dict[str, tuple[str, bytes]]] = {}

    def _file_hash(self, path: Path) -> str | None:
        if path not in self._file_sha:
            try:
                self._file_sha[path] = _sha256_bytes(path.read_bytes()) if path.is_file() else None
            except OSError:
                self._file_sha[path] = None
        return self._file_sha[path]

    def _local_candidates(self, recorded: Path) -> list[tuple[str, Path]]:
        out: list[tuple[str, Path]] = [("path", recorded)]
        parts = recorded.parts
        # Suffixes of the recorded path under each registered root: a cluster
        # run records e.g. /orcd/.../asr/scripts/x.bth.toml, which lives locally
        # at <root>/scripts/x.bth.toml.
        for root in self.roots:
            for i in range(1, len(parts) - 1):
                cand = root.joinpath(*parts[i:])
                if cand != recorded:
                    out.append(("registered_root", cand))
        return out

    def _history(self, repo: Path, rel: str) -> dict[str, tuple[str, bytes]]:
        key = (repo, rel)
        if key not in self._git_blobs:
            blobs: dict[str, tuple[str, bytes]] = {}
            log = _git(["log", "--all", "--format=%H", "--", rel], repo)
            for commit in (log or b"").decode().split():
                content = _git(["show", f"{commit}:{rel}"], repo)
                if content is not None:
                    blobs.setdefault(_sha256_bytes(content), (commit, content))
            self._git_blobs[key] = blobs
        return self._git_blobs[key]

    def resolve(self, sidecar_path: str, sha: str) -> tuple[str, str, bytes] | None:
        if not sidecar_path or not sha:
            return None
        local = self._local_candidates(Path(sidecar_path))
        for source, cand in local:
            if self._file_hash(cand) == sha:
                return source, str(cand), cand.read_bytes()
        # Git history of every candidate path that sits in a local repo.
        seen: set[tuple[Path, str]] = set()
        for _source, cand in local:
            if not cand.parent.exists():
                continue
            repo = _git_toplevel(cand)
            if repo is None:
                continue
            try:
                rel = cand.resolve().relative_to(repo.resolve()).as_posix()
            except ValueError:
                continue
            if (repo, rel) in seen:
                continue
            seen.add((repo, rel))
            hit = self._history(repo, rel).get(sha)
            if hit is not None:
                commit, content = hit
                return "git", f"{repo}@{commit}:{rel}", content
        return None


def _parse_declaration(content: bytes) -> tuple[list[str], list[str]]:
    from bathos.sidecar import parse_sidecar

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "sidecar.bth.toml"
        path.write_bytes(content)
        sidecar = parse_sidecar(path)
    return list(sidecar.claim_discriminates), list(sidecar.claim_isolates)


def find_candidates(
    catalog_dir: Path,
    *,
    run_ids: list[str] | None = None,
    project: str | None = None,
    roots: list[Path] | None = None,
) -> list[BackfillCandidate]:
    """Runs with a NULL claim field and a recorded sidecar hash, classified.

    `run_ids` entries may be unique prefixes.
    """
    sql = (
        "SELECT id, project_slug, sidecar_sha256, sidecar_path, "
        "claim_discriminates, claim_isolates FROM runs "
        "WHERE (claim_discriminates IS NULL OR claim_isolates IS NULL) "
        "AND coalesce(sidecar_sha256, '') <> ''"
    )
    params: list[str] = []
    if project:
        sql += " AND project_slug = ?"
        params.append(project)
    if run_ids:
        sql += " AND (" + " OR ".join("id LIKE ?" for _ in run_ids) + ")"
        params.extend(f"{r}%" for r in run_ids)
    sql += " ORDER BY id"

    con = connect_read(catalog_dir)
    try:
        rows = con.execute(sql, params).fetchall()
    finally:
        con.close()

    resolver = _SidecarResolver(roots if roots is not None else list_registered_roots())
    out: list[BackfillCandidate] = []
    for run_id, slug, sha, path, disc_cur, iso_cur in rows:
        cand = BackfillCandidate(
            run_id=run_id,
            project_slug=slug or "",
            sidecar_sha256=sha,
            sidecar_path=path or "",
            status="unresolved",
        )
        hit = resolver.resolve(cand.sidecar_path, sha)
        if hit is None:
            cand.detail = "no file or git blob hashes to the recorded sidecar_sha256"
            out.append(cand)
            continue
        cand.resolved_from, cand.resolved_locator, content = hit
        try:
            disc, iso = _parse_declaration(content)
        except Exception as e:  # noqa: BLE001 -- recorded per run, never aborts the scan
            cand.status = "parse_error"
            cand.detail = str(e)
            out.append(cand)
            continue
        # Only fields still NULL on the run are filled.
        cand.claim_discriminates = disc if disc_cur is None else []
        cand.claim_isolates = iso if iso_cur is None else []
        cand.status = (
            "backfillable"
            if (cand.claim_discriminates or cand.claim_isolates)
            else "no_declaration"
        )
        out.append(cand)
    return out


def _event_cwd(cand: BackfillCandidate) -> Path | None:
    """Emit into the run's own project log, not wherever `bth` was invoked."""
    if cand.resolved_from == "git":
        return Path(cand.resolved_locator.split("@", 1)[0])
    if cand.resolved_locator:
        return Path(cand.resolved_locator).parent
    return None


def backfill_declarations(
    catalog_dir: Path,
    *,
    run_ids: list[str] | None = None,
    project: str | None = None,
    apply: bool = False,
    roots: list[Path] | None = None,
) -> list[BackfillCandidate]:
    """Classify candidates and, with `apply`, emit one event per backfillable run.

    Raises BackfillError outside log mode.
    """
    with unit_of_work(catalog_dir):
        if not current_mode():
            raise BackfillError(
                "backfill-declarations needs log mode: a legacy warm-tier UPDATE is lost on "
                "the next rebuild from cool fragments. Run `bth migrate --to-log` first."
            )
        candidates = find_candidates(catalog_dir, run_ids=run_ids, project=project, roots=roots)
        if apply:
            now = datetime.now(UTC).isoformat()
            for cand in candidates:
                if cand.status != "backfillable":
                    continue
                data: dict[str, object] = {
                    "sidecar_sha256": cand.sidecar_sha256,
                    "resolved_from": cand.resolved_from,
                    "resolved_locator": cand.resolved_locator,
                    "backfilled_at": now,
                }
                # Same JSON-array-string encoding `bth run` uses (runner.py).
                if cand.claim_discriminates:
                    data["claim_discriminates"] = json.dumps(cand.claim_discriminates)
                if cand.claim_isolates:
                    data["claim_isolates"] = json.dumps(cand.claim_isolates)
                emit_event(kind=EVENT_KIND, entity=[cand.run_id], data=data, cwd=_event_cwd(cand))
    return candidates


__all__ = [
    "EVENT_KIND",
    "BackfillCandidate",
    "BackfillError",
    "backfill_declarations",
    "find_candidates",
]
