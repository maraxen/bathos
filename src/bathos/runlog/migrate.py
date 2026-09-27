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
    `missing_project_ids`, `squeue_unavailable`, `squeue_conflict`,
    `step1_reap_failed`, `legacy_db_locked`, `unclassified_residual`,
    `residual_pending`, `switched`, `already_migrated`."""

    status: str
    attempt: str | None = None
    report_path: str | None = None
    report_sha256: str | None = None
    residual_lines: list[dict[str, Any]] = field(default_factory=list)
    unclassified: list[dict[str, Any]] = field(default_factory=list)
    missing_project_ids: list[str] = field(default_factory=list)
    conflicting_jobs: list[str] = field(default_factory=list)
    locked_source: str | None = None
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
    boundary) rather than every caller."""
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
) -> dict[Path | None, list]:
    """Bucket every candidate by its resolved OWNING root (`None` ==
    unaffiliated). With 0 or 1 registered roots the routing is unambiguous
    (everything goes there, or to unaffiliated with none registered).

    With 2+ registered roots (spec-ambiguity call, documented in the module
    docstring and the task report): only candidates whose OWN legacy fields
    carry a `project_slug` matching a registered root's slug route there;
    everything else (edges, `campaign_run.imported`, anchors, the three
    simple ledgers -- none of which carry `project_slug` on their own
    fields) falls to `unaffiliated/`. This is a known, deliberate scope
    limitation for the multi-project catalog case; the common single-
    project catalog (this wave's fixtures) is unaffected.
    """
    if not roots:
        return {None: list(candidates)}
    if len(roots) == 1:
        root, _cfg = roots[0]
        return {root: list(candidates)}

    slug_to_root = {cfg.slug: root for root, cfg in roots if cfg is not None}
    buckets: dict[Path | None, list] = {}
    for cand in candidates:
        slug = cand.fields.get("project_slug") if isinstance(cand.fields, dict) else None
        root = slug_to_root.get(slug) if slug else None
        buckets.setdefault(root, []).append(cand)
    return buckets


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
) -> ImportReport:
    """The shared body of Migration step 2 and `--import-legacy`: scan every
    legacy source once (`_all_candidates`, AC-18's `connect_legacy`), route
    each candidate to its owning destination, and append. `staging_root`
    picks staging (step 2) vs. direct project logs (`--import-legacy`).
    """
    report = ImportReport()
    candidates = _all_candidates(catalog_dir, report)
    roots = _registered_roots_with_config()
    buckets = _route_candidates(candidates, roots)
    for root, bucket in buckets.items():
        dest = _dest_for_root(root, staging_root=staging_root)
        slug, pid = _root_project_info(root)
        main_root = root if root is not None else catalog_dir
        n = _append_import_events(bucket, dest, project=slug, project_id=pid, main_root=main_root)
        report.appended += n
    return report


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


def _classify(
    table: str,
    key: tuple,
    column: str,
    lrow: dict[str, Any] | None,
    srow: dict[str, Any] | None,
    *,
    step1_touched_run_ids: set[str],
    cool_run_rows: dict[str, dict[str, Any]],
) -> str | None:
    """Assign one allow-listed class (spec Migration step 3) to a residual
    row, or `None` (unclassified -- aborts the migration).

    Implements four classes:

    - `step1_pulled_or_reaped` (AC-30): the run entity was touched by THIS
      invocation's own step-1 pull/reap.
    - `warm_only_row` (AC-30): the entity has a warm row but no legacy
      source at all (nothing staged for it).
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

    Every other allow-listed class in the spec's prose (force-rebuild loss,
    corrupt-fragment skips, output_metadata drift, postmortem/worktree loss,
    campaign e-value confounds, sidecar-edited e-values, unresolvable
    project) is a deliberately deferred scope limitation of this wave -- a
    residual of one of those kinds is reported as unclassified (aborting the
    migration) rather than silently accepted, which is the safe direction
    (see the task report for the full list).
    """
    run_id = key[0] if table == "runs" and key else None
    if run_id and run_id in step1_touched_run_ids:
        return "step1_pulled_or_reaped"
    if srow is None and lrow is not None:
        return "warm_only_row"
    if table == "runs" and run_id and run_id in cool_run_rows:
        cool_val = _jsonify_value(cool_run_rows[run_id].get(column))
        staged_val = srow.get(column) if srow is not None else None
        if _values_equal(cool_val, staged_val):
            return "fragment_not_yet_compacted"
    if table == "runs" and column == "metadata":
        return "frozen_at_earlier_compact"
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


def _build_and_diff(
    catalog_dir: Path, attempt: str, *, step1_touched_run_ids: set[str]
) -> DiffResult:
    legacy_tables, legacy_columns, legacy_table_exists, locked_status = _read_legacy_tables(
        catalog_dir
    )
    if locked_status is not None:
        return DiffResult(lines=[], unclassified=[], locked_status=locked_status, sha256="")

    staging_root = _attempt_staging_root(attempt)
    staging_root.mkdir(parents=True, exist_ok=True)
    idx_path = staging_root / "index.db"
    idx_path.unlink(missing_ok=True)
    roots = staging_roots_for_attempt(attempt)
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
# Top-level entry points
# --------------------------------------------------------------------------


def migrate_to_log(
    catalog_dir: Path | None = None,
    *,
    force: bool = False,
    accept_residual: str | None = None,
) -> MigrateToLogResult:
    """`bth migrate --to-log` (Migration steps 0-4)."""
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

        import_report = _import_candidates(cd, staging_root=_attempt_staging_root(attempt))
        if import_report.locked:
            return MigrateToLogResult(
                status="legacy_db_locked",
                attempt=attempt,
                locked_source=import_report.locked[0],
                detail="a legacy source is locked by another process; retry once it is free",
            )

        diff = _build_and_diff(cd, attempt, step1_touched_run_ids=step1_touched)
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
        report = _import_candidates(cd, staging_root=None)
    if report.locked:
        return MigrateToLogResult(
            status="legacy_db_locked",
            locked_source=report.locked[0],
            detail="a legacy source is locked by another process; retry once it is free",
        )
    return MigrateToLogResult(
        status="imported",
        detail=f"appended={report.appended} unchanged={report.unchanged}",
    )


__all__ = [
    "DiffResult",
    "MigrateToLogError",
    "MigrateToLogResult",
    "ResidualLine",
    "committed_project_id",
    "import_legacy_post_cutover",
    "migrate_to_log",
    "my_squeue_job_ids",
    "mirror_remote_runs_full",
    "pull_and_mirror_all_remotes",
    "roots_missing_project_id",
    "rsync_full_mirror",
    "squeue_conflict",
]
