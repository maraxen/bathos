"""First-class observation ledger (spec 261015, §3.3, §3.3.1 & S3).

Supports:
- Run-backed observations (``bth observe <run_id> "<text>"``) linking to the run and output sha.
- Run-less observations (``bth observe --ref ... --locator ... --claim ...``) with skepticism fields
  and appraisal linkage (unappraised observations record ``status="open", unappraised=True``).
- Append-only corrections via ``supersedes = <obs_id>``.
- Separate status events (``observation.status`` with ``of = <obs_id>``) in
  ``{"open", "refuted", "survived-seed-check", "promoted"}``.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.appraisal import (
    AppraisalRecord,
    compute_use_tier,
    get_appraisal,
    list_appraisals,
    validate_uses_as,
)
from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.errors import BathosError, BathosErrorCode
from bathos.index import connect_read
from bathos.query import AmbiguousRunId, get_run
from bathos.runlog.emit import emit_or_legacy, unit_of_work
from bathos.runlog.envelope import uuid7
from bathos.runlog.mode import is_log_mode
from bathos.stepback.escalation import AGENT_ENV_VARS
from bathos.workspace import resolve_workspace

OBSERVATION_STATUSES: frozenset[str] = frozenset(
    {"open", "refuted", "survived-seed-check", "promoted"}
)
OBSERVATION_ORIGINS: frozenset[str] = frozenset({"human", "agent"})
VALUE_PROVENANCES: frozenset[str] = frozenset(
    {"published", "figure-read", "derived", "agent-summary"}
)
LITERATURE_DISPOSITIONS: frozenset[str] = frozenset(
    {"supports", "contradicts", "scope-differs"}
)
USES_AS_VALUES: frozenset[str] = frozenset(
    {"contrast-context", "hypothesis-source", "threshold-source"}
)

_OBS_FRAGMENT_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("project_slug", pa.string()),
        pa.field("run_id", pa.string()),
        pa.field("text", pa.string()),
        pa.field("figure", pa.string()),
        pa.field("output_sha256", pa.string()),
        pa.field("output_shas_json", pa.string()),
        pa.field("tags_json", pa.string()),
        pa.field("supersedes", pa.string()),
        pa.field("status", pa.string()),
        pa.field("origin", pa.string()),
        pa.field("agent_session", pa.string()),
        pa.field("ref", pa.string()),
        pa.field("locator", pa.string()),
        pa.field("claim", pa.string()),
        pa.field("quote", pa.string()),
        pa.field("value_provenance", pa.string()),
        pa.field("quote_checked", pa.bool_()),
        pa.field("disposition", pa.string()),
        pa.field("bears_on", pa.string()),
        pa.field("uses_as", pa.string()),
        pa.field("appraisal_ids_json", pa.string()),
        pa.field("unappraised", pa.bool_()),
        pa.field("created_at", pa.string()),
        pa.field("eid", pa.string()),
    ]
)

_OBS_STATUS_FRAGMENT_SCHEMA = pa.schema(
    [
        pa.field("eid", pa.string()),
        pa.field("obs_id", pa.string()),
        pa.field("status", pa.string()),
        pa.field("reason", pa.string()),
        pa.field("actor", pa.string()),
        pa.field("agent_session", pa.string()),
        pa.field("ts", pa.string()),
    ]
)


# ── Domain Errors ────────────────────────────────────────────────────────────


class ObservationError(BathosError):
    """Base error for observation validation, lookup, and persistence."""

    code = BathosErrorCode.INVALID_PARAM


class ObservationValidationError(ObservationError):
    """Raised when observation inputs violate §3.3 / §3.3.1 rules."""

    code = BathosErrorCode.INVALID_PARAM


class ObservationNotFoundError(ObservationError):
    """Raised when a target observation id does not exist."""

    code = BathosErrorCode.INVALID_PARAM


class ObservationRunNotFoundError(ObservationError):
    """Raised when a run-backed observation references an unknown run id."""

    code = BathosErrorCode.RUN_NOT_FOUND


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ObservationRecord:
    """One persisted observation row (run-backed or run-less)."""

    id: str
    project_slug: str
    run_id: str
    text: str
    figure: str
    output_sha256: str
    output_shas: dict[str, str]
    tags: tuple[str, ...]
    supersedes: str
    status: str
    origin: str
    agent_session: str
    ref: str
    locator: str
    claim: str
    quote: str
    value_provenance: str
    quote_checked: bool
    disposition: str
    bears_on: str
    uses_as: str
    appraisal_ids: tuple[str, ...]
    unappraised: bool
    created_at: str
    eid: str = ""
    superseded_by: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "obs_id": self.id,
            "project_slug": self.project_slug,
            "run_id": self.run_id,
            "text": self.text,
            "figure": self.figure,
            "output_sha256": self.output_sha256,
            "output_shas": dict(self.output_shas),
            "tags": list(self.tags),
            "supersedes": self.supersedes,
            "superseded_by": list(self.superseded_by),
            "status": self.status,
            "origin": self.origin,
            "agent_session": self.agent_session,
            "ref": self.ref,
            "locator": self.locator,
            "claim": self.claim,
            "quote": self.quote,
            "value_provenance": self.value_provenance,
            "quote_checked": self.quote_checked,
            "disposition": self.disposition,
            "bears_on": self.bears_on,
            "uses_as": self.uses_as,
            "appraisal_ids": list(self.appraisal_ids),
            "unappraised": self.unappraised,
            "created_at": self.created_at,
            "eid": self.eid,
        }


@dataclass(frozen=True)
class ObservationStatusRecord:
    """One append-only observation.status transition row."""

    eid: str
    obs_id: str
    status: str
    reason: str
    actor: str
    agent_session: str
    ts: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "eid": self.eid,
            "obs_id": self.obs_id,
            "status": self.status,
            "reason": self.reason,
            "actor": self.actor,
            "agent_session": self.agent_session,
            "ts": self.ts,
        }


# ── Helpers ──────────────────────────────────────────────────────────────────


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"))


def _resolve_catalog_dir(catalog_dir: Path | str | None = None) -> Path:
    if catalog_dir:
        return Path(catalog_dir)
    env_cat = os.environ.get("BTH_CATALOG_DIR")
    if env_cat:
        return Path(env_cat)
    return default_catalog_dir()


def _resolve_workspace_root(project_root: Path | str | None = None) -> Path:
    if project_root:
        return Path(project_root).expanduser().resolve()
    try:
        return resolve_workspace().fs_root
    except Exception:
        return Path.cwd()


def _resolve_project_slug(root: Path) -> str:
    env_slug = os.environ.get("BTH_PROJECT_SLUG")
    if env_slug:
        return env_slug
    cfg_path = find_project_config(root)
    if cfg_path and cfg_path.is_file():
        try:
            return load_project_config(cfg_path).slug
        except Exception:
            pass
    return "default"


def _detect_origin_and_session(
    origin: str = "",
    agent_session: str = "",
    *,
    default_origin: str | None = None,
) -> tuple[str, str]:
    env_session = (
        os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("BTH_AGENT_SESSION") or ""
    ).strip()
    resolved_session = agent_session.strip() or env_session

    if origin:
        resolved_origin = origin.strip()
        if resolved_origin not in OBSERVATION_ORIGINS:
            raise ObservationValidationError(
                f"origin must be one of {sorted(OBSERVATION_ORIGINS)}, got {resolved_origin!r}"
            )
    elif resolved_session or any(bool(os.environ.get(v)) for v in AGENT_ENV_VARS):
        resolved_origin = "agent"
    elif default_origin in OBSERVATION_ORIGINS:
        resolved_origin = default_origin
    else:
        resolved_origin = "human"

    return resolved_origin, resolved_session


def _normalize_str_list(val: list[str] | tuple[str, ...] | str | None) -> tuple[str, ...]:
    if not val:
        return ()
    if isinstance(val, str):
        items = [x.strip() for x in val.split(",") if x.strip()]
    else:
        items = []
        for entry in val:
            for part in str(entry).split(","):
                stripped = part.strip()
                if stripped:
                    items.append(stripped)
    seen: set[str] = set()
    unique: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            unique.append(item)
    return tuple(unique)


def _sha256_file(path: Path) -> str | None:
    if not path.is_file():
        return None
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _extract_run_output_hashes(run: Any) -> dict[str, str]:
    """Read any pre-recorded output sha256 map from run.output_metadata / metadata."""
    hashes: dict[str, str] = {}
    raw_om = getattr(run, "output_metadata", None)
    if isinstance(raw_om, str) and raw_om.strip():
        try:
            om = json.loads(raw_om)
            if isinstance(om, dict):
                for key in ("files", "hashes", "outputs"):
                    sub = om.get(key)
                    if isinstance(sub, dict):
                        for k, v in sub.items():
                            if isinstance(v, str) and len(v) == 64:
                                hashes[str(k)] = v
                            elif isinstance(v, dict) and isinstance(v.get("sha256"), str):
                                hashes[str(k)] = v["sha256"]
            elif isinstance(om, list):
                for item in om:
                    if isinstance(item, dict) and item.get("path") and item.get("sha256"):
                        hashes[str(item["path"])] = str(item["sha256"])
        except json.JSONDecodeError:
            pass
    return hashes


def _resolve_output_shas(
    run: Any,
    figure: str,
    ws_root: Path,
) -> tuple[str, dict[str, str]]:
    """Resolve output sha256(s) for ``figure`` and the run's output_paths."""
    recorded = _extract_run_output_hashes(run)
    shas: dict[str, str] = dict(recorded)

    candidate_paths: list[str] = []
    if figure:
        candidate_paths.append(figure)
    run_outputs = getattr(run, "output_paths", None) or []
    for op in run_outputs:
        if op not in candidate_paths:
            candidate_paths.append(str(op))

    out_dir_env = os.environ.get("BTH_OUTPUT_DIR")
    for raw_p in candidate_paths:
        if raw_p in shas:
            continue
        p = Path(raw_p)
        search = [p] if p.is_absolute() else [ws_root / p, Path.cwd() / p]
        if out_dir_env and not p.is_absolute():
            search.append(Path(out_dir_env) / p)
        # Also match by basename against run output_paths
        for rop in run_outputs:
            rop_path = Path(str(rop))
            if rop_path.name == p.name:
                search.append(rop_path if rop_path.is_absolute() else ws_root / rop_path)
        for cand in search:
            digest = _sha256_file(cand)
            if digest:
                shas[raw_p] = digest
                break

    primary_sha = ""
    if figure:
        if figure in shas:
            primary_sha = shas[figure]
        else:
            fig_name = Path(figure).name
            for k, v in shas.items():
                if Path(k).name == fig_name:
                    primary_sha = v
                    break
    elif shas:
        primary_sha = next(iter(shas.values()))

    return primary_sha, shas


