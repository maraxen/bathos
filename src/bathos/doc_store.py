"""Technical documentation store resolution, Praxia frontmatter, and doc archiving.

Spec §5.3:
- 5-layer doc store resolution:
    1. CLI/tool argument (--doc-store)
    2. Environment variable BTH_DOC_STORE
    3. Nearest pyproject.toml [tool.bathos] doc_store
    4. User config ${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml [docs] doc_store
    5. Default fallback <main_root>/.bth/docs
- Anti-Goal A5 guard:
    Refuse writes to .praxia/docs unless explicitly configured via layers 1-4.
    If unset/default/"none", writes to .praxia/docs are refused (AC-P3-11).
    If target path is unwritable, raise DocStoreUnavailableError.
- Praxia frontmatter compatibility:
    status_enum in ["draft", "active", "complete", "superseded", "archived"]
    supersedes: str
- Doc archival:
    Pack doc into <doc_store>/archive/<year>/<slug>.tar.zst with paired <slug>.md manifest.
"""

from __future__ import annotations

import io
import os
import re
import tarfile
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
import zstandard

from bathos.errors import DocStoreUnavailableError

PRAXIA_STATUSES = frozenset({"draft", "active", "complete", "superseded", "archived"})


def resolve_doc_store(
    project_root: Path | str,
    doc_store_arg: str = "",
) -> tuple[Path, bool]:
    """Resolve the technical documentation directory according to the 5-layer hierarchy.

    Returns:
        (resolved_path, is_explicitly_configured)
    """
    root = Path(project_root).resolve()

    # Layer 1: CLI / Tool argument
    if doc_store_arg:
        clean = doc_store_arg.strip()
        if clean.lower() in ("none", ""):
            return root / ".bth" / "docs", False
        p = Path(clean)
        if not p.is_absolute():
            p = (root / p).resolve()
        return p, True

    # Layer 2: Environment variable BTH_DOC_STORE
    env_val = os.environ.get("BTH_DOC_STORE")
    if env_val is not None:
        clean = env_val.strip()
        if clean.lower() in ("none", ""):
            return root / ".bth" / "docs", False
        p = Path(clean)
        if not p.is_absolute():
            p = (root / p).resolve()
        return p, True

    # Layer 3: Nearest pyproject.toml
    curr = root
    while True:
        pyproj = curr / "pyproject.toml"
        if pyproj.is_file():
            try:
                with open(pyproj, "rb") as f:
                    data = tomllib.load(f)
                val = data.get("tool", {}).get("bathos", {}).get("doc_store")
                if val is not None:
                    clean = str(val).strip()
                    if clean.lower() in ("none", ""):
                        return root / ".bth" / "docs", False
                    p = Path(clean)
                    if not p.is_absolute():
                        p = (curr / p).resolve()
                    return p, True
            except Exception:
                pass
        if curr.parent == curr:
            break
        curr = curr.parent

    # Layer 4: User config ${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml
    xdg = os.environ.get("XDG_CONFIG_HOME")
    cfg_path = (Path(xdg) if xdg else Path.home() / ".config") / "bathos" / "config.toml"
    if cfg_path.is_file():
        try:
            with open(cfg_path, "rb") as f:
                data = tomllib.load(f)
            val = data.get("docs", {}).get("doc_store")
            if val is not None:
                clean = str(val).strip()
                if clean.lower() in ("none", ""):
                    return root / ".bth" / "docs", False
                p = Path(os.path.expanduser(clean)).resolve()
                return p, True
        except Exception:
            pass

    # Layer 5: Default fallback
    return root / ".bth" / "docs", False


def check_write_allowed(target_path: Path, project_root: Path, is_explicit: bool) -> None:
    """Enforce Anti-Goal A5: refuse writes to .praxia/docs unless explicitly configured."""
    target_res = target_path.resolve()
    praxia_docs = (project_root.resolve() / ".praxia" / "docs").resolve()

    under_praxia = False
    try:
        target_res.relative_to(praxia_docs)
        under_praxia = True
    except ValueError:
        pass

    if target_res == praxia_docs:
        under_praxia = True

    if under_praxia and not is_explicit:
        raise DocStoreUnavailableError(
            "Refusing to write to .praxia/docs: external doc store not explicitly configured (Anti-Goal A5). "
            "Configure doc_store via --doc-store, BTH_DOC_STORE, pyproject.toml [tool.bathos], or user config."
        )

    # Check if target directory or existing ancestor is writable
    check_p = target_res
    while not check_p.exists() and check_p.parent != check_p:
        check_p = check_p.parent
    if check_p.exists() and not os.access(check_p, os.W_OK):
        raise DocStoreUnavailableError(f"Doc store path is not writable: {target_path}")


