"""AC-17: differential fold test.

Runs the SAME randomized operation sequence through the real bathos write
APIs twice -- flag off (legacy: cool fragments + warm `bathos.db`) and flag
on (events + the new index fold) -- against two isolated fixtures, then
compares the resulting `runs`, `campaigns`, `campaign_runs`,
`sidecar_anchors`, `campaign_edges` and `run_edges` rows.

**Determinism strategy (documented per the task brief -- "choose one and
document it"): a hybrid.**

- Run ids, event timestamps, sidecar file content, and anchor `anchored_at`
  are INJECTED: literal, identical values are constructed once by
  `ac17_gen.generate_ops()` and handed to BOTH backends, since we bypass
  `bth run`'s subprocess spawn and build `Run` rows / call the lifecycle
  write-sites directly (`ac17_harness.execute_ops`, `_start_run`/
  `_finish_run`) -- this is the "lower-level APIs that emit the SAME events
  and write the SAME legacy fragments" alternative the task brief allows,
  since `run_script` itself launches a real subprocess plus git/manifest/gate
  machinery that is orthogonal to what AC-17 tests (fold correctness) and
  would make many-seed randomized coverage far too slow. Concretely this
  means: `bathos.schema.Run(...)` is constructed directly (not via
  `runner.run_script`), `bathos.catalog.write_run()` is the legacy write (the
  SAME function `runner.py` calls), and `bathos.runlog.emit.emit_event()`
  with `run_event_data()` is the new-mode write (the SAME helpers
  `runner.py` calls for `run.started`/`run.finished`). Every other operation
  (campaigns, reap, postmortem, edges, anchors) goes through the REAL public
  API (`create_campaign`, `add_run_to_campaign`, `conclude_campaign`,
  `reap_runs`, `bathos.mcp.postmortem_validate_tool`, `add_campaign_edge`,
  `add_run_edge`, `CatalogAnchorStore.insert`), which already branch on the
  runlog flag internally (`emit_or_legacy`) -- so calling the identical
  function twice, once per backend, exercises production code end to end on
  both sides.
- The one entity whose id is minted INSIDE the library with no injection
  point is a campaign (`create_campaign`'s `uuid4()`): this is handled by a
  per-backend **handle -> concrete id mapping** (`ExecState.camp_id`),
  built while executing, and every cross-backend comparison below
  translates a campaign-id-valued column through that mapping before
  comparing rows keyed by the SYMBOLIC handle rather than the raw id.

**Wall-clock fields that cannot be injected** (`campaigns.started_at`/
`concluded_at`, minted by `datetime.now(UTC)` inside `create_campaign`/
`conclude_campaign`; the reap ledger's `reaped_at`/`reverted_at`, minted
inside `reap.py`) are scrubbed to a placeholder before comparison. This is a
harness limitation (a wall-clock read with no injectable override in the
public API), not evidence about fold correctness, and is called out
explicitly rather than silently passing.

Every classified divergence this test's development surfaced is written up
in the delivery report (task brief item 4); this docstring only carries the
mechanics.
"""

from __future__ import annotations

import math
import os
from datetime import datetime
from pathlib import Path

import pytest

from .ac17_gen import generate_ops
from .ac17_harness import (
    _WALLCLOCK_KEYS,
    canonical_legacy_state,
    execute_ops,
    make_backend,
    new_fold_state,
)

SEEDS = [1, 2, 3, 4, 5, 7, 11, 13]