# ── Legacy Parquet fragment I/O + DuckDB index reads ─────────────────────────


def _obs_fragments_dir(catalog_dir: Path, project_slug: str) -> Path:
    return catalog_dir / "observations" / project_slug


def _write_obs_fragment(record: ObservationRecord, catalog_dir: Path) -> None:
    frag_dir = _obs_fragments_dir(catalog_dir, record.project_slug)
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / f"{record.id}.parquet"
    tmp = frag_dir / f"{record.id}.tmp.parquet"

    table = pa.table(
        {
            "id": [record.id],
            "project_slug": [record.project_slug],
            "run_id": [record.run_id],
            "text": [record.text],
            "figure": [record.figure],
            "output_sha256": [record.output_sha256],
            "output_shas_json": [_canonical_json(record.output_shas)],
            "tags_json": [_canonical_json(list(record.tags))],
            "supersedes": [record.supersedes],
            "status": [record.status],
            "origin": [record.origin],
            "agent_session": [record.agent_session],
            "ref": [record.ref],
            "locator": [record.locator],
            "claim": [record.claim],
            "quote": [record.quote],
            "value_provenance": [record.value_provenance],
            "quote_checked": [record.quote_checked],
            "disposition": [record.disposition],
            "bears_on": [record.bears_on],
            "uses_as": [record.uses_as],
            "appraisal_ids_json": [_canonical_json(list(record.appraisal_ids))],
            "unappraised": [record.unappraised],
            "created_at": [record.created_at],
            "eid": [record.eid],
        },
        schema=_OBS_FRAGMENT_SCHEMA,
    )
    t_start = time.monotonic()
    pq.write_table(table, tmp)
    tmp.rename(target)
    _ = time.monotonic() - t_start