def parse_doc_frontmatter(content: str) -> tuple[dict[str, Any], str, str]:
    """Parse YAML frontmatter from document content.

    Supports standard YAML frontmatter bounded by --- lines,
    as well as in-comment blocks like # ---bth--- or ---bth---.
    Returns (metadata_dict, body_content, delimiter_style).
    """
    lines = content.splitlines(keepends=True)
    if not lines:
        return {}, "", "---"

    first_line = lines[0].rstrip()
    if first_line in ("---", "---bth---", "# ---bth---"):
        delimiter = first_line
        closing_delims = (
            ("---",)
            if delimiter == "---"
            else (delimiter, "---")
            if not delimiter.startswith("#")
            else (delimiter, "# ---")
        )
        for idx in range(1, len(lines)):
            if lines[idx].rstrip() in closing_delims:
                raw_lines = lines[1:idx]
                if delimiter.startswith("#"):
                    stripped = []
                    for ln in raw_lines:
                        if ln.startswith("# "):
                            stripped.append(ln[2:])
                        elif ln.startswith("#"):
                            stripped.append(ln[1:])
                        else:
                            stripped.append(ln)
                    fm_text = "".join(stripped)
                else:
                    fm_text = "".join(raw_lines)
                body_text = "".join(lines[idx + 1 :])
                try:
                    meta = yaml.safe_load(fm_text) or {}
                    if not isinstance(meta, dict):
                        meta = {}
                    return meta, body_text, delimiter
                except Exception:
                    pass
                break

    return {}, content, "---"


def format_doc_frontmatter(metadata: dict[str, Any], body: str, delimiter_style: str = "---") -> str:
    """Serialize YAML frontmatter and body."""
    if not metadata:
        return body
    fm_yaml = yaml.dump(metadata, sort_keys=False, default_flow_style=False).strip()
    if delimiter_style.startswith("#"):
        fm_yaml = "\n".join(f"# {line}" if line else "#" for line in fm_yaml.splitlines())
    closing = "---" if delimiter_style == "---" else delimiter_style
    body_clean = body.lstrip("\n")
    return f"{delimiter_style}\n{fm_yaml}\n{closing}\n\n{body_clean}"


def update_doc_status(path: Path, new_status: str, supersedes: str | None = None) -> None:
    """Update frontmatter status and supersedes of a markdown file."""
    if new_status not in PRAXIA_STATUSES:
        raise ValueError(f"Invalid status '{new_status}'. Allowed: {sorted(PRAXIA_STATUSES)}")
    if not path.is_file():
        raise FileNotFoundError(f"Document not found: {path}")
    content = path.read_text(encoding="utf-8")
    meta, body, delim = parse_doc_frontmatter(content)
    meta["status"] = new_status
    if supersedes is not None:
        if supersedes:
            meta["supersedes"] = supersedes
        elif "supersedes" in meta:
            del meta["supersedes"]
    new_content = format_doc_frontmatter(meta, body, delim)
    path.write_text(new_content, encoding="utf-8")


