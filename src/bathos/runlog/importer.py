"""The legacy importer (spec "Migration" step 2, "Fold rules" / "Imported
history", the "Import kinds" table).

Reads every legacy source under a catalog directory -- warm `bathos.db` (via
`bathos.index.connect_legacy`, never a direct `duckdb.connect`, AC-18), cool
run fragments, the reap ledger (`reaped/<slug>/*.json` and its
`reverted/*.json`), submit-provenance Parquet, campaign JSON, the
trust-ledger/archived-item/blast-radius fragments, and the anchors table in
warm -- and appends `*.imported` events (`origin="migration"`) to a staging
log directory the caller supplies.

Scope note (delivery step 4, wave a): this module is the standalone importer
function only. It does NOT resolve project roots, attempt ids, or the
`import-staging/<attempt>/<root kind>/<root id>/` subtree layout Migration
step 2 describes -- that per-project orchestration (the `attempt` id, calling
this once per registered root, `bth migrate --to-log` itself) is wave c. This
wave's caller passes a plain `staging_dir` to write into directly, plus the
envelope's `project`/`project_id`/`main_root`/`worktree_root` fields (each
optional, defaulting to values derived from `catalog_dir` when omitted) --
wave c is expected to supply the real per-root values when it wires this up.

It NEVER reads current sidecar or output files (spec: "The importer never
reads current sidecar or output files") -- only the legacy catalog's own
stored records.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pyarrow.parquet as pq

from bathos.index import connect_legacy
from bathos.runlog.envelope import NAMESPACE_BATHOS, build_envelope
from bathos.runlog.fold_runs import TERMINAL_STATUSES

CANON_VERSION = 1

# spec "Imported history": precedence order for the warm database itself --
# `bathos.db.frozen` (or `bathos.db` while no frozen copy exists) is class
# `warm`; a `bathos.db` recreated alongside an existing `.frozen` copy (an
# older install writing after cut-over) is `warm_recreated`.
_WARM_FROZEN_NAME = "bathos.db.frozen"
_WARM_PLAIN_NAME = "bathos.db"


@dataclass(frozen=True)
class ImportCandidate:
    """One legacy entity as read from one concrete source, ready to become
    one `*.imported` event (spec: "the legacy importer emits one
    `<kind>.imported` event per (entity, legacy source)")."""

    kind: str
    entity: list[str]
    fields: dict[str, Any]
    ts: str
    source_class: str
    source_locator: str


@dataclass
class ImportReport:
    """What one `import_legacy_catalog` call did."""

    appended: int = 0
    unchanged: int = 0
    unreadable: list[str] = field(default_factory=list)
    locked: list[str] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)


# --- canon / hashing / eid -----------------------------------------------


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"not JSON-serializable in a legacy import record: {type(value)!r}")


def _canon_bytes(fields: dict[str, Any]) -> bytes:
    """spec `data.canon: 1`: "a JSON object with sorted keys, no whitespace,
    UTF-8, timestamps as RFC3339 UTC and floats in shortest round-trip
    form." Python's `json` module already renders a `float` via `repr()`,
    which has been shortest-round-trip since Python 3.1; a `datetime` is
    rendered via `isoformat()` (RFC3339-compatible) by `_json_default`.
    """
    return json.dumps(
        fields, sort_keys=True, separators=(",", ":"), default=_json_default, ensure_ascii=False
    ).encode("utf-8")


def _source_sha256(fields: dict[str, Any]) -> str:
    return hashlib.sha256(_canon_bytes(fields)).hexdigest()


def _import_eid(
    kind: str,
    entity: list[str],
    source_class: str,
    source_locator: str,
    snapshot: int,
    source_sha256: str,
) -> str:
    """spec: `eid = uuid5(NAMESPACE_BATHOS,
    f"import:{kind}:{entity}:{source_class}:{source_locator}:{snapshot}:{source_sha256}")`.

    Spec-ambiguity call: `{entity}` for a multi-part entity key (e.g.
    `campaign_run.imported`'s `(campaign_id, run_id)`, `edge.imported`'s
    `(src, dst, type)`) is rendered here as its parts joined by `:` -- the
    spec gives the formula for a single string, and this is the most direct
    reading that stays deterministic and collision-resistant against any one
    part containing a `:` (`source_locator`/`source_class` are drawn from a
    small fixed vocabulary and a well-formed relative path, so a genuine
    collision would require an adversarial entity id).
    """
    entity_str = ":".join(entity)
    name = f"import:{kind}:{entity_str}:{source_class}:{source_locator}:{snapshot}:{source_sha256}"
    return str(uuid.uuid5(NAMESPACE_BATHOS, name))