def _write_obs_status_fragment(
    status_rec: ObservationStatusRecord, project_slug: str, catalog_dir: Path
) -> None:
    frag_dir = _obs_fragments_dir(catalog_dir, project_slug or "default")
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / f"status_{status_rec.obs_id}_{status_rec.eid}.parquet"
    tmp = frag_dir / f"status_{status_rec.obs_id}_{status_rec.eid}.tmp.parquet"

    table = pa.table(
        {
            "eid": [status_rec.eid],
            "obs_id": [status_rec.obs_id],
            "status": [status_rec.status],
            "reason": [status_rec.reason],
            "actor": [status_rec.actor],
            "agent_session": [status_rec.agent_session],
            "ts": [status_rec.ts],
        },
        schema=_OBS_STATUS_FRAGMENT_SCHEMA,
    )
    pq.write_table(table, tmp)
    tmp.rename(target)


def _record_from_row(row: dict[str, Any]) -> ObservationRecord:
    shas_raw = row.get("output_shas_json") or "{}"
    tags_raw = row.get("tags_json") or "[]"
    appr_raw = row.get("appraisal_ids_json") or "[]"
    shas = json.loads(shas_raw) if isinstance(shas_raw, str) else dict(shas_raw)
    tags = json.loads(tags_raw) if isinstance(tags_raw, str) else list(tags_raw)
    apprs = json.loads(appr_raw) if isinstance(appr_raw, str) else list(appr_raw)
    return ObservationRecord(
        id=str(row.get("id") or ""),
        project_slug=str(row.get("project_slug") or ""),
        run_id=str(row.get("run_id") or ""),
        text=str(row.get("text") or ""),
        figure=str(row.get("figure") or ""),
        output_sha256=str(row.get("output_sha256") or ""),
        output_shas={str(k): str(v) for k, v in shas.items()},
        tags=tuple(str(t) for t in tags),
        supersedes=str(row.get("supersedes") or ""),
        status=str(row.get("status") or "open"),
        origin=str(row.get("origin") or "human"),
        agent_session=str(row.get("agent_session") or ""),
        ref=str(row.get("ref") or ""),
        locator=str(row.get("locator") or ""),
        claim=str(row.get("claim") or ""),
        quote=str(row.get("quote") or ""),
        value_provenance=str(row.get("value_provenance") or ""),
        quote_checked=bool(row.get("quote_checked", False)),
        disposition=str(row.get("disposition") or ""),
        bears_on=str(row.get("bears_on") or ""),
        uses_as=str(row.get("uses_as") or ""),
        appraisal_ids=tuple(str(a) for a in apprs),
        unappraised=bool(row.get("unappraised", False)),
        created_at=str(row.get("created_at") or ""),
        eid=str(row.get("eid") or ""),
    )


