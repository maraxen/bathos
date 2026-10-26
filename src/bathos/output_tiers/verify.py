"""``bth verify --tier outputs``: re-read every live cold / mirror copy of a run's outputs on its host and record
what was measured (spec 261023_output-tiering.md sections 4, 5.3, 7 and 9 row 8).

For each live (not superseded) ``cold`` / ``mirror`` row of ``output_copies``, the archive and the manifest are
re-hashed on their host with ``myxcel cold verify-file`` against the sha256 the copy record holds, and one
``output_copy.verified`` event is appended per copy (``state``, ``verified_at``, ``detail``, plus the per-file
answers). The fold takes ``state`` / ``verified_at`` from the latest of {recorded, verified}, so ``bth outputs
where`` shows the new state after the ingest that closes the command.

Three states, never a bool (spec section 4): ``verified`` (bytes re-read and matched), ``failed`` (bytes read and
different), ``unverifiable`` (bytes could not be read: host unreachable, Duo, sandbox). ``unverifiable`` never
turns into either of the others. A copy is ``verified`` only if BOTH files are; ``failed`` if either failed;
otherwise ``unverifiable``.

Opt-in only: nothing in ``bathos.verify.verify_all`` (the default ``--tier all``) imports or calls this module,
because it needs the network.

Interpretations of the spec, each flagged where it matters:

* The recorded ``manifest_size_bytes`` (written by ``bth cold`` since this step) is what ``verify-file`` needs for
  the manifest, whose size the ``output_copies`` columns do not hold. A copy recorded without it has the manifest
  re-read with ``myxcel cold get`` and hashed here against ``manifest_sha256`` (method ``get``).
* The remote path myxcel takes is the recorded absolute archive path minus the profile's ``cold_root``; a path
  that is not under the profile's current ``cold_root`` (the profile was edited since) is ``unverifiable``, not a
  guess. The manifest sits beside the archive under its fixed name.
* "Then checks cold and mirror agree": the live cold / mirror rows of one run must record the same archive and
  manifest sha256. If they do not, the run verdict is ``failed`` whatever each copy's own state is.
* Warm copies are not covered by the section 4 row and are listed as skipped; a missing or unreadable myxcel
  profile makes that copy ``unverifiable`` (the message names the file) instead of aborting the other runs.
* Exit semantics: any ``failed`` copy or a cold/mirror disagreement is ``ok: false`` (exit 1); ``unverifiable``
  is reported distinctly and is ``ok: false`` only under ``strict``.
* An old myxcel without the ``cold`` primitives raises ``myxcel_capability_gap`` (spec section 7) once at least one
  copy needs verifying: that is a missing tool, not an unreachable host.
"""

from __future__ import annotations

import logging
import posixpath
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.output_tiers import myxcel as mx
from bathos.output_tiers.archive import manifest_name
from bathos.output_tiers.config import ColdProfile, OutputTiersConfigError, load_profile
from bathos.output_tiers.reference import hash_file
from bathos.output_tiers.run_context import (
    RunContext,
    _resolve_root,
    open_run,
    require_log_mode,
)
from bathos.runlog.fold_output_copies import KIND_RECORDED, KIND_VERIFIED

logger = logging.getLogger(__name__)

OUTPUT_COPIES_FAILED = "output_copies_failed"
OUTPUT_COPIES_UNVERIFIABLE = "output_copies_unverifiable"

_TIERS = ("cold", "mirror")


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _aggregate(*states: str) -> str:
    if set(states) == {"verified"}:
        return "verified"
    return "failed" if "failed" in states else "unverifiable"


def _file_result(res: mx.VerifyResult) -> dict[str, Any]:
    return {
        "state": res.status,
        "sha256_observed": res.sha256_observed,
        "size_observed": res.size_observed,
        "method": res.method,
        "message": res.message,
    }


def _unverifiable_file(message: str) -> dict[str, Any]:
    return {
        "state": "unverifiable",
        "sha256_observed": None,
        "size_observed": None,
        "method": None,
        "message": message,
    }


