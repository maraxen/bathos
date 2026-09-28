"""`bth migrate --to-log` (spec "Migration (quiesced cut-over, no dual-write)",
Migration steps 0-4).

Scope (delivery step 4, wave c): steps 0 ("Ids"), 1 ("Quiesce": pull + full
remote-runs mirror + reap-on-folded-status with warm reconciliation disabled
+ the squeue/submit-record refusal), 2 ("Import": legacy sources ->
`*.imported` events in a staging tree), 3 ("Build and diff": a staging-only
folded index, diffed table-by-table against the current `bathos.db`, into a
canonical, allow-listed residual report), and 4 ("Switch": the cut-over
marker as the single commit point, in order (a)-(e)). Also `--import-legacy`
(post-cut-over re-import of a stale legacy write, AC-23).

Reused, not reimplemented (see task brief): `bathos.index.connect_read` /
`connect_legacy` / `catalog_readable`; `bathos.runlog.mode.is_log_mode` /
`writers_lock` / `cutover_marker_path`; `bathos.runlog.importer.
import_legacy_catalog` (and its internals, for the multi-root split this
module needs); `bathos.runlog.ingest.fold_roots_into` /
`staging_roots_for_attempt` (the folds, via the ingest builder extracted for
this wave); `bathos.reap.reap_runs(reconcile_warm=...)`; `bathos.verify.
verify_runlog`; `bathos.sync.sync_catalog` / the myxcel wrappers for the
pull.

Every local write in this module is made ONLY under the writers lock (shared
for step 1, exclusive from the re-check onward through step 4), per "Mode".
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import subprocess
import tempfile
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb

from bathos.config import ProjectConfig, default_catalog_dir, load_project_config
from bathos.index import connect_legacy, index_db_path
from bathos.runlog.envelope import build_envelope
from bathos.runlog.importer import (
    CANON_VERSION,
    ImportReport,
    _all_candidates,
    _existing_chains,
    _import_eid,
    _read_staging_events,
    _source_sha256,
)
from bathos.runlog.ingest import (
    delete_staging_watermarks,
    discover_roots,
    fold_roots_into,
    read_folded_tables,
    staging_roots_for_attempt,
)
from bathos.runlog.mode import cutover_marker_path, writers_lock
from bathos.runlog.project_id import list_registered_roots, read_project_id
from bathos.runlog.writer import mirror_dir_for

logger = logging.getLogger(__name__)

# Primary-key column(s) per folded table (spec "Fold rules" tables; used for
# both the staging-only diff build and the residual report's `key` field).
_TABLE_KEYS: dict[str, list[str]] = {
    "runs": ["id"],
    "campaigns": ["id"],
    "campaign_runs": ["campaign_id", "run_id"],
    "campaign_edges": ["child_campaign_id", "parent_campaign_id"],
    "run_edges": ["child_run_id", "parent_run_id"],
    "sidecar_anchors": ["id"],
    "blast_radius_ledger": ["id"],
    "trust_ledger": ["id"],
    "archived_items": ["record_id"],
    "submits": ["id"],
}


class MigrateToLogError(RuntimeError):
    """A `bth migrate --to-log` precondition failed hard enough to raise
    rather than return a structured refusal -- reserved for programmer
    errors (e.g. calling `--import-legacy` machinery directly with bad
    arguments), not the ordinary refusal paths, which return a
    `MigrateToLogResult` instead."""


@dataclass(frozen=True)
class ResidualLine:
    """One line of the Migration step 3 canonical residual report: exactly
    `{table, key, column, class, legacy_value, staged_value}`, `key` a JSON
    array (the row's primary key), and each value `[v]` or `null` (spec:
    "each value `[v]` ... or `null` when the row is absent on that side")."""

    table: str
    key: list[Any]
    column: str
    cls: str
    legacy_value: list[Any] | None
    staged_value: list[Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "key": self.key,
            "column": self.column,
            "class": self.cls,
            "legacy_value": self.legacy_value,
            "staged_value": self.staged_value,
        }


@dataclass
class MigrateToLogResult:
    """The outcome of one `migrate_to_log()` call. `status` is one of:
    `missing_project_ids`, `foreign_catalogs`, `squeue_unavailable`,
    `squeue_conflict`, `step1_reap_failed`, `legacy_db_locked`,
    `unclassified_residual`, `residual_pending`, `switched`,
    `already_migrated`, `dry_run`.

    `foreign_catalogs` (spec Migration step 0 amendment, debt #1998): at
    least one registered root still explicitly configures `[project]
    catalog_dir` to somewhere other than the catalog being migrated -- a
    per-project catalog_dir is obsolete under the project-local run log
    (its runs would otherwise be invisible to migration, and post-cut-over
    it would keep writing legacy-mode into a catalog with no cutover
    marker: split brain). Checked BEFORE `dry_run` branches, so a dry run
    reports it too. `foreign_catalogs` on the result is a list of
    `{"root": str, "catalog_dir": str}`, mirroring `would_pull`'s shape.
    Refused unconditionally -- there is no `--force` override, since the
    fix (`bth migrate --consolidate-catalog <root>`, then drop `catalog_dir`
    from that root's `.bth.toml` and commit) is mechanical, not a judgment
    call like the squeue conflict `--force` overrides.

    `dry_run` (spec "Dry run"): `migrate_to_log(..., dry_run=True)` performed
    NO data writes anywhere -- steps 1-3 ran read-effect-only (reap with
    `apply=False`, remote pull/mirror skipped entirely) and the residual
    report was built in a throwaway scratch directory, never persisted.
    `would_pull` is populated only for this status: the `(root, remote)`
    pairs a real run's step 1 would have pulled/mirrored, since a dry run's
    report is built WITHOUT that mirroring and may therefore differ from a
    real run's (a genuinely un-mirrored remote fragment cannot show up as a
    residual here). `report_sha256` on a `dry_run` result is advisory only
    -- it is never a value `--accept-residual` should be pre-supplied with
    (rejected together with `dry_run`, see `migrate_to_log`), and a
    subsequent real run recomputes its own hash independently."""

    status: str
    attempt: str | None = None
    report_path: str | None = None
    report_sha256: str | None = None
    residual_lines: list[dict[str, Any]] = field(default_factory=list)
    unclassified: list[dict[str, Any]] = field(default_factory=list)
    missing_project_ids: list[str] = field(default_factory=list)
    foreign_catalogs: list[dict[str, str]] = field(default_factory=list)
    conflicting_jobs: list[str] = field(default_factory=list)
    locked_source: str | None = None
    unresolved_routing_count: int = 0
    would_pull: list[dict[str, str]] = field(default_factory=list)
    detail: str = ""


# --------------------------------------------------------------------------
# Step 0: ids
# --------------------------------------------------------------------------


def _run_git_text(args: list[str], cwd: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _has_git_repo(root: Path) -> bool:
    return _run_git_text(["rev-parse", "--is-inside-work-tree"], root) is not None


def _parse_project_id_toml_text(text: str) -> str | None:
    import tomlkit

    try:
        doc = tomlkit.parse(text)
    except Exception:  # noqa: BLE001 -- unparsable committed content is "no id"
        return None
    project = doc.get("project")
    if not isinstance(project, dict):
        return None
    value = project.get("id")
    return str(value) if value else None


def committed_project_id(root: Path) -> str | None:
    """`[project] id`, read from `.bth.toml`'s committed `HEAD` for a git
    root, or from the working file directly when there is no git repository
    (spec Migration step 0): "every registered root's `.bth.toml` has a
    `[project] id` present in its committed `HEAD`, or, for a root with no
    git repository, present in the file."""
    if _has_git_repo(root):
        text = _run_git_text(["show", "HEAD:.bth.toml"], root)
        if text is None:
            return None
        return _parse_project_id_toml_text(text)
    return read_project_id(root / ".bth.toml")


def roots_missing_project_id() -> list[Path]:
    """Every registered, still-existing root lacking a committed project id
    (spec Migration step 0)."""
    missing = []
    for root in list_registered_roots():
        if not root.exists():
            continue
        if committed_project_id(root) is None:
            missing.append(root)
    return missing


def explicit_catalog_dir(root: Path) -> Path | None:
    """`root`'s `.bth.toml` `[project] catalog_dir`, only if the key is
    EXPLICITLY present -- never `config.py`'s default-catalog fallback
    (`load_project_config` always returns a `catalog_dir`, defaulted when
    the key is absent, which would make "explicit vs. default" impossible
    to tell apart here). Resolved the same way `config.py`'s
    `load_project_config` resolves it (`Path(...).expanduser()`); there is
    no relative-path handling to mirror -- `config.py` does none either.

    Read directly from the current working `.bth.toml` (not `git show
    HEAD:...`, unlike `committed_project_id`): a catalog location is a live
    runtime setting a local `bth run` reads off the working file every
    time, not something Migration step 0's committed-HEAD discipline
    governs.

    Only the flat `[project] catalog_dir` form is recognised, matching
    `config.py`'s `load_project_config` exactly -- the nested `[project.
    catalog] catalog_dir` form is not read by `config.py` (so a project
    using it is, in practice, on the default catalog) and is deliberately
    ignored here too, per the same rule.
    """
    cfg_path = root / ".bth.toml"
    if not cfg_path.is_file():
        return None
    try:
        with open(cfg_path, "rb") as f:
            data = tomllib.load(f)
    except (OSError, tomllib.TOMLDecodeError):
        return None
    project = data.get("project")
    if not isinstance(project, dict) or "catalog_dir" not in project:
        return None
    raw = project["catalog_dir"]
    if not raw:
        return None
    return Path(str(raw)).expanduser()


def roots_with_foreign_catalog(cd: Path) -> list[tuple[Path, Path]]:
    """Every registered, still-existing root whose `.bth.toml` explicitly
    sets `[project] catalog_dir` to somewhere other than `cd` (spec
    Migration step 0 amendment, debt #1998): `(root, resolved_catalog)`
    pairs, in registry order. A root with no `catalog_dir` key at all (the
    common case -- tests' roots included) never appears here."""
    out: list[tuple[Path, Path]] = []
    resolved_cd = cd.resolve()
    for root in list_registered_roots():
        if not root.exists():
            continue
        configured = explicit_catalog_dir(root)
        if configured is None:
            continue
        if configured.resolve() != resolved_cd:
            out.append((root, configured))
    return out


# --------------------------------------------------------------------------
# Registered roots + their config
# --------------------------------------------------------------------------


def _registered_roots_with_config() -> list[tuple[Path, ProjectConfig | None]]:
    out: list[tuple[Path, ProjectConfig | None]] = []
    for root in list_registered_roots():
        if not root.exists():
            continue
        cfg = None
        cfg_path = root / ".bth.toml"
        if cfg_path.is_file():
            with contextlib.suppress(Exception):
                cfg = load_project_config(cfg_path)
        out.append((root, cfg))
    return out


def root_id(root: Path) -> str:
    """A filesystem-safe, stable id for `root`'s resolved absolute path, for
    use as a single PATH COMPONENT (`remote-runs/<root id>/<remote>/`,
    `import-staging/<attempt>/project/<root id>/`) -- never the raw resolved
    path itself: `Path(a) / "project" / "/abs/path"` silently discards `a`
    and `"project"` (`Path.__truediv__` treats an absolute-looking operand
    as a full replacement, not a join), which is not a hypothetical -- it
    silently dropped every staged event into the wrong directory tree
    before this fix. `root_id` is a pure, deterministic function of the
    resolved path, so a later invocation (a fresh process resuming Migration
    step 4(c)) recomputes the SAME id from `list_registered_roots()` without
    needing any extra persisted mapping.
    """
    return hashlib.sha256(str(root.resolve()).encode()).hexdigest()


# --------------------------------------------------------------------------
# Step 1: quiesce -- pull, full remote-runs mirror, reap, squeue refusal
# --------------------------------------------------------------------------


def pull_and_mirror_all_remotes(catalog_dir: Path) -> None:
    """Migration step 1: pull the legacy catalog from every configured
    remote (the existing `sync.py` rsync, via `sync_catalog(pull=True)`),
    and additionally mirror each remote's `runs/` in full (no
    `--ignore-existing`; `--checksum`, deleting nothing) into
    `remote-runs/<root id>/<remote>/`. A registered root with no configured
    remotes (or a catalog with no registered roots at all) is a no-op for
    that root -- there is nothing to pull.

    Each step is best-effort per remote: one remote's failure (unreachable
    host, misconfigured entry) must not abort the pull for every OTHER
    registered root/remote.
    """
    from bathos.sync import sync_catalog

    for root, cfg in _registered_roots_with_config():
        if cfg is None or not cfg.remotes:
            continue
        rid = root_id(root)
        for remote_name in cfg.remotes:
            with contextlib.suppress(Exception):
                sync_catalog(remote_name, cfg, catalog_dir, pull=True)
            with contextlib.suppress(Exception):
                mirror_remote_runs_full(remote_name, cfg, catalog_dir, rid)


def mirror_remote_runs_full(
    remote_name: str, config: ProjectConfig, catalog_dir: Path, rid: str
) -> None:
    """Full mirror of one remote's `runs/` into
    `~/.bth/catalog/remote-runs/<root id>/<remote>/` (spec Migration step 1:
    root id from the project's `main_root`, because remote names such as
    `engaging` repeat across projects; never into local `runs/`). `rid` is
    `root_id(root)` -- a filesystem-safe id, not the raw resolved path."""
    from bathos.cluster_catalog import remote_catalog_path

    remote_config = config.remotes[remote_name]
    host = remote_config["host"]
    remote_root = remote_config["remote_root"]
    remote_cat = remote_catalog_path(remote_root)
    dest = catalog_dir / "remote-runs" / rid / remote_name
    dest.mkdir(parents=True, exist_ok=True)
    src = f"{host}:{remote_cat}/runs/"
    rsync_full_mirror(src, str(dest) + "/")


def rsync_full_mirror(src: str, dst: str) -> None:
    """The actual rsync invocation for `mirror_remote_runs_full` -- its own
    function so tests can monkeypatch this ONE seam (the myxcel/SSH
    boundary) rather than every caller.

    Deliberately calls `rsync` directly rather than going through myxcel
    (verified 260927, see `bathos.cluster.pull_path`'s docstring for the
    full trail): myxcel has no capability that pulls an arbitrary remote
    directory into an arbitrary local destination with `--checksum`
    comparison and no deletion. `myxcel pull` only pulls a
    myxcel-*registered project*'s configured `pull_paths` into
    `profile.local_workspace/<project>` (or a worktree's `local_root`); this
    mirror's destination (`catalog_dir/remote-runs/<root id>/<remote>/`) is
    neither a registered project nor addressable by project name, and its
    semantics (full checksum mirror, delete-nothing locally, no
    `--ignore-existing`) don't match any myxcel pull mode either. If myxcel
    grows a generic path-pull subcommand or importable rsync-wrapper API,
    route this through it instead of inventing flags on `myxcel pull`.
    """
    subprocess.run(
        [
            "rsync",
            "-az",
            "--checksum",
            "-e",
            "ssh -o ConnectTimeout=10 -o BatchMode=yes",
            src,
            dst,
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def _running_slurm_job_ids(catalog_dir: Path) -> set[str]:
    """Job ids from submit records and `running` run rows (legacy tier,
    since this check runs before cut-over) -- spec Migration step 1: "any
    job in the user's `squeue` has a job id found in submit records or in a
    `running` run's `slurm_job_id`."""
    from bathos.catalog import read_runs

    ids: set[str] = set()
    submits_dir = catalog_dir / "submits"
    if submits_dir.is_dir():
        import pyarrow.parquet as pq

        for f in submits_dir.rglob("*_submit.parquet"):
            if f.name.endswith(".tmp"):
                continue
            with contextlib.suppress(Exception):
                tbl = pq.read_table(f)
                if "slurm_job_id" in tbl.column_names:
                    for v in tbl.column("slurm_job_id").to_pylist():
                        if v:
                            ids.add(str(v))
    with contextlib.suppress(Exception):
        for run in read_runs(catalog_dir):
            if run.status == "running" and run.slurm_job_id:
                ids.add(str(run.slurm_job_id))
    return ids


class SqueueUnavailableError(RuntimeError):
    """`squeue` could not be queried at all (unreachable host, timeout, or a
    non-zero exit) -- fails CLOSED. Review finding (HIGH, 260927): the
    earlier version of `my_squeue_job_ids()` returned `[]` on ANY of these,
    which is indistinguishable from "checked, no jobs" -- a genuinely
    unreachable cluster would silently let migration proceed as if the
    queue were empty. This is refused UNCONDITIONALLY (never overridable by
    `--force`, which only overrides an actual, successfully-observed
    conflict): the spec gives `--force` no role in "couldn't check"."""


def my_squeue_job_ids() -> list[str]:
    """`squeue --me --noheader --format=%A`, via the transparently-wrapped
    command (myxcel). Its own function so tests can mock the cluster
    boundary directly rather than shelling out (no real SSH). Raises
    `SqueueUnavailableError` rather than returning `[]` on any failure to
    query it -- see that class's docstring."""
    try:
        result = subprocess.run(
            ["squeue", "--me", "--noheader", "--format=%A"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except OSError as exc:
        raise SqueueUnavailableError(f"squeue could not be run: {exc}") from exc
    except subprocess.TimeoutExpired as exc:
        raise SqueueUnavailableError(f"squeue timed out: {exc}") from exc
    if result.returncode != 0:
        stderr = (result.stderr or "").strip()
        stdout = (result.stdout or "").strip()
        raise SqueueUnavailableError(
            f"squeue exited {result.returncode}: {stderr or stdout or '(no output)'}"
        )
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def squeue_conflict(catalog_dir: Path) -> list[str]:
    """Raises `SqueueUnavailableError` (propagated from `my_squeue_job_ids`)
    rather than ever silently reporting "no conflict" when the queue itself
    could not be checked."""
    known = _running_slurm_job_ids(catalog_dir)
    mine = my_squeue_job_ids()
    return sorted(j for j in mine if j in known)


def _check_squeue(catalog_dir: Path) -> tuple[list[str], str | None]:
    """`(conflicting_jobs, unavailable_message)`. `unavailable_message` is
    set (and `conflicting_jobs` empty) when `squeue` itself could not be
    queried -- callers must refuse on this UNCONDITIONALLY, `--force` or
    not (see `SqueueUnavailableError`)."""
    try:
        return squeue_conflict(catalog_dir), None
    except SqueueUnavailableError as exc:
        return [], str(exc)


def _run_step1_shared(
    catalog_dir: Path, *, force: bool
) -> tuple[list[str], set[str], str | None, str | None]:
    """Step 1's shared-lock phase: pull + mirror + reap(reconcile_warm=False),
    then the squeue/submit-record refusal. Returns `(conflicting_jobs,
    step1_touched_run_ids, squeue_unavailable_message, reap_error_message)`
    -- `step1_touched_run_ids` feeds step 3's `step1_pulled_or_reaped`
    residual class (spec: "a staged status the fragment carries" / "a
    `metadata.reaped` equal to a step-1 ledger record" are both runs THIS
    pass itself touched).

    Review finding (MEDIUM, 260927): a `reap_runs()` failure used to be
    silently swallowed (`contextlib.suppress(Exception)`), letting a
    half-reaped catalog proceed as if nothing had failed. `reap_error_message`
    now surfaces it; the caller aborts on it (the stricter option) rather
    than continuing on unknown state. `squeue_conflict` is skipped entirely
    when reap failed -- there is nothing useful to check against a state
    step 1 itself could not finish establishing.
    """
    from bathos.reap import reap_runs

    touched: set[str] = set()
    reap_error: str | None = None
    with writers_lock(catalog_dir, exclusive=False):
        pull_and_mirror_all_remotes(catalog_dir)
        try:
            candidates, _skipped = reap_runs(catalog_dir, apply=True, reconcile_warm=False)
            touched.update(r.id for r in candidates)
        except Exception as exc:  # noqa: BLE001 -- surfaced to the caller, not swallowed
            reap_error = str(exc)

        if reap_error is not None:
            return [], touched, None, reap_error

        remote_runs_dir = catalog_dir / "remote-runs"
        if remote_runs_dir.is_dir():
            import pyarrow.parquet as pq

            for f in remote_runs_dir.rglob("run_*.parquet"):
                if f.name.endswith(".tmp.parquet"):
                    continue
                with contextlib.suppress(Exception):
                    tbl = pq.read_table(f, columns=["id"])
                    touched.update(v for v in tbl.column("id").to_pylist() if v)

        conflict, squeue_error = _check_squeue(catalog_dir)
        if squeue_error is not None:
            return [], touched, squeue_error, None
        if force:
            conflict = []
    return conflict, touched, None, None


# --------------------------------------------------------------------------
# Step 2: import -- legacy sources -> staged *.imported events
# --------------------------------------------------------------------------


def _attempt_staging_root(attempt: str) -> Path:
    return Path.home() / ".bth" / "log" / "import-staging" / attempt


def _new_attempt_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]


def _cleanup_prior_attempt_state(catalog_dir: Path) -> None:
    """Re-run semantics (spec Migration step 4, "Re-running `bth migrate
    --to-log`"): with the marker absent, delete every leftover staging
    attempt directory and `index.db` -- never a step-1 legacy source (those
    stay and are re-imported).

    Review finding (LOW, 260927): also sweeps stray temp files an earlier,
    interrupted attempt could have left behind -- `index.db.<gen>.tmp` (a
    step 4(a) build killed before its `os.replace`) and
    `.import-<attempt>-<n>.jsonl.tmp` (a step 4(c) segment copy killed
    mid-write) in every registered root's log dir, its mirror, and
    unaffiliated + its mirror. Neither is load-bearing for correctness (a
    `.tmp` file is never read by anything), but leaving them around forever
    is still a real, unbounded disk leak across repeated aborted attempts.
    """
    staging_base = Path.home() / ".bth" / "log" / "import-staging"
    if staging_base.is_dir():
        shutil.rmtree(staging_base, ignore_errors=True)
    idx_path = index_db_path(catalog_dir)
    idx_path.unlink(missing_ok=True)
    with contextlib.suppress(OSError):
        idx_path.with_name(idx_path.name + ".wal").unlink()
    for tmp in catalog_dir.glob("index.db.*.tmp"):
        with contextlib.suppress(OSError):
            tmp.unlink()
        with contextlib.suppress(OSError):
            tmp.with_name(tmp.name + ".wal").unlink()

    search_dirs = [Path.home() / ".bth" / "log" / "unaffiliated", mirror_dir_for(None, None)]
    for root, _cfg in _registered_roots_with_config():
        search_dirs.append(root / ".bth" / "log")
        slug, pid = _root_project_info(root)
        search_dirs.append(mirror_dir_for(pid, slug))
    for d in search_dirs:
        if not d.is_dir():
            continue
        for tmp in d.glob(".import-*.jsonl.tmp"):
            with contextlib.suppress(OSError):
                tmp.unlink()


def _append_import_events(
    candidates: list,
    dest: Path,
    *,
    project: str | None,
    project_id: str | None,
    main_root: Path,
) -> int:
    """Append `*.imported` / `legacy_source.unreadable` events for
    `candidates` into `dest`, stamped with ONE destination's
    project/project_id/main_root. The per-destination half of
    `import_legacy_catalog`'s own per-candidate logic (spec Migration step
    2: "one subtree per destination project log or unaffiliated"),
    extracted here so both the multi-root staging split and post-cut-over
    `--import-legacy`'s multi-root case can route candidates individually
    -- something a single `import_legacy_catalog` call (which stamps ONE
    project onto every event it emits) cannot do.
    """
    existing = _existing_chains(_read_staging_events(dest))
    new_lines: list[str] = []
    appended = 0
    for cand in candidates:
        chain_key = (cand.kind, *cand.entity, cand.source_class, cand.source_locator)
        if cand.kind == "legacy_source.unreadable":
            sha_val = cand.fields.get("byte_sha256", "")
        else:
            sha_val = _source_sha256(cand.fields)
        prior = existing.get(chain_key)
        if prior is not None and prior[1] == sha_val:
            continue
        ordinal = 0 if prior is None else prior[0] + 1
        data = dict(cand.fields)
        data.update(
            {
                "source_class": cand.source_class,
                "source_locator": cand.source_locator,
                "canon": CANON_VERSION,
                "snapshot": ordinal,
                "source_sha256": sha_val,
            }
        )
        eid = _import_eid(
            cand.kind, cand.entity, cand.source_class, cand.source_locator, ordinal, sha_val
        )
        env = build_envelope(
            kind=cand.kind,
            entity=cand.entity,
            data=data,
            main_root=main_root,
            worktree_root=main_root,
            project=project,
            project_id=project_id,
            writer="importer",
            seq=len(new_lines) + 1,
            origin="migration",
            ts=cand.ts,
            eid=eid,
        )
        new_lines.append(json.dumps(env, sort_keys=True))
        existing[chain_key] = (ordinal, sha_val)
        appended += 1

    if new_lines:
        dest.mkdir(parents=True, exist_ok=True)
        seg_path = dest / f"import_{uuid.uuid4().hex[:12]}.jsonl"
        tmp_path = seg_path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for line in new_lines:
                fh.write(line + "\n")
        tmp_path.replace(seg_path)
    return appended


def _route_candidates(
    candidates: list, roots: list[tuple[Path, ProjectConfig | None]]
) -> tuple[dict[Path | None, list], int, set[str]]:
    """Bucket every candidate by its resolved OWNING root (`None` ==
    unaffiliated). Returns `(buckets, unresolved_count, unresolved_ids)`:
    the count and the set of each unresolved candidate's OWN entity id
    (`entity[0]` -- a run id, campaign id, or edge/ledger source id) feed
    step 3's `unresolvable_project` residual class, so a diff on an entity
    whose project genuinely could not be determined (never because there
    simply is no registered root) is classified rather than left
    unclassified -- a real multi-project catalog can see how many entities
    need attention.

    With 0 or 1 registered roots the routing is unambiguous (everything
    goes there, or to unaffiliated with none registered) -- `unresolved_
    count`/`unresolved_ids` are always empty there.

    With 2+ registered roots (review finding (a), 260927 -- the real
    catalog has 10+ projects sharing one catalog): resolved via the
    candidate's own `project_slug` field when it carries one directly
    (runs, campaigns, submits, archived items); else via its PARENT
    entity's project -- `campaign_run.imported`/a `campaign` edge via the
    campaign's project, a `run` edge / `blast_radius.imported` /
    `trust_ledger.imported` keyed on a run via THAT run's project,
    `anchor.imported` via its `campaign_id` field or (fallback) its `path`
    resolving under one registered root's own filesystem prefix. Only when
    NONE of that resolves does a candidate fall to `unaffiliated/`
    (`legacy_source.unreadable` always does -- it carries no entity data
    at all to resolve a parent from).
    """
    if not roots:
        return {None: list(candidates)}, 0, set()
    if len(roots) == 1:
        root, _cfg = roots[0]
        return {root: list(candidates)}, 0, set()

    slug_to_root = {cfg.slug: root for root, cfg in roots if cfg is not None}
    root_to_slug = {root: cfg.slug for root, cfg in roots if cfg is not None}
    resolved_roots = {root.resolve(): root for root, _cfg in roots}

    # Pass 1: index the direct project_slug every run/campaign candidate
    # already carries on its own fields, so pass 2 can resolve their
    # dependents (edges, campaign_runs, ledgers, anchors) by parent lookup.
    run_project: dict[str, str] = {}
    campaign_project: dict[str, str] = {}
    for cand in candidates:
        fields = cand.fields if isinstance(cand.fields, dict) else {}
        slug = fields.get("project_slug")
        if not slug or not cand.entity:
            continue
        if cand.kind in ("run.imported", "run_reap.imported"):
            run_project[cand.entity[0]] = slug
        elif cand.kind == "campaign.imported":
            campaign_project[cand.entity[0]] = slug

    def _resolve_slug(cand) -> str | None:
        fields = cand.fields if isinstance(cand.fields, dict) else {}
        slug = fields.get("project_slug")
        if slug:
            return slug
        entity = cand.entity or []
        if cand.kind == "campaign_run.imported" and entity:
            return campaign_project.get(entity[0])
        if cand.kind == "edge.imported":
            etype = fields.get("type")
            src = fields.get("src") or (entity[0] if entity else None)
            if etype == "campaign":
                return campaign_project.get(src)
            return run_project.get(src)
        if cand.kind == "trust_ledger.imported":
            return run_project.get(fields.get("run_id"))
        if cand.kind == "blast_radius.imported":
            etype = fields.get("entity_type")
            eid = fields.get("entity_id")
            if etype == "run":
                return run_project.get(eid)
            if etype == "campaign":
                return campaign_project.get(eid)
            return None
        if cand.kind == "anchor.imported":
            campaign_id = fields.get("campaign_id")
            if campaign_id:
                via_campaign = campaign_project.get(campaign_id)
                if via_campaign:
                    return via_campaign
            path_val = fields.get("path")
            if path_val:
                p = Path(path_val)
                if p.is_absolute():
                    for resolved, root in resolved_roots.items():
                        with contextlib.suppress(ValueError):
                            p.relative_to(resolved)
                            return root_to_slug.get(root)
            return None
        return None

    buckets: dict[Path | None, list] = {}
    unresolved = 0
    unresolved_ids: set[str] = set()
    for cand in candidates:
        slug = _resolve_slug(cand)
        root = slug_to_root.get(slug) if slug else None
        if root is None:
            unresolved += 1
            if cand.entity:
                unresolved_ids.add(cand.entity[0])
        buckets.setdefault(root, []).append(cand)
    return buckets, unresolved, unresolved_ids


def _root_project_info(root: Path | None) -> tuple[str | None, str | None]:
    if root is None:
        return None, None
    cfg_path = root / ".bth.toml"
    slug = None
    if cfg_path.is_file():
        with contextlib.suppress(Exception):
            slug = load_project_config(cfg_path).slug
    return slug, read_project_id(cfg_path)


def _dest_for_root(root: Path | None, *, staging_root: Path | None) -> Path:
    """Where one destination bucket's events go: under a migration attempt's
    staging tree (`staging_root` given, Migration step 2) or directly into
    the owning project's real log (`staging_root=None`, post-cut-over
    `--import-legacy`, AC-23)."""
    if staging_root is not None:
        if root is None:
            return staging_root / "unaffiliated"
        return staging_root / "project" / root_id(root)
    if root is None:
        return Path.home() / ".bth" / "log" / "unaffiliated"
    return root / ".bth" / "log"


def _import_candidates(
    catalog_dir: Path, *, staging_root: Path | None
) -> tuple[ImportReport, int, set[str]]:
    """The shared body of Migration step 2 and `--import-legacy`: scan every
    legacy source once (`_all_candidates`, AC-18's `connect_legacy`), route
    each candidate to its owning destination, and append. `staging_root`
    picks staging (step 2) vs. direct project logs (`--import-legacy`).
    Returns `(report, unresolved_routing_count, unresolved_ids)` -- the
    latter two from `_route_candidates` (empty for a 0/1-root catalog).
    """
    report = ImportReport()
    candidates = _all_candidates(catalog_dir, report)
    roots = _registered_roots_with_config()
    buckets, unresolved, unresolved_ids = _route_candidates(candidates, roots)
    for root, bucket in buckets.items():
        dest = _dest_for_root(root, staging_root=staging_root)
        slug, pid = _root_project_info(root)
        main_root = root if root is not None else catalog_dir
        n = _append_import_events(bucket, dest, project=slug, project_id=pid, main_root=main_root)
        report.appended += n
    return report, unresolved, unresolved_ids


# --------------------------------------------------------------------------
# Step 3: build and diff
# --------------------------------------------------------------------------


def _jsonify_value(v: Any) -> Any:
    if hasattr(v, "isoformat"):
        return v.isoformat()
    return v


#: `sidecar_anchors` is excluded from the diff entirely: the legacy warm
#: `id` is a fresh `uuid4` per insert/rebuild (`anchor.py:228`,
#: `compact.py:751`), while the folded index's `id` is the content-derived
#: `uuid5(path, sha256)` (`fold_anchors`/`importer._warm_anchor_candidates`)
#: -- the two sides can never share a key by construction, matching this
#: codebase's own documented precedent ("AC-17 does not compare it", spec
#: "Authoritative writes"). A real reconciliation would need to key by
#: `(path, sha256)` instead; deferred (see the task report).
_DIFF_EXCLUDED_TABLES = frozenset({"sidecar_anchors"})


def _read_all_tables(
    con: duckdb.DuckDBPyConnection,
) -> tuple[dict[str, dict[tuple, dict[str, Any]]], dict[str, set[str]], dict[str, bool]]:
    """`(rows_by_table, columns_by_table, table_exists)`. `columns_by_table`
    is the set of columns the physical table ACTUALLY has (from `cur.
    description`), kept separate from any one row's own keys so the diff can
    tell "this table predates this column" (schema drift -- nothing to
    compare) apart from "this row has no value for this column" (a real,
    per-row absence). `table_exists` is False for a table the legacy schema
    never created at all (e.g. `submits`, which `compact.py` never builds --
    "no legacy warm precedent", `bathos/index.py`'s own comment) -- every
    entity in such a table is new BY DEFINITION, not a residual to report.
    """
    out: dict[str, dict[tuple, dict[str, Any]]] = {}
    columns: dict[str, set[str]] = {}
    exists: dict[str, bool] = {}
    for table, keys in _TABLE_KEYS.items():
        try:
            cur = con.execute(f"SELECT * FROM {table}")  # noqa: S608 -- table is an internal constant
        except duckdb.CatalogException:
            out[table] = {}
            columns[table] = set()
            exists[table] = False
            continue
        exists[table] = True
        cols = [d[0] for d in cur.description]
        columns[table] = set(cols)
        rows: dict[tuple, dict[str, Any]] = {}
        for r in cur.fetchall():
            row = {c: _jsonify_value(v) for c, v in zip(cols, r, strict=True)}
            key = tuple(row.get(k) for k in keys)
            rows[key] = row
        out[table] = rows
    return out, columns, exists


def _read_legacy_tables(
    catalog_dir: Path,
) -> tuple[
    dict[str, dict[tuple, dict[str, Any]]], dict[str, set[str]], dict[str, bool], str | None
]:
    """`(tables, columns_by_table, table_exists, locked_status)`.
    `locked_status` is `"legacy_db_locked"` or `"corrupt_legacy_source"` when
    the CURRENT `bathos.db` (not a fragment -- AC-18's `connect_legacy`)
    could not be opened; a missing `bathos.db` (never compacted before
    cut-over) is simply the empty catalog, not an error."""
    db_path = catalog_dir / "bathos.db"
    if not db_path.exists():
        empty = {t: {} for t in _TABLE_KEYS}
        return empty, {t: set() for t in _TABLE_KEYS}, {t: False for t in _TABLE_KEYS}, None
    opened = connect_legacy(db_path)
    if isinstance(opened, dict):
        return {}, {}, {}, opened.get("status")
    try:
        rows, columns, exists = _read_all_tables(opened)
        return rows, columns, exists, None
    finally:
        opened.close()


def _parse_output_metadata(value: Any) -> dict[str, dict[str, Any]] | None:
    """`runs.output_metadata`'s JSON-in-VARCHAR shape (a list of `{"path":
    ..., "status": ..., "size_bytes": ..., "mtime_unix": ..., "sha256":
    ...}` entries, `compact.py`'s `_collect_output_metadata`), parsed into
    `{path: entry}` -- or `None` if the value isn't in that shape
    (conservative: an unparseable or unexpected value never earns
    `output_metadata_drift`, it stays unclassified like anything else this
    class doesn't recognize)."""
    if isinstance(value, str):
        if not value:
            value = []
        else:
            try:
                value = json.loads(value)
            except (ValueError, TypeError):
                return None
    if value is None:
        value = []
    if not isinstance(value, list):
        return None
    out: dict[str, dict[str, Any]] = {}
    for entry in value:
        if not isinstance(entry, dict) or "path" not in entry:
            return None
        out[entry["path"]] = entry
    return out


def _sidecar_current_sha256(path: str) -> str | None:
    """The CURRENT sha256 of a sidecar file on disk, or `None` if it no
    longer exists (BC-3: "a deleted one gives NULL in the canonical
    state")."""
    p = Path(path)
    if not p.is_file():
        return None
    with contextlib.suppress(OSError):
        return hashlib.sha256(p.read_bytes()).hexdigest()
    return None


def _classify(
    table: str,
    key: tuple,
    column: str,
    lrow: dict[str, Any] | None,
    srow: dict[str, Any] | None,
    *,
    step1_touched_run_ids: set[str],
    cool_run_rows: dict[str, dict[str, Any]],
    unresolved_ids: set[str],
) -> str | None:
    """Assign one allow-listed class (spec Migration step 3) to a residual
    row, or `None` (unclassified -- aborts the migration).

    Implements:

    - `step1_pulled_or_reaped` (AC-30): the run entity was touched by THIS
      invocation's own step-1 pull/reap.
    - `warm_only_row` (AC-30): the entity has a warm row but no legacy
      source at all (nothing staged for it).
    - `unresolvable_project` (review finding (b), 260927): the entity's own
      project could not be determined by `_route_candidates` (routed to
      `unaffiliated/`) -- a residual on it is explained by "we don't know
      which project this belongs to", not a genuine loss.
    - `fragment_not_yet_compacted` (spec: "a fragment not yet compacted into
      bathos.db, including those just pulled or reaped in step 1: a column
      of such a run whose staged value equals the value that fragment or a
      reap ledger ... carries"): checked directly against the run's CURRENT
      cool fragment, not scoped to any one invocation's step 1 -- a run
      reaped by an ordinary (non-migration) `reap_runs()` call, or by an
      EARLIER aborted migration attempt's step 1, is exactly as "not yet
      compacted" as one reaped by THIS attempt's step 1; the spec's own
      wording ("including those just pulled or reaped in step 1") frames
      step 1 as one example source of this class, not its only trigger.
    - `frozen_at_earlier_compact` for `runs.metadata` specifically -- the
      same underlying phenomenon (a warm row compacted before a later
      ledger/postmortem merge) for the one column not fully explained by a
      single cool-fragment field comparison (`metadata.reaped` is merged in
      from a separate ledger record, not the fragment's own `metadata`
      column, whenever `reconcile_warm_tier`'s merge -- not just a plain
      fragment rewrite -- is what lagged).
    - `sidecar_edited_evalue` (BC-3, review finding (b)): an `evalue` column
      (`runs` or `campaign_runs`) whose folded run's frozen `sidecar_path`
      either no longer exists or no longer hashes to its frozen
      `sidecar_sha256` -- the legacy campaign pass re-parses the CURRENT
      file on every compact, so an edited or deleted sidecar changes its
      warm e-value while the fold (frozen at `run.started.data`) does not.
    - `campaign_evalue_confound` (BC-7, review finding (b)): an `evalue`
      column on a run carrying a postmortem verdict override -- the legacy
      campaign pass always computes from the fragment's RAW outcome
      (`compact.py:1044` rebinds `run` before the postmortem override at
      `:1061`), while the fold uses the folded (postmortem-overridden)
      outcome.
    - `force_rebuild_loss` (review finding (b)): the entity has a staged
      fold but NO legacy row at all (any table) -- e.g. BC-8's non-durable
      `CatalogAnchorStore` anchors, which write no fragment, so a
      force-rebuild drops them while the fold (driven by durable events)
      keeps every one.
    - `output_metadata_drift` (spec: "`output_metadata` whose files changed
      since"; debt #1944, review finding (b), 260927; **narrowed 260927**,
      review remediation B finding 1): a `runs.output_metadata` residual
      where BOTH sides parse as the `_collect_output_metadata` list shape,
      name the SAME set of paths (no output file was added or dropped --
      that would be a different, unexplained kind of drift), every
      differing path is workspace-RELATIVE (never absolute), AND -- the
      narrow part -- for every differing path the two sides' `status`
      values are exactly `{"missing", "present"}` (one of each, in either
      order). `compact.py`'s per-compact refresh (`compact.py:964-992`)
      calls `_collect_output_metadata` relative to THAT COMPACTING
      PROCESS'S OWN cwd, so a relative `output_path` can genuinely read
      `"missing"` (`_collect_output_metadata` returns
      `{"status": "missing", "size_bytes": 0}`, no `sha256`/`mtime_unix` at
      all) from one compact's cwd and `"present"` from another's -- that is
      debt #1944's actual signature, and it is the ONLY thing this class
      explains. A path that is `"present"` on both sides but with a
      different `sha256`/`size_bytes`/`mtime_unix` is a genuine output
      mutation or corruption, not cwd drift -- an absolute path's
      `Path.exists()`/stat never depends on cwd at all, so a residual on an
      absolute path is NOT explained by this mechanism either, and both
      cases must stay unclassified rather than getting the same "explained"
      label (the narrow, testable distinction this class exists to draw).
    - `postmortem_worktree_deleted`: investigated and NOT implemented --
      see the docstring's final paragraph below for why.

    Deliberately deferred (documented, not silently papered over -- see the
    task report): `corrupt_fragment_skip` (compact.py:787's corrupt-fragment
    skip appears structurally unreachable as a genuine residual given this
    importer's warm-source redundancy, the same class of finding as AC-30's
    original "warm-only row" wording before v35 corrected it).

    `postmortem_worktree_deleted` (spec: "postmortem overrides whose files
    are only in a deleted worktree") is investigated and judged structurally
    UNREACHABLE with this importer/fold, for the same reason as
    `corrupt_fragment_skip` -- no column this diff ever compares carries a
    "which worktree" signal to test against:
      - `compact.py`'s postmortem walk (`compact.py:807`,
        `resolve_workspace().fs_root`) always resolves to the repo's MAIN
        worktree (spec "Log directory resolution": a linked worktree maps to
        its main worktree), never to the run's own (possibly since-deleted)
        worktree, and its `iter_project_files` walk is boundary-aware and
        PRUNES `.claude/worktrees/` (backlog #4233) -- a postmortem file
        living inside any worktree subtree, deleted or not, is never found
        by it either way, so no before/after delta is ever produced.
      - `runs.postmortem_path` is stored workspace-root-relative
        (`compact.py`'s `rel_path = pm_file.relative_to(workspace_root)`),
        never worktree-relative, and no compared table (`_TABLE_KEYS`) has a
        `worktree_root`/similar column for a run or postmortem at all --
        only the (unrelated to this diff) per-EVENT envelope carries
        `worktree_root`, and the importer stamps it `main_root` on every
        import regardless (`_append_import_events`), so it is constant and
        carries no signal either.
      - `run.postmortem_applied` (the one live write path that could
        otherwise diverge from the stage-1 imported warm value) only fires
        behind `current_mode()` (`mcp.py:3046`), which is never true before
        cut-over -- so during a migration there is no live event of this
        kind to disagree with the import in the first place.
      A residual invented for this class without one of these signals would
      be exactly the failure this rule exists to prevent: a false-positive
      classification papering over a genuinely different bug.
    """
    run_id = key[0] if table == "runs" and key else None
    if run_id and run_id in step1_touched_run_ids:
        return "step1_pulled_or_reaped"
    if key and key[0] in unresolved_ids:
        return "unresolvable_project"
    if srow is None and lrow is not None:
        return "warm_only_row"
    if table == "runs" and run_id and run_id in cool_run_rows:
        cool_val = _jsonify_value(cool_run_rows[run_id].get(column))
        staged_val = srow.get(column) if srow is not None else None
        if _values_equal(cool_val, staged_val):
            return "fragment_not_yet_compacted"
    if table == "runs" and column == "metadata":
        return "frozen_at_earlier_compact"
    if table == "runs" and column == "output_metadata":
        legacy_entries = _parse_output_metadata(lrow.get(column) if lrow is not None else None)
        staged_entries = _parse_output_metadata(srow.get(column) if srow is not None else None)
        if (
            legacy_entries is not None
            and staged_entries is not None
            and legacy_entries
            and set(legacy_entries) == set(staged_entries)
        ):
            differing_paths = [
                p for p, entry in legacy_entries.items() if entry != staged_entries[p]
            ]
            if (
                differing_paths
                and all(not Path(p).is_absolute() for p in differing_paths)
                and all(
                    {legacy_entries[p].get("status"), staged_entries[p].get("status")}
                    == {"missing", "present"}
                    for p in differing_paths
                )
            ):
                return "output_metadata_drift"
    if column == "evalue" and table in ("runs", "campaign_runs"):
        member_run_id = key[-1] if table == "campaign_runs" and key else run_id
        run_row = cool_run_rows.get(member_run_id) if member_run_id else None
        sidecar_path = (run_row or {}).get("sidecar_path") or (srow or {}).get("sidecar_path")
        sidecar_sha256 = (run_row or {}).get("sidecar_sha256") or (srow or {}).get(
            "sidecar_sha256"
        )
        if sidecar_path:
            current_sha = _sidecar_current_sha256(sidecar_path)
            if current_sha != (sidecar_sha256 or None):
                return "sidecar_edited_evalue"
        override = (run_row or {}).get("postmortem_verdict_override") or (srow or {}).get(
            "postmortem_verdict_override"
        )
        if override and override not in ("none", ""):
            return "campaign_evalue_confound"
    if lrow is None and srow is not None:
        return "force_rebuild_loss"
    return None


@dataclass
class DiffResult:
    lines: list[ResidualLine]
    unclassified: list[dict[str, Any]]
    locked_status: str | None
    sha256: str


def _normalize_for_compare(v: Any) -> Any:
    """A JSON-encoded string parsed back to its native value when it looks
    like one, else `v` unchanged. The folded index stores list/dict-shaped
    columns (`argv`, `output_paths`, `tags`, ...) as JSON-in-VARCHAR, while
    the legacy warm schema stores several of the same columns as native
    LIST/STRUCT -- semantically identical, syntactically different Python
    values coming back from DuckDB. Comparing THIS normalized form (on both
    sides) means a real content difference still surfaces, but the storage
    representation alone never does.
    """
    if isinstance(v, str) and v[:1] in ("[", "{"):
        with contextlib.suppress(ValueError, TypeError):
            return json.loads(v)
    return v


def _values_equal(lval: Any, sval: Any) -> bool:
    ln, sn = _normalize_for_compare(lval), _normalize_for_compare(sval)
    if ln == sn:
        return True
    # A legacy warm NULL and a fold's "" default both mean "no value" for an
    # unset string field (e.g. `adversarial_check_status`, `manifest_path`) --
    # a storage-convention mismatch, not a content difference.
    return (ln is None and sn == "") or (sn is None and ln == "")


def _diff_tables(
    legacy: dict[str, dict[tuple, dict[str, Any]]],
    legacy_columns: dict[str, set[str]],
    legacy_table_exists: dict[str, bool],
    staged: dict[str, dict[tuple, dict[str, Any]]],
    *,
    step1_touched_run_ids: set[str],
    cool_run_rows: dict[str, dict[str, Any]],
    unresolved_ids: set[str],
) -> tuple[list[ResidualLine], list[dict[str, Any]]]:
    lines: list[ResidualLine] = []
    unclassified: list[dict[str, Any]] = []
    for table in _TABLE_KEYS:
        if table in _DIFF_EXCLUDED_TABLES:
            continue
        if not legacy_table_exists.get(table, True):
            # The legacy schema never created this table at all (e.g.
            # `submits`) -- every entity in it is new by definition, not a
            # residual difference to classify.
            continue
        legacy_rows = legacy.get(table, {})
        staged_rows = staged.get(table, {})
        table_cols = legacy_columns.get(table, set())
        all_keys = set(legacy_rows) | set(staged_rows)
        for key in sorted(all_keys, key=lambda k: [str(x) for x in k]):
            lrow = legacy_rows.get(key)
            srow = staged_rows.get(key)
            # A column absent from the legacy TABLE's own schema (schema
            # drift -- an older bathos.db predating a later ALTER TABLE ADD
            # COLUMN) is never compared: there is nothing on the legacy side
            # to have lost. When the legacy ROW itself is entirely absent
            # (the entity never made it into bathos.db at all), every staged
            # column is reported instead, since we don't know in advance
            # which ones "would" have mattered.
            cols = table_cols if lrow is not None else set(srow or {})
            for col in sorted(cols):
                lval = lrow.get(col) if lrow is not None else None
                sval = srow.get(col) if srow is not None else None
                l_present = lrow is not None and col in lrow
                s_present = srow is not None and col in srow
                if l_present == s_present and _values_equal(lval, sval):
                    continue
                cls = _classify(
                    table,
                    key,
                    col,
                    lrow,
                    srow,
                    step1_touched_run_ids=step1_touched_run_ids,
                    cool_run_rows=cool_run_rows,
                    unresolved_ids=unresolved_ids,
                )
                rl = ResidualLine(
                    table=table,
                    key=list(key),
                    column=col,
                    cls=cls or "unclassified",
                    legacy_value=[lval] if l_present else None,
                    staged_value=[sval] if s_present else None,
                )
                if cls is None:
                    unclassified.append(rl.to_dict())
                else:
                    lines.append(rl)
    return lines, unclassified


def _report_bytes(lines: list[ResidualLine]) -> bytes:
    json_lines = sorted(
        json.dumps(rl.to_dict(), sort_keys=True, separators=(",", ":")) for rl in lines
    )
    body = "\n".join(json_lines)
    return (body + "\n" if body else "").encode("utf-8")


def _report_sha256(lines: list[ResidualLine]) -> str:
    return hashlib.sha256(_report_bytes(lines)).hexdigest()


def _cool_fragment_run_rows(catalog_dir: Path) -> dict[str, dict[str, Any]]:
    """Every run's CURRENT cool-fragment row (spec: "a fragment not yet
    compacted into `bathos.db`"), as a plain JSON-safe dict per run id --
    the direct source `_classify`'s `fragment_not_yet_compacted` class
    checks a residual against, independent of which pass (this migration
    attempt's own step 1, an earlier aborted attempt's, or an ordinary
    `reap_runs()` call outside migration entirely) actually wrote it.
    """
    import dataclasses

    from bathos.catalog import read_runs

    out: dict[str, dict[str, Any]] = {}
    with contextlib.suppress(Exception):
        for run in read_runs(catalog_dir):
            out[run.id] = dataclasses.asdict(run)
    return out


def _staging_roots_from(staging_root: Path, attempt: str) -> list[tuple[str, str, Path]]:
    """`ingest.staging_roots_for_attempt`'s own body, parameterized on an
    explicit `staging_root` directory instead of hardcoding `Path.home() /
    ".bth" / "log" / "import-staging" / attempt`.

    `--dry-run`'s only seam for pointing Migration step 3's fold at a
    throwaway scratch tree instead of the real `~/.bth/log/import-staging/`
    -- `bathos.runlog.ingest` is owned by another fixer's task this sprint,
    so this is a small, deliberate duplication of its traversal logic rather
    than an edit there. Keep in sync with `ingest.staging_roots_for_attempt`
    if that ever changes shape.
    """
    if not staging_root.is_dir():
        return []
    roots: list[tuple[str, str, Path]] = []
    for kind_dir in sorted(staging_root.iterdir()):
        if not kind_dir.is_dir():
            continue
        if kind_dir.name == "unaffiliated":
            roots.append(("staging", f"{attempt}/unaffiliated", kind_dir))
            continue
        for sub in sorted(kind_dir.iterdir()):
            if sub.is_dir():
                roots.append(("staging", f"{attempt}/{kind_dir.name}/{sub.name}", sub))
    return roots


def _build_and_diff(
    catalog_dir: Path,
    attempt: str,
    *,
    step1_touched_run_ids: set[str],
    unresolved_ids: set[str] | None = None,
    staging_root: Path | None = None,
) -> DiffResult:
    """`staging_root`: override the attempt's staging directory (default
    `_attempt_staging_root(attempt)`, the real `~/.bth/log/import-staging/
    <attempt>`) -- `--dry-run` passes a throwaway `tempfile.mkdtemp()`
    scratch tree here instead, so the fold's index.db and the diff's own
    read of the staged events never touch the real one."""
    legacy_tables, legacy_columns, legacy_table_exists, locked_status = _read_legacy_tables(
        catalog_dir
    )
    if locked_status is not None:
        return DiffResult(lines=[], unclassified=[], locked_status=locked_status, sha256="")

    sroot = staging_root if staging_root is not None else _attempt_staging_root(attempt)
    sroot.mkdir(parents=True, exist_ok=True)
    idx_path = sroot / "index.db"
    idx_path.unlink(missing_ok=True)
    roots = (
        _staging_roots_from(sroot, attempt)
        if staging_root is not None
        else staging_roots_for_attempt(attempt)
    )
    fold_roots_into(idx_path, roots)

    staged_tables = {
        table: {
            key: {c: _jsonify_value(v) for c, v in row.items()} for key, row in rows.items()
        }
        for table, rows in read_folded_tables(idx_path, _TABLE_KEYS).items()
    }

    cool_run_rows = _cool_fragment_run_rows(catalog_dir)

    lines, unclassified = _diff_tables(
        legacy_tables,
        legacy_columns,
        legacy_table_exists,
        staged_tables,
        step1_touched_run_ids=step1_touched_run_ids,
        cool_run_rows=cool_run_rows,
        unresolved_ids=unresolved_ids or set(),
    )
    sha = _report_sha256(lines)
    return DiffResult(lines=lines, unclassified=unclassified, locked_status=None, sha256=sha)


def _write_residual_report(attempt: str, lines: list[ResidualLine]) -> Path:
    staging_root = _attempt_staging_root(attempt)
    staging_root.mkdir(parents=True, exist_ok=True)
    report_path = staging_root / "residual_report.jsonl"
    json_lines = sorted(
        json.dumps(rl.to_dict(), sort_keys=True, separators=(",", ":")) for rl in lines
    )
    tmp_path = report_path.with_suffix(".jsonl.tmp")
    tmp_path.write_text("\n".join(json_lines) + ("\n" if json_lines else ""))
    tmp_path.replace(report_path)
    return report_path


# --------------------------------------------------------------------------
# Step 4: switch
# --------------------------------------------------------------------------


def _check_no_wal_or_raise(tmp_path: Path) -> None:
    """AC-19, mirrored from `ingest._check_no_wal`: refuse the swap if a
    `.wal` remains after `close()`."""
    from bathos.runlog.ingest import IngestWalRemainsError

    wal_path = tmp_path.with_name(tmp_path.name + ".wal")
    if wal_path.exists():
        with contextlib.suppress(OSError):
            tmp_path.unlink()
        with contextlib.suppress(OSError):
            wal_path.unlink()
        raise IngestWalRemainsError(
            f"{wal_path} still present after close; refusing Migration step 4(a)'s build"
        )


def _step4a_build_index(catalog_dir: Path, attempt: str) -> None:
    real_path = index_db_path(catalog_dir)
    tmp_path = catalog_dir / f"index.db.{uuid.uuid4().hex[:12]}.tmp"
    if real_path.exists():
        shutil.copy2(real_path, tmp_path)
    roots = [*discover_roots(catalog_dir), *staging_roots_for_attempt(attempt)]
    fold_roots_into(tmp_path, roots)
    _check_no_wal_or_raise(tmp_path)
    os.replace(tmp_path, real_path)


def _step4b_write_marker(catalog_dir: Path, attempt: str, staging_root: Path) -> None:
    import bathos

    marker_path = cutover_marker_path(catalog_dir)
    segments = [str(p) for p in sorted(staging_root.rglob("*.jsonl"))]
    marker = {
        "at": datetime.now(UTC).isoformat(),
        "bathos": bathos.__version__,
        "attempt": attempt,
        "segments": segments,
    }
    tmp_marker = marker_path.with_suffix(".json.tmp")
    tmp_marker.write_text(json.dumps(marker, sort_keys=True))
    os.replace(tmp_marker, marker_path)


def _step4c_move_staged_segments(staging_root: Path) -> None:
    """Move (copy into both destinations; the original stays in staging
    until step 4(e)) each staged segment into its owning project log and
    mirror as `import-<attempt>-<n>.jsonl` -- skipped per-destination if
    already present (idempotent re-entry, spec: "skipped if already
    present")."""
    if not staging_root.is_dir():
        return
    attempt = staging_root.name
    segments = sorted(p for p in staging_root.rglob("*.jsonl") if p.name != "residual_report.jsonl")
    # Reverse `root_id(root) -> root`, recomputed fresh (never persisted --
    # `root_id` is a pure function of the resolved path, so a later process
    # resuming this step gets the identical mapping from the SAME registry).
    id_to_root = {root_id(root): root for root, _cfg in _registered_roots_with_config()}
    for n, seg in enumerate(segments, start=1):
        rel = seg.relative_to(staging_root).parts
        if not rel:
            continue
        if rel[0] == "unaffiliated":
            dest_log_dir = Path.home() / ".bth" / "log" / "unaffiliated"
            dest_mirror_dir = mirror_dir_for(None, None)
        elif rel[0] == "project" and len(rel) >= 3:
            root = id_to_root.get(rel[1])
            if root is None:
                # The registered root vanished (unregistered/deleted) between
                # step 2 and this step 4(c) resume -- leave it in staging for
                # a future retry rather than guessing a destination.
                continue
            dest_log_dir = root / ".bth" / "log"
            slug, pid = _root_project_info(root)
            dest_mirror_dir = mirror_dir_for(pid, slug)
        else:
            continue

        new_name = f"import-{attempt}-{n}.jsonl"
        for dest_dir in (dest_log_dir, dest_mirror_dir):
            dest_path = dest_dir / new_name
            if dest_path.exists():
                continue
            dest_dir.mkdir(parents=True, exist_ok=True)
            tmp_path = dest_dir / f".{new_name}.tmp"
            shutil.copy2(seg, tmp_path)
            os.replace(tmp_path, dest_path)


def _step4d_rename_warm(catalog_dir: Path) -> None:
    db_path = catalog_dir / "bathos.db"
    frozen_path = catalog_dir / "bathos.db.frozen"
    if db_path.exists() and not frozen_path.exists():
        os.replace(db_path, frozen_path)


def _step4e_delete_staging(catalog_dir: Path, attempt: str, staging_root: Path) -> None:
    delete_staging_watermarks(catalog_dir, attempt)
    shutil.rmtree(staging_root, ignore_errors=True)


def _do_switch(catalog_dir: Path, attempt: str) -> None:
    """Migration step 4, actions (a)-(e), each independently idempotent so a
    marker-present re-run can safely repeat whichever remain (spec: "the
    cut-over marker is the single commit point")."""
    marker_path = cutover_marker_path(catalog_dir)
    staging_root = _attempt_staging_root(attempt)

    if not marker_path.exists():
        _step4a_build_index(catalog_dir, attempt)
        _step4b_write_marker(catalog_dir, attempt, staging_root)

    _step4c_move_staged_segments(staging_root)
    _step4d_rename_warm(catalog_dir)
    _step4e_delete_staging(catalog_dir, attempt, staging_root)


def _finish_remainder(catalog_dir: Path, attempt: str) -> MigrateToLogResult:
    """Marker already present (this invocation, or the shared->exclusive
    re-check, found it there): finish whichever of (c)-(e) remain, or
    report `already_migrated` if there is nothing left to do."""
    staging_root = _attempt_staging_root(attempt)
    if not staging_root.exists():
        return MigrateToLogResult(status="already_migrated", attempt=attempt)
    _do_switch(catalog_dir, attempt)
    return MigrateToLogResult(status="switched", attempt=attempt)


def _read_marker(catalog_dir: Path) -> dict[str, Any] | None:
    marker_path = cutover_marker_path(catalog_dir)
    if not marker_path.is_file():
        return None
    try:
        return json.loads(marker_path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# --------------------------------------------------------------------------
# Dry run (spec "Dry run"): steps 1-3 read-effect-only, no writes anywhere
# --------------------------------------------------------------------------


def _would_pull_pairs(catalog_dir: Path) -> list[dict[str, str]]:  # noqa: ARG001 -- symmetry
    """Read-only preview of Migration step 1's per-`(root, remote)` pulls
    (`pull_and_mirror_all_remotes`) for `--dry-run`: every registered root's
    configured remote name, without any network I/O, rsync, or mirroring.
    `catalog_dir` is accepted (unused) for signature symmetry with the other
    step-1 helpers and in case a future revision scopes this per-catalog."""
    pairs: list[dict[str, str]] = []
    for root, cfg in _registered_roots_with_config():
        if cfg is None or not cfg.remotes:
            continue
        for remote_name in cfg.remotes:
            pairs.append({"root": str(root), "remote": remote_name})
    return pairs


def _dry_run_report_from_marker(marker: dict[str, Any]) -> MigrateToLogResult:
    """`--dry-run` when a cut-over marker already exists: step 4 (spec: "the
    cut-over marker is the single commit point") has already committed, so
    there is no fresh diff left to preview -- only whichever residual report
    step 3 of THAT attempt already wrote to staging, read back here (never
    recomputed, never rewritten). If staging itself is already gone (step
    4(e) completed), migration is simply done already."""
    attempt = marker.get("attempt")
    if not attempt:
        return MigrateToLogResult(
            status="unclassified_residual", detail="cutover.json has no 'attempt' field"
        )
    staging_root = _attempt_staging_root(str(attempt))
    if not staging_root.exists():
        return MigrateToLogResult(status="already_migrated", attempt=str(attempt))
    report_path = staging_root / "residual_report.jsonl"
    lines: list[dict[str, Any]] = []
    sha: str | None = None
    if report_path.is_file():
        text = report_path.read_text()
        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        lines = [json.loads(ln) for ln in text.splitlines() if ln.strip()]
    return MigrateToLogResult(
        status="dry_run",
        attempt=str(attempt),
        report_path=str(report_path) if report_path.is_file() else None,
        report_sha256=sha,
        residual_lines=lines,
        detail=(
            "a prior attempt already reached the cut-over commit point (step 4(b)); this "
            "reflects its already-written residual report, not a fresh preview -- re-run "
            "`bth migrate --to-log` (without --dry-run) to finish it"
        ),
    )


def _migrate_to_log_dry_run(cd: Path, *, force: bool) -> MigrateToLogResult:
    """`--dry-run`: steps 1-3 run for their READ effects only -- no data
    FILES are written to the catalog dir, `~/.bth/projects.toml`,
    `~/.bth/log/`, any project root, or any remote (spec "Dry run"). The one
    exception: `writers_lock` (below) may still create the empty
    `writers.lock` mutex file under the catalog dir if it does not already
    exist -- every reader takes that lock shared, dry run or not, and the
    lock file itself carries no data (see `bathos.runlog.mode.writers_lock`).

    - Remote pull/mirror (`pull_and_mirror_all_remotes`) never runs; `
      would_pull` reports the `(root, remote)` pairs a real run would have
      pulled instead, so a caller knows the report below may be missing
      not-yet-locally-mirrored remote fragments (see the result's `detail`).
    - `reap_runs(..., apply=False)` -- its candidate list becomes the
      step1-touched set (`_classify`'s `step1_pulled_or_reaped`), matching
      the spec's "steps 1-3 run for real (read effects only)".
    - The squeue check still runs (read-only) so a caller previewing the
      residual report also sees a genuine conflict, if any.
    - Step 2's staged import events, step 3's throwaway fold index, and the
      diff all live under one `tempfile.mkdtemp()` scratch directory,
      removed in a `finally` -- never under `catalog_dir` or
      `~/.bth/log/import-staging/`.
    - `_cleanup_prior_attempt_state` never runs (no prior attempt state is
      touched, per the spec: no cleanup of prior attempt state in dry run).
    - Never reaches `_do_switch`; the returned `report_sha256` is advisory
      only (see `MigrateToLogResult.dry_run`'s docstring).
    """
    marker = _read_marker(cd)
    if marker is not None:
        return _dry_run_report_from_marker(marker)

    would_pull = _would_pull_pairs(cd)

    from bathos.reap import reap_runs

    with writers_lock(cd, exclusive=False):
        candidates, _skipped = reap_runs(cd, apply=False, reconcile_warm=False)
        step1_touched = {r.id for r in candidates}
        conflict, squeue_error = _check_squeue(cd)

    if squeue_error is not None:
        return MigrateToLogResult(
            status="squeue_unavailable", detail=squeue_error, would_pull=would_pull
        )
    if conflict and not force:
        return MigrateToLogResult(
            status="squeue_conflict", conflicting_jobs=conflict, would_pull=would_pull
        )

    scratch_root = Path(tempfile.mkdtemp(prefix="bth-migrate-dryrun-"))
    try:
        attempt = _new_attempt_id()
        staging_root = scratch_root / "staging"

        import_report, unresolved_routing, unresolved_ids = _import_candidates(
            cd, staging_root=staging_root
        )
        if import_report.locked:
            return MigrateToLogResult(
                status="legacy_db_locked",
                attempt=attempt,
                locked_source=import_report.locked[0],
                detail="a legacy source is locked by another process; retry once it is free",
                would_pull=would_pull,
            )

        diff = _build_and_diff(
            cd,
            attempt,
            step1_touched_run_ids=step1_touched,
            unresolved_ids=unresolved_ids,
            staging_root=staging_root,
        )
        if diff.locked_status is not None:
            return MigrateToLogResult(
                status="legacy_db_locked",
                attempt=attempt,
                locked_source=str(cd / "bathos.db"),
                detail=f"bathos.db: {diff.locked_status}",
                would_pull=would_pull,
            )

        return MigrateToLogResult(
            status="dry_run",
            attempt=attempt,
            report_sha256=diff.sha256,
            residual_lines=[rl.to_dict() for rl in diff.lines],
            unclassified=diff.unclassified,
            unresolved_routing_count=unresolved_routing,
            would_pull=would_pull,
            detail=(
                f"dry run: no data files were written (the empty writers.lock mutex "
                f"may still be created); {len(diff.lines)} residual line(s), "
                f"{len(diff.unclassified)} unclassified. Remote fragments were not "
                "mirrored for this preview (see would_pull) -- a real run's report may "
                "differ because remotes get pulled and mirrored first. This sha256 is "
                "advisory only: a real `bth migrate --to-log` recomputes its own report "
                "independently, and that is the hash `--accept-residual` must match."
            ),
        )
    finally:
        shutil.rmtree(scratch_root, ignore_errors=True)


# --------------------------------------------------------------------------
# Top-level entry points
# --------------------------------------------------------------------------


def migrate_to_log(
    catalog_dir: Path | None = None,
    *,
    force: bool = False,
    accept_residual: str | None = None,
    dry_run: bool = False,
) -> MigrateToLogResult:
    """`bth migrate --to-log` (Migration steps 0-4). `dry_run=True` (spec
    "Dry run") runs steps 1-3 read-effect-only and returns status `dry_run`
    instead of writing anything or ever reaching step 4's switch; combining
    it with `accept_residual` is rejected outright (a dry run never reaches
    the switch, so there is nothing to accept a residual report for)."""
    if dry_run and accept_residual:
        raise ValueError(
            "migrate_to_log(): --accept-residual is incompatible with --dry-run -- a dry "
            "run never reaches the switch, so there is nothing to accept a residual "
            "report for. Drop --accept-residual, or drop --dry-run and re-run for real."
        )

    cd = catalog_dir or default_catalog_dir()

    missing = roots_missing_project_id()
    if missing:
        return MigrateToLogResult(
            status="missing_project_ids",
            missing_project_ids=[str(p) for p in missing],
            detail=(
                "every registered root needs a committed `[project] id` in `.bth.toml` "
                "before migration; run `bth init --assign-id` in each listed root, commit "
                "the result, and retry."
            ),
        )

    foreign = roots_with_foreign_catalog(cd)
    if foreign:
        return MigrateToLogResult(
            status="foreign_catalogs",
            foreign_catalogs=[{"root": str(r), "catalog_dir": str(c)} for r, c in foreign],
            detail=(
                "a per-project [project] catalog_dir is obsolete under the project-local "
                "run log; for each root listed, run `bth migrate --consolidate-catalog "
                "<root>`, then remove catalog_dir from that root's .bth.toml and commit "
                "the result before retrying `bth migrate --to-log`."
            ),
        )

    if dry_run:
        return _migrate_to_log_dry_run(cd, force=force)

    marker = _read_marker(cd)
    if marker is not None:
        attempt = marker.get("attempt")
        if not attempt:
            return MigrateToLogResult(
                status="unclassified_residual", detail="cutover.json has no 'attempt' field"
            )
        with writers_lock(cd, exclusive=True):
            return _finish_remainder(cd, str(attempt))

    conflict, step1_touched, squeue_error, reap_error = _run_step1_shared(cd, force=force)
    if reap_error is not None:
        return MigrateToLogResult(
            status="step1_reap_failed",
            detail=(
                f"step 1's reap_runs() failed: {reap_error}; aborting rather than "
                "proceeding on a possibly half-reaped catalog"
            ),
        )
    if squeue_error is not None:
        return MigrateToLogResult(status="squeue_unavailable", detail=squeue_error)
    if conflict:
        return MigrateToLogResult(status="squeue_conflict", conflicting_jobs=conflict)

    with writers_lock(cd, exclusive=True):
        marker = _read_marker(cd)
        if marker is not None:
            attempt = marker.get("attempt")
            if attempt:
                return _finish_remainder(cd, str(attempt))

        conflict2, squeue_error2 = _check_squeue(cd)
        if squeue_error2 is not None:
            return MigrateToLogResult(status="squeue_unavailable", detail=squeue_error2)
        if conflict2 and not force:
            return MigrateToLogResult(status="squeue_conflict", conflicting_jobs=conflict2)

        _cleanup_prior_attempt_state(cd)
        attempt = _new_attempt_id()

        import_report, unresolved_routing, unresolved_ids = _import_candidates(
            cd, staging_root=_attempt_staging_root(attempt)
        )
        if import_report.locked:
            return MigrateToLogResult(
                status="legacy_db_locked",
                attempt=attempt,
                locked_source=import_report.locked[0],
                detail="a legacy source is locked by another process; retry once it is free",
            )

        diff = _build_and_diff(
            cd,
            attempt,
            step1_touched_run_ids=step1_touched,
            unresolved_ids=unresolved_ids,
        )
        if diff.locked_status is not None:
            return MigrateToLogResult(
                status="legacy_db_locked",
                attempt=attempt,
                locked_source=str(cd / "bathos.db"),
                detail=f"bathos.db: {diff.locked_status}",
            )
        if diff.unclassified:
            return MigrateToLogResult(
                status="unclassified_residual",
                attempt=attempt,
                unclassified=diff.unclassified,
                detail=(
                    f"{len(diff.unclassified)} residual difference(s) do not fall into any "
                    "allow-listed class; migration aborted, staging left in place for "
                    "inspection"
                ),
            )

        report_path = _write_residual_report(attempt, diff.lines)
        if accept_residual != diff.sha256:
            return MigrateToLogResult(
                status="residual_pending",
                attempt=attempt,
                report_path=str(report_path),
                report_sha256=diff.sha256,
                residual_lines=[rl.to_dict() for rl in diff.lines],
                unresolved_routing_count=unresolved_routing,
                detail=(
                    "review the residual report, then re-run with "
                    f"--accept-residual {diff.sha256}"
                ),
            )

        _do_switch(cd, attempt)
        return MigrateToLogResult(
            status="switched",
            attempt=attempt,
            report_path=str(report_path),
            report_sha256=diff.sha256,
            unresolved_routing_count=unresolved_routing,
        )


