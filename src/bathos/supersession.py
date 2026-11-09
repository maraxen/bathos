"""Typed supersession edges across runs, findings, sidecars, and documents.

Spec 261106 Phase 2 (D5):
- entity types: "run" | "finding" | "sidecar" | "doc"
- DFS DAG cycle detection (CycleRejectedError)
- Target existence validation (SupersededTargetNotFoundError)
- unit_of_work emission of "supersession.declared"
- Bidirectional queries (replaces vs replaced_by)
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bathos.campaign_edges import CycleRejectedError
from bathos.config import default_catalog_dir
from bathos.errors import SupersededTargetNotFoundError
from bathos.index import SUPERSESSION_EDGES_DDL, connect_read
from bathos.runlog.emit import emit_or_legacy, unit_of_work

VALID_ENTITY_TYPES = frozenset({"run", "finding", "sidecar", "doc"})


@dataclass(frozen=True)
class SupersessionEdge:
    id: str
    entity_type: str
    entity_id: str
    superseded_by_type: str
    superseded_by_id: str
    reason: str
    declared_by: str
    declared_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "entity_type": self.entity_type,
            "entity_id": self.entity_id,
            "superseded_by_type": self.superseded_by_type,
            "superseded_by_id": self.superseded_by_id,
            "reason": self.reason,
            "declared_by": self.declared_by,
            "declared_at": self.declared_at,
        }


def _entity_exists(
    entity_type: str,
    entity_id: str,
    catalog_dir: Path,
    workspace_root: Path | None = None,
) -> bool:
    """Validate existence of an entity target."""
    if entity_type == "run":
        from bathos.catalog import read_runs
        from bathos.query import get_run

        try:
            r = get_run(entity_id, catalog_dir, allow_cool_fallback=True, include_superseded=True)
            if r is not None:
                return True
        except Exception:
            pass
        try:
            for run in read_runs(catalog_dir):
                if run.id == entity_id or run.id.startswith(entity_id):
                    return True
        except Exception:
            pass
        return False
    elif entity_type == "finding":
        from bathos.findings import get_findings_dir

        fdir = get_findings_dir(workspace_root)
        if not fdir.exists():
            return False
        for toml_path in fdir.glob("*.toml"):
            if toml_path.stem == entity_id or toml_path.name == f"{entity_id}.toml":
                return True
            try:
                import tomllib

                data = tomllib.loads(toml_path.read_text())
                if data.get("id") == entity_id or data.get("question_slug") == entity_id:
                    return True
            except Exception:
                pass
        return False
    elif entity_type in ("sidecar", "doc"):
        root = workspace_root or Path.cwd()
        p = Path(entity_id)
        if p.is_absolute():
            return p.exists()
        return (root / p).exists()
    return False


def _check_cycle_dfs(
    con: duckdb.DuckDBPyConnection,
    entity_type: str,
    entity_id: str,
    successor_type: str,
    successor_id: str,
) -> bool:
    """Return True if adding entity -> successor would introduce a cycle."""
    if entity_type == successor_type and entity_id == successor_id:
        return True

    cols = [c[1] for c in con.execute("PRAGMA table_info('supersession_edges')").fetchall()]
    use_canonical = "entity_type" in cols

    visited: set[tuple[str, str]] = set()
    frontier: list[tuple[str, str]] = [(successor_type, successor_id)]

    while frontier:
        curr_type, curr_id = frontier.pop()
        if (curr_type, curr_id) == (entity_type, entity_id):
            return True
        if (curr_type, curr_id) in visited:
            continue
        visited.add((curr_type, curr_id))

        if use_canonical:
            rows = con.execute(
                "SELECT superseded_by_type, superseded_by_id FROM supersession_edges "
                "WHERE entity_type = ? AND entity_id = ?",
                [curr_type, curr_id],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT to_type, to_id FROM supersession_edges "
                "WHERE from_type = ? AND from_id = ?",
                [curr_type, curr_id],
            ).fetchall()

        for succ_t, succ_i in rows:
            if (succ_t, succ_i) not in visited:
                frontier.append((succ_t, succ_i))

    return False


def add_supersession_edge(
    entity_type: str,
    entity_id: str,
    superseded_by_type: str,
    superseded_by_id: str,
    reason: str,
    *,
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
    declared_by: str | None = None,
) -> SupersessionEdge:
    """Record a typed supersession edge with cycle check and target existence validation."""
    if entity_type not in VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid entity_type {entity_type!r}; expected one of {sorted(VALID_ENTITY_TYPES)}"
        )
    if superseded_by_type not in VALID_ENTITY_TYPES:
        raise ValueError(
            f"Invalid superseded_by_type {superseded_by_type!r}; expected one of {sorted(VALID_ENTITY_TYPES)}"
        )

    cd = Path(catalog_dir or default_catalog_dir())
    ws_root = Path(workspace_root) if workspace_root else None

    # Target existence check
    if not _entity_exists(entity_type, entity_id, cd, ws_root):
        raise SupersededTargetNotFoundError(
            f"Target entity '{entity_id}' of type '{entity_type}' not found",
            entity_type=entity_type,
            entity_id=entity_id,
        )

    # Successor existence check
    if not _entity_exists(superseded_by_type, superseded_by_id, cd, ws_root):
        raise SupersededTargetNotFoundError(
            f"Successor entity '{superseded_by_id}' of type '{superseded_by_type}' not found",
            entity_type=superseded_by_type,
            entity_id=superseded_by_id,
        )

    # Self-loop cycle check
    if entity_type == superseded_by_type and entity_id == superseded_by_id:
        raise CycleRejectedError(
            f"Adding supersession edge from {entity_type}:{entity_id} to itself would create a cycle."
        )

    con = connect_read(cd, missing="empty")
    try:
        has_table = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'supersession_edges'"
        ).fetchone()
        if has_table and has_table[0] > 0 and _check_cycle_dfs(con, entity_type, entity_id, superseded_by_type, superseded_by_id):
            raise CycleRejectedError(
                f"Adding supersession edge from {entity_type}:{entity_id} to "
                f"{superseded_by_type}:{superseded_by_id} would create a cycle."
            )
    finally:
        con.close()

    edge_id = str(uuid.uuid4())
    now_iso = datetime.now(UTC).isoformat()
    actor_str = declared_by or os.environ.get("USER", "unknown")

    edge = SupersessionEdge(
        id=edge_id,
        entity_type=entity_type,
        entity_id=entity_id,
        superseded_by_type=superseded_by_type,
        superseded_by_id=superseded_by_id,
        reason=reason,
        declared_by=actor_str,
        declared_at=now_iso,
    )

    def _write_legacy() -> None:
        db_path = cd / "bathos.db"
        conn = duckdb.connect(str(db_path))
        try:
            conn.execute(SUPERSESSION_EDGES_DDL)
            conn.execute(
                "INSERT INTO supersession_edges "
                "(id, entity_type, entity_id, superseded_by_type, superseded_by_id, reason, declared_by, declared_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [
                    edge.id,
                    edge.entity_type,
                    edge.entity_id,
                    edge.superseded_by_type,
                    edge.superseded_by_id,
                    edge.reason,
                    edge.declared_by,
                    edge.declared_at,
                ],
            )
        finally:
            conn.close()

    with unit_of_work(cd):
        emit_or_legacy(
            kind="supersession.declared",
            entity=[edge_id],
            data=edge.to_dict(),
            legacy_write=_write_legacy,
            cwd=cd,
        )

    return edge


def get_superseded_by(
    entity_type: str, entity_id: str, catalog_dir: Path | str | None = None
) -> list[dict[str, Any]]:
    """Return all successors that supersede this entity (replaces query)."""
    cd = Path(catalog_dir or default_catalog_dir())
    con = connect_read(cd, missing="empty")
    try:
        has_table = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'supersession_edges'"
        ).fetchone()
        if not has_table or has_table[0] == 0:
            return []
        cols_info = [c[1] for c in con.execute("PRAGMA table_info('supersession_edges')").fetchall()]
        if "entity_type" in cols_info:
            rows = con.execute(
                "SELECT id, superseded_by_type, superseded_by_id, reason, declared_by, declared_at "
                "FROM supersession_edges WHERE entity_type = ? AND entity_id = ? "
                "ORDER BY declared_at ASC",
                [entity_type, entity_id],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT record_id, to_type, to_id, reason, 'unknown', declared_at "
                "FROM supersession_edges WHERE from_type = ? AND from_id = ? "
                "ORDER BY declared_at ASC",
                [entity_type, entity_id],
            ).fetchall()
        cols = ["id", "superseded_by_type", "superseded_by_id", "reason", "declared_by", "declared_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()


def get_replaces(
    superseded_by_type: str, superseded_by_id: str, catalog_dir: Path | str | None = None
) -> list[dict[str, Any]]:
    """Return all predecessors replaced by this successor (replaced_by query)."""
    cd = Path(catalog_dir or default_catalog_dir())
    con = connect_read(cd, missing="empty")
    try:
        has_table = con.execute(
            "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'supersession_edges'"
        ).fetchone()
        if not has_table or has_table[0] == 0:
            return []
        cols_info = [c[1] for c in con.execute("PRAGMA table_info('supersession_edges')").fetchall()]
        if "superseded_by_type" in cols_info:
            rows = con.execute(
                "SELECT id, entity_type, entity_id, reason, declared_by, declared_at "
                "FROM supersession_edges WHERE superseded_by_type = ? AND superseded_by_id = ? "
                "ORDER BY declared_at ASC",
                [superseded_by_type, superseded_by_id],
            ).fetchall()
        else:
            rows = con.execute(
                "SELECT record_id, from_type, from_id, reason, 'unknown', declared_at "
                "FROM supersession_edges WHERE to_type = ? AND to_id = ? "
                "ORDER BY declared_at ASC",
                [superseded_by_type, superseded_by_id],
            ).fetchall()
        cols = ["id", "entity_type", "entity_id", "reason", "declared_by", "declared_at"]
        return [dict(zip(cols, r)) for r in rows]
    finally:
        con.close()
