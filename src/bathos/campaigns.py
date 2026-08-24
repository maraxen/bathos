from __future__ import annotations

import importlib.metadata
import json
import logging
from collections import defaultdict
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import duckdb

from bathos.sidecar import compute_evalue
from bathos.telemetry import event


class CampaignError(Exception):
    pass


@dataclass
class Campaign:
    id: str
    project_slug: str
    name: str
    mode: str  # "exploration" | "confirmation" | "sequential"
    question: str | None = None
    hypothesis: str | None = None
    status: str = "open"
    started_at: str = ""
    concluded_at: str | None = None
    conclusion: str | None = None
    outcome_label: str | None = None
    parent_campaign_id: str | None = None
    stopping_threshold: float | None = None
    negative_check: str | None = None
    claim_path: str | None = None
    claim_sha256: str | None = None
    claim_mode: str | None = None


def _open_db(catalog_dir) -> duckdb.DuckDBPyConnection:
    return duckdb.connect(str(Path(catalog_dir) / "bathos.db"))


def write_campaign_cool(campaign: Campaign, catalog_dir: Path) -> Path:
    """Atomic write-then-rename of `{catalog}/campaigns/{id}.json`."""
    dest_dir = Path(catalog_dir) / "campaigns"
    dest_dir.mkdir(parents=True, exist_ok=True)
    target = dest_dir / f"{campaign.id}.json"
    tmp = dest_dir / f"{campaign.id}.json.tmp"
    tmp.write_text(json.dumps(asdict(campaign), indent=2, sort_keys=True) + "\n")
    tmp.replace(target)
    return target


def _campaign_from_payload(payload: dict) -> Campaign:
    return Campaign(
        id=payload["id"],
        project_slug=payload.get("project_slug", ""),
        name=payload.get("name", ""),
        mode=payload.get("mode", "exploration"),
        question=payload.get("question"),
        hypothesis=payload.get("hypothesis"),
        status=payload.get("status", "open"),
        started_at=payload.get("started_at", ""),
        concluded_at=payload.get("concluded_at"),
        conclusion=payload.get("conclusion"),
        outcome_label=payload.get("outcome_label"),
        parent_campaign_id=payload.get("parent_campaign_id"),
        stopping_threshold=payload.get("stopping_threshold"),
        negative_check=payload.get("negative_check"),
        claim_path=payload.get("claim_path"),
        claim_sha256=payload.get("claim_sha256"),
        claim_mode=payload.get("claim_mode"),
    )


def read_cool_campaigns(catalog_dir: Path) -> list[Campaign]:
    logger = logging.getLogger(__name__)
    dest_dir = Path(catalog_dir) / "campaigns"
    if not dest_dir.exists():
        return []
    campaigns: list[Campaign] = []
    seen: set[str] = set()
    for path in dest_dir.glob("*.json"):
        try:
            payload = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as e:
            logger.warning("Skipping unreadable campaign JSON %s: %s", path, e)
            event("catalog.campaign_json_unreadable", path=str(path))
            continue
        if not isinstance(payload, dict) or "id" not in payload:
            logger.warning("Skipping campaign JSON missing id: %s", path)
            continue
        if path.stem != payload["id"]:
            logger.warning(
                "Skipping campaign JSON whose filename %s does not match id %s",
                path.name,
                payload["id"],
            )
            continue
        if payload["id"] in seen:
            continue
        seen.add(payload["id"])
        campaigns.append(_campaign_from_payload(payload))
    return campaigns


def ingest_cool_campaigns(db, catalog_dir: Path) -> int:
    """Upsert cool-tier campaign JSON into the warm `campaigns` table."""
    n = 0
    for campaign in read_cool_campaigns(catalog_dir):
        db.execute(
            """
            INSERT INTO campaigns (
                id, project_slug, name, mode, question, hypothesis, status,
                started_at, concluded_at, conclusion, outcome_label,
                parent_campaign_id, stopping_threshold, negative_check,
                claim_path, claim_sha256, claim_mode
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                project_slug = excluded.project_slug,
                name = COALESCE(NULLIF(excluded.name, ''), campaigns.name),
                mode = campaigns.mode,
                question = COALESCE(NULLIF(excluded.question, ''), campaigns.question),
                hypothesis = COALESCE(NULLIF(excluded.hypothesis, ''), campaigns.hypothesis),
                status = CASE WHEN campaigns.status = 'concluded' THEN campaigns.status ELSE excluded.status END,
                started_at = COALESCE(NULLIF(excluded.started_at, ''), campaigns.started_at),
                concluded_at = CASE WHEN campaigns.status = 'concluded' THEN campaigns.concluded_at ELSE excluded.concluded_at END,
                conclusion = CASE WHEN campaigns.status = 'concluded' THEN campaigns.conclusion ELSE excluded.conclusion END,
                outcome_label = CASE WHEN campaigns.status = 'concluded' THEN campaigns.outcome_label ELSE excluded.outcome_label END,
                parent_campaign_id = excluded.parent_campaign_id,
                stopping_threshold = COALESCE(campaigns.stopping_threshold, excluded.stopping_threshold),
                negative_check = COALESCE(NULLIF(campaigns.negative_check, ''), NULLIF(excluded.negative_check, '')),
                claim_path = COALESCE(NULLIF(campaigns.claim_path, ''), NULLIF(excluded.claim_path, '')),
                claim_sha256 = COALESCE(NULLIF(campaigns.claim_sha256, ''), NULLIF(excluded.claim_sha256, '')),
                claim_mode = COALESCE(NULLIF(campaigns.claim_mode, ''), NULLIF(excluded.claim_mode, ''))
            """,
            [
                campaign.id,
                campaign.project_slug,
                campaign.name,
                campaign.mode,
                campaign.question,
                campaign.hypothesis,
                campaign.status,
                campaign.started_at,
                campaign.concluded_at,
                campaign.conclusion,
                campaign.outcome_label,
                campaign.parent_campaign_id,
                campaign.stopping_threshold,
                campaign.negative_check,
                campaign.claim_path,
                campaign.claim_sha256,
                campaign.claim_mode,
            ],
        )
        n += 1
    return n


def connect_catalog_db(catalog_dir: Path, *, read_only: bool = True):
    """Open bathos.db if it exists; otherwise return None."""
    path = Path(catalog_dir) / "bathos.db"
    if not path.exists():
        return None
    return duckdb.connect(str(path), read_only=read_only)


def union_campaign_member_ids(db, campaign_id: str, catalog_dir: Path | None) -> list[str]:
    """Warm campaign_runs union cool parquet rows stamped with campaign_id."""
    ids: set[str] = set()
    if db is not None:
        try:
            for (rid,) in db.execute(
                "SELECT run_id FROM campaign_runs WHERE campaign_id = ?", [campaign_id]
            ).fetchall():
                if rid:
                    ids.add(rid)
        except duckdb.Error:
            pass
    if catalog_dir is not None:
        from bathos.catalog import read_runs

        for run in read_runs(catalog_dir):
            if run.campaign_id == campaign_id:
                ids.add(run.id)
    return sorted(ids)


def prepare_catalog_for_conclude(catalog_dir: Path) -> None:
    """Ensure warm rows and campaign_runs membership exist for conclude and threshold checks."""
    from bathos.catalog import read_runs
    from bathos.compact import compact

    catalog_dir = Path(catalog_dir)
    db_path = catalog_dir / "bathos.db"
    cool = read_runs(catalog_dir)
    if not db_path.exists():
        compact(catalog_dir)
        return
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        warm_ids = {r[0] for r in con.execute("SELECT id FROM runs").fetchall()}
    except duckdb.Error:
        warm_ids = set()
    finally:
        con.close()
    if any(run.id not in warm_ids for run in cool):
        compact(catalog_dir)
        return
    con = duckdb.connect(str(db_path))
    try:
        ingest_cool_campaigns(con, catalog_dir)
        link_cool_runs_to_campaigns(con, cool, catalog_dir=catalog_dir)
    finally:
        con.close()