# Every column excluded from strict comparison, with the reason. Never a
# blanket exclusion -- each is a single, named column on a single table.
_RUNS_EXCLUDE_COLS = {
    # BC-1: output_metadata is populated ONLY from an explicit
    # `run.outputs_hashed` event after cut-over; this harness never emits
    # one (no run declares output_paths), so the fold always leaves it NULL.
    # The canonical legacy state, by contrast, ALWAYS (re)computes it fresh
    # from disk on every full rebuild regardless of any event -- exactly the
    # "silently by compact" behaviour BC-1 exists to except.
    "output_metadata",
    # AC-17 finding (classification b, legacy quirk -- NOT fixed here,
    # `compact.py` is out of scope): the warm `runs` table has NO
    # `slurm_array_task_id` column at all (`_RUNS_TABLE_SCHEMA` never
    # declares one, unlike `slurm_job_id`), even though it is a real `Run`
    # field; the new index schema does have the column (it is a real
    # `Run` field a SLURM array task run always carries). Excluded rather
    # than comparing "the legacy side never has this key" against a real
    # value the fold correctly populates.
    "slurm_array_task_id",
    # AC-17 finding (classification b, legacy quirk -- NOT fixed here):
    # `manifest_sha256`/`manifest_path`/`adversarial_check_status` ARE real
    # columns in the warm `runs` table (`_RUNS_TABLE_SCHEMA`, plus a
    # redundant idempotent `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` for
    # each), and the `Run` dataclass carries real values for them, but
    # `compact.py`'s multi-column INSERT statement (the "Insert into
    # DuckDB" block, ~compact.py:1092-1173) simply never lists these three
    # columns -- so every fresh warm row gets SQL NULL for them regardless
    # of what the cool fragment actually carries. Confirmed independent of
    # this delivery's fold: a plain `bth compact` on any catalog exhibits
    # this today. Not fixed here (compact.py is out of scope); the new
    # fold correctly populates all three from the frozen event data.
    "manifest_sha256",
    "manifest_path",
    "adversarial_check_status",
    # Not a divergence at all: the legacy warm `runs` table has NO
    # `evalue`/`seq_position` columns -- those exist only on
    # `campaign_runs` (`_CAMPAIGN_RUNS_TABLE_SCHEMA`). The new index
    # DELIBERATELY denormalizes the campaign fold's `evalue`/`seq_position`
    # onto `runs` too (`ingest.py`'s `_refold_run` docstring: "wave b fills
    # them in at the ingest layer"), which is real, additional NEW
    # functionality with no legacy equivalent to compare against -- so
    # `l.get(col)` is unconditionally a missing key here, not a legacy
    # value the fold got wrong. `campaign_runs.evalue`/`.seq_position`
    # (below) IS the real, shared comparison for this logic (AC-22); the
    # `runs`-level denormalization is separately covered by
    # `test_ingest_wave_b.py`'s dedicated tests.
    "evalue",
    "seq_position",
}
_LIST_COLS = {"argv", "output_paths", "tags"}
_CAMPAIGN_ID_COLS = {"campaign_id", "child_campaign_id", "parent_campaign_id"}
_WALLCLOCK_COLS = {"started_at", "concluded_at"}
_PATH_COLS = {"sidecar_path"}


def _floaty_equal(a, b) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-9)
        except (TypeError, ValueError):
            return a == b
    return a == b


def _norm_ts(v):
    if v is None:
        return None
    if isinstance(v, datetime):
        return v
    if isinstance(v, str):
        try:
            return datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            return v
    return v


def _norm_path(v, ws: Path):
    if not isinstance(v, str) or not v:
        return v
    s = str(ws)
    if v.startswith(s):
        return "<WS>" + v[len(s) :]
    return v