def archive_doc(
    doc_path: Path,
    doc_store: Path,
    project_root: Path,
    year: str | None = None,
    item_id: str | None = None,
    is_explicit: bool = False,
) -> tuple[Path, Path]:
    """Pack doc file into <doc_store>/archive/<year>/<slug>.tar.zst + <slug>.md manifest.

    Unlinks the original doc file upon successful packing.
    """
    doc_path = doc_path.resolve()
    if not doc_path.is_file():
        raise FileNotFoundError(f"Doc file not found: {doc_path}")

    check_write_allowed(doc_store, project_root, is_explicit)

    if year is None:
        year = datetime.now(UTC).strftime("%Y")

    slug = doc_path.stem
    archive_dir = doc_store / "archive" / year
    archive_dir.mkdir(parents=True, exist_ok=True)

    tar_zst_target = archive_dir / f"{slug}.tar.zst"
    manifest_target = archive_dir / f"{slug}.md"

    try:
        source_rel = str(doc_path.relative_to(project_root.resolve()))
    except ValueError:
        source_rel = str(doc_path)

    doc_bytes = doc_path.read_bytes()
    doc_size = len(doc_bytes)

    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w") as tar:
        tar_info = tarfile.TarInfo(name=source_rel)
        tar_info.size = doc_size
        tar_info.mtime = doc_path.stat().st_mtime
        tar.addfile(tar_info, io.BytesIO(doc_bytes))

    raw_tar = tar_buf.getvalue()
    cctx = zstandard.ZstdCompressor(level=19)
    compressed_bytes = cctx.compress(raw_tar)

    tmp_tar_zst = archive_dir / f".{slug}.tar.zst.tmp"
    tmp_tar_zst.write_bytes(compressed_bytes)
    tmp_tar_zst.rename(tar_zst_target)

    created_str = datetime.now(UTC).strftime("%y%m%d")
    manifest_meta = {
        "archive": f"{slug}.tar.zst",
        "created": created_str,
        "source": source_rel,
        "size_bytes": len(compressed_bytes),
        "contents": [
            {
                "path": doc_path.name,
                "size_bytes": doc_size,
            }
        ],
    }
    if item_id:
        manifest_meta["item_id"] = item_id

    manifest_content = format_doc_frontmatter(
        manifest_meta,
        f"# Archived document: {slug}\n\nArchived from `{source_rel}` on {datetime.now(UTC).strftime('%Y-%m-%d')}.\n",
    )
    manifest_target.write_text(manifest_content, encoding="utf-8")

    doc_path.unlink()

    return tar_zst_target, manifest_target


_UUID_RE = re.compile(
    r"\b([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b",
    re.IGNORECASE,
)
_RUN_PREFIX_RE = re.compile(r"\brun[:_]\s*([0-9a-f-]{8,36})\b", re.IGNORECASE)
_RUN_PROSE_RE = re.compile(r"\bRun\s+([0-9a-f]{8,36})\b")
_CAMPAIGN_PREFIX_RE = re.compile(r"\bcampaign[:_]\s*([A-Za-z0-9_.-]{4,64})\b", re.IGNORECASE)
_FINDING_PREFIX_RE = re.compile(r"\bfinding[:]\s*([A-Za-z0-9_.-]{3,64})\b", re.IGNORECASE)
_FINDING_ID_RE = re.compile(r"\b(finding-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4})\b", re.IGNORECASE)

_RUN_FRONTMATTER_KEYS = ("run_id", "run_ids", "runs", "cited_runs", "supporting_runs")
_CAMPAIGN_FRONTMATTER_KEYS = (
    "campaign_id",
    "campaign_ids",
    "campaigns",
    "cited_campaigns",
)
_FINDING_FRONTMATTER_KEYS = ("finding_id", "finding_ids", "findings", "cited_findings")
_ACK_FRONTMATTER_KEYS = ("acknowledged_overrides", "acknowledged_postmortems")


@dataclass(frozen=True)
class DocCitations:
    """Bathos run/campaign/finding citations extracted from a decision document."""

    run_ids: tuple[str, ...] = ()
    campaign_ids: tuple[str, ...] = ()
    finding_ids: tuple[str, ...] = ()
    doc_status: str = ""
    stated_verdict: str = ""
    acknowledged_overrides: tuple[str, ...] = ()


@dataclass(frozen=True)
class FlaggedDecisionDoc:
    """A decision doc whose cited run/campaign verdict was overridden by a postmortem."""

    doc_path: str
    doc_status: str
    cited_kind: str  # "run" | "campaign" | "finding"
    cited_id: str
    target_id: str
    verdict_override: str
    hypothesis_status: str
    postmortem_path: str
    override_kind: str
    stale_reason: str
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "doc_path": self.doc_path,
            "doc_status": self.doc_status,
            "cited_kind": self.cited_kind,
            "cited_id": self.cited_id,
            "target_id": self.target_id,
            "verdict_override": self.verdict_override,
            "hypothesis_status": self.hypothesis_status,
            "postmortem_path": self.postmortem_path,
            "override_kind": self.override_kind,
            "stale_reason": self.stale_reason,
            "reason": self.reason,
        }


