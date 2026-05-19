# Schema Versioning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Centralize schema versioning with a `CURRENT_SCHEMA_VERSION` constant, document the v0→v1→v2 migration history in code, add a `_schema_migrations` audit table to the warm DuckDB, and make `bth migrate` use `schema_version` rather than column-presence checks.

**Architecture:** Add `CURRENT_SCHEMA_VERSION = "2"` to `schema.py`. Update `Run` to use it. Add `_schema_migrations` table to `compact.py` populated at compaction. Update `migrate.py` to write `schema_version` on upgraded fragments. Add `bth version` CLI command showing catalog schema status.

**Tech Stack:** Python 3.12, DuckDB, PyArrow, Typer

---

## File Map

| Action | Path |
|--------|------|
| Modify | `src/bathos/schema.py` |
| Modify | `src/bathos/compact.py` |
| Modify | `src/bathos/migrate.py` |
| Modify | `src/bathos/cli.py` |
| Modify | `tests/test_schema.py` |
| Modify | `tests/test_migrate.py` |
| Modify | `tests/test_compact.py` |

---

## Schema Version History

```
v0  (legacy): No schema_version field, no hostname field.
v1  (legacy): Added schema_version field (string). No hostname.
v2  (current): Added hostname field. COOL_SCHEMA has 18 fields.
```

---

## Task 1: Add `CURRENT_SCHEMA_VERSION` constant to `schema.py`

**Files:**
- Modify: `src/bathos/schema.py`

- [ ] **Step 1.1: Write failing test**

Add to `tests/test_schema.py`:

```python
def test_current_schema_version_defined():
    from bathos.schema import CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_VERSION == "2"


def test_run_schema_version_uses_constant():
    from bathos.schema import Run, CURRENT_SCHEMA_VERSION
    r = Run(project_slug="p", command="c", argv=["c"],
            git_hash="abc", git_branch="main", git_dirty=False)
    assert r.schema_version == CURRENT_SCHEMA_VERSION
```

- [ ] **Step 1.2: Run to confirm failure**

```bash
uv run pytest tests/test_schema.py::test_current_schema_version_defined -v
```

Expected: `FAILED` — `ImportError: cannot import name 'CURRENT_SCHEMA_VERSION'`

- [ ] **Step 1.3: Add constant to `schema.py`**

Near the top of `src/bathos/schema.py`, before `COOL_SCHEMA`, add:

```python
CURRENT_SCHEMA_VERSION = "2"
```

Then in the `Run` dataclass, update the `schema_version` default:

```python
    schema_version: str = field(default_factory=lambda: CURRENT_SCHEMA_VERSION)
```

(Replace `schema_version: str = "2"` with this.)

- [ ] **Step 1.4: Run tests**

```bash
uv run pytest tests/test_schema.py -v
```

Expected: all pass

- [ ] **Step 1.5: Update any hardcoded "2" references in compact.py and migrate.py**

```bash
grep -n '"2"' /home/marielle/projects/bathos/src/bathos/compact.py
grep -n '"2"' /home/marielle/projects/bathos/src/bathos/migrate.py
```

For each occurrence that represents the schema version, replace with `CURRENT_SCHEMA_VERSION` (import it from `bathos.schema`).

- [ ] **Step 1.6: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 1.7: Commit**

```bash
git add src/bathos/schema.py src/bathos/compact.py src/bathos/migrate.py tests/test_schema.py
git commit -m "feat(schema): add CURRENT_SCHEMA_VERSION constant, document v0/v1/v2 history"
```

---

## Task 2: Add `_schema_migrations` audit table to warm DuckDB

**Files:**
- Modify: `src/bathos/compact.py`

- [ ] **Step 2.1: Write failing test**

Add to `tests/test_compact.py`:

```python
def test_compact_creates_schema_migrations_table(tmp_path):
    """bth compact creates _schema_migrations table in warm DuckDB."""
    import duckdb
    from bathos.schema import Run
    from bathos.catalog import write_run
    from bathos.compact import compact

    r = Run(project_slug="p", command="c", argv=["c"],
            git_hash="abc", git_branch="main", git_dirty=False)
    write_run(r, tmp_path)
    compact(tmp_path)

    con = duckdb.connect(str(tmp_path / "bathos.db"))
    tables = [row[0] for row in con.execute("SHOW TABLES").fetchall()]
    con.close()
    assert "_schema_migrations" in tables


def test_schema_migrations_has_record(tmp_path):
    """_schema_migrations contains a record after compact."""
    import duckdb
    from bathos.schema import Run, CURRENT_SCHEMA_VERSION
    from bathos.catalog import write_run
    from bathos.compact import compact

    r = Run(project_slug="p", command="c", argv=["c"],
            git_hash="abc", git_branch="main", git_dirty=False)
    write_run(r, tmp_path)
    compact(tmp_path)

    con = duckdb.connect(str(tmp_path / "bathos.db"))
    rows = con.execute("SELECT warm_version FROM _schema_migrations").fetchall()
    con.close()
    assert len(rows) >= 1
    assert rows[-1][0] == CURRENT_SCHEMA_VERSION
```

- [ ] **Step 2.2: Run to confirm failure**

```bash
uv run pytest tests/test_compact.py::test_compact_creates_schema_migrations_table -v
```

Expected: `FAILED` — `AssertionError: '_schema_migrations' not in tables`

