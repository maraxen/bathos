"""Targeted test suite for Phase 1 (Stop the Leak) - Result Validity, Supersession, and Archive.

Covers AC-P1-01 through AC-P1-19 and Invariant I1 (Non-Obliteration immutability).
Spec: 261106 §2.1, §2.2, §3.4, §3.5, §3.6, §3.7, §3.8.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.blast_radius import (
    clear_blast_radius_flag,
)
from bathos.campaigns import (
    add_run_to_campaign,
    conclude_campaign,
    create_campaign,
    get_campaign,
)
from bathos.catalog import init_catalog, write_run
from bathos.claim import register_claim
from bathos.compact import compact
from bathos.errors import (
    EstimandBoundaryMixedError,
    ResultHiddenError,
    ResultSuspectUnacknowledgedError,
    ResultVoidError,
)
from bathos.index import connect_read
from bathos.mcp import (
    cite_run_tool,
    defect_declare_tool,
    defect_list_tool,
    find_runs_tool,
    get_run_tool,
    lineage_prov_tool,
    list_runs_tool,
    run_sql_tool,
)
from bathos.query import find_runs, get_run, list_runs
from bathos.readback import ProductTrustState, get_trust_state
from bathos.runlog.project_id import assign_project_id, register_main_root
from bathos.schema import Run
from bathos.trust_ledger import (
    GraduationRefused,
    TrustLedgerRecord,
    graduate_product,
    write_ledger_fragment,
)
from bathos.validity import (
    compute_distance_from_present,
    emit_validity_filter_applied,
    emit_validity_override_used,
    ensure_validity_cache,
    resolve_validity,
)
from bathos.version_resolver import (
    load_instrument_registry_packages,
    version_in_range,
)


def _make_git_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.com"], cwd=path, check=True)
    subprocess.run(["git", "config", "user.name", "Test User"], cwd=path, check=True)
    (path / "README.md").write_text("test repo\n")
    (path / ".bth.toml").write_text(f'[project]\nslug = "{path.name}"\n')
    subprocess.run(["git", "add", "."], cwd=path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=path, check=True)
    assign_project_id(path)
    register_main_root(path)
    return path


def _create_sample_run(
    catalog_dir: Path,
    run_id: str,
    project_slug: str = "repo",
    git_hash: str = "abc1234",
    metadata: dict | None = None,
    output_metadata: list[dict] | None = None,
    dependency_provenance: dict | None = None,
    dependency_lock_sha256: str | None = None,
    toolchain_version_source: str | None = None,
    stage_name: str | None = None,
    epistemic_class: str | None = None,
    argv: list[str] | None = None,
    parent_run_id: str | None = None,
    timestamp: datetime | None = None,
) -> Run:
    r = Run(
        id=run_id,
        project_slug=project_slug,
        command=f"python run_{run_id[:8]}.py",
        argv=argv or ["python", f"run_{run_id[:8]}.py"],
        parent_run_id=parent_run_id,
        git_hash=git_hash,
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        duration_s=1.0,
        metadata=json.dumps(metadata) if metadata else None,
        output_metadata=json.dumps(output_metadata) if output_metadata else None,
        dependency_provenance=json.dumps(dependency_provenance) if dependency_provenance else None,
        dependency_lock_sha256=dependency_lock_sha256,
        toolchain_version_source=toolchain_version_source,
        stage_name=stage_name,
        epistemic_class=epistemic_class,
        timestamp=timestamp or datetime.now(UTC),
    )
    write_run(r, catalog_dir)
    return r


# ---------------------------------------------------------------------------
# AC-P1-01: Defect predicate & verified lock (sample_s > 1, sample_s = 4 -> void)
# ---------------------------------------------------------------------------
def test_ac_p1_01_predicate_and_verified_lock(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    lock_content = """version = 1
requires-python = ">=3.13"

[[package]]
name = "aminx"
version = "0.1.0a15"
"""
    lock_file = repo / "uv.lock"
    lock_file.write_text(lock_content)
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add lock"], cwd=repo, check=True)
    c_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    lock_sha = hashlib.sha256(lock_file.read_bytes()).hexdigest()

    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    defect_toml = """schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [
  { type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }
]

[entries.predicate]
condition = "sample_s > 1"
description = "Inflated rates when sample_s exceeds 1"
"""
    (inst_dir / "aminx.toml").write_text(defect_toml)

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    run_id = "00000000-0000-0000-0000-000000000001"
    _create_sample_run(
        cat_dir,
        run_id,
        git_hash=c_hash,
        dependency_lock_sha256=lock_sha,
        metadata={"sample_s": 4},
    )
    compact(cat_dir)

    res = resolve_validity([run_id], cat_dir, workspace_root=repo)
    assert run_id in res
    v = res[run_id]
    assert v.state == "void"
    assert v.is_hidden is True
    assert v.version_source == "verified-from-lock"
    assert v.defect_id == "AMINX-2026-001"


# ---------------------------------------------------------------------------
# AC-P1-02: Predicate OD-2: sample_s = 1 -> current; missing sample_s -> suspect
# ---------------------------------------------------------------------------
def test_ac_p1_02_predicate_od2(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    lock_file = repo / "uv.lock"
    lock_file.write_text('version = 1\n[[package]]\nname = "aminx"\nversion = "0.1.0a15"\n')
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add lock"], cwd=repo, check=True)
    c_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    lock_sha = hashlib.sha256(lock_file.read_bytes()).hexdigest()

    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    defect_toml = """schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [
  { type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }
]

