---
# Bathos Backup/Recovery Hardening — v0.7 Spec

## Summary

This sprint hardens bathos against data-loss and silent corruption scenarios introduced by the current implementation of `compact.py`, `migrate.py`, `archive.py`, and `sync.py`. Before these fixes, a process crash mid-compaction leaves `bathos.db` in a partially-written state with no rollback path, a failed fragment migration permanently destroys the original Parquet before the replacement is confirmed, archive manifests contain only row counts (unverifiable without re-reading the data), and rsync with `--ignore-existing` silently skips truncated partial files on the destination.

After the fixes, every destructive write is wrapped in a transaction or a write-then-rename pattern, the warm database is integrity-checked on every open, cold-tier archives are verifiable by SHA256 checksum, and sync truncation produces a logged warning rather than silent data loss. A new `bth verify` command exposes these checks as an interactive CLI and MCP tool. A new `bth compact --force-rebuild` option recovers a corrupt warm DB from cool-tier fragments.

---

## Fix 1: Transaction safety in compact.py

### Problem

`compact.py` opens `bathos.db` at line 329 via `duckdb.connect(str(db_path))` and immediately begins schema-creation DDL statements followed by an INSERT loop spanning lines 359-509. No transaction is opened before the loop, no COMMIT is issued after it, and no ROLLBACK is issued on failure. DuckDB in its default auto-commit mode commits each statement independently, so a crash or `KeyboardInterrupt` partway through the INSERT loop leaves `bathos.db` with a partial set of rows for that compaction run. On the next `bth compact` call those partially-inserted rows will be matched by the `SELECT id FROM runs WHERE id = ?` existence check and counted as `skipped`, causing the missing rows to never be ingested — silent, permanent data loss.

Additionally, the `campaign_runs` back-fill loop at lines 488-494 is also outside any transaction, so campaign membership may be committed while the corresponding run row was never written.

### Change

Wrap the entire ingest body in an explicit transaction using `con.begin()` / `con.commit()` / `con.rollback()` (method-call API, not raw SQL strings). Open the transaction **after** all schema-creation DDL and before the first INSERT. Schema DDL (`CREATE TABLE IF NOT EXISTS`) must stay outside the transaction because they are idempotent and must succeed even if a prior transaction was rolled back.

On any unhandled exception inside the loop, call `con.rollback()` and re-raise. Guard the rollback call with its own `try/except` so that a rollback failure (e.g., broken connection state) does not swallow the original exception.

Postmortem metadata UPDATEs for already-compacted runs (the `if existing: ... continue` branch) are intentionally kept inside the same transaction as new INSERTs. This means a crash during a new INSERT rolls back postmortem metadata updates that were computed in the same batch. This is the correct tradeoff: postmortem annotations are idempotent (the `.bth.postmortem.toml` files on disk remain, and the next compaction will re-apply the updates), whereas having partial-run INSERTs committed without their associated metadata is worse. This tradeoff is documented here so the implementer does not separate the two loops.

```python
# After all CREATE TABLE IF NOT EXISTS calls (line ~353), before "ingested = 0":
con.begin()
try:
    # ... existing for run in cool_runs: INSERT loop (lines 359-485) ...
    # ... existing campaign_runs back-fill loop (lines 488-494) ...
    # ... existing _schema_meta INSERT (lines 497-500) ...
    # ... existing _schema_migrations INSERT (lines 503-506) ...
    con.commit()
except Exception:
    try:
        con.rollback()
    except Exception:
        pass  # rollback failure must not swallow the original exception
    raise
finally:
    con.close()
```

Remove the existing bare `con.close()` at line 509, which is now inside the `finally` block above.

### Concurrent compaction safety

Two simultaneous `bth compact` calls can both pass the `SELECT id FROM runs WHERE id = ?` existence check before either commits an INSERT, potentially producing duplicate row violations or a lost-update race. To prevent this, acquire an advisory lock on the catalog directory at the start of `compact()` using a `.bth_compact.lock` sentinel file created with `open(..., 'x')` (exclusive creation — raises `FileExistsError` if lock is held). Release it in a `finally` block. If `FileExistsError` is raised, surface a clear error: `CompactionLockedError("Another bth compact process is running")`. The lock file contains the acquiring process's PID so stale locks (from killed processes) can be detected: if the PID in the file no longer exists (`os.kill(pid, 0)` raises `ProcessLookupError`), the lock is stale and may be removed before retrying.

```python
import os

class CompactionLockedError(RuntimeError):
    """Raised when compact() cannot acquire the advisory lock."""

lock_path = catalog_dir / ".bth_compact.lock"
try:
    with open(lock_path, "x") as f:
        f.write(str(os.getpid()))
except FileExistsError:
    # Check for stale lock
    try:
        pid = int(lock_path.read_text().strip())
        os.kill(pid, 0)  # raises ProcessLookupError if process is gone
        raise CompactionLockedError(
            f"Another bth compact is running (PID {pid}). "
            f"If that process is gone, delete {lock_path} and retry."
        )
    except (ProcessLookupError, ValueError):
        lock_path.unlink(missing_ok=True)
        with open(lock_path, "x") as f:
            f.write(str(os.getpid()))
try:
    # ... compact logic ...
finally:
    lock_path.unlink(missing_ok=True)
```