def _jsonify_row(row: dict[str, Any]) -> dict[str, Any]:
    """A DuckDB or PyArrow row, as a plain dict of JSON-safe values (only
    `datetime` needs converting -- lists/bools/ints/floats/strings/None are
    already JSON-safe)."""
    out: dict[str, Any] = {}
    for k, v in row.items():
        out[k] = v.isoformat() if isinstance(v, datetime) else v
    return out


def _run_ts(row: dict[str, Any]) -> str:
    """spec: "run: end time if finished, else start" -- "end time" is
    `timestamp + duration_s` (spec "Run status": "there being no end-time
    column")."""
    ts_start = row.get("timestamp")
    status = row.get("status")
    if status in TERMINAL_STATUSES and isinstance(ts_start, datetime):
        duration = row.get("duration_s") or 0.0
        try:
            end = ts_start + timedelta(seconds=float(duration))
        except (TypeError, ValueError):
            end = ts_start
        return end.isoformat()
    if isinstance(ts_start, datetime):
        return ts_start.isoformat()
    return ts_start or ""


# --- warm source resolution ----------------------------------------------


def _warm_sources(catalog_dir: Path) -> list[tuple[Path, str, str]]:
    """`[(path, source_class, locator_prefix), ...]` per the warm/
    warm_recreated rule (spec "Imported history"): `bathos.db.frozen` (or
    `bathos.db` while no frozen copy exists) is class `warm`, locator prefix
    `warm`; a `bathos.db` found ALONGSIDE an existing `.frozen` copy is class
    `warm_recreated`, locator prefix `bathos.db` (so the step-4 rename
    changes nothing about a pre-cutover `warm` locator, and a post-cutover
    stray `bathos.db` is clearly distinguished)."""
    frozen = catalog_dir / _WARM_FROZEN_NAME
    plain = catalog_dir / _WARM_PLAIN_NAME
    sources: list[tuple[Path, str, str]] = []
    if frozen.exists():
        sources.append((frozen, "warm", "warm"))
        if plain.exists():
            sources.append((plain, "warm_recreated", "bathos.db"))
    elif plain.exists():
        sources.append((plain, "warm", "warm"))
    return sources


def _read_table_rows(con: duckdb.DuckDBPyConnection, table: str) -> list[dict[str, Any]]:
    """`SELECT *` from one warm table, self-adapting to whatever columns
    this particular installation's schema actually has (older warm catalogs
    may predate a later `ALTER TABLE ADD COLUMN`) -- an absent table (this
    catalog never created it) is simply no rows, not an error."""
    try:
        cur = con.execute(f"SELECT * FROM {table}")  # noqa: S608 -- table is an internal constant
    except duckdb.CatalogException:
        return []
    cols = [d[0] for d in cur.description]
    return [_jsonify_row(dict(zip(cols, r, strict=True))) for r in cur.fetchall()]


# --- per-entity candidate builders: warm ----------------------------------


def _warm_run_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    locator = f"{locator_prefix}:runs"
    out = []
    for row in rows:
        run_id = row.get("id")
        if not run_id:
            continue
        out.append(
            ImportCandidate("run.imported", [run_id], row, _run_ts(row), source_class, locator)
        )
    return out


def _warm_campaign_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    locator = f"{locator_prefix}:campaigns"
    out = []
    for row in rows:
        campaign_id = row.get("id")
        if not campaign_id:
            continue
        ts = row.get("concluded_at") or row.get("started_at") or ""
        out.append(
            ImportCandidate("campaign.imported", [campaign_id], row, ts, source_class, locator)
        )
    return out


def _warm_campaign_run_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    """Only `evalue` is carried (spec: "with stored `evalue`" -- "not
    `seq_position`, which the fold always recomputes"). `campaign_runs` has
    no timestamp column of its own; spec-ambiguity call: use the empty
    string (ties then resolve by `eid`, which is deterministic) rather than
    inventing a timestamp this source does not have.
    """
    locator = f"{locator_prefix}:campaign_runs"
    out = []
    for row in rows:
        campaign_id, run_id = row.get("campaign_id"), row.get("run_id")
        if not campaign_id or not run_id:
            continue
        fields = {"evalue": row.get("evalue")}
        out.append(
            ImportCandidate(
                "campaign_run.imported", [campaign_id, run_id], fields, "", source_class, locator
            )
        )
    return out