def _iter_str_values(val: Any) -> list[str]:
    if isinstance(val, str):
        s = val.strip()
        return [s] if s else []
    if isinstance(val, (list, tuple, set)):
        out: list[str] = []
        for item in val:
            out.extend(_iter_str_values(item))
        return out
    return []


def extract_doc_citations(content: str) -> DocCitations:
    """Extract cited run IDs, campaign IDs, finding IDs, and ack metadata from markdown."""
    meta, body, _ = parse_doc_frontmatter(content)

    run_ids: list[str] = []
    campaign_ids: list[str] = []
    finding_ids: list[str] = []
    ack_ids: list[str] = []

    def _add_unique(bucket: list[str], item: str) -> None:
        cleaned = item.strip()
        if cleaned and cleaned not in bucket:
            bucket.append(cleaned)

    for key in _RUN_FRONTMATTER_KEYS:
        for val in _iter_str_values(meta.get(key)):
            # Strip optional "run:" prefix if used in frontmatter
            if val.lower().startswith("run:"):
                val = val[4:].strip()
            _add_unique(run_ids, val.lower() if _UUID_RE.fullmatch(val) else val)

    for key in _CAMPAIGN_FRONTMATTER_KEYS:
        for val in _iter_str_values(meta.get(key)):
            if val.lower().startswith("campaign:"):
                val = val[9:].strip()
            _add_unique(campaign_ids, val)

    for key in _FINDING_FRONTMATTER_KEYS:
        for val in _iter_str_values(meta.get(key)):
            if val.lower().startswith("finding:"):
                val = val[8:].strip()
            _add_unique(finding_ids, val)

    for key in _ACK_FRONTMATTER_KEYS:
        for val in _iter_str_values(meta.get(key)):
            _add_unique(ack_ids, val)

    # Scan body (and string values in frontmatter excluding ack/supersedes) for inline citations
    scan_chunks = [body]
    for k, v in meta.items():
        if k in (*_ACK_FRONTMATTER_KEYS, "supersedes", "overridden_by_postmortem", "stale_reason"):
            continue
        if isinstance(v, str):
            scan_chunks.append(v)
        elif isinstance(v, list):
            scan_chunks.extend(str(x) for x in v if isinstance(x, str))

    joined = "\n".join(scan_chunks)
    for m in _UUID_RE.finditer(joined):
        _add_unique(run_ids, m.group(1).lower())
    for m in _RUN_PREFIX_RE.finditer(joined):
        raw = m.group(1).lower()
        if not any(r.startswith(raw) for r in run_ids):
            _add_unique(run_ids, raw)
    for m in _RUN_PROSE_RE.finditer(joined):
        raw = m.group(1).lower()
        if not any(r.startswith(raw) for r in run_ids):
            _add_unique(run_ids, raw)
    for m in _CAMPAIGN_PREFIX_RE.finditer(joined):
        _add_unique(campaign_ids, m.group(1))
    for m in _FINDING_PREFIX_RE.finditer(joined):
        _add_unique(finding_ids, m.group(1))
    for m in _FINDING_ID_RE.finditer(joined):
        _add_unique(finding_ids, m.group(1).lower())

    doc_status = str(meta.get("status", "") or "").strip()
    stated_verdict = str(meta.get("verdict") or meta.get("outcome") or "").strip()

    return DocCitations(
        run_ids=tuple(run_ids),
        campaign_ids=tuple(campaign_ids),
        finding_ids=tuple(finding_ids),
        doc_status=doc_status,
        stated_verdict=stated_verdict,
        acknowledged_overrides=tuple(ack_ids),
    )


def _id_matches(cited: str, target: str) -> bool:
    c = cited.strip().lower()
    t = target.strip().lower()
    if not c or not t:
        return False
    if c == t:
        return True
    return len(c) >= 8 and t.startswith(c)


