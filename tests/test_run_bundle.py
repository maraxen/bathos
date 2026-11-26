"""Tests for canonical verifiable run bundles (`bathos.bundle.v1`), durable receipts,
canonical envelope hashing, equivocation checks, and local sidecar resolution.

Covers AC-1, AC-2, AC-6, AC-7, AC-8, AC-9 and NC-1..NC-5, NC-8.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
import uuid
from pathlib import Path

import duckdb
import pytest
import zstandard

from bathos.catalog import write_run
from bathos.errors import (
    RunBundleInvalidError,
    RunBundleTamperedError,
    RunBundleUnsafePathError,
    RunImportConflictError,
)
from bathos.index import connect_read, index_db_path
from bathos.output_tiers.reference import PHASE_ADOPTED, PHASE_RUN_END, hash_file
from bathos.query import get_run, latest_import_receipt, resolve_local_sidecar_path
from bathos.run_bundle import export_run_bundle, import_run_bundle, verify_run_bundle
from bathos.runlog.envelope import build_envelope, canonical_json_bytes
from bathos.runlog.ingest import _refold_run, reconcile_campaign_runs, run_ingest
from bathos.runlog.mode import cutover_marker_path
from bathos.runlog.project_id import assign_project_id, read_project_id, register_main_root
from bathos.schema import Run
from tests.runlog.conftest import make_git_repo, write_bth_toml


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


def _write_seg(repo: Path, name: str, events: list[dict]) -> Path:
    seg = repo / ".bth" / "log" / name
    seg.parent.mkdir(parents=True, exist_ok=True)
    seg.write_text("".join(json.dumps(e, sort_keys=True) + "\n" for e in events), encoding="utf-8")
    return seg


def _make_sample_run_events(
    *,
    repo: Path,
    pid: str,
    run_id: str,
    sidecar_rel: str = "scripts/experiments/probe.bth.toml",
    sidecar_text: str = '[experiment]\nhypothesis = "h"\n[result_schema]\nx = "float"\n',
    output_rel: str = "outputs/res.txt",
    output_bytes: bytes = b"hello-output\n",
    hostname: str = "titanix.mit.edu",
    campaign_id: str = "",
    ts_start: str = "2026-11-25T10:00:00.000000Z",
    ts_end: str = "2026-11-25T10:01:00.000000Z",
    include_full_closure: bool = False,
) -> tuple[list[dict], str, str]:
    sc_file = repo / sidecar_rel
    sc_file.parent.mkdir(parents=True, exist_ok=True)
    sc_file.write_text(sidecar_text, encoding="utf-8")
    sc_sha = hashlib.sha256(sidecar_text.encode("utf-8")).hexdigest()

    out_file = repo / output_rel
    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_bytes(output_bytes)
    out_sha = hash_file(out_file)

    common = dict(
        main_root=repo,
        worktree_root=repo,
        project="proj",
        project_id=pid,
        writer=f"{hostname}.101.1",
    )
    events: list[dict] = []
    seq = 0

    def _add(kind: str, entity: list[str], data: dict, ts: str, eid: str | None = None, **extra):
        nonlocal seq
        seq += 1
        ev = build_envelope(
            kind=kind,
            entity=entity,
            data=data,
            seq=seq,
            ts=ts,
            eid=eid or f"eid-{run_id[:8]}-{seq}",
            **common,
        )
        ev.update(extra)
        events.append(ev)
        return ev

    _add(
        "run.started",
        [run_id],
        {
            "id": run_id,
            "project_slug": "proj",
            "command": "python scripts/experiments/probe.py",
            "argv": ["python", "scripts/experiments/probe.py"],
            "git_hash": "abc1234",
            "git_branch": "main",
            "git_dirty": False,
            "timestamp": ts_start,
            "hostname": hostname,
            "sidecar_path": str(sc_file),
            "sidecar_sha256": sc_sha,
            "campaign_id": campaign_id,
            "outputs_declared": True,
        },
        ts_start,
    )
    _add(
        "run.finished",
        [run_id],
        {
            "id": run_id,
            "project_slug": "proj",
            "status": "completed",
            "exit_code": 0,
            "duration_s": 60.0,
            "outcome": "pass",
            "output_paths": [str(out_file)],
            "metadata": json.dumps({"x": 1.0}),
        },
        ts_end,
    )
    _add(
        "run.outputs_hashed",
        [run_id],
        {
            "phase": PHASE_RUN_END,
            "output_metadata": [
                {
                    "path": str(out_file),
                    "relpath": output_rel,
                    "status": "present",
                    "size_bytes": len(output_bytes),
                    "sha256": out_sha,
                    "phase": PHASE_RUN_END,
                }
            ],
        },
        ts_end,
    )

    if include_full_closure:
        _add(
            "anchor.recorded",
            ["anchor-1"],
            {
                "id": "anchor-1",
                "path": str(sc_file),
                "sha256": sc_sha,
                "kind": "sidecar",
                "label": "probe",
                "content_hash": sc_sha,
                "campaign_id": campaign_id,
                "anchored_at": ts_start,
            },
            ts_start,
        )
        _add(
            "output_copy.recorded",
            [run_id, "copy-1"],
            {
                "copy_id": "copy-1",
                "run_id": run_id,
                "tier": "cold",
                "profile": "s3",
                "host": hostname,
                "path": "/cold/run.tar.zst",
                "config_layer": "project",
                "archive_sha256": "a" * 64,
                "manifest_sha256": "b" * 64,
                "archive_size_bytes": 100,
                "state": "verified",
                "verified_at": ts_end,
                "recorded_at": ts_end,
            },
            ts_end,
        )
        _add(
            "submit.recorded",
            ["sub-1"],
            {
                "id": "sub-1",
                "project_slug": "proj",
                "command": "python scripts/experiments/probe.py",
                "sidecar_sha256": sc_sha,
                "bth_submit_version": "0.18.0",
                "submitted_at": ts_start,
                "myxcel_job_id": "mx-1",
                "slurm_job_id": "",
                "stage_name": "train",
            },
            ts_start,
        )
        _add(
            "edge.added",
            [run_id, "parent-run-0", "run"],
            {"src": run_id, "dst": "parent-run-0", "type": "run"},
            ts_start,
        )
        _add(
            "supersession.declared",
            ["sup-1"],
            {
                "id": "sup-1",
                "entity_type": "run",
                "entity_id": "old-run-0",
                "superseded_by_type": "run",
                "superseded_by_id": run_id,
                "reason": "bugfix",
                "declared_by": "researcher",
                "declared_at": ts_end,
            },
            ts_end,
        )
        _add(
            "blast_radius.recorded",
            ["br-1"],
            {
                "id": "br-1",
                "entity_type": "run",
                "entity_id": run_id,
                "from_state": "clean",
                "to_state": "affected",
                "anchor_kind": "sidecar",
                "anchor_value": sc_sha,
                "reason": "test blast",
                "amended_at": ts_end,
            },
            ts_end,
        )
        _add(
            "trust_ledger.recorded",
            ["tl-1"],
            {
                "id": "tl-1",
                "run_id": run_id,
                "output_path": output_rel,
                "content_hash": out_sha,
                "from_state": "candidate",
                "to_state": "promoted",
                "attestation_ref": "att-1",
                "amended_at": ts_end,
                "reason": "verified",
            },
            ts_end,
        )
        _add(
            "archived_item.recorded",
            ["arch-1"],
            {
                "id": "item-1",
                "record_id": "arch-1",
                "project_slug": "proj",
                "event": "archived",
                "kind": "outputs",
                "paths": [output_rel],
                "pre_archive_sha": out_sha,
                "recorded_at": ts_end,
                "run_ids": [run_id],
            },
            ts_end,
        )
        _add(
            "observation.created",
            ["obs-1"],
            {
                "id": "obs-1",
                "project_slug": "proj",
                "run_id": run_id,
                "text": "observed stability",
                "created_at": ts_end,
            },
            ts_end,
        )
        _add(
            "validity.acknowledged",
            ["ack-1"],
            {
                "id": "ack-1",
                "kind": "suspect",
                "entity_type": "run",
                "entity_id": run_id,
                "run_ids": json.dumps([run_id]),
                "surface": "review",
                "reason": "checked manually",
                "actor": "marielle",
                "recorded_at": ts_end,
            },
            ts_end,
            actor="marielle",
        )

    return events, sc_sha, out_sha


def _rewrite_tar_zst(
    src_bundle: Path,
    dst_bundle: Path,
    mutator,
) -> None:
    """Read members from src_bundle, apply mutator(members_dict), and write dst_bundle."""
    members: list[tuple[tarfile.TarInfo, bytes]] = []
    with open(src_bundle, "rb") as raw_fh:
        dctx = zstandard.ZstdDecompressor()
        with dctx.stream_reader(raw_fh) as zr, tarfile.open(fileobj=zr, mode="r|") as tf:
            for m in tf:
                fobj = tf.extractfile(m)
                data = fobj.read() if fobj is not None else b""
                members.append((m, data))

    mutated = mutator(members)
    cctx = zstandard.ZstdCompressor(level=19)
    with (
        open(dst_bundle, "wb") as out_fh,
        cctx.stream_writer(out_fh, closefd=False) as zw,
        tarfile.open(fileobj=zw, mode="w|", format=tarfile.PAX_FORMAT) as tf,
    ):
        for info, data in mutated:
            if info.isreg():
                info.size = len(data)
                tf.addfile(info, io.BytesIO(data))
            else:
                tf.addfile(info)


def test_ac1_bundle_round_trip_and_full_causal_closure(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "e88aa486-3a4c-4977-826b-2f5d511ca29d"
    events, sc_sha, out_sha = _make_sample_run_events(
        repo=src_repo, pid=pid, run_id=run_id, include_full_closure=True
    )
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)

    bundle_path = tmp_path / "run.bthbundle"
    exp = export_run_bundle(
        run_id[:8], bundle_path, include_outputs=True, catalog_dir=src_cat
    )
    assert exp["ok"] is True
    assert exp["run_id"] == run_id
    assert exp["outputs_count"] == 1
    assert exp["sidecar_included"] is True

    ver = verify_run_bundle(bundle_path)
    assert ver["ok"] is True
    assert ver["events_count"] == len(events)

    # Switch HOME to simulate a separate receiving machine with no local checkout
    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)
    extract_dir = tmp_path / "extracted_outputs"

    imp = import_run_bundle(
        bundle_path,
        extract_outputs=extract_dir,
        catalog_dir=dst_cat,
    )
    assert imp["ok"] is True
    assert imp["status"] == "imported"
    assert imp["new_events"] == len(events)
    assert imp["origin_host"] == "titanix.mit.edu"
    assert (extract_dir / "outputs" / "res.txt").read_bytes() == b"hello-output\n"

    con = connect_read(dst_cat, wire_validity=False)
    try:
        assert con.execute("SELECT id, outcome, sidecar_sha256, imported_from_host, import_method, bundle_sha256 FROM runs").fetchone() == (
            run_id,
            "pass",
            sc_sha,
            "titanix.mit.edu",
            "bundle_import",
            imp["bundle_sha256"],
        )
        assert con.execute("SELECT count(*) FROM sidecar_anchors").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM output_copies").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM submits").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM run_edges").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM supersession_edges").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM blast_radius_ledger").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM trust_ledger").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM archived_items").fetchone()[0] == 1
        assert con.execute("SELECT count(*) FROM observations").fetchone()[0] == 1
        assert con.execute("SELECT actor FROM acknowledgment_ledger").fetchone()[0] == "marielle"
        assert con.execute("SELECT count(*) FROM quarantine").fetchone()[0] == 0
        # NC-8: no bad_origin, origin stays in ("live", "migration")
        origins = {r[0] for r in con.execute("SELECT DISTINCT origin FROM events").fetchall()}
        assert origins <= {"live", "migration"}
    finally:
        con.close()

    run_obj = get_run(run_id, dst_cat, include_superseded=True)
    assert run_obj is not None
    assert run_obj.id == run_id
    assert run_obj.timestamp.isoformat() == "2026-11-25T10:00:00+00:00"
    assert run_obj.imported_from_host == "titanix.mit.edu"
    assert Path(imp["local_sidecar_path"]).is_file()


def test_ac1_legacy_pre_cutover_export_and_max_output_bytes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_leg"))
    leg_cat = tmp_path / "leg_cat"
    leg_cat.mkdir(parents=True)
    work_dir = tmp_path / "leg_work"
    work_dir.mkdir()
    sc = work_dir / "scripts" / "experiments" / "leg.bth.toml"
    sc.parent.mkdir(parents=True)
    sc_txt = '[experiment]\nhypothesis = "leg"\n'
    sc.write_text(sc_txt, encoding="utf-8")
    sc_sha = hashlib.sha256(sc_txt.encode()).hexdigest()

    out_file = work_dir / "outputs" / "big.bin"
    out_file.parent.mkdir(parents=True)
    out_bytes = b"A" * 2048
    out_file.write_bytes(out_bytes)
    out_sha = hash_file(out_file)

    run = Run(
        id="leg-run-1111",
        project_slug="legproj",
        command="python leg.py",
        argv=["python", "leg.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        outcome="pass",
        hostname="titanix",
        sidecar_path=str(sc),
        sidecar_sha256=sc_sha,
        output_metadata=json.dumps(
            [
                {
                    "path": str(out_file),
                    "relpath": "outputs/big.bin",
                    "status": "present",
                    "size_bytes": len(out_bytes),
                    "sha256": out_sha,
                    "phase": PHASE_RUN_END,
                }
            ]
        ),
    )
    write_run(run, leg_cat)

    # 512 MiB cap override check: max_output_bytes=100 refuses 2048-byte output
    with pytest.raises(RunBundleInvalidError, match="max_output_bytes"):
        export_run_bundle(
            "leg-run",
            tmp_path / "too_big.bthbundle",
            include_outputs=True,
            max_output_bytes=100,
            catalog_dir=leg_cat,
        )

    # max_output_bytes=0 means unlimited
    bundle = tmp_path / "leg.bthbundle"
    exp = export_run_bundle(
        "leg-run",
        bundle,
        include_outputs=True,
        max_output_bytes=0,
        catalog_dir=leg_cat,
    )
    assert exp["source_mode"] == "legacy"

    dst_cat = tmp_path / "dst_cat"
    _init_log_catalog(dst_cat)
    imp = import_run_bundle(bundle, catalog_dir=dst_cat)
    assert imp["ok"] is True
    assert imp["status"] == "imported"
    got = get_run("leg-run-1111", dst_cat)
    assert got is not None
    assert got.outcome == "pass"
    assert got.imported_from_host == "titanix"


def test_ac2_durable_receipts_across_index_rebuild_and_refold(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "rec-run-0001"
    events, _, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=run_id)
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)

    bundle = tmp_path / "rec.bthbundle"
    export_run_bundle(run_id, bundle, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)
    imp = import_run_bundle(bundle, catalog_dir=dst_cat)
    assert imp["status"] == "imported"

    # Delete index.db completely and rebuild via run_ingest()
    idx_path = index_db_path(dst_cat)
    assert idx_path.exists()
    idx_path.unlink()

    run_ingest(dst_cat)
    receipt = latest_import_receipt(run_id, dst_cat)
    assert receipt is not None
    assert receipt["origin_host"] == "titanix.mit.edu"
    assert receipt["bundle_sha256"] == imp["bundle_sha256"]

    rebuilt = get_run(run_id, dst_cat)
    assert rebuilt is not None
    assert rebuilt.imported_from_host == "titanix.mit.edu"
    assert rebuilt.bundle_sha256 == imp["bundle_sha256"]

    # Refolding the run directly must preserve runs.imported_*
    con = duckdb.connect(str(idx_path))
    try:
        _refold_run(con, run_id)
        row = con.execute(
            "SELECT imported_from_host, import_method, bundle_sha256 FROM runs WHERE id = ?",
            [run_id],
        ).fetchone()
        assert row == ("titanix.mit.edu", "bundle_import", imp["bundle_sha256"])
    finally:
        con.close()


def test_connect_read_pre_upgrade_index_lacking_import_receipts(tmp_path: Path):
    cat = tmp_path / "old_cat"
    _init_log_catalog(cat)
    db_path = index_db_path(cat)
    con = duckdb.connect(str(db_path))
    try:
        # Minimal pre-upgrade tables without import_receipts
        con.execute("CREATE TABLE runs (id VARCHAR PRIMARY KEY, project_slug VARCHAR, status VARCHAR)")
        con.execute("CREATE TABLE events (eid VARCHAR PRIMARY KEY, kind VARCHAR)")
        con.execute("CREATE TABLE quarantine (file VARCHAR)")
        con.execute("CREATE TABLE ingest_watermarks (root_kind VARCHAR)")
        con.execute("CREATE TABLE campaigns (id VARCHAR PRIMARY KEY)")
        con.execute("CREATE TABLE campaign_runs (campaign_id VARCHAR, run_id VARCHAR)")
        con.execute("CREATE TABLE campaign_edges (child_campaign_id VARCHAR)")
        con.execute("CREATE TABLE run_edges (child_run_id VARCHAR)")
        con.execute("CREATE TABLE sidecar_anchors (id VARCHAR PRIMARY KEY)")
        con.execute("CREATE TABLE blast_radius_ledger (id VARCHAR PRIMARY KEY)")
        con.execute("CREATE TABLE trust_ledger (id VARCHAR PRIMARY KEY)")
        con.execute("CREATE TABLE archived_items (record_id VARCHAR PRIMARY KEY)")
        con.execute("CREATE TABLE submits (id VARCHAR PRIMARY KEY)")
    finally:
        con.close()

    rcon = connect_read(cat, wire_validity=False)
    try:
        assert rcon.execute("SELECT * FROM import_receipts").fetchall() == []
    finally:
        rcon.close()


def test_ac6_idempotent_replay_and_incremental_adopt(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "inc-run-0001"
    events, _, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=run_id)
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)

    b1 = tmp_path / "b1.bthbundle"
    export_run_bundle(run_id, b1, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)

    imp1 = import_run_bundle(b1, catalog_dir=dst_cat)
    assert imp1["status"] == "imported"
    assert imp1["new_events"] == len(events)

    # Idempotent replay
    replay = import_run_bundle(b1, catalog_dir=dst_cat)
    assert replay["status"] == "already_present"
    assert replay["new_events"] == 0
    assert replay["skipped_existing"] == len(events)

    # Incremental append (new adopted output event on source)
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    adopted_file = src_repo / "outputs" / "extra.csv"
    adopted_file.write_text("a,b\n1,2\n", encoding="utf-8")
    adopt_ev = build_envelope(
        kind="run.outputs_hashed",
        entity=[run_id],
        data={
            "phase": PHASE_ADOPTED,
            "output_metadata": [
                {
                    "path": str(adopted_file),
                    "relpath": "outputs/extra.csv",
                    "status": "present",
                    "size_bytes": adopted_file.stat().st_size,
                    "sha256": hash_file(adopted_file),
                    "phase": PHASE_ADOPTED,
                }
            ],
        },
        main_root=src_repo,
        worktree_root=src_repo,
        project="proj",
        project_id=pid,
        writer="titanix.101.1",
        seq=99,
        ts="2026-11-25T10:05:00.000000Z",
        eid="eid-adopt-01",
    )
    _write_seg(src_repo, "seg2.jsonl", [adopt_ev])
    run_ingest(src_cat)

    b2 = tmp_path / "b2.bthbundle"
    export_run_bundle(run_id, b2, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    imp2 = import_run_bundle(b2, catalog_dir=dst_cat)
    assert imp2["status"] == "imported"
    assert imp2["new_events"] == 1
    assert imp2["skipped_existing"] == len(events)

    con = connect_read(dst_cat, wire_validity=False)
    try:
        adopted_json = con.execute("SELECT adopted_outputs FROM runs WHERE id = ?", [run_id]).fetchone()[0]
        assert adopted_json is not None
        adopted_list = json.loads(adopted_json)
        assert [a["relpath"] for a in adopted_list] == ["outputs/extra.csv"]
    finally:
        con.close()


def test_ac7_resolve_local_sidecar_path_checkout_then_cache(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "sc-run-0001"
    sidecar_text = '[experiment]\nhypothesis = "local check"\n'
    events, _, _ = _make_sample_run_events(
        repo=src_repo, pid=pid, run_id=run_id, sidecar_text=sidecar_text
    )
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)
    bundle = tmp_path / "sc.bthbundle"
    export_run_bundle(run_id, bundle, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)
    dst_repo, _ = _setup_repo(tmp_path / "dst_repo")
    local_sc = dst_repo / "scripts" / "experiments" / "probe.bth.toml"
    local_sc.parent.mkdir(parents=True)
    local_sc.write_text(sidecar_text, encoding="utf-8")

    imp = import_run_bundle(bundle, worktree_root=dst_repo, catalog_dir=dst_cat)
    assert Path(imp["local_sidecar_path"]) == local_sc.resolve()

    run_obj = get_run(run_id, dst_cat)
    assert run_obj is not None
    assert resolve_local_sidecar_path(run_obj, dst_cat, worktree_root=dst_repo) == local_sc.resolve()

    # Modify local sidecar -> SHA-256 mismatch -> falls back to imported_sidecars cache?
    # Wait: when local checkout matched at import time, imported_sidecars wasn't written;
    # if local file diverges later, returns None unless cached. Test cached fallback too:
    dst_cat2 = tmp_path / "cat_dst2"
    _init_log_catalog(dst_cat2)
    imp_cached = import_run_bundle(bundle, catalog_dir=dst_cat2)
    cached_path = Path(imp_cached["local_sidecar_path"])
    assert "imported_sidecars" in str(cached_path)
    assert cached_path.is_file()
    run_obj2 = get_run(run_id, dst_cat2)
    assert run_obj2 is not None
    assert resolve_local_sidecar_path(run_obj2, dst_cat2) == cached_path.resolve()


def test_ac9_external_campaign_ref_preserves_campaign_id_without_siblings(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    cid = "camp-" + str(uuid.uuid4())
    r1 = "run-camp-1111"
    r2 = "run-camp-2222"

    ev_camp = build_envelope(
        kind="campaign.created",
        entity=[cid],
        data={
            "id": cid,
            "project_slug": "proj",
            "name": "c1",
            "mode": "exploration",
            "status": "open",
            "started_at": "2026-11-25T09:00:00Z",
        },
        main_root=src_repo,
        worktree_root=src_repo,
        project="proj",
        project_id=pid,
        writer="titanix.1.1",
        seq=1,
        ts="2026-11-25T09:00:00Z",
        eid="eid-camp-created",
    )
    ev1, _, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=r1, campaign_id=cid)
    ev2, _, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=r2, campaign_id=cid)
    add1 = build_envelope(
        kind="campaign.run_added",
        entity=[cid, r1],
        data={"campaign_id": cid, "run_id": r1},
        main_root=src_repo,
        worktree_root=src_repo,
        project="proj",
        project_id=pid,
        writer="titanix.1.1",
        seq=50,
        ts="2026-11-25T10:00:00Z",
        eid="eid-add-1",
    )
    _write_seg(src_repo, "seg1.jsonl", [ev_camp, add1, *ev1, *ev2])
    run_ingest(src_cat)

    # Single-run export (with_campaign=False)
    b_single = tmp_path / "single.bthbundle"
    export_run_bundle(r1, b_single, with_campaign=False, catalog_dir=src_cat)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)
    import_run_bundle(b_single, catalog_dir=dst_cat)

    con = connect_read(dst_cat, wire_validity=False)
    try:
        assert con.execute("SELECT id, campaign_id FROM runs").fetchall() == [(r1, cid)]
        assert con.execute("SELECT count(*) FROM campaigns").fetchone()[0] == 0
        assert con.execute("SELECT count(*) FROM campaign_runs").fetchone()[0] == 0
        assert (
            con.execute(
                "SELECT count(*) FROM events WHERE kind LIKE 'campaign%'"
            ).fetchone()[0]
            == 0
        )
    finally:
        con.close()

    # Re-ingest and reconcile_campaign_runs must keep runs.campaign_id intact
    con_rw = duckdb.connect(str(index_db_path(dst_cat)))
    try:
        reconcile_campaign_runs(con_rw)
        assert con_rw.execute("SELECT campaign_id FROM runs WHERE id = ?", [r1]).fetchone() == (cid,)
    finally:
        con_rw.close()

    # Full-campaign mode (--with-campaign) folds campaign natively
    b_camp = tmp_path / "with_camp.bthbundle"
    export_run_bundle(r1, b_camp, with_campaign=True, catalog_dir=src_cat)
    dst_cat2 = tmp_path / "cat_dst2"
    _init_log_catalog(dst_cat2)
    import_run_bundle(b_camp, catalog_dir=dst_cat2)
    con2 = connect_read(dst_cat2, wire_validity=False)
    try:
        assert con2.execute("SELECT id, name FROM campaigns").fetchall() == [(cid, "c1")]
        assert con2.execute("SELECT campaign_id, run_id FROM campaign_runs").fetchall() == [(cid, r1)]
    finally:
        con2.close()


def test_nc1_tampered_metadata_and_nan_refused_zero_writes(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "nc1-run-0001"
    events, _, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=run_id)
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)
    orig = tmp_path / "orig.bthbundle"
    export_run_bundle(run_id, orig, catalog_dir=src_cat)

    # 1. Alter non-data key (`writer` and `actor`) in events.jsonl -> RunBundleTamperedError
    tampered_meta = tmp_path / "tampered_meta.bthbundle"

    def _mutate_writer(members):
        out = []
        for info, data in members:
            if info.name == "events.jsonl":
                lines = [json.loads(ln) for ln in data.decode().splitlines() if ln.strip()]
                lines[0]["writer"] = "evil-writer"
                lines[0]["actor"] = "forger"
                data = b"".join(canonical_json_bytes(ev) + b"\n" for ev in lines)
            out.append((info, data))
        return out

    _rewrite_tar_zst(orig, tampered_meta, _mutate_writer)

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)
    with pytest.raises(RunBundleTamperedError):
        import_run_bundle(tampered_meta, catalog_dir=dst_cat)
    assert not index_db_path(dst_cat).exists()

    # 2. Inject NaN in events.jsonl (even with recomputed hashes) -> RunBundleInvalidError
    nan_bundle = tmp_path / "nan.bthbundle"

    def _mutate_nan(members):
        m_dict = {info.name: (info, data) for info, data in members}
        ev_info, ev_data = m_dict["events.jsonl"]
        ev_text = ev_data.decode().replace('"duration_s":60.0', '"duration_s":NaN')
        new_ev_bytes = ev_text.encode()
        man_info, man_data = m_dict["manifest.json"]
        man = json.loads(man_data.decode())
        man["events_sha256"] = hashlib.sha256(new_ev_bytes).hexdigest()
        man_body = {k: v for k, v in man.items() if k != "bundle_sha256"}
        man["bundle_sha256"] = hashlib.sha256(canonical_json_bytes(man_body)).hexdigest()
        m_dict["events.jsonl"] = (ev_info, new_ev_bytes)
        m_dict["manifest.json"] = (man_info, json.dumps(man).encode())
        return list(m_dict.values())

    _rewrite_tar_zst(orig, nan_bundle, _mutate_nan)
    with pytest.raises((RunBundleInvalidError, RunBundleTamperedError)):
        import_run_bundle(nan_bundle, catalog_dir=dst_cat)
    assert not index_db_path(dst_cat).exists()


def test_nc2_same_eid_envelope_conflict_in_ingest_and_import(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    cat = tmp_path / "cat"
    _init_log_catalog(cat)
    repo, pid = _setup_repo(tmp_path / "repo")
    ev1 = build_envelope(
        kind="run.started",
        entity=["run-nc2"],
        data={"id": "run-nc2", "project_slug": "proj"},
        main_root=repo,
        worktree_root=repo,
        project="proj",
        project_id=pid,
        writer="w1",
        seq=1,
        ts="2026-11-25T10:00:00Z",
        eid="same-eid-1",
    )
    _write_seg(repo, "seg1.jsonl", [ev1])
    rep1 = run_ingest(cat)
    assert rep1.new_events == 1

    # Duplicate eid with identical (kind, entity, data) but altered ts/writer/v -> quarantined as eid_conflict!
    ev_conflict = dict(ev1, ts="2026-11-25T11:00:00Z", writer="w2", v=2)
    _write_seg(repo, "seg2.jsonl", [ev_conflict])
    rep2 = run_ingest(cat)
    assert rep2.quarantined == 1
    assert rep2.skipped_existing == 0

    con = connect_read(cat, wire_validity=False)
    try:
        reasons = [r[0] for r in con.execute("SELECT reason FROM quarantine").fetchall()]
        assert reasons == ["eid_conflict"]
    finally:
        con.close()

    # Also verify import_run_bundle refuses same-eid conflict before writing
    monkeypatch.setenv("HOME", str(tmp_path / "home_src2"))
    src_cat2 = tmp_path / "cat_src2"
    _init_log_catalog(src_cat2)
    repo2, pid2 = _setup_repo(tmp_path / "repo2")
    ev_other = dict(ev1, main_root=str(repo2), worktree_root=str(repo2), project_id=pid2, ts="2026-11-25T12:00:00Z")
    _write_seg(repo2, "seg1.jsonl", [ev_other])
    run_ingest(src_cat2)
    b_conf = tmp_path / "conf.bthbundle"
    export_run_bundle("run-nc2", b_conf, allow_missing_sidecar=True, catalog_dir=src_cat2)

    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    with pytest.raises(RunImportConflictError):
        import_run_bundle(b_conf, catalog_dir=cat)


def test_nc3_distinct_eid_genesis_terminal_run_end_equivocation(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "dst_cat"
    _init_log_catalog(dst_cat)
    dst_repo, pid = _setup_repo(tmp_path / "dst_repo")
    run_id = "equiv-run-1"
    events_a, _, _ = _make_sample_run_events(
        repo=dst_repo, pid=pid, run_id=run_id, hostname="titanix"
    )
    _write_seg(dst_repo, "seg1.jsonl", events_a)
    run_ingest(dst_cat)

    def _export_variant(name: str, mutate_events) -> Path:
        monkeypatch.setenv("HOME", str(tmp_path / f"home_{name}"))
        sc = tmp_path / f"cat_{name}"
        _init_log_catalog(sc)
        sr, spid = _setup_repo(tmp_path / f"repo_{name}")
        evs, _, _ = _make_sample_run_events(repo=sr, pid=spid, run_id=run_id, hostname="titanix")
        for i, e in enumerate(evs):
            e["eid"] = f"alt-{name}-{i}"
        mutate_events(evs)
        _write_seg(sr, "seg1.jsonl", evs)
        run_ingest(sc)
        out = tmp_path / f"{name}.bthbundle"
        export_run_bundle(run_id, out, allow_missing_sidecar=True, catalog_dir=sc)
        return out

    # 1. Conflicting genesis (command differs)
    b_gen = _export_variant("gen", lambda evs: evs[0]["data"].update({"command": "python other.py"}))
    # 2. Conflicting terminal (outcome differs)
    b_term = _export_variant("term", lambda evs: evs[1]["data"].update({"outcome": "fail"}))
    # 3. Conflicting run_end output hash
    b_end = _export_variant(
        "end",
        lambda evs: evs[2]["data"]["output_metadata"][0].update({"sha256": "f" * 64}),
    )

    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    for b_bad in (b_gen, b_term, b_end):
        with pytest.raises(RunImportConflictError):
            import_run_bundle(b_bad, allow_missing_sidecar=True, catalog_dir=dst_cat)


def test_nc4_missing_or_tampered_sidecar_and_outputs(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "nc4-run-1"
    events, sc_sha, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=run_id)
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)

    # Delete sidecar from source repo -> export refuses without allow_missing_sidecar=True
    (src_repo / "scripts" / "experiments" / "probe.bth.toml").unlink()
    with pytest.raises(RunBundleInvalidError):
        export_run_bundle(run_id, tmp_path / "no_sc.bthbundle", catalog_dir=src_cat)

    # Export with allow_missing_sidecar=True -> import refuses without allow_missing_sidecar=True
    b_no_sc = tmp_path / "allowed_no_sc.bthbundle"
    export_run_bundle(
        run_id, b_no_sc, allow_missing_sidecar=True, catalog_dir=src_cat
    )
    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)
    with pytest.raises(RunBundleInvalidError):
        import_run_bundle(b_no_sc, catalog_dir=dst_cat)

    # With allow_missing_sidecar=True -> succeeds
    imp_ok = import_run_bundle(b_no_sc, allow_missing_sidecar=True, catalog_dir=dst_cat)
    assert imp_ok["status"] == "imported"

    # Re-create sidecar on source and test tampered outputs manifest (even with include_outputs=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home_src2"))
    src_repo2, pid2 = _setup_repo(tmp_path / "src_repo2")
    src_cat2 = tmp_path / "cat_src2"
    _init_log_catalog(src_cat2)
    evs2, _, _ = _make_sample_run_events(repo=src_repo2, pid=pid2, run_id="other-run-2")
    _write_seg(src_repo2, "seg1.jsonl", evs2)
    run_ingest(src_cat2)
    b_meta = tmp_path / "meta.bthbundle"
    export_run_bundle("other-run-2", b_meta, include_outputs=False, catalog_dir=src_cat2)

    b_tampered_out = tmp_path / "tampered_out.bthbundle"

    def _drop_manifest_outputs(members):
        m_dict = {info.name: (info, data) for info, data in members}
        man_info, man_data = m_dict["manifest.json"]
        man = json.loads(man_data.decode())
        man["outputs"] = []
        man_body = {k: v for k, v in man.items() if k != "bundle_sha256"}
        man["bundle_sha256"] = hashlib.sha256(canonical_json_bytes(man_body)).hexdigest()
        m_dict["manifest.json"] = (man_info, json.dumps(man).encode())
        return list(m_dict.values())

    _rewrite_tar_zst(b_meta, b_tampered_out, _drop_manifest_outputs)
    with pytest.raises(RunBundleTamperedError):
        verify_run_bundle(b_tampered_out)


def test_nc5_container_security_and_extract_outputs_guards(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path / "home_src"))
    src_cat = tmp_path / "cat_src"
    _init_log_catalog(src_cat)
    src_repo, pid = _setup_repo(tmp_path / "src_repo")
    run_id = "nc5-run-1"
    events, _, _ = _make_sample_run_events(repo=src_repo, pid=pid, run_id=run_id)
    _write_seg(src_repo, "seg1.jsonl", events)
    run_ingest(src_cat)
    orig = tmp_path / "orig.bthbundle"
    export_run_bundle(run_id, orig, include_outputs=True, catalog_dir=src_cat)

    bad_paths = [
        "../evil.txt",
        "./manifest.json",
        "blobs//outputs/res.txt",
        "blobs/outputs/a\x00b",
        "unknown_top.txt",
    ]
    for bad in bad_paths:
        bad_bundle = tmp_path / f"bad_{abs(hash(bad))}.bthbundle"

        def _add_bad(members, _bad=bad):
            if "\x00" in _bad:
                info = tarfile.TarInfo(name="blobs/outputs/nul")
                info.pax_headers = {"path": _bad}
            else:
                info = tarfile.TarInfo(name=_bad)
            return [*members, (info, b"x")]

        _rewrite_tar_zst(orig, bad_bundle, _add_bad)
        with pytest.raises(RunBundleUnsafePathError):
            verify_run_bundle(bad_bundle)

    # Symlink member inside archive
    sym_bundle = tmp_path / "sym.bthbundle"

    def _add_sym(members):
        info = tarfile.TarInfo(name="blobs/outputs/link")
        info.type = tarfile.SYMTYPE
        info.linkname = "/etc/passwd"
        return [*members, (info, b"")]

    _rewrite_tar_zst(orig, sym_bundle, _add_sym)
    with pytest.raises(RunBundleUnsafePathError):
        verify_run_bundle(sym_bundle)

    # Extraction guards: pre-existing parent symlink & clobber refusal
    monkeypatch.setenv("HOME", str(tmp_path / "home_dst"))
    dst_cat = tmp_path / "cat_dst"
    _init_log_catalog(dst_cat)

    sym_extract = tmp_path / "extract_sym"
    sym_extract.mkdir()
    real_sub = tmp_path / "real_sub"
    real_sub.mkdir()
    (sym_extract / "outputs").symlink_to(real_sub)
    with pytest.raises(RunBundleUnsafePathError):
        import_run_bundle(orig, extract_outputs=sym_extract, catalog_dir=dst_cat)
    assert not index_db_path(dst_cat).exists()

    # Differing existing file refused without overwrite_outputs=True
    clobber_dir = tmp_path / "extract_clobber"
    (clobber_dir / "outputs").mkdir(parents=True)
    (clobber_dir / "outputs" / "res.txt").write_bytes(b"differing")
    with pytest.raises(RunBundleTamperedError):
        import_run_bundle(orig, extract_outputs=clobber_dir, catalog_dir=dst_cat)
    assert (clobber_dir / "outputs" / "res.txt").read_bytes() == b"differing"

    # overwrite_outputs=True succeeds
    imp = import_run_bundle(
        orig, extract_outputs=clobber_dir, overwrite_outputs=True, catalog_dir=dst_cat
    )
    assert imp["status"] == "imported"
    assert (clobber_dir / "outputs" / "res.txt").read_bytes() == b"hello-output\n"