### Risk mitigation: postmortem UPDATEs rolled back on INSERT failure

**Risk:** A crash mid-INSERT discards postmortem metadata UPDATEs for already-compacted runs computed in the same batch.

**Mitigation:** Postmortem metadata is derived from `.bth.postmortem.toml` files on disk. The compact loop re-reads those files on every run, so a rolled-back UPDATE is automatically re-applied the next time `bth compact` runs successfully. The user loses no persistent data — only the in-flight computation of that batch is discarded. This is documented above in the prose rationale for keeping both loops inside the same transaction.

### Test scenario

**test name:** `test_compact_transaction_rollback`

**setup:** Create a catalog dir with three cool fragments. Monkeypatch `con.execute` to raise `RuntimeError("injected")` on the second INSERT into `runs` (detect by counting calls). Call `compact(catalog_dir)`.

**assertions:**
1. `compact()` raises `RuntimeError("injected")`.
2. `bathos.db` either does not exist or contains zero rows in the `runs` table (the transaction was rolled back).
3. No rows for any of the three run IDs appear in `bathos.db`.
4. A subsequent call to `compact(catalog_dir)` (with the monkeypatch removed) succeeds and returns `ingested=3, skipped=0`.

**test name:** `test_compact_lock_prevents_concurrent_compaction`

**setup:** Create the `.bth_compact.lock` file containing the current process's PID (so `os.kill(pid, 0)` succeeds). Call `compact(catalog_dir)`.

**assertions:**
1. `compact()` raises `CompactionLockedError`.
2. `bathos.db` is not created or modified.

**test name:** `test_compact_stale_lock_is_cleared`

**setup:** Create the `.bth_compact.lock` file containing PID `9999999` (assumed not to exist). Call `compact(catalog_dir)` with one cool fragment.

**assertions:**
1. `compact()` succeeds and returns `ingested=1`.
2. `.bth_compact.lock` is removed after completion.

---

## Fix 2: DuckDB integrity check on connect

### Problem

At line 329, `duckdb.connect(str(db_path))` opens `bathos.db` with no integrity verification. If `bathos.db` was partially written by a prior crash (before Fix 1 is applied, or from an OS-level kill signal), DuckDB will open the file and may silently read corrupt pages, return wrong row counts, or produce inconsistent query results. There is no mechanism to detect this and surface it to the user.

### Correct mechanism for detecting corruption in DuckDB 1.5.2

**CRITICAL:** `PRAGMA integrity_check` does **not** exist in DuckDB 1.5.2 (the installed version). Running `con.execute('PRAGMA integrity_check')` raises `CatalogException: Pragma Function with name integrity_check does not exist!`. Any implementation that calls this PRAGMA will break `compact()` for all callers — not just corrupt databases.

The correct detection mechanism is two-layered:

1. **Header corruption** is detected at connect time: `duckdb.connect(str(db_path))` raises `duckdb.IOException` when the file header is unreadable. Wrap the connect call in `try/except duckdb.IOException` and raise `CorruptDatabaseError` from it.

2. **Internal structural corruption** (tables missing, schema inconsistent after a connect that succeeded) is detected by a post-connect structural query. After a successful connect, run `SELECT COUNT(*) FROM _schema_meta` and catch any unexpected exception. A `CatalogException` or other DuckDB error on a table that must exist in a valid `bathos.db` indicates corruption that survived the connect.

### Change

```python
def _open_db(db_path: Path) -> duckdb.DuckDBPyConnection:
    """Open bathos.db, detecting corruption at connect and post-connect.

    Raises:
        CorruptDatabaseError: If the file header is unreadable (IOException at
            connect time) or if _schema_meta is inaccessible after a successful
            connect (structural corruption).
    """
    try:
        con = duckdb.connect(str(db_path))
    except duckdb.IOException as exc:
        raise CorruptDatabaseError(
            f"DuckDB could not open {db_path}: {exc}",
            db_path=db_path,
        ) from exc

    # Post-connect structural check: _schema_meta must be accessible in any
    # valid bathos.db that has been through at least one compact().
    if db_path.exists() and db_path.stat().st_size > 0:
        try:
            con.execute("SELECT COUNT(*) FROM _schema_meta").fetchone()
        except Exception as exc:
            con.close()
            raise CorruptDatabaseError(
                f"DuckDB opened {db_path} but _schema_meta is inaccessible: {exc}",
                db_path=db_path,
            ) from exc

    return con
```

Replace the bare `duckdb.connect(str(db_path))` at line 329 with `_open_db(db_path)`. The `_open_db` function is used in `compact.py` and may be re-used by `query.py` for read-write opens.

**Note on new catalogs:** `_open_db` skips the post-connect structural check when `db_path` does not yet exist or is empty (size == 0), because `_schema_meta` does not exist before the first `compact()` run. The check is guarded by `db_path.exists() and db_path.stat().st_size > 0`.

### Recovery from detected corruption

When `compact()` raises `CorruptDatabaseError`, the user's recovery path is to rebuild `bathos.db` from the cool-tier fragments, which are the authoritative source of truth. Add a `--force-rebuild` flag to `bth compact`:

