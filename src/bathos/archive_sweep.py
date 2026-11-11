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
_RUN_PREFIX_OR_ID_RE = re.compile(
    r"\b(?:run_[a-zA-Z0-9_-]+|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|(?=[a-f0-9]*\d)[a-f0-9]{8})\b"
)
_PY_PATH_RE = re.compile(r"[A-Za-z0-9_./-]+\.py\b")
_IDENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]{3,}\b")
_IMPORT_LINE_RE = re.compile(r"^\s*(?:from|import)\s+([A-Za-z0-9_.,\s]+)", re.MULTILINE)

_NON_EXPERIMENT_SCRIPT_SUBDIRS = frozenset({
    "gates",
    "fixtures",
    "data",
    "cluster",
    "docs",
    "retro",
    "audit",
    "slurm",
})
_ALLOWED_PACKAGE_ROOT_REL = frozenset({
    "scripts",
    "scripts/experiments",
    "scripts/benchmarks",
    "scripts/validation",
    "scripts/analysis",
    "scripts/debug",
    "scripts/explore",
})


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
        con = connect_read(catalog_dir, read_only=True, wire_validity=False)
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


def _extract_run_tokens_from_obj(obj: object) -> set[str]:
    out: set[str] = set()
    if isinstance(obj, str):
        out.update(_RUN_PREFIX_OR_ID_RE.findall(obj))
    elif isinstance(obj, (list, tuple, set)):
        for item in obj:
            if isinstance(item, str):
                matches = _RUN_PREFIX_OR_ID_RE.findall(item)
                if matches:
                    out.update(matches)
                elif item.strip():
                    out.add(item.strip())
            else:
                out.update(_extract_run_tokens_from_obj(item))
    elif isinstance(obj, dict):
        for v in obj.values():
            out.update(_extract_run_tokens_from_obj(v))
    return out


def _extract_script_refs_from_text(text: str, include_identifiers: bool = False) -> set[str]:
    refs: set[str] = set()
    if not text:
        return refs
    for m in _PY_PATH_RE.findall(text):
        norm = m.lstrip("./")
        refs.add(norm)
        p = Path(norm)
        refs.add(p.name)
        refs.add(p.stem)
    if include_identifiers:
        for ident in _IDENT_RE.findall(text):
            refs.add(ident)
    return refs


def _collect_findings_cited_runs(project_root: Path) -> set[str]:
    cited: set[str] = set()
    findings_dir = project_root / ".bth" / "findings"
    if findings_dir.is_dir():
        for f in findings_dir.glob("*.toml"):
            try:
                with open(f, "rb") as fp:
                    data = tomllib.load(fp)
                if data.get("status") not in ("superseded", "void", "refuted_or_superseded"):
                    for r in data.get("supporting_runs", []):
                        if isinstance(r, str) and r.strip():
                            cited.add(r.strip())
                        cited.update(_extract_run_tokens_from_obj(r))
                    for field_k in ("question", "answer", "where_stated"):
                        val = data.get(field_k)
                        if isinstance(val, str):
                            cited.update(_extract_run_tokens_from_obj(val))
            except Exception:
                pass

    # Legacy/companion ledgers: scripts/audit/claims_ledger.toml
    claims_ledger = project_root / "scripts" / "audit" / "claims_ledger.toml"
    if claims_ledger.is_file():
        with contextlib.suppress(Exception):
            with open(claims_ledger, "rb") as fp:
                ldata = tomllib.load(fp)
            c_list = ldata.get("claims", [])
            if isinstance(c_list, dict):
                c_list = list(c_list.values())
            for c in c_list:
                if not isinstance(c, dict):
                    continue
                ev_st = str(c.get("evidential_status", c.get("status", "tentative")))
                if ev_st in ("refuted_or_superseded", "superseded", "void"):
                    continue
                for k in ("supporting_runs", "runs", "bathos_run", "where_stated"):
                    cited.update(_extract_run_tokens_from_obj(c.get(k)))

    # Companion Praxia verdicts: .praxia/verdicts/*.toml
    verdicts_dir = project_root / ".praxia" / "verdicts"
    if verdicts_dir.is_dir():
        for vf in verdicts_dir.glob("*.toml"):
            with contextlib.suppress(Exception):
                with open(vf, "rb") as fp:
                    vdata = tomllib.load(fp)
                v_tbl = vdata.get("verdict") if isinstance(vdata.get("verdict"), dict) else {}
                pm_tbl = vdata.get("postmortem") if isinstance(vdata.get("postmortem"), dict) else {}
                raw_st = str(
                    vdata.get("status")
                    or v_tbl.get("status")
                    or v_tbl.get("evidential_status")
                    or pm_tbl.get("status")
                    or "settled"
                )
                if raw_st in ("refuted_or_superseded", "superseded", "void"):
                    continue
                for tbl in (vdata, v_tbl, pm_tbl):
                    for k in ("supporting_runs", "run_ids", "outcome", "status"):
                        cited.update(_extract_run_tokens_from_obj(tbl.get(k)))

    return cited