def _warm_edge_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str, *, table: str, etype: str
) -> list[ImportCandidate]:
    locator = f"{locator_prefix}:{table}"
    child_col, parent_col = (
        ("child_campaign_id", "parent_campaign_id")
        if etype == "campaign"
        else ("child_run_id", "parent_run_id")
    )
    out = []
    for row in rows:
        src, dst = row.get(child_col), row.get(parent_col)
        if not src or not dst:
            continue
        fields = {"src": src, "dst": dst, "type": etype}
        out.append(
            ImportCandidate("edge.imported", [src, dst, etype], fields, "", source_class, locator)
        )
    return out


def _warm_anchor_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    from bathos.anchor import _anchor_entity_id

    locator = f"{locator_prefix}:sidecar_anchors"
    out = []
    for row in rows:
        path, sha256 = row.get("path"), row.get("sha256")
        if not path or not sha256:
            continue
        anchor_id = _anchor_entity_id(path, sha256)
        ts = row.get("anchored_at") or ""
        out.append(ImportCandidate("anchor.imported", [anchor_id], row, ts, source_class, locator))
    return out


def _warm_blast_radius_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    locator = f"{locator_prefix}:blast_radius_ledger"
    out = []
    for row in rows:
        record_id = row.get("id")
        if not record_id:
            continue
        ts = row.get("amended_at") or ""
        out.append(
            ImportCandidate("blast_radius.imported", [record_id], row, ts, source_class, locator)
        )
    return out


def _warm_trust_ledger_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    locator = f"{locator_prefix}:trust_ledger"
    out = []
    for row in rows:
        record_id = row.get("id")
        if not record_id:
            continue
        ts = row.get("amended_at") or ""
        out.append(
            ImportCandidate("trust_ledger.imported", [record_id], row, ts, source_class, locator)
        )
    return out


def _warm_archived_item_candidates(
    rows: list[dict[str, Any]], source_class: str, locator_prefix: str
) -> list[ImportCandidate]:
    locator = f"{locator_prefix}:archived_items"
    out = []
    for row in rows:
        record_id = row.get("record_id")
        if not record_id:
            continue
        ts = row.get("recorded_at") or ""
        out.append(
            ImportCandidate("archived_item.imported", [record_id], row, ts, source_class, locator)
        )
    return out


# --- per-entity candidate builders: cool fragments / JSON / Parquet ------


def _iter_parquet_rows(path: Path) -> list[dict[str, Any]] | None:
    """Every row of one Parquet fragment (there is always exactly one row
    per bathos fragment file), or `None` if the file cannot be read at
    all."""
    try:
        table = pq.read_table(path)
    except Exception:  # noqa: BLE001 -- any read failure means "unreadable", regardless of cause
        return None
    pydict = table.to_pydict()
    return [_jsonify_row({k: v[i] for k, v in pydict.items()}) for i in range(table.num_rows)]


def _emit_unreadable(report: ImportReport, source_locator: str, path: Path) -> ImportCandidate:
    """spec: "Unreadable sources ... have no record to import. The importer
    records each in `import_manifest` as a chain with `source_class=corrupt`
    and the sha256 of the file's bytes, via a `legacy_source.unreadable`
    event (entity: `source_locator`); it takes no part in any fold."""
    report.unreadable.append(source_locator)
    try:
        raw = path.read_bytes()
        byte_sha256 = hashlib.sha256(raw).hexdigest()
    except OSError:
        byte_sha256 = ""
    return ImportCandidate(
        kind="legacy_source.unreadable",
        entity=[source_locator],
        fields={"reason": "unreadable", "byte_sha256": byte_sha256},
        ts="",
        source_class="corrupt",
        source_locator=source_locator,
    )


def _cool_run_fragment_candidates(catalog_dir: Path, report: ImportReport) -> list[ImportCandidate]:
    runs_dir = catalog_dir / "runs"
    out: list[ImportCandidate] = []
    if not runs_dir.is_dir():
        return out
    for f in sorted(runs_dir.rglob("run_*.parquet")):
        if f.name.endswith(".tmp.parquet"):
            continue
        locator = str(f.relative_to(catalog_dir))
        rows = _iter_parquet_rows(f)
        if rows is None:
            out.append(_emit_unreadable(report, locator, f))
            continue
        for row in rows:
            run_id = row.get("id")
            if not run_id:
                continue
            out.append(
                ImportCandidate("run.imported", [run_id], row, _run_ts(row), "fragment", locator)
            )
    return out