[entries.predicate]
condition = "sample_s > 1"
"""
    (inst_dir / "aminx.toml").write_text(defect_toml)

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Run A: sample_s = 1 -> predicate evaluates to False -> current
    run_a_id = "00000000-0000-0000-0000-00000000000a"
    _create_sample_run(
        cat_dir,
        run_a_id,
        git_hash=c_hash,
        dependency_lock_sha256=lock_sha,
        metadata={"sample_s": 1},
    )

    # Run B: missing sample_s -> AST check catches missing column -> suspect
    run_b_id = "00000000-0000-0000-0000-00000000000b"
    _create_sample_run(
        cat_dir,
        run_b_id,
        git_hash=c_hash,
        dependency_lock_sha256=lock_sha,
        metadata={"other_field": 42},
    )
    compact(cat_dir)

    res = resolve_validity([run_a_id, run_b_id], cat_dir, workspace_root=repo)
    va = res[run_a_id]
    assert va.state == "current"
    assert va.is_hidden is False

    vb = res[run_b_id]
    assert vb.state == "suspect"
    assert vb.is_hidden is False
    assert "missing predicate field: sample_s" in (vb.reason or "")


# ---------------------------------------------------------------------------
# AC-P1-03: Inferred matches OD-1: hide_inferred=false -> suspect, hide_inferred=true -> void
# ---------------------------------------------------------------------------
def test_ac_p1_03_inferred_od1(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)

    # Defect with hide_inferred = false
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-DEFAULT"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]

[[entries]]
id = "AMINX-OPTIN"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Run 1 matches AMINX-DEFAULT (hide_inferred=false)
    run_1_id = "00000000-0000-0000-0000-000000000001"
    _create_sample_run(
        cat_dir,
        run_1_id,
        metadata={"aminx_version": "0.1.0a15"},  # Tier 3 inferred
    )

    # Let's test with single-entry registry files to isolate behavior cleanly
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-DEFAULT"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]
""")
    compact(cat_dir)

    res1 = resolve_validity([run_1_id], cat_dir, workspace_root=repo)
    v1 = res1[run_1_id]
    assert v1.version_source == "inferred-from-commit"
    assert v1.state == "suspect"
    assert v1.is_hidden is False

    # Now change hide_inferred = true
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-DEFAULT"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]
""")

    res2 = resolve_validity([run_1_id], cat_dir, workspace_root=repo)
    v2 = res2[run_1_id]
    assert v2.version_source == "inferred-from-commit"
    assert v2.state == "void"
    assert v2.is_hidden is True


# ---------------------------------------------------------------------------
# AC-P1-04: Query filtering and pre-LIMIT pushdown (list_runs & find_runs)
# ---------------------------------------------------------------------------
def test_ac_p1_04_query_filtering_and_pagination(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Create 5 void runs and 2 current runs
    void_ids = []
    for i in range(5):
        rid = f"11111111-0000-0000-0000-00000000000{i}"
        _create_sample_run(cat_dir, rid, metadata={"aminx_version": "0.1.5"})
        void_ids.append(rid)

    curr_ids = []
    for i in range(2):
        rid = f"22222222-0000-0000-0000-00000000000{i}"
        _create_sample_run(cat_dir, rid, metadata={"aminx_version": "0.3.0"})
        curr_ids.append(rid)

    compact(cat_dir)

    # 1. list_runs without include_superseded: returns only the 2 current runs
    runs = list_runs(cat_dir, limit=5, include_superseded=False)
    assert len(runs) == 2
    assert {r.id for r in runs} == set(curr_ids)

    # 2. list_runs with include_superseded: returns 5 runs (limit)
    runs_all = list_runs(cat_dir, limit=5, include_superseded=True)
    assert len(runs_all) == 5

    # 3. MCP list_runs_tool reports hidden_count and notice
    mcp_res = list_runs_tool(catalog_dir=str(cat_dir), limit=5)
    assert mcp_res["hidden_count"] == 5
    assert "5 superseded/void/archived results hidden" in (mcp_res["notice"] or "")
    assert len(mcp_res["runs"]) == 2

    # 4. find_runs excludes void runs by default
    f_runs = find_runs(cat_dir, include_superseded=False)
    assert len(f_runs) == 2

    # 5. find_runs_tool reports hidden_count
    f_mcp = find_runs_tool(catalog_dir=str(cat_dir))
    assert f_mcp["hidden_count"] == 5
    assert len(f_mcp["runs"]) == 2


# ---------------------------------------------------------------------------
# AC-P1-05 & AC-P1-06: get_run refusal & banners (ResultHiddenError vs [VOID])
# ---------------------------------------------------------------------------
def test_ac_p1_05_and_06_get_run_and_show(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    void_id = "33333333-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, void_id, metadata={"aminx_version": "0.1.5"})
    compact(cat_dir)

    # AC-P1-05: get_run on hidden run raises ResultHiddenError
    with pytest.raises(ResultHiddenError) as exc_info:
        get_run(void_id, cat_dir, include_superseded=False)
    assert exc_info.value.state == "void"
    assert exc_info.value.defect_id == "DEF-001"

    # MCP get_run_tool returns structured result_hidden error
    mcp_err = get_run_tool(void_id, catalog_dir=str(cat_dir), include_superseded=False)
    assert mcp_err.get("error_code") == "result_hidden"
    assert mcp_err.get("state") == "void"

    # AC-P1-06: with include_superseded=True, get_run succeeds and get_run_tool includes [VOID] banner
    run_obj = get_run(void_id, cat_dir, include_superseded=True)
    assert run_obj is not None
    assert run_obj.id == void_id

    mcp_ok = get_run_tool(void_id, catalog_dir=str(cat_dir), include_superseded=True)
    assert "error" not in mcp_ok
    assert "[VOID" in mcp_ok.get("validity_banner", "")


# ---------------------------------------------------------------------------
# AC-P1-07 & AC-P1-08: Citation Gate (OD-5 acknowledgment rationale)
# ---------------------------------------------------------------------------
def test_ac_p1_07_and_08_cite_gate(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]

[[entries]]
id = "DEF-SUSPECT"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.2.0" }, { fixed = "0.3.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    void_id = "44444444-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, void_id, metadata={"aminx_version": "0.1.5"})

    suspect_id = "44444444-0000-0000-0000-000000000002"
    _create_sample_run(cat_dir, suspect_id, metadata={"aminx_version": "0.2.5"})
    compact(cat_dir)

    # AC-P1-07: cite_run on void run raises ResultVoidError
    with pytest.raises(ResultVoidError):
        cite_run_tool(void_id, catalog_dir=str(cat_dir))

    # AC-P1-08: cite_run on suspect run without rationale raises ResultSuspectUnacknowledgedError
    with pytest.raises(ResultSuspectUnacknowledgedError):
        cite_run_tool(suspect_id, catalog_dir=str(cat_dir), ack_suspect="")

    with pytest.raises(ResultSuspectUnacknowledgedError):
        cite_run_tool(suspect_id, catalog_dir=str(cat_dir), ack_suspect="   ")

    # Providing rationale succeeds, adds banner, and records to acknowledgment_ledger
    cite_res = cite_run_tool(
        suspect_id,
        catalog_dir=str(cat_dir),
        ack_suspect="Validated with orthogonal mass spectrometry",
    )
    assert "[SUSPECT — ACKNOWLEDGED: Validated with orthogonal mass spectrometry]" in cite_res["citation"]

    # Check acknowledgment ledger
    con = connect_read(cat_dir)
    acks = con.execute("SELECT kind, entity_id, reason FROM acknowledgment_ledger").fetchall()
    con.close()
    assert len(acks) >= 1
    assert acks[-1][0] == "validity.acknowledged"
    assert acks[-1][1] == suspect_id
    assert acks[-1][2] == "Validated with orthogonal mass spectrometry"


# ---------------------------------------------------------------------------
# AC-P1-09: Claim & Parity Gates (refuse hidden member or parity_run_id)
# ---------------------------------------------------------------------------
def test_ac_p1_09_claim_and_parity_gates(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    void_id = "55555555-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, void_id, metadata={"aminx_version": "0.1.5"})
    compact(cat_dir)

    # Create campaign and add void run
    db = duckdb.connect(str(cat_dir / "bathos.db"))
    camp = create_campaign(db, name="Test Campaign", project_slug="repo", mode="confirmation")
    # Link run to campaign in warm db
    db.execute("INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)", [camp.id, void_id])

    claim_path = repo / "claim.bth.toml"
    claim_path.write_text(f"""[claim]