def _collect_claims_cited_runs(
    project_root: Path,
    catalog_dir: Path | None = None,
    project_slug: str | None = None,
) -> set[str]:
    cited: set[str] = set()
    campaign_ids: set[str] = set()
    claims_dir = project_root / ".bth" / "claims"
    if claims_dir.is_dir():
        for f in claims_dir.glob("*.toml"):
            try:
                with open(f, "rb") as fp:
                    data = tomllib.load(fp)
                if data.get("status") in ("void", "superseded", "abandoned"):
                    continue
                for r in data.get("supporting_runs", []):
                    if isinstance(r, str) and r.strip():
                        cited.add(r.strip())
                    cited.update(_extract_run_tokens_from_obj(r))
                bp = data.get("baseline_parity")
                if isinstance(bp, dict) and bp.get("parity_run_id"):
                    cited.add(str(bp["parity_run_id"]).strip())
                    cited.update(_extract_run_tokens_from_obj(bp["parity_run_id"]))
                for k in ("campaign_id", "campaign", "campaigns"):
                    val = data.get(k)
                    if isinstance(val, str) and val.strip():
                        campaign_ids.add(val.strip())
                    elif isinstance(val, list):
                        for item in val:
                            if isinstance(item, str) and item.strip():
                                campaign_ids.add(item.strip())
                for sec_k in ("assumptions", "confounds", "controls", "reproduction"):
                    cited.update(_extract_run_tokens_from_obj(data.get(sec_k)))
            except Exception:
                pass

        for jf in claims_dir.glob("claim_coverage_*.json"):
            with contextlib.suppress(Exception):
                jdata = json.loads(jf.read_text(encoding="utf-8"))
                cited.update(_extract_run_tokens_from_obj(jdata))
                if isinstance(jdata, dict) and jdata.get("campaign_id"):
                    campaign_ids.add(str(jdata["campaign_id"]).strip())

    # Also check .praxia/verdicts/*.toml for campaign references
    verdicts_dir = project_root / ".praxia" / "verdicts"
    if verdicts_dir.is_dir():
        for vf in verdicts_dir.glob("*.toml"):
            with contextlib.suppress(Exception):
                with open(vf, "rb") as fp:
                    vdata = tomllib.load(fp)
                for tbl in (
                    vdata,
                    vdata.get("verdict") if isinstance(vdata.get("verdict"), dict) else {},
                    vdata.get("postmortem") if isinstance(vdata.get("postmortem"), dict) else {},
                ):
                    for k in ("campaign_id", "campaign", "campaigns"):
                        val = tbl.get(k)
                        if isinstance(val, str) and val.strip():
                            campaign_ids.add(val.strip())
                        elif isinstance(val, list):
                            for item in val:
                                if isinstance(item, str) and item.strip():
                                    campaign_ids.add(item.strip())

    # Active campaigns in DuckDB
    if catalog_dir is not None and catalog_readable(catalog_dir):
        with contextlib.suppress(Exception):
            con = connect_read(catalog_dir, read_only=True, wire_validity=False)
            try:
                tables = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
                active_camp_ids = set(campaign_ids)
                if "campaigns" in tables:
                    if project_slug:
                        rows = con.execute(
                            "SELECT id FROM campaigns WHERE COALESCE(status, 'active') != 'abandoned' "
                            "AND (project_slug = ? OR project_slug IS NULL)",
                            [project_slug],
                        ).fetchall()
                    else:
                        rows = con.execute(
                            "SELECT id FROM campaigns WHERE COALESCE(status, 'active') != 'abandoned'"
                        ).fetchall()
                    active_camp_ids.update(r[0] for r in rows if r[0])

                if "campaign_runs" in tables and active_camp_ids:
                    placeholders = ", ".join("?" for _ in active_camp_ids)
                    cr_rows = con.execute(
                        f"SELECT run_id FROM campaign_runs WHERE campaign_id IN ({placeholders})",
                        list(active_camp_ids),
                    ).fetchall()
                    cited.update(r[0] for r in cr_rows if r[0])

                if "runs" in tables:
                    run_cols = {r[1] for r in con.execute("PRAGMA table_info('runs')").fetchall()}
                    if "campaign_id" in run_cols:
                        if active_camp_ids:
                            placeholders = ", ".join("?" for _ in active_camp_ids)
                            r_rows = con.execute(
                                f"SELECT id FROM runs WHERE campaign_id IN ({placeholders})",
                                list(active_camp_ids),
                            ).fetchall()
                            cited.update(r[0] for r in r_rows if r[0])
                        # Also protect runs with non-empty campaign_id when campaigns table has no abandoned entry for it
                        abandoned_ids: set[str] = set()
                        if "campaigns" in tables:
                            ab_rows = con.execute(
                                "SELECT id FROM campaigns WHERE status = 'abandoned'"
                            ).fetchall()
                            abandoned_ids.update(r[0] for r in ab_rows if r[0])
                        if project_slug:
                            rc_rows = con.execute(
                                "SELECT id, campaign_id FROM runs WHERE project_slug = ? "
                                "AND campaign_id IS NOT NULL AND campaign_id != ''",
                                [project_slug],
                            ).fetchall()
                        else:
                            rc_rows = con.execute(
                                "SELECT id, campaign_id FROM runs "
                                "WHERE campaign_id IS NOT NULL AND campaign_id != ''"
                            ).fetchall()
                        for r_id, c_id in rc_rows:
                            if r_id and c_id not in abandoned_ids:
                                cited.add(r_id)
            finally:
                con.close()

    return cited