def _relative(profile: ColdProfile, absolute: str) -> str | None:
    """The path myxcel takes (relative to ``cold_root``), or None if ``absolute`` is not under it."""
    root = profile.cold_root.rstrip("/") + "/"
    return absolute[len(root) :] if absolute.startswith(root) else None


def _recorded_manifest_size(con: Any, run_id: str, copy_id: str) -> int | None:
    """``manifest_size_bytes`` from the latest ``output_copy.recorded`` event of the copy, if it carries one."""
    from bathos.runlog.ingest import _fetch_events_by_entity_key

    recorded = [
        ev
        for ev in _fetch_events_by_entity_key(con, [run_id, copy_id])
        if ev.get("kind") == KIND_RECORDED
    ]
    if not recorded:
        return None
    latest = max(recorded, key=lambda ev: (ev.get("ts", ""), ev.get("eid", "")))
    size = (latest.get("data") or {}).get("manifest_size_bytes")
    return size if isinstance(size, int) and not isinstance(size, bool) and size >= 0 else None


def _verify_manifest(
    profile: ColdProfile,
    rel: str,
    sha: str,
    size: int | None,
    runner: mx.Runner | None,
) -> dict[str, Any]:
    if size is not None:
        return _file_result(mx.verify_file(profile.name, rel, sha, size, runner=runner))
    # No recorded size: re-read the (small) manifest and hash it here.
    with tempfile.TemporaryDirectory(prefix="bth-verify-") as tmp:
        got = mx.get(profile.name, rel, tmp, runner=runner)
        if got.status != "ok" or got.local_path is None:
            return _unverifiable_file(got.message or "could not re-read the manifest")
        try:
            observed = hash_file(got.local_path)
            observed_size = Path(got.local_path).stat().st_size
        except OSError as e:
            return _unverifiable_file(f"could not hash the fetched manifest: {e}")
    state = "verified" if observed == sha.lower() else "failed"
    return {
        "state": state,
        "sha256_observed": observed,
        "size_observed": observed_size,
        "method": "get",
        "message": "" if state == "verified" else "manifest sha256 differs from the recorded one",
    }


def _verify_copy(
    row: dict[str, Any],
    manifest_size: int | None,
    config_dir: Path | None,
    runner: mx.Runner | None,
) -> dict[str, Any]:
    """The measured state of one copy: ``state``, ``archive`` and ``manifest`` answers, ``detail``."""
    run_id = row["run_id"]

    def finish(archive: dict[str, Any], manifest: dict[str, Any]) -> dict[str, Any]:
        state = _aggregate(archive["state"], manifest["state"])
        problems = [
            f"{label}: {part['message'] or part['state']}"
            for label, part in (("archive", archive), ("manifest", manifest))
            if part["state"] != "verified"
        ]
        methods = sorted({m for m in (archive["method"], manifest["method"]) if m})
        return {
            "state": state,
            "method": "+".join(methods) if methods else None,
            "detail": "; ".join(problems)
            if problems
            else "archive and manifest re-read and matched",
            "archive": archive,
            "manifest": manifest,
        }

    def cannot(message: str) -> dict[str, Any]:
        return finish(_unverifiable_file(message), _unverifiable_file(message))

    try:
        profile = load_profile(row["profile"], config_dir)
    except OutputTiersConfigError as e:
        return cannot(f"cannot resolve myxcel profile {row['profile']!r}: {e}")
    archive_rel = _relative(profile, row["path"])
    if archive_rel is None:
        return cannot(
            f"recorded path {row['path']} is not under profile {row['profile']!r} cold_root "
            f"{profile.cold_root}"
        )
    manifest_rel = posixpath.join(posixpath.dirname(archive_rel), manifest_name(run_id))
    if not row.get("archive_sha256") or row.get("archive_size_bytes") is None:
        return cannot("the copy record holds no archive sha256 / size to verify against")
    if not row.get("manifest_sha256"):
        return cannot("the copy record holds no manifest sha256 to verify against")
    try:
        archive = _file_result(
            mx.verify_file(
                profile.name,
                archive_rel,
                row["archive_sha256"],
                int(row["archive_size_bytes"]),
                runner=runner,
            )
        )
        manifest = _verify_manifest(
            profile,
            manifest_rel,
            row["manifest_sha256"],
            manifest_size,
            runner,
        )
    except OutputTiersConfigError as e:  # a path or hash myxcel (and so bathos) refuses
        return cannot(str(e))
    return finish(archive, manifest)