statement = "Test claim"
campaign_id = "{camp.id}"

[clauses.c1]
label = "clause 1"
predicate = "temp_mean > 0"
""")

    # register_claim on campaign with void member run raises ResultVoidError
    with pytest.raises(ResultVoidError):
        register_claim(claim_path, camp.id, db, repo, catalog_dir=cat_dir)

    db.close()


# ---------------------------------------------------------------------------
# AC-P1-10: Campaign Conclude unconditionally refuses hidden member runs
# ---------------------------------------------------------------------------
def test_ac_p1_10_campaign_conclude_unconditional(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    void_id = "66666666-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, void_id, metadata={"aminx_version": "0.1.5"})
    compact(cat_dir)

    # Create campaign without claim
    db = duckdb.connect(str(cat_dir / "bathos.db"))
    camp = create_campaign(db, name="Claimless Campaign", project_slug="repo", mode="exploration")
    db.execute("INSERT INTO campaign_runs (campaign_id, run_id) VALUES (?, ?)", [camp.id, void_id])

    # conclude_campaign must unconditionally refuse with ResultVoidError BEFORE checking claim
    with pytest.raises(ResultVoidError):
        conclude_campaign(db, camp.id, "pass", "conclude should fail", catalog_dir=cat_dir, workspace_root=repo)

    db.close()


# ---------------------------------------------------------------------------
# AC-P1-11: graduate_product refuses hidden producing runs before early return
# ---------------------------------------------------------------------------
def test_ac_p1_11_graduate_product_refuses_hidden_producing_runs(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    content_hash = "a" * 64
    run_id = "77777777-0000-0000-0000-000000000001"
    _create_sample_run(
        cat_dir,
        run_id,
        metadata={"aminx_version": "0.1.5"},
        output_metadata=[{"sha256": content_hash, "path": "/tmp/out.dat"}],
    )
    compact(cat_dir)

    # First graduate with a dummy PASS attestation record written to trust ledger
    tl_rec = TrustLedgerRecord(
        content_hash=content_hash,
        from_state="candidate",
        to_state="promoted",
        attestation_ref="attn-dummy",
        run_id=run_id,
    )
    write_ledger_fragment(tl_rec, cat_dir)

    # Declare defect that voids run_id
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    # Attempting to re-graduate the same product must be intercepted BEFORE line 360 early return
    # and fail with GraduationRefused
    with pytest.raises(GraduationRefused) as exc_info:
        graduate_product(cat_dir, content_hash, "attn-new", run_id=run_id)
    assert "are hidden" in str(exc_info.value)


# ---------------------------------------------------------------------------
# AC-P1-12: get_trust_state read-time downgrade (zero ledger mutation)
# ---------------------------------------------------------------------------
def test_ac_p1_12_trust_state_read_downgrade(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Product 1: produced ONLY by void run
    h1 = "1" * 64
    r1_id = "88888888-0000-0000-0000-000000000001"
    _create_sample_run(
        cat_dir,
        r1_id,
        metadata={"aminx_version": "0.1.5"},
        output_metadata=[{"sha256": h1, "path": "/tmp/out1.dat"}],
    )
    tl1 = TrustLedgerRecord(
        content_hash=h1,
        from_state="candidate",
        to_state="promoted",
        attestation_ref="attn-1",
        run_id=r1_id,
    )
    write_ledger_fragment(tl1, cat_dir)

    # Product 2: produced by void run AND current run
    h2 = "2" * 64
    r2_void_id = "88888888-0000-0000-0000-000000000002"
    _create_sample_run(
        cat_dir,
        r2_void_id,
        metadata={"aminx_version": "0.1.5"},
        output_metadata=[{"sha256": h2, "path": "/tmp/out2.dat"}],
    )
    r2_curr_id = "88888888-0000-0000-0000-000000000003"
    _create_sample_run(
        cat_dir,
        r2_curr_id,
        metadata={"aminx_version": "0.3.0"},
        output_metadata=[{"sha256": h2, "path": "/tmp/out2.dat"}],
    )
    tl2 = TrustLedgerRecord(
        content_hash=h2,
        from_state="candidate",
        to_state="promoted",
        attestation_ref="attn-2",
        run_id=r2_void_id,
    )
    write_ledger_fragment(tl2, cat_dir)
    compact(cat_dir)

    # Readback check:
    # h1 has ALL producing runs hidden -> downgraded to CANDIDATE
    st1 = get_trust_state(cat_dir, h1)
    assert st1 == ProductTrustState.CANDIDATE

    # h2 has at least 1 current producing run -> preserved as PROMOTED
    st2 = get_trust_state(cat_dir, h2)
    assert st2 == ProductTrustState.PROMOTED

    # Invariant: trust ledger files on disk were not mutated/added
    con = connect_read(cat_dir)
    tl_count = con.execute("SELECT count(*) FROM trust_ledger").fetchone()[0]
    con.close()
    assert tl_count == 2, "No new ledger entries written during read-time downgrade"


# ---------------------------------------------------------------------------
# AC-P1-13: Invariant I1 (21 CFR Part 11 Non-Obliteration Immutability)
# ---------------------------------------------------------------------------
def test_ac_p1_13_non_obliteration_invariant_i1(tmp_path: Path):
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Create several runs in cool tier
    run_ids = []
    for i in range(10):
        rid = f"99999999-0000-0000-0000-0000000000{i:02d}"
        _create_sample_run(cat_dir, rid, metadata={"i": i})
        run_ids.append(rid)

    compact(cat_dir)

    # Snapshot sha256 of all Parquet fragments
    cool_parquets = sorted(cat_dir.glob("runs/**/*.parquet"))
    assert len(cool_parquets) == 10
    hashes_before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in cool_parquets}

    # Snapshot DuckDB rows
    con = connect_read(cat_dir)
    runs_before = con.execute("SELECT id, metadata FROM runs ORDER BY id").fetchall()
    con.close()

    # Execute resolve_validity multiple times
    res1 = resolve_validity(run_ids, cat_dir)
    assert len(res1) == 10
    ensure_validity_cache(cat_dir)
    res2 = resolve_validity(run_ids, cat_dir)
    assert len(res2) == 10

    # Verify 100% byte-identical immutability
    for p in cool_parquets:
        assert hashlib.sha256(p.read_bytes()).hexdigest() == hashes_before[p], (
            f"Parquet fragment {p} was modified!"
        )

    # Verify DuckDB rows in runs table are untouched
    con = connect_read(cat_dir)
    runs_after = con.execute("SELECT id, metadata FROM runs ORDER BY id").fetchall()
    con.close()
    assert runs_before == runs_after, "DuckDB runs table was mutated!"


# ---------------------------------------------------------------------------
# AC-P1-14: SELECT * FROM runs_current excludes void/affected/superseded/archived
# ---------------------------------------------------------------------------
def test_ac_p1_14_runs_current_view(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    void_id = "aaaaaaaa-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, void_id, metadata={"aminx_version": "0.1.5"})

    curr_id = "aaaaaaaa-0000-0000-0000-000000000002"
    _create_sample_run(cat_dir, curr_id, metadata={"aminx_version": "0.3.0"})
    compact(cat_dir)

    con = connect_read(cat_dir)
    all_runs = con.execute("SELECT id FROM runs").fetchall()
    current_runs = con.execute("SELECT id FROM runs_current").fetchall()
    con.close()

    assert len(all_runs) == 2
    assert len(current_runs) == 1
    assert current_runs[0][0] == curr_id


# ---------------------------------------------------------------------------
# AC-P1-15: run_sql OD-3 notice check on base runs table
# ---------------------------------------------------------------------------
def test_ac_p1_15_raw_runs_notice_od3(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    void_id = "bbbbbbbb-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, void_id, metadata={"aminx_version": "0.1.5"})
    curr_id = "bbbbbbbb-0000-0000-0000-000000000002"
    _create_sample_run(cat_dir, curr_id, metadata={"aminx_version": "0.3.0"})
    compact(cat_dir)

    # 1. Query against runs emits stderr notice and MCP notice
    mcp_res = run_sql_tool(sql="SELECT count(*) FROM runs", catalog_dir=str(cat_dir))
    assert mcp_res["hidden_count"] == 1
    assert "notice" in mcp_res and "unmuted history" in mcp_res["notice"]

    # 2. Query against runs_current produces NO notice
    mcp_curr = run_sql_tool(sql="SELECT count(*) FROM runs_current", catalog_dir=str(cat_dir))
    assert mcp_curr["notice"] is None

    # 3. Query against runs with allow_hidden=True suppresses notice
    mcp_suppressed = run_sql_tool(
        sql="SELECT count(*) FROM runs", catalog_dir=str(cat_dir), allow_hidden=True
    )
    assert mcp_suppressed["notice"] is None


# ---------------------------------------------------------------------------
# AC-P1-16: Auto-union of tracked packages
# ---------------------------------------------------------------------------
def test_ac_p1_16_runner_auto_union_tracked_packages(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"
[[entries]]
id = "AMINX-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = []
""")
    pkgs = load_instrument_registry_packages(repo)
    assert "aminx" in pkgs