def _collect_active_script_citations(project_root: Path) -> set[str]:
    """Collect script paths, filenames, and stems cited directly in active findings, claims, verdicts, or claims_ledger."""
    refs: set[str] = set()

    # 1. .bth/findings/*.toml
    findings_dir = project_root / ".bth" / "findings"
    if findings_dir.is_dir():
        for f in findings_dir.glob("*.toml"):
            with contextlib.suppress(Exception):
                with open(f, "rb") as fp:
                    data = tomllib.load(fp)
                if data.get("status") in ("superseded", "void", "refuted_or_superseded"):
                    continue
                for k in ("question", "answer"):
                    if isinstance(data.get(k), str):
                        refs.update(_extract_script_refs_from_text(data[k], include_identifiers=False))
                if isinstance(data.get("where_stated"), str):
                    refs.update(_extract_script_refs_from_text(data["where_stated"], include_identifiers=True))

    # 2. .bth/claims/*.toml and claim_coverage_*.json
    claims_dir = project_root / ".bth" / "claims"
    if claims_dir.is_dir():
        for f in claims_dir.glob("*.toml"):
            with contextlib.suppress(Exception):
                with open(f, "rb") as fp:
                    data = tomllib.load(fp)
                if data.get("status") in ("void", "superseded", "abandoned"):
                    continue
                raw_text = f.read_text(encoding="utf-8", errors="ignore")
                refs.update(_extract_script_refs_from_text(raw_text, include_identifiers=False))
                for sec_k in ("assumptions", "confounds", "controls", "reproduction"):
                    sec = data.get(sec_k)
                    if isinstance(sec, dict):
                        for v in sec.values():
                            if isinstance(v, str):
                                refs.update(_extract_script_refs_from_text(v, include_identifiers=True))
                            elif isinstance(v, dict):
                                for sub_k in ("verified_by", "checked_by", "reproduction_run", "script"):
                                    if isinstance(v.get(sub_k), str):
                                        refs.update(_extract_script_refs_from_text(v[sub_k], include_identifiers=True))
                    elif isinstance(sec, list):
                        for item in sec:
                            if isinstance(item, dict):
                                for sub_k in ("verified_by", "checked_by", "reproduction_run", "script"):
                                    if isinstance(item.get(sub_k), str):
                                        refs.update(_extract_script_refs_from_text(item[sub_k], include_identifiers=True))

        for jf in claims_dir.glob("claim_coverage_*.json"):
            with contextlib.suppress(Exception):
                raw_json = jf.read_text(encoding="utf-8", errors="ignore")
                refs.update(_extract_script_refs_from_text(raw_json, include_identifiers=False))

    # 3. scripts/audit/claims_ledger.toml
    claims_ledger = project_root / "scripts" / "audit" / "claims_ledger.toml"
    if claims_ledger.is_file():
        with contextlib.suppress(Exception):
            with open(claims_ledger, "rb") as fp:
                ldata = tomllib.load(fp)
            c_list = ldata.get("claims", [])
            if isinstance(c_list, dict):
                c_list = list(c_list.values())
            for c in c_list:
                if not isinstance(c, dict):
                    continue
                ev_st = str(c.get("evidential_status", c.get("status", "tentative")))
                if ev_st in ("refuted_or_superseded", "superseded", "void"):
                    continue
                for k in ("claim", "question", "numbers", "answer"):
                    if isinstance(c.get(k), str):
                        refs.update(_extract_script_refs_from_text(c[k], include_identifiers=False))
                if isinstance(c.get("where_stated"), str):
                    refs.update(_extract_script_refs_from_text(c["where_stated"], include_identifiers=True))

    # 4. .praxia/verdicts/*.toml
    verdicts_dir = project_root / ".praxia" / "verdicts"
    if verdicts_dir.is_dir():
        for vf in verdicts_dir.glob("*.toml"):
            with contextlib.suppress(Exception):
                with open(vf, "rb") as fp:
                    vdata = tomllib.load(fp)
                v_tbl = vdata.get("verdict") if isinstance(vdata.get("verdict"), dict) else {}
                pm_tbl = vdata.get("postmortem") if isinstance(vdata.get("postmortem"), dict) else {}
                raw_st = str(
                    vdata.get("status")
                    or v_tbl.get("status")
                    or v_tbl.get("evidential_status")
                    or pm_tbl.get("status")
                    or "settled"
                )
                if raw_st in ("refuted_or_superseded", "superseded", "void"):
                    continue
                raw_text = vf.read_text(encoding="utf-8", errors="ignore")
                refs.update(_extract_script_refs_from_text(raw_text, include_identifiers=False))

    return refs


