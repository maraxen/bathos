"""B4 of the runlog cut-over (spec 261012_retire-compact-runlog-cutover, runlog AC-12).

Question: after cut-over, is a just-finished `bth run` visible to every reader with
no explicit `bth compact`, and how long does a read take?

Procedure (pre-registered in b4_cutover_read_latency.bth.toml):

0. Precondition: the installed `bth` carries the run-end ingest trigger
   (`ingest_after_write` in the installed `bathos.runner`). Without it the
   measurement is of the wrong build and is recorded as invalid.
1. Snapshot the live `index.db` (+ cutover.json) into a scratch catalog S.
2. Sensitivity control: one probe run with BTH_NO_INGEST=1. Every reader must MISS
   it on the live catalog. If any reader sees it, the instrument cannot detect a
   miss and the measurement is invalid.
3. Stale-index control: in S (taken before the control run, so S has never seen
   it) delete every ingest watermark and run a full re-fold. The control run must
   then be visible in S. A broken re-fold shows here as a fail.
4. N probe runs with the trigger on. Immediately after each child exits, time the
   three CLI readers (`bth show`, `bth ls`, `bth sql`) and the in-process readers
   (get_run, find_runs, run_sql), and record whether each one sees the run. The
   run-end ingest duration is read from each child's telemetry
   (`ingest.after_write`, `duration_ms`).
5. Baseline (descriptive, AC-12): the same CLI reads on a legacy copy of
   `bathos.db.frozen` (no cutover.json) for an id that copy holds.

Primary metric: p95 over all CLI read invocations in step 4 (the latency a user or
agent shell sees, including interpreter start-up). Scratch catalogs live under a
temp dir and are removed at the end; the live catalog is only read and appended
to by ordinary `bth run`s.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

log = logging.getLogger("b4")

REPO = Path(__file__).resolve().parents[2]
PROBE = "scripts/analysis/b4_probe.py"
PROJECT = "bathos"
SCRUB_ENV = (
    "BTH_RESULTS_PATH",
    "BTH_PROGRESS_PATH",
    "BTH_OUTPUT_DIR",
    "BTH_NO_INGEST",
    "BTH_DIFFERENTIAL_KNOB",
    "BTH_DIFFERENTIAL_VALUE",
    "BTH_DIFFERENTIAL_PHASE",
    "BTH_LOG_DIR",
)


def p95(xs: list[float]) -> float:
    xs = sorted(xs)
    if not xs:
        return float("nan")
    # nearest-rank p95
    k = max(0, -(-95 * len(xs) // 100) - 1)
    return xs[k]


def live_catalog() -> Path:
    return Path(os.environ.get("BTH_CATALOG_DIR") or Path.home() / ".bth" / "catalog")


def trigger_installed() -> tuple[bool, str]:
    bth = shutil.which("bth")
    if bth is None:
        return False, "bth not on PATH"
    shebang = Path(bth).read_text(errors="replace").splitlines()[0]
    interp = shebang.removeprefix("#!").strip().split()[0]
    out = subprocess.run(
        [
            interp,
            "-I",
            "-c",
            "import importlib.util as u; print(u.find_spec('bathos.runner').origin)",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    return "ingest_after_write" in Path(out).read_text(), out


def child_env(log_dir: Path, *, no_ingest: bool) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    env["BTH_LOG_DIR"] = str(log_dir)
    if no_ingest:
        env["BTH_NO_INGEST"] = "1"
    return env


def probe_run(work: Path, i: int, *, no_ingest: bool) -> tuple[str, float, float | None]:
    """Launch one probe `bth run`; return (run_id, wall_s, ingest_ms)."""
    tag = "ctrl" if no_ingest else f"p{i:02d}"
    id_file = work / f"{tag}.id"
    log_dir = work / f"telemetry_{tag}"
    log_dir.mkdir()
    cmd = [
        "bth",
        "run",
        "--project-slug",
        PROJECT,
        "--",
        "uv",
        "run",
        "--no-sync",
        "python3",
        PROBE,
        str(id_file),
    ]
    t0 = time.monotonic()
    proc = subprocess.run(
        cmd, cwd=REPO, env=child_env(log_dir, no_ingest=no_ingest), capture_output=True, text=True
    )
    wall = time.monotonic() - t0
    if proc.returncode != 0 or not id_file.exists():
        raise RuntimeError(f"probe {tag} failed rc={proc.returncode}: {proc.stderr[-800:]}")
    run_id = id_file.read_text().strip()
    ingest_ms = None
    for f in log_dir.glob("*.jsonl*"):
        for line in f.read_text(errors="replace").splitlines():
            if "ingest.after_write" in line and "after_write_failed" not in line:
                rec = json.loads(line)
                ms = _find_key(rec, "duration_ms")
                if ms is not None:
                    ingest_ms = float(ms)
    shutil.rmtree(REPO / "outputs" / run_id[:8], ignore_errors=True)
    log.info("probe %s id=%s wall=%.2fs ingest_ms=%s", tag, run_id, wall, ingest_ms)
    return run_id, wall, ingest_ms


def _find_key(obj, key):
    if isinstance(obj, dict):
        if key in obj:
            return obj[key]
        for v in obj.values():
            r = _find_key(v, key)
            if r is not None:
                return r
    return None


def cli_reads(
    run_id: str, catalog: Path | None, since: str = "2h"
) -> dict[str, tuple[bool, float]]:
    cat = ["--catalog-dir", str(catalog)] if catalog else []
    calls = {
        "cli_show": ["bth", "show", "--run-id", run_id, *cat],
        "cli_ls": ["bth", "ls", "--project", PROJECT, "--since", since, "-n", "1000", *cat],
        "cli_sql": ["bth", "sql", "--sql", f"SELECT id FROM runs WHERE id = '{run_id}'", *cat],
    }
    out = {}
    env = {k: v for k, v in os.environ.items() if k not in SCRUB_ENV}
    for name, cmd in calls.items():
        t0 = time.monotonic()
        proc = subprocess.run(cmd, cwd=REPO, env=env, capture_output=True, text=True)
        dt = time.monotonic() - t0
        if name == "cli_show":
            seen = proc.returncode == 0 and run_id in proc.stdout
        else:
            seen = False
            if proc.returncode == 0:
                payload = json.loads(proc.stdout)
                rows = payload.get("runs") if name == "cli_ls" else payload.get("rows")
                seen = any(run_id in json.dumps(r) for r in rows or [])
        out[name] = (seen, dt)
    return out


def inproc_reads(run_id: str, catalog: Path) -> dict[str, tuple[bool, float]]:
    from bathos.cli_common import parse_since
    from bathos.query import find_runs, get_run, run_sql

    out = {}
    t0 = time.monotonic()
    seen = get_run(run_id, catalog) is not None
    out["py_get_run"] = (seen, time.monotonic() - t0)
    t0 = time.monotonic()
    runs = find_runs(catalog, since=parse_since("2h"), project=PROJECT)
    out["py_find_runs"] = (any(r.id == run_id for r in runs), time.monotonic() - t0)
    t0 = time.monotonic()
    rows = run_sql(f"SELECT id FROM runs WHERE id = '{run_id}'", catalog_dir=catalog)
    out["py_run_sql"] = (len(rows) > 0, time.monotonic() - t0)
    return out


def stale_refold(snapshot: Path, run_id: str) -> tuple[bool, bool, float]:
    """Rewind all watermarks in the snapshot, re-fold; return (seen_before, seen_after, secs)."""
    import duckdb

    from bathos.query import run_sql
    from bathos.runlog.ingest import run_ingest

    seen_before = (
        len(run_sql(f"SELECT id FROM runs WHERE id = '{run_id}'", catalog_dir=snapshot)) > 0
    )
    con = duckdb.connect(str(snapshot / "index.db"))
    n_wm = con.execute("SELECT count(*) FROM ingest_watermarks").fetchone()[0]
    con.execute("DELETE FROM ingest_watermarks")
    con.close()
    log.info("stale snapshot: deleted %d watermarks; seen_before=%s", n_wm, seen_before)
    t0 = time.monotonic()
    rep = run_ingest(snapshot)
    secs = time.monotonic() - t0
    log.info("full re-fold %.2fs report=%s", secs, rep)
    seen_after = (
        len(run_sql(f"SELECT id FROM runs WHERE id = '{run_id}'", catalog_dir=snapshot)) > 0
    )
    return seen_before, seen_after, secs


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--n", type=int, default=20, help="probe runs with the trigger on")
    ap.add_argument("--out", type=Path, default=REPO / "outputs" / "b4_cutover_read_latency.json")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")

    live = live_catalog()
    if not (live / "cutover.json").exists():
        raise SystemExit(f"{live} is not in log mode (no cutover.json)")
    installed, origin = trigger_installed()
    log.info("trigger_installed=%s (%s)", installed, origin)

    result: dict = {"trigger_installed": installed, "installed_runner": origin, "n_probes": args.n}
    tmp = Path(tempfile.mkdtemp(prefix="b4-"))
    try:
        work = tmp / "work"
        work.mkdir()
        snap = tmp / "snapshot"
        snap.mkdir()
        shutil.copy2(live / "cutover.json", snap / "cutover.json")
        shutil.copy2(live / "index.db", snap / "index.db")

        # Step 2: sensitivity control (no ingest -> every reader must miss).
        ctrl_id, _, _ = probe_run(work, 0, no_ingest=True)
        ctrl = {**cli_reads(ctrl_id, None), **inproc_reads(ctrl_id, live)}
        result["neg_control_detected"] = not any(seen for seen, _ in ctrl.values())
        result["neg_control_seen_by"] = ",".join(k for k, (s, _) in ctrl.items() if s)
        result["ctrl_run_id"] = ctrl_id

        # Step 3: stale snapshot, full re-fold must surface the control run.
        before, after, refold_s = stale_refold(snap, ctrl_id)
        result["stale_snapshot_had_ctrl"] = before
        result["stale_refold_visible"] = (not before) and after
        result["full_refold_s"] = refold_s

        # Step 4: N probe runs with the trigger on.
        cli_lat: list[float] = []
        py_lat: list[float] = []
        per_reader: dict[str, list[float]] = {}
        misses: list[str] = []
        walls: list[float] = []
        ingest_ms: list[float] = []
        for i in range(1, args.n + 1):
            rid, wall, ims = probe_run(work, i, no_ingest=False)
            walls.append(wall)
            if ims is not None:
                ingest_ms.append(ims)
            reads = {**cli_reads(rid, None), **inproc_reads(rid, live)}
            for name, (seen, dt) in reads.items():
                per_reader.setdefault(name, []).append(dt)
                (cli_lat if name.startswith("cli_") else py_lat).append(dt)
                if not seen:
                    misses.append(f"{rid[:8]}:{name}")
        result["all_visible"] = not misses
        result["misses"] = ",".join(misses)
        result["n_cli_reads"] = len(cli_lat)
        result["p95_read_s"] = p95(cli_lat)
        result["median_read_s"] = statistics.median(cli_lat)
        result["max_read_s"] = max(cli_lat)
        result["p95_inproc_read_s"] = p95(py_lat)
        for name, xs in per_reader.items():
            result[f"p95_{name}_s"] = p95(xs)
        result["n_ingest_samples"] = len(ingest_ms)
        result["median_ingest_s"] = (
            statistics.median(ingest_ms) / 1000 if ingest_ms else float("nan")
        )
        result["p95_ingest_s"] = p95(ingest_ms) / 1000 if ingest_ms else float("nan")
        result["median_run_wall_s"] = statistics.median(walls)

        # Step 5: legacy baseline on a copy of the frozen warm DB.
        legacy = tmp / "legacy"
        legacy.mkdir()
        shutil.copy2(live / "bathos.db.frozen", legacy / "bathos.db")
        import duckdb

        con = duckdb.connect(str(legacy / "bathos.db"), read_only=True)
        # Newest bathos-project run in the frozen DB, with a --since window that covers it
        # (the live reads use 2h because their probe runs are seconds old).
        base_id, age_h = con.execute(
            "SELECT id, ceil(epoch(now() - timestamp) / 3600) + 1 FROM runs "
            "WHERE project_slug = ? ORDER BY timestamp DESC LIMIT 1",
            [PROJECT],
        ).fetchone()
        con.close()
        result["baseline_since_h"] = int(age_h)
        base_lat: list[float] = []
        base_seen = True
        for _ in range(5):
            for _name, (seen, dt) in cli_reads(base_id, legacy, since=f"{int(age_h)}h").items():
                base_lat.append(dt)
                base_seen &= seen
        result["baseline_p95_read_s"] = p95(base_lat)
        result["baseline_median_read_s"] = statistics.median(base_lat)
        result["baseline_all_seen"] = base_seen
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    log.info("result %s", json.dumps(result, indent=1))
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(result, indent=1))
    rp = os.environ.get("BTH_RESULTS_PATH")
    if rp:
        Path(rp).write_text(json.dumps(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