- [ ] **Step 2.3: Add `_schema_migrations` table creation to `compact.py`**

Find where `_schema_meta` or the warm DB is initialized in `compact.py`. After the `CREATE TABLE IF NOT EXISTS runs` DDL, add:

```python
con.execute("""
    CREATE TABLE IF NOT EXISTS _schema_migrations (
        warm_version TEXT NOT NULL,
        migrated_at TIMESTAMPTZ DEFAULT now(),
        notes TEXT
    )
""")
```

Then after the initial schema setup (where `_schema_meta` is populated), insert a migration record:

```python
from bathos.schema import CURRENT_SCHEMA_VERSION
con.execute(
    "INSERT INTO _schema_migrations (warm_version, notes) VALUES (?, ?)",
    [CURRENT_SCHEMA_VERSION, "compact"],
)
```

Read `compact.py` carefully to find the exact location before editing.

- [ ] **Step 2.4: Run tests**

```bash
uv run pytest tests/test_compact.py -v
```

Expected: all pass

- [ ] **Step 2.5: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 2.6: Commit**

```bash
git add src/bathos/compact.py tests/test_compact.py
git commit -m "feat(compact): add _schema_migrations audit table to warm DuckDB"
```

---

## Task 3: Make `bth migrate` write `schema_version` on upgraded fragments

**Files:**
- Modify: `src/bathos/migrate.py`

- [ ] **Step 3.1: Write failing test**

Add to `tests/test_migrate.py`:

```python
def test_migrate_writes_schema_version_on_upgraded_fragments(tmp_path):
    """migrate_catalog sets schema_version = CURRENT_SCHEMA_VERSION on upgraded fragments."""
    import pyarrow.parquet as pq
    from bathos.migrate import migrate_catalog
    from bathos.schema import CURRENT_SCHEMA_VERSION

    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    _write_old_fragment(runs_dir, "ver_test")

    migrate_catalog(tmp_path, dry_run=False)

    tbl = pq.read_table(runs_dir / "run_ver_test.parquet")
    versions = tbl.column("schema_version").to_pylist()
    assert all(v == CURRENT_SCHEMA_VERSION for v in versions)
```

- [ ] **Step 3.2: Run to confirm failure**

```bash
uv run pytest tests/test_migrate.py::test_migrate_writes_schema_version_on_upgraded_fragments -v
```

Expected: `FAILED` — the current migrate doesn't explicitly set `schema_version`

- [ ] **Step 3.3: Update `migrate_catalog` to set `schema_version`**

In `src/bathos/migrate.py`, after adding missing columns, add logic to update the `schema_version` column value to `CURRENT_SCHEMA_VERSION`:

```python
from bathos.schema import COOL_SCHEMA, CURRENT_SCHEMA_VERSION

# After appending missing columns and reordering...
# Update schema_version to current for all rows
import pyarrow as pa
schema_version_idx = tbl.schema.get_field_index("schema_version")
if schema_version_idx >= 0:
    tbl = tbl.set_column(
        schema_version_idx,
        "schema_version",
        pa.array([CURRENT_SCHEMA_VERSION] * len(tbl), type=pa.string()),
    )
```

Place this before the `tbl.cast(COOL_SCHEMA)` call.

- [ ] **Step 3.4: Run tests**

```bash
uv run pytest tests/test_migrate.py -v
```

Expected: all pass

- [ ] **Step 3.5: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 3.6: Commit**

```bash
git add src/bathos/migrate.py tests/test_migrate.py
git commit -m "feat(migrate): stamp CURRENT_SCHEMA_VERSION on all upgraded Parquet fragments"
```

---

## Task 4: Add `bth catalog-version` command

**Files:**
- Modify: `src/bathos/cli.py`

- [ ] **Step 4.1: Add command**

```python
@app.command("catalog-version")
def catalog_version_cmd():
    """Show schema version status of the catalog."""
    from bathos.schema import CURRENT_SCHEMA_VERSION
    from bathos.migrate import migrate_catalog

    catalog_dir = _catalog_dir()
    typer.echo(f"Current schema version: {CURRENT_SCHEMA_VERSION}")

    result = migrate_catalog(catalog_dir, dry_run=True)
    typer.echo(f"Cool-tier fragments: {result.scanned} scanned, {result.migrated} need migration.")

    db_path = catalog_dir / "bathos.db"
    if db_path.exists():
        import duckdb
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            rows = con.execute(
                "SELECT warm_version, migrated_at FROM _schema_migrations ORDER BY migrated_at DESC LIMIT 1"
            ).fetchall()
            if rows:
                typer.echo(f"Warm DB version: {rows[0][0]} (last migration: {rows[0][1]})")
        except Exception:
            typer.echo("Warm DB: no migration history found.")
        finally:
            con.close()
    else:
        typer.echo("Warm DB: not yet created (run bth compact).")
```

- [ ] **Step 4.2: Smoke test**

```bash
uv run bth catalog-version
```

Expected: version info printed, no error

- [ ] **Step 4.3: Run full suite**

```bash
uv run pytest -q
```

Expected: all pass

- [ ] **Step 4.4: Commit**

```bash
git add src/bathos/cli.py
git commit -m "feat(cli): add bth catalog-version to show schema status"
```

---

## Final Verification

```bash
uv run bth catalog-version
uv run bth migrate --dry-run
uv run pytest -q
```

All tests pass. `catalog-version` shows version 2 with 0 fragments needing migration.