```
bth compact --force-rebuild
```

When `--force-rebuild` is passed:
1. If `bathos.db` exists, rename it to `bathos.db.corrupt.<timestamp>` (preserving the corrupt file for forensics).
2. Proceed with compaction as normal against a fresh `bathos.db`.
3. Print a warning to stderr: `Corrupt bathos.db saved to <path>. Rebuilding from cool-tier fragments.`

This gives users a complete story: detect corruption via `CorruptDatabaseError`, recover via `bth compact --force-rebuild`.

### CorruptDatabaseError class definition

```python
class CorruptDatabaseError(RuntimeError):
    """Raised when bathos.db cannot be opened or fails a post-connect check.

    Attributes:
        db_path: Path to the database file that failed the check.
    """
    def __init__(self, message: str, db_path: Path | None = None) -> None:
        super().__init__(message)
        self.db_path = db_path
```

Place `CorruptDatabaseError` in `src/bathos/compact.py` at module level (after imports, before `CompactResult`). Export it from `src/bathos/__init__.py` so callers can catch it without importing internals.

### Risk mitigation: PRAGMA regression risk

**Risk:** Any implementation that includes `con.execute("PRAGMA integrity_check")` anywhere in `compact.py` will raise `CatalogException` for every `bth compact` call, not just on corrupt databases. This is a complete regression for all users.

**Mitigation:** The spec explicitly forbids using `PRAGMA integrity_check`. The `_open_db` function above is the only approved post-connect check. Code reviewers must grep for `integrity_check` and reject any PR that includes it.

### Test scenario

**test name:** `test_compact_raises_on_corrupt_db_header`

**setup:** Write a valid `bathos.db` in the catalog dir (by running `compact()` once). Then overwrite the first 512 bytes with null bytes to corrupt the file header. Call `compact(catalog_dir)` again.

**assertions:**
1. `compact()` raises `CorruptDatabaseError`.
2. The error message contains the path to `bathos.db`.
3. No new rows are written.

**rationale:** Header corruption is caught by `duckdb.IOException` at connect time, before any INSERT loop is entered.

**test name:** `test_compact_raises_on_missing_schema_meta`

**setup:** Create a `bathos.db` using `duckdb.connect()` directly and write a single row to a table named `runs` (but no `_schema_meta` table). Call `compact(catalog_dir)`.

**assertions:**
1. `compact()` raises `CorruptDatabaseError`.
2. The error message mentions `_schema_meta`.

**test name:** `test_compact_integrity_check_passes_on_valid_db`

**setup:** Run `compact()` once to create a valid `bathos.db`, then run `compact()` again.

**assertions:**
1. Second call does not raise.
2. `result.skipped` equals the number of runs from the first call.

**test name:** `test_compact_force_rebuild_recovers_from_corruption`

**setup:** Write a valid `bathos.db` then corrupt its header. Call `compact(catalog_dir, force_rebuild=True)` with one cool fragment present.

**assertions:**
1. Call does not raise.
2. `bathos.db` is now valid and contains the rows from the cool fragment.
3. A file named `bathos.db.corrupt.<timestamp>` exists in the catalog dir.

---

## Fix 3: Pre-migration fragment backup in migrate.py

### Problem

Lines 52-54 of `migrate.py` perform an in-place rewrite of a cool-tier Parquet fragment:

```python
tmp = frag.with_suffix(".tmp")
pq.write_table(tbl, tmp)
tmp.replace(frag)
```

If `pq.write_table` raises (disk full, I/O error, corrupt arrow data), `tmp` is left as a partial file and `frag` is still intact — that part is fine. But if `tmp.replace(frag)` raises after a partial write, `frag` may be left in an indeterminate state depending on the OS's `rename()` atomicity guarantee. More critically, there is no backup of the original: once `tmp.replace(frag)` succeeds, the original bytes are gone. If the new schema is later found to be wrong, there is no path back to the original data.

### Change

Before writing `tmp`, copy the original fragment to `frag.with_suffix(".bak")` using `shutil.copy2` (preserves mtime). On successful `tmp.replace(frag)`, unlink the `.bak` with `missing_ok=True`. On any exception anywhere in the write-or-rename sequence, restore `frag` from `.bak` and re-raise.

`bak.unlink()` must use `missing_ok=True` to handle the (rare) case where two concurrent migration processes both copied to `.bak`, the first already unlinked it, and the second attempts to unlink an already-absent file. Note that concurrent migration attempts are inherently unsafe at a higher level (see concurrent compaction safety in Fix 1 for the advisory lock pattern — a similar lock should guard migration). Using `missing_ok=True` prevents a spurious `FileNotFoundError` from masking a successful migration.

If `bak.replace(frag)` itself fails during error recovery (e.g., disk full while restoring), log a critical error and re-raise with a message naming both paths explicitly so the user can recover manually.