def _read_legacy_obs_fragments(
    catalog_dir: Path,
) -> tuple[list[ObservationRecord], list[ObservationStatusRecord]]:
    root = catalog_dir / "observations"
    if not root.is_dir():
        return [], []
    obs_list: list[ObservationRecord] = []
    for f in sorted(root.rglob("obs_*.parquet")):
        table = pq.read_table(f)
        d = table.to_pydict()
        for i in range(table.num_rows):
            obs_list.append(_record_from_row({k: d[k][i] for k in d}))

    status_list: list[ObservationStatusRecord] = []
    for f in sorted(root.rglob("status_*.parquet")):
        table = pq.read_table(f)
        d = table.to_pydict()
        for i in range(table.num_rows):
            status_list.append(
                ObservationStatusRecord(
                    eid=str(d["eid"][i] or ""),
                    obs_id=str(d["obs_id"][i] or ""),
                    status=str(d["status"][i] or ""),
                    reason=str(d["reason"][i] or ""),
                    actor=str(d["actor"][i] or ""),
                    agent_session=str(d["agent_session"][i] or ""),
                    ts=str(d["ts"][i] or ""),
                )
            )
    return obs_list, status_list


def _attach_superseded_by(records: list[ObservationRecord]) -> list[ObservationRecord]:
    by_target: dict[str, list[str]] = {}
    for r in records:
        if r.supersedes:
            by_target.setdefault(r.supersedes, []).append(r.id)
    out: list[ObservationRecord] = []
    for r in records:
        succs = tuple(sorted(by_target.get(r.id, ())))
        out.append(replace(r, superseded_by=succs) if succs else r)
    return out


