"""Tests for Option E runtime output binding, external adopt, run_context localization,
shared-FS safety, sidecar cache, and internal reader localization.

Covers AC-2, AC-7, AC-10, AC-11, NC-6.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path

import pytest
from rich.console import Console

from bathos.checker import check_output_sha_drift, output_metadata_has_sha_drift
from bathos.cite import format_citation
from bathos.claim import _resolve_member_sidecar, parity_confound_check
from bathos.cli_cyclopts import app
from bathos.errors import RemoteOutputsUndeclaredError
from bathos.index import connect_read
from bathos.mcp import (
    get_run_tool,
    mcp_outputs_adopt_tool,
    mcp_reap_tool,
    mcp_run_tool,
)
from bathos.mcp_auth import get_or_create_token
from bathos.output_tiers.adopt import adopt_outputs, hash_outputs
from bathos.output_tiers.reference import (
    PHASE_RUN_END,
    OutputTierError,
    check_root_syntax,
    hash_file,
)
from bathos.output_tiers.run_context import WORKTREE_UNKNOWN, open_run
from bathos.prereg import verify_run_manifest_detail
from bathos.query import get_run
from bathos.rich_fmt import render_run_detail
from bathos.run_bundle import export_run_bundle, import_run_bundle
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.runner import run_script
from bathos.schema import Run
from tests._cyclopts_runner import CyclopticRunner
from tests.runlog.conftest import make_git_repo, write_bth_toml

runner = CyclopticRunner()


def _init_log_catalog(cat_dir: Path) -> None:
    cat_dir.mkdir(parents=True, exist_ok=True)
    cutover_marker_path(cat_dir).write_text(
        json.dumps({"at": "2026-11-25T00:00:00Z", "bathos": "test", "attempt": "1", "segments": []}),
        encoding="utf-8",
    )


def _setup_repo(root: Path, slug: str = "proj") -> tuple[Path, str]:
    repo = make_git_repo(root)
    write_bth_toml(repo, slug=slug)
    assign_project_id(repo)
    pid = read_project_id(repo / ".bth.toml")
    assert pid is not None
    register_main_root(repo)
    return repo, pid


def _create_finished_run(
    *,
    repo: Path,
    pid: str,
    slug: str,
    run_id: str,
    outputs_declared: bool = False,
    with_run_end: bool = True,
    hostname: str = "titanix.mit.edu",
    parity_type: str | None = None,
) -> tuple[str, str]:
    sc_rel = "scripts/experiments/probe.bth.toml"
    sc_text = '[experiment]\nhypothesis = "h"\n[result_schema]\nx = "float"\n'
    if parity_type:
        sc_text += f'[parity]\nrun_type = "{parity_type}"\n'
    sc_file = repo / sc_rel
    sc_file.parent.mkdir(parents=True, exist_ok=True)
    sc_file.write_text(sc_text, encoding="utf-8")
    sc_sha = hashlib.sha256(sc_text.encode("utf-8")).hexdigest()

    out_rel = "outputs/res.parquet"
    out_file = repo / out_rel
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(b"parquet-payload-1\n")
    out_sha = hash_file(out_file)

    common = dict(
        main_root=repo,
        worktree_root=repo,
        project=slug,
        project_id=pid,
        writer=f"{hostname}.1.1",
    )
    ts0 = "2026-11-25T10:00:00.000000Z"
    ts1 = "2026-11-25T10:01:00.000000Z"
    evs = [
        build_envelope(
            kind="run.started",
            entity=[run_id],
            data={
                "id": run_id,
                "project_slug": slug,
                "command": "python probe.py",
                "argv": ["python", "probe.py"],
                "git_hash": "abc1234",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": ts0,
                "hostname": hostname,
                "sidecar_sha256": sc_sha,
                "sidecar_path": sc_rel,
                "outputs_declared": outputs_declared,
                "parity_run_type": parity_type,
            },
            seq=1,
            ts=ts0,
            eid=f"e-{run_id[:8]}-1",
            **common,
        )
    ]
    if with_run_end:
        evs.append(
            build_envelope(
                kind="run.outputs_hashed",
                entity=[run_id],
                data={
                    "phase": PHASE_RUN_END,
                    "output_metadata": [
                        {
                            "path": str(out_file),
                            "relpath": out_rel,
                            "status": "present",
                            "size_bytes": out_file.stat().st_size,
                            "mtime_unix": 1700000000.0,
                            "sha256": out_sha,
                            "phase": PHASE_RUN_END,
                        }
                    ],
                },
                seq=2,
                ts=ts1,
                eid=f"e-{run_id[:8]}-2",
                **common,
            )
        )
    evs.append(
        build_envelope(
            kind="run.finished",
            entity=[run_id],
            data={
                "id": run_id,
                "project_slug": slug,
                "command": "python probe.py",
                "argv": ["python", "probe.py"],
                "git_hash": "abc1234",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": ts0,
                "status": "completed",
                "exit_code": 0,
                "duration_s": 12.3,
                "outcome": "pass",
                "hostname": hostname,
                "sidecar_sha256": sc_sha,
                "sidecar_path": sc_rel,
                "output_paths": [str(out_file)] if with_run_end else [],
                "parity_run_type": parity_type,
            },
            seq=3,
            ts=ts1,
            eid=f"e-{run_id[:8]}-3",
            **common,
        )
    )
    seg = repo / ".bth" / "log" / f"seg-{run_id[:8]}.jsonl"
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in evs), encoding="utf-8")
    return sc_sha, out_sha


# ---------------------------------------------------------------------------
# 1. check_root_syntax & RunContext localization + shared-FS safety
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["a\x00b", ".", "./a", "a/./b", "a//b"])
def test_check_root_syntax_closes_nul_dot_and_double_slash_gaps(bad: str):
    assert check_root_syntax(bad) is not None


def test_run_context_imported_receipt_and_shared_fs_not_misclassified(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    rem_cat = tmp_path / "rem_cat"
    _init_log_catalog(rem_cat)
    rem_repo, pid = _setup_repo(tmp_path / "rem_repo", slug="proj_ctx")
    rid = "11112222-0000-4000-8000-000000000001"
    _create_finished_run(repo=rem_repo, pid=pid, slug="proj_ctx", run_id=rid, outputs_declared=True)

    bundle = tmp_path / "run.bthbundle"
    export_run_bundle(rid, bundle, catalog_dir=rem_cat)

    # Local repo with matching output file and project_id
    (tmp_path / "home" / ".bth" / "projects.toml").unlink(missing_ok=True)
    loc_cat = tmp_path / "loc_cat"
    _init_log_catalog(loc_cat)
    loc_repo = make_git_repo(tmp_path / "loc_repo")
    (loc_repo / ".bth.toml").write_text(f'[project]\nslug = "proj_ctx"\nid = "{pid}"\n', encoding="utf-8")
    (loc_repo / "outputs").mkdir(parents=True, exist_ok=True)
    (loc_repo / "outputs" / "res.parquet").write_bytes(b"parquet-payload-1\n")
    register_main_root(loc_repo)

    import_run_bundle(bundle, worktree_root=loc_repo, catalog_dir=loc_cat)

    # Imported run resolves local_worktree_root automatically from import_receipts
    ctx = open_run(loc_cat, rid[:8])
    assert ctx.imported is True
    assert ctx.root_source == "local_resolved"
    assert ctx.worktree_root == loc_repo.resolve()
    hashed = hash_outputs(loc_cat, rid[:8])
    assert hashed["all_match"] is True
    assert hashed["worktree_root_source"] == "local_resolved"

    # If receipt had no local_worktree_root, open_run raises WORKTREE_UNKNOWN mentioning --worktree-root
    for rp in (loc_repo / ".bth" / "log" / "imported").rglob("*.receipt.json"):
        data = json.loads(rp.read_text(encoding="utf-8"))
        data["local_worktree_root"] = None
        rp.write_text(json.dumps(data), encoding="utf-8")
    run_ingest(loc_cat)
    with pytest.raises(OutputTierError) as exc_info:
        open_run(loc_cat, rid[:8])
    assert exc_info.value.error_code == WORKTREE_UNKNOWN
    assert "--worktree-root" in exc_info.value.detail

    # Same-cluster shared-FS run (hostname != local, sitting in local project root): MUST NOT flip imported=True!
    shared_cat = tmp_path / "shared_cat"
    _init_log_catalog(shared_cat)
    shared_repo, spid = _setup_repo(tmp_path / "shared_repo", slug="proj_shared")
    rid_shared = "22223333-0000-4000-8000-000000000002"
    _create_finished_run(
        repo=shared_repo,
        pid=spid,
        slug="proj_shared",
        run_id=rid_shared,
        outputs_declared=True,
        hostname="compute-node-999",
    )
    run_ingest(shared_cat)
    shared_ctx = open_run(shared_cat, rid_shared[:8])
    assert shared_ctx.imported is False
    assert shared_ctx.root_source == "recorded"


# ---------------------------------------------------------------------------
# 2. External adopt (`--allow-external-root --relpath-base`) & NC-6
# ---------------------------------------------------------------------------


def test_adopt_allow_external_root_and_nc6_safety(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    cat = tmp_path / "cat"
    _init_log_catalog(cat)
    repo, pid = _setup_repo(tmp_path / "repo", slug="proj_ext")
    rid = "33334444-0000-4000-8000-000000000003"
    _create_finished_run(
        repo=repo, pid=pid, slug="proj_ext", run_id=rid, outputs_declared=False, with_run_end=False
    )
    run_ingest(cat)

    ext_base = tmp_path / "external_scratch"
    ext_sub = ext_base / "run_data"
    ext_sub.mkdir(parents=True)
    (ext_sub / "metrics.csv").write_text("a,b\n1,2\n", encoding="utf-8")

    # Without --allow-external-root -> ROOT_OUTSIDE_WORKTREE
    with pytest.raises(OutputTierError) as exc1:
        adopt_outputs(cat, rid[:8], [str(ext_sub)], "ext test")
    assert exc1.value.error_code == "root_outside_worktree"

    # --relpath-base without --allow-external-root -> INVALID_PARAM
    with pytest.raises(OutputTierError) as exc2:
        adopt_outputs(cat, rid[:8], ["run_data"], "ext test", relpath_base=str(ext_base))
    assert exc2.value.error_code == "invalid_param"

    # --allow-external-root without --relpath-base -> INVALID_PARAM
    with pytest.raises(OutputTierError) as exc3:
        adopt_outputs(cat, rid[:8], ["run_data"], "ext test", allow_external_root=True)
    assert exc3.value.error_code == "invalid_param"

    # Valid external adopt (AC-11)
    res = adopt_outputs(
        cat,
        rid[:8],
        ["run_data"],
        "external scratch results",
        allow_external_root=True,
        relpath_base=str(ext_base),
    )
    assert res["ok"] is True
    assert res["adopted_from_external_root"] is True
    assert res["external_root"] == str(ext_base.resolve())
    assert res["files"] == ["run_data/metrics.csv"]

    # hash_outputs verifies against external_root as match
    h1 = hash_outputs(cat, rid[:8])
    assert h1["all_match"] is True
    assert h1["files"][0]["status"] == "match"
    assert h1["files"][0]["external_root"] == str(ext_base.resolve())

    # Mutating external file is detected as drift
    (ext_sub / "metrics.csv").write_text("a,b\n1,999\n", encoding="utf-8")
    h2 = hash_outputs(cat, rid[:8])
    assert h2["counts"]["drift"] == 1

    # NC-6: colliding relpath under a DIFFERENT external_root is refused
    ext_base2 = tmp_path / "external_scratch_2"
    (ext_base2 / "run_data").mkdir(parents=True)
    (ext_base2 / "run_data" / "metrics.csv").write_text("other", encoding="utf-8")
    with pytest.raises(OutputTierError) as exc_col:
        adopt_outputs(
            cat,
            rid[:8],
            ["run_data"],
            "collision",
            allow_external_root=True,
            relpath_base=str(ext_base2),
        )
    assert exc_col.value.error_code == "invalid_param"

    # NC-6: symlink inside external root is refused (SYMLINK_IN_OUTPUTS)
    (ext_sub / "bad_link.txt").symlink_to(ext_sub / "metrics.csv")
    with pytest.raises(OutputTierError) as exc_sym:
        adopt_outputs(
            cat,
            rid[:8],
            ["run_data"],
            "symlink",
            allow_external_root=True,
            relpath_base=str(ext_base),
        )
    assert exc_sym.value.error_code == "symlink_in_outputs"
    (ext_sub / "bad_link.txt").unlink()

    # NC-6: symlink relpath_base is refused
    sym_base = tmp_path / "sym_base"
    sym_base.symlink_to(ext_base)
    with pytest.raises(OutputTierError) as exc_sbase:
        adopt_outputs(
            cat,
            rid[:8],
            ["run_data"],
            "symlink base",
            allow_external_root=True,
            relpath_base=str(sym_base),
        )
    assert exc_sbase.value.error_code == "symlink_in_outputs"

    # NC-6: special file (FIFO) inside external root is refused (SPECIAL_FILE_IN_OUTPUTS)
    fifo_path = ext_sub / "pipe.fifo"
    os.mkfifo(fifo_path)
    with pytest.raises(OutputTierError) as exc_fifo:
        adopt_outputs(
            cat,
            rid[:8],
            ["run_data"],
            "fifo",
            allow_external_root=True,
            relpath_base=str(ext_base),
        )
    assert exc_fifo.value.error_code == "special_file_in_outputs"
    fifo_path.unlink()

    # CLI & MCP wiring for outputs adopt --allow-external-root --relpath-base
    ext_extra = ext_base / "extra"
    ext_extra.mkdir()
    (ext_extra / "extra.txt").write_text("extra", encoding="utf-8")
    cli_res = runner.invoke(
        app,
        [
            "outputs",
            "adopt",
            rid[:8],
            "--root",
            "extra",
            "--reason",
            "cli ext",
            "--allow-external-root",
            "--relpath-base",
            str(ext_base),
            "--catalog-dir",
            str(cat),
        ],
    )
    assert cli_res.exit_code == 0, cli_res.output
    assert json.loads(cli_res.output)["files"] == ["extra/extra.txt"]

    mcp_res = asyncio.run(
        mcp_outputs_adopt_tool(
            run_id=rid[:8],
            root=["extra"],
            reason="mcp ext",
            allow_external_root=True,
            relpath_base=str(ext_base),
            dry_run=True,
            catalog_dir=str(cat),
        )
    )
    assert mcp_res["ok"] is True


# ---------------------------------------------------------------------------
# 3. Option E runtime binding & sidecar cache (`runner.py`)
# ---------------------------------------------------------------------------


def test_runner_remote_strict_output_binding_and_sidecar_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    cat = tmp_path / "cat"
    _init_log_catalog(cat)
    repo, _pid = _setup_repo(tmp_path / "repo", slug="proj_run")

    script = repo / "scripts" / "explore" / "probe.py"
    script.parent.mkdir(parents=True, exist_ok=True)
    script.write_text(
        "import json, os\n"
        "from pathlib import Path\n"
        "Path('outputs').mkdir(exist_ok=True)\n"
        "Path('outputs/out.txt').write_text('ok')\n"
        "Path(os.environ['BTH_RESULTS_PATH']).write_text(json.dumps({'x': 1.0}))\n",
        encoding="utf-8",
    )
    sc = repo / "scripts" / "explore" / "probe.bth.toml"
    sc_text = '[experiment]\nhypothesis = "h"\n[outcomes.pass]\ncondition = "x > 0"\n[outcomes.fail]\ncondition = "x <= 0"\nis_residual = true\n[result_schema]\nx = "float"\n'
    sc.write_text(sc_text, encoding="utf-8")
    sc_sha = hashlib.sha256(sc_text.encode("utf-8")).hexdigest()

    # 1. Under BTH_REMOTE_LAUNCH=1 with no declared outputs -> RemoteOutputsUndeclaredError
    monkeypatch.setenv("BTH_REMOTE_LAUNCH", "1")
    monkeypatch.delenv("BTH_OUTPUT_DIR", raising=False)
    with pytest.raises(RemoteOutputsUndeclaredError):
        run_script(["python", str(script)], "proj_run", cat, [], [], cwd=repo)

    # CLI surfaces error cleanly
    monkeypatch.chdir(repo)
    cli_fail = runner.invoke(
        app, ["run", "--catalog-dir", str(cat), str(script)]
    )
    assert cli_fail.exit_code != 0
    assert "remote_outputs_undeclared" in cli_fail.output

    # MCP surfaces error code cleanly
    tok = get_or_create_token()
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(tmp_path / "home" / ".bth" / "mcp_token"))
    tok = get_or_create_token()
    mcp_fail = asyncio.run(
        mcp_run_tool(script_path=str(script), project_slug="proj_run", catalog_dir=str(cat), token=tok)
    )
    assert mcp_fail["ok"] is False
    assert mcp_fail["error_code"] == "remote_outputs_undeclared"

    # 2. --bth-allow-unbound-outputs allows run AND caches sidecar under ~/.bth/sidecar-cache/<sha[:2]>/
    cli_ok = runner.invoke(
        app, ["run", "--bth-allow-unbound-outputs", "--catalog-dir", str(cat), str(script)]
    )
    assert cli_ok.exit_code == 0, cli_ok.output
    cached_sc = tmp_path / "home" / ".bth" / "sidecar-cache" / sc_sha[:2] / f"{sc_sha}.bth.toml"
    assert cached_sc.is_file()
    assert cached_sc.read_text(encoding="utf-8") == sc_text

    # Delete original sidecar to verify bundle export recovers from ~/.bth/sidecar-cache/<sha[:2]>/
    run_id = json.loads(cli_ok.output)["run_id"]
    sc.unlink()
    bundle_out = tmp_path / "cached.bthbundle"
    exp = export_run_bundle(run_id, bundle_out, catalog_dir=cat)
    assert exp["sidecar_included"] is True

    # 3. Under BTH_REMOTE_LAUNCH=1, run_end_payload `refused` fails the run (exit non-zero)
    sc_sym = repo / "scripts" / "explore" / "probe.bth.toml"
    sc_sym.write_text(sc_text + '\n[outputs]\nroots = ["outputs/bad_dir"]\n', encoding="utf-8")
    bad_dir = repo / "outputs" / "bad_dir"
    bad_dir.mkdir(parents=True, exist_ok=True)
    (repo / "outputs" / "real.txt").write_text("hi", encoding="utf-8")
    (bad_dir / "link.txt").symlink_to(repo / "outputs" / "real.txt")

    rc_refused = run_script(["python", str(script)], "proj_run", cat, [], [], cwd=repo)
    assert rc_refused != 0


# ---------------------------------------------------------------------------
# 4. Internal readers (`checker`, `claim`, `prereg`, `rich_fmt`, `cite`, `get_run`, `reap`)
# ---------------------------------------------------------------------------


def test_internal_readers_and_display_for_imported_run(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    rem_cat = tmp_path / "rem_cat"
    _init_log_catalog(rem_cat)
    rem_repo, pid = _setup_repo(tmp_path / "rem_repo", slug="proj_rdr")
    rid = "44445555-0000-4000-8000-000000000004"
    sc_sha, _out_sha = _create_finished_run(
        repo=rem_repo,
        pid=pid,
        slug="proj_rdr",
        run_id=rid,
        outputs_declared=True,
        parity_type="literature_parity",
    )

    bundle = tmp_path / "rdr.bthbundle"
    export_run_bundle(rid, bundle, catalog_dir=rem_cat)

    # Import into a fresh catalog with a local checkout holding matching outputs/res.parquet
    (tmp_path / "home" / ".bth" / "projects.toml").unlink(missing_ok=True)
    loc_cat = tmp_path / "loc_cat"
    _init_log_catalog(loc_cat)
    loc_repo = make_git_repo(tmp_path / "loc_repo")
    (loc_repo / ".bth.toml").write_text(f'[project]\nslug = "proj_rdr"\nid = "{pid}"\n', encoding="utf-8")
    (loc_repo / "outputs").mkdir(parents=True, exist_ok=True)
    (loc_repo / "outputs" / "res.parquet").write_bytes(b"parquet-payload-1\n")
    register_main_root(loc_repo)

    import_run_bundle(bundle, worktree_root=loc_repo, catalog_dir=loc_cat)

    run = get_run(rid, loc_cat)
    assert run is not None

    # Remove remote repo so entry["path"] does NOT exist on disk; checker must resolve via relpath!
    import shutil

    shutil.rmtree(rem_repo)
    drifts = check_output_sha_drift(run, catalog_dir=loc_cat)
    assert len(drifts) == 1
    assert drifts[0].status == "OK"
    assert not output_metadata_has_sha_drift(run.output_metadata, run_id=rid, catalog_dir=loc_cat)

    # claim.py parity_confound_check must stay "controlled" (not downgraded to "uncontrolled")
    claim_file = loc_repo / ".bth" / "claims" / "c1.bth.toml"
    claim_file.parent.mkdir(parents=True, exist_ok=True)
    claim_file.write_text(
        f'[claim]\nid = "c1"\nhypothesis = "h"\n\n'
        f'[[confounds]]\nid = "parity"\nlabel = "lit"\n'
        f'[confounds.reference_parity]\nparity_run_id = "{rid}"\n',
        encoding="utf-8",
    )
    con = connect_read(loc_cat, wire_validity=False)
    try:
        pchk = parity_confound_check(claim_file, db=con)
    finally:
        con.close()
    assert pchk["confounds"][0]["status"] == "controlled"

    # claim.py _resolve_member_sidecar resolves from imported_sidecars when not in loc_repo
    sc_obj = _resolve_member_sidecar(
        None,
        run_id=rid,
        sidecar_path=run.sidecar_path,
        sidecar_sha256=sc_sha,
        workspace_root=loc_repo,
        catalog_dir=loc_cat,
    )
    assert sc_obj is not None
    assert sc_obj.hypothesis == "h"

    # prereg.py verify_run_manifest_detail handles remote common_dir gracefully (no crash)
    foreign_manifest_run = Run(
        **{
            **{f.name: getattr(run, f.name) for f in Run.__dataclass_fields__.values()},
            "manifest_sha256": "f" * 64,
            "manifest_path": f"git:/nonexistent/titanix/repo/.git#refs/bathos/locks/{rid}",
        }
    )
    mv = verify_run_manifest_detail(foreign_manifest_run)
    assert mv.ok is False
    assert mv.reason == "lock_unlocatable"

    # AC-2 display surfaces: render_run_detail, format_citation, get_run_tool
    buf = io.StringIO()
    render_run_detail(run, console=Console(file=buf, force_terminal=False, width=160))
    rendered = buf.getvalue()
    assert f"Imported from: {run.imported_from_host}" in rendered

    md_cite = format_citation(run, fmt="markdown")
    assert f"Imported from:    {run.imported_from_host}" in md_cite
    json_cite = json.loads(format_citation(run, fmt="json"))
    assert json_cite["imported_from_host"] == run.imported_from_host

    shown = get_run_tool(run_id=rid, catalog_dir=str(loc_cat))
    assert shown["imported_from_host"] == run.imported_from_host
    assert shown["resolved_sidecar_path"] is not None

    # CLI & MCP reap --include-remote wiring
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(tmp_path / "home" / ".bth" / "mcp_token"))
    tok = get_or_create_token()
    reap_cli = runner.invoke(app, ["catalog", "reap", "--include-remote", "--catalog-dir", str(loc_cat)])
    assert reap_cli.exit_code == 0, reap_cli.output
    reap_mcp = asyncio.run(mcp_reap_tool(catalog_dir=str(loc_cat), include_remote=True, token=tok))
    assert reap_mcp["ok"] is True
