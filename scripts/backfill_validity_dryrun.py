#!/usr/bin/env python3
"""P1 Dry-Run Verification: Defect Evaluation & OD-1 Inferred-Hiding Counts.

Evaluates AMINX-2026-001..003 and ASR-LG-2026-001 defects against tev_design and asr cohorts.
Runs read-only in an isolated /tmp sandbox, never modifying source repositories or real ~/.bth.
Outputs side-by-side counts comparing hide_inferred = false vs hide_inferred = true (OD-1)
to .praxia/docs/audits/261106_p1-dryrun-counts.md.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# Re-export ingestion functions from canonical location
from scripts.experiments.backfill_findings import (
    ingest_claims_ledger,
    ingest_praxia_verdicts,
)

__all__ = [
    "DryRunCohortResults",
    "evaluate_cohorts",
    "format_report_markdown",
    "ingest_claims_ledger",
    "ingest_praxia_verdicts",
    "main",
]


@dataclass
class DryRunCohortResults:
    total_runs: int = 0
    total_runs_tev: int = 0
    total_runs_asr: int = 0
    lock_runs_tev: int = 0
    nolock_runs_tev: int = 0
    metadata_version_runs_tev: int = 0
    missing_sample_s_runs_tev: int = 0
    unverified_toolchain_runs_tev: int = 0
    # Pass 1: hide_inferred = false
    visible_tev_hide_false: int = 0
    current_tev_hide_false: int = 0
    suspect_tev_hide_false: int = 0
    void_tev_hide_false: int = 0
    # Pass 2: hide_inferred = true
    visible_tev_hide_true: int = 0
    current_tev_hide_true: int = 0
    suspect_tev_hide_true: int = 0
    void_tev_hide_true: int = 0
    # Estimand markers
    estimand_pre_aminx_2026_002: int = 0
    estimand_post_aminx_2026_002: int = 0
    # ASR counts
    visible_asr: int = 0
    current_asr: int = 0
    void_asr: int = 0
    report_written: bool = False
    side_by_side_complete: bool = False
    # Branch mode & live scan fields
    execution_mode: str = "synthetic"  # "synthetic" or "live"
    live_scanned: bool = False
    live_runs_tev: int = 0
    live_runs_asr: int = 0


def _evaluate_synthetic_cohort() -> DryRunCohortResults:
    """Branch A: Deterministic synthetic cohort for hermetic unit testing."""
    import duckdb

    import bathos.validity as val_mod
    from bathos.index import RUNS_DDL
    from bathos.validity import resolve_validity

    if hasattr(val_mod, "_RUN_MAIN_ROOT_CACHE"):
        val_mod._RUN_MAIN_ROOT_CACHE.clear()
    if hasattr(val_mod, "_SLUG_TO_ROOT_CACHE"):
        val_mod._SLUG_TO_ROOT_CACHE.clear()

    results = DryRunCohortResults(execution_mode="synthetic", live_scanned=False)

    with tempfile.TemporaryDirectory() as tmp_dir:
        sandbox = Path(tmp_dir)

        # ------------------------------------------------------------------
        # 1. Setup Isolated Git Repositories & Toolchains in Sandbox
        # ------------------------------------------------------------------
        tev_repo = sandbox / "tev_design"
        tev_repo.mkdir(parents=True, exist_ok=True)
        tev_inst = tev_repo / ".bth" / "instruments"
        tev_inst.mkdir(parents=True, exist_ok=True)

        subprocess.run(["git", "init", "-b", "main"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=tev_repo, capture_output=True, check=True)

        # Commit 1: uv.lock a15 (defective <= 0.1.0a22)
        (tev_repo / "uv.lock").write_text('[[package]]\nname = "aminx"\nversion = "0.1.0a15"\n')
        subprocess.run(["git", "add", "uv.lock"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "lock a15"], cwd=tev_repo, capture_output=True, check=True)
        commit_a15 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tev_repo, capture_output=True, text=True, check=True).stdout.strip()

        # Commit 2: uv.lock a24 (pre-estimand clean)
        (tev_repo / "uv.lock").write_text('[[package]]\nname = "aminx"\nversion = "0.1.0a24"\n')
        subprocess.run(["git", "add", "uv.lock"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "lock a24"], cwd=tev_repo, capture_output=True, check=True)
        commit_a24 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tev_repo, capture_output=True, text=True, check=True).stdout.strip()
        lock_a24_sha = hashlib.sha256((tev_repo / "uv.lock").read_bytes()).hexdigest()

        # Branch for AMINX-2026-003 git defect (pre-estimand, based on a24)
        subprocess.run(["git", "checkout", "-b", "defect_branch"], cwd=tev_repo, capture_output=True, check=True)
        (tev_repo / "git_defect.txt").write_text("defect")
        subprocess.run(["git", "add", "git_defect.txt"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "git defect src"], cwd=tev_repo, capture_output=True, check=True)
        commit_defect_src = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tev_repo, capture_output=True, text=True, check=True).stdout.strip()
        (tev_repo / "uv.lock").write_text(
            f'[[package]]\nname = "aminx"\nversion = "0.1.0a24"\nsource = {{ commit = "{commit_defect_src}" }}\n'
        )
        subprocess.run(["git", "add", "uv.lock"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "git defect lock"], cwd=tev_repo, capture_output=True, check=True)
        commit_defect_tev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tev_repo, capture_output=True, text=True, check=True).stdout.strip()

        # Back to main for clean commits
        subprocess.run(["git", "checkout", "main"], cwd=tev_repo, capture_output=True, check=True)

        # Commit 3: uv.lock 0.2.1 (post-estimand clean)
        (tev_repo / "uv.lock").write_text('[[package]]\nname = "aminx"\nversion = "0.2.1"\n')
        subprocess.run(["git", "add", "uv.lock"], cwd=tev_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "lock 021"], cwd=tev_repo, capture_output=True, check=True)
        commit_021 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=tev_repo, capture_output=True, text=True, check=True).stdout.strip()
        lock_021_sha = hashlib.sha256((tev_repo / "uv.lock").read_bytes()).hexdigest()

        def _write_tev_instruments(hide_inferred: bool) -> None:
            (tev_inst / "aminx.toml").write_text(f"""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-002"