# ── Query API ────────────────────────────────────────────────────────────────


def list_observations(
    *,
    run_id: str | None = None,
    ref: str | None = None,
    status: str | None = None,
    project: str | None = None,
    unappraised_only: bool = False,
    catalog_dir: Path | str | None = None,
) -> list[ObservationRecord]:
    """List observations from the index (or legacy Parquet fragments), with optional filters."""
    cd = _resolve_catalog_dir(catalog_dir)
    by_id: dict[str, ObservationRecord] = {}

    con = connect_read(cd, wire_validity=False)
    try:
        cur = con.execute("SELECT * FROM observations")
        cols = [d[0] for d in cur.description]
        for tup in cur.fetchall():
            rec = _record_from_row(dict(zip(cols, tup, strict=True)))
            if rec.id:
                by_id[rec.id] = rec
    except duckdb.Error:
        pass
    finally:
        con.close()

    if not is_log_mode(cd):
        frag_obs, frag_statuses = _read_legacy_obs_fragments(cd)
        latest_status: dict[str, tuple[str, str, str]] = {}
        for st in frag_statuses:
            key = (st.ts, st.eid)
            prev = latest_status.get(st.obs_id)
            if prev is None or key > (prev[0], prev[1]):
                latest_status[st.obs_id] = (st.ts, st.eid, st.status)
        for rec in frag_obs:
            if rec.id not in by_id:
                if rec.id in latest_status:
                    rec = replace(rec, status=latest_status[rec.id][2])
                by_id[rec.id] = rec

    records = _attach_superseded_by(list(by_id.values()))
    if run_id:
        records = [r for r in records if r.run_id == run_id or r.run_id.startswith(run_id)]
    if ref:
        records = [r for r in records if r.ref == ref]
    if status:
        records = [r for r in records if r.status == status]
    if project:
        records = [r for r in records if r.project_slug == project]
    if unappraised_only:
        records = [r for r in records if r.unappraised]

    records.sort(key=lambda r: (r.created_at, r.id))
    return records