def create_campaign(
    db,
    name: str,
    project_slug: str,
    mode: str,
    question: str | None = None,
    hypothesis: str | None = None,
    parent_campaign_id: str | None = None,
    catalog_dir: Path | None = None,
) -> Campaign:
    if mode not in ("exploration", "confirmation", "sequential"):
        raise CampaignError(
            f"mode must be 'exploration', 'confirmation', or 'sequential', got {mode!r}"
        )
    campaign_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    db.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, question, hypothesis, status, started_at, parent_campaign_id) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)",
        [
            campaign_id,
            project_slug,
            name,
            mode,
            question,
            hypothesis,
            started_at,
            parent_campaign_id,
        ],
    )
    # Use campaign_name field since 'name' is reserved by logging.LogRecord
    event("campaign.create", campaign_id=campaign_id, campaign_name=name)
    campaign = Campaign(
        id=campaign_id,
        project_slug=project_slug,
        name=name,
        mode=mode,
        question=question,
        hypothesis=hypothesis,
        status="open",
        started_at=started_at,
        parent_campaign_id=parent_campaign_id,
    )
    if catalog_dir is not None:
        write_campaign_cool(campaign, catalog_dir)
    return campaign


def add_run_to_campaign(db, campaign_id: str, run_id: str, catalog_dir: Path | None = None) -> None:
    """Add run to campaign (idempotent). For sequential campaigns, computes e-value and applies threshold lock."""
    campaign_id = _resolve_campaign_id(db, campaign_id, catalog_dir=catalog_dir)
    if catalog_dir is not None:
        ingest_cool_campaigns(db, catalog_dir)
    campaign_rows = db.execute(
        "SELECT mode, started_at, stopping_threshold FROM campaigns WHERE id = ?", [campaign_id]
    ).fetchall()
    if not campaign_rows:
        raise CampaignError(f"Campaign not found: {campaign_id}")
    campaign_mode, campaign_started_at, campaign_threshold = campaign_rows[0]

    run_rows = db.execute(
        "SELECT timestamp, outcome, sidecar_path FROM runs WHERE id = ?", [run_id]
    ).fetchall()
    if not run_rows:
        raise CampaignError(f"Run not found: {run_id}")
    run_timestamp, run_outcome, run_sidecar_path = run_rows[0]

    # Enforce temporal ordering for confirmation campaigns
    if campaign_mode == "confirmation":
        try:
            campaign_dt = datetime.fromisoformat(campaign_started_at)
            if campaign_dt.tzinfo is None:
                campaign_dt = campaign_dt.replace(tzinfo=UTC)
        except (ValueError, TypeError):
            campaign_dt = None

        run_dt = run_timestamp if isinstance(run_timestamp, datetime) else None
        if run_dt is not None and run_dt.tzinfo is None:
            run_dt = run_dt.replace(tzinfo=UTC)

        if campaign_dt is not None and run_dt is not None and run_dt < campaign_dt:
            raise CampaignError(
                f"Cannot add run {run_id} to confirmation campaign {campaign_id}: "
                f"run timestamp ({run_dt.isoformat()}) predates campaign creation ({campaign_dt.isoformat()})"
            )

    if campaign_mode == "sequential":
        # Compute e-value from sidecar
        evalue = 1.0
        sidecar_stopping_threshold = None
        if run_sidecar_path:
            from pathlib import Path

            from bathos.sidecar import SidecarError, parse_sidecar

            try:
                sidecar_path_obj = Path(run_sidecar_path)
                if sidecar_path_obj.exists():
                    sidecar = parse_sidecar(sidecar_path_obj)
                    evalue = compute_evalue(sidecar, run_outcome or "unknown")
                    sidecar_stopping_threshold = sidecar.popper_stopping_threshold
            except SidecarError:
                evalue = 1.0

        # Assign seq_position (1-based, monotonically increasing per campaign)
        pos_row = db.execute(
            "SELECT COALESCE(MAX(seq_position), 0) + 1 FROM campaign_runs WHERE campaign_id = ?",
            [campaign_id],
        ).fetchone()
        seq_position = pos_row[0] if pos_row else 1

        # Threshold lock logic (only locks for non-error/non-unknown outcomes)
        is_neutral_outcome = run_outcome in ("error", "unknown", None, "")
        if not is_neutral_outcome:
            if campaign_threshold is None and sidecar_stopping_threshold is not None:
                # Lock threshold from this sidecar
                db.execute(
                    "UPDATE campaigns SET stopping_threshold = ? WHERE id = ?",
                    [sidecar_stopping_threshold, campaign_id],
                )
                campaign_threshold = sidecar_stopping_threshold
                if catalog_dir is not None:
                    refreshed = get_campaign(db, campaign_id, catalog_dir=catalog_dir)
                    if refreshed is not None:
                        write_campaign_cool(refreshed, catalog_dir)
            elif campaign_threshold is not None and sidecar_stopping_threshold is not None:
                if sidecar_stopping_threshold != campaign_threshold:
                    n_runs = db.execute(
                        "SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ? AND seq_position IS NOT NULL",
                        [campaign_id],
                    ).fetchone()[0]
                    raise CampaignError(
                        f"Cannot change stopping_threshold for campaign {campaign_id[:8]}: "
                        f"{n_runs} non-error run(s) already added (threshold locked at {campaign_threshold}). "
                        f"To use a different threshold, create a new campaign with "
                        f"--parent {campaign_id[:8]} to preserve lineage."
                    )

        db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id, evalue, seq_position) VALUES (?, ?, ?, ?) ON CONFLICT DO NOTHING",
            [campaign_id, run_id, evalue, seq_position],
        )
    else:
        db.execute(
            "INSERT INTO campaign_runs (campaign_id, run_id, evalue, seq_position) VALUES (?, ?, NULL, NULL) ON CONFLICT DO NOTHING",
            [campaign_id, run_id],
        )
    if catalog_dir is not None:
        from bathos.catalog import read_runs, write_run

        for run in read_runs(catalog_dir):
            if run.id == run_id:
                run.campaign_id = campaign_id
                write_run(run, catalog_dir)
                break