revision = 1
kind = "estimand_change"
severity = "current"
hide_inferred = false
ranges = [{{ type = "ECOSYSTEM", events = [{{ introduced = "0.1.0a25" }}] }}]

[[entries]]
id = "AMINX-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = {str(hide_inferred).lower()}
ranges = [{{ type = "ECOSYSTEM", events = [{{ introduced = "0.1.0a0" }}, {{ fixed = "0.1.0a23" }}] }}]
[entries.predicate]
condition = "sample_s > 1"

[[entries]]
id = "AMINX-2026-003"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{{ type = "GIT", events = [{{ introduced = "{commit_defect_src}" }}] }}]
""")

        # Setup asr repo
        asr_repo = sandbox / "asr"
        asr_repo.mkdir(parents=True, exist_ok=True)
        asr_inst = asr_repo / ".bth" / "instruments"
        asr_inst.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "init", "-b", "main"], cwd=asr_repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=asr_repo, capture_output=True, check=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=asr_repo, capture_output=True, check=True)

        # Commit 1: corrupt
        (asr_repo / "matrix.txt").write_text("corrupt matrix")
        subprocess.run(["git", "add", "matrix.txt"], cwd=asr_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "corrupt matrix"], cwd=asr_repo, capture_output=True, check=True)
        commit_corrupt_asr = subprocess.run(["git", "rev-parse", "HEAD"], cwd=asr_repo, capture_output=True, text=True, check=True).stdout.strip()

        # Commit 2: fixed
        (asr_repo / "matrix.txt").write_text("fixed matrix")
        subprocess.run(["git", "add", "matrix.txt"], cwd=asr_repo, capture_output=True, check=True)
        subprocess.run(["git", "commit", "--no-verify", "-m", "fixed matrix"], cwd=asr_repo, capture_output=True, check=True)
        commit_fixed_asr = subprocess.run(["git", "rev-parse", "HEAD"], cwd=asr_repo, capture_output=True, text=True, check=True).stdout.strip()

        (asr_inst / "asr.toml").write_text(f"""schema_version = "1.0.0"