def get_observation(
    obs_id: str,
    *,
    catalog_dir: Path | str | None = None,
) -> ObservationRecord | None:
    """Get a single observation by exact id or unambiguous prefix, or None."""
    if not obs_id:
        return None
    all_obs = list_observations(catalog_dir=catalog_dir)
    for rec in all_obs:
        if rec.id == obs_id:
            return rec
    matches = [rec for rec in all_obs if rec.id.startswith(obs_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ObservationError(
            f"Observation id prefix {obs_id!r} is ambiguous: matches {[m.id for m in matches]}"
        )
    return None


def get_observation_status_history(
    obs_id: str,
    *,
    catalog_dir: Path | str | None = None,
) -> list[ObservationStatusRecord]:
    """Return chronological ``observation.status`` transitions for ``obs_id``."""
    cd = _resolve_catalog_dir(catalog_dir)
    by_eid: dict[str, ObservationStatusRecord] = {}

    con = connect_read(cd, wire_validity=False)
    try:
        cur = con.execute(
            "SELECT eid, obs_id, status, reason, actor, agent_session, ts "
            "FROM observation_statuses WHERE obs_id = ? ORDER BY ts, eid",
            [obs_id],
        )
        for eid, oid, st, reason, actor, sess, ts in cur.fetchall():
            by_eid[str(eid)] = ObservationStatusRecord(
                eid=str(eid),
                obs_id=str(oid),
                status=str(st),
                reason=str(reason or ""),
                actor=str(actor or ""),
                agent_session=str(sess or ""),
                ts=str(ts or ""),
            )
    except duckdb.Error:
        pass
    finally:
        con.close()

    if not is_log_mode(cd):
        _, frag_statuses = _read_legacy_obs_fragments(cd)
        for st in frag_statuses:
            if st.obs_id == obs_id and st.eid not in by_eid:
                by_eid[st.eid] = st

    history = list(by_eid.values())
    history.sort(key=lambda s: (s.ts, s.eid))
    return history


# ── Write API ────────────────────────────────────────────────────────────────


def create_observation(
    *,
    run_id: str = "",
    text: str = "",
    figure: str = "",
    tags: list[str] | tuple[str, ...] | str | None = None,
    ref: str = "",
    locator: str = "",
    claim: str = "",
    quote: str = "",
    value_provenance: str = "",
    quote_checked: bool = False,
    disposition: str = "",
    bears_on: str = "",
    uses_as: str = "",
    appraisal_ids: list[str] | tuple[str, ...] | str | None = None,
    supersedes: str = "",
    origin: str = "",
    agent_session: str = "",
    default_origin: str | None = None,
    project_root: Path | str | None = None,
    catalog_dir: Path | str | None = None,
) -> ObservationRecord:
    """Create and persist a run-backed or run-less observation (spec §3.3 & §3.3.1)."""
    cd = _resolve_catalog_dir(catalog_dir)
    ws_root = _resolve_workspace_root(project_root)
    project_slug = _resolve_project_slug(ws_root)

    run_id_clean = run_id.strip()
    ref_clean = ref.strip()

    if run_id_clean and ref_clean:
        raise ObservationValidationError(
            "Provide either 'run_id' (run-backed) or 'ref' (run-less), not both"
        )
    if not run_id_clean and not ref_clean:
        raise ObservationValidationError(
            "Provide either 'run_id' (run-backed) or '--ref' (run-less observation)"
        )

    resolved_origin, resolved_session = _detect_origin_and_session(
        origin=origin,
        agent_session=agent_session,
        default_origin=default_origin,
    )
    norm_tags = _normalize_str_list(tags)

    resolved_supersedes = ""
    if supersedes.strip():
        sup_target = get_observation(supersedes.strip(), catalog_dir=cd)
        if sup_target is None:
            raise ObservationNotFoundError(
                f"Cannot supersede unknown observation {supersedes!r}"
            )
        resolved_supersedes = sup_target.id

    resolved_run_id = ""
    resolved_figure = figure.strip()
    primary_sha = ""
    output_shas: dict[str, str] = {}
    resolved_text = text.strip()
    resolved_claim = claim.strip()
    resolved_locator = locator.strip()
    resolved_quote = quote.strip()
    resolved_prov = value_provenance.strip()
    resolved_disp = disposition.strip()
    resolved_bears_on = bears_on.strip()
    resolved_uses_as = uses_as.strip()
    norm_apprs = _normalize_str_list(appraisal_ids)
    unappraised = False

    if run_id_clean:
        if not resolved_text:
            raise ObservationValidationError("Run-backed observation requires non-empty 'text'")
        try:
            run = get_run(run_id_clean, cd, allow_cool_fallback=True)
        except AmbiguousRunId as exc:
            raise ObservationError(str(exc)) from exc
        if run is None:
            raise ObservationRunNotFoundError(
                f"Run not found: {run_id_clean!r} in catalog {cd}"
            )
        resolved_run_id = run.id
        if run.project_slug:
            project_slug = run.project_slug
        primary_sha, output_shas = _resolve_output_shas(run, resolved_figure, ws_root)
    else:
        # Run-less observation (§3.3.1)
        if not resolved_locator:
            raise ObservationValidationError(
                "Run-less observation (--ref) requires non-empty '--locator' (page, figure, or line)"
            )
        if not resolved_claim and not resolved_text:
            raise ObservationValidationError(
                "Run-less observation requires '--claim' or observation 'text'"
            )
        if not resolved_text:
            resolved_text = resolved_claim
        if not resolved_claim:
            resolved_claim = resolved_text

        if resolved_prov and resolved_prov not in VALUE_PROVENANCES:
            raise ObservationValidationError(
                f"value_provenance {resolved_prov!r} must be one of {sorted(VALUE_PROVENANCES)}"
            )
        if resolved_disp and resolved_disp not in LITERATURE_DISPOSITIONS:
            raise ObservationValidationError(
                f"disposition {resolved_disp!r} must be one of {sorted(LITERATURE_DISPOSITIONS)}"
            )
        if resolved_uses_as and resolved_uses_as not in USES_AS_VALUES:
            raise ObservationValidationError(
                f"uses_as {resolved_uses_as!r} must be one of {sorted(USES_AS_VALUES)}"
            )

        if not norm_apprs:
            if resolved_uses_as:
                raise ObservationValidationError(
                    f"uses_as={resolved_uses_as!r} requires at least one '--appraisal <id>'"
                )
            unappraised = True
        else:
            resolved_appr_ids: list[str] = []
            appr_records: list[AppraisalRecord] = []
            for aid in norm_apprs:
                rec = get_appraisal(aid, catalog_dir=cd)
                if rec is None:
                    raise ObservationValidationError(
                        f"Referenced appraisal {aid!r} does not exist in appraisal ledger"
                    )
                resolved_appr_ids.append(rec.id)
                appr_records.append(rec)
            norm_apprs = tuple(resolved_appr_ids)
            if resolved_uses_as:
                all_lookup = {a.id: a for a in list_appraisals(catalog_dir=cd)}
                tier_res = compute_use_tier(
                    appr_records,
                    value_provenance=resolved_prov,
                    citing_session=resolved_session or None,
                    lookup_appraisals=all_lookup,
                )
                validate_uses_as(resolved_uses_as, tier_res)
            unappraised = False

    obs_id = f"obs_{uuid.uuid4().hex[:12]}"
    created_at = _now_iso()

    record = ObservationRecord(
        id=obs_id,
        project_slug=project_slug,
        run_id=resolved_run_id,
        text=resolved_text,
        figure=resolved_figure,
        output_sha256=primary_sha,
        output_shas=output_shas,
        tags=norm_tags,
        supersedes=resolved_supersedes,
        status="open",
        origin=resolved_origin,
        agent_session=resolved_session,
        ref=ref_clean,
        locator=resolved_locator,
        claim=resolved_claim,
        quote=resolved_quote,
        value_provenance=resolved_prov,
        quote_checked=bool(quote_checked),
        disposition=resolved_disp,
        bears_on=resolved_bears_on,
        uses_as=resolved_uses_as,
        appraisal_ids=norm_apprs,
        unappraised=unappraised,
        created_at=created_at,
    )

    event_data: dict[str, Any] = {
        "id": record.id,
        "obs_id": record.id,
        "project_slug": record.project_slug,
        "run_id": record.run_id,
        "text": record.text,
        "figure": record.figure,
        "output_sha256": record.output_sha256,
        "output_shas": dict(record.output_shas),
        "tags": list(record.tags),
        "supersedes": record.supersedes,
        "status": record.status,
        "origin": record.origin,
        "agent_session": record.agent_session,
        "ref": record.ref,
        "locator": record.locator,
        "claim": record.claim,
        "quote": record.quote,
        "value_provenance": record.value_provenance,
        "quote_checked": record.quote_checked,
        "disposition": record.disposition,
        "bears_on": record.bears_on,
        "uses_as": record.uses_as,
        "appraisal_ids": list(record.appraisal_ids),
        "unappraised": record.unappraised,
        "created_at": record.created_at,
    }

    with unit_of_work(cd):
        outcome = emit_or_legacy(
            kind="observation.created",
            entity=[record.id],
            data=event_data,
            legacy_write=lambda: _write_obs_fragment(record, cd),
            cwd=ws_root,
        )
    if outcome is not None and not outcome.ok:
        raise ObservationError(
            f"observation.created could not be recorded: "
            f"project_error={outcome.project_error!r}, fallback_error={outcome.fallback_error!r}"
        )

    return record


def update_observation_status(
    obs_id: str,
    status: str,
    *,
    reason: str = "",
    actor: str = "",
    agent_session: str = "",
    project_root: Path | str | None = None,
    catalog_dir: Path | str | None = None,
) -> ObservationStatusRecord:
    """Append an ``observation.status`` event (``of = <obs_id>``) changing status (spec §3.3)."""
    cd = _resolve_catalog_dir(catalog_dir)
    ws_root = _resolve_workspace_root(project_root)

    status_clean = status.strip()
    if status_clean not in OBSERVATION_STATUSES:
        raise ObservationValidationError(
            f"Observation status {status_clean!r} must be one of {sorted(OBSERVATION_STATUSES)}"
        )

    target = get_observation(obs_id.strip(), catalog_dir=cd)
    if target is None:
        raise ObservationNotFoundError(f"Observation not found: {obs_id!r}")

    _, resolved_session = _detect_origin_and_session(agent_session=agent_session)
    resolved_actor = actor.strip() or os.environ.get("USER", "unknown")
    ts_now = _now_iso()
    legacy_eid = str(uuid7())

    status_rec = ObservationStatusRecord(
        eid=legacy_eid,
        obs_id=target.id,
        status=status_clean,
        reason=reason.strip(),
        actor=resolved_actor,
        agent_session=resolved_session,
        ts=ts_now,
    )

    event_data: dict[str, Any] = {
        "of": target.id,
        "status": status_clean,
        "reason": status_rec.reason,
        "actor": status_rec.actor,
        "agent_session": status_rec.agent_session,
    }

    with unit_of_work(cd):
        outcome = emit_or_legacy(
            kind="observation.status",
            entity=[target.id],
            data=event_data,
            legacy_write=lambda: _write_obs_status_fragment(status_rec, target.project_slug, cd),
            cwd=ws_root,
        )
    if outcome is not None and not outcome.ok:
        raise ObservationError(
            f"observation.status could not be recorded: "
            f"project_error={outcome.project_error!r}, fallback_error={outcome.fallback_error!r}"
        )

    return status_rec


__all__ = [
    "LITERATURE_DISPOSITIONS",
    "OBSERVATION_ORIGINS",
    "OBSERVATION_STATUSES",
    "USES_AS_VALUES",
    "VALUE_PROVENANCES",
    "ObservationError",
    "ObservationNotFoundError",
    "ObservationRecord",
    "ObservationRunNotFoundError",
    "ObservationStatusRecord",
    "ObservationValidationError",
    "create_observation",
    "get_observation",
    "get_observation_status_history",
    "list_observations",
    "update_observation_status",
]