# ---------------------------------------------------------------------------
# AC-P1-17: Lineage PROV-JSON banners & catalog_readable
# ---------------------------------------------------------------------------
def test_ac_p1_17_lineage_prov_banners(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "DEF-VOID"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0" }, { fixed = "0.2.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    parent_id = "cccccccc-0000-0000-0000-000000000001"
    child_id = "cccccccc-0000-0000-0000-000000000002"
    _create_sample_run(cat_dir, parent_id, metadata={"aminx_version": "0.1.5"})
    _create_sample_run(
        cat_dir,
        child_id,
        metadata={"aminx_version": "0.3.0"},
        parent_run_id=parent_id,
    )
    compact(cat_dir)

    # Insert run_edges
    con = duckdb.connect(str(cat_dir / "bathos.db"))
    con.execute(
        "CREATE TABLE IF NOT EXISTS run_edges (child_run_id VARCHAR, parent_run_id VARCHAR, PRIMARY KEY (child_run_id, parent_run_id))"
    )
    con.execute(
        "INSERT INTO run_edges VALUES (?, ?)", [child_id, parent_id]
    )
    con.close()

    res = lineage_prov_tool(child_id, catalog_dir=str(cat_dir))
    assert "validity_banners" in res
    assert parent_id in res["validity_banners"]
    assert "VOID" in res["validity_banners"][parent_id]


# ---------------------------------------------------------------------------
# AC-P1-18: Estimand change boundary (OD-6)
# ---------------------------------------------------------------------------
def test_ac_p1_18_estimand_boundary_od6(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-ESTIMAND"
revision = 1
kind = "estimand_change"
severity = "void"
hide_inferred = false
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.2.0" }, { fixed = "0.3.0" }] }]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r1_id = "dddddddd-0000-0000-0000-000000000001"
    _create_sample_run(cat_dir, r1_id, metadata={"aminx_version": "0.1.0"}, timestamp=datetime(2027, 1, 1, 0, 0, 0, tzinfo=UTC))

    r2_id = "dddddddd-0000-0000-0000-000000000002"
    _create_sample_run(cat_dir, r2_id, metadata={"aminx_version": "0.2.5"}, timestamp=datetime(2027, 1, 1, 1, 0, 0, tzinfo=UTC))
    compact(cat_dir)

    db = duckdb.connect(str(cat_dir / "bathos.db"))
    camp = create_campaign(db, name="Estimand Campaign", project_slug="repo", mode="confirmation")

    # 1. Adding runs to campaign: allows mixing with warning banner
    add_run_to_campaign(db, camp.id, r1_id, catalog_dir=cat_dir)
    add_run_to_campaign(db, camp.id, r2_id, catalog_dir=cat_dir)

    # 2. Conclude without allow_mixed_estimand fails with EstimandBoundaryMixedError
    with pytest.raises(EstimandBoundaryMixedError):
        conclude_campaign(db, camp.id, "pass", "done", catalog_dir=cat_dir, workspace_root=repo)

    # 3. Conclude with allow_mixed_estimand succeeds
    conclude_campaign(
        db,
        camp.id,
        "pass",
        "done",
        catalog_dir=cat_dir,
        workspace_root=repo,
        allow_mixed_estimand="Calibrated cross-epoch variance",
    )
    c_final = get_campaign(db, camp.id)
    assert c_final.status == "concluded"
    db.close()

    # Check override recorded to acknowledgment_ledger
    con = connect_read(cat_dir)
    acks = con.execute("SELECT kind, reason FROM acknowledgment_ledger").fetchall()
    con.close()
    assert any(a[0] == "validity.estimand_override_recorded" for a in acks)


# ---------------------------------------------------------------------------
# AC-P1-19: Commit-range defects (GIT) and per-revision exemptions (@r1 vs @r2)
# ---------------------------------------------------------------------------
def test_ac_p1_19_git_range_and_exemption_revision(tmp_path: Path):
    repo = _make_git_repo(tmp_path / "repo")

    # Commit 1 (introduced)
    (repo / "file1.txt").write_text("v1\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit 1"], cwd=repo, check=True)
    c1 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 2 (affected)
    (repo / "file2.txt").write_text("v2\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit 2"], cwd=repo, check=True)
    c2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # Commit 3 (fixed)
    (repo / "file3.txt").write_text("v3\n")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "commit 3"], cwd=repo, check=True)
    c3 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "asr.toml").write_text(f"""schema_version = "1.0.0"
package = "asr"
ecosystem = "PyPI"

[[entries]]
id = "ASR-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{{ type = "GIT", events = [{{ introduced = "{c1}" }}, {{ fixed = "{c3}" }}] }}]
""")

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_c2_id = "eeeeeeee-0000-0000-0000-000000000002"
    _create_sample_run(cat_dir, r_c2_id, git_hash=c2, dependency_provenance={"asr_source_commit": c2})

    r_c3_id = "eeeeeeee-0000-0000-0000-000000000003"
    _create_sample_run(cat_dir, r_c3_id, git_hash=c3, dependency_provenance={"asr_source_commit": c3})
    compact(cat_dir)

    # c2 matches range -> void; c3 is fixed -> current
    res = resolve_validity([r_c2_id, r_c3_id], cat_dir, workspace_root=repo)
    assert res[r_c2_id].state == "void"
    assert res[r_c3_id].state == "current"

    # Per-revision exemption: clear flag for ASR-001@r1
    clear_blast_radius_flag(
        cat_dir,
        entity_type="run",
        entity_id=r_c2_id,
        reason="Manual audit verified unaffected by commit range bug",
        anchor_kind="dependency_range",
        anchor_value="ASR-001@r1",
    )
    compact(cat_dir)

    # Re-resolving r_c2_id now respects exemption -> current
    res_exempt = resolve_validity([r_c2_id], cat_dir, workspace_root=repo)
    assert res_exempt[r_c2_id].state == "current"

    # Bump revision to 2
    (inst_dir / "asr.toml").write_text(f"""schema_version = "1.0.0"
package = "asr"
ecosystem = "PyPI"

[[entries]]
id = "ASR-001"
revision = 2
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{{ type = "GIT", events = [{{ introduced = "{c1}" }}, {{ fixed = "{c3}" }}] }}]
""")

    # Exemption for @r1 no longer matches @r2 -> reverts to void
    res_bumped = resolve_validity([r_c2_id], cat_dir, workspace_root=repo)
    assert res_bumped[r_c2_id].state == "void"