def import_legacy_post_cutover(catalog_dir: Path | None = None) -> MigrateToLogResult:
    """`bth migrate --import-legacy` (spec Migration step 2 / step 5 / AC-23):
    refuses before cut-over; after it, imports every legacy source directly
    into the owning project's real `.bth/log/` (unresolvable ones to
    `unaffiliated/`)."""
    cd = catalog_dir or default_catalog_dir()
    if _read_marker(cd) is None:
        return MigrateToLogResult(
            status="refused_before_cutover",
            detail=(
                "bth migrate --import-legacy refuses to run before cut-over; "
                "run `bth migrate --to-log` first."
            ),
        )
    with writers_lock(cd, exclusive=True):
        report, unresolved_routing, _unresolved_ids = _import_candidates(cd, staging_root=None)
    if report.locked:
        return MigrateToLogResult(
            status="legacy_db_locked",
            locked_source=report.locked[0],
            detail="a legacy source is locked by another process; retry once it is free",
        )
    return MigrateToLogResult(
        status="imported",
        unresolved_routing_count=unresolved_routing,
        detail=f"appended={report.appended} unchanged={report.unchanged}",
    )


# --------------------------------------------------------------------------
# Consolidation: fold a registered root's foreign catalog_dir into the
# catalog being migrated (spec Migration step 0 amendment, debt #1998).
# --------------------------------------------------------------------------