def link_cool_runs_to_campaigns(
    db, cool_runs, catalog_dir: Path | None = None, campaign_id: str | None = None
) -> None:
    """Insert campaign_runs from parquet campaign_id; fill sequential e-values.

    Does not enforce confirmation temporal ordering — cool fragments already stamped membership.
    Sequential rank is the union of parquet campaign_id stamps and warm campaign_runs.
    Catalog-wide compact skips a campaign on threshold mismatch instead of aborting.
    """
    from types import SimpleNamespace

    from bathos.sidecar import SidecarError, parse_sidecar

    members = [r for r in cool_runs if r.campaign_id]
    if campaign_id is not None:
        members = [r for r in members if r.campaign_id == campaign_id]
    for run in members:
        db.execute(
            """
            INSERT INTO campaign_runs (campaign_id, run_id)
            VALUES (?, ?)
            ON CONFLICT DO NOTHING
            """,
            [run.campaign_id, run.id],
        )

    def _run_sort_key(r):
        ts = r.timestamp
        if ts is None:
            ts = datetime.min.replace(tzinfo=UTC)
        elif getattr(ts, "tzinfo", None) is None:
            ts = ts.replace(tzinfo=UTC)
        return (ts, r.id)

    cids: set[str] = {r.campaign_id for r in members if r.campaign_id}
    try:
        for row in db.execute("SELECT DISTINCT campaign_id FROM campaign_runs").fetchall():
            if row[0]:
                cids.add(row[0])
    except duckdb.Error:
        pass
    if campaign_id is not None:
        cids = {campaign_id}

    parquet_by_cid: dict[str, list] = defaultdict(list)
    for run in members:
        parquet_by_cid[run.campaign_id].append(run)

    for cid in cids:
        mode_row = db.execute(
            "SELECT mode, stopping_threshold FROM campaigns WHERE id = ?", [cid]
        ).fetchone()
        if not mode_row or mode_row[0] != "sequential":
            continue
        by_id: dict[str, object] = {}
        for run in parquet_by_cid.get(cid, []):
            by_id[run.id] = run
        try:
            for run_id, ts, sidecar_path, outcome in db.execute(
                """
                SELECT cr.run_id, r.timestamp, r.sidecar_path, r.outcome
                FROM campaign_runs cr
                INNER JOIN runs r ON cr.run_id = r.id
                WHERE cr.campaign_id = ?
                """,
                [cid],
            ).fetchall():
                if run_id not in by_id:
                    by_id[run_id] = SimpleNamespace(
                        id=run_id,
                        timestamp=ts,
                        sidecar_path=sidecar_path,
                        outcome=outcome,
                        campaign_id=cid,
                    )
        except duckdb.Error:
            pass
        ordered = sorted(by_id.values(), key=_run_sort_key)
        campaign_threshold = mode_row[1]
        planned: list[tuple] = []
        mismatch: CampaignError | None = None
        pending_threshold = campaign_threshold
        for i, run in enumerate(ordered, start=1):
            sidecar_path_obj = Path(run.sidecar_path) if run.sidecar_path else None
            sidecar_stopping_threshold = None
            evalue = None
            if sidecar_path_obj is not None and sidecar_path_obj.exists():
                try:
                    sidecar = parse_sidecar(sidecar_path_obj)
                    evalue = compute_evalue(sidecar, run.outcome or "unknown")
                    sidecar_stopping_threshold = sidecar.popper_stopping_threshold
                except SidecarError:
                    evalue = None
            is_neutral_outcome = run.outcome in ("error", "unknown", None, "")
            if (
                not is_neutral_outcome
                and pending_threshold is not None
                and sidecar_stopping_threshold is not None
                and sidecar_stopping_threshold != pending_threshold
            ):
                n_runs = db.execute(
                    "SELECT COUNT(*) FROM campaign_runs WHERE campaign_id = ? AND seq_position IS NOT NULL",
                    [cid],
                ).fetchone()[0]
                mismatch = CampaignError(
                    f"Cannot change stopping_threshold for campaign {cid[:8]}: "
                    f"{n_runs} non-error run(s) already added (threshold locked at {pending_threshold}). "
                    f"To use a different threshold, create a new campaign with "
                    f"--parent {cid[:8]} to preserve lineage."
                )
                break
            lock_threshold = None
            if (
                not is_neutral_outcome
                and pending_threshold is None
                and sidecar_stopping_threshold is not None
            ):
                lock_threshold = sidecar_stopping_threshold
                pending_threshold = sidecar_stopping_threshold
            planned.append((evalue, i, run.id, lock_threshold))
        if mismatch is not None:
            if campaign_id is not None:
                raise mismatch
            event("catalog.sequential_threshold_mismatch", campaign_id=cid, error=str(mismatch))
            continue
        for evalue, i, run_id, lock_threshold in planned:
            if lock_threshold is not None:
                db.execute(
                    "UPDATE campaigns SET stopping_threshold = ? WHERE id = ?",
                    [lock_threshold, cid],
                )
                if catalog_dir is not None:
                    refreshed = get_campaign(db, cid, catalog_dir=catalog_dir)
                    if refreshed is not None:
                        write_campaign_cool(refreshed, catalog_dir)
            db.execute(
                """
                UPDATE campaign_runs SET evalue = COALESCE(?, evalue), seq_position = ?
                WHERE campaign_id = ? AND run_id = ?
                """,
                [evalue, i, cid, run_id],
            )


def _campaign_threshold_met(db, campaign_id: str, stopping_threshold: float) -> bool:
    """Return True if all scripts in the campaign have E_n >= stopping_threshold."""
    rows = db.execute(
        """
        SELECT EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown'))
        FROM campaign_runs cr
        INNER JOIN runs r ON cr.run_id = r.id
        WHERE cr.campaign_id = ?
        GROUP BY COALESCE(NULLIF(r.script_sha256, ''), r.sidecar_path, '_ungrouped')
    """,
        [campaign_id],
    ).fetchall()
    if not rows:
        return False
    return all((row[0] is not None and row[0] >= stopping_threshold) for row in rows)


def _resolve_campaign_id(db, campaign_id: str, catalog_dir: Path | None = None) -> str:
    """Resolve a full or short (prefix) campaign ID to a full UUID.

    Unions cool-tier JSON ids with warm `campaigns.id` before exact/prefix match
    so a partial sync cannot uniquely resolve a prefix that is ambiguous in DuckDB.
    """
    ids: list[str] = []
    if catalog_dir is not None:
        ids.extend(c.id for c in read_cool_campaigns(catalog_dir))
    if db is not None:
        with suppress(duckdb.Error):
            ids.extend(r[0] for r in db.execute("SELECT id FROM campaigns").fetchall())
    unique = list(dict.fromkeys(ids))
    exact = [i for i in unique if i == campaign_id]
    if exact:
        return exact[0]
    prefix = [i for i in unique if i.startswith(campaign_id)]
    if len(prefix) == 1:
        return prefix[0]
    if len(prefix) > 1:
        matches = ", ".join(i[:8] for i in prefix)
        raise CampaignError(f"Ambiguous campaign ID prefix {campaign_id!r} matches: {matches}")
    raise CampaignError(f"Campaign not found: {campaign_id}")


def count_seeds_for_script(db, script_sha256: str) -> int:
    """Count distinct non-null seeds recorded across runs of a given script_sha256.

    B2-02 (AC-16): the data the stats-battery gate (B2-01) needs to enforce ">=3-seed
    ICC replication" at conclude. This function only counts; the hard-block enforcement
    itself lives in B2-01's stats_gates.py, not here.
    """
    rows = db.execute(
        "SELECT COUNT(DISTINCT seed) FROM runs WHERE script_sha256 = ? AND seed IS NOT NULL",
        [script_sha256],
    ).fetchall()
    return rows[0][0] if rows else 0


def count_runs_for_script(db, script_sha256: str) -> int:
    """Count total runs recorded for a given script_sha256.

    B2-02 (AC-16): the data the stats-battery gate (B2-01) needs to enforce the
    "N>=29-per-script_sha256" power floor at conclude.
    """
    rows = db.execute(
        "SELECT COUNT(*) FROM runs WHERE script_sha256 = ?",
        [script_sha256],
    ).fetchall()
    return rows[0][0] if rows else 0


@dataclass(frozen=True)
class ConcludedRunInfo:
    """One campaign member run's identity + artifact pointers, as known to bathos
    at conclude time -- enough for a hook to locate the run's output without
    re-querying the catalog itself."""

    run_id: str
    output_path: str | None
    sidecar_path: str | None
    content_hash: str | None


@dataclass(frozen=True)
class CampaignConcludeEvent:
    """Payload passed to every ``bathos.campaign_conclude_hooks`` entry point.

    See the "Post-conclude hooks" section of conclude_campaign()'s docstring for
    the full contract (call signature, ordering/timing, non-propagation guarantee).
    """

    campaign_id: str
    outcome_label: str
    members: tuple[ConcludedRunInfo, ...]


def _output_metadata_content_hash(metadata_json: str | None, output_path: str | None) -> str | None:
    """The sha256 recorded for ``output_path`` in a run's output_metadata JSON array.

    output_metadata entries are produced 1:1 with output_paths, keyed by "path"
    (see bathos.compact._collect_output_metadata / the compaction write path) --
    NOT necessarily all hash-bearing, since a >100MB or unreadable output is
    recorded with sha256 omitted. Matching must be by path, not by "first entry
    with a truthy hash": for a multi-output run where output_paths[0] itself has
    no recorded hash but a later output does, returning that later hash would
    silently pair ConcludedRunInfo.output_path with the wrong file's content_hash.
    """
    if not metadata_json or not output_path:
        return None
    try:
        entries = json.loads(metadata_json)
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        if isinstance(entry, dict) and entry.get("path") == output_path and entry.get("sha256"):
            return entry["sha256"]
    return None


def _resolve_member_run_infos(db, run_ids: list[str]) -> tuple[ConcludedRunInfo, ...]:
    """Best-effort, single-query lookup of every member run's output/sidecar path
    and content hash.

    Reuses the already-open warm-tier connection rather than re-deriving from the
    catalog, and batches all member runs into one IN-list query rather than one
    query per run -- conclude_campaign() must not scale linearly with campaign
    membership just to build the hook payload. Any lookup failure (missing table,
    missing row, missing/malformed output_metadata) degrades to None fields rather
    than raising -- this feeds an advisory hook payload, not a gate, so it must
    never be able to break conclude_campaign().
    """
    if not run_ids:
        return ()

    rows_by_id: dict[str, tuple] = {}
    try:
        placeholders = ", ".join("?" for _ in run_ids)
        for row in db.execute(
            f"SELECT id, output_paths, sidecar_path, output_metadata FROM runs "
            f"WHERE id IN ({placeholders})",
            run_ids,
        ).fetchall():
            rows_by_id[row[0]] = row[1:]
    except duckdb.Error:
        rows_by_id = {}

    infos = []
    for run_id in run_ids:
        output_path: str | None = None
        sidecar_path: str | None = None
        content_hash: str | None = None
        row = rows_by_id.get(run_id)
        if row:
            paths, sidecar, metadata_json = row
            if paths:
                output_path = paths[0]
            if sidecar:
                sidecar_path = sidecar
            content_hash = _output_metadata_content_hash(metadata_json, output_path)
        infos.append(
            ConcludedRunInfo(
                run_id=run_id,
                output_path=output_path,
                sidecar_path=sidecar_path,
                content_hash=content_hash,
            )
        )
    return tuple(infos)


