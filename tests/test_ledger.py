"""Unit and regression tests for ``bathos.ledger.AppendOnlyLedger`` (#1474)."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from bathos.ledger import AppendOnlyLedger, now_iso


@dataclass(frozen=True)
class _SampleRecord:
    key: str
    val: str
    amended_at: str = field(default_factory=now_iso)
    id: str = "rec-1"
    extra: str | None = None


_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("key", pa.string()),
        pa.field("val", pa.string()),
        pa.field("amended_at", pa.string()),
        pa.field("extra", pa.string()),
    ]
)


def _make_ledger(con_box: list[duckdb.DuckDBPyConnection] | None = None) -> AppendOnlyLedger[_SampleRecord]:
    def _connect(cat: Path | str) -> duckdb.DuckDBPyConnection:
        con = duckdb.connect(str(Path(cat) / "bathos.db"))
        con.execute(
            "CREATE TABLE IF NOT EXISTS sample_ledger ("
            "id TEXT PRIMARY KEY, key TEXT NOT NULL, val TEXT NOT NULL, "
            "amended_at TEXT NOT NULL, extra TEXT)"
        )
        if con_box is not None:
            con_box.append(con)
        return con

    return AppendOnlyLedger(
        table_name="sample_ledger",
        fragments_dirname="sample_frags",
        fragment_prefix="sample",
        pk_col="id",
        order_col="amended_at",
        columns=("id", "key", "val", "amended_at", "extra"),
        legacy_columns=("id", "key", "val", "amended_at"),
        fragment_schema=_SCHEMA,
        runlog_kind="sample.recorded",
        telemetry_prefix="sample_ledger",
        connect_fn=_connect,
        record_from_values=lambda v: _SampleRecord(
            id=v["id"],
            key=v["key"],
            val=v["val"],
            amended_at=v["amended_at"],
            extra=v.get("extra"),
        ),
        append_event_fields=lambda r: {"key": r.key, "val": r.val},
    )


def test_write_read_and_idempotent_warm_insert(tmp_path: Path) -> None:
    cat = tmp_path / "cat"
    cat.mkdir()
    ledger = _make_ledger()

    r1 = _SampleRecord(id="r1", key="k1", val="v1", amended_at="2026-11-24T00:00:00+00:00")
    r2 = _SampleRecord(id="r2", key="k1", val="v2", amended_at="2026-11-24T01:00:00+00:00")
    ledger.append_record(r1, cat)
    ledger.append_record(r2, cat)

    # Duplicate insert with same primary key is a no-op (append-only)
    ledger.insert_warm_row(_SampleRecord(id="r1", key="k1", val="mutated"), cat)

    frags = ledger.read_fragments(cat)
    assert {r.id for r in frags} == {"r1", "r2"}
    latest = ledger.latest_record_via_connect(cat, [("key", "k1")])
    assert latest is not None and latest.id == "r2" and latest.val == "v2"

    # Original r1 row was never mutated
    con = ledger.connect_fn(cat)
    try:
        row = con.execute("SELECT val FROM sample_ledger WHERE id = 'r1'").fetchone()
        assert row == ("v1",)
    finally:
        con.close()


def test_read_fragments_ignores_tmp_and_skips_corrupt(tmp_path: Path) -> None:
    cat = tmp_path / "cat"
    cat.mkdir()
    ledger = _make_ledger()

    r1 = _SampleRecord(id="good-1", key="k1", val="ok")
    ledger.write_fragment(r1, cat)
    frag_dir = ledger.fragments_dir(cat)
    (frag_dir / "sample_interrupted.tmp.parquet").write_bytes(b"partial write")
    (frag_dir / "sample_corrupt.parquet").write_bytes(b"corrupt bytes")

    records = ledger.read_fragments(cat)
    assert [r.id for r in records] == ["good-1"]


def test_legacy_columns_fallback_on_binder_exception(tmp_path: Path) -> None:
    cat = tmp_path / "cat"
    cat.mkdir()
    ledger = _make_ledger()

    con = duckdb.connect(str(cat / "bathos.db"))
    con.execute(
        "CREATE TABLE sample_ledger (id TEXT PRIMARY KEY, key TEXT, val TEXT, amended_at TEXT)"
    )
    con.execute(
        "INSERT INTO sample_ledger VALUES ('old-1', 'k-old', 'v-old', '2026-01-01T00:00:00+00:00')"
    )
    con.close()

    ro = duckdb.connect(str(cat / "bathos.db"), read_only=True)
    try:
        rec = ledger.latest_record_using_conn(ro, [("key", "k-old")])
        assert rec is not None
        assert rec.id == "old-1"
        assert rec.extra is None
    finally:
        ro.close()


def test_missing_table_returns_none_while_bad_schema_raises() -> None:
    ledger = _make_ledger()
    empty_con = duckdb.connect(":memory:")
    try:
        assert ledger.latest_record_using_conn(empty_con, [("key", "x")]) is None
    finally:
        empty_con.close()

    bad_con = duckdb.connect(":memory:")
    try:
        bad_con.execute("CREATE TABLE sample_ledger (wrong INT)")
        with pytest.raises(duckdb.Error):
            ledger.latest_record_using_conn(bad_con, [("key", "x")])
    finally:
        bad_con.close()


def test_all_three_ledgers_adopt_append_only_ledger() -> None:
    from bathos import archived_items, blast_radius, trust_ledger

    assert isinstance(blast_radius._LEDGER, AppendOnlyLedger)
    assert isinstance(trust_ledger._LEDGER, AppendOnlyLedger)
    assert isinstance(archived_items._LEDGER, AppendOnlyLedger)


def test_read_fragments_skips_schema_mismatch_and_missing_required_col(tmp_path: Path) -> None:
    import pyarrow.parquet as pq

    cat = tmp_path / "cat"
    cat.mkdir()
    ledger = _make_ledger()

    r1 = _SampleRecord(id="good-1", key="k1", val="ok")
    ledger.write_fragment(r1, cat)
    frag_dir = ledger.fragments_dir(cat)

    # Incompatible column type for `extra` (int64 instead of string)
    bad_type_tbl = pa.table(
        {
            "id": ["bad-type"],
            "key": ["k1"],
            "val": ["v"],
            "amended_at": ["2026-11-24T00:00:00+00:00"],
            "extra": pa.array([123], type=pa.int64()),
        }
    )
    pq.write_table(bad_type_tbl, frag_dir / "sample_badtype.parquet")

    # Missing required column `val` -> KeyError during row conversion
    missing_col_tbl = pa.table(
        {
            "id": ["missing-col"],
            "key": ["k1"],
            "amended_at": ["2026-11-24T00:00:00+00:00"],
        }
    )
    pq.write_table(missing_col_tbl, frag_dir / "sample_missingcol.parquet")

    records = ledger.read_fragments(cat)
    assert [r.id for r in records] == ["good-1"]