#: Only these subtrees (plus `runs/`, matched by id -- see `_plan_runs`) are
#: ever copied -- never `bathos.db*`, `index.db`, `cutover.json`,
#: `writers.lock`, `logs/`, `remote-runs/`, or anything else not explicitly
#: listed (spec: "restricted to an allowlist of subtrees"). `anchors/`,
#: `ledger/`, `blast_radius/`, `archived_items/` are flat directories keyed
#: by a globally-unique per-fragment uuid in the filename (`anchor_<uuid>.
#: parquet`, `ledger_<id>.parquet`, `blast_radius_<id>.parquet`,
#: `archived_<record_id>.parquet`), so path-matching is exact for them the
#: same way it is for `campaigns/submits/reaped/sidecars` -- there is no
#: cross-path id collision to resolve the way `runs/` needs. Attestations
#: (`attestation.py`'s `_ATTESTATIONS_DIRNAME`) live at
#: `<catalog_dir>/sidecars/attestations/<sha256>.attestation.bth.toml`,
#: already inside the `sidecars` subtree, so no separate entry is needed for
#: them.
_CONSOLIDATE_PATH_MATCHED_SUBTREES: tuple[str, ...] = (
    "campaigns",
    "submits",
    "reaped",
    "sidecars",
    "anchors",
    "ledger",
    "blast_radius",
    "archived_items",
)