def _run_campaign_conclude_hooks(
    db,
    campaign_id: str,
    outcome_label: str,
    catalog_dir: Path | None,
    member_ids: list[str] | None = None,
) -> None:
    """Discover and invoke ``bathos.campaign_conclude_hooks`` entry points.

    Must be called only after the campaign is durably marked concluded. Hook
    failures are caught and printed as warnings -- they can never propagate out
    of conclude_campaign(), and can never affect its return value or the
    campaign's concluded state. See conclude_campaign()'s docstring for the
    full contract.

    ``member_ids``, if given, is reused as-is instead of re-querying the catalog
    -- the caller passes this when it already resolved membership earlier in the
    same conclude_campaign() call (e.g. for the Union Gate), so the same warm
    ``campaign_runs``-union-cool-parquet scan doesn't run twice.

    Connection handling: ``db`` (the writable connection conclude_campaign() was
    given) is closed here, after member-info resolution but before any hook is
    invoked. DuckDB refuses a second (even read-only) connection to the same
    catalog file while a writable one is open in the same process -- and a hook
    is expected to open its own connection to resolve/verify things against the
    catalog (this is exactly what affigit-wire's phase1_preflight gate does).
    Closing proactively, rather than leaving that conflict for the first hook to
    discover, is safe here specifically because this is conclude_campaign()'s
    final action: nothing in conclude_campaign() or its callers (bathos's own CLI
    and MCP tool surfaces) touches ``db`` again after this call returns -- both
    callers' own ``finally: db.close()`` on an already-closed connection is a
    documented DuckDB no-op, not an error.
    """
    try:
        hooks = importlib.metadata.entry_points(group="bathos.campaign_conclude_hooks")
    except Exception as e:
        print(f"WARNING: could not discover campaign_conclude_hooks: {e}")
        return

    if not hooks:
        return

    try:
        if member_ids is None:
            member_ids = union_campaign_member_ids(db, campaign_id, catalog_dir)
        members = _resolve_member_run_infos(db, member_ids)
    except Exception as e:
        print(f"WARNING: could not resolve campaign_conclude_hooks member run info: {e}")
        return
    payload = CampaignConcludeEvent(
        campaign_id=campaign_id, outcome_label=outcome_label, members=members
    )

    try:
        db.close()
    except Exception as e:
        print(f"WARNING: could not close catalog connection before campaign_conclude_hooks: {e}")

    for hook_ep in hooks:
        try:
            hook = hook_ep.load()
            hook(payload)
        except Exception as e:
            print(f"WARNING: campaign_conclude_hook '{hook_ep.name}' failed: {e}")