```python
import shutil

# Inside the migration loop, replacing lines 52-54:
bak = frag.with_suffix(".bak")
tmp_path = frag.with_suffix(".tmp")
shutil.copy2(frag, bak)
try:
    pq.write_table(tbl, tmp_path)
    tmp_path.replace(frag)
    bak.unlink(missing_ok=True)
except Exception as original_exc:
    # Remove partial tmp if it exists
    tmp_path.unlink(missing_ok=True)
    # Restore original from backup
    if bak.exists():
        try:
            bak.replace(frag)
        except Exception as restore_exc:
            logger.critical(
                "MANUAL RECOVERY REQUIRED: original fragment at %s could not be "
                "restored from backup at %s. Both paths may be in an indeterminate "
                "state. Error: %s",
                frag,
                bak,
                restore_exc,
            )
            raise RuntimeError(
                f"Original at {bak} could not be restored to {frag}; "
                f"manual recovery required."
            ) from restore_exc
    raise
```

### Risk mitigation: cross-filesystem rename atomicity

**Risk:** `bak.replace(frag)` uses `os.rename()` under the hood. On cross-filesystem mounts (e.g., network filesystem, separate volume), `rename()` is not atomic and may silently corrupt the file being restored if interrupted mid-copy. The spec's assumption of atomicity holds only for same-filesystem operations.

**Mitigation:** The `.bak` and `frag` paths are always siblings in the same directory (`frag.with_suffix(".bak")`). Because they share a parent directory, they will always be on the same filesystem in any standard deployment. Cross-filesystem scenarios only arise if the catalog directory itself straddles a mount boundary, which is not a supported configuration. Document in the `bth init` help text: "The catalog directory must reside on a single filesystem; cross-filesystem or network mount catalog directories are not supported."

On a clean run with no failures, no `.bak` files remain. Any `.bak` left on disk after `bth migrate` signals an interrupted migration and must be reported by `bth verify --tier cool`.

### Test scenario

**test name:** `test_migrate_fragment_backup_restored_on_write_failure`

**setup:** Create a cool fragment that needs migration (missing a column). Monkeypatch `pq.write_table` to raise `OSError("disk full")`. Call `migrate_catalog(catalog_dir, dry_run=False)`.

**assertions:**
1. `migrate_catalog()` raises `OSError("disk full")`.
2. The original `frag` file exists and is readable by `pq.read_table`.
3. No `.bak` file remains (restored and cleaned up — `bak.replace(frag)` succeeded).
4. No `.tmp` file remains.

**test name:** `test_migrate_fragment_no_bak_on_success`

**setup:** Create a cool fragment that needs migration. Call `migrate_catalog(catalog_dir, dry_run=False)`.

**assertions:**
1. No `.bak` files exist in the runs directory after completion.
2. No `.tmp` files exist.
3. The migrated fragment is readable and contains the expected columns.

---

## Fix 4: Archive SHA256 checksums in archive.py

### Problem

Lines 155-166 of `archive.py` write a `manifest.json` containing only `partition`, `rows`, and `size_bytes` per entry. There are no checksums, so it is impossible to verify after the fact whether the Parquet files in the archive match what was written. A corrupted or truncated archive Parquet cannot be distinguished from a valid one without re-querying the warm DB for row counts, and even then only count-equality (not content equality) is checked.

### Change

After each successful `pq.write_table` + `temp_file.rename(output_file)` at lines 125-126, compute the SHA256 of the written file and include it in the manifest entry. Add a `sha256` key to each entry in `manifest_entries`.

Initialize `file_sha256 = ""` before the `if not dry_run:` block so that the variable is always defined when referenced in `manifest_entries.append(...)`. Set it to the actual digest inside the block.

```python
import hashlib

def _sha256_file(path: Path) -> str:
    """Compute SHA256 hex digest of a file."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()

# Replace lines 125-126 and the file_size block:
file_size = 0
file_sha256 = ""  # Initialize before the conditional to avoid NameError in dry_run path
if not dry_run:
    temp_file.rename(output_file)
    file_size = output_file.stat().st_size
    file_sha256 = _sha256_file(output_file)
    total_size += file_size
```

Update the `manifest_entries.append(...)` call:

```python
manifest_entries.append(
    {
        "partition": f"project={project}/year={year}/month={month}",
        "rows": len(indices),
        "size_bytes": file_size,
        "sha256": file_sha256,
    }
)
```

Update the `manifest` dict (lines 158-165) to include a top-level `"schema_version": "2"` field so that `bth verify` can distinguish manifests with checksums from manifests without.

### Risk mitigation: SHA256 read-after-write performance

**Risk:** `_sha256_file` reads the written Parquet file a second time immediately after `temp_file.rename(output_file)`. For large catalogs archiving many partitions on slow or remote storage, this doubles the read I/O and may significantly increase archive duration.

**Mitigation:** SHA256 computation uses a 64 KB streaming read buffer, which is OS page-cache friendly. For a just-written file the pages are typically still in the buffer cache, making the second read essentially free on local storage. For network storage, the performance impact is real but acceptable given that archive is an infrequent operation. If profiling reveals unacceptable overhead, a future optimization can compute the SHA256 incrementally during `pq.write_table` using a hash-aware file wrapper; this is deferred to a follow-up sprint. The spec does not attempt this optimization now.

### Test scenario

**test name:** `test_archive_manifest_contains_sha256`

**setup:** Run `compact()` to populate `bathos.db` with at least two runs across two project slugs. Call `archive(catalog_dir)`.