#: Every allowlisted top-level subtree -- `runs/` plus the path-matched set
#: above -- used to compute `skipped_unknown` (fix 2, debt #1998 review):
#: anything at `source`'s top level that is neither allowlisted nor an
#: explicitly-excluded name/pattern is reported rather than silently
#: ignored (e.g. `harness_runs/`, `quarantine/` -- real catalog subtrees
#: that exist today but are deliberately out of scope for this pass).
_CONSOLIDATE_ALLOWED_TOP_LEVEL: frozenset[str] = frozenset(
    {"runs", *_CONSOLIDATE_PATH_MATCHED_SUBTREES}
)

#: Top-level names/patterns that are excluded on purpose (never copied, and
#: never reported as `skipped_unknown` either -- the exclusion is the
#: documented behavior, not an oversight).
_CONSOLIDATE_EXCLUDED_EXACT: frozenset[str] = frozenset(
    {"index.db", "cutover.json", "writers.lock", "remote-runs", "logs"}
)

_CONSOLIDATE_CONFLICT_PATH_CAP = 200

#: Fragment names to never treat as real content in any subtree (fix 4,
#: debt #1998 review): an in-flight tmp-write-then-rename target from this
#: module or another writer (`*.tmp`, `*.tmp.parquet`, `*.json.tmp`,
#: `*.parquet.tmp`) or a dotfile. Checked against the leaf filename only.
_CONSOLIDATE_TEMP_SUFFIXES: tuple[str, ...] = (
    ".tmp",
    ".tmp.parquet",
    ".json.tmp",
    ".parquet.tmp",
)

