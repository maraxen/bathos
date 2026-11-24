"""Parity declaration command (`bth parity declare`, WP03a).

In Ring 0, ``declare_parity_record`` validates the declaration (G01/G02),
captures git ``HEAD`` from the declaration repository, writes the exact raw
TOML bytes to the content-addressed parity store, and records the first/known
declaration metadata in the store. Ledger appending is deferred to WP02 (Ring 1)
without modifying the stored Ring 0 blob.
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.parity.schema import ParityValidationError, validate_declaration
from bathos.parity.store import ParityStore, atomic_write_json, require_parity_store


class ParityDeclareError(RuntimeError):
    """Raised when ``bth parity declare`` cannot capture git HEAD or read declaration."""


@dataclass(frozen=True)
class DeclareResult:
    """Result of declaring a ``*.parity.bth.toml`` file into the parity store."""

    record_id: str
    declaration_sha256: str
    head_commit: str
    blob_path: Path
    is_first_declaration: bool
    first_declaration_sha256: str


def _capture_git_head(repo_dir: Path) -> str:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(repo_dir),
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise ParityDeclareError(f"Failed to invoke git in {repo_dir}: {exc}") from exc
    if proc.returncode != 0:
        raise ParityDeclareError(
            f"Declaration directory {repo_dir} is not in a git repository with HEAD: {proc.stderr.strip()}"
        )
    sha = proc.stdout.strip()
    if not sha:
        raise ParityDeclareError(f"git rev-parse HEAD returned empty output in {repo_dir}")
    return sha


def load_parsed_declaration(decl_path: Path) -> tuple[bytes, dict[str, Any]]:
    """Read raw declaration bytes and parse TOML."""
    if not decl_path.is_file():
        raise ParityDeclareError(f"Declaration file not found: {decl_path}")
    raw_bytes = decl_path.read_bytes()
    try:
        data = tomllib.loads(raw_bytes.decode("utf-8"))
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as exc:
        raise ParityDeclareError(f"Malformed TOML in {decl_path}: {exc}") from exc
    return raw_bytes, data


def declare_parity_record(
    decl_path: Path | str,
    *,
    parity_store: Path | str | None = None,
    repo_root: Path | None = None,
) -> DeclareResult:
    """Validate and write a declaration blob + HEAD metadata to the parity store."""
    path = Path(decl_path).resolve()
    raw_bytes, data = load_parsed_declaration(path)
    issues = validate_declaration(data)
    if issues:
        raise ParityValidationError(issues)

    record_id = str(data["parity"]["record_id"])
    head_dir = Path(repo_root).resolve() if repo_root is not None else path.parent
    head_commit = _capture_git_head(head_dir)

    store, _res = require_parity_store(explicit=parity_store, start_dir=path.parent)
    decl_sha = store.put_bytes(raw_bytes)
    blob_path = store.blob_path(decl_sha)

    decl_dir = store.root / "declarations" / record_id
    first_meta_path = decl_dir / "first.json"
    version_meta_path = decl_dir / f"{decl_sha}.json"

    meta_payload = {
        "record_id": record_id,
        "declaration_sha256": decl_sha,
        "head_commit": head_commit,
        "declared_at": datetime.now(UTC).isoformat(),
        "source_path": str(path),
    }
    if not version_meta_path.is_file():
        atomic_write_json(version_meta_path, meta_payload)

    is_first = not first_meta_path.is_file()
    if is_first:
        atomic_write_json(first_meta_path, meta_payload)
        first_sha = decl_sha
    else:
        first_data = json.loads(first_meta_path.read_text(encoding="utf-8"))
        first_sha = str(first_data["declaration_sha256"])

    return DeclareResult(
        record_id=record_id,
        declaration_sha256=decl_sha,
        head_commit=head_commit,
        blob_path=blob_path,
        is_first_declaration=is_first,
        first_declaration_sha256=first_sha,
    )


def load_declaration_baseline(
    store: ParityStore,
    record_id: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(first_meta, first_declaration_data)`` from store if ``declare`` was run."""
    first_meta_path = store.root / "declarations" / record_id / "first.json"
    if not first_meta_path.is_file():
        return None, None
    first_meta = json.loads(first_meta_path.read_text(encoding="utf-8"))
    first_sha = str(first_meta["declaration_sha256"])
    raw_bytes = store.get_bytes(first_sha)
    first_data = tomllib.loads(raw_bytes.decode("utf-8"))
    return first_meta, first_data


def load_declaration_version_meta(
    store: ParityStore,
    record_id: str,
    decl_sha: str,
) -> dict[str, Any] | None:
    """Return stored declaration metadata (e.g. ``head_commit``) for ``decl_sha`` if declared."""
    meta_path = store.root / "declarations" / record_id / f"{decl_sha}.json"
    if not meta_path.is_file():
        return None
    return json.loads(meta_path.read_text(encoding="utf-8"))