def _is_run_cited(run_id: str, all_active_cited: set[str]) -> bool:
    if not run_id:
        return False
    if run_id in all_active_cited:
        return True
    for cited in all_active_cited:
        if len(cited) >= 8 and (run_id.startswith(cited) or cited.startswith(run_id)):
            return True
    return False


def _is_script_directly_cited(rel_py: str, py_path: Path, cited_script_refs: set[str]) -> bool:
    return (
        rel_py in cited_script_refs
        or py_path.name in cited_script_refs
        or py_path.stem in cited_script_refs
    )


def _is_excluded_script_path(project_root: Path, py_path: Path) -> bool:
    """Return True if py_path is a library package module, test file, or non-experiment convention file."""
    name = py_path.name
    if name == "__init__.py" or name.startswith("_") or name.endswith(("_common.py", "_utils.py")):
        return True
    if name.startswith("test_") or name.endswith("_test.py"):
        return True

    try:
        rel = py_path.relative_to(project_root)
    except ValueError:
        return True

    parts = rel.parts
    if any(p in ("tests", "test", "src", ".bth", ".praxia", "decks") for p in parts[:-1]):
        return True

    if len(parts) >= 2 and parts[0] == "scripts" and parts[1] in _NON_EXPERIMENT_SCRIPT_SUBDIRS:
        return True

    # Exclude Python packages under scripts/ (directories containing __init__.py other than top-level convention dirs)
    curr = py_path.parent
    while curr != project_root and curr.is_relative_to(project_root):
        rel_dir = curr.relative_to(project_root).as_posix()
        if (curr / "__init__.py").is_file() and rel_dir not in _ALLOWED_PACKAGE_ROOT_REL:
            return True
        if curr == project_root / "scripts":
            break
        curr = curr.parent

    return False