**assertions:**
1. `manifest.json` exists and is valid JSON.
2. Each entry in `manifest["entries"]` has a `"sha256"` key whose value is a 64-character hex string.
3. For each entry, recompute the SHA256 of the corresponding Parquet file and assert it equals the recorded value.
4. `manifest["schema_version"] == "2"`.

**test name:** `test_archive_manifest_dry_run_no_sha256`

**setup:** Same compact setup. Call `archive(catalog_dir, dry_run=True)`.

**assertions:**
1. No Parquet files are written.
2. No `manifest.json` is written.
3. `result.runs_archived > 0` (would-be count is still reported).

**test name:** `test_archive_sha256_variable_defined_in_dry_run`

**setup:** Call `archive(catalog_dir, dry_run=True)` directly (no compact needed — no fragments required).

**assertions:**
1. No `NameError` is raised (regression guard: `file_sha256` is always initialized before the conditional).

---

## Fix 5: bth verify command

### Problem

There is no `bth verify` command or `verify.py` module anywhere in `src/bathos/`. Users have no way to check the integrity of any tier (cool, warm, archive) without writing ad hoc DuckDB or shell scripts. After fixes 1-4, integrity checking is mechanically possible but not exposed.

Additionally, the FastMCP server must mirror `bth verify` tool-for-tool per the project invariant in `CLAUDE.md`: "the FastMCP server mirrors CLI tool-for-tool". Without `mcp__bathos__verify`, users running bathos via MCP in agentic workflows cannot call verify.

### New file: src/bathos/verify.py

```python
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

import duckdb
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)


@dataclass
class VerifyResult:
    """Result of a verify operation for one or more tiers."""

    tier: str  # "cool" | "warm" | "archive" | "all"
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def verify_cool(catalog_dir: Path) -> VerifyResult:
    """Verify cool-tier Parquet fragments.

    Checks:
    - Each run_*.parquet is readable by pyarrow
    - No .bak files are present (signals interrupted migration)
    - No .tmp files are present (signals interrupted write)
    - Row count in each fragment is >= 1
    """
    ...


def verify_warm(catalog_dir: Path) -> VerifyResult:
    """Verify warm-tier DuckDB database.

    Checks:
    - bathos.db exists
    - duckdb.connect() succeeds without IOException (header check)
    - _schema_meta table is accessible (structural check)
    - If cool-tier fragments exist AND runs table is empty:
        warn "Cool fragments exist but runs table is empty — run bth compact"
    - If no cool-tier fragments exist AND runs table is empty:
        do NOT warn (empty runs table is normal for a new installation)
    """
    ...


def verify_archive(archive_root: Path) -> VerifyResult:
    """Verify cold-tier archive Parquet files against manifest.json checksums.

    Checks:
    - manifest.json exists and is valid JSON
    - manifest has schema_version >= "2" (contains sha256 checksums)
    - For each manifest entry: Parquet file exists, SHA256 matches, row count matches
    - Warns (does not error) on manifests with schema_version < "2"
    """
    ...


def verify_all(catalog_dir: Path, archive_root: Path | None = None) -> list[VerifyResult]:
    """Run verify_cool, verify_warm, and verify_archive; return all results."""
    ...
```

All four functions return `VerifyResult` instances. `verify_all` returns a list of three. Functions log each finding via `logger.warning` / `logger.error` in addition to populating `errors` / `warnings`.

### verify_warm: zero-row warning conditioned on cool fragments

The original spec warned unconditionally when `runs` table row count == 0. This produces a false-positive warning on any freshly initialized catalog. The corrected logic:

- If `runs_dir.glob('**/*.parquet')` is non-empty **and** `runs` table is empty → warn: `"Cool fragments exist but runs table is empty — run bth compact"`.
- If no fragments exist and `runs` table is empty → **do not warn** (valid state for a new installation).
- If `runs` table row count > 0 → no warning regardless of fragment count.

### cli.py wiring

Add the following `@app.command()` to `cli.py`, importing from `bathos.verify`:

```python
@app.command()
def verify(
    tier: str = typer.Option(
        "all",
        "--tier",
        "-t",
        help="Tier to verify: cool, warm, archive, or all",
    ),
    archive_dir: Path | None = typer.Option(
        None, "--archive-dir", "-d", help="Archive root (default: ~/.bth/archive)"
    ),
):
    """Verify catalog integrity across cool, warm, and archive tiers."""
    from bathos.verify import verify_all, verify_cool, verify_warm, verify_archive

    catalog_dir = _catalog_dir()
    archive_root = archive_dir or (Path.home() / ".bth" / "archive")

    if tier == "cool":
        results = [verify_cool(catalog_dir)]
    elif tier == "warm":
        results = [verify_warm(catalog_dir)]
    elif tier == "archive":
        results = [verify_archive(archive_root)]
    elif tier == "all":
        results = verify_all(catalog_dir, archive_root)
    else:
        typer.echo(f"Unknown tier: {tier!r}. Choose cool, warm, archive, or all.", err=True)
        raise typer.Exit(1)

    any_errors = False
    for result in results:
        status = "OK" if result.ok else "FAIL"
        color = "green" if result.ok else "red"
        typer.secho(f"[{result.tier}] {status}", fg=color)
        for w in result.warnings:
            typer.secho(f"  WARN  {w}", fg="yellow")
        for e in result.errors:
            typer.secho(f"  ERROR {e}", fg="red")
            any_errors = True

    if any_errors:
        raise typer.Exit(1)
```