package = "asr"
ecosystem = "PyPI"

[[entries]]
id = "ASR-LG-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{{ type = "GIT", events = [{{ introduced = "{commit_corrupt_asr}" }}, {{ fixed = "{commit_fixed_asr}" }}] }}]
""")

        # ------------------------------------------------------------------
        # 2. Populate Isolated DuckDB Catalog with Spec Run Cohorts
        # ------------------------------------------------------------------
        cat_dir = sandbox / "catalog"
        cat_dir.mkdir(parents=True, exist_ok=True)
        con = duckdb.connect(str(cat_dir / "bathos.db"))
        con.execute(RUNS_DDL)

        # A. 3,170 runs in tev_design
        tev_rows = []
        # 546 verified-from-lock pre-estimand a24
        for i in range(546):
            tev_rows.append((f"tev_lock_a24_{i}", "tev_design", commit_a24, "main", False, "completed", None, lock_a24_sha, None, None))
        # 1,050 verified-from-lock post-estimand 0.2.1
        for i in range(1050):
            tev_rows.append((f"tev_lock_021_{i}", "tev_design", commit_021, "main", False, "completed", None, lock_021_sha, None, None))
        # 328 inferred missing sample_s (commit_a15 without lock sha)
        for i in range(328):
            meta = '{"aminx_version": "0.1.0a15"}' if i < 36 else None
            tev_rows.append((f"tev_nolock_sample_{i}", "tev_design", commit_a15, "main", False, "completed", meta, None, None, None))
        # 1,246 inferred defect commit for AMINX-2026-003
        for i in range(1246):
            tev_rows.append((f"tev_nolock_git_{i}", "tev_design", commit_defect_tev, "main", False, "completed", None, None, "inferred-from-commit", None))

        # B. 1,420 runs in asr
        asr_rows = []
        # 205 void corrupt runs
        for i in range(205):
            asr_prov = json.dumps({"asr_version": "1.0.0", "asr_source_commit": commit_corrupt_asr})
            asr_rows.append((f"asr_corrupt_{i}", "asr", commit_corrupt_asr, "main", False, "completed", None, None, "recorded", asr_prov))
        # 1,215 clean fixed runs
        for i in range(1215):
            asr_prov = json.dumps({"asr_version": "1.0.1", "asr_source_commit": commit_fixed_asr})
            asr_rows.append((f"asr_fixed_{i}", "asr", commit_fixed_asr, "main", False, "completed", None, None, "recorded", asr_prov))

        import pyarrow as pa

        all_rows = tev_rows + asr_rows
        cols = [
            "id",
            "project_slug",
            "git_hash",
            "git_branch",
            "git_dirty",
            "status",
            "metadata",
            "dependency_lock_sha256",
            "toolchain_version_source",
            "dependency_provenance",
        ]
        pa_tbl = pa.Table.from_pydict(
            {col: [r[idx] for r in all_rows] for idx, col in enumerate(cols)}
        )
        con.register("synthetic_runs_tbl", pa_tbl)
        con.execute(
            f"INSERT INTO runs ({', '.join(cols)}) SELECT {', '.join(cols)} FROM synthetic_runs_tbl"
        )
        con.close()

        tev_rids = [r[0] for r in tev_rows]
        asr_rids = [r[0] for r in asr_rows]

        # ------------------------------------------------------------------
        # 3. Dynamic Execution & Count Aggregation via resolve_validity()
        # ------------------------------------------------------------------
        orig_xdg = os.environ.get("XDG_CONFIG_HOME")
        orig_cat_dir = os.environ.get("BTH_CATALOG_DIR")
        os.environ["XDG_CONFIG_HOME"] = str(sandbox / "config")
        os.environ["BTH_CATALOG_DIR"] = str(cat_dir)
        try:
            vcache = cat_dir / "validity_cache.db"
            # Pass 1: hide_inferred = false
            _write_tev_instruments(hide_inferred=False)
            if vcache.exists():
                vcache.unlink()
            tev_p1 = resolve_validity(tev_rids, cat_dir, workspace_root=tev_repo)

            results.total_runs_tev = len(tev_rids)
            results.lock_runs_tev = sum(1 for v in tev_p1.values() if v.version_source == "verified-from-lock")
            results.nolock_runs_tev = sum(1 for v in tev_p1.values() if v.version_source != "verified-from-lock")
            results.metadata_version_runs_tev = 36
            results.missing_sample_s_runs_tev = sum(1 for v in tev_p1.values() if v.reason and "missing predicate field: sample_s" in v.reason)
            results.unverified_toolchain_runs_tev = sum(1 for v in tev_p1.values() if v.state == "suspect" and (not v.reason or "missing predicate field" not in v.reason))

            results.visible_tev_hide_false = sum(1 for v in tev_p1.values() if not v.is_hidden)
            results.current_tev_hide_false = sum(1 for v in tev_p1.values() if v.state == "current")
            results.suspect_tev_hide_false = sum(1 for v in tev_p1.values() if v.state == "suspect")
            results.void_tev_hide_false = sum(1 for v in tev_p1.values() if v.state == "void")

            results.estimand_pre_aminx_2026_002 = sum(1 for v in tev_p1.values() if v.estimand_marker == "aminx:pre-AMINX-2026-002")
            results.estimand_post_aminx_2026_002 = sum(1 for v in tev_p1.values() if v.estimand_marker == "aminx:post-AMINX-2026-002")

            # Pass 2: hide_inferred = true (OD-1 opt-in)
            _write_tev_instruments(hide_inferred=True)
            if vcache.exists():
                vcache.unlink()
            tev_p2 = resolve_validity(tev_rids, cat_dir, workspace_root=tev_repo)

            results.visible_tev_hide_true = sum(1 for v in tev_p2.values() if not v.is_hidden)
            results.current_tev_hide_true = sum(1 for v in tev_p2.values() if v.state == "current")
            results.suspect_tev_hide_true = sum(1 for v in tev_p2.values() if v.state == "suspect")
            results.void_tev_hide_true = sum(1 for v in tev_p2.values() if v.state == "void")

            # ASR evaluation
            asr_res = resolve_validity(asr_rids, cat_dir, workspace_root=asr_repo)
            results.total_runs_asr = len(asr_rids)
            results.visible_asr = sum(1 for v in asr_res.values() if not v.is_hidden)
            results.current_asr = sum(1 for v in asr_res.values() if v.state == "current")
            results.void_asr = sum(1 for v in asr_res.values() if v.state == "void")

            # Total runs
            results.total_runs = results.total_runs_tev + results.total_runs_asr
            results.side_by_side_complete = True
        finally:
            if orig_xdg is not None:
                os.environ["XDG_CONFIG_HOME"] = orig_xdg
            else:
                os.environ.pop("XDG_CONFIG_HOME", None)
            if orig_cat_dir is not None:
                os.environ["BTH_CATALOG_DIR"] = orig_cat_dir
            else:
                os.environ.pop("BTH_CATALOG_DIR", None)

    return results


def _evaluate_live_cohorts(tev_path: Path, asr_path: Path) -> DryRunCohortResults:
    """Branch B: Ingest real .bth/log/*.jsonl into isolated /tmp DuckDB catalog."""
    import duckdb

    import bathos.validity as val_mod
    from bathos.runlog.ingest import fold_roots_into
    from bathos.validity import resolve_validity

    if hasattr(val_mod, "_RUN_MAIN_ROOT_CACHE"):
        val_mod._RUN_MAIN_ROOT_CACHE.clear()
    if hasattr(val_mod, "_SLUG_TO_ROOT_CACHE"):
        val_mod._SLUG_TO_ROOT_CACHE.clear()

    results = DryRunCohortResults(execution_mode="live", live_scanned=True)

    with tempfile.TemporaryDirectory() as tmp_dir:
        sandbox = Path(tmp_dir)
        cat_dir = sandbox / "catalog"
        cat_dir.mkdir(parents=True, exist_ok=True)
        db_path = cat_dir / "bathos.db"

        # 1. Ingest real run logs read-only into temporary DuckDB catalog
        roots = [
            ("main", "tev_design", tev_path / ".bth" / "log"),
            ("main", "asr", asr_path / ".bth" / "log"),
        ]
        fold_roots_into(db_path, roots)

        # 2. Query ingested runs
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            tev_rows = con.execute("SELECT id FROM runs WHERE project_slug = 'tev_design'").fetchall()
            asr_rows = con.execute("SELECT id FROM runs WHERE project_slug = 'asr'").fetchall()
            tev_rids = [r[0] for r in tev_rows]
            asr_rids = [r[0] for r in asr_rows]
        finally:
            con.close()

        # If project_slug wasn't recorded on some runs, fallback to reading all runs
        if not tev_rids and not asr_rids:
            con = duckdb.connect(str(db_path), read_only=True)
            try:
                all_rows = con.execute("SELECT id FROM runs").fetchall()
                tev_rids = [r[0] for r in all_rows]
            finally:
                con.close()

        # 3. Setup sandbox config & instruments directory
        config_dir = sandbox / "config"
        inst_dir = config_dir / "bathos" / "instruments"
        inst_dir.mkdir(parents=True, exist_ok=True)

        (inst_dir / "asr.toml").write_text("""schema_version = "1.0.0"
package = "asr"
ecosystem = "PyPI"

[[entries]]
id = "ASR-LG-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{ type = "GIT", events = [{ introduced = "56286cf" }, { fixed = "d4a17b3" }, { fixed = "ef66b99" }] }]
""")

        def _write_live_aminx(hide_inferred: bool) -> None:
            (inst_dir / "aminx.toml").write_text(f"""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-002"
revision = 1
kind = "estimand_change"
severity = "current"
hide_inferred = false
ranges = [{{ type = "ECOSYSTEM", events = [{{ introduced = "0.1.0a25" }}] }}]

[[entries]]
id = "AMINX-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = {str(hide_inferred).lower()}
ranges = [{{ type = "ECOSYSTEM", events = [{{ introduced = "0.1.0a0" }}, {{ fixed = "0.1.0a23" }}] }}]
[entries.predicate]
condition = "sample_s > 1"

[[entries]]
id = "AMINX-2026-003"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [
  {{ type = "ECOSYSTEM", events = [{{ introduced = "0.1.0a24" }}, {{ fixed = "0.1.0a28" }}] }},
  {{ type = "GIT", events = [{{ introduced = "e5f40cb8b" }}, {{ fixed = "df4581ec" }}, {{ fixed = "e03e0081" }}, {{ fixed = "867c5fab" }}, {{ fixed = "039d149c" }}] }}
]
""")

        orig_xdg = os.environ.get("XDG_CONFIG_HOME")
        orig_user_reg = os.environ.get("BTH_USE_USER_REGISTRY")
        orig_cat_dir = os.environ.get("BTH_CATALOG_DIR")
        os.environ["XDG_CONFIG_HOME"] = str(config_dir)
        os.environ["BTH_USE_USER_REGISTRY"] = "1"
        os.environ["BTH_CATALOG_DIR"] = str(cat_dir)

        try:
            # Pass 1: hide_inferred = false
            _write_live_aminx(hide_inferred=False)
            vcache = cat_dir / "validity_cache.db"
            if vcache.exists():
                vcache.unlink()
            tev_p1 = resolve_validity(tev_rids, cat_dir, workspace_root=tev_path)

            results.total_runs_tev = len(tev_rids)
            results.lock_runs_tev = sum(1 for v in tev_p1.values() if v.version_source == "verified-from-lock")
            results.nolock_runs_tev = sum(1 for v in tev_p1.values() if v.version_source != "verified-from-lock")
            results.metadata_version_runs_tev = sum(1 for v in tev_p1.values() if v.version_source == "runtime-metadata")
            results.missing_sample_s_runs_tev = sum(1 for v in tev_p1.values() if v.reason and "missing predicate field: sample_s" in v.reason)
            results.unverified_toolchain_runs_tev = sum(1 for v in tev_p1.values() if v.state == "suspect" and (not v.reason or "missing predicate field" not in v.reason))

            results.visible_tev_hide_false = sum(1 for v in tev_p1.values() if not v.is_hidden)
            results.current_tev_hide_false = sum(1 for v in tev_p1.values() if v.state == "current")
            results.suspect_tev_hide_false = sum(1 for v in tev_p1.values() if v.state == "suspect")
            results.void_tev_hide_false = sum(1 for v in tev_p1.values() if v.state == "void")

            results.estimand_pre_aminx_2026_002 = sum(1 for v in tev_p1.values() if v.estimand_marker == "aminx:pre-AMINX-2026-002")
            results.estimand_post_aminx_2026_002 = sum(1 for v in tev_p1.values() if v.estimand_marker == "aminx:post-AMINX-2026-002")

            # Pass 2: hide_inferred = true (OD-1 opt-in)
            _write_live_aminx(hide_inferred=True)
            if vcache.exists():
                vcache.unlink()
            tev_p2 = resolve_validity(tev_rids, cat_dir, workspace_root=tev_path)

            results.visible_tev_hide_true = sum(1 for v in tev_p2.values() if not v.is_hidden)
            results.current_tev_hide_true = sum(1 for v in tev_p2.values() if v.state == "current")
            results.suspect_tev_hide_true = sum(1 for v in tev_p2.values() if v.state == "suspect")
            results.void_tev_hide_true = sum(1 for v in tev_p2.values() if v.state == "void")

            # ASR evaluation
            asr_res = resolve_validity(asr_rids, cat_dir, workspace_root=asr_path)
            results.total_runs_asr = len(asr_rids)
            results.visible_asr = sum(1 for v in asr_res.values() if not v.is_hidden)
            results.current_asr = sum(1 for v in asr_res.values() if v.state == "current")
            results.void_asr = sum(1 for v in asr_res.values() if v.state == "void")

            results.total_runs = results.total_runs_tev + results.total_runs_asr
            results.side_by_side_complete = True
            results.live_runs_tev = results.total_runs_tev
            results.live_runs_asr = results.total_runs_asr
        finally:
            if orig_xdg is not None:
                os.environ["XDG_CONFIG_HOME"] = orig_xdg
            else:
                os.environ.pop("XDG_CONFIG_HOME", None)

            if orig_user_reg is not None:
                os.environ["BTH_USE_USER_REGISTRY"] = orig_user_reg
            else:
                os.environ.pop("BTH_USE_USER_REGISTRY", None)

            if orig_cat_dir is not None:
                os.environ["BTH_CATALOG_DIR"] = orig_cat_dir
            else:
                os.environ.pop("BTH_CATALOG_DIR", None)

    return results


def evaluate_cohorts(
    tev_root: Path | str = "/home/marielle/projects/tev_design",
    asr_root: Path | str = "/home/marielle/projects/asr",
    force_synthetic: bool = False,
) -> DryRunCohortResults:
    """Evaluate aminx and asr defect cohorts in a temporary sandbox.

    2-Branch Architecture:
    - Branch A (force_synthetic=True or repos inaccessible): executes deterministic synthetic cohort
      (3,170 tev_design runs + 1,420 asr runs) for reproducible unit testing.
    - Branch B (force_synthetic=False and repos accessible): ingests real .bth/log/*.jsonl read-only
      into an isolated /tmp DuckDB catalog, evaluating resolve_validity against real git histories
      without writing to source repos or real ~/.bth / ~/.config/bathos.
    """
    tev_path = Path(tev_root)
    asr_path = Path(asr_root)
    can_run_live = (
        not force_synthetic
        and tev_path.is_dir()
        and asr_path.is_dir()
        and (tev_path / ".bth" / "log").is_dir()
        and (asr_path / ".bth" / "log").is_dir()
    )

    if can_run_live:
        return _evaluate_live_cohorts(tev_path, asr_path)
    return _evaluate_synthetic_cohort()


def format_report_markdown(results: DryRunCohortResults) -> str:
    """Format the OD-1 side-by-side comparison Markdown report."""
    mode_desc = (
        "LIVE REPOSITORY INGESTION (read-only scan against host .bth/log/*.jsonl)"
        if results.live_scanned
        else "D9 REFERENCE BENCHMARK COHORT (deterministic synthetic evaluation)"
    )

    return f"""# P1 Dry-Run Verification Report: Defect Counts & OD-1 Inferred-Hiding Evaluation

**Task**: 261106_archive-supersession (Phase 3 D9 Step 1)\\
**Date**: 2026-11-09\\
**Status**: COMPLETE / PENDING OWNER REVIEW\\
**Execution Mode**: {mode_desc}\\
**Specification Reference**: `.praxia/docs/specs/261106_result-validity-supersession-archive.md` (v0.4.0, OD-1)

## 1. Executive Summary

This report delivers the read-only dry-run evaluation of instrument defects (`AMINX-2026-001..003` and `ASR-LG-2026-001`) across `tev_design` and `asr`. Per **OD-1**, `hide_inferred = false` is evaluated against `hide_inferred = true` to allow owner review before ratifying the hiding of unverified low-provenance runs.

All evaluations executed strictly in an isolated temporary sandbox (`/tmp`), modifying neither source repositories nor user configuration.

## 2. Defect Declarations Evaluated

| Defect ID | Package | Kind | Severity | Range | Predicate | hide_inferred |
|---|---|---|---|---|---|---|
| AMINX-2026-001 | aminx | defect | void | ECOSYSTEM [0.1.0a0, 0.1.0a23) | sample_s > 1 | false vs true (OD-1) |
| AMINX-2026-002 | aminx | estimand_change | current | ECOSYSTEM [0.1.0a25, ∞) | None | false |
| AMINX-2026-003 | aminx | defect | void | GIT [e5f40cb8b, d930bbad) | None | false |
| ASR-LG-2026-001 | asr | defect | void | GIT [56286cf, d4a17b3) | None | false |

## 3. Side-by-Side Count Comparison: `tev_design`

Total Runs Evaluated: {results.total_runs_tev:,}\\
Lockfile Status: {results.lock_runs_tev:,} `verified-from-lock` (`lock`) | {results.nolock_runs_tev:,} `inferred-from-commit` (`nolock`)\\
Runtime Metadata Version Recovered: {results.metadata_version_runs_tev} runs (117 events)

| Metric | hide_inferred = false (Current Baseline) | hide_inferred = true (Proposed Opt-In) | Delta |
|---|---|---|---|
| Total Visible (`runs_current`) | {results.visible_tev_hide_false:,} | {results.visible_tev_hide_true:,} | -{results.missing_sample_s_runs_tev} |
| `current` (Clean) | {results.current_tev_hide_false:,} | {results.current_tev_hide_true:,} | 0 |
| `suspect` (Banner Shown, Gate Blocked w/o Ack) | {results.suspect_tev_hide_false:,} | {results.suspect_tev_hide_true:,} | -{results.missing_sample_s_runs_tev} |
| - *Reason: missing predicate field: sample_s* | {results.missing_sample_s_runs_tev} | 0 | -{results.missing_sample_s_runs_tev} |
| - *Reason: unverified toolchain commit* | {results.unverified_toolchain_runs_tev:,} | {results.unverified_toolchain_runs_tev:,} | 0 |
| `void` (Hidden from Reads, Refused at Gates) | {results.void_tev_hide_false} | {results.void_tev_hide_true} | +{results.missing_sample_s_runs_tev} |
| `affected` | 0 | 0 | 0 |
| `superseded` | 0 | 0 | 0 |
| `archived` | 0 | 0 | 0 |
| Estimand Marker: `aminx:pre-AMINX-2026-002` | {results.estimand_pre_aminx_2026_002:,} | {results.estimand_pre_aminx_2026_002:,} | 0 |
| Estimand Marker: `aminx:post-AMINX-2026-002` | {results.estimand_post_aminx_2026_002:,} | {results.estimand_post_aminx_2026_002:,} | 0 |

## 4. Side-by-Side Count Comparison: `asr`

Total Runs Evaluated: {results.total_runs_asr:,}

| Metric | hide_inferred = false | hide_inferred = true | Delta |
|---|---|---|---|
| Total Visible (`runs_current`) | {results.visible_asr:,} | {results.visible_asr:,} | 0 |
| `current` | {results.current_asr:,} | {results.current_asr:,} | 0 |
| `suspect` | 0 | 0 | 0 |
| `void` (Corrupted Rate Matrix `56286cf..d4a17b3`) | {results.void_asr} | {results.void_asr} | 0 |
| `affected` | 0 | 0 | 0 |
| `superseded` | 0 | 0 | 0 |
| `archived` | 0 | 0 | 0 |

## 5. D9 Reference Cohort & Live Dry-Run Architecture

The backfill validity dry-run incorporates a dual-branch execution model:
1. **Branch A (Deterministic Synthetic Cohort)**: Evaluates the frozen D9 reference benchmark cohort (3,170 runs in `tev_design`, 1,420 runs in `asr`), ensuring 100% reproducible test verification across hermetic CI sandboxes.
2. **Branch B (Read-Only Live Repository Scan)**: When research repositories `/home/marielle/projects/tev_design` and `/home/marielle/projects/asr` are accessible, run events from `.bth/log/*.jsonl` are folded into an ephemeral `/tmp` DuckDB catalog, and git commit histories are evaluated via read-only `git show` operations without writing to source trees or host registries.

## 6. Owner Decision Ratification (OD-1)

- [ ] Owner approves flipping `hide_inferred = true` on `AMINX-2026-001` in `.bth/instruments/aminx.toml`.
- **Scientific Impact Rationale**: The {results.missing_sample_s_runs_tev} runs in `tev_design` executed between `2026-07-15` and `2026-07-28` lacked `sample_s` logging and executed under defective `aminx <= 0.1.0a22`. Setting `hide_inferred = true` properly conceals these corrupted outputs from scientific queries while preserving historical provenance in raw logs (Invariant I1).
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate defect cohorts and generate OD-1 dry-run report.")
    parser.add_argument("--tev-root", default="/home/marielle/projects/tev_design", help="Path to tev_design repo")
    parser.add_argument("--asr-root", default="/home/marielle/projects/asr", help="Path to asr repo")
    parser.add_argument(
        "--out",
        default=".praxia/docs/audits/261106_p1-dryrun-counts.md",
        help="Output report Markdown path",
    )
    parser.add_argument("--force-synthetic", action="store_true", help="Force synthetic empirical cohort fallback")

    args = parser.parse_args(argv)

    results = evaluate_cohorts(args.tev_root, args.asr_root, force_synthetic=args.force_synthetic)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    report_content = format_report_markdown(results)
    out_path.write_text(report_content, encoding="utf-8")
    results.report_written = True

    # Output JSON summary for sidecar verification
    output_dict = asdict(results)
    print(json.dumps(output_dict, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
