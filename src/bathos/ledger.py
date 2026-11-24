"""Shared append-only cool/warm ledger helper (`#1474`).

Provides :class:`AppendOnlyLedger`, the common substrate used by
``bathos.blast_radius``, ``bathos.trust_ledger``, and ``bathos.archived_items``:

- Cool-tier immutable Parquet fragments (one per record, atomic tmp-write + POSIX
  rename, ``*.tmp.parquet`` exclusion, and corrupt-fragment telemetry skip so one
  bad file never crashes ``bathos.compact.compact()``).
- Warm-tier DuckDB idempotent insert (skip-if-present by primary key, never
  updating an existing row) with single-connection reuse for batched writes.
- AC-25 runlog-flagged append (``unit_of_work`` + ``emit_or_legacy``) and
  latest-wins query helpers (including read-only legacy-column fallback and
  log-mode / cool-fragment fallback).

``duckdb.connect`` itself remains in each caller's ``connect_fn`` so AC-18's
AST check on catalog connections continues to track each legacy write site.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.index import catalog_readable, connect_read
from bathos.telemetry import event


def now_iso() -> str:
    """Return current UTC timestamp in ISO-8601 format."""
    return datetime.now(UTC).isoformat()


def _default_row_to_values(record: Any, columns: tuple[str, ...]) -> list[Any]:
    return [getattr(record, col) for col in columns]


def _default_fragment_row(record: Any, columns: tuple[str, ...]) -> dict[str, list[Any]]:
    return {col: [getattr(record, col)] for col in columns}


@dataclass(frozen=True)
class AppendOnlyLedger[RecordT]:
    """Declarative specification and operations for an append-only cool/warm ledger."""

    table_name: str
    fragments_dirname: str
    fragment_prefix: str
    pk_col: str
    order_col: str
    columns: tuple[str, ...]
    fragment_schema: pa.Schema
    runlog_kind: str
    telemetry_prefix: str
    connect_fn: Callable[[Path | str], duckdb.DuckDBPyConnection]
    record_from_values: Callable[[Mapping[str, Any]], RecordT]
    append_event_fields: Callable[[RecordT], dict[str, Any]]
    record_to_fragment_row: Callable[[RecordT], dict[str, list[Any]]] | None = None
    fragment_pydict_to_record: Callable[[dict[str, list[Any]], int], RecordT] | None = None
    record_to_warm_values: Callable[[RecordT], list[Any]] | None = None
    legacy_columns: tuple[str, ...] | None = None

    def fragments_dir(self, catalog_dir: Path | str) -> Path:
        """Return the cool-tier fragments directory for this ledger."""
        return Path(catalog_dir) / self.fragments_dirname

    def pk_of(self, record: RecordT) -> str:
        """Return the primary-key value for *record*."""
        return str(getattr(record, self.pk_col))

    def order_of(self, record: RecordT) -> str:
        """Return the ordering timestamp string for *record*."""
        return str(getattr(record, self.order_col) or "")

    def write_fragment(self, record: RecordT, catalog_dir: Path | str) -> None:
        """Write an immutable cool-tier Parquet fragment for one ledger record."""
        frag_dir = self.fragments_dir(catalog_dir)
        frag_dir.mkdir(parents=True, exist_ok=True)
        pk = self.pk_of(record)
        target = frag_dir / f"{self.fragment_prefix}_{pk}.parquet"
        tmp = frag_dir / f"{self.fragment_prefix}_{pk}.tmp.parquet"

        row_dict = (
            self.record_to_fragment_row(record)
            if self.record_to_fragment_row is not None
            else _default_fragment_row(record, self.columns)
        )

        t_start = time.monotonic()
        table = pa.table(row_dict, schema=self.fragment_schema)
        pq.write_table(table, tmp)
        tmp.rename(target)  # atomic on POSIX
        duration_ms = (time.monotonic() - t_start) * 1000

        event(
            f"{self.telemetry_prefix}.write_fragment",
            path=str(target),
            rows=1,
            duration_ms=int(duration_ms),
        )

    def read_fragments(self, catalog_dir: Path | str) -> list[RecordT]:
        """Read every cool-tier ledger fragment, unfolded (full history).

        Excludes ``*.tmp.parquet`` and skips corrupt fragments with telemetry so
        one damaged fragment never crashes ``bathos.compact.compact()``.
        """
        frag_dir = self.fragments_dir(catalog_dir)
        if not frag_dir.exists():
            return []
        parquet_files = [
            f
            for f in frag_dir.glob(f"{self.fragment_prefix}_*.parquet")
            if not f.name.endswith(".tmp.parquet")
        ]
        if not parquet_files:
            return []

        tables = []
        for f in parquet_files:
            try:
                tables.append(pq.read_table(f))
            except Exception as exc:  # noqa: BLE001 - one bad fragment must not crash compact()
                event(f"{self.telemetry_prefix}.corrupt_fragment", path=str(f), error=str(exc))
        if not tables:
            return []

        combined = pa.concat_tables(tables, promote_options="permissive")
        pydict = combined.to_pydict()

        records: list[RecordT] = []
        for i in range(combined.num_rows):
            if self.fragment_pydict_to_record is not None:
                records.append(self.fragment_pydict_to_record(pydict, i))
            else:
                values = {col: pydict[col][i] for col in pydict}
                records.append(self.record_from_values(values))
        return records

    def insert_warm_row_using_conn(
        self, con: duckdb.DuckDBPyConnection, record: RecordT
    ) -> None:
        """Idempotently insert *record* into the warm table on an open connection."""
        pk = self.pk_of(record)
        existing = con.execute(
            f"SELECT {self.pk_col} FROM {self.table_name} WHERE {self.pk_col} = ?",  # noqa: S608
            [pk],
        ).fetchone()
        if existing:
            return  # append-only: never update an existing ledger row
        cols_sql = ", ".join(self.columns)
        placeholders = ", ".join("?" for _ in self.columns)
        values = (
            self.record_to_warm_values(record)
            if self.record_to_warm_values is not None
            else _default_row_to_values(record, self.columns)
        )
        con.execute(
            f"INSERT INTO {self.table_name} ({cols_sql}) VALUES ({placeholders})",  # noqa: S608
            values,
        )

    def insert_warm_row(self, record: RecordT, catalog_dir: Path | str) -> None:
        """Open a warm connection, insert *record* idempotently, and close."""
        con = self.connect_fn(catalog_dir)
        try:
            self.insert_warm_row_using_conn(con, record)
        finally:
            con.close()

    def append_record(
        self,
        record: RecordT,
        catalog_dir: Path | str,
        *,
        con: duckdb.DuckDBPyConnection | None = None,
        cwd: Path | None = None,
    ) -> RecordT:
        """Durably append one ledger record (cool fragment + warm row, or runlog event)."""
        from bathos.runlog.emit import emit_or_legacy, unit_of_work

        def _legacy_write() -> None:
            self.write_fragment(record, catalog_dir)
            if con is not None:
                self.insert_warm_row_using_conn(con, record)
            else:
                self.insert_warm_row(record, catalog_dir)

        with unit_of_work(Path(catalog_dir)):
            emit_or_legacy(
                kind=self.runlog_kind,
                entity=[self.pk_of(record)],
                data=asdict(cast(Any, record)),
                legacy_write=_legacy_write,
                cwd=cwd,
            )
        event(f"{self.telemetry_prefix}.append", **self.append_event_fields(record))
        return record

    def is_missing_table_error(self, exc: BaseException) -> bool:
        """True only when DuckDB reports that ``self.table_name`` does not exist."""
        msg = str(exc)
        return (
            isinstance(exc, duckdb.CatalogException)
            and bool(re.search(rf"\b{re.escape(self.table_name)}\b", msg, re.IGNORECASE))
            and "does not exist" in msg.lower()
        )

    def fetch_latest_record(
        self, con: duckdb.DuckDBPyConnection, key_filters: Sequence[tuple[str, Any]]
    ) -> RecordT | None:
        """Query *con* for the latest record matching *key_filters*."""
        where_sql = " AND ".join(f"{col} = ?" for col, _ in key_filters)
        params = [val for _, val in key_filters]

        def _query(cols: tuple[str, ...]):
            cols_sql = ", ".join(cols)
            return con.execute(
                f"SELECT {cols_sql} FROM {self.table_name} "  # noqa: S608
                f"WHERE {where_sql} ORDER BY {self.order_col} DESC LIMIT 1",
                params,
            ).fetchone()

        try:
            row = _query(self.columns)
            used_cols = self.columns
        except duckdb.BinderException:
            if self.legacy_columns is None:
                raise
            row = _query(self.legacy_columns)
            used_cols = self.legacy_columns

        if row is None:
            return None
        return self.record_from_values(dict(zip(used_cols, row, strict=True)))

    def latest_record_via_connect(
        self, catalog_dir: Path | str, key_filters: Sequence[tuple[str, Any]]
    ) -> RecordT | None:
        """Query the latest record by opening a connection via ``self.connect_fn``."""
        con = self.connect_fn(catalog_dir)
        try:
            return self.fetch_latest_record(con, key_filters)
        finally:
            con.close()

    def latest_record_from_fragments(
        self, catalog_dir: Path | str, key_filters: Sequence[tuple[str, Any]]
    ) -> RecordT | None:
        """Latest-by-order-col record from cool-tier Parquet fragments."""
        matching = [
            r
            for r in self.read_fragments(catalog_dir)
            if all(getattr(r, col) == val for col, val in key_filters)
        ]
        if not matching:
            return None
        return max(matching, key=lambda r: (self.order_of(r), self.pk_of(r)))

    def latest_record_log_aware(
        self, catalog_dir: Path | str, key_filters: Sequence[tuple[str, Any]]
    ) -> RecordT | None:
        """Latest record across log-mode ``index.db``, warm ``bathos.db``, or cool fragments."""
        from bathos.runlog.mode import is_log_mode

        cat = Path(catalog_dir)
        if is_log_mode(cat):
            con = connect_read(cat, read_only=True)
            try:
                return self.fetch_latest_record(con, key_filters)
            except duckdb.Error as exc:
                if self.is_missing_table_error(exc):
                    return None
                raise
            finally:
                con.close()

        db_path = cat / "bathos.db"
        if not db_path.exists():
            return self.latest_record_from_fragments(cat, key_filters)

        try:
            con = connect_read(cat, read_only=True)
        except duckdb.ConnectionException as exc:
            if "different configuration" not in str(exc).lower():
                raise
            con = connect_read(cat, read_only=False, wire_validity=False)
        try:
            return self.fetch_latest_record(con, key_filters)
        except duckdb.Error as exc:
            if self.is_missing_table_error(exc):
                return self.latest_record_from_fragments(cat, key_filters)
            raise
        finally:
            con.close()

    def latest_record_using_conn(
        self, con: duckdb.DuckDBPyConnection, key_filters: Sequence[tuple[str, Any]]
    ) -> RecordT | None:
        """Query an already-open connection; returns ``None`` only if the ledger table is absent."""
        try:
            return self.fetch_latest_record(con, key_filters)
        except duckdb.Error as exc:
            if self.is_missing_table_error(exc):
                return None
            raise

    def catalog_readable_for_fragments(self, catalog_dir: Path | str) -> bool:
        """Check whether the catalog DB is readable."""
        return catalog_readable(Path(catalog_dir))