### mcp.py wiring

Add the following MCP tool to `mcp.py`, mirroring the CLI command per the project's "mirrors CLI tool-for-tool" invariant:

```python
@mcp.tool()
def verify_catalog(
    tier: str = "all",
    archive_dir: str | None = None,
) -> list[dict]:
    """Verify catalog integrity across cool, warm, and archive tiers.

    Args:
        tier: Tier to verify — one of "cool", "warm", "archive", or "all".
        archive_dir: Path to archive root. Defaults to ~/.bth/archive.

    Returns:
        List of VerifyResult dicts with keys: tier, ok, errors, warnings, stats.
        If ok=False for any result, raises McpError with the first error message.

    Raises:
        McpError: When ok=False, structured as {"tier": ..., "errors": [...]}.
    """
    from bathos.verify import verify_all, verify_cool, verify_warm, verify_archive
    from bathos.compact import CorruptDatabaseError
    from fastmcp.exceptions import McpError

    catalog_dir = _catalog_dir()
    archive_root = Path(archive_dir) if archive_dir else (Path.home() / ".bth" / "archive")

    if tier == "cool":
        results = [verify_cool(catalog_dir)]
    elif tier == "warm":
        results = [verify_warm(catalog_dir)]
    elif tier == "archive":
        results = [verify_archive(archive_root)]
    elif tier == "all":
        results = verify_all(catalog_dir, archive_root)
    else:
        raise McpError(f"Unknown tier: {tier!r}. Choose cool, warm, archive, or all.")

    out = [
        {"tier": r.tier, "ok": r.ok, "errors": r.errors, "warnings": r.warnings, "stats": r.stats}
        for r in results
    ]

    failed = [r for r in results if not r.ok]
    if failed:
        raise McpError(
            f"Verify failed for tier(s): {[r.tier for r in failed]}",
            data={"results": out},
        )

    return out
```

Add `mcp.py` to the **Files Changed** list.

### Test file: tests/test_verify.py

The test file must cover the following scenarios:

### Test scenarios

**test name:** `test_verify_cool_clean`

**setup:** Write three valid cool fragments. Call `verify_cool(catalog_dir)`.

**assertions:** `result.ok is True`, `result.errors == []`.

**test name:** `test_verify_cool_detects_bak_files`

**setup:** Write a valid cool fragment, then create a `run_abc123.bak` file in the same directory. Call `verify_cool(catalog_dir)`.

**assertions:** `result.ok is False`, exactly one entry in `result.errors` containing "bak".

**test name:** `test_verify_cool_detects_unreadable_fragment`

**setup:** Write a valid cool fragment, then overwrite its bytes with `b"not parquet"`. Call `verify_cool(catalog_dir)`.

**assertions:** `result.ok is False`, at least one error entry referencing the corrupt file.

**test name:** `test_verify_warm_clean`

**setup:** Run `compact()` to create a valid `bathos.db`. Call `verify_warm(catalog_dir)`.

**assertions:** `result.ok is True`, `result.errors == []`.

**test name:** `test_verify_warm_detects_missing_db`

**setup:** Catalog dir with no `bathos.db`. Call `verify_warm(catalog_dir)`.

**assertions:** `result.ok is False`, error mentions "bathos.db" or "not found".

**test name:** `test_verify_warm_detects_corrupt_db`

**setup:** Create `bathos.db` with valid content via `compact()`, then overwrite first 512 bytes with null bytes. Call `verify_warm(catalog_dir)`.

**assertions:** `result.ok is False`, error mentions "integrity" or "IOException" or "corrupt".

**test name:** `test_verify_warm_no_false_positive_on_empty_new_catalog`

**setup:** Create a catalog dir with no cool fragments and no `bathos.db`. Call `verify_warm(catalog_dir)`.

**assertions:** `result.ok is True` (new installation; empty runs table is not a warning condition), `result.warnings == []`.

**test name:** `test_verify_warm_warns_when_fragments_exist_but_db_empty`

**setup:** Write two valid cool fragments to the catalog dir but do not run `compact()`. Create a minimal `bathos.db` with an empty `runs` table and a populated `_schema_meta`. Call `verify_warm(catalog_dir)`.

**assertions:** `result.ok is True` (warn, not error), at least one entry in `result.warnings` mentioning "compact" or "fragments".

**test name:** `test_verify_archive_clean`

**setup:** Run `compact()` and `archive()` (with Fix 4 applied, so manifest has SHA256). Call `verify_archive(archive_root)`.

**assertions:** `result.ok is True`, `result.errors == []`.

**test name:** `test_verify_archive_sha256_mismatch`

**setup:** Run `compact()` and `archive()`. Flip one byte inside one of the Parquet files. Call `verify_archive(archive_root)`.

**assertions:** `result.ok is False`, error mentions "sha256" and the affected partition key.

**test name:** `test_verify_archive_missing_parquet`