def _remote_run_fragment_candidates(
    catalog_dir: Path, report: ImportReport
) -> list[ImportCandidate]:
    """`remote-runs/<root id>/<remote>/` (Migration step 1's mirror, source
    class `fragment_remote`) -- populated by the cluster pull (wave c, not
    built yet), read defensively here so the importer already recognizes it
    once that mirror exists on disk, matching every other cool-fragment
    reader's own tree shape."""
    remote_dir = catalog_dir / "remote-runs"
    out: list[ImportCandidate] = []
    if not remote_dir.is_dir():
        return out
    for f in sorted(remote_dir.rglob("run_*.parquet")):
        if f.name.endswith(".tmp.parquet"):
            continue
        locator = str(f.relative_to(catalog_dir))
        rows = _iter_parquet_rows(f)
        if rows is None:
            out.append(_emit_unreadable(report, locator, f))
            continue
        for row in rows:
            run_id = row.get("id")
            if not run_id:
                continue
            out.append(
                ImportCandidate(
                    "run.imported", [run_id], row, _run_ts(row), "fragment_remote", locator
                )
            )
    return out


_REVERTED_FILENAME_RE = re.compile(r"^(?P<run_id>.+)\.(?P<ts>\d{14})$")


def _parse_reverted_filename_ts(stem: str) -> str | None:
    m = _REVERTED_FILENAME_RE.match(stem)
    if not m:
        return None
    try:
        dt = datetime.strptime(m.group("ts"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return dt.isoformat()


def _reap_ledger_candidates(catalog_dir: Path, report: ImportReport) -> list[ImportCandidate]:
    reaped_dir = catalog_dir / "reaped"
    out: list[ImportCandidate] = []
    if not reaped_dir.is_dir():
        return out
    for project_dir in sorted(reaped_dir.iterdir()):
        if not project_dir.is_dir() or project_dir.name == "reverted":
            continue
        for f in sorted(project_dir.glob("*.json")):
            if f.parent.name == "reverted":
                continue
            locator = str(f.relative_to(catalog_dir))
            try:
                record = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                out.append(_emit_unreadable(report, locator, f))
                continue
            # Valid JSON that is not a ledger object ([], null, a string) is
            # unreadable too -- it must never abort the whole import.
            if not isinstance(record, dict):
                out.append(_emit_unreadable(report, locator, f))
                continue
            run_id = record.get("run_id") or f.stem
            fields = dict(record)
            # spec "Reap ledgers": "the fold treats it as an abandoned claim"
            # -- `fold_runs._expand_claims` classifies a `run.imported` by
            # `data.status`, so the importer marks it "abandoned" the same
            # way a live `run.reaped` already carries that status implicitly
            # via its own dedicated kind.
            fields["status"] = "abandoned"
            ts = str(record.get("reaped_at") or "")
            out.append(
                ImportCandidate("run.imported", [run_id], fields, ts, "ledger_json", locator)
            )

        reverted_dir = project_dir / "reverted"
        if not reverted_dir.is_dir():
            continue
        for f in sorted(reverted_dir.glob("*.json")):
            locator = str(f.relative_to(catalog_dir))
            try:
                record = json.loads(f.read_text())
            except (OSError, json.JSONDecodeError):
                out.append(_emit_unreadable(report, locator, f))
                continue
            # Valid JSON that is not a ledger object ([], null, a string) is
            # unreadable too -- it must never abort the whole import.
            if not isinstance(record, dict):
                out.append(_emit_unreadable(report, locator, f))
                continue
            run_id = record.get("run_id") or ""
            reaped_at = str(record.get("reaped_at") or "")
            filename_ts = _parse_reverted_filename_ts(f.stem)
            reverted_at = max(reaped_at, filename_ts) if filename_ts else reaped_at
            fields = dict(record)
            fields["reverted_at"] = reverted_at
            out.append(
                ImportCandidate(
                    "run_reap.imported", [run_id], fields, reaped_at, "ledger_reverted", locator
                )
            )
    return out


def _submit_candidates(catalog_dir: Path, report: ImportReport) -> list[ImportCandidate]:
    """A submit-provenance Parquet record has no stable id of its own (the
    legacy Parquet schema, `catalog.write_submit_provenance`'s
    `submit_schema`, carries no `submit_id` column at all -- that field is a
    fresh uuid4 the live write path mints only for its OWN event, never
    persisted to the fragment). Spec-ambiguity call: mint the entity id
    deterministically from the file's own path (`uuid5(NAMESPACE_BATHOS,
    f"submit:{locator}")`) -- this is path-addressed rather than
    content-addressed, but the file IS the entity (one Parquet record per
    file, one file per submission), so path and entity coincide exactly and
    re-running the importer over an unchanged file yields the same id.
    """
    submits_dir = catalog_dir / "submits"
    out: list[ImportCandidate] = []
    if not submits_dir.is_dir():
        return out
    for f in sorted(submits_dir.rglob("*_submit.parquet")):
        if f.name.endswith(".tmp"):
            continue
        locator = str(f.relative_to(catalog_dir))
        rows = _iter_parquet_rows(f)
        if rows is None:
            out.append(_emit_unreadable(report, locator, f))
            continue
        for row in rows:
            submitted_at = row.get("submitted_at") or ""
            submit_id = str(uuid.uuid5(NAMESPACE_BATHOS, f"submit:{locator}"))
            out.append(
                ImportCandidate(
                    "submit.imported", [submit_id], row, submitted_at, "submit_parquet", locator
                )
            )
    return out


def _campaign_json_candidates(catalog_dir: Path, report: ImportReport) -> list[ImportCandidate]:
    campaigns_dir = catalog_dir / "campaigns"
    out: list[ImportCandidate] = []
    if not campaigns_dir.is_dir():
        return out
    for f in sorted(campaigns_dir.glob("*.json")):
        locator = str(f.relative_to(catalog_dir))
        try:
            payload = json.loads(f.read_text())
        except (OSError, json.JSONDecodeError):
            out.append(_emit_unreadable(report, locator, f))
            continue
        if not isinstance(payload, dict) or "id" not in payload:
            # Structurally invalid but parseable JSON -- legacy
            # `read_cool_campaigns` skips this with a warning, not an error;
            # matching that (not `legacy_source.unreadable`, which is for a
            # file that "cannot be parsed" at all).
            continue
        campaign_id = payload["id"]
        ts = payload.get("concluded_at") or payload.get("started_at") or ""
        out.append(
            ImportCandidate(
                "campaign.imported", [campaign_id], payload, ts, "campaign_json", locator
            )
        )
    return out


def _ledger_fragment_candidates(
    catalog_dir: Path,
    report: ImportReport,
    *,
    dirname: str,
    glob: str,
    kind: str,
    id_field: str,
    ts_field: str,
) -> list[ImportCandidate]:
    frag_dir = catalog_dir / dirname
    out: list[ImportCandidate] = []
    if not frag_dir.is_dir():
        return out
    for f in sorted(frag_dir.glob(glob)):
        if f.name.endswith(".tmp.parquet"):
            continue
        locator = str(f.relative_to(catalog_dir))
        rows = _iter_parquet_rows(f)
        if rows is None:
            out.append(_emit_unreadable(report, locator, f))
            continue
        for row in rows:
            record_id = row.get(id_field)
            if not record_id:
                continue
            ts = row.get(ts_field) or ""
            out.append(ImportCandidate(kind, [record_id], row, ts, "fragment", locator))
    return out


def _trust_ledger_fragment_candidates(
    catalog_dir: Path, report: ImportReport
) -> list[ImportCandidate]:
    return _ledger_fragment_candidates(
        catalog_dir,
        report,
        dirname="ledger",
        glob="ledger_*.parquet",
        kind="trust_ledger.imported",
        id_field="id",
        ts_field="amended_at",
    )


def _archived_item_fragment_candidates(
    catalog_dir: Path, report: ImportReport
) -> list[ImportCandidate]:
    return _ledger_fragment_candidates(
        catalog_dir,
        report,
        dirname="archived_items",
        glob="archived_*.parquet",
        kind="archived_item.imported",
        id_field="record_id",
        ts_field="recorded_at",
    )


def _blast_radius_fragment_candidates(
    catalog_dir: Path, report: ImportReport
) -> list[ImportCandidate]:
    return _ledger_fragment_candidates(
        catalog_dir,
        report,
        dirname="blast_radius",
        glob="blast_radius_*.parquet",
        kind="blast_radius.imported",
        id_field="id",
        ts_field="amended_at",
    )


# --- warm dispatch ----------------------------------------------------


def _warm_candidates(catalog_dir: Path, report: ImportReport) -> list[ImportCandidate]:
    out: list[ImportCandidate] = []
    for path, source_class, locator_prefix in _warm_sources(catalog_dir):
        opened = connect_legacy(path)
        if isinstance(opened, dict):
            status = opened.get("status")
            if status == "legacy_db_locked":
                report.locked.append(str(path))
                continue
            if status == "corrupt_legacy_source":
                out.append(_emit_unreadable(report, f"{locator_prefix}", path))
                continue
            continue  # pragma: no cover -- future connect_legacy status values
        con = opened
        try:
            out.extend(
                _warm_run_candidates(_read_table_rows(con, "runs"), source_class, locator_prefix)
            )
            out.extend(
                _warm_campaign_candidates(
                    _read_table_rows(con, "campaigns"), source_class, locator_prefix
                )
            )
            out.extend(
                _warm_campaign_run_candidates(
                    _read_table_rows(con, "campaign_runs"), source_class, locator_prefix
                )
            )
            out.extend(
                _warm_edge_candidates(
                    _read_table_rows(con, "campaign_edges"),
                    source_class,
                    locator_prefix,
                    table="campaign_edges",
                    etype="campaign",
                )
            )
            out.extend(
                _warm_edge_candidates(
                    _read_table_rows(con, "run_edges"),
                    source_class,
                    locator_prefix,
                    table="run_edges",
                    etype="run",
                )
            )
            out.extend(
                _warm_anchor_candidates(
                    _read_table_rows(con, "sidecar_anchors"), source_class, locator_prefix
                )
            )
            out.extend(
                _warm_blast_radius_candidates(
                    _read_table_rows(con, "blast_radius_ledger"), source_class, locator_prefix
                )
            )
            out.extend(
                _warm_trust_ledger_candidates(
                    _read_table_rows(con, "trust_ledger"), source_class, locator_prefix
                )
            )
            out.extend(
                _warm_archived_item_candidates(
                    _read_table_rows(con, "archived_items"), source_class, locator_prefix
                )
            )
        finally:
            con.close()
    return out


def _all_candidates(catalog_dir: Path, report: ImportReport) -> list[ImportCandidate]:
    out: list[ImportCandidate] = []
    out.extend(_warm_candidates(catalog_dir, report))
    out.extend(_cool_run_fragment_candidates(catalog_dir, report))
    out.extend(_remote_run_fragment_candidates(catalog_dir, report))
    out.extend(_reap_ledger_candidates(catalog_dir, report))
    out.extend(_submit_candidates(catalog_dir, report))
    out.extend(_campaign_json_candidates(catalog_dir, report))
    out.extend(_trust_ledger_fragment_candidates(catalog_dir, report))
    out.extend(_archived_item_fragment_candidates(catalog_dir, report))
    out.extend(_blast_radius_fragment_candidates(catalog_dir, report))
    return out


# --- staging: existing snapshot chains + append ---------------------------


def _read_staging_events(staging_dir: Path) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    if not staging_dir.is_dir():
        return events
    for f in sorted(staging_dir.glob("*.jsonl")):
        with open(f, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    return events


def _existing_chains(
    staging_events: list[dict[str, Any]],
) -> dict[tuple[str, ...], tuple[int, str]]:
    """`(kind, *entity, source_class, source_locator) -> (max ordinal,
    source_sha256 at that ordinal)` -- spec "Snapshots": "'Existing' means,
    before cut-over, the current staging attempt only"."""
    chains: dict[tuple[str, ...], tuple[int, str]] = {}
    for ev in staging_events:
        if ev.get("origin") != "migration":
            continue
        data = ev.get("data") or {}
        key = (
            ev.get("kind", ""),
            *(ev.get("entity") or []),
            data.get("source_class", ""),
            data.get("source_locator", ""),
        )
        ordinal = data.get("snapshot") or 0
        sha = data.get("source_sha256", "")
        current = chains.get(key)
        if current is None or ordinal > current[0]:
            chains[key] = (ordinal, sha)
    return chains


def import_legacy_catalog(
    catalog_dir: Path,
    staging_dir: Path,
    *,
    project: str | None = None,
    project_id: str | None = None,
    main_root: Path | None = None,
    worktree_root: Path | None = None,
) -> ImportReport:
    """Read every legacy source under `catalog_dir` and append `*.imported`
    events (`origin="migration"`) to `staging_dir` for anything new or
    changed since the last run (spec "Snapshots"): re-running with unchanged
    sources appends nothing; a changed source appends a new snapshot at the
    next ordinal in its chain, never overwriting or removing the old one.

    `project`/`project_id`/`main_root`/`worktree_root` are stamped onto every
    emitted envelope as given (default: `None`/`None`/`catalog_dir`/
    `catalog_dir` -- wave c's per-root migration orchestration is expected to
    pass the real values for the root this catalog belongs to).
    """
    report = ImportReport()
    candidates = _all_candidates(catalog_dir, report)
    existing = _existing_chains(_read_staging_events(staging_dir))

    root = main_root or catalog_dir
    wroot = worktree_root or catalog_dir

    new_lines: list[str] = []
    for cand in candidates:
        if cand.kind == "legacy_source.unreadable":
            # Never part of a snapshot chain/fold (spec: "it takes no part
            # in any fold") -- still de-duplicated against staging so a
            # repeat import of the same unreadable file doesn't re-append
            # (compared by the file's own byte sha256, carried in `fields`).
            chain_key = (cand.kind, *cand.entity, cand.source_class, cand.source_locator)
            byte_sha = cand.fields.get("byte_sha256", "")
            prior = existing.get(chain_key)
            if prior is not None and prior[1] == byte_sha:
                report.unchanged += 1
                continue
            ordinal = 0 if prior is None else prior[0] + 1
            data = dict(cand.fields)
            data.update(
                {
                    "source_class": cand.source_class,
                    "source_locator": cand.source_locator,
                    "canon": CANON_VERSION,
                    "snapshot": ordinal,
                    "source_sha256": byte_sha,
                }
            )
            eid = _import_eid(
                cand.kind, cand.entity, cand.source_class, cand.source_locator, ordinal, byte_sha
            )
            env = build_envelope(
                kind=cand.kind,
                entity=cand.entity,
                data=data,
                main_root=root,
                worktree_root=wroot,
                project=project,
                project_id=project_id,
                writer="importer",
                seq=len(new_lines) + 1,
                origin="migration",
                ts=cand.ts,
                eid=eid,
            )
            new_lines.append(json.dumps(env, sort_keys=True))
            report.appended += 1
            report.events.append(env)
            existing[chain_key] = (ordinal, byte_sha)
            continue

        sha = _source_sha256(cand.fields)
        chain_key = (cand.kind, *cand.entity, cand.source_class, cand.source_locator)
        prior = existing.get(chain_key)
        if prior is not None and prior[1] == sha:
            report.unchanged += 1
            continue
        ordinal = 0 if prior is None else prior[0] + 1

        data = dict(cand.fields)
        data.update(
            {
                "source_class": cand.source_class,
                "source_locator": cand.source_locator,
                "canon": CANON_VERSION,
                "snapshot": ordinal,
                "source_sha256": sha,
            }
        )
        eid = _import_eid(
            cand.kind, cand.entity, cand.source_class, cand.source_locator, ordinal, sha
        )
        env = build_envelope(
            kind=cand.kind,
            entity=cand.entity,
            data=data,
            main_root=root,
            worktree_root=wroot,
            project=project,
            project_id=project_id,
            writer="importer",
            seq=len(new_lines) + 1,
            origin="migration",
            ts=cand.ts,
            eid=eid,
        )
        new_lines.append(json.dumps(env, sort_keys=True))
        report.appended += 1
        report.events.append(env)
        existing[chain_key] = (ordinal, sha)

    if new_lines:
        staging_dir.mkdir(parents=True, exist_ok=True)
        seg_path = staging_dir / f"import_{uuid.uuid4().hex[:12]}.jsonl"
        tmp_path = seg_path.with_suffix(".jsonl.tmp")
        with open(tmp_path, "w", encoding="utf-8") as fh:
            for line in new_lines:
                fh.write(line + "\n")
        tmp_path.replace(seg_path)

    return report


__all__ = [
    "CANON_VERSION",
    "ImportCandidate",
    "ImportReport",
    "import_legacy_catalog",
]
