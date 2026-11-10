"""Archive sweep engine for bathos workspace.

Spec §5.2:
- Scans workspace for stale sidecars, scripts with 100% hidden runs, unreferenced scripts (>= age_days),
  and docs citing exclusively hidden runs.
- --dry-run: generates .bth/archive_proposals/sweep_<timestamp>.toml with candidate manifests
  and age-distribution histograms, making no workspace modifications.
- --apply: requires --proposal <path> (raises ArchiveSweepUnapprovedError if missing).
  Verifies proposal freshness against file SHA256 and git status (raises ArchiveProposalStaleError if stale).
  Archives items with commit=False, stages changes, and creates a single atomic git commit
  at the batch tail with git notes and index regeneration.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import time
import tomllib
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.archived_items import (
    ArchivedItemRecord,
    append_archived_item_record,
)
from bathos.artifact_archive import (
    _regenerate_index,
    _run_git,
    archive_experiment_bundle,
)
from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.doc_store import archive_doc, resolve_doc_store
from bathos.errors import (
    ArchiveProposalStaleError,
    ArchiveSweepUnapprovedError,
)
from bathos.git import capture_git_state
from bathos.index import catalog_readable, connect_read
from bathos.sidecar import find_sidecar, parse_sidecar
from bathos.validity import resolve_validity
from bathos.walk import iter_project_files
from bathos.workspace import resolve_workspace

_RUN_ID_RE = re.compile(r"\b(?:run_[a-zA-Z0-9_-]+|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})\b")


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _get_project_slug(project_root: Path) -> str:
    cfg_file = find_project_config(project_root)
    if cfg_file is not None and cfg_file.is_file():
        with contextlib.suppress(Exception):
            cfg = load_project_config(cfg_file)
            if cfg and cfg.slug:
                return cfg.slug
    return project_root.name


def _get_archived_rel_paths(catalog_dir: Path, project_slug: str | None = None) -> set[str]:
    archived_paths: set[str] = set()
    if not catalog_readable(catalog_dir):
        return archived_paths
    with contextlib.suppress(Exception):
        con = connect_read(catalog_dir, read_only=True)
        try:
            if project_slug is not None:
                rows = con.execute(
                    "SELECT id, paths, event, recorded_at FROM archived_items "
                    "WHERE project_slug = ? ORDER BY recorded_at ASC",
                    [project_slug],
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT id, paths, event, recorded_at FROM archived_items ORDER BY recorded_at ASC"
                ).fetchall()
        finally:
            con.close()
        latest_by_id: dict[str, tuple[str, str]] = {}
        for item_id, paths_json, event_name, _recorded_at in rows:
            latest_by_id[item_id] = (event_name, paths_json)
        for event_name, paths_json in latest_by_id.values():
            if event_name != "archived":
                continue
            try:
                paths = json.loads(paths_json) if paths_json else []
            except (TypeError, ValueError):
                paths = []
            archived_paths.update(paths)
    return archived_paths


def _collect_findings_cited_runs(project_root: Path) -> set[str]:
    cited = set()
    findings_dir = project_root / ".bth" / "findings"
    if findings_dir.is_dir():
        for f in findings_dir.glob("*.toml"):
            try:
                with open(f, "rb") as fp:
                    data = tomllib.load(fp)
                if data.get("status") != "superseded":
                    for r in data.get("supporting_runs", []):
                        cited.add(r)
            except Exception:
                pass
    return cited


def _collect_claims_cited_runs(project_root: Path) -> set[str]:
    cited = set()
    claims_dir = project_root / ".bth" / "claims"
    if claims_dir.is_dir():
        for f in claims_dir.glob("*.toml"):
            try:
                with open(f, "rb") as fp:
                    data = tomllib.load(fp)
                for r in data.get("supporting_runs", []):
                    cited.add(r)
            except Exception:
                pass
    return cited


def scan_archive_candidates(
    project_root: Path,
    catalog_dir: Path,
    project_slug: str,
    age_days: int = 90,
    doc_store: Path | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen_paths: set[str] = set()
    archived_paths = _get_archived_rel_paths(catalog_dir, project_slug)

    findings_cited = _collect_findings_cited_runs(project_root)
    claims_cited = _collect_claims_cited_runs(project_root)
    all_active_cited = findings_cited | claims_cited

    # Criterion 1: Stale sidecars ([status] stale = true)
    sidecar_files = iter_project_files(project_root, ".bth.toml")
    for s_path in sidecar_files:
        try:
            sidecar = parse_sidecar(s_path)
        except Exception:
            continue
        if not (sidecar.status and sidecar.status.stale):
            continue
        rel_s = str(s_path.relative_to(project_root))
        if rel_s in archived_paths or rel_s in seen_paths:
            continue

        # Look for companion script
        companion_script = None
        for ext in (".py", ".sh", ".slurm"):
            stem_path = s_path.parent / s_path.name.replace(".bth.toml", ext)
            if stem_path.is_file():
                companion_script = stem_path
                break

        paths = [rel_s]
        file_hashes = {rel_s: _sha256_file(s_path)}
        primary = rel_s
        if companion_script is not None:
            rel_c = str(companion_script.relative_to(project_root))
            paths.insert(0, rel_c)
            file_hashes[rel_c] = _sha256_file(companion_script)
            primary = rel_c

        seen_paths.update(paths)
        candidates.append({
            "item_id": str(uuid.uuid4()),
            "kind": "experiment",
            "primary_path": primary,
            "paths": paths,
            "criterion": "stale_sidecar",
            "reason": "Sidecar declared [status] stale = true",
            "file_hashes": file_hashes,
            "mtime": s_path.stat().st_mtime,
        })

    # Criterion 2: Scripts with 100% hidden runs
    # Criterion 3: Unreferenced scripts (age >= age_days, 0 findings, 0 claims)
    if (project_root / "scripts").is_dir():
        py_files = iter_project_files(project_root / "scripts", ".py")
    else:
        # Repository has no scripts/ directory: iterate project files with boundary pruning
        # and strictly exclude operational source, test suites, internal doc stores, and build trees.
        EXCLUDE_SCRIPT_PARTS = frozenset({
            "src", "tests", "test", ".bth", ".praxia", "build", "dist", ".claude", "docs"
        })
        all_py = iter_project_files(project_root, ".py")
        py_files = [
            p for p in all_py
            if not any(part in EXCLUDE_SCRIPT_PARTS for part in p.relative_to(project_root).parts[:-1])
        ]

    now_ts = time.time()
    for py_path in py_files:
        rel_py = str(py_path.relative_to(project_root))
        if rel_py in archived_paths or rel_py in seen_paths:
            continue

        # Check runs in catalog
        py_sha = _sha256_file(py_path)
        runs: list[str] = []
        if catalog_readable(catalog_dir):
            with contextlib.suppress(Exception):
                con = connect_read(catalog_dir, read_only=True)
                try:
                    rows = con.execute(
                        "SELECT id FROM runs WHERE (script_sha256 = ? OR command LIKE ?) AND project_slug = ?",
                        [py_sha, f"%{rel_py}%", project_slug],
                    ).fetchall()
                    runs = [r[0] for r in rows]
                finally:
                    con.close()

        # Criterion 2 check: count > 0 and 100% hidden
        if runs:
            v_map = resolve_validity(runs, catalog_dir, workspace_root=project_root)
            if all(v_map.get(r) and v_map[r].is_hidden for r in runs):
                paths = [rel_py]
                file_hashes = {rel_py: py_sha}
                sidecar_p = find_sidecar(py_path)
                if sidecar_p and sidecar_p.is_file():
                    rel_sc = str(sidecar_p.relative_to(project_root))
                    paths.append(rel_sc)
                    file_hashes[rel_sc] = _sha256_file(sidecar_p)

                seen_paths.update(paths)
                candidates.append({
                    "item_id": str(uuid.uuid4()),
                    "kind": "experiment",
                    "primary_path": rel_py,
                    "paths": paths,
                    "criterion": "all_runs_hidden",
                    "reason": f"All {len(runs)} historical runs are hidden (void/superseded/archived)",
                    "file_hashes": file_hashes,
                    "mtime": py_path.stat().st_mtime,
                    "run_ids": runs,
                })
                continue

        # Criterion 3 check: age >= age_days and 0 active citations
        mtime = py_path.stat().st_mtime
        age_d = (now_ts - mtime) / 86400.0
        if age_d >= age_days:
            # Check if cited by active findings or claims
            is_cited = False
            for r in runs:
                if r in all_active_cited:
                    is_cited = True
                    break
            if not is_cited:
                paths = [rel_py]
                file_hashes = {rel_py: py_sha}
                sidecar_p = find_sidecar(py_path)
                if sidecar_p and sidecar_p.is_file():
                    rel_sc = str(sidecar_p.relative_to(project_root))
                    paths.append(rel_sc)
                    file_hashes[rel_sc] = _sha256_file(sidecar_p)

                seen_paths.update(paths)
                candidates.append({
                    "item_id": str(uuid.uuid4()),
                    "kind": "experiment",
                    "primary_path": rel_py,
                    "paths": paths,
                    "criterion": "unreferenced_age",
                    "reason": f"Unreferenced for {int(age_d)} days with 0 active findings/claims",
                    "file_hashes": file_hashes,
                    "mtime": mtime,
                    "run_ids": runs,
                })

    # Criterion 4: Docs citing exclusively hidden runs
    target_doc_stores: list[Path] = []
    if doc_store and doc_store.is_dir():
        target_doc_stores.append(doc_store)
    default_docs = project_root / ".bth" / "docs"
    if default_docs.is_dir() and default_docs not in target_doc_stores:
        target_doc_stores.append(default_docs)

    for ds in target_doc_stores:
        for md_path in ds.rglob("*.md"):
            if "archive" in md_path.parts:
                continue
            try:
                rel_md = str(md_path.relative_to(project_root))
            except ValueError:
                rel_md = str(md_path)
            if rel_md in archived_paths or rel_md in seen_paths:
                continue

            try:
                text = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            cited_run_ids = set(_RUN_ID_RE.findall(text))
            if not cited_run_ids:
                continue

            v_map = resolve_validity(list(cited_run_ids), catalog_dir, workspace_root=project_root)
            # Candidate if every cited run is hidden
            if cited_run_ids and all(v_map.get(r) and v_map[r].is_hidden for r in cited_run_ids):
                md_sha = _sha256_file(md_path)
                seen_paths.add(rel_md)
                candidates.append({
                    "item_id": str(uuid.uuid4()),
                    "kind": "doc",
                    "primary_path": rel_md,
                    "paths": [rel_md],
                    "criterion": "doc_citing_hidden_runs",
                    "reason": f"Doc cites exclusively hidden runs: {sorted(cited_run_ids)}",
                    "file_hashes": {rel_md: md_sha},
                    "mtime": md_path.stat().st_mtime,
                    "run_ids": list(cited_run_ids),
                })

    return candidates


def _compute_histogram(candidates: list[dict[str, Any]], now_ts: float) -> dict[str, int]:
    bins = {
        "0-90": 0,
        "90-180": 0,
        "180-365": 0,
        "365+": 0,
    }
    for c in candidates:
        mtime = c.get("mtime", now_ts)
        age_d = max(0.0, (now_ts - mtime) / 86400.0)
        if age_d < 90:
            bins["0-90"] += 1
        elif age_d < 180:
            bins["90-180"] += 1
        elif age_d < 365:
            bins["180-365"] += 1
        else:
            bins["365+"] += 1
    return bins


def archive_sweep(
    dry_run: bool = True,  # noqa: ARG001
    age_days: int = 90,
    proposal_path: str = "",
    apply: bool = False,
    doc_store: str = "",
    project_root: str = "",
    catalog_dir: str = "",
) -> dict[str, Any]:
    """Archive sweep entrypoint: scans workspace or applies an approved sweep proposal."""
    # Resolve roots
    root_path = Path(project_root).resolve() if project_root else resolve_workspace().fs_root
    cat_path = Path(catalog_dir or default_catalog_dir()).resolve()
    project_slug = _get_project_slug(root_path)

    resolved_doc_store, is_doc_explicit = resolve_doc_store(root_path, doc_store)

    if apply:
        if not proposal_path:
            raise ArchiveSweepUnapprovedError(
                "Archive sweep requires an approved proposal file when --apply is set. "
                "Run with --dry-run first to generate a proposal."
            )
        prop_file = Path(proposal_path)
        if not prop_file.is_absolute():
            prop_file = (root_path / prop_file).resolve()
        if not prop_file.is_file():
            raise ArchiveSweepUnapprovedError(f"Archive proposal file not found: {proposal_path}")

        try:
            with open(prop_file, "rb") as f:
                proposal_data = tomllib.load(f)
        except Exception as e:
            raise ArchiveProposalStaleError(f"Cannot parse proposal file {prop_file}: {e}") from e

        # Freshness verification
        candidates = proposal_data.get("candidates", [])
        if not candidates:
            return {
                "ok": True,
                "apply": True,
                "commit_sha": "",
                "archived_count": 0,
                "archived_ids": [],
                "proposal_path": str(prop_file),
                "message": "Proposal contains 0 candidates; no changes made.",
            }

        # Check git status for modifications in candidate paths
        git_res = _run_git(["status", "--porcelain"], cwd=root_path, check=False)
        dirty_lines = [ln.strip() for ln in git_res.stdout.splitlines() if ln.strip()]

        for cand in candidates:
            for p_str, expected_hash in cand.get("file_hashes", {}).items():
                p_abs = root_path / p_str
                if not p_abs.exists():
                    raise ArchiveProposalStaleError(
                        f"Candidate file no longer exists on disk: {p_str}"
                    )
                current_hash = _sha256_file(p_abs)
                if current_hash != expected_hash:
                    raise ArchiveProposalStaleError(
                        f"Candidate file {p_str} was modified after proposal was generated"
                    )
                # Check porcelain status for this file
                for d_ln in dirty_lines:
                    if p_str in d_ln:
                        raise ArchiveProposalStaleError(
                            f"Working tree has uncommitted changes in candidate path: {p_str}"
                        )

        # Batch execution: archive items with commit=False
        archived_ids: list[str] = []
        doc_archive_targets: list[Path] = []
        for cand in candidates:
            cand_id = cand.get("item_id") or str(uuid.uuid4())
            kind = cand.get("kind", "experiment")
            reason = cand.get("reason", "archive sweep")
            verdict = cand.get("verdict", "archived")

            if kind == "experiment":
                script_path = root_path / cand["primary_path"]
                archived_item = archive_experiment_bundle(
                    project_root=root_path,
                    script_path=script_path,
                    catalog_dir=cat_path,
                    project_slug=project_slug,
                    verdict=verdict,
                    reason=reason,
                    commit=False,
                    item_id=cand_id,
                )
                archived_ids.append(archived_item.id)
            elif kind == "doc":
                doc_path = root_path / cand["primary_path"]
                tar_zst, manifest_md = archive_doc(
                    doc_path=doc_path,
                    doc_store=resolved_doc_store,
                    project_root=root_path,
                    item_id=cand_id,
                    is_explicit=is_doc_explicit,
                )
                doc_archive_targets.extend([tar_zst, manifest_md])
                doc_record = ArchivedItemRecord(
                    id=cand_id,
                    project_slug=project_slug,
                    event="archived",
                    kind="doc",
                    paths=cand.get("paths", [cand["primary_path"]]),
                    pre_archive_sha=capture_git_state(root_path).hash,
                    stub_commit_sha="",
                    verdict=verdict,
                    reason=reason,
                    bundle_path=str(tar_zst),
                    archived_by="agent",
                    run_ids=cand.get("run_ids", []),
                )
                append_archived_item_record(doc_record, cat_path)
                archived_ids.append(cand_id)

        # Regenerate index before staging and commit so ARCHIVE_INDEX.md is included in commit
        _regenerate_index(root_path, cat_path)

        # Scoped explicit git staging
        paths_to_stage: set[str] = set()
        for cand in candidates:
            for p in cand.get("paths", [cand["primary_path"]]):
                paths_to_stage.add(p)
            for p in cand.get("file_hashes", {}):
                paths_to_stage.add(p)
        for target in doc_archive_targets:
            try:
                rel_target = str(target.resolve().relative_to(root_path.resolve()))
                paths_to_stage.add(rel_target)
            except ValueError:
                pass
        if proposal_path:
            p_path = Path(proposal_path)
            if not p_path.is_absolute():
                p_path = root_path / p_path
            try:
                rel_prop = str(p_path.resolve().relative_to(root_path.resolve()))
                paths_to_stage.add(rel_prop)
            except ValueError:
                pass
        paths_to_stage.add(".bth/ARCHIVE_INDEX.md")

        tracked_output = _run_git(["ls-files"], cwd=root_path, check=False).stdout
        tracked_set = set(ln.strip() for ln in tracked_output.splitlines() if ln.strip())

        staging_paths = [
            p for p in sorted(paths_to_stage)
            if (root_path / p).exists() or p in tracked_set
        ]

        if staging_paths:
            _run_git(["add", "--", *staging_paths], cwd=root_path)

        commit_msg = f"archive: sweep {len(candidates)} obsolete items (proposal: {proposal_path})"
        diff_check = _run_git(["diff", "--cached", "--quiet"], cwd=root_path, check=False)
        if diff_check.returncode != 0:
            _run_git(["commit", "-m", commit_msg], cwd=root_path)
        batch_commit_sha = _run_git(["rev-parse", "HEAD"], cwd=root_path).stdout.strip()

        # Attach git notes under bathos-archive (use append to prevent collision across batch items)
        for cand in candidates:
            notes_payload = json.dumps({
                "item_id": cand.get("item_id"),
                "kind": cand.get("kind"),
                "paths": cand.get("paths"),
                "reason": cand.get("reason"),
                "batch_commit_sha": batch_commit_sha,
                "proposal": proposal_path,
            })
            _run_git(
                ["notes", "--ref=bathos-archive", "append", "-m", notes_payload, batch_commit_sha],
                cwd=root_path,
                check=False,
            )

        return {
            "ok": True,
            "apply": True,
            "commit_sha": batch_commit_sha,
            "archived_count": len(candidates),
            "archived_ids": archived_ids,
            "proposal_path": str(prop_file),
        }

    # Dry run
    now_ts = time.time()
    candidates = scan_archive_candidates(
        project_root=root_path,
        catalog_dir=cat_path,
        project_slug=project_slug,
        age_days=age_days,
        doc_store=resolved_doc_store,
    )
    histogram = _compute_histogram(candidates, now_ts)

    proposals_dir = root_path / ".bth" / "archive_proposals"
    proposals_dir.mkdir(parents=True, exist_ok=True)

    ts_slug = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    proposal_filename = f"sweep_{ts_slug}.toml"
    prop_file = proposals_dir / proposal_filename

    git_hash = capture_git_state(root_path).hash

    # Serialize proposal TOML
    lines = [
        "[meta]",
        f'proposal_id = "sweep_{ts_slug}"',
        f'generated_at = "{datetime.now(UTC).isoformat()}"',
        f"age_days = {age_days}",
        f'workspace_git_sha = "{git_hash}"',
        f"total_candidates = {len(candidates)}",
        "",
        "[histograms.age_days]",
    ]
    for bin_k, count in histogram.items():
        lines.append(f'"{bin_k}" = {count}')
    lines.append("")

    for cand in candidates:
        lines.append("[[candidates]]")
        lines.append(f'item_id = "{cand["item_id"]}"')
        lines.append(f'kind = "{cand["kind"]}"')
        lines.append(f'primary_path = "{cand["primary_path"]}"')
        paths_json = json.dumps(cand["paths"])
        lines.append(f"paths = {paths_json}")
        lines.append(f'criterion = "{cand["criterion"]}"')
        lines.append(f'reason = "{cand["reason"]}"')
        hashes_toml = ", ".join(f'"{k}" = "{v}"' for k, v in cand["file_hashes"].items())
        lines.append(f"file_hashes = {{ {hashes_toml} }}")
        if cand.get("run_ids"):
            r_json = json.dumps(cand["run_ids"])
            lines.append(f"run_ids = {r_json}")
        lines.append("")

    prop_file.write_text("\n".join(lines), encoding="utf-8")

    return {
        "ok": True,
        "dry_run": True,
        "proposal_path": str(prop_file),
        "candidate_count": len(candidates),
        "histogram": histogram,
        "candidates": [
            {
                "item_id": c["item_id"],
                "kind": c["kind"],
                "primary_path": c["primary_path"],
                "criterion": c["criterion"],
                "reason": c["reason"],
            }
            for c in candidates
        ],
    }