def _live_rows(con: Any, run_id: str | None) -> list[dict[str, Any]]:
    from bathos.index import OUTPUT_COPIES_COLUMNS

    cols = ", ".join(OUTPUT_COPIES_COLUMNS)
    sql = f"SELECT {cols} FROM output_copies WHERE superseded_at IS NULL AND state != 'superseded'"  # noqa: S608
    params: list[str] = []
    if run_id is not None:
        sql += " AND run_id = ?"
        params.append(run_id)
    sql += " ORDER BY run_id, recorded_at, copy_id"
    return [
        dict(zip(OUTPUT_COPIES_COLUMNS, r, strict=True))
        for r in con.execute(sql, params).fetchall()
    ]


def _agreement(rows: list[dict[str, Any]]) -> tuple[bool, str]:
    """Whether the live cold / mirror rows of one run record the same archive and manifest sha256."""
    legs = [r for r in rows if r["tier"] in _TIERS]
    problems = []
    for key in ("archive_sha256", "manifest_sha256"):
        values = {r[key] for r in legs}
        if len(values) > 1:
            problems.append(
                f"{key} differs across {', '.join(sorted(r['tier'] + ':' + r['profile'] for r in legs))}"
            )
    return (not problems), "; ".join(problems)


def verify_outputs(
    catalog_dir: Path,
    run_prefix: str = "",
    *,
    profile: str | None = None,
    strict: bool = False,
    runner: mx.Runner | None = None,
    config_dir: Path | None = None,
) -> dict[str, Any]:
    """Re-verify the live cold / mirror copies of ``run_prefix`` (a unique prefix) or of every run with copies.

    ``profile`` limits the copies verified to that myxcel profile. Appends one ``output_copy.verified`` event per
    verified copy (each as soon as it is measured, so an interrupted command keeps what it did). Returns the
    per-run, per-copy JSON; ``ok`` is False when any copy ``failed`` (or its cold/mirror disagree), or, under
    ``strict``, any copy is ``unverifiable``."""
    from bathos.index import connect_read
    from bathos.runlog.emit import emit_event, unit_of_work
    from bathos.runlog.ingest import ingest_after_write

    require_log_mode(catalog_dir)
    with unit_of_work(catalog_dir):
        # Phase 1 reads everything (index, run contexts) before any event is written, so no read connection is
        # open while an ingest folds this command's own events.
        run_ids: list[str] | None = None
        if run_prefix:
            run_ids = [open_run(catalog_dir, run_prefix, need_root=False).run_id]
        else:
            ingest_after_write(catalog_dir)
        con = connect_read(catalog_dir)
        try:
            rows = _live_rows(con, run_ids[0] if run_ids else None)
            sizes = {
                r["copy_id"]: _recorded_manifest_size(con, r["run_id"], r["copy_id"]) for r in rows
            }
        finally:
            con.close()
        if run_ids is None:
            run_ids = sorted({r["run_id"] for r in rows})
        by_run: dict[str, list[dict[str, Any]]] = {rid: [] for rid in run_ids}
        for r in rows:
            by_run[r["run_id"]].append(r)
        ctxs = {rid: open_run(catalog_dir, rid, need_root=False) for rid in run_ids}

        # Phase 2: the network, one event per copy as soon as it is measured.
        if any(r["tier"] in _TIERS and (profile is None or r["profile"] == profile) for r in rows):
            mx.probe_cold_capability(runner)
        runs_out = [
            _verify_run(
                ctxs[run_id],
                by_run[run_id],
                sizes,
                profile=profile,
                runner=runner,
                config_dir=config_dir,
                emit=emit_event,
            )
            for run_id in run_ids
        ]

    counts = {"verified": 0, "failed": 0, "unverifiable": 0}
    for run in runs_out:
        for copy in run["copies"]:
            counts[copy["state"]] += 1
    disagree = [r["run_id"] for r in runs_out if not r["agree"]]
    ok = counts["failed"] == 0 and not disagree and not (strict and counts["unverifiable"])
    out: dict[str, Any] = {
        "ok": ok,
        "tier": "outputs",
        "strict": strict,
        "profile": profile,
        "n_runs": len(runs_out),
        "n_copies": sum(counts.values()),
        "n_verified": counts["verified"],
        "n_failed": counts["failed"],
        "n_unverifiable": counts["unverifiable"],
        "runs": runs_out,
    }
    if not ok:
        parts = []
        if counts["failed"]:
            parts.append(f"{counts['failed']} copies failed")
        if disagree:
            parts.append(f"cold and mirror disagree for {', '.join(r[:8] for r in disagree)}")
        if strict and counts["unverifiable"]:
            parts.append(f"{counts['unverifiable']} copies unverifiable (--strict)")
        out["error_code"] = (
            OUTPUT_COPIES_FAILED if (counts["failed"] or disagree) else OUTPUT_COPIES_UNVERIFIABLE
        )
        out["error"] = f"{out['error_code']}: {'; '.join(parts)}"
    return out