#: Characters/values that make a `project_slug` unsafe to use as a single
#: path component (fix 7, debt #1998 review) -- a source fragment claiming
#: one of these is reported unplaceable rather than trusted.
_INVALID_SLUG_VALUES: frozenset[str] = frozenset({".", ".."})
_INVALID_SLUG_CHARS: tuple[str, ...] = ("/", "\\", "\x00")


@dataclass
class ConsolidateResult:
    """The outcome of one `consolidate_project_catalog()` call. `status` is
    one of `ok` (clean -- no conflicts, nothing unplaceable), `needs_review`
    (the copy still ran additively on apply, but `conflicts > 0` or
    `unplaceable` is non-empty and must be reviewed before retrying `bth
    migrate --to-log`), `refused_log_mode` (dest already cut over -- see the
    function docstring), `refused_source_log_mode` (source already cut over
    -- see the function docstring), `source_missing`, `same_catalog`. Only
    `ok`/`needs_review` ever populate the counts below. `applied` mirrors
    the `apply` argument verbatim (True even when the plan was empty) -- it
    is NOT "something was copied"; check `copied` for that.

    `already_present_differs` and `conflict_paths` are both capped at
    `_CONSOLIDATE_CONFLICT_PATH_CAP` entries; `already_present_differs_count`
    and `conflicts`/`len(conflict_paths)` (uncapped counts, tracked
    separately per `per_subtree`) give the true totals when either list is
    truncated. `skipped_unknown` (fix 2, debt #1998 review) lists every
    top-level entry in `source` that is neither copied nor part of the
    documented exclusion list -- never silently dropped."""

    status: str
    applied: bool = False
    copied: int = 0
    already_present: int = 0
    already_present_differs: list[str] = field(default_factory=list)
    already_present_differs_count: int = 0
    conflicts: int = 0
    conflict_paths: list[str] = field(default_factory=list)
    unplaceable: list[str] = field(default_factory=list)
    skipped_unknown: list[str] = field(default_factory=list)
    per_subtree: dict[str, dict[str, int]] = field(default_factory=dict)
    detail: str = ""