def _collect_static_script_references(project_root: Path) -> tuple[set[str], dict[Path, set[str]]]:
    """Scan workspace files once for static imports and script invocations.

    Returns:
      - external_refs: set of relative paths, .py filenames, and module/script stems referenced by
        src/, tests/, Justfile, .praxia/c1_invariant_gates.toml, decks/**/.mrx/figures.toml,
        .praxia/docs/**/*.md, and scripts/**/*.{sh,sbatch,slurm}.
      - script_to_refs: mapping from each scripts/**/*.py file to the set of .py filenames and
        imported module tokens it references.
    """
    external_refs: set[str] = set()
    script_to_refs: dict[Path, set[str]] = {}

    def _scan_text_into(target_set: set[str], text: str, all_idents: bool = False) -> None:
        for m in _PY_PATH_RE.findall(text):
            norm = m.lstrip("./")
            target_set.add(norm)
            p = Path(norm)
            target_set.add(p.name)
            target_set.add(p.stem)
        for m in _IMPORT_LINE_RE.findall(text):
            for tok in _IDENT_RE.findall(m):
                target_set.add(tok)
        if all_idents:
            for tok in _IDENT_RE.findall(text):
                target_set.add(tok)

    # 1. src/**/*.py and tests/**/*.py
    for sub in ("src", "tests", "test"):
        sub_dir = project_root / sub
        if sub_dir.is_dir():
            for p in iter_project_files(sub_dir, ".py"):
                with contextlib.suppress(OSError):
                    _scan_text_into(external_refs, p.read_text(encoding="utf-8", errors="ignore"), all_idents=True)

    # 2. Justfile / justfile and .praxia/c1_invariant_gates.toml
    for single_rel in ("Justfile", "justfile", ".praxia/c1_invariant_gates.toml"):
        sp = project_root / single_rel
        if sp.is_file():
            with contextlib.suppress(OSError):
                _scan_text_into(external_refs, sp.read_text(encoding="utf-8", errors="ignore"), all_idents=True)

    # 3. decks/**/.mrx/figures.toml
    decks_dir = project_root / "decks"
    if decks_dir.is_dir():
        for fig_toml in decks_dir.rglob("figures.toml"):
            with contextlib.suppress(OSError):
                _scan_text_into(external_refs, fig_toml.read_text(encoding="utf-8", errors="ignore"), all_idents=True)

    # 4. .praxia/docs/**/*.md (active docs)
    praxia_docs = project_root / ".praxia" / "docs"
    if praxia_docs.is_dir():
        for md_p in praxia_docs.rglob("*.md"):
            if "archive" in md_p.parts:
                continue
            with contextlib.suppress(OSError):
                txt = md_p.read_text(encoding="utf-8", errors="ignore")
                _scan_text_into(external_refs, txt, all_idents=False)

    # 5. scripts/**/*.{sh,sbatch,slurm} and scripts/**/*.py
    scripts_dir = project_root / "scripts"
    if scripts_dir.is_dir():
        for sh_p in iter_project_files(scripts_dir, (".sh", ".sbatch", ".slurm")):
            with contextlib.suppress(OSError):
                _scan_text_into(external_refs, sh_p.read_text(encoding="utf-8", errors="ignore"), all_idents=True)

        for py_p in iter_project_files(scripts_dir, ".py"):
            with contextlib.suppress(OSError):
                txt = py_p.read_text(encoding="utf-8", errors="ignore")
                refs_here: set[str] = set()
                _scan_text_into(refs_here, txt, all_idents=False)
                script_to_refs[py_p] = refs_here

    return external_refs, script_to_refs