def conclude_campaign(
    db,
    campaign_id: str,
    outcome_label: str,
    conclusion: str,
    workspace_root=None,
    force_verdict: bool = False,
    negative_check: str | None = None,
    negative_outcome_pattern=None,
    catalog_dir: Path | None = None,
) -> None:
    """Mark campaign as concluded.

    If campaign has a registered claim file, runs Union Gate to validate discriminability.
    Union Gate behavior depends on campaign mode:
    - exploration: checks run, prints warning if uncovered
    - confirmation/sequential: downgrades verdict to 'confounded' if uncovered (unless force_verdict)
    - claim_path IS NULL: skips Union Gate entirely (opt-in model)

    BP-3: if a claim is registered AND outcome_label matches the negative-outcome vocabulary
    (see bathos.claim.is_negative_outcome), negative_check must be non-blank or this raises.
    Like Union Gate, this is opt-in on claim registration (claim_path IS NULL skips it too) --
    a campaign with no claim-tier adoption is unaffected.

    Args:
        db: DuckDB connection
        campaign_id: Campaign ID (prefix or full UUID)
        outcome_label: Verdict to record
        conclusion: Summary text
        workspace_root: Path to workspace (defaults to resolve_workspace().fs_root)
        force_verdict: If True, bypass Union Gate confounded downgrade (records claim_mode='bypassed')
        negative_check: Falsification backing / hedge for a negative outcome_label (BP-3)
        negative_outcome_pattern: Optional compiled regex override for the negative-outcome vocabulary

    Raises:
        CampaignError: If a claim is registered, outcome_label is a negative claim, and
            negative_check is blank.

    Post-conclude hooks:
        After the campaign is durably marked concluded (DB committed, cool-tier
        JSON re-synced, telemetry emitted), this function discovers and invokes
        every entry point registered under the ``bathos.campaign_conclude_hooks``
        group (via ``importlib.metadata.entry_points``). This lets external
        packages react to a concluded campaign -- e.g. attempting a real
        promotion -- without bathos taking a hard dependency on them.

        Each entry point must resolve to a callable of signature::

            def hook(event: bathos.campaigns.CampaignConcludeEvent) -> None: ...

        ``CampaignConcludeEvent`` carries ``campaign_id``, the final
        ``outcome_label`` (post any Union-Gate/parity-confound/review-coverage/
        obligation-gate downgrade), and ``members``: a tuple of
        ``ConcludedRunInfo(run_id, output_path, sidecar_path, content_hash)`` for
        every run in the campaign's union membership (warm ``campaign_runs`` union
        cool-tier parquet rows, per union_campaign_member_ids()). Any of
        ``output_path``/``sidecar_path``/``content_hash`` may be None when bathos
        cannot resolve it (e.g. the run predates compaction, or has no recorded
        output SHA).

        Ordering/timing guarantees: hooks run once per conclude_campaign() call,
        strictly after the campaign's concluded state is committed -- a hook can
        rely on immediately re-querying bathos and seeing the campaign as
        concluded. Hooks run synchronously, in the order importlib.metadata
        returns them (unspecified across ties), and conclude_campaign() blocks
        until every hook has returned or raised.

        Connection handling: the ``db`` connection passed into conclude_campaign()
        is closed before any hook is invoked (DuckDB does not allow a second
        connection, even read-only, to the same catalog file while a writable one
        is open in-process) -- a hook that needs to query the catalog must open
        its own connection via ``connect_catalog_db``/``duckdb.connect``. This is
        safe because hook dispatch is conclude_campaign()'s final action.

        Non-propagation guarantee: a hook that raises, that fails to load, or
        whose entry-point group itself cannot be resolved, NEVER raises out of
        conclude_campaign() and NEVER changes the campaign's concluded status or
        this function's (None) return value. Each such failure is caught and
        printed as a ``WARNING: campaign_conclude_hook '<name>' failed: ...``
        line, matching this function's existing print()-based warning pattern.
        A downstream consumer's bug must never break bathos's own conclusion
        contract. See ``.praxia/docs/decisions/`` for the design rationale.
    """
    from pathlib import Path

    from bathos.claim import (
        check_sha,
        is_negative_outcome,
        parse_claim,
        resolve_claim_path,
        run_union_gate,
    )
    from bathos.workspace import resolve_workspace

    full_id = _resolve_campaign_id(db, campaign_id, catalog_dir=catalog_dir)
    # Reused by _run_campaign_conclude_hooks() at the end of this function, if the
    # Union Gate below already resolved membership -- avoids a second identical
    # catalog scan (warm campaign_runs union cool-tier parquet) for the same call.
    precomputed_member_ids: list[str] | None = None

    if catalog_dir is not None:
        from bathos.catalog import read_runs

        ingest_cool_campaigns(db, catalog_dir)
        link_cool_runs_to_campaigns(
            db, read_runs(catalog_dir), catalog_dir=catalog_dir, campaign_id=full_id
        )

    # Check if campaign has a registered claim
    row = db.execute(
        "SELECT claim_path, claim_sha256, mode FROM campaigns WHERE id=?", [full_id]
    ).fetchone()

    claim_path_rel = None
    registered_sha = None
    campaign_mode = None

    if row:
        claim_path_rel, registered_sha, campaign_mode = row[0], row[1], row[2]
        if not claim_path_rel:
            claim_path_rel = None
        if not registered_sha:
            registered_sha = None

    # AC-08: Union Gate short-circuits if claim_path IS NULL (opt-in adoption ladder)
    if claim_path_rel and registered_sha:
        # Resolve workspace root if not provided
        if workspace_root is None:
            workspace_root = resolve_workspace(Path.cwd()).fs_root

        abs_path = resolve_claim_path(claim_path_rel, workspace_root)

        # AC-08: File-not-found is always an error, never a silent bypass
        if not abs_path.exists():
            raise RuntimeError(
                f"claim.bth.toml not found at {abs_path} — file may have been moved or deleted. "
                "Set BTH_WORKSPACE_ROOT or pass workspace_root to locate it."
            )

        # AC-11: SHA integrity check at conclude
        check_sha(claim_path_rel, registered_sha, workspace_root)

        # Parse the claim
        claim = parse_claim(abs_path)

        members = union_campaign_member_ids(db, full_id, catalog_dir)
        if not members:
            raise CampaignError(
                f"Cannot conclude campaign {full_id[:8]}: empty membership with a registered claim"
            )
        precomputed_member_ids = members

        # BP-3: negative-claim backing check (opt-in via claim registration, same as Union Gate)
        if (
            is_negative_outcome(outcome_label, negative_outcome_pattern)
            and not (negative_check or "").strip()
        ):
            raise CampaignError(
                f"outcome '{outcome_label}' is a negative claim — provide --negative-check with "
                "the falsification backing or hedge for this conclusion, or use a less definitive "
                "outcome label"
            )

        # F2 PARITY CONFOUND CHECK (before Union Gate)
        # Check for uncontrolled reference_parity confounds and downgrade if needed
        from bathos.claim import parity_confound_check

        parity_result = parity_confound_check(abs_path, db)
        parity_confounds = parity_result.get("confounds", [])

        # Downgrade verdict to 'confounded' if any parity confound is uncontrolled
        # (except for exploration mode, which only warns)
        parity_uncontrolled = [c for c in parity_confounds if c["status"] == "uncontrolled"]
        if parity_uncontrolled:
            if campaign_mode in ("confirmation", "sequential"):
                # Hard downgrade for confirmation/sequential
                for confound in parity_uncontrolled:
                    print(f"Parity confound check: '{confound['label']}' is uncontrolled")
                print("Parity confound check: verdict downgraded to 'confounded'")
                outcome_label = "confounded"
            elif campaign_mode == "exploration":
                # Advisory warning for exploration
                for confound in parity_uncontrolled:
                    print(
                        f"WARNING: Parity confound '{confound['label']}' is uncontrolled "
                        "(exploration mode, no downgrade)"
                    )

        # REVIEW COVERAGE GATE (build-order step 3, spec §4)
        # Every hypothesis and confound must be covered by >=1 [review] entry naming it.
        # Same downgrade posture as the parity/synthetic-recovery checks above:
        # confirmation/sequential downgrade, exploration warns only.
        from bathos.claim import review_coverage_check

        # §7 scopes this gate to confirmation/sequential only. Running it on exploration
        # campaigns would add a warning line to a mode the spec never asked it to touch.
        review_result = (
            review_coverage_check(db, full_id, claim, workspace_root=workspace_root)
            if campaign_mode in ("confirmation", "sequential")
            else None
        )
        if review_result is not None and review_result["verdict"] != "covered":
            detail = (
                "claim declares no hypotheses or confounds to cover"
                if review_result["verdict"] == "empty_slate"
                else "uncovered: " + ", ".join(review_result["uncovered"])
            )
            # ENFORCEMENT IS OPT-IN, and deliberately so. §7 sequences this gate behind step 2
            # having produced real [review] entries: "the coverage gate should be calibrated
            # against observed data". Enforcing on day one would downgrade EVERY existing
            # confirmatory campaign to 'confounded', since no sidecar authored before step 2
            # can carry a [review] block — which is a retroactive verdict change, not a gate.
            # Set BTH_REVIEW_COVERAGE_ENFORCE=1 once real entries exist. Until then the gate
            # runs, reports, and changes no verdict.
            from bathos.config import resolve_flag

            enforcing = resolve_flag(
                "BTH_REVIEW_COVERAGE_ENFORCE",
                "claim",
                "review_coverage_enforce",
                workspace_root,
            )
            if enforcing:
                print(f"Review coverage gate: {detail}")
                print("Review coverage gate: verdict downgraded to 'confounded'")
                outcome_label = "confounded"
            else:
                print(
                    f"WARNING: Review coverage gate: {detail} "
                    "(advisory until [claim] review_coverage_enforce = true, or "
                    "BTH_REVIEW_COVERAGE_ENFORCE=1)"
                )
        # Reported unconditionally: a sidecar that could not be read is NOT evidence that
        # review is absent, and the reader must be able to tell those apart.
        if review_result is not None and review_result["sidecars_unreadable"]:
            print(
                f"WARNING: Review coverage gate: {review_result['sidecars_unreadable']} member "
                f"sidecar(s) could not be read; coverage was computed from "
                f"{review_result['sidecars_read']} readable sidecar(s)"
            )

        # BP-2 SYNTHETIC_RECOVERY CONFOUND CHECK (before Union Gate)
        # Check for uncontrolled synthetic_recovery confounds (stale/red/unknown gate) and downgrade if needed
        from bathos.gate import synthetic_recovery_confound_check

        synth_result = synthetic_recovery_confound_check(claim, workspace_root)
        synth_confounds = synth_result.get("confounds", [])

        # Downgrade verdict to 'confounded' if any synthetic_recovery confound is uncontrolled
        # (except for exploration mode, which only warns) -- same pattern as parity confounds above
        synth_uncontrolled = [c for c in synth_confounds if c["status"] == "uncontrolled"]
        if synth_uncontrolled:
            if campaign_mode in ("confirmation", "sequential"):
                # Hard downgrade for confirmation/sequential
                for confound in synth_uncontrolled:
                    print(
                        f"Synthetic-recovery confound check: '{confound['label']}' is uncontrolled "
                        f"(gate '{confound['gate_name']}' is {confound['gate_state']})"
                    )
                print("Synthetic-recovery confound check: verdict downgraded to 'confounded'")
                outcome_label = "confounded"
            elif campaign_mode == "exploration":
                # Advisory warning for exploration
                for confound in synth_uncontrolled:
                    print(
                        f"WARNING: Synthetic-recovery confound '{confound['label']}' is uncontrolled "
                        f"(gate '{confound['gate_name']}' is {confound['gate_state']}) "
                        "(exploration mode, no downgrade)"
                    )

        # Run Union Gate (which may also downgrade if clauses are uncovered)
        from bathos.claim import format_clause_list

        verdict, uncovered = run_union_gate(db, full_id, claim, workspace_root=workspace_root)
        uncovered_display = format_clause_list(claim, uncovered)

        # AC-08: Gate behavior by campaign mode
        if uncovered:
            if campaign_mode in ("confirmation", "sequential"):
                if force_verdict:
                    # AC-09: Bypass with audit trail
                    print(f"Union Gate bypassed — unmapped clauses: {uncovered_display}")
                    outcome_label = outcome_label  # Keep researcher's label
                    db.execute("UPDATE campaigns SET claim_mode='bypassed' WHERE id=?", [full_id])
                else:
                    # AC-08: Soft-block downgrade to confounded
                    print(
                        "Union Gate: verdict downgraded to 'confounded' — "
                        f"unmapped clauses: {uncovered_display}"
                    )
                    outcome_label = "confounded"
            elif campaign_mode == "exploration":
                # AC-08: Warning-only for exploration
                print(
                    f"WARNING: Union Gate — unmapped clauses: {uncovered_display} "
                    "(exploration mode, no downgrade)"
                )

        # OBLIGATION TRIGGER 4 (§5.4): a [review] entry that vouched for a hypothesis the
        # run's own outcome disfavours. The highest-value trigger and the safest to enable --
        # it can only fire where a [review] entry already exists, so it cannot reach anything
        # authored before build-order step 2. Opt-in via BTH_OBLIGATION_CITATION_CONTRADICTED.
        #
        # Evaluated here rather than at run end because only conclude holds both the catalog
        # and the claim, which is where the discriminability map lives (decision D1).
        from bathos.obligations import maybe_open, trigger_enabled

        if trigger_enabled("citation_contradicted", workspace_root):
            try:
                from bathos.claim import contradicted_citations

                cc = contradicted_citations(db, full_id, claim, workspace_root=workspace_root)
                # One obligation per run, not per citation: open_obligation is idempotent on
                # (entity, trigger), so several contradicted refs on one run collapse into a
                # single obligation whose detail names them all.
                by_run: dict[str, list[str]] = {}
                for rec in cc["contradicted"]:
                    by_run.setdefault(rec["run_id"], []).append(f"{rec['ref']}→{rec['bears_on']}")
                for rid, refs in by_run.items():
                    maybe_open(
                        workspace_root,
                        "run",
                        rid,
                        "citation_contradicted",
                        detail="contradicted: " + ", ".join(refs),
                    )
                if cc["contradicted"]:
                    print(
                        f"Citation contradiction: {len(cc['contradicted'])} 'supports' "
                        f"citation(s) contradicted across {len(by_run)} run(s)"
                    )
                # §8b: a trigger that COULD NOT fire must not read as a clean bill of health.
                if cc["indeterminate"]:
                    print(
                        f"WARNING: Citation contradiction: {len(cc['indeterminate'])} of "
                        f"{cc['supports_seen']} 'supports' citation(s) were indeterminate — no "
                        "discriminability row covers the observed label, so nothing can be "
                        "concluded either way"
                    )
            except Exception as e:  # never let a trigger break conclude
                print(f"WARNING: citation contradiction check failed: {e}")

        # AC-12: emit claim-coverage JSON sidecar after union gate
        verdict_str = "covered" if not uncovered else "confounded"
        bypass_reason = "force_verdict flag" if force_verdict else None
        emit_claim_coverage_report(
            db,
            catalog_dir if catalog_dir is not None else Path.home() / ".bth" / "catalog",
            full_id,
            verdict_str,
            uncovered,
            claim,
            bypass_reason=bypass_reason,
        )

    # ── OBLIGATION GATE (D1: conclude is the only binding site) ──────────────────────────
    # Runs outside a campaign never reach here, which is the accepted cost §5 names: an
    # exploratory campaign that is never concluded never pays, and Signal 11 is what makes
    # that visible rather than silent.
    from bathos.obligations import (
        ENFORCE_FLAG,
        enforcement_enabled,
        list_obligations_for_scope,
        maybe_open,
    )

    # The gate must never be able to break a conclude: a campaign whose verdict is already
    # decided cannot be lost to a ledger read. Resolution is inside the guard because it is
    # the only step here that touches config and git.
    try:
        if workspace_root is None:
            workspace_root = resolve_workspace(Path.cwd()).fs_root
        member_ids = [
            r[0]
            for r in db.execute(
                "SELECT run_id FROM campaign_runs WHERE campaign_id = ?", [full_id]
            ).fetchall()
        ]
        open_obs = list_obligations_for_scope(workspace_root, {full_id, *member_ids})
    except Exception as e:
        print(f"WARNING: could not read the obligation ledger: {e}")
        open_obs = []

    if open_obs:
        for ob in open_obs:
            print(
                f"Open obligation: {ob.obligation_id} ({ob.trigger}, "
                f"{ob.age_days():.1f}d) — {ob.detail or 'no detail'}"
            )
        # Same split as the Review Coverage Gate: the check always runs and always reports;
        # only the verdict change is opt-in. Enforcing by default would let an obligation
        # opened by a newly-enabled trigger retroactively downgrade an unrelated campaign.
        if enforcement_enabled(workspace_root):
            if campaign_mode in ("confirmation", "sequential"):
                print(
                    f"Obligation gate: {len(open_obs)} open obligation(s) — "
                    "verdict downgraded to 'confounded'"
                )
                outcome_label = "confounded"
            elif campaign_mode == "exploration":
                print(
                    f"WARNING: Obligation gate: {len(open_obs)} open obligation(s) "
                    "(exploration mode, no downgrade)"
                )
        else:
            print(
                f"WARNING: Obligation gate: {len(open_obs)} open obligation(s) unexplained "
                f"(advisory until {ENFORCE_FLAG}=1)"
            )

    # OBLIGATION TRIGGER 2 (§5.2): the campaign concluded 'confounded' — whether the
    # researcher labelled it so directly, or one of the gates above downgraded it. Opened
    # AFTER the gate so a campaign cannot downgrade itself on an obligation this same
    # conclude created.
    if outcome_label == "confounded" and workspace_root is not None:
        try:
            maybe_open(
                workspace_root,
                "campaign",
                full_id,
                "campaign_confounded",
                detail=f"concluded confounded: {conclusion[:200]}" if conclusion else "",
            )
        except Exception as e:
            print(f"WARNING: could not open a campaign_confounded obligation: {e}")

    # Final update
    concluded_at = datetime.now(UTC).isoformat()
    exists = db.execute("SELECT 1 FROM campaigns WHERE id = ?", [full_id]).fetchone()
    if not exists and catalog_dir is not None:
        cool = next((c for c in read_cool_campaigns(catalog_dir) if c.id == full_id), None)
        if cool is not None:
            db.execute(
                """
                INSERT INTO campaigns (
                    id, project_slug, name, mode, question, hypothesis, status,
                    started_at, concluded_at, conclusion, outcome_label,
                    parent_campaign_id, stopping_threshold, negative_check,
                    claim_path, claim_sha256, claim_mode
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    cool.id,
                    cool.project_slug,
                    cool.name,
                    cool.mode,
                    cool.question,
                    cool.hypothesis,
                    cool.status,
                    cool.started_at,
                    cool.concluded_at,
                    cool.conclusion,
                    cool.outcome_label,
                    cool.parent_campaign_id,
                    cool.stopping_threshold,
                    cool.negative_check,
                    cool.claim_path,
                    cool.claim_sha256,
                    cool.claim_mode,
                ],
            )
    db.execute(
        "UPDATE campaigns SET status = 'concluded', concluded_at = ?, outcome_label = ?, conclusion = ?, negative_check = ? WHERE id = ?",
        [concluded_at, outcome_label, conclusion, negative_check, full_id],
    )
    db.commit()
    if catalog_dir is not None:
        refreshed = get_campaign(db, full_id, catalog_dir=catalog_dir)
        if refreshed is not None:
            write_campaign_cool(refreshed, catalog_dir)
    event("campaign.conclude", campaign_id=full_id, verdict=outcome_label)

    _run_campaign_conclude_hooks(
        db, full_id, outcome_label, catalog_dir, member_ids=precomputed_member_ids
    )


def _nz(value: str | None) -> str | None:
    return value if value else None


def _merge_warm_cool(warm: Campaign | None, cool: Campaign | None) -> Campaign | None:
    """Merge warm DuckDB and cool JSON using ingest warm-wins rules."""
    if warm is None:
        return cool
    if cool is None:
        return warm
    concluded = warm.status == "concluded"
    return Campaign(
        id=warm.id,
        project_slug=cool.project_slug or warm.project_slug,
        name=cool.name or warm.name,
        mode=warm.mode,
        question=cool.question if cool.question is not None else warm.question,
        hypothesis=cool.hypothesis if cool.hypothesis is not None else warm.hypothesis,
        status=warm.status if concluded else (cool.status or warm.status),
        started_at=cool.started_at or warm.started_at,
        concluded_at=warm.concluded_at if concluded else cool.concluded_at,
        conclusion=warm.conclusion if concluded else cool.conclusion,
        outcome_label=warm.outcome_label if concluded else cool.outcome_label,
        parent_campaign_id=cool.parent_campaign_id or warm.parent_campaign_id,
        stopping_threshold=(
            warm.stopping_threshold
            if warm.stopping_threshold is not None
            else cool.stopping_threshold
        ),
        negative_check=_nz(warm.negative_check) or _nz(cool.negative_check),
        claim_path=_nz(warm.claim_path) or _nz(cool.claim_path),
        claim_sha256=_nz(warm.claim_sha256) or _nz(cool.claim_sha256),
        claim_mode=_nz(warm.claim_mode) or _nz(cool.claim_mode),
    )


def get_campaign(db, campaign_id: str, catalog_dir: Path | None = None) -> Campaign | None:
    """Fetch campaign by ID from warm DuckDB merged with cool JSON."""
    try:
        full_id = _resolve_campaign_id(db, campaign_id, catalog_dir=catalog_dir)
    except CampaignError as e:
        if "Ambiguous" in str(e):
            raise
        return None
    warm: Campaign | None = None
    if db is not None:
        try:
            rows = db.execute(
                "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold, negative_check, claim_path, claim_sha256, claim_mode FROM campaigns WHERE id = ?",
                [full_id],
            ).fetchall()
        except duckdb.Error:
            try:
                rows = db.execute(
                    "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold, negative_check FROM campaigns WHERE id = ?",
                    [full_id],
                ).fetchall()
            except duckdb.Error:
                rows = db.execute(
                    "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold FROM campaigns WHERE id = ?",
                    [full_id],
                ).fetchall()
                if rows:
                    r = rows[0]
                    warm = Campaign(
                        id=r[0],
                        project_slug=r[1],
                        name=r[2],
                        mode=r[3],
                        question=r[4],
                        hypothesis=r[5],
                        status=r[6],
                        started_at=r[7],
                        concluded_at=r[8],
                        conclusion=r[9],
                        outcome_label=r[10],
                        parent_campaign_id=r[11],
                        stopping_threshold=r[12],
                    )
                rows = []
            else:
                if rows:
                    r = rows[0]
                    warm = Campaign(
                        id=r[0],
                        project_slug=r[1],
                        name=r[2],
                        mode=r[3],
                        question=r[4],
                        hypothesis=r[5],
                        status=r[6],
                        started_at=r[7],
                        concluded_at=r[8],
                        conclusion=r[9],
                        outcome_label=r[10],
                        parent_campaign_id=r[11],
                        stopping_threshold=r[12],
                        negative_check=r[13],
                    )
                rows = []
        if rows:
            r = rows[0]
            extra = r[14:] if len(r) > 14 else (None, None, None)
            warm = Campaign(
                id=r[0],
                project_slug=r[1],
                name=r[2],
                mode=r[3],
                question=r[4],
                hypothesis=r[5],
                status=r[6],
                started_at=r[7],
                concluded_at=r[8],
                conclusion=r[9],
                outcome_label=r[10],
                parent_campaign_id=r[11],
                stopping_threshold=r[12],
                negative_check=r[13],
                claim_path=extra[0] if extra else None,
                claim_sha256=extra[1] if len(extra) > 1 else None,
                claim_mode=extra[2] if len(extra) > 2 else None,
            )
    cool = None
    if catalog_dir is not None:
        cool = next((c for c in read_cool_campaigns(catalog_dir) if c.id == full_id), None)
    return _merge_warm_cool(warm, cool)


def list_campaigns(
    db,
    project_slug: str | None = None,
    status: str | None = None,
    catalog_dir: Path | None = None,
) -> list[Campaign]:
    """List campaigns with optional filters, unioning cool JSON when catalog_dir is set."""
    by_id: dict[str, Campaign] = {}
    if db is not None:
        query = "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold, negative_check, claim_path, claim_sha256, claim_mode FROM campaigns WHERE 1=1"
        params: list = []
        if project_slug:
            query += " AND project_slug = ?"
            params.append(project_slug)
        try:
            rows = db.execute(query, params).fetchall()
        except duckdb.Error:
            query = "SELECT id, project_slug, name, mode, question, hypothesis, status, started_at, concluded_at, conclusion, outcome_label, parent_campaign_id, stopping_threshold, negative_check FROM campaigns WHERE 1=1"
            if project_slug:
                query += " AND project_slug = ?"
            rows = db.execute(query, params).fetchall()
        for r in rows:
            extra = r[14:] if len(r) > 14 else (None, None, None)
            by_id[r[0]] = Campaign(
                id=r[0],
                project_slug=r[1],
                name=r[2],
                mode=r[3],
                question=r[4],
                hypothesis=r[5],
                status=r[6],
                started_at=r[7],
                concluded_at=r[8],
                conclusion=r[9],
                outcome_label=r[10],
                parent_campaign_id=r[11],
                stopping_threshold=r[12],
                negative_check=r[13],
                claim_path=extra[0] if extra else None,
                claim_sha256=extra[1] if len(extra) > 1 else None,
                claim_mode=extra[2] if len(extra) > 2 else None,
            )
    if catalog_dir is not None:
        for cool in read_cool_campaigns(catalog_dir):
            if project_slug and cool.project_slug != project_slug:
                continue
            by_id[cool.id] = _merge_warm_cool(by_id.get(cool.id), cool)
    campaigns = [c for c in by_id.values() if c is not None]
    if status:
        campaigns = [c for c in campaigns if c.status == status]
    return campaigns


def review_campaign(db, campaign_id: str, catalog_dir: Path | None = None) -> dict:
    """Generate campaign review: residual rate, bypass rate, outcome distribution, anomalies, and POPPER summary."""
    try:
        campaign_id = _resolve_campaign_id(db, campaign_id, catalog_dir=catalog_dir)
    except CampaignError as e:
        return {"error": str(e)}
    rows = []
    if db is not None:
        try:
            rows = db.execute(
                """
                SELECT r.id, r.sidecar_mode, r.outcome, r.outcome_is_residual
                FROM campaign_runs cr
                INNER JOIN runs r ON cr.run_id = r.id
                WHERE cr.campaign_id = ?
            """,
                [campaign_id],
            ).fetchall()
        except duckdb.Error:
            rows = []
    by_id = {r[0]: r for r in rows}
    if catalog_dir is not None:
        from bathos.catalog import read_runs

        for r in read_runs(catalog_dir):
            if r.campaign_id == campaign_id and r.id not in by_id:
                by_id[r.id] = (r.id, r.sidecar_mode, r.outcome, r.outcome_is_residual)
    rows = list(by_id.values())

    if not rows:
        return {"error": f"Campaign {campaign_id} not found or has no runs"}

    total = len(rows)
    residual_count = sum(1 for r in rows if r[3])
    bypassed_count = sum(1 for r in rows if r[1] == "bypassed")
    unknown_count = sum(1 for r in rows if r[2] in ("unknown", ""))

    outcome_dist = {}
    for r in rows:
        outcome_dist[r[2] or "unknown"] = outcome_dist.get(r[2] or "unknown", 0) + 1

    anomalies = []
    residual_rate = residual_count / total
    bypass_rate = bypassed_count / total
    unknown_rate = unknown_count / total
    if residual_rate > 0.10:
        anomalies.append(f"High residual rate: {residual_rate:.1%} ({residual_count}/{total} runs)")
    if bypass_rate > 0.10:
        anomalies.append(f"High bypass rate: {bypass_rate:.1%} ({bypassed_count}/{total} runs)")
    if unknown_count > 0:
        anomalies.append(f"{unknown_count} runs with unknown outcome")

    # POPPER sequential test summary
    popper_data = None
    campaign_meta = None
    if db is not None:
        try:
            campaign_meta = db.execute(
                "SELECT mode, stopping_threshold FROM campaigns WHERE id = ?", [campaign_id]
            ).fetchone()
        except duckdb.Error:
            campaign_meta = None
    if campaign_meta is None and catalog_dir is not None:
        cool_c = next((c for c in read_cool_campaigns(catalog_dir) if c.id == campaign_id), None)
        if cool_c is not None:
            campaign_meta = (cool_c.mode, cool_c.stopping_threshold)
    if campaign_meta and campaign_meta[0] == "sequential":
        stopping_threshold = campaign_meta[1]
        script_rows = []
        if db is not None:
            try:
                script_rows = db.execute(
                    """
            SELECT
                COALESCE(NULLIF(r.script_sha256, ''), r.sidecar_path, '_ungrouped') AS script_key,
                COUNT(*) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown') AS n_effective,
                COUNT(*) FILTER (WHERE r.outcome = 'error' OR r.outcome = 'unknown') AS n_excluded,
                EXP(SUM(LN(cr.evalue)) FILTER (WHERE r.outcome != 'error' AND r.outcome != 'unknown')) AS evalue_product
            FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ? AND cr.evalue IS NOT NULL
            GROUP BY script_key
            ORDER BY script_key
        """,
                    [campaign_id],
                ).fetchall()
            except duckdb.Error:
                script_rows = []

        scripts = []
        for sr in script_rows:
            ep = sr[3] if sr[3] is not None else 1.0
            met = stopping_threshold is not None and ep >= stopping_threshold
            scripts.append(
                {
                    "script_key": sr[0],
                    "n_effective": sr[1],
                    "n_excluded": sr[2],
                    "evalue_product": ep,
                    "threshold_met": met,
                }
            )

        threshold_met = (
            len(scripts) > 0
            and stopping_threshold is not None
            and all(s["threshold_met"] for s in scripts)
        )
        popper_data = {
            "mode": "sequential",
            "stopping_threshold": stopping_threshold,
            "threshold_met": threshold_met,
            "scripts": scripts,
        }
        if not scripts:
            popper_data["gap"] = "evalues_unavailable"

    return {
        "total_runs": total,
        "residual_rate": residual_rate,
        "bypass_rate": bypass_rate,
        "unknown_rate": unknown_rate,
        "outcome_distribution": outcome_dist,
        "anomalies": anomalies,
        "popper": popper_data,
    }


def emit_campaign_report(
    db, catalog_dir: str, campaign_id: str, figure_manifest_ref: str | None = None
) -> None:
    """Emit a campaign report JSON sidecar at <catalog>/sidecars/<campaign_id>/campaign_report.json.

    This function generates a truth-only report capturing summary stats from the campaign,
    closing the recon gap where campaign_review renders stats to console and discards them.

    Args:
        db: DuckDB connection.
        catalog_dir: Path to the bathos catalog root (where sidecars/ lives).
        campaign_id: Campaign ID to generate the report for.
        figure_manifest_ref: Optional path reference to the figure manifest
            (e.g., "sidecars/<campaign_id>/figure_manifest.json").

    Raises:
        CampaignError: If campaign not found or has no runs.
    """
    from pathlib import Path

    from bathos.campaign_report import CampaignReport

    cat = Path(catalog_dir)
    campaign_id = _resolve_campaign_id(db, campaign_id, catalog_dir=cat)
    campaign = get_campaign(db, campaign_id, catalog_dir=cat)
    if campaign is None:
        raise CampaignError(f"Campaign {campaign_id} not found")
    campaign_conclusion = campaign.conclusion

    member_ids = union_campaign_member_ids(db, campaign_id, cat)
    if not member_ids:
        review_data = {
            "total_runs": 0,
            "residual_rate": 0.0,
            "bypass_rate": 0.0,
            "unknown_rate": 0.0,
            "outcome_distribution": {},
            "anomalies": [],
            "popper": None,
        }
        stage_breakdown = {}
    else:
        review_data = review_campaign(db, campaign_id, catalog_dir=cat)
        if "error" in review_data:
            raise CampaignError(review_data["error"])
        stage_breakdown = {}
        if db is not None:
            stage_rows = db.execute(
                """
            SELECT COALESCE(NULLIF(r.stage_name, ''), NULL) AS stage_key, COUNT(*) AS count
            FROM campaign_runs cr
            INNER JOIN runs r ON cr.run_id = r.id
            WHERE cr.campaign_id = ?
            GROUP BY stage_key
        """,
                [campaign_id],
            ).fetchall()
            for stage_key, count in stage_rows:
                stage_breakdown[stage_key] = count

    # Create the campaign report
    report = CampaignReport(
        report_version="1.0",
        campaign_id=campaign_id,
        total_runs=review_data["total_runs"],
        residual_rate=review_data["residual_rate"],
        bypass_rate=review_data["bypass_rate"],
        unknown_rate=review_data["unknown_rate"],
        outcome_distribution=review_data["outcome_distribution"],
        anomalies=review_data["anomalies"],
        popper=review_data["popper"],
        conclude=campaign_conclusion,
        figure_manifest_ref=figure_manifest_ref,
        stage_breakdown=stage_breakdown,
    )

    # Write the report to the sidecar path
    sidecar_dir = Path(catalog_dir) / "sidecars" / campaign_id
    report_path = sidecar_dir / "campaign_report.json"
    report.write_report(report_path)

    event("campaign.report.emit", campaign_id=campaign_id, report_path=str(report_path))


def emit_figure_manifest(db, catalog_dir: str, campaign_id: str) -> None:
    """Emit an empty figure manifest JSON sidecar at <catalog>/sidecars/<campaign_id>/figure_manifest.json.

    This function generates a truth-only figure manifest that declares figure INTENT
    (which runs/data a figure derives from) without rendering artifacts. Rendering remains
    maraxiom's concern.

    For now, bathos emits an empty manifest (zero figures) since all rendering is delegated
    to maraxiom. The manifest structure is prepared for future figure pinning if needed.

    Args:
        db: DuckDB connection.
        catalog_dir: Path to the bathos catalog root (where sidecars/ lives).
        campaign_id: Campaign ID to generate the manifest for.

    Raises:
        CampaignError: If campaign not found.
    """
    from pathlib import Path

    from bathos.figure_manifest import FigureManifest

    cat = Path(catalog_dir)
    campaign_id = _resolve_campaign_id(db, campaign_id, catalog_dir=cat)
    campaign = get_campaign(db, campaign_id, catalog_dir=cat)
    if campaign is None:
        raise CampaignError(f"Campaign {campaign_id} not found")

    # Create an empty figure manifest (bathos truth-only: no rendering)
    manifest = FigureManifest(
        manifest_version="1.0",
        campaign_id=campaign_id,
        figures=[],  # Empty: all rendering delegated to maraxiom
    )

    # Write the manifest to the sidecar path
    sidecar_dir = Path(catalog_dir) / "sidecars" / campaign_id
    manifest_path = sidecar_dir / "figure_manifest.json"
    manifest.write_manifest(manifest_path)

    event("campaign.manifest.emit", campaign_id=campaign_id, manifest_path=str(manifest_path))


def emit_claim_coverage_report(
    db,  # noqa: ARG001 - kept for signature compatibility
    catalog_dir: str | Path,
    campaign_id: str,
    verdict: str,
    uncovered_clauses: list[str],
    claim,
    bypass_reason: str | None = None,
) -> None:
    """Emit a claim-coverage JSON report to the catalog sidecar directory.

    AC-12 implementation: Creates a JSON report documenting union gate clause coverage.

    Args:
        db: DuckDB connection (not used in this implementation, kept for signature compatibility)
        catalog_dir: Path to the bathos catalog root (where sidecars/ lives)
        campaign_id: Campaign ID
        verdict: Result of union gate check ('covered' or 'confounded')
        uncovered_clauses: List of clause IDs that were not covered by any run
        claim: ClaimFile object parsed from claim.bth.toml
        bypass_reason: Optional reason if verdict was bypassed (e.g., "force_verdict flag")

    Raises:
        None (errors are raised for directory creation failures)
    """
    import json
    from pathlib import Path

    # Compute coverage fraction
    total_clauses = len(claim.union_gate_clauses)
    covered_clauses = [
        c["id"] for c in claim.union_gate_clauses if c["id"] not in uncovered_clauses
    ]
    coverage_fraction = len(covered_clauses) / total_clauses if total_clauses > 0 else 1.0

    # Determine if verdict was blocked (confounded with no bypass)
    verdict_blocked = verdict == "confounded" and bypass_reason is None

    # Build JSON payload
    from bathos.claim import format_clause_ref

    clause_labels = {
        clause.get("id"): format_clause_ref(clause)
        for clause in claim.union_gate_clauses
        if clause.get("id")
    }

    payload = {
        "coverage_fraction": coverage_fraction,
        "covered_clauses": covered_clauses,
        "uncovered_clauses": uncovered_clauses,
        "clause_labels": clause_labels,
        "contradicted_clauses": [],  # AC-12: placeholder for future
        "verdict_blocked": verdict_blocked,
        "bypass_reason": bypass_reason,
    }

    # Write to sidecar directory with atomic write-then-rename
    catalog_path = Path(catalog_dir)
    sidecar_dir = catalog_path / "sidecars" / campaign_id
    sidecar_dir.mkdir(parents=True, exist_ok=True)

    filename = f"claim_coverage_{campaign_id}.json"
    tmp_path = sidecar_dir / (filename + ".tmp")
    final_path = sidecar_dir / filename

    # Atomic write
    with open(tmp_path, "w") as f:
        json.dump(payload, f, indent=2)
    tmp_path.rename(final_path)

    event("claim.coverage_report.emit", campaign_id=campaign_id, report_path=str(final_path))