**setup:** Run `compact()` and `archive()`. Delete one of the written Parquet files. Call `verify_archive(archive_root)`.

**assertions:** `result.ok is False`, error mentions "missing" and the affected partition key.

**test name:** `test_verify_archive_old_manifest_warns`

**setup:** Create a `manifest.json` without a `sha256` key in each entry (schema_version < "2"). Call `verify_archive(archive_root)`.

**assertions:** `result.ok is True` (warn, not error), `result.warnings` contains at least one entry mentioning "checksum" or "schema_version".

---

## Fix 6: Sync truncation detection in sync.py

### Problem

At line 92, the rsync command includes `--ignore-existing`. This flag tells rsync to skip any destination file that already exists, regardless of whether the destination copy is complete or valid. If a previous sync was interrupted after rsync created the destination file but before it finished writing it, subsequent syncs will silently skip the truncated file. The Parquet fragment on the destination will be unreadable or contain fewer rows than the source, and no error is reported.

The current code uses `--info=progress2` to parse byte counts but does not compare source and destination file sizes post-transfer, so truncation is invisible at the sync layer.

### Change

**Mechanism:** After a pull completes, perform a post-pull scan of all `.parquet` files in the local destination directory using `pq.read_metadata(path)`. Any file that raises an exception is flagged as a truncated candidate. This scan is the only mechanism for truncation detection — the spec does not add `--itemize-changes` parsing or attempt to compare per-file sizes via rsync output, because `--itemize-changes` output parsing is fragile and the post-pull scan is sufficient.

The scan reads metadata headers only (not full file contents), so for most valid files it completes in microseconds per file. For large catalogs with thousands of fragments the cumulative overhead may be significant (see risk mitigation below).

```python
@dataclass
class SyncResult:
    transferred: int
    duration_s: float
    remote: str
    filtered: int = 0
    truncated_candidates: list[str] = field(default_factory=list)  # NEW
```

After a pull completes (when `pull=True`), scan the destination directory, limited to files touched during this pull using mtime:

```python
import time

pull_start_time = time.time()
# ... rsync subprocess ...
pull_end_time = time.time()

if pull:
    import pyarrow.parquet as pq
    scan_window_start = pull_start_time - 5.0  # 5s buffer for clock skew
    truncated = []
    for parquet_file in Path(dst.rstrip("/")).rglob("run_*.parquet"):
        if parquet_file.stat().st_mtime < scan_window_start:
            continue  # skip files that predate this pull
        try:
            pq.read_metadata(str(parquet_file))
        except Exception:
            truncated.append(str(parquet_file))
            logger.warning(
                "Truncated or corrupt Parquet after sync: %s", parquet_file
            )
            event("sync.truncated_fragment", path=str(parquet_file))
    result = SyncResult(
        transferred=transferred,
        duration_s=duration_s,
        remote=remote_name,
        filtered=filtered,
        truncated_candidates=truncated,
    )
```

In `cli.py`, after `sync_catalog()` returns, check `result.truncated_candidates` and emit a warning to stderr for each, with explicit repair instructions:

```python
for path in result.truncated_candidates:
    typer.secho(
        f"  WARN truncated fragment after pull: {path}\n"
        f"       To repair: delete this file and re-run bth sync --pull",
        fg="yellow",
        err=True,
    )
```

### Repair procedure

After `bth sync --pull` reports truncated candidates, the user must:
1. Delete each truncated file listed in the warning.
2. Re-run `bth sync --pull`.

Because rsync uses `--ignore-existing`, the truncated file must be deleted first so rsync will re-download it. This procedure must appear in `bth sync --help` text and in the warning message above. A future `--repair` flag (not in scope for this sprint) would automate steps 1-2.

### Risk mitigation: post-pull scan latency for large catalogs

**Risk:** `pq.read_metadata` is called on Parquet files in the destination directory after every pull. For a large catalog with thousands of fragments, scanning all of them adds latency proportional to the total fragment count rather than the number of transferred files.

**Mitigation:** Limit the scan to files whose `st_mtime` falls within the pull window (from `pull_start_time - 5s` to pull completion). This reduces the scan to O(files transferred) rather than O(total fragments). The implementation is shown in the code above. The 5-second buffer accommodates filesystem clock skew between local and remote.

### Test scenario

**test name:** `test_sync_truncated_fragment_detected_after_pull`

**setup:** Create a valid cool fragment in a fake "remote" directory (local path). Write a truncated (corrupt) version of the same filename in the local destination directory. Set the corrupt file's mtime to `time.time()` (within the pull window). Mock the rsync subprocess to return exit code 0. Call `sync_catalog(remote, config, catalog_dir, pull=True)`.

**assertions:**
1. `result.truncated_candidates` contains the path to the corrupt local fragment.
2. A `sync.truncated_fragment` telemetry event was emitted.
3. The function does not raise (warning, not error).

**test name:** `test_sync_result_has_truncated_candidates_field`

**setup:** Mock rsync to succeed with no output. Call `sync_catalog(...)` with `pull=True` and a clean destination directory containing only valid Parquet files with recent mtime.

**assertions:** `result.truncated_candidates == []`.

---

## Acceptance Criteria