@dataclass
class _PlanItem:
    """One queued copy: `src` -> `dest`, `subtree` names which counter
    bucket it belongs to (`"runs"` or one of
    `_CONSOLIDATE_PATH_MATCHED_SUBTREES`). `run_id`/`rel` carry the bit of
    identity needed to reclassify the item if `dest` turns out to have
    appeared between planning and copy (fix 3, TOCTOU)."""

    src: Path
    dest: Path
    subtree: str
    run_id: str = ""
    rel: str = ""


@dataclass
class _ConsolidationPlan:
    per_subtree: dict[str, dict[str, int]]
    all_plan: list[_PlanItem]
    conflict_paths: list[str]
    unplaceable: list[str]
    differs: list[str]
    skipped_unknown: list[str]


def _files_identical(a: Path, b: Path) -> bool:
    try:
        if a.stat().st_size != b.stat().st_size:
            return False
        return a.read_bytes() == b.read_bytes()
    except OSError:
        return False


def _is_consolidate_temp_name(name: str) -> bool:
    """True for an in-flight write's tmp file or a dotfile (fix 4) -- never
    real content in any consolidated subtree."""
    if name.startswith("."):
        return True
    return name.endswith(_CONSOLIDATE_TEMP_SUFFIXES)


def _is_consolidate_excluded_top_level(name: str) -> bool:
    """True for a top-level `source` entry that is excluded on purpose
    (never copied, never reported as `skipped_unknown`)."""
    if name in _CONSOLIDATE_EXCLUDED_EXACT:
        return True
    if name.startswith("bathos.db"):
        return True
    return name.endswith(".log")