def _is_acknowledged(
    citations: DocCitations,
    cited_id: str,
    target_id: str,
    postmortem_path: str,
) -> bool:
    for ack in citations.acknowledged_overrides:
        if _id_matches(ack, target_id) or _id_matches(ack, cited_id):
            return True
        if postmortem_path and ack.strip() == postmortem_path:
            return True
    return False


def _scan_doc_directories(
    project_root: Path,
    doc_store: str = "",
) -> list[Path]:
    dirs: list[Path] = []
    resolved_ds, _ = resolve_doc_store(project_root, doc_store)
    if resolved_ds.is_dir():
        dirs.append(resolved_ds.resolve())
    default_docs = (project_root / ".bth" / "docs").resolve()
    if default_docs.is_dir() and default_docs not in dirs:
        dirs.append(default_docs)
    if not doc_store:
        praxia_decisions = (project_root / ".praxia" / "docs" / "decisions").resolve()
        if (
            praxia_decisions.is_dir()
            and praxia_decisions not in dirs
            and not any(
                praxia_decisions == d or praxia_decisions.is_relative_to(d) for d in dirs
            )
        ):
            dirs.append(praxia_decisions)
    return dirs


def flag_overridden_doc(
    doc_path: Path,
    flagged_entries: list[FlaggedDecisionDoc],
    *,
    project_root: Path | None = None,
    is_explicit: bool = False,
) -> None:
    """Annotate a markdown decision doc's YAML frontmatter as stale/overridden.

    Respects Anti-Goal A5 via ``check_write_allowed`` before mutating disk.
    """
    if not flagged_entries:
        return
    root = (project_root or doc_path.parent).resolve()
    check_write_allowed(doc_path, root, is_explicit)
    content = doc_path.read_text(encoding="utf-8")
    meta, body, delim = parse_doc_frontmatter(content)
    meta["stale"] = True
    meta["stale_reason"] = "; ".join(dict.fromkeys(e.stale_reason for e in flagged_entries))
    overridden_refs = list(meta.get("overridden_runs") or [])
    for entry in flagged_entries:
        ref = entry.target_id or entry.cited_id
        if ref and ref not in overridden_refs:
            overridden_refs.append(ref)
    if overridden_refs:
        meta["overridden_runs"] = overridden_refs
    pm_paths = [e.postmortem_path for e in flagged_entries if e.postmortem_path]
    if pm_paths:
        meta["overridden_by_postmortem"] = pm_paths[0] if len(pm_paths) == 1 else sorted(set(pm_paths))
    doc_path.write_text(format_doc_frontmatter(meta, body, delim), encoding="utf-8")