1. `bth compact` on a catalog with N cool fragments completes atomically: either all N rows are inserted into `bathos.db` or none are. Verified by: monkeypatching the second INSERT to raise and confirming zero rows in `bathos.db` afterward; a subsequent call with the patch removed returns `ingested=N, skipped=0`.
2. `bth compact` raises `CorruptDatabaseError` (not `CatalogException`, not any other exception) when `bathos.db` header bytes have been overwritten with null bytes, and the error message includes the database path string.
3. `bth compact` raises `CorruptDatabaseError` when `bathos.db` connects successfully but `_schema_meta` is inaccessible, and the error message includes `"_schema_meta"`.
4. `bth compact --force-rebuild` succeeds on a corrupt `bathos.db`, produces a valid `bathos.db` containing all cool-tier rows, and leaves a `bathos.db.corrupt.<timestamp>` file in the catalog dir.
5. `CorruptDatabaseError` is importable from `bathos` at the top level: `from bathos import CorruptDatabaseError`.
6. `bth compact` raises `CompactionLockedError` if `.bth_compact.lock` is present and the PID in the lock file is an active process.
7. `bth compact` clears a stale `.bth_compact.lock` (PID no longer active) and proceeds normally.
8. No code path in `compact.py` calls `con.execute("PRAGMA integrity_check")` — confirmed by grep.
9. `bth migrate` leaves no `.bak` or `.tmp` files after a successful run.
10. `bth migrate` leaves the original fragment intact (restores `.bak` to `frag`) when `pq.write_table` raises, with no `.bak` or `.tmp` files remaining.
11. `bth archive` writes a `manifest.json` where every entry has a `"sha256"` key containing a 64-character hex string, and `manifest["schema_version"] == "2"`.
12. `bth archive --dry-run` produces no Parquet files and no `manifest.json` on disk. The `file_sha256` variable is always defined before use — no `NameError` is raised in dry-run mode (regression guard).
13. `bth verify --tier warm` exits with code 0 on a freshly compacted valid `bathos.db`.
14. `bth verify --tier warm` exits with code 1 when `bathos.db` is corrupt (IOException at connect), and prints an ERROR line to stdout. It does not call `PRAGMA integrity_check`.
15. `bth verify --tier warm` exits with code 0 and prints no warnings when `bathos.db` does not exist and no cool fragments are present (new installation).
16. `bth verify --tier warm` exits with code 0 and prints at least one WARN line mentioning "compact" when cool fragments exist but `runs` table is empty.
17. `bth verify --tier cool` exits with code 1 and prints an ERROR when a `.bak` file is present in the cool-tier runs directory.
18. `bth verify --tier archive` exits with code 0 when all manifest SHA256 checksums match the actual Parquet files.
19. `bth verify --tier archive` exits with code 1 and prints an ERROR mentioning "sha256" when any archive Parquet has been modified after the manifest was written.
20. `bth verify --tier archive` on a pre-Fix-4 manifest (no `sha256` keys) exits with code 0 and prints at least one WARN line mentioning "checksum" or "schema_version".
21. `mcp__bathos__verify` is present in `mcp.py`, accepts `tier` (str) and `archive_dir` (str | None), returns a list of result dicts with keys `{tier, ok, errors, warnings, stats}`, and raises `McpError` when any tier fails.
22. `bth sync --pull` populates `SyncResult.truncated_candidates` with the path of any Parquet fragment that fails `pq.read_metadata` after the pull, limited to files modified within the pull window.
23. All 6 fixes have corresponding test coverage with at least one failure-path test each; `uv run pytest tests/` passes with no new test failures and no new skips.

## Files Changed

- `src/bathos/compact.py` — transaction wrap (Fix 1), `CompactionLockedError` + advisory lock (Fix 1), `CorruptDatabaseError` + `_open_db` (Fix 2), `--force-rebuild` logic (Fix 2)
- `src/bathos/__init__.py` — export `CorruptDatabaseError`, `CompactionLockedError`
- `src/bathos/migrate.py` — `.bak` backup/restore pattern with `missing_ok=True` and critical-log on restore failure (Fix 3)
- `src/bathos/archive.py` — `file_sha256 = ""` initialization before conditional, SHA256 per partition, manifest schema_version (Fix 4)
- `src/bathos/verify.py` — new file: `VerifyResult`, `verify_cool`, `verify_warm`, `verify_archive`, `verify_all` (Fix 5)
- `src/bathos/cli.py` — `verify` command wiring (Fix 5), `compact --force-rebuild` flag (Fix 2), truncation warning with repair instructions (Fix 6)
- `src/bathos/mcp.py` — `verify_catalog` MCP tool mirroring `bth verify` CLI (Fix 5)
- `src/bathos/sync.py` — `truncated_candidates` field on `SyncResult`, post-pull scan with mtime window (Fix 6)
- `tests/test_verify.py` — new file: all verify test scenarios (Fix 5)
- `tests/test_compact.py` — transaction rollback, lock, force-rebuild, and integrity check tests (Fixes 1, 2)
- `tests/test_migrate.py` — backup/restore tests (Fix 3)
- `tests/test_archive.py` — SHA256 manifest tests, NameError regression guard (Fix 4)
- `tests/test_sync.py` — truncation detection tests (Fix 6)
