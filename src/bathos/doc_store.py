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
import tarfile
import tomllib
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
