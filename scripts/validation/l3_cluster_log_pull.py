"""L3 cluster test for the run-log pull (spec 261012_retire-compact-runlog-cutover, B0.3).

A SLURM job on the remote ran `bth run` with BTH_LOG_MODE=1 inside a scratch
project, so its run exists only as log lines under `<remote_root>/.bth/log/`
(plus the remote fallback/mirror). This script, run locally, checks the chain
that cut-over depends on:

1. `bathos.sync.pull_cluster_log` (via `myxcel pull-path`) brings the remote log
   home, and `ClusterLogPullResult.pulled` contains "log".
2. Negative control: the same pull against a remote root that does not exist
   must put "log" in `failures`, never in `pulled`.
3. After registering the scratch root and ingesting into a scratch catalog in
   log mode, `get_run(<fixture run id>)` finds the run.

Isolation: the pull uses the real HOME (myxcel and ssh need it) but writes only
under `--scratch`. Registration, ingest and the query run with HOME redirected to
`--scratch/home` and a scratch catalog, so the real ~/.bth is never touched.

Results go to BTH_RESULTS_PATH (when run under `bth run`) and to `--out`.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
from pathlib import Path

log = logging.getLogger("l3_cluster_log_pull")


def _make_local_root(root: Path, slug: str, project_id: str) -> None:
    root.mkdir(parents=True, exist_ok=True)
    if not (root / ".git").exists():
        subprocess.run(["git", "init", "-q"], cwd=root, check=True)
    (root / ".gitignore").write_text(".bth/log/\n")
    (root / ".bth.toml").write_text(
        f'[project]\nslug = "{slug}"\nroot = "{root}"\nid = "{project_id}"\n'
    )


def _pull(remote: str, slug: str, remote_root: str, local_root: Path, project_id: str):
    from bathos.config import ProjectConfig
    from bathos.sync import pull_cluster_log

    config = ProjectConfig(
        slug=slug,
        root=local_root,
        remotes={remote: {"host": remote, "remote_root": remote_root}},
    )
    return pull_cluster_log(remote, config, local_root, project_id)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--remote", default="engaging")
    ap.add_argument("--slug", default="bth-l3-scratch")
    ap.add_argument("--project-id", required=True)
    ap.add_argument("--remote-root", required=True)
    ap.add_argument("--bad-remote-root", required=True)
    ap.add_argument("--run-id", required=True, help="fixture run id printed by the SLURM job")
    ap.add_argument("--scratch", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    scratch = args.scratch.resolve()
    if scratch.exists() and any(scratch.iterdir()):
        raise SystemExit(f"--scratch must be empty or absent: {scratch}")
    local_root = scratch / "root"
    bad_root = scratch / "bad-root"
    _make_local_root(local_root, args.slug, args.project_id)
    _make_local_root(bad_root, args.slug, args.project_id)

    # 1. Positive pull (real HOME).
    pos = _pull(args.remote, args.slug, args.remote_root, local_root, args.project_id)
    log.info("positive pull: pulled=%s failures=%s", pos.pulled, pos.failures)

    # 2. Negative control: nonexistent remote root.
    neg = _pull(args.remote, args.slug, args.bad_remote_root, bad_root, args.project_id)
    log.info("negative pull: pulled=%s failures=%s", neg.pulled, neg.failures)
    neg_failed_labels = [f["label"] for f in neg.failures]

    pulled_files = sorted(
        str(p.relative_to(local_root)) for p in (local_root / ".bth" / "log").rglob("*") if p.is_file()
    )
    log.info("pulled files: %s", pulled_files)

    # 3. Isolated ingest + query.
    os.environ["HOME"] = str(scratch / "home")
    (scratch / "home" / ".bth").mkdir(parents=True, exist_ok=True)
    catalog = scratch / "catalog"
    catalog.mkdir()
    from bathos.query import get_run
    from bathos.runlog.ingest import run_ingest
    from bathos.runlog.mode import cutover_marker_path
    from bathos.runlog.project_id import register_main_root

    cutover_marker_path(catalog).write_text(
        json.dumps({"at": "2026-10-12T00:00:00Z", "bathos": "l3-scratch", "attempt": "1", "segments": []})
    )
    register_main_root(local_root)
    report = run_ingest(catalog)
    log.info("ingest report: %s", report)
    run = get_run(args.run_id, catalog)

    results = {
        "pulled_labels": ",".join(pos.pulled),
        "failure_labels": ",".join(f["label"] for f in pos.failures),
        "failure_errors": json.dumps(pos.failures)[:2000],
        "log_pulled": "log" in pos.pulled,
        "neg_log_failed": "log" in neg_failed_labels and "log" not in neg.pulled,
        "n_pulled_files": len(pulled_files),
        "fixture_visible": run is not None,
        "fixture_project_slug": run.project_slug if run is not None else "",
        "fixture_status": run.status if run is not None else "",
        "remote_root": args.remote_root,
    }
    log.info("results: %s", results)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(json.dumps(results, indent=2))
    if os.environ.get("BTH_RESULTS_PATH"):
        Path(os.environ["BTH_RESULTS_PATH"]).write_text(json.dumps(results))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