def _find_skipped_unknown(source: Path) -> list[str]:
    """Every top-level entry in `source` that is neither allowlisted nor
    explicitly excluded (fix 2) -- e.g. `harness_runs/`, `quarantine/`."""
    out: list[str] = []
    if not source.is_dir():
        return out
    for entry in sorted(source.iterdir()):
        name = entry.name
        if name in _CONSOLIDATE_ALLOWED_TOP_LEVEL or _is_consolidate_excluded_top_level(name):
            continue
        out.append(name)
    return out


def _run_id_from_filename(name: str) -> str:
    """`run_<id>.parquet` -> `<id>` -- the same id stored in the fragment's
    own `id` column, used only for the human-facing
    `already_present_differs` list (fix 6)."""
    stem = name
    if stem.endswith(".parquet"):
        stem = stem[: -len(".parquet")]
    if stem.startswith("run_"):
        stem = stem[len("run_") :]
    return stem


def _sanitize_project_slug(slug: str) -> str | None:
    """`None` for a `project_slug` unsafe to use as a single path component
    (fix 7): empty, `.`/`..`, or containing `/`, `\\`, or NUL."""
    if not slug:
        return None
    if slug in _INVALID_SLUG_VALUES:
        return None
    if any(ch in slug for ch in _INVALID_SLUG_CHARS):
        return None
    return slug


def _read_run_project_slug(path: Path) -> str | None:
    """The `project_slug` column of a single run fragment, read cheaply
    (one column, no full-table load) -- or `None` if it is missing, empty,
    or the file cannot be read at all (a source catalog scan must never
    abort on one bad fragment)."""
    import pyarrow.parquet as pq

    try:
        tbl = pq.read_table(path, columns=["project_slug"])
    except Exception:  # noqa: BLE001 -- unreadable/corrupt source fragment, not fatal here
        return None
    if tbl.num_rows == 0:
        return None
    val = tbl.column("project_slug")[0].as_py()
    return str(val) if val else None


def _index_dest_run_fragments(dest: Path) -> dict[str, Path]:
    """`{filename: path}` for every run fragment already anywhere under
    `dest/runs/` -- matched by FILENAME (the run id lives in it, e.g.
    `run_<id>.parquet`), not by relative path, so a flat-layout source
    fragment whose same run already lives at `dest/runs/<slug>/run_<id>.
    parquet` is recognised as already present rather than duplicated
    (real case: asr has 322 legacy fragments sitting flat in a foreign
    catalog's `runs/` whose same run is already compacted into the
    migration catalog under its slug subdir)."""
    runs_dir = dest / "runs"
    if not runs_dir.is_dir():
        return {}
    out: dict[str, Path] = {}
    for f in runs_dir.rglob("run_*.parquet"):
        if _is_consolidate_temp_name(f.name):
            continue
        out.setdefault(f.name, f)
    return out


def _plan_runs(
    source: Path, dest: Path
) -> tuple[list[tuple[Path, Path]], dict[str, int], list[str], list[str]]:
    """Copy plan for `source/runs/` (spec 260927 refinement -- run fragments
    are matched by run id, i.e. filename, ANYWHERE under `dest/runs/`, never
    by relative path; new ones land under `dest/runs/<project_slug>/`, the
    slug read off the fragment's OWN `project_slug` column, not its source
    path, so a flat-layout source lands in the canonical layout).

    Returns `(plan, counts, unplaceable_rel_paths, already_present_differs)`,
    the last now a list of run ids (fix 6), not a count. `counts` has
    `copied`/`already_present` (the diff-by-bytes case for an existing id is
    NOT a conflict for runs -- it is reported via the 4th return value
    instead, dest always winning, per the same refinement). A within-source
    duplicate (fix 5) is byte-compared against the FIRST queued occurrence
    of the same run id, not silently dropped: identical -> already_present,
    different -> also reported via the differs list. A `project_slug` that
    fails `_sanitize_project_slug` (fix 7), or whose resulting dest path
    would not resolve under `dest/runs/`, is reported unplaceable rather
    than trusted."""
    runs_src = source / "runs"
    plan: list[tuple[Path, Path]] = []
    counts = {"copied": 0, "already_present": 0}
    unplaceable: list[str] = []
    differs: list[str] = []
    if not runs_src.is_dir():
        return plan, counts, unplaceable, differs

    dest_index = _index_dest_run_fragments(dest)
    runs_root = (dest / "runs").resolve()
    first_seen: dict[str, Path] = {}
    for f in sorted(runs_src.rglob("run_*.parquet")):
        if _is_consolidate_temp_name(f.name):
            continue
        run_id = _run_id_from_filename(f.name)
        existing = dest_index.get(f.name)
        if existing is not None:
            if _files_identical(f, existing):
                counts["already_present"] += 1
            else:
                differs.append(run_id)
            continue
        prior = first_seen.get(f.name)
        if prior is not None:
            # A second source fragment for the same run id (e.g. both a
            # flat and a slug-subdir copy sitting in the source itself,
            # neither yet in dest) -- byte-compare against the first queued
            # occurrence rather than assuming identity (fix 5).
            if _files_identical(f, prior):
                counts["already_present"] += 1
            else:
                differs.append(run_id)
            continue
        raw_slug = _read_run_project_slug(f) or f.parent.name
        if raw_slug == "runs":
            # No project_slug column and the fragment sits directly under
            # runs/ itself (no subdir to fall back to either) -- nowhere
            # canonical to place it; report rather than guess.
            unplaceable.append(str(f.relative_to(source)))
            continue
        slug = _sanitize_project_slug(raw_slug)
        if slug is None:
            unplaceable.append(str(f.relative_to(source)))
            continue
        dest_path = dest / "runs" / slug / f.name
        if not dest_path.resolve().is_relative_to(runs_root):
            unplaceable.append(str(f.relative_to(source)))
            continue
        plan.append((f, dest_path))
        first_seen[f.name] = f
        counts["copied"] += 1
    return plan, counts, unplaceable, differs