# ---------------------------------------------------------------------------
# Defect Declare and List MCP Tools
# ---------------------------------------------------------------------------
def test_defect_declare_and_list_tools(tmp_path: Path, monkeypatch):
    repo = _make_git_repo(tmp_path / "repo")
    monkeypatch.chdir(repo)

    res_dec = defect_declare_tool(
        package="testpkg",
        defect_id="TEST-001",
        introduced="1.0.0",
        fixed="1.1.0",
        summary="Test defect declaration",
    )
    assert res_dec["status"] == "declared"

    res_list = defect_list_tool(package="testpkg")
    assert res_list["count"] == 1
    assert res_list["defects"][0]["defect_id"] == "TEST-001"


# ---------------------------------------------------------------------------
# PEP 440 local version stripping
# ---------------------------------------------------------------------------
def test_pep440_local_version_handling():
    # Defect fixed in 0.2.1
    # Version 0.2.1+armask strips to 0.2.1 base_version >= 0.2.1 -> not in range
    assert not version_in_range("0.2.1+armask", "0.1.0", "0.2.1")
    # Version 0.2.0+armask strips to 0.2.0 base_version < 0.2.1 -> in range
    assert version_in_range("0.2.0+armask", "0.1.0", "0.2.1")


# ---------------------------------------------------------------------------
# M1 Track 1: compute_distance_from_present 5-Condition Enforcement (§1.4.C.3)
# ---------------------------------------------------------------------------
def test_compute_distance_from_present_spec_1_4_c_3(tmp_path: Path):
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    t0 = datetime(2026, 1, 1, 10, 0, 0, tzinfo=UTC)
    t1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
    t2 = datetime(2026, 1, 1, 14, 0, 0, tzinfo=UTC)

    def _make_run(run_id, slug, status="completed", outcome="pass", sha=None, ts=None):
        r = Run(
            id=run_id,
            project_slug=slug,
            command="python exp.py",
            argv=["python", "exp.py"],
            git_hash="abc1234",
            git_branch="main",
            git_dirty=False,
            status=status,
            outcome=outcome,
            sidecar_sha256=sha,
            timestamp=ts or datetime.now(UTC),
        )
        write_run(r, cat_dir)
        return r

    # Base run R
    r_base = _make_run("00000000-0000-0000-0000-000000000010", "projA", "completed", "pass", "sha-valid-123", t1)
    # (a) Prior run (timestamp < R.timestamp) -> MUST NOT count
    _make_run("00000000-0000-0000-0000-000000000011", "projA", "completed", "pass", "sha-valid-123", t0)
    # (b) Crashed/failed run (status != 'completed') -> MUST NOT count
    _make_run("00000000-0000-0000-0000-000000000012", "projA", "failed", "pass", "sha-valid-123", t2)
    # (e) Differing outcome (outcome != R.outcome) -> MUST NOT count
    _make_run("00000000-0000-0000-0000-000000000013", "projA", "completed", "fail", "sha-valid-123", t2)
    # Project scoping: different project -> MUST NOT count
    _make_run("00000000-0000-0000-0000-000000000014", "projB", "completed", "pass", "sha-valid-123", t2)
    # (d) Differing sidecar hash -> MUST NOT count
    _make_run("00000000-0000-0000-0000-000000000015", "projA", "completed", "pass", "sha-diff-456", t2)
    # Genuine reproduction -> MUST COUNT
    _make_run("00000000-0000-0000-0000-000000000016", "projA", "completed", "pass", "sha-valid-123", t2)

    compact(cat_dir)

    con = connect_read(cat_dir)
    res_map = resolve_validity([r_base.id], cat_dir, con=con)
    facts = compute_distance_from_present(r_base, res_map[r_base.id], con=con)
    con.close()
    assert facts["reproduction_count"] == 1

    # Exploratory run with neutral outcome -> MUST return reproduction_count == 0
    r_exploratory = _make_run(
        "00000000-0000-0000-0000-000000000017",
        "projA",
        "completed",
        "observed",
        "sha-valid-123",
        t1,
    )
    compact(cat_dir)
    con = connect_read(cat_dir)
    exp_map = resolve_validity([r_exploratory.id], cat_dir, con=con)
    exp_facts = compute_distance_from_present(r_exploratory, exp_map[r_exploratory.id], con=con)
    con.close()
    assert exp_facts["reproduction_count"] == 0