def _verify_run(
    ctx: RunContext,
    rows: list[dict[str, Any]],
    sizes: dict[str, int | None],
    *,
    profile: str | None,
    runner: mx.Runner | None,
    config_dir: Path | None,
    emit: Callable[..., Any],
) -> dict[str, Any]:
    from bathos.output_tiers.adopt import _log_cwd
    from bathos.output_tiers.reference import OutputTierError

    run_id = ctx.run_id
    log_cwd = _log_cwd(ctx, None)
    try:
        worktree_root: Path | None = _resolve_root(ctx.events, None)[0]
    except OutputTierError:
        worktree_root = None

    agree, agree_detail = _agreement(rows)
    skipped = [
        {"copy_id": r["copy_id"], "tier": r["tier"], "reason": "not_covered_by_outputs_tier"}
        for r in rows
        if r["tier"] not in _TIERS
    ]
    copies: list[dict[str, Any]] = []
    for row in rows:
        if row["tier"] not in _TIERS:
            continue
        if profile is not None and row["profile"] != profile:
            skipped.append(
                {"copy_id": row["copy_id"], "tier": row["tier"], "reason": "other_profile"}
            )
            continue
        measured = _verify_copy(row, sizes.get(row["copy_id"]), config_dir, runner)
        checked_at = _now_iso()
        data = {
            "state": measured["state"],
            "verified_at": checked_at if measured["state"] == "verified" else None,
            "checked_at": checked_at,
            "method": measured["method"],
            "detail": measured["detail"],
            "tier": row["tier"],
            "profile": row["profile"],
            "host": row["host"],
            "path": row["path"],
            "archive": measured["archive"],
            "manifest": measured["manifest"],
        }
        out = emit(
            kind=KIND_VERIFIED,
            entity=[run_id, row["copy_id"]],
            data=data,
            cwd=log_cwd,
            hard_fail=True,
            worktree_root=worktree_root,
        )
        copies.append(
            {
                "copy_id": row["copy_id"],
                "tier": row["tier"],
                "profile": row["profile"],
                "host": row["host"],
                "path": row["path"],
                "previous_state": row["state"],
                "state": measured["state"],
                "method": measured["method"],
                "detail": measured["detail"],
                "checked_at": checked_at,
                "archive": measured["archive"],
                "manifest": measured["manifest"],
                "eid": (out.envelope or {}).get("eid"),
            }
        )
    states = [c["state"] for c in copies]
    if not agree or "failed" in states:
        verdict = "failed"
    elif "unverifiable" in states:
        verdict = "unverifiable"
    else:
        verdict = "verified" if states else "no_copies"
    return {
        "run_id": run_id,
        "verdict": verdict,
        "agree": agree,
        "agree_detail": agree_detail,
        "copies": copies,
        "skipped": skipped,
    }