def _plan_path_matched(
    source: Path, dest: Path, subtree: str
) -> tuple[list[tuple[Path, Path]], dict[str, int], list[str]]:
    """Copy plan for one of `_CONSOLIDATE_PATH_MATCHED_SUBTREES`: matched by
    relative path (unlike `runs/`), byte-identical existing files are
    skipped, and a same-path file with different bytes is a genuine
    conflict -- never overwritten. Temp/dotfile names (fix 4) are skipped
    entirely, never queued and never counted."""
    sub_src = source / subtree
    plan: list[tuple[Path, Path]] = []
    counts = {"copied": 0, "already_present": 0, "conflicts": 0}
    conflict_paths: list[str] = []
    if not sub_src.is_dir():
        return plan, counts, conflict_paths
    for f in sorted(
        p for p in sub_src.rglob("*") if p.is_file() and not _is_consolidate_temp_name(p.name)
    ):
        rel = f.relative_to(source)
        dest_path = dest / rel
        if dest_path.exists():
            if _files_identical(f, dest_path):
                counts["already_present"] += 1
            else:
                counts["conflicts"] += 1
                conflict_paths.append(str(rel))
            continue
        plan.append((f, dest_path))
        counts["copied"] += 1
    return plan, counts, conflict_paths


def _build_consolidation_plan(source: Path, dest: Path) -> _ConsolidationPlan:
    """Compute the full copy plan (all subtrees) without writing anything.
    Callers decide whether this runs under `dest`'s writers lock (fix 3:
    `apply=True` must plan INSIDE the lock; a dry run may plan without it)."""
    per_subtree: dict[str, dict[str, int]] = {}
    all_plan: list[_PlanItem] = []
    conflict_paths: list[str] = []
    differs: list[str] = []

    runs_plan, runs_counts, unplaceable, run_differs = _plan_runs(source, dest)
    differs.extend(run_differs)
    per_subtree["runs"] = {**runs_counts, "already_present_differs": len(run_differs)}
    for src_f, dest_f in runs_plan:
        all_plan.append(_PlanItem(src_f, dest_f, "runs", run_id=_run_id_from_filename(src_f.name)))

    for subtree in _CONSOLIDATE_PATH_MATCHED_SUBTREES:
        plan, counts, c_paths = _plan_path_matched(source, dest, subtree)
        per_subtree[subtree] = counts
        conflict_paths.extend(c_paths)
        for src_f, dest_f in plan:
            all_plan.append(_PlanItem(src_f, dest_f, subtree, rel=str(src_f.relative_to(source))))

    skipped_unknown = _find_skipped_unknown(source)

    return _ConsolidationPlan(
        per_subtree=per_subtree,
        all_plan=all_plan,
        conflict_paths=conflict_paths,
        unplaceable=unplaceable,
        differs=differs,
        skipped_unknown=skipped_unknown,
    )


def _reclassify_late_clash(item: _PlanItem, plan: _ConsolidationPlan) -> None:
    """`item.dest` appeared between planning and copy (fix 3, TOCTOU) --
    never clobber it. Reclassify the item as already-present (identical
    bytes) or a genuine late conflict/differs entry, mirroring the same
    classification the planner would have produced had it seen `dest` in
    its current state."""
    counts = plan.per_subtree[item.subtree]
    counts["copied"] = max(0, counts.get("copied", 0) - 1)
    identical = _files_identical(item.src, item.dest)
    if item.subtree == "runs":
        if identical:
            counts["already_present"] = counts.get("already_present", 0) + 1
        else:
            plan.differs.append(item.run_id)
            counts["already_present_differs"] = counts.get("already_present_differs", 0) + 1
    elif identical:
        counts["already_present"] = counts.get("already_present", 0) + 1
    else:
        counts["conflicts"] = counts.get("conflicts", 0) + 1
        plan.conflict_paths.append(item.rel)


def _apply_consolidation_plan(plan: _ConsolidationPlan) -> None:
    """Execute `plan.all_plan`: atomic copy (same-directory temp file, then
    a no-clobber link-then-unlink -- fix 3) into `dest`. Never overwrites a
    `dest` file that appeared since planning; such a file is reclassified
    instead via `_reclassify_late_clash`."""
    for item in plan.all_plan:
        item.dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = item.dest.parent / f".{item.dest.name}.{uuid.uuid4().hex[:8]}.tmp"
        shutil.copy2(item.src, tmp)
        linked = False
        try:
            os.link(tmp, item.dest)
            linked = True
        except FileExistsError:
            linked = False
        except OSError:
            # Not a same-path clash (e.g. cross-device) -- fall back to an
            # exists-check immediately before the rename; still no-clobber.
            if not item.dest.exists():
                os.replace(tmp, item.dest)
                linked = True
        finally:
            if tmp.exists():
                with contextlib.suppress(OSError):
                    tmp.unlink()

        if not linked:
            _reclassify_late_clash(item, plan)


def consolidate_project_catalog(source: Path, dest: Path, *, apply: bool) -> ConsolidateResult:
    """Additively copy `source`'s `runs/`, `campaigns/`, `submits/`,
    `reaped/`, `sidecars/`, `anchors/`, `ledger/`, `blast_radius/`,
    `archived_items/` subtrees into `dest` (spec Migration step 0 amendment,
    debt #1998: consolidating a registered root's obsolete per-project
    `catalog_dir` before `bth migrate --to-log` will accept it).

    Never touches `bathos.db*`, `index.db`, `cutover.json`, `writers.lock`,
    `logs/`, `remote-runs/`, or anything outside the allowlisted subtrees
    (anything else at `source`'s top level is reported in
    `skipped_unknown`, never silently ignored -- fix 2), and NEVER modifies
    or deletes anything in `source` -- every write lands only under `dest`,
    atomically (copy to a same-directory temp file, then a no-clobber
    link-then-unlink, falling back to an exists-checked `os.replace`).

    `apply=False` (dry run) computes the plan and counts without writing
    anything or taking any lock. `apply=True` computes the SAME plan INSIDE
    `dest`'s writers lock, held exclusively for the duration of both the
    planning and the copy (fix 3: closes the TOCTOU window between an
    unlocked plan and a locked copy), then -- if at least one file was
    actually copied -- runs the normal (non-force) `bathos.compact.
    compact(dest)` outside the lock, so the warm tier sees the newly-visible
    runs. Even under the lock, each file is written with a per-file
    no-clobber check immediately before the rename, in case something wrote
    directly to `dest` outside this module's own locking discipline; a file
    that appeared there is never overwritten -- it is reclassified as
    already-present or a late conflict/differs entry instead.

    `status` is `"ok"` only when the run is fully clean: `"needs_review"`
    covers the case where the additive copy still ran (on apply) but
    `conflicts > 0` or `unplaceable` is non-empty and needs a human look
    before `bth migrate --to-log` is retried (fix 1) -- a clean result is
    never conflated with one that needs review.

    Refuses (`status="refused_log_mode"`) if `dest` already has a cut-over
    marker: consolidation is a pre-cut-over step; after cut-over, a stale
    legacy write is handled by `bth migrate --import-legacy` instead, which
    imports into the OWNING project's own log rather than folding raw
    Parquet into a shared catalog. Refuses (`status="refused_source_log_mode"`,
    fix 8) symmetrically if `source` itself already has a cut-over marker --
    it is no longer a legacy per-project catalog to fold in, and a stale
    write discovered there after its own cut-over is `--import-legacy`'s
    job, not consolidation's.
    """
    if cutover_marker_path(dest).exists():
        return ConsolidateResult(
            status="refused_log_mode",
            detail=(
                f"{dest} is already in log mode (cutover marker present); consolidation "
                "is a pre-cut-over step -- after cut-over, use `bth migrate "
                "--import-legacy` instead"
            ),
        )
    if not source.is_dir():
        return ConsolidateResult(
            status="source_missing", detail=f"source catalog {source} does not exist"
        )
    if source.resolve() == dest.resolve():
        return ConsolidateResult(
            status="same_catalog", detail=f"source and dest are the same catalog ({dest})"
        )
    if cutover_marker_path(source).exists():
        return ConsolidateResult(
            status="refused_source_log_mode",
            detail=(
                f"{source} already has a cutover marker (it is itself in log mode); it is "
                "no longer a legacy per-project catalog to fold in -- a stale write "
                "discovered there after its own cut-over is `bth migrate --import-legacy`'s "
                "job, not consolidation's"
            ),
        )

    if apply:
        with writers_lock(dest, exclusive=True):
            plan = _build_consolidation_plan(source, dest)
            _apply_consolidation_plan(plan)
    else:
        plan = _build_consolidation_plan(source, dest)

    total_copied = sum(c.get("copied", 0) for c in plan.per_subtree.values())
    total_already_present = sum(c.get("already_present", 0) for c in plan.per_subtree.values())
    total_conflicts = sum(c.get("conflicts", 0) for c in plan.per_subtree.values())

    if apply and total_copied > 0:
        from bathos.compact import compact

        compact(dest)

    status = "needs_review" if (total_conflicts > 0 or plan.unplaceable) else "ok"

    return ConsolidateResult(
        status=status,
        applied=apply,
        copied=total_copied,
        already_present=total_already_present,
        already_present_differs=plan.differs[:_CONSOLIDATE_CONFLICT_PATH_CAP],
        already_present_differs_count=len(plan.differs),
        conflicts=total_conflicts,
        conflict_paths=plan.conflict_paths[:_CONSOLIDATE_CONFLICT_PATH_CAP],
        unplaceable=plan.unplaceable,
        skipped_unknown=plan.skipped_unknown,
        per_subtree=plan.per_subtree,
        detail=(
            f"{'applied' if apply else 'dry run'}: copied={total_copied} "
            f"already_present={total_already_present} "
            f"already_present_differs={len(plan.differs)} "
            f"conflicts={total_conflicts}"
            + (f" unplaceable={len(plan.unplaceable)}" if plan.unplaceable else "")
            + (f" skipped_unknown={len(plan.skipped_unknown)}" if plan.skipped_unknown else "")
            + (f" status={status}" if status != "ok" else "")
        ),
    )


__all__ = [
    "ConsolidateResult",
    "DiffResult",
    "MigrateToLogError",
    "MigrateToLogResult",
    "ResidualLine",
    "committed_project_id",
    "consolidate_project_catalog",
    "explicit_catalog_dir",
    "import_legacy_post_cutover",
    "migrate_to_log",
    "my_squeue_job_ids",
    "mirror_remote_runs_full",
    "pull_and_mirror_all_remotes",
    "roots_missing_project_id",
    "roots_with_foreign_catalog",
    "rsync_full_mirror",
    "squeue_conflict",
]