# ---------------------------------------------------------------------------
# M1 Track 1: Step 1 Archived Items Multi-Attribute Matching (Spec §2.1 Step 1)
# ---------------------------------------------------------------------------
def test_archived_items_multi_attribute_matching_and_replacement_id(tmp_path: Path):
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Insert archived_items records
    con = duckdb.connect(str(cat_dir / "bathos.db"))
    con.execute("""
        CREATE TABLE IF NOT EXISTS archived_items (
            id TEXT PRIMARY KEY,
            bundle_sha256 TEXT,
            pre_archive_sha TEXT,
            script_sha256 TEXT,
            paths TEXT,
            run_ids TEXT,
            reason TEXT,
            superseded_by TEXT,
            project_slug TEXT,
            recorded_at TEXT,
            event TEXT
        )
    """)
    con.execute("""
        INSERT INTO archived_items VALUES
        ('arch-1', 'bundle1', null, null, '["outputs/model.pt"]', null, 'model archived', 'item-succ-1', 'projA', '2026-01-01', 'archived'),
        ('arch-2', 'bundle2', null, null, '["scripts/train.py"]', null, 'script archived', 'item-succ-2', 'projA', '2026-01-01', 'archived'),
        ('arch-3', 'bundle3', null, null, '["scripts/other.py"]', null, 'other project script', 'item-succ-3', 'projB', '2026-01-01', 'archived')
    """)
    con.close()

    def _make_arch_run(run_id, slug, **kwargs):
        r = Run(
            id=run_id,
            project_slug=slug,
            command=kwargs.get("command", "python run.py"),
            argv=kwargs.get("argv", ["python", "run.py"]),
            git_hash="abc1234",
            git_branch="main",
            git_dirty=False,
            status="completed",
            output_paths=kwargs.get("output_paths", []),
        )
        write_run(r, cat_dir)
        return r

    # Run 1: matched via output_paths
    r1 = _make_arch_run("00000000-0000-0000-0000-000000000021", "projA", output_paths=["outputs/model.pt"])
    # Run 2: matched via command script
    r2 = _make_arch_run("00000000-0000-0000-0000-000000000022", "projA", command="python scripts/train.py --batch-size 32")
    # Run 3: matched via argv script
    r3 = _make_arch_run("00000000-0000-0000-0000-000000000023", "projA", argv=["scripts/train.py", "--lr", "0.01"])
    # Run 4: path matches arch-3 but project_slug differs (projA vs projB) -> NOT archived
    r4 = _make_arch_run("00000000-0000-0000-0000-000000000024", "projA", command="python scripts/other.py")

    for r in (r1, r2, r3, r4):
        write_run(r, cat_dir)
    compact(cat_dir)

    res = resolve_validity([r1.id, r2.id, r3.id, r4.id], cat_dir)
    assert res[r1.id].state == "archived"
    assert res[r1.id].replacement_id == "item-succ-1"

    assert res[r2.id].state == "archived"
    assert res[r2.id].replacement_id == "item-succ-2"

    assert res[r3.id].state == "archived"
    assert res[r3.id].replacement_id == "item-succ-2"

    assert res[r4.id].state == "current"
    assert res[r4.id].replacement_id is None