def _is_script_statically_referenced(
    rel_py: str,
    py_path: Path,
    external_refs: set[str],
    script_to_refs: dict[Path, set[str]],
) -> bool:
    if (
        rel_py in external_refs
        or py_path.name in external_refs
        or py_path.stem in external_refs
    ):
        return True
    for other_py, refs in script_to_refs.items():
        if other_py == py_path:
            continue
        if rel_py in refs or py_path.name in refs or py_path.stem in refs:
            return True
    return False


def _find_companion_sidecars(project_root: Path, py_path: Path) -> list[Path]:
    sidecars: list[Path] = []
    adj = find_sidecar(py_path)
    if adj and adj.is_file():
        sidecars.append(adj)
    exp_sc = project_root / "scripts" / "experiments" / f"{py_path.stem}.bth.toml"
    if exp_sc.is_file() and exp_sc not in sidecars:
        sidecars.append(exp_sc)
    return sidecars


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
    claims_cited = _collect_claims_cited_runs(project_root, catalog_dir=catalog_dir, project_slug=project_slug)
    all_active_cited = findings_cited | claims_cited
    cited_script_refs = _collect_active_script_citations(project_root)
    external_static_refs, script_to_static_refs = _collect_static_script_references(project_root)

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

    # Pre-load project runs once for accurate boundary matching
    project_runs: list[tuple[str, str | None, str | None, str | None]] = []
    if catalog_readable(catalog_dir):
        with contextlib.suppress(Exception):
            con = connect_read(catalog_dir, read_only=True, wire_validity=False)
            try:
                cols = {r[1] for r in con.execute("PRAGMA table_info('runs')").fetchall()}
                sc_col = "sidecar_path" if "sidecar_path" in cols else "NULL AS sidecar_path"
                rows = con.execute(
                    f"SELECT id, script_sha256, command, {sc_col} FROM runs WHERE project_slug = ?",
                    [project_slug],
                ).fetchall()
                project_runs = [(r[0], r[1], r[2], r[3]) for r in rows]
            finally:
                con.close()

    now_ts = time.time()
    script_entries: list[tuple[str, Path, str, float, float, list[Path], list[str]]] = []
    all_runs_to_resolve: set[str] = set()

    for py_path in py_files:
        rel_py = str(py_path.relative_to(project_root))
        if rel_py in archived_paths or rel_py in seen_paths:
            continue
        if _is_excluded_script_path(project_root, py_path):
            continue
        if _is_script_statically_referenced(rel_py, py_path, external_static_refs, script_to_static_refs):
            continue

        companion_sidecars = _find_companion_sidecars(project_root, py_path)
        sidecar_rels = {str(sc.relative_to(project_root)) for sc in companion_sidecars}
        if rel_py.endswith(".py"):
            sidecar_rels.add(f"{rel_py[:-3]}.bth.toml")
        sidecar_names = {sc.name for sc in companion_sidecars} | {f"{py_path.stem}.bth.toml"}

        # Match runs in catalog
        py_sha = _sha256_file(py_path)
        mod_dot = rel_py[:-3].replace("/", ".") if rel_py.endswith(".py") else rel_py.replace("/", ".")
        rel_py_re = re.compile(rf"(?:^|[\s\"'=/:]){re.escape(rel_py)}(?:$|[\s\"'])")
        mod_dot_re = re.compile(rf"(?:^|[\s\"'=]){re.escape(mod_dot)}(?:$|[\s\"'])")

        runs: list[str] = []
        for r_id, r_sha, r_cmd, r_sc in project_runs:
            matched = False
            if r_sha and r_sha == py_sha:
                matched = True
            elif r_sc:
                r_sc_norm = str(r_sc).lstrip("./")
                if (
                    r_sc_norm in sidecar_rels
                    or Path(r_sc_norm).name in sidecar_names
                    or any(r_sc_norm.endswith("/" + sr) for sr in sidecar_rels)
                ):
                    matched = True
            if not matched and r_cmd and (rel_py_re.search(r_cmd) or mod_dot_re.search(r_cmd)):
                matched = True
            if matched:
                runs.append(r_id)

        mtime = py_path.stat().st_mtime
        age_d = (now_ts - mtime) / 86400.0
        is_cited = _is_script_directly_cited(rel_py, py_path, cited_script_refs) or any(
            _is_run_cited(r, all_active_cited) for r in runs
        )
        if is_cited or age_d < age_days:
            continue

        script_entries.append((rel_py, py_path, py_sha, mtime, age_d, companion_sidecars, runs))
        all_runs_to_resolve.update(runs)

    # Criterion 4: Docs citing exclusively hidden runs (collect first so we can batch resolve_validity)
    target_doc_stores: list[Path] = []
    if doc_store and doc_store.is_dir():
        target_doc_stores.append(doc_store)
    default_docs = project_root / ".bth" / "docs"
    if default_docs.is_dir() and default_docs not in target_doc_stores:
        target_doc_stores.append(default_docs)

    doc_entries: list[tuple[str, Path, set[str]]] = []
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

            doc_entries.append((rel_md, md_path, cited_run_ids))
            all_runs_to_resolve.update(cited_run_ids)

    v_map_all = (
        resolve_validity(sorted(all_runs_to_resolve), catalog_dir, workspace_root=project_root)
        if all_runs_to_resolve
        else {}
    )

    for rel_py, _py_path, py_sha, mtime, age_d, companion_sidecars, runs in script_entries:
        paths = [rel_py]
        file_hashes = {rel_py: py_sha}
        for sidecar_p in companion_sidecars:
            rel_sc = str(sidecar_p.relative_to(project_root))
            if rel_sc not in paths:
                paths.append(rel_sc)
                file_hashes[rel_sc] = _sha256_file(sidecar_p)

        # Criterion 2 check: count > 0, 100% hidden, not cited, and age_d >= age_days
        if runs and all(v_map_all.get(r) and v_map_all[r].is_hidden for r in runs):
            seen_paths.update(paths)
            candidates.append({
                "item_id": str(uuid.uuid4()),
                "kind": "experiment",
                "primary_path": rel_py,
                "paths": paths,
                "criterion": "all_runs_hidden",
                "reason": f"All {len(runs)} historical runs are hidden (void/superseded/archived)",
                "file_hashes": file_hashes,
                "mtime": mtime,
                "run_ids": runs,
            })
            continue

        # Criterion 3 check: age >= age_days and 0 active citations
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

    for rel_md, md_path, cited_run_ids in doc_entries:
        if rel_md in seen_paths:
            continue
        # Candidate if every cited run is hidden
        if cited_run_ids and all(v_map_all.get(r) and v_map_all[r].is_hidden for r in cited_run_ids):
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
                extra_paths = [
                    root_path / p
                    for p in cand.get("paths", [])
                    if p != cand["primary_path"]
                ]
                archived_item = archive_experiment_bundle(
                    project_root=root_path,
                    script_path=script_path,
                    catalog_dir=cat_path,
                    project_slug=project_slug,
                    verdict=verdict,
                    reason=reason,
                    commit=False,
                    item_id=cand_id,
                    extra_paths=extra_paths,
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
                append_archived_item_record(doc_record, cat_path, cwd=root_path)
                archived_ids.append(cand_id)

        # Regenerate index before staging and commit so ARCHIVE_INDEX.md is included in commit
        _regenerate_index(root_path, cat_path, project_slug=project_slug)

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
            _run_git(["add", "-f", "--", *staging_paths], cwd=root_path)

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