def find_overridden_decision_docs(
    project_root: Path | str,
    catalog_dir: Path | str | None = None,
    *,
    doc_store: str = "",
    include_superseded: bool = False,
    flag_docs: bool = False,
) -> list[FlaggedDecisionDoc]:
    """Detect decision docs citing runs, campaigns, or findings overridden by a postmortem.

    Read-only by default (``flag_docs=False``). When ``flag_docs=True``, annotates
    matching docs' frontmatter (enforcing Anti-Goal A5 via ``check_write_allowed``).
    """
    from bathos.postmortem import collect_postmortem_overrides

    root = Path(project_root).resolve()
    overrides = collect_postmortem_overrides(root, catalog_dir=catalog_dir)
    if not overrides:
        return []

    doc_dirs = _scan_doc_directories(root, doc_store=doc_store)
    if not doc_dirs:
        return []

    _, is_explicit = resolve_doc_store(root, doc_store)

    # Index findings -> supporting runs so docs citing a finding also catch postmortem overrides
    finding_runs_by_key: dict[str, list[str]] = {}
    try:
        from bathos.findings import get_findings_dir, parse_finding_file

        fdir = get_findings_dir(root)
        if fdir.is_dir():
            for p in sorted(fdir.glob("*.toml")):
                try:
                    f = parse_finding_file(p)
                    if f.supporting_runs:
                        if f.id:
                            finding_runs_by_key[f.id.lower()] = list(f.supporting_runs)
                        if f.question_slug:
                            finding_runs_by_key[f.question_slug.lower()] = list(f.supporting_runs)
                except Exception:
                    continue
    except Exception:
        pass

    flagged: list[FlaggedDecisionDoc] = []
    seen_md: set[Path] = set()

    for ds in doc_dirs:
        for md_path in sorted(ds.rglob("*.md")):
            resolved_md = md_path.resolve()
            if resolved_md in seen_md or "archive" in md_path.parts:
                continue
            seen_md.add(resolved_md)
            try:
                content = md_path.read_text(encoding="utf-8")
            except OSError:
                continue

            citations = extract_doc_citations(content)
            if not citations.run_ids and not citations.campaign_ids and not citations.finding_ids:
                continue
            if not include_superseded and citations.doc_status in ("superseded", "archived"):
                continue

            try:
                rel_doc = str(resolved_md.relative_to(root))
            except ValueError:
                rel_doc = str(md_path)

            doc_hits: list[FlaggedDecisionDoc] = []
            doc_hit_keys: set[tuple[str, str, str]] = set()

            def _record_hit(
                cited_kind: str,
                cited_id: str,
                ov: Any,
                stale_reason: str,
            ) -> None:
                if _is_acknowledged(citations, cited_id, ov.target_id, ov.postmortem_path):
                    return
                key = (cited_kind, ov.target_id, ov.postmortem_path)
                if key in doc_hit_keys:
                    return
                doc_hit_keys.add(key)
                doc_hits.append(
                    FlaggedDecisionDoc(
                        doc_path=rel_doc,
                        doc_status=citations.doc_status,
                        cited_kind=cited_kind,
                        cited_id=cited_id,
                        target_id=ov.target_id,
                        verdict_override=ov.verdict_override,
                        hypothesis_status=ov.hypothesis_status,
                        postmortem_path=ov.postmortem_path,
                        override_kind=ov.override_kind,
                        stale_reason=stale_reason,
                        reason=ov.reason,
                    )
                )

            for rid in citations.run_ids:
                for ov in overrides:
                    if ov.target_type == "run" and _id_matches(rid, ov.target_id):
                        detail = (
                            f"verdict_override={ov.verdict_override!r}"
                            if ov.verdict_override not in ("", "none")
                            else f"hypothesis_status={ov.hypothesis_status!r}"
                        )
                        _record_hit(
                            "run",
                            rid,
                            ov,
                            f"cited run {rid} overridden by postmortem ({detail})",
                        )

            for cid in citations.campaign_ids:
                for ov in overrides:
                    if ov.target_type == "campaign" and _id_matches(cid, ov.target_id):
                        detail = (
                            f"verdict_override={ov.verdict_override!r}"
                            if ov.verdict_override not in ("", "none")
                            else f"hypothesis_status={ov.hypothesis_status!r}"
                        )
                        _record_hit(
                            "campaign",
                            cid,
                            ov,
                            f"cited campaign {cid} overridden by postmortem ({detail})",
                        )
                    elif ov.target_type == "run" and ov.campaign_id and _id_matches(cid, ov.campaign_id):
                        detail = (
                            f"verdict_override={ov.verdict_override!r}"
                            if ov.verdict_override not in ("", "none")
                            else f"hypothesis_status={ov.hypothesis_status!r}"
                        )
                        _record_hit(
                            "campaign",
                            cid,
                            ov,
                            f"cited campaign {cid} has member run {ov.target_id[:8]} overridden by postmortem ({detail})",
                        )

            for fid in citations.finding_ids:
                s_runs = finding_runs_by_key.get(fid.lower(), [])
                for s_rid in s_runs:
                    for ov in overrides:
                        if ov.target_type == "run" and _id_matches(s_rid, ov.target_id):
                            detail = (
                                f"verdict_override={ov.verdict_override!r}"
                                if ov.verdict_override not in ("", "none")
                                else f"hypothesis_status={ov.hypothesis_status!r}"
                            )
                            _record_hit(
                                "finding",
                                fid,
                                ov,
                                f"cited finding {fid} rests on run {ov.target_id[:8]} overridden by postmortem ({detail})",
                            )

            if doc_hits:
                flagged.extend(doc_hits)
                if flag_docs:
                    flag_overridden_doc(
                        md_path,
                        doc_hits,
                        project_root=root,
                        is_explicit=is_explicit,
                    )

    return flagged