# ---------------------------------------------------------------------------
# M1 Track 1: Persistent version_cache across processes
# ---------------------------------------------------------------------------
def test_version_cache_persistent_across_processes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _make_git_repo(tmp_path / "repo")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))

    lock_file = repo / "uv.lock"
    lock_file.write_text('version = 1\n[[package]]\nname = "aminx"\nversion = "0.3.0"\n')
    subprocess.run(["git", "add", "uv.lock"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "add uv lock", "--no-verify"], cwd=repo, check=True)
    c_hash = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()
    lock_sha = hashlib.sha256(lock_file.read_bytes()).hexdigest()

    from bathos.version_resolver import _get_uv_lock_packages, _read_version_cache

    # 1. First lookup runs git and writes to cache
    sha, pkgs = _get_uv_lock_packages(str(repo), c_hash)
    assert sha == lock_sha
    assert "aminx" in pkgs
    assert pkgs["aminx"][0] == "0.3.0"

    # 2. Check JSON cache file was written to <main_root>/.bth/cache/version_cache.json
    cache_file = repo / ".bth" / "cache" / "version_cache.json"
    assert cache_file.is_file()
    doc = json.loads(cache_file.read_text())
    assert c_hash in doc
    assert doc[c_hash]["lock_sha256"] == lock_sha

    # 3. Explicit non-mocked assertion: DuckDB validity_cache.db:version_cache table is populated
    db_path = cat_dir / "validity_cache.db"
    assert db_path.is_file(), "validity_cache.db was not created in BTH_CATALOG_DIR"
    with duckdb.connect(str(db_path), read_only=True) as con:
        tables = {r[0] for r in con.execute("SELECT table_name FROM duckdb_tables()").fetchall()}
        assert "version_cache" in tables, "Table version_cache does not exist in validity_cache.db"
        rows = con.execute(
            "SELECT lock_sha256, package_name, resolved_version, resolved_commit FROM version_cache "
            "WHERE main_root = ? AND git_hash = ?",
            [str(repo), c_hash],
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == lock_sha
        assert rows[0][1] == "aminx"
        assert rows[0][2] == "0.3.0"

    # 4. Read back via _read_version_cache
    read_sha, read_pkgs = _read_version_cache(str(repo), c_hash)
    assert read_sha == lock_sha
    assert read_pkgs["aminx"][0] == "0.3.0"

    # 5. Remove JSON cache to prove DuckDB fallback is genuinely queried and non-mocked
    cache_file.unlink()
    assert not cache_file.exists()
    db_read_sha, db_read_pkgs = _read_version_cache(str(repo), c_hash)
    assert db_read_sha == lock_sha
    assert db_read_pkgs["aminx"][0] == "0.3.0"


# ---------------------------------------------------------------------------
# M1 Track 1: Diagnostic telemetry emission (Spec §6.2)
# ---------------------------------------------------------------------------
def test_validity_diagnostic_telemetry_emission(tmp_path: Path):
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r = Run(
        id="00000000-0000-0000-0000-000000000030",
        project_slug="projA",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash="abc1234",
        git_branch="main",
        git_dirty=False,
        status="completed",
    )
    write_run(r, cat_dir)
    compact(cat_dir)

    # Calling resolve_validity emits validity.resolved
    res = resolve_validity([r.id], cat_dir)
    assert r.id in res

    # Test telemetry helper wrappers without raising
    emit_validity_filter_applied(filter_name="test_filter", hidden_count=2, total_count=10)
    emit_validity_override_used(override_kind="test_override", entity_id=r.id, reason="test reason")


# ---------------------------------------------------------------------------
# Option A Regression Tests: Bug 1a, 1b, 1c
# ---------------------------------------------------------------------------
def test_bug1a_unknown_fixed_sha_and_multi_fixed_events(tmp_path: Path):
    """Bug 1a: Never treat git merge-base exit 128 on fixed SHA as unfixed; support multiple fixed events."""
    from bathos.version_resolver import commit_in_range, parse_instrument_registry

    repo = _make_git_repo(tmp_path / "asr")
    (repo / "src.txt").write_text("v1: buggy\n")
    subprocess.run(["git", "add", "src.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "buggy commit", "--no-verify"], cwd=repo, check=True)
    c_intro = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    (repo / "src.txt").write_text("v2: fixed on main\n")
    subprocess.run(["git", "add", "src.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "fix on main", "--no-verify"], cwd=repo, check=True)
    c_fix_main = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    (repo / "src.txt").write_text("v3: descendant of fix\n")
    subprocess.run(["git", "add", "src.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "post-fix work", "--no-verify"], cwd=repo, check=True)
    c_post_fix = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    # 1. Unknown/dangling fixed SHA (exit code 128) must NOT be treated as unfixed
    assert commit_in_range(c_post_fix, c_intro, "deadbeefdeadbeef", repo) is False

    # 2. Unknown introduced SHA must return False
    assert commit_in_range(c_post_fix, "deadbeefdeadbeef", None, repo) is False

    # 3. Multiple fixed SHAs (one dangling/other-worktree SHA + one valid ancestor fix on main)
    inst_toml = repo / "asr.toml"
    inst_toml.write_text(f"""schema_version = "1.0.0"
package = "asr"
ecosystem = "PyPI"

[[entries]]
id = "ASR-LG-2026-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{{ type = "GIT", events = [{{ introduced = "{c_intro}" }}, {{ fixed = "deadbeef0000" }}, {{ fixed = "{c_fix_main}" }}] }}]
""")
    reg = parse_instrument_registry(inst_toml)
    r_git = reg.entries[0].ranges[0]
    assert len(r_git.fixed_all) == 2
    assert commit_in_range(c_intro, r_git.introduced, r_git.fixed_all, repo) is True
    assert commit_in_range(c_fix_main, r_git.introduced, r_git.fixed_all, repo) is False
    assert commit_in_range(c_post_fix, r_git.introduced, r_git.fixed_all, repo) is False


def test_bug1b_external_pkg_no_fallback_to_project_git_hash_and_editable_self_pkg(tmp_path: Path):
    """Bug 1b: External pkg GIT range never uses project git_hash; editable self-pkg resolves inferred-from-commit."""
    from bathos.version_resolver import resolve_toolchain_version

    repo = _make_git_repo(tmp_path / "asr")
    (repo / ".bth.toml").write_text('[project]\nslug = "asr"\n')
    (repo / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "asr"\nversion = "0.1.0"\nsource = { editable = "." }\n'
        '[[package]]\nname = "aminx"\nversion = "0.2.1"\n'
    )
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "lock editable asr", "--no-verify"], cwd=repo, check=True)
    c_asr = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()
    lock_sha = hashlib.sha256((repo / "uv.lock").read_bytes()).hexdigest()

    run_dict = {
        "id": "00000000-0000-0000-0000-000000000091",
        "project_slug": "asr",
        "git_hash": c_asr,
        "dependency_lock_sha256": lock_sha,
    }
    # Editable self-package in uv.lock has no pinned commit SHA in uv.lock -> inferred-from-commit
    ver, commit, v_src = resolve_toolchain_version(run_dict, repo, "asr")
    assert ver == "0.1.0"
    assert commit == c_asr
    assert v_src == "inferred-from-commit"

    # External package GIT range with c_asr as introduced must NOT match a consuming project's git_hash
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text(f"""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-GIT-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{{ type = "GIT", events = [{{ introduced = "{c_asr}" }}] }}]
""")
    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)
    r = Run(
        id=run_dict["id"],
        project_slug="asr",
        command="python run.py",
        argv=["python", "run.py"],
        git_hash=c_asr,
        git_branch="main",
        git_dirty=False,
        status="completed",
        dependency_lock_sha256=lock_sha,
    )
    write_run(r, cat_dir)
    compact(cat_dir)

    res = resolve_validity([r.id], cat_dir, workspace_root=repo)
    assert res[r.id].state == "current"


def test_bug1c_multi_package_highest_severity_and_non_consumer_script_protection(tmp_path: Path):
    """Bug 1c: Evaluate all packages for highest severity; skip external pkg defects when script doesn't use pkg."""
    repo = _make_git_repo(tmp_path / "tev_design")
    (repo / ".bth.toml").write_text('[project]\nslug = "tev_design"\n')
    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)

    # Script that does NOT import or mention aminx
    pure_script = exp_dir / "pure_numpy.py"
    pure_script.write_text("import math\nprint(math.pi)\n")
    # Script that DOES import aminx
    consumer_script = exp_dir / "use_aminx.py"
    consumer_script.write_text("import aminx\nprint('using aminx')\n")

    (repo / "uv.lock").write_text(
        'version = 1\n[[package]]\nname = "aminx"\nversion = "0.1.0a15"\n'
        '[[package]]\nname = "otherpkg"\nversion = "1.0.0"\n'
    )
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    # Package 1 (aa_first alphabetically): suspect defect (missing predicate field)
    (inst_dir / "aa_first.toml").write_text("""schema_version = "1.0.0"
package = "otherpkg"
ecosystem = "PyPI"

[[entries]]
id = "OTHER-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = false
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.5.0" }, { fixed = "2.0.0" }] }]
[entries.predicate]
condition = "missing_col > 1"
""")
    # Package 2 (zz_second alphabetically): void defect without predicate
    (inst_dir / "zz_second.toml").write_text("""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-001"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{ type = "ECOSYSTEM", events = [{ introduced = "0.1.0a0" }, { fixed = "0.1.0a23" }] }]
[entries.predicate]
condition = "sample_s > 1"
""")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "setup", "--no-verify"], cwd=repo, check=True)
    c_sha = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    # Run 1: executes pure_numpy.py (does not import aminx or otherpkg) -> must stay current!
    r_pure = Run(
        id="00000000-0000-0000-0000-000000000092",
        project_slug="tev_design",
        command="python scripts/experiments/pure_numpy.py",
        argv=["python", "scripts/experiments/pure_numpy.py"],
        git_hash=c_sha,
        git_branch="main",
        git_dirty=False,
        status="completed",
    )
    # Run 2: executes use_aminx.py and explicitly records both packages -> must pick void over suspect!
    r_both = Run(
        id="00000000-0000-0000-0000-000000000093",
        project_slug="tev_design",
        command="python scripts/experiments/use_aminx.py",
        argv=["python", "scripts/experiments/use_aminx.py"],
        git_hash=c_sha,
        git_branch="main",
        git_dirty=False,
        status="completed",
        metadata=json.dumps({"otherpkg_version": "1.0.0", "aminx_version": "0.1.0a15"}),
    )
    write_run(r_pure, cat_dir)
    write_run(r_both, cat_dir)
    compact(cat_dir)

    res = resolve_validity([r_pure.id, r_both.id], cat_dir, workspace_root=repo)
    assert res[r_pure.id].state == "current"
    assert res[r_both.id].state == "void"
    assert res[r_both.id].defect_id == "AMINX-001"


def test_bug1b_and_1c_sibling_git_repo_and_depth3_transitive_imports(tmp_path: Path):
    """Verify sibling git repo resolution for external GIT ranges and depth-3 from..import local transitive imports."""
    # 1. Create sibling external package git repository (tmp_path / "aminx")
    aminx_repo = _make_git_repo(tmp_path / "aminx")
    (aminx_repo / "core.py").write_text("v1 = 'buggy'\n")
    subprocess.run(["git", "add", "."], cwd=aminx_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "aminx buggy", "--no-verify"], cwd=aminx_repo, check=True)
    c_aminx_bug = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=aminx_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    (aminx_repo / "core.py").write_text("v2 = 'fixed'\n")
    subprocess.run(["git", "add", "."], cwd=aminx_repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "aminx fixed", "--no-verify"], cwd=aminx_repo, check=True)
    c_aminx_fix = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=aminx_repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    # 2. Create consuming project git repository (tmp_path / "tev_design")
    repo = _make_git_repo(tmp_path / "tev_design")
    (repo / ".bth.toml").write_text('[project]\nslug = "tev_design"\n')
    exp_dir = repo / "scripts" / "experiments"
    exp_dir.mkdir(parents=True)

    # 3-hop local import chain: entry.py -> from scripts.experiments import hop1 -> from .hop2 import run_fn -> import aminx
    (exp_dir / "hop2.py").write_text("import aminx\ndef run_fn(): return 1\n")
    (exp_dir / "hop1.py").write_text("from .hop2 import run_fn\n")
    (exp_dir / "entry.py").write_text("from scripts.experiments import hop1\nhop1.run_fn()\n")

    (repo / "uv.lock").write_text(
        f'version = 1\n[[package]]\nname = "aminx"\nversion = "0.1.0a24"\nsource = {{ commit = "{c_aminx_bug}" }}\n'
    )
    inst_dir = repo / ".bth" / "instruments"
    inst_dir.mkdir(parents=True)
    (inst_dir / "aminx.toml").write_text(f"""schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-GIT-SIBLING"
revision = 1
kind = "defect"
severity = "void"
hide_inferred = true
ranges = [{{ type = "GIT", events = [{{ introduced = "{c_aminx_bug}" }}, {{ fixed = "{c_aminx_fix}" }}] }}]
""")
    subprocess.run(["git", "add", "."], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init tev_design", "--no-verify"], cwd=repo, check=True)
    c_tev = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()

    cat_dir = tmp_path / "catalog"
    init_catalog(cat_dir)

    r_transitive = Run(
        id="00000000-0000-0000-0000-000000000094",
        project_slug="tev_design",
        command="python scripts/experiments/entry.py",
        argv=["python", "scripts/experiments/entry.py"],
        git_hash=c_tev,
        git_branch="main",
        git_dirty=False,
        status="completed",
    )
    write_run(r_transitive, cat_dir)
    compact(cat_dir)

    res = resolve_validity([r_transitive.id], cat_dir, workspace_root=repo)
    assert res[r_transitive.id].state == "void"
    assert res[r_transitive.id].defect_id == "AMINX-GIT-SIBLING"


