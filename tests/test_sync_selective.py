"""Tests for Option A selective remote pull, mirror fallback, whole-remote ingest,
linked-worktree `bth submit --wait --then-sync`, and CLI/MCP bundle & sync wiring.

Covers AC-3, AC-4, AC-5, AC-6, NC-7, NC-8, NC-10.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bathos.cli_cyclopts import app
from bathos.config import ProjectConfig
from bathos.errors import RunBundleInvalidError, RunImportConflictError
from bathos.mcp import (
    get_run_tool,
    mcp_bundle_export_tool,
    mcp_bundle_import_tool,
    mcp_sync_tool,
    sync_tool,
)
from bathos.mcp_auth import get_or_create_token
from bathos.output_tiers.reference import PHASE_RUN_END, hash_file
from bathos.query import get_run, latest_import_receipt, run_sql
from bathos.run_bundle import export_run_bundle
from bathos.runlog.envelope import build_envelope
from bathos.runlog.ingest import run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.sync import selective_pull_runs
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


def _write_run_seg(
    *,
    repo: Path,
    slug: str,
    pid: str,
    run_id: str,
    ts_start: str,
    ts_end: str,
    hostname: str = "titanix.mit.edu",
    status: str = "completed",
    include_finished: bool = True,
    with_sidecar: bool = True,
    campaign_id: str = "",
    seg_name: str | None = None,
) -> tuple[list[dict], str]:
    sc_rel = f"scripts/experiments/probe_{run_id[:8]}.bth.toml"
    sc_sha = ""
    if with_sidecar:
        sc_text = f'[experiment]\nhypothesis = "h-{run_id[:8]}"\n[result_schema]\nx = "float"\n'
        sc_file = repo / sc_rel
        sc_file.parent.mkdir(parents=True, exist_ok=True)
        sc_file.write_text(sc_text, encoding="utf-8")
        sc_sha = hashlib.sha256(sc_text.encode("utf-8")).hexdigest()
    else:
        sc_sha = "a" * 64

    out_rel = f"outputs/{run_id[:8]}.txt"
    out_file = repo / out_rel
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(f"out-{run_id}\n".encode())
    out_sha = hash_file(out_file)

    common = dict(
        main_root=repo,
        worktree_root=repo,
        project=slug,
        project_id=pid,
        writer=f"{hostname}.1.1",
    )
    evs: list[dict] = []
    seq = 0

    def _add(kind: str, entity: list[str], data: dict, ts: str) -> None:
        nonlocal seq
        seq += 1
        evs.append(
            build_envelope(
                kind=kind,
                entity=entity,
                data=data,
                seq=seq,
                ts=ts,
                eid=f"eid-{run_id[:8]}-{seq}",
                **common,
            )
        )

    _add(
        "run.started",
        [run_id],
        {
            "id": run_id,
            "project_slug": slug,
            "command": f"python probe_{run_id[:8]}.py",
            "argv": ["python", f"probe_{run_id[:8]}.py"],
            "git_hash": "abc1234",
            "git_branch": "main",
            "git_dirty": False,
            "timestamp": ts_start,
            "hostname": hostname,
            "sidecar_sha256": sc_sha,
            "sidecar_path": sc_rel,
            "campaign_id": campaign_id or None,
        },
        ts_start,
    )
    if campaign_id:
        _add(
            "campaign.created",
            [campaign_id],
            {"id": campaign_id, "name": "camp1", "mode": "exploration", "project_slug": slug},
            ts_start,
        )
        _add(
            "campaign.run_added",
            [campaign_id, run_id],
            {"campaign_id": campaign_id, "run_id": run_id, "seq_position": 1},
            ts_start,
        )
    _add(
        "run.outputs_hashed",
        [run_id],
        {
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
        ts_end,
    )
    if include_finished:
        _add(
            "run.finished",
            [run_id],
            {
                "id": run_id,
                "project_slug": slug,
                "command": f"python probe_{run_id[:8]}.py",
                "argv": ["python", f"probe_{run_id[:8]}.py"],
                "git_hash": "abc1234",
                "git_branch": "main",
                "git_dirty": False,
                "timestamp": ts_start,
                "status": status,
                "exit_code": 0,
                "duration_s": 5.0,
                "outcome": "pass",
                "hostname": hostname,
                "sidecar_sha256": sc_sha,
                "sidecar_path": sc_rel,
                "output_paths": [str(out_file)],
                "campaign_id": campaign_id or None,
            },
            ts_end,
        )
    seg = repo / ".bth" / "log" / (seg_name or f"seg-{run_id[:8]}.jsonl")
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in evs), encoding="utf-8")
    return evs, sc_sha


# ---------------------------------------------------------------------------
# NC-10 static check
# ---------------------------------------------------------------------------


def test_no_import_myxcel_in_src():
    """NC-10: codebase must contain zero `import myxcel` statements."""
    import ast

    src_root = Path(__file__).resolve().parents[1] / "src" / "bathos"
    for py_file in src_root.rglob("*.py"):
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name != "myxcel" and not alias.name.startswith("myxcel."), (
                        f"Forbidden `import myxcel` in {py_file}:{node.lineno}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module != "myxcel" and not node.module.startswith("myxcel."), (
                    f"Forbidden `from myxcel import ...` in {py_file}:{node.lineno}"
                )


# ---------------------------------------------------------------------------
# Option A Primary Selective Pull (AC-3, AC-4, AC-6)
# ---------------------------------------------------------------------------


def test_selective_pull_primary_single_and_multi_run_with_project_and_since(
    tmp_path: Path, monkeypatch
):
    # Remote multi-project workspace + remote catalog (with its own HOME)
    rem_home = tmp_path / "remote_home"
    loc_home = tmp_path / "home"
    monkeypatch.setenv("HOME", str(rem_home))
    remote_cat = tmp_path / "remote_cat"
    _init_log_catalog(remote_cat)
    rem_a, pid_a = _setup_repo(tmp_path / "remote_a", slug="proj_a")
    rem_b, pid_b = _setup_repo(tmp_path / "remote_b", slug="proj_b")

    now = datetime.now(UTC)
    t_recent = (now - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
    t_old = (now - timedelta(days=10)).strftime("%Y-%m-%dT%H:%M:%S.000000Z")

    rid_a1 = "aaaa1111-0000-4000-8000-000000000001"
    rid_a2 = "bbbb2222-0000-4000-8000-000000000002"
    # Colliding prefix in proj_b!
    rid_b1 = "aaaa1111-0000-4000-8000-000000000099"

    _write_run_seg(repo=rem_a, slug="proj_a", pid=pid_a, run_id=rid_a1, ts_start=t_recent, ts_end=t_recent)
    _write_run_seg(repo=rem_a, slug="proj_a", pid=pid_a, run_id=rid_a2, ts_start=t_old, ts_end=t_old)
    _write_run_seg(repo=rem_b, slug="proj_b", pid=pid_b, run_id=rid_b1, ts_start=t_recent, ts_end=t_recent)

    # Local host has its own HOME, projects.toml, and catalog
    monkeypatch.setenv("HOME", str(loc_home))
    local_cat = tmp_path / "local_cat"
    _init_log_catalog(local_cat)
    local_a, _ = _setup_repo(tmp_path / "local_a", slug="proj_a")
    # Match project_id on local_a to remote_a by overwriting .bth.toml id cleanly
    cfg_txt = (local_a / ".bth.toml").read_text(encoding="utf-8")
    local_pid_line = next(line for line in cfg_txt.splitlines() if line.startswith("id = "))
    (local_a / ".bth.toml").write_text(
        cfg_txt.replace(local_pid_line, f'id = "{pid_a}"')
        + '\n[remotes.titanix]\nhost = "titanix"\nremote_root = "~/projects/proj_a"\n',
        encoding="utf-8",
    )
    register_main_root(local_a)

    remote_meta = {
        rid_a1: {"id": rid_a1, "project_slug": "proj_a", "timestamp": t_recent},
        rid_a2: {"id": rid_a2, "project_slug": "proj_a", "timestamp": t_old},
        rid_b1: {"id": rid_b1, "project_slug": "proj_b", "timestamp": t_recent},
    }
    remote_staged_store = tmp_path / "remote_home_tmp"
    ssh_calls: list[list[str]] = []
    real_subprocess_run = subprocess.run

    def fake_ssh_run(argv, **kwargs):
        if not argv or argv[0] != "ssh":
            return real_subprocess_run(argv, **kwargs)
        ssh_calls.append(list(argv))
        assert "-o" in argv and "BatchMode=yes" in argv
        remote_cmd = argv[-1]
        if remote_cmd.startswith("rm -rf "):
            for d in remote_staged_store.glob("sync-*"):
                shutil.rmtree(d, ignore_errors=True)
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

        # Extract JSON arg passed to _REMOTE_SELECTIVE_EXPORT_PY
        import shlex

        cfg = json.loads(shlex.split(remote_cmd)[-1])
        runs = list(remote_meta.values())
        if cfg["project"]:
            runs = [r for r in runs if r["project_slug"] == cfg["project"]]
        if cfg["since_iso"]:
            sdt = datetime.fromisoformat(cfg["since_iso"])
            runs = [r for r in runs if datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")) >= sdt]
        if cfg["run_ids"]:
            matched = []
            for pfx in cfg["run_ids"]:
                hits = [r["id"] for r in runs if r["id"].startswith(pfx)]
                assert len(hits) == 1, f"Unexpected hits for {pfx}: {hits}"
                matched.append(hits[0])
        else:
            matched = [r["id"] for r in runs]

        if cfg["dry_run"]:
            return subprocess.CompletedProcess(
                argv,
                0,
                stdout=json.dumps({"mode": "bundle", "dry_run": True, "matched_runs": matched}),
                stderr="",
            )

        batch = f"sync-{uuid.uuid4().hex[:8]}"
        bdir = remote_staged_store / batch
        bdir.mkdir(parents=True, exist_ok=True)
        staged_files = []
        exports = []
        for idx, rid in enumerate(matched):
            bf = bdir / f"{idx:04d}.bthbundle"
            # Temporarily point HOME at remote host's home during remote export
            with monkeypatch.context() as mc:
                mc.setenv("HOME", str(rem_home))
                exp = export_run_bundle(
                    rid,
                    bf,
                    project=cfg["project"] or None,
                    include_outputs=cfg["include_outputs"],
                    with_campaign=cfg["with_campaign"],
                    allow_missing_sidecar=cfg["allow_missing_sidecar"],
                    catalog_dir=remote_cat,
                )
            exports.append(exp)
            staged_files.append(f"~/.bth/tmp/{batch}/{bf.name}")
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=json.dumps(
                {
                    "mode": "bundle",
                    "dry_run": False,
                    "batch_name": batch,
                    "matched_runs": matched,
                    "staged_dir": f"~/.bth/tmp/{batch}",
                    "staged_files": staged_files,
                    "exports": exports,
                }
            ),
            stderr="",
        )

    def fake_pull_path(host, remote_path, local_dest):
        assert host == "titanix"
        assert "~/.bth/tmp/" in remote_path
        batch = remote_path.rstrip("/").split("/")[-1]
        src_dir = remote_staged_store / batch
        dest = Path(local_dest)
        for bf in src_dir.glob("*.bthbundle"):
            shutil.copy2(bf, dest / bf.name)

    config = ProjectConfig(
        slug="proj_a",
        root=local_a,
        remotes={"titanix": {"host": "titanix", "remote_root": "~/projects/proj_a"}},
    )

    # 1. Dry-run with --project narrows prefix collision and writes nothing locally
    with (
        patch("bathos.sync.subprocess.run", side_effect=fake_ssh_run),
        patch("bathos.cluster.pull_path", side_effect=fake_pull_path),
    ):
        dry_res = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=["aaaa1111"],
            project="proj_a",
            dry_run=True,
            project_root=local_a,
        )
    assert dry_res["dry_run"] is True
    assert dry_res["matched_runs"] == [rid_a1]
    assert not (local_a / ".bth" / "log" / "imported").exists()

    # 2. Pull single run with --project narrowing colliding prefix
    with (
        patch("bathos.sync.subprocess.run", side_effect=fake_ssh_run),
        patch("bathos.cluster.pull_path", side_effect=fake_pull_path),
    ):
        res1 = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=["aaaa1111"],
            project="proj_a",
            include_outputs=True,
            project_root=local_a,
        )
    assert res1["imported_count"] == 1
    assert res1["imported"][0]["run_id"] == rid_a1
    # Unrelated runs must stay absent
    assert get_run(rid_a1, local_cat) is not None
    assert get_run(rid_a2, local_cat) is None
    assert get_run(rid_b1, local_cat) is None
    receipt = latest_import_receipt(rid_a1, local_cat)
    assert receipt is not None
    assert receipt["import_method"] == "sync_pull"
    assert receipt["remote_name"] == "titanix"
    # Remote ~/.bth/tmp/ was cleaned up
    assert list(remote_staged_store.glob("sync-*")) == []

    # Immediate queryability via run_sql & get_run_tool
    rows = run_sql("SELECT id, imported_from_host FROM runs", local_cat)
    assert len(rows) == 1 and rows[0][0] == rid_a1
    shown = get_run_tool(run_id=rid_a1, catalog_dir=str(local_cat))
    assert shown["imported_from_host"] == "titanix.mit.edu"
    assert shown["import_method"] == "sync_pull"

    # 3. Idempotent re-pull (AC-6)
    with (
        patch("bathos.sync.subprocess.run", side_effect=fake_ssh_run),
        patch("bathos.cluster.pull_path", side_effect=fake_pull_path),
    ):
        res_dup = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=["aaaa1111"],
            project="proj_a",
            project_root=local_a,
        )
    assert res_dup["already_present_count"] == 1
    assert res_dup["imported"][0]["status"] == "already_present"
    assert res_dup["imported"][0]["new_events"] == 0

    # 4. Selective pull with --since 24h & --project pulls rid_a1 only (not rid_a2 which is 10d old)
    with (
        patch("bathos.sync.subprocess.run", side_effect=fake_ssh_run),
        patch("bathos.cluster.pull_path", side_effect=fake_pull_path),
    ):
        res_since = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            project="proj_a",
            since="24h",
            project_root=local_a,
        )
    assert res_since["matched_runs"] == [rid_a1]

    # 5. Multiple --run prefixes pull N single-run bundles cleanly
    with (
        patch("bathos.sync.subprocess.run", side_effect=fake_ssh_run),
        patch("bathos.cluster.pull_path", side_effect=fake_pull_path),
    ):
        res_multi = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=["aaaa1111", "bbbb2222"],
            project="proj_a",
            project_root=local_a,
        )
    assert sorted(res_multi["matched_runs"]) == sorted([rid_a1, rid_a2])
    assert get_run(rid_a2, local_cat) is not None


# ---------------------------------------------------------------------------
# Option A Mirror Fallback (`sync_pull_mirror_unverified`)
# ---------------------------------------------------------------------------


def test_selective_pull_mirror_fallback_filters_closure_and_guards(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    rem_repo, pid = _setup_repo(tmp_path / "rem_repo", slug="proj_m")
    t1 = "2026-11-25T10:00:00.000000Z"
    t2 = "2026-11-20T10:00:00.000000Z"
    rid_target = "cccc3333-0000-4000-8000-000000000001"
    rid_other = "dddd4444-0000-4000-8000-000000000002"
    rid_running = "eeee5555-0000-4000-8000-000000000003"

    _write_run_seg(
        repo=rem_repo,
        slug="proj_m",
        pid=pid,
        run_id=rid_target,
        ts_start=t1,
        ts_end=t1,
        campaign_id="camp-1",
    )
    _write_run_seg(repo=rem_repo, slug="proj_m", pid=pid, run_id=rid_other, ts_start=t2, ts_end=t2)
    _write_run_seg(
        repo=rem_repo,
        slug="proj_m",
        pid=pid,
        run_id=rid_running,
        ts_start=t1,
        ts_end=t1,
        include_finished=False,
    )

    # Put mirror logs in a remote mirror source dir
    remote_mirror = tmp_path / "remote_mirror_src"
    remote_mirror.mkdir()
    for seg in (rem_repo / ".bth" / "log").glob("*.jsonl"):
        shutil.copy2(seg, remote_mirror / seg.name)

    # Local checkout WITHOUT the sidecar file initially (to test allow_missing_sidecar guard)
    (tmp_path / "home" / ".bth" / "projects.toml").unlink(missing_ok=True)
    local_cat = tmp_path / "local_cat"
    _init_log_catalog(local_cat)
    local_repo = make_git_repo(tmp_path / "local_repo")
    write_bth_toml(local_repo, slug="proj_m")
    # Set matching project_id
    (local_repo / ".bth.toml").write_text(
        f'[project]\nslug = "proj_m"\nid = "{pid}"\n', encoding="utf-8"
    )
    register_main_root(local_repo)

    config = ProjectConfig(
        slug="proj_m",
        root=local_repo,
        remotes={"titanix": {"host": "titanix", "remote_root": "~/projects/proj_m"}},
    )

    def ssh_no_bundle(argv, **_kwargs):
        return subprocess.CompletedProcess(
            argv, 0, stdout=json.dumps({"mode": "no_bundle_cmd"}), stderr=""
        )

    def pull_mirror(_host, remote_path, local_dest):
        if "log-mirror" in remote_path:
            dest = Path(local_dest)
            for seg in remote_mirror.glob("*.jsonl"):
                shutil.copy2(seg, dest / seg.name)
        else:
            raise RuntimeError("not present")

    # 1. Refuses missing sidecar unless allow_missing_sidecar=True
    with (
        patch("bathos.sync.subprocess.run", side_effect=ssh_no_bundle),
        patch("bathos.cluster.pull_path", side_effect=pull_mirror),
        pytest.raises(RunBundleInvalidError, match="missing locally"),
    ):
        selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=[rid_target[:8]],
            project_root=local_repo,
        )

    # 2. Dry-run with allow_missing_sidecar=True writes nothing locally
    with (
        patch("bathos.sync.subprocess.run", side_effect=ssh_no_bundle),
        patch("bathos.cluster.pull_path", side_effect=pull_mirror),
    ):
        dry = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=[rid_target[:8]],
            allow_missing_sidecar=True,
            dry_run=True,
            project_root=local_repo,
        )
    assert dry["mode"] == "sync_pull_mirror_unverified"
    assert dry["dry_run"] is True
    assert not (local_repo / ".bth" / "log" / "imported").exists()

    # 3. Actual mirror fallback pull records import_method="sync_pull_mirror_unverified",
    # omits unrelated runs & omits dangling campaign.run_added, and ingests cleanly
    with (
        patch("bathos.sync.subprocess.run", side_effect=ssh_no_bundle),
        patch("bathos.cluster.pull_path", side_effect=pull_mirror),
    ):
        res = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=[rid_target[:8]],
            allow_missing_sidecar=True,
            project_root=local_repo,
        )
    assert res["mode"] == "sync_pull_mirror_unverified"
    assert res["imported_count"] == 1
    assert get_run(rid_target, local_cat) is not None
    assert get_run(rid_other, local_cat) is None
    rec = latest_import_receipt(rid_target, local_cat)
    assert rec is not None and rec["import_method"] == "sync_pull_mirror_unverified"
    assert rec["external_campaign_ref"] == "camp-1"

    # 4. Non-terminal run emits warning
    with (
        patch("bathos.sync.subprocess.run", side_effect=ssh_no_bundle),
        patch("bathos.cluster.pull_path", side_effect=pull_mirror),
    ):
        res_run = selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=[rid_running[:8]],
            allow_missing_sidecar=True,
            project_root=local_repo,
        )
    assert any("non-terminal" in w for w in res_run["warnings"])

    # 5. Equivocation in mirror is refused
    for seg in remote_mirror.glob("*.jsonl"):
        lines = seg.read_text(encoding="utf-8").splitlines()
        new_lines = []
        for ln in lines:
            ev = json.loads(ln)
            if ev["entity"] == [rid_target] and ev["kind"] == "run.started":
                ev["ts"] = "2099-01-01T00:00:00.000000Z"  # same eid, altered ts!
            new_lines.append(json.dumps(ev))
        seg.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    with (
        patch("bathos.sync.subprocess.run", side_effect=ssh_no_bundle),
        patch("bathos.cluster.pull_path", side_effect=pull_mirror),
        pytest.raises(RunImportConflictError),
    ):
        selective_pull_runs(
            "titanix",
            config,
            local_cat,
            run_ids=[rid_target[:8]],
            allow_missing_sidecar=True,
            project_root=local_repo,
        )


# ---------------------------------------------------------------------------
# Whole-remote sync_tool(pull=True) & submit --wait --then-sync (AC-5)
# ---------------------------------------------------------------------------


def test_sync_tool_whole_remote_pull_triggers_run_ingest(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    cat = tmp_path / "cat"
    _init_log_catalog(cat)
    repo, pid = _setup_repo(tmp_path / "repo", slug="proj_w")
    (repo / ".bth.toml").write_text(
        f'[project]\nslug = "proj_w"\nroot = "{repo}"\nid = "{pid}"\n\n'
        '[remotes.titanix]\nhost = "titanix"\nremote_root = "~/projects/proj_w"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(repo)

    rid = "ffff6666-0000-4000-8000-000000000001"
    evs, _ = _write_run_seg(
        repo=repo,
        slug="proj_w",
        pid=pid,
        run_id=rid,
        ts_start="2026-11-25T10:00:00.000000Z",
        ts_end="2026-11-25T10:01:00.000000Z",
    )
    # Move segment to remote_dir so it only exists after pull_cluster_log writes into remote/titanix/log/
    staged = tmp_path / "rem_seg.jsonl"
    (repo / ".bth" / "log" / f"seg-{rid[:8]}.jsonl").replace(staged)

    def fake_pull_path(_host, remote_path, local_dest):
        if remote_path.endswith("/.bth/log/"):
            shutil.copy2(staged, Path(local_dest) / staged.name)

    fake_sync = MagicMock(transferred=0, duration_s=0.1, remote="titanix", filtered=0)
    with (
        patch("bathos.mcp.sync_catalog", return_value=fake_sync),
        patch("bathos.cluster.pull_path", side_effect=fake_pull_path),
    ):
        res = sync_tool(remote_name="titanix", pull=True, catalog_dir=str(cat))

    assert res["cluster_log_pulled"] is True
    assert res["ingest_report"]["new_events"] == len(evs)
    assert get_run(rid, cat) is not None


def test_submit_then_sync_in_linked_worktree_uses_main_root_and_ingests(
    tmp_path: Path, monkeypatch
):
    """AC-5: `bth submit --wait --then-sync` from a linked worktree passes main_root
    to `pull_cluster_log` and runs `run_ingest`."""
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))

    main_repo, pid = _setup_repo(tmp_path / "main_repo", slug="proj_wt")
    (main_repo / ".bth.toml").write_text(
        f'[project]\nslug = "proj_wt"\nroot = "{main_repo}"\nid = "{pid}"\n\n'
        '[remotes.engaging]\nhost = "engaging"\nremote_root = "~/projects/proj_wt"\n'
        '[slurm]\nremote = "engaging"\npreset = "gpu"\n',
        encoding="utf-8",
    )
    subprocess.run(["git", "add", ".bth.toml"], cwd=main_repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "cfg"], cwd=main_repo, check=True, capture_output=True)

    wt_dir = tmp_path / "linked_wt"
    subprocess.run(
        ["git", "worktree", "add", "-b", "wt-branch", str(wt_dir)],
        cwd=main_repo,
        check=True,
        capture_output=True,
    )
    (wt_dir / "train.py").write_text("print('hi')\n", encoding="utf-8")

    cat = tmp_path / "cat"
    _init_log_catalog(cat)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat))
    monkeypatch.chdir(wt_dir)

    pull_calls: list[tuple] = []
    fake_pull_res = MagicMock(any_pulled=True, pulled=["log"], failures=[])
    fake_sync = MagicMock(filtered=0, transferred=0, remote="engaging", duration_s=0.1)

    with (
        patch("bathos.cluster.push_project"),
        patch("bathos.cluster.submit_job", return_value={"slurm_job_id": "12345"}),
        patch("bathos.cluster.job_wait", return_value={"wait_result": "COMPLETED", "failure_class": "SUCCESS"}),
        patch("bathos.cluster.pull_project"),
        patch("bathos.cluster_catalog.check_env_catalog_matches_remote"),
        patch("bathos.cluster_catalog.ensure_remote_catalog_dir"),
        patch("bathos.sync.sync_catalog", return_value=fake_sync),
        patch(
            "bathos.sync.pull_cluster_log",
            side_effect=lambda r, _c, root, p: (pull_calls.append((r, root.resolve(), p)), fake_pull_res)[1],
        ),
        patch("bathos.runlog.ingest.run_ingest", wraps=run_ingest) as mock_ingest,
    ):
        res = runner.invoke(app, ["submit", "--then-sync", "python", "train.py"])

    assert res.exit_code == 0, res.output
    assert len(pull_calls) == 1
    assert pull_calls[0][0] == "engaging"
    assert pull_calls[0][1] == main_repo.resolve()
    assert pull_calls[0][2] == pid
    assert mock_ingest.called


# ---------------------------------------------------------------------------
# CLI & MCP wiring for `bth bundle`, `bth import`, `bth sync`
# ---------------------------------------------------------------------------


def test_cli_and_mcp_bundle_and_import_alias_and_sync_flags(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    monkeypatch.setenv("BTH_PROJECTS_FILE", str(tmp_path / "home" / ".bth" / "projects.toml"))
    tok_path = tmp_path / "mcp_token"
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(tok_path))
    tok = get_or_create_token()

    src_cat = tmp_path / "src_cat"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo", slug="proj_cli")
    rid = "12345678-0000-4000-8000-000000000001"
    _write_run_seg(
        repo=src_repo,
        slug="proj_cli",
        pid=pid,
        run_id=rid,
        ts_start="2026-11-25T10:00:00.000000Z",
        ts_end="2026-11-25T10:01:00.000000Z",
    )

    bundle_file = tmp_path / "out.bthbundle"
    res_exp = runner.invoke(
        app,
        ["bundle", "export", rid[:8], "-o", str(bundle_file), "--catalog-dir", str(src_cat)],
    )
    assert res_exp.exit_code == 0, res_exp.output
    assert bundle_file.is_file()

    # Clear projects registry and import via CLI top-level `bth import` alias
    (tmp_path / "home" / ".bth" / "projects.toml").unlink(missing_ok=True)
    dst_cat = tmp_path / "dst_cat"
    _init_log_catalog(dst_cat)

    res_imp = runner.invoke(
        app,
        ["import", str(bundle_file), "--catalog-dir", str(dst_cat)],
    )
    assert res_imp.exit_code == 0, res_imp.output
    imp_payload = json.loads(res_imp.output)
    assert imp_payload["status"] == "imported"
    assert imp_payload["run_id"] == rid

    # Also test MCP wrappers (auth + forwarding)
    mcp_exp = asyncio.run(
        mcp_bundle_export_tool(
            run_id=rid[:8],
            output_path=str(tmp_path / "mcp.bthbundle"),
            catalog_dir=str(src_cat),
        )
    )
    assert mcp_exp["ok"] is True

    dst_cat2 = tmp_path / "dst_cat2"
    _init_log_catalog(dst_cat2)
    mcp_imp = asyncio.run(
        mcp_bundle_import_tool(
            bundle_path=str(tmp_path / "mcp.bthbundle"),
            catalog_dir=str(dst_cat2),
            token=tok,
        )
    )
    assert mcp_imp["ok"] is True
    assert mcp_imp["status"] == "imported"

    # CLI `bth sync --pull --run ... --dry-run` reaches selective_pull_runs
    (src_repo / ".bth.toml").write_text(
        (src_repo / ".bth.toml").read_text(encoding="utf-8")
        + '\n[remotes.titanix]\nhost = "titanix"\nremote_root = "~/projects/proj_cli"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(src_repo)
    with patch(
        "bathos.sync.subprocess.run",
        return_value=subprocess.CompletedProcess(
            ["ssh"], 0, stdout=json.dumps({"mode": "bundle", "dry_run": True, "matched_runs": [rid]}), stderr=""
        ),
    ):
        res_sync_cli = runner.invoke(
            app,
            [
                "sync",
                "--pull",
                "--run",
                rid[:8],
                "--project",
                "proj_cli",
                "--dry-run",
                "--catalog-dir",
                str(dst_cat),
            ],
        )
    assert res_sync_cli.exit_code == 0, res_sync_cli.output
    assert json.loads(res_sync_cli.output)["matched_runs"] == [rid]

    # MCP sync wrapper rejects selective flags without pull=True
    bad_sync = asyncio.run(
        mcp_sync_tool(catalog_dir=str(dst_cat), pull=False, run_ids=[rid[:8]], token=tok)
    )
    assert bad_sync["ok"] is False
    assert "require --pull" in (bad_sync.get("error") or "")