def _scrub_wallclock(obj):
    if isinstance(obj, dict):
        return {
            k: ("<TS>" if k in _WALLCLOCK_KEYS and v else _scrub_wallclock(v))
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [_scrub_wallclock(x) for x in obj]
    return obj


def _norm_json_col(v):
    import json

    if v is None:
        return None
    if isinstance(v, str):
        try:
            parsed = json.loads(v)
        except (ValueError, TypeError):
            return v
        return _scrub_wallclock(parsed)
    return _scrub_wallclock(v)


def _norm_list_col(v):
    import json

    if isinstance(v, str):
        try:
            return json.loads(v)
        except (ValueError, TypeError):
            return v
    return v


def _normalize_row(table: str, row: dict, ws: Path, camp_rev: dict[str, str]) -> dict:
    out = {}
    for k, v in row.items():
        if table == "sidecar_anchors" and k == "id":
            continue  # AC-17 explicitly excludes the legacy anchor id (BC-8)
        if table == "runs" and k in _RUNS_EXCLUDE_COLS:
            continue
        if k in _CAMPAIGN_ID_COLS and v:
            v = camp_rev.get(v, v)
        if k in _WALLCLOCK_COLS:
            v = "<TS>" if v else v
        if k in _PATH_COLS:
            v = _norm_path(v, ws)
        if k in ("metadata", "postmortem_asset_links"):
            v = _norm_json_col(v)
        if k in _LIST_COLS:
            v = _norm_list_col(v)
        if k == "timestamp":
            v = _norm_ts(v)
        out[k] = v
    return out


def _diff_rows(legacy_row: dict, new_row: dict) -> list[tuple[str, object, object]]:
    diffs = []
    for col in sorted(set(legacy_row) | set(new_row)):
        lv, nv = legacy_row.get(col), new_row.get(col)
        if not _floaty_equal(lv, nv):
            diffs.append((col, lv, nv))
    return diffs


def _reassignments(ops: list[dict]) -> dict[str, set[str]]:
    """handle -> set(run_ids) reassigned AWAY from that campaign (BC-6)."""
    out: dict[str, set[str]] = {}
    for op in ops:
        if op["kind"] == "add_run_to_campaign":
            prev = op.get("prev_campaign_handle")
            if prev:
                out.setdefault(prev, set()).add(op["run_id"])
    return out


# Fixed by `ac17_harness.sidecar_toml`'s template: `pass`/`fail` are both
# declared non-residual outcomes, `marginal` is the only residual one -- so
# `derive_pass_labels` is this constant for every sidecar this generator
# emits, regardless of its popper rates.
_PASS_LABELS = frozenset({"pass", "fail"})


def _evalue_bucket(outcome: str | None, null: float, alt: float) -> float:
    """A local re-implementation of `bathos.sidecar.compute_evalue`'s
    outcome-label branching (not the sidecar-parsing plumbing) -- used only
    to detect whether two CANDIDATE outcome labels for the same run would
    land in the same e-value bucket, never to assert an actual e-value."""
    if outcome in ("error", "unknown", None, ""):
        return 1.0
    if outcome == "marginal":
        return 1.0
    return alt / null if outcome in _PASS_LABELS else (1.0 - alt) / (1.0 - null)


def _bc7_affected_run_ids(ops: list[dict]) -> set[str]:
    """BC-7: "campaign e-values ... use the member's folded (postmortem-
    overridden) outcome; the legacy campaign pass always sees the
    fragment's raw outcome" (`compact.py:1044` rebinds `run =
    _apply_migrations(run)`, a NEW `Run`, before the postmortem override is
    applied, so the `cool_runs` element `link_cool_runs_to_campaigns` sees
    is never overridden).

    A run is BC-7-affected only when a LATER postmortem override actually
    changes which e-value BUCKET its outcome falls into (pass-direction vs.
    fail-direction vs. the marginal/neutral 1.0 special cases) -- not every
    postmortem changes anything (e.g. `verdict_override="none"`, or an
    override that happens to land in the same bucket as the raw outcome,
    changes nothing either side computes).
    """
    raw_outcome: dict[str, str] = {}
    popper: dict[str, tuple[float, float, float] | None] = {}
    for op in ops:
        if op["kind"] == "start_run":
            popper[op["run_id"]] = op.get("popper")
        elif op["kind"] == "finish_run":
            raw_outcome[op["run_id"]] = op["outcome"]

    affected: set[str] = set()
    for op in ops:
        if op["kind"] != "postmortem":
            continue
        run_id = op["run_id"]
        override = op["verdict_override"]
        if override == "none":
            continue
        raw = raw_outcome.get(run_id)
        pop = popper.get(run_id)
        if pop is None:
            continue
        null, alt, _threshold = pop
        if _evalue_bucket(raw, null, alt) != _evalue_bucket(override, null, alt):
            affected.add(run_id)
    return affected


def _run_differential(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int) -> None:
    ops = generate_ops(seed)

    legacy = make_backend(tmp_path, "legacy", is_new=False)
    with monkeypatch.context() as mp:
        mp.setenv("BTH_WORKSPACE_ROOT", str(legacy.workspace))
        legacy_exec_state = execute_ops(legacy, ops)
        legacy_tables = canonical_legacy_state(legacy, mp)

    new = make_backend(tmp_path, "new", is_new=True)
    with monkeypatch.context() as mp:
        mp.setenv("BTH_WORKSPACE_ROOT", str(new.workspace))
        new_exec_state = execute_ops(new, ops)
    new_tables = new_fold_state(new)

    camp_rev_legacy = {v: k for k, v in legacy_exec_state.camp_id.items()}
    camp_rev_new = {v: k for k, v in new_exec_state.camp_id.items()}

    reassigned_out = _reassignments(ops)
    bc6_handles = {h for h, rids in reassigned_out.items() if rids}
    bc7_run_ids = _bc7_affected_run_ids(ops)

    all_diffs: list[str] = []

    # ---- runs -------------------------------------------------------
    l_runs = {
        r["id"]: _normalize_row("runs", r, legacy.workspace, camp_rev_legacy)
        for r in legacy_tables["runs"]
    }
    n_runs = {
        r["id"]: _normalize_row("runs", r, new.workspace, camp_rev_new)
        for r in new_tables["runs"]
    }
    assert set(l_runs) == set(n_runs), (
        f"run id sets differ: only_legacy={set(l_runs) - set(n_runs)} "
        f"only_new={set(n_runs) - set(l_runs)}"
    )
    for rid in l_runs:
        for col, lv, nv in _diff_rows(l_runs[rid], n_runs[rid]):
            all_diffs.append(f"runs[{rid}].{col}: legacy={lv!r} new={nv!r}")

    # ---- campaigns ---------------------------------------------------
    l_camps = {
        camp_rev_legacy.get(r["id"], r["id"]): _normalize_row(
            "campaigns", r, legacy.workspace, camp_rev_legacy
        )
        for r in legacy_tables["campaigns"]
    }
    n_camps = {
        camp_rev_new.get(r["id"], r["id"]): _normalize_row(
            "campaigns", r, new.workspace, camp_rev_new
        )
        for r in new_tables["campaigns"]
    }
    assert set(l_camps) == set(n_camps), (
        f"campaign handle sets differ: only_legacy={set(l_camps) - set(n_camps)} "
        f"only_new={set(n_camps) - set(l_camps)}"
    )
    for h in l_camps:
        legacy_row, new_row = dict(l_camps[h]), dict(n_camps[h])
        legacy_row.pop("id", None)
        new_row.pop("id", None)
        if h in bc6_handles:
            # BC-6: `add_run_to_campaign` overwrites the fragment's single
            # campaign_id; on a full rebuild the dropped member's earlier
            # stamp is gone entirely, which shifts the stopping_threshold
            # lock for whichever remaining member locks it first.
            legacy_row.pop("stopping_threshold", None)
            new_row.pop("stopping_threshold", None)
        for col, lv, nv in _diff_rows(legacy_row, new_row):
            all_diffs.append(f"campaigns[{h}].{col}: legacy={lv!r} new={nv!r}")

    # ---- campaign_runs -------------------------------------------------
    def _cr_key(rows, camp_rev):
        out = {}
        for r in rows:
            handle = camp_rev.get(r["campaign_id"], r["campaign_id"])
            out[(handle, r["run_id"])] = r
        return out

    l_cr = _cr_key(legacy_tables["campaign_runs"], camp_rev_legacy)
    n_cr = _cr_key(new_tables["campaign_runs"], camp_rev_new)
    bc6_only_new = {(h, rid) for h, rids in reassigned_out.items() for rid in rids}
    only_new = set(n_cr) - set(l_cr)
    only_legacy = set(l_cr) - set(n_cr)
    unexplained_only_new = only_new - bc6_only_new
    if unexplained_only_new:
        all_diffs.append(f"campaign_runs present only in new fold: {unexplained_only_new}")
    if only_legacy:
        all_diffs.append(f"campaign_runs present only in legacy: {only_legacy}")
    for key in set(l_cr) & set(n_cr):
        lr, nr = l_cr[key], n_cr[key]
        campaign_handle, run_id = key
        cols = ["evalue", "seq_position"]
        if campaign_handle in bc6_handles:
            # BC-6: the dropped membership shifts the remaining members'
            # seq_position (a full rebuild only sees the fragment's LATEST
            # single campaign_id stamp, so a departed member's original
            # position in the sequence is gone entirely).
            cols.remove("seq_position")
        if run_id in bc7_run_ids:
            # BC-7: legacy's `link_cool_runs_to_campaigns` sees the raw
            # fragment outcome; the fold uses the postmortem-overridden
            # one -- and for this run, that changes which e-value bucket
            # it lands in (see `_bc7_affected_run_ids`).
            cols.remove("evalue")
        for col in cols:
            if not _floaty_equal(lr.get(col), nr.get(col)):
                all_diffs.append(f"campaign_runs[{key}].{col}: legacy={lr.get(col)!r} new={nr.get(col)!r}")

    # ---- sidecar_anchors (BC-8) ----------------------------------------
    legacy_anchor_rows = legacy_tables["sidecar_anchors"]
    if legacy_anchor_rows:
        all_diffs.append(
            f"BC-8 violated: legacy sidecar_anchors non-empty after force_rebuild: "
            f"{legacy_anchor_rows}"
        )
    anchor_ops = [op for op in ops if op["kind"] == "anchor_insert"]
    new_anchor_by_key = {(r["path"], r["sha256"]): r for r in new_tables["sidecar_anchors"]}
    if len(new_anchor_by_key) != len(anchor_ops):
        all_diffs.append(
            f"sidecar_anchors count mismatch: expected {len(anchor_ops)} inserts, "
            f"got {len(new_anchor_by_key)} distinct (path, sha256) rows"
        )
    for op in anchor_ops:
        row = new_anchor_by_key.get((op["path"], op["sha256"]))
        if row is None:
            all_diffs.append(f"anchor {op['path']}/{op['sha256']} missing from new fold")
            continue
        if row["kind"] != op["anchor_kind"]:
            all_diffs.append(f"anchor {op['path']} kind: expected {op['anchor_kind']} got {row['kind']}")
        if row["label"] != op["label"]:
            all_diffs.append(f"anchor {op['path']} label: expected {op['label']} got {row['label']}")
        expected_handle = op.get("campaign_handle")
        actual_handle = camp_rev_new.get(row["campaign_id"], row["campaign_id"])
        if actual_handle != expected_handle:
            all_diffs.append(
                f"anchor {op['path']} campaign_id: expected handle {expected_handle} "
                f"got {actual_handle}"
            )
        if row["anchored_at"] != op["ts"].isoformat():
            all_diffs.append(
                f"anchor {op['path']} anchored_at: expected {op['ts'].isoformat()} got {row['anchored_at']}"
            )

    # ---- campaign_edges / run_edges ------------------------------------
    # Spec text (AC-17): "campaign_edges and run_edges, which any rebuild
    # (including a mid-sequence reap or revert) drops, are compared against
    # the set of add_campaign_edge/add_run_edge calls that succeeded." --
    # i.e. NOT against the legacy warm table post-rebuild: there is no
    # cool-tier fragment backing either edge table (unlike `runs`), so
    # `compact(force_rebuild=True)` (this test's own canonical-state
    # computation) unconditionally deletes `bathos.db` and therefore both
    # edge tables along with it -- the legacy side of this comparison is
    # BY DESIGN always empty after our own reference computation, the same
    # non-durability BC-8 describes for anchors. So only the NEW fold is
    # checked against the expected set; the legacy table's emptiness is
    # asserted explicitly (a positive confirmation of this known behaviour,
    # not silently skipped).
    expected_camp_edges = {(op["child"], op["parent"]) for op in ops if op["kind"] == "add_campaign_edge"}
    got_new_camp_edges = {
        (
            camp_rev_new.get(r["child_campaign_id"], r["child_campaign_id"]),
            camp_rev_new.get(r["parent_campaign_id"], r["parent_campaign_id"]),
        )
        for r in new_tables["campaign_edges"]
    }
    if got_new_camp_edges != expected_camp_edges:
        all_diffs.append(f"new campaign_edges {got_new_camp_edges} != expected {expected_camp_edges}")
    if legacy_tables["campaign_edges"]:
        all_diffs.append(
            "legacy campaign_edges non-empty after force_rebuild (expected empty -- "
            f"edges have no cool-tier fragment): {legacy_tables['campaign_edges']}"
        )

    expected_run_edges = {(op["child"], op["parent"]) for op in ops if op["kind"] == "add_run_edge"}
    got_new_run_edges = {(r["child_run_id"], r["parent_run_id"]) for r in new_tables["run_edges"]}
    if got_new_run_edges != expected_run_edges:
        all_diffs.append(f"new run_edges {got_new_run_edges} != expected {expected_run_edges}")
    if legacy_tables["run_edges"]:
        all_diffs.append(
            "legacy run_edges non-empty after force_rebuild (expected empty -- "
            f"edges have no cool-tier fragment): {legacy_tables['run_edges']}"
        )

    assert not all_diffs, "AC-17 unexplained divergence(s):\n" + "\n".join(all_diffs)


@pytest.mark.parametrize("seed", SEEDS)
def test_ac17_differential_fold(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int):
    _run_differential(tmp_path, monkeypatch, seed)


@pytest.mark.slow
@pytest.mark.skipif(
    not os.environ.get("BTH_AC17_SWEEP"),
    reason="opt-in wider seed sweep; set BTH_AC17_SWEEP=1 to run (see marker config)",
)
@pytest.mark.parametrize("seed", list(range(100, 130)))
def test_ac17_differential_fold_sweep(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, seed: int):
    """A wider seed sweep (30 seeds). This project's `addopts` has no `-m`
    filter, so a bare marker would still run by default -- the `skipif` is
    what actually keeps this out of a plain `pytest tests/runlog` (well
    under the ~2-minute budget the 8 SEEDS above already spend); set
    `BTH_AC17_SWEEP=1` to run it explicitly for a deeper confidence pass.
    Same body as the main test, factored into `_run_differential` so the two
    never drift apart."""
    _run_differential(tmp_path, monkeypatch, seed)
