"""Shared stage-1/stage-2 merge primitives for the legacy importer's
`*.imported` events (spec "Fold rules": "An entity folds in two stages: (1)
its `*.imported` events merge into a base state by the import merge rule
below; (2) its live events apply on top").

Every fold module that has to reconcile imported history with live events
(`fold_runs`, `fold_campaigns`, `fold_edges`, `fold_anchors`, `fold_ledgers`)
uses these same primitives, so the source-class precedence chain and the
"empty" definition are defined in exactly one place rather than re-derived
per module.
"""

from __future__ import annotations

from typing import Any

# spec "Fold rules" / "Imported history": "precedence is that order" --
# highest precedence first. An event's `data.source_class` is one of these
# literal strings (set by the importer, `bathos.runlog.importer`).
SOURCE_CLASS_PRECEDENCE: dict[str, int] = {
    "warm": 0,
    "warm_recreated": 1,
    "fragment": 2,
    "fragment_remote": 3,
    "campaign_json": 4,
    "ledger_json": 5,
    "ledger_reverted": 6,
    "submit_parquet": 7,
}

# An event with no/unknown `source_class` sorts last (lowest precedence)
# rather than raising -- a malformed or future source class degrades
# gracefully instead of poisoning the whole merge.
_UNKNOWN_SOURCE_CLASS_RANK = len(SOURCE_CLASS_PRECEDENCE)


def source_class_rank(ev: dict) -> int:
    """Ascending precedence rank of an imported event's `data.source_class`
    (0 = highest precedence, i.e. `warm`)."""
    sc = (ev.get("data") or {}).get("source_class")
    return SOURCE_CLASS_PRECEDENCE.get(sc, _UNKNOWN_SOURCE_CLASS_RANK)


def is_empty(value: Any) -> bool:
    """Spec: "empty means absent, SQL NULL, `""`, `[]` or `{}`; `0`, `0.0`
    and `false` are values." (`False == 0` in Python, so this must compare
    against `None`/`""`/`[]`/`{}` by value, never treat `False` as one of
    them via a truthiness check.)
    """
    return value is None or value == "" or value == [] or value == {}


def import_precedence_key(ev: dict) -> tuple[int, str, str]:
    """(source_class precedence rank ascending, ts, eid) -- "the first
    non-empty value in order of source precedence, then ts, then eid"."""
    return (source_class_rank(ev), ev.get("ts", ""), ev.get("eid", ""))


def dedup_import_snapshots(events: list[dict]) -> list[dict]:
    """Spec "Snapshots": "a snapshot chain is one `(kind, entity, source_class,
    source_locator)`: one concrete source record ... Only the highest
    ordinal of each chain takes part in the fold; every chain does." Live
    events pass through untouched; every `*.imported` event is grouped by
    its full chain key and only the one with the highest `data.snapshot`
    ordinal survives -- an earlier snapshot of the SAME concrete source is
    dropped from the merge (though it remains in the events table for
    audit), while a DIFFERENT chain (a different source_class/source_locator,
    or a different entity that happens to share a source_class) is
    untouched and still participates.

    This must run before any stage-1 merge or status-claim ranking sees the
    imported events -- called at the top of every fold function that
    receives a raw per-entity event list.
    """
    live = [ev for ev in events if ev.get("origin") != "migration"]
    imported = [ev for ev in events if ev.get("origin") == "migration"]
    grouped: dict[tuple[str, tuple[str, ...], str | None, str | None], dict] = {}
    for ev in imported:
        data = ev.get("data") or {}
        key = (
            ev.get("kind"),
            tuple(ev.get("entity") or []),
            data.get("source_class"),
            data.get("source_locator"),
        )
        ordinal = data.get("snapshot") or 0
        current = grouped.get(key)
        current_ordinal = ((current.get("data") or {}).get("snapshot") or 0) if current else None
        if current is None or ordinal > current_ordinal:
            grouped[key] = ev
    return live + list(grouped.values())


def stage1_field_merge(imported_events: list[dict], fields: tuple[str, ...]) -> dict[str, Any]:
    """Stage 1 of the two-stage fold: merge one entity's `*.imported` events
    into a base dict, field by field, keeping the first NON-EMPTY value in
    `import_precedence_key` order (spec "Import merge (stage 1)": "keeps the
    first non-empty value in order of source precedence, then ts, then
    eid"). Never whole-state replacement -- a lower-precedence source only
    ever fills a field a higher-precedence source left empty, and a field no
    import carries at all is simply absent from the result (left for the
    caller's own defaulting).
    """
    ordered = sorted(imported_events, key=import_precedence_key)
    row: dict[str, Any] = {}
    for field in fields:
        for ev in ordered:
            data = ev.get("data") or {}
            if field in data and not is_empty(data[field]):
                row[field] = data[field]
                break
    return row


def latest_whole_event(live_events: list[dict], imported_events: list[dict]) -> dict | None:
    """For entities folded as one atomic snapshot rather than field-by-field
    (edges, anchors, the simple append-only ledgers, submit provenance):
    stage 2's "live events apply on top" degrades to "the latest live event
    wins outright over any import" -- live is always the highest source
    precedence (spec "Ties go by source precedence (live counts as
    highest)"). With no live event, the highest-precedence import wins,
    tie-broken by latest `(ts, eid)`. Returns the winning event itself (not
    just its `data`, since a caller may also need its own `ts`/`entity`), or
    `None` if there is no event at all for this entity.
    """
    if live_events:
        return max(live_events, key=lambda e: (e.get("ts", ""), e.get("eid", "")))
    if imported_events:
        return max(
            imported_events,
            key=lambda e: (-source_class_rank(e), e.get("ts", ""), e.get("eid", "")),
        )
    return None


def latest_whole_row(live_events: list[dict], imported_events: list[dict]) -> dict[str, Any] | None:
    """`latest_whole_event`, returning just the winning event's `data` (or
    `None` if there is no event at all for this entity)."""
    ev = latest_whole_event(live_events, imported_events)
    return None if ev is None else (ev.get("data") or {})


__all__ = [
    "SOURCE_CLASS_PRECEDENCE",
    "dedup_import_snapshots",
    "import_precedence_key",
    "is_empty",
    "latest_whole_event",
    "latest_whole_row",
    "source_class_rank",
    "stage1_field_merge",
]
