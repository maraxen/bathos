"""``bth cold``: write a run's reference set to a cold profile (and a mirror), and ``bth outputs where``
(spec 261023_output-tiering.md sections 4, 5.3, 6.1, 6.4, 7).

One run is one unit: its own ``unit_of_work``, its own process and timeout when driven from SLURM, its own events.
For each leg (cold first, then mirror) the order is the spec's, and it is what the crash windows depend on:

1. resolve the profile; probe ``myxcel cold`` once (``myxcel_capability_gap``);
2. build the archive and manifest locally from ``reference_hashes`` (a drifted live file refuses here, so nothing
   has touched the remote), then **take the remote lock first** (``lock_held`` if held; ``--break-lock`` removes the
   lock directory only and records the broken holder);
3. resume check against the remote finals and the locally built reference set (both present and consistent: record
   without uploading; they differ: ``archive_conflict``; exactly one present: ``archive_conflict`` /
   ``half_committed``);
4. put the manifest and the archive, each to its own ``.partial-<attempt8>`` name;
5. ``verify-file`` both partials; commit the manifest and then the archive (``ln && rm``, never an overwrite);
6. ``verify-file`` both finals; release the lock; emit one ``output_copy.recorded``.

``myxcel cold ls`` and ``cold rm-partial`` are optional capabilities (``myxcel.cold_capabilities``). With them: the
resume check lists the destination directory and reports other attempts' ``*.partial-<8 hex>`` files by name in the
leg result (``partials.other_attempts``), never removing them; a failed partial verify or a failed commit removes
only THIS attempt's two partials, still holding the lock; ``--clean-partials`` removes every ``*.partial-<8 hex>``
in the run's own destination directory (from ``ls``), only while holding the lock, then continues the normal write.
A removal that fails is a warning, not a failure of an otherwise verified copy. Without them (an older myxcel)
nothing is listed or removed: partials are left and named in the error, and ``--clean-partials`` refuses
``myxcel_capability_gap``.

A kill anywhere after the lock leaves it held: the plain rerun is ``lock_held`` and the owner reruns with
``--break-lock``, which then lands in the resume check. Nothing here ever deletes or overwrites a remote final.

Interpretations of the spec, each flagged where it matters (see also the Step 6 report):

* The resume check compares the remote manifest with the local one on everything except ``archive.sha256`` /
  ``archive.size_bytes``, then verifies the archive against the sha256 THE REMOTE MANIFEST names. Section 6.1 says
  both "byte-identical to the manifest built locally" and "resume does not depend on byte identity" of the
  archive; the manifest embeds the archive's hash, so the two cannot both hold if a zstd build differs.
* ``myxcel.lock`` takes no holder argument (section 6.2), so the holder record is whatever myxcel writes.
* The existence of a remote final comes from ``ls`` when it answered (``ok`` / ``missing``): a final that is
  listed but cannot be fetched or verified refuses ``copy_unverifiable`` rather than reading as absent. Without
  a listing (no ``ls``, or it failed: a warning, not a refusal) it is inferred as in Step 6: the manifest by
  ``get`` (its bytes are needed anyway), the archive by ``verify-file`` (``verified`` or ``failed`` mean the file
  was read; ``unverifiable`` means absent or unreadable). That inference is safe because commit never overwrites.
* Section 6.1 step 5 says to delete a failed attempt's partials "on a mismatch". Cleanup here runs on any partial
  verify that is not ``verified`` (``failed`` and ``unverifiable``) and on any failed commit, because a partial is
  never reused (every attempt has a fresh name). A failed ``put`` still leaves its partials, named in the error
  (INTERPRETATION: the brief names verify and commit failures only).
* ``--clean-partials`` runs after the resume check passes, so a ``lock_held`` / ``archive_conflict`` refusal removes
  nothing, and it also runs on a record-only (resumed) pass.
* ``--supersede`` supersedes and then writes the fresh copy in the same invocation, holding the lock throughout.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, NoReturn

from bathos.cluster import MyxcelCapabilityGapError
from bathos.output_tiers import myxcel as mx
from bathos.output_tiers.adopt import (
    NO_REFERENCE_HASHES,
    RUN_NOT_TERMINAL,
    _check_legacy_resolvable,
    _live_state,
    _log_cwd,
)
from bathos.output_tiers.archive import (
    ARCHIVE_CONFLICT,
    OUTPUTS_DRIFTED,
    BuiltArchive,
    archive_name,
    build_archive,
    manifest_name,
)
from bathos.output_tiers.config import (
    ColdProfile,
    OutputTiersConfigError,
    load_profile,
    resolve_profile,
)
from bathos.output_tiers.reference import (
    OutputTierError,
    ReferenceEntry,
    reference_hashes,
)
from bathos.output_tiers.run_context import (
    INVALID_PARAM,
    RunContext,
    is_terminal,
    open_run,
    require_log_mode,
)
from bathos.runlog.fold_output_copies import (
    KIND_RECORDED,
    KIND_SUPERSEDED,
    copy_id_for,
)

logger = logging.getLogger(__name__)

NO_COLD_ROOT = "no_cold_root"
MYXCEL_ERROR = "myxcel_error"
LOCK_HELD = "lock_held"
COPY_VERIFY_FAILED = "copy_verify_failed"
COPY_UNVERIFIABLE = "copy_unverifiable"
COLD_RUNS_FAILED = "cold_runs_failed"
MYXCEL_CAPABILITY_GAP = "myxcel_capability_gap"
HALF_COMMITTED = "half_committed"

LOCK_DIR_NAME = ".bth-lock"

#: Errors a per-run failure under ``--all-uncopied`` can raise; the loop records them and goes on.
_PER_RUN_ERRORS: tuple[type[BaseException], ...] = (OutputTierError,)

Progress = Callable[[dict[str, Any]], None]


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _error_code(exc: BaseException) -> str:
    return str(getattr(exc, "error_code", "internal"))


def _print_progress(line: dict[str, Any]) -> None:
    """One JSON object per line on stdout. Under SLURM the index is not refreshed on the node, so this and the
    event log are the only progress record (spec 6.1, "Under SLURM the index is not the authority")."""
    sys.stdout.write(json.dumps(line, sort_keys=True) + "\n")
    sys.stdout.flush()


def default_progress() -> Progress | None:
    return _print_progress if os.environ.get("SLURM_JOB_ID") else None


# --------------------------------------------------------------------------------------- profiles


@dataclass(frozen=True)
class Leg:
    """One destination: ``tier`` is ``cold`` or ``mirror``."""

    tier: str
    profile: ColdProfile
    layer: str


def resolve_legs(
    profile: str | None, mirror: str | None, config_dir: Path | None = None
) -> list[Leg]:
    """The cold leg, then the mirror leg if one resolves. No cold profile refuses ``no_cold_root`` and names the
    layers checked (spec 3.8)."""
    cold = resolve_profile("cold", profile)
    if cold.name is None:
        raise OutputTierError(NO_COLD_ROOT, f"no cold profile resolves: {cold.layer}")
    legs = [Leg("cold", load_profile(cold.name, config_dir), cold.layer)]
    mir = resolve_profile("mirror", mirror)
    if mir.name is not None:
        if mir.name == cold.name:
            raise OutputTierError(
                INVALID_PARAM, f"the mirror profile {mir.name!r} is the cold profile; pick another"
            )
        legs.append(Leg("mirror", load_profile(mir.name, config_dir), mir.layer))
    return legs


# --------------------------------------------------------------------------------------- remote helpers


def _fail_myxcel(what: str, message: str) -> NoReturn:
    raise OutputTierError(MYXCEL_ERROR, f"{what}: {message or 'myxcel reported an error'}")


def _holder_text(holder: Any) -> str:
    return json.dumps(holder, sort_keys=True, default=str) if holder is not None else "unknown"


def _acquire_lock(
    leg: Leg, dest: str, display_dest: str, runner: mx.Runner | None, break_lock: bool
) -> Any:
    """Take ``<dest>/.bth-lock``. Returns the broken holder (``None`` if the lock was free)."""
    prof = leg.profile.name
    res = mx.lock(prof, dest, runner=runner)
    if res.status == "acquired":
        return None
    if res.status != "held":
        _fail_myxcel(f"cannot lock {display_dest}", res.message)
    where = f"{display_dest}/{LOCK_DIR_NAME}"
    if not break_lock:
        raise OutputTierError(
            LOCK_HELD,
            f"{where} is held by {_holder_text(res.holder)}; bathos does not guess whether it is alive. "
            "If it is dead, rerun with --break-lock",
        )
    broken = res.holder
    freed = mx.unlock(prof, dest, runner=runner)
    # Real `myxcel cold unlock` answers `error` ("lock does not exist") when there is no `.bth-lock`, which here
    # means the holder released between our `lock` and our `unlock`: nothing is left to break. An `error` is
    # therefore not fatal by itself; the relock below decides. If the lock is still there (an unlock that failed
    # for another reason) the relock answers `held` and that is refused with the unlock message attached.
    # Any other non-`released` status (a malformed reply) is refused as before.
    unlock_error = (freed.message or freed.status) if freed.status == "error" else None
    if freed.status not in {"released", "error"}:
        raise OutputTierError(
            LOCK_HELD,
            f"{where} (holder {_holder_text(broken)}) could not be broken: "
            f"{freed.message or freed.status}",
        )
    again = mx.lock(prof, dest, runner=runner)
    if again.status == "held":
        if unlock_error is not None:
            raise OutputTierError(
                LOCK_HELD,
                f"{where} (holder {_holder_text(broken)}) could not be broken: {unlock_error}",
            )
        raise OutputTierError(
            LOCK_HELD, f"{where} was retaken by {_holder_text(again.holder)} after it was broken"
        )
    if again.status != "acquired":
        _fail_myxcel(f"cannot relock {display_dest}", again.message)
    if unlock_error is not None:
        return None  # the lock vanished before we could remove it: nothing was broken
    return broken if broken is not None else "unknown"


def _release_lock(leg: Leg, dest: str, runner: mx.Runner | None, warnings: list[str]) -> None:
    try:
        res = mx.unlock(leg.profile.name, dest, runner=runner)
    except Exception as e:  # noqa: BLE001 - a failed release must not mask the outcome; it is reported
        warnings.append(f"lock {dest}/{LOCK_DIR_NAME} was not released: {e}")
        return
    if res.status != "released":
        warnings.append(
            f"lock {dest}/{LOCK_DIR_NAME} was not released: {res.message or res.status}"
        )


def _get_bytes(
    leg: Leg, rel: str, scratch: Path, runner: mx.Runner | None, counter: list[int]
) -> bytes | None:
    """Fetch one small remote file; ``None`` if it could not be read (absent, or the host is unreachable: the
    contract cannot tell, and every caller is safe under both because nothing is ever overwritten)."""
    counter[0] += 1
    target = scratch / f"get-{counter[0]}"
    target.mkdir(parents=True, exist_ok=True)
    got = mx.get(leg.profile.name, rel, str(target), runner=runner)
    if got.status != "ok" or not got.local_path:
        return None
    try:
        return Path(got.local_path).read_bytes()
    except OSError:
        return None


def _manifest_equivalent(local: dict, remote: dict) -> bool:
    """Equal on everything except the archive's own hash and size (see the module docstring)."""

    def strip(doc: dict) -> dict:
        out = dict(doc)
        archive = dict(out.get("archive") or {})
        archive.pop("sha256", None)
        archive.pop("size_bytes", None)
        out["archive"] = archive
        return out

    return strip(local) == strip(remote)


# --------------------------------------------------------------------------------------- one leg


@dataclass
class _Paths:
    dest: str  # relative to cold_root
    display_dest: str
    manifest_rel: str
    archive_rel: str
    display_manifest: str
    display_archive: str


def _paths(leg: Leg, ctx: RunContext) -> _Paths:
    dest = leg.profile.relative_destination(ctx.project_slug, ctx.run_id)
    display = leg.profile.display_destination(ctx.project_slug, ctx.run_id)
    a, m = archive_name(ctx.run_id), manifest_name(ctx.run_id)
    return _Paths(dest, display, f"{dest}/{m}", f"{dest}/{a}", f"{display}/{m}", f"{display}/{a}")


@dataclass
class _Env:
    """What every leg of one run shares."""

    ctx: RunContext
    built: BuiltArchive
    manifest_bytes: bytes
    scratch: Path
    runner: mx.Runner | None
    emit: Callable[..., str | None]
    break_lock: bool
    supersede: bool
    reason: str
    gets: list[int]
    caps: mx.Capabilities
    clean_partials: bool


def _verify_one(
    leg: Leg, rel: str, sha: str, size: int, runner: mx.Runner | None
) -> mx.VerifyResult:
    return mx.verify_file(leg.profile.name, rel, sha, size, runner=runner)


def _aggregate(*results: mx.VerifyResult) -> str:
    states = {r.status for r in results}
    if states == {"verified"}:
        return "verified"
    return "failed" if "failed" in states else "unverifiable"


def _supersede_pair(env: _Env, leg: Leg, p: _Paths) -> dict[str, Any] | None:
    """Rename whichever of the final pair exists aside (archive first, so a crash between the two leaves the
    recognisable manifest-only ``half_committed`` state). Never deletes. Emits ``output_copy.superseded``."""
    ctx, built = env.ctx, env.built
    prof = leg.profile.name
    remote_bytes = _get_bytes(leg, p.manifest_rel, env.scratch, env.runner, env.gets)
    remote_doc: dict | None = None
    if remote_bytes is not None:
        with contextlib.suppress(ValueError):
            parsed = json.loads(remote_bytes)
            remote_doc = parsed if isinstance(parsed, dict) else None
    archive_info = (remote_doc or {}).get("archive") or {}
    sha = str(archive_info.get("sha256") or built.archive_sha256)
    size = archive_info.get("size_bytes")
    size = size if isinstance(size, int) else built.archive_size_bytes
    probe = _verify_one(leg, p.archive_rel, sha, size, env.runner)
    have_archive = probe.status in {"verified", "failed"}
    have_manifest = remote_bytes is not None
    if not (have_archive or have_manifest):
        return None

    stem = f"run-{ctx.run_id}"
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    suffixes = [f"superseded-{stamp}", f"superseded-{stamp}-{secrets.token_hex(4)}"]
    chosen = suffixes[0]
    renamed_archive = renamed_manifest = None
    if have_archive:
        for chosen in suffixes:
            res = mx.commit(
                prof, p.archive_rel, f"{p.dest}/{stem}.{chosen}.tar.zst", runner=env.runner
            )
            if res.status == "ok":
                renamed_archive = f"{p.display_dest}/{stem}.{chosen}.tar.zst"
                break
            if res.status != "exists":
                _fail_myxcel("cannot rename the archive aside", res.message)
        else:
            _fail_myxcel("cannot rename the archive aside", "every aside name exists")
    if have_manifest:
        res = mx.commit(
            prof, p.manifest_rel, f"{p.dest}/{stem}.{chosen}.manifest.json", runner=env.runner
        )
        if res.status != "ok":
            _fail_myxcel("cannot rename the manifest aside", res.message)
        renamed_manifest = f"{p.display_dest}/{stem}.{chosen}.manifest.json"

    detail = {
        "tier": leg.tier,
        "profile": prof,
        "host": leg.profile.host,
        "path": p.display_archive,
        "config_layer": leg.layer,
        "archive_sha256": sha,
        "manifest_sha256": hashlib.sha256(remote_bytes).hexdigest() if remote_bytes else None,
        "archive_size_bytes": size,
        "reason": env.reason,
        "renamed_to": renamed_archive,
        "renamed_manifest_to": renamed_manifest,
        "superseded_at": _now_iso(),
    }
    copy_id = copy_id_for(ctx.run_id, leg.profile.host, prof, p.display_archive)
    eid = env.emit(KIND_SUPERSEDED, [ctx.run_id, copy_id], detail)
    return {**detail, "copy_id": copy_id, "eid": eid}


def _resume_check(
    env: _Env, leg: Leg, p: _Paths, listed: frozenset[str] | None = None
) -> dict[str, Any] | None:
    """Spec 6.1 step 3. ``None``: nothing on the remote, write. A dict: both finals are present and consistent
    (record, do not upload). Anything else raises ``archive_conflict`` (``half_committed`` if exactly one exists).

    ``listed`` is the destination's entry names from a successful ``ls`` (``frozenset()`` for ``missing``), or
    ``None`` when there is no listing (no ``ls``, or it failed). With a listing, existence is what ``ls`` says, and a
    final that is listed but cannot be read refuses ``copy_unverifiable``: an unreachable host or a transient failure
    must not read as "absent" (review finding 3). Without one, existence is inferred from ``get`` / ``verify-file``
    as in Step 6, which is safe (commit never overwrites) but cannot tell absent from unreadable."""
    built = env.built
    m_name, a_name = Path(p.manifest_rel).name, Path(p.archive_rel).name
    if listed is not None:
        has_m, has_a = m_name in listed, a_name in listed
        if not has_m and not has_a:
            return None
        if not has_m:
            raise OutputTierError(
                ARCHIVE_CONFLICT,
                f"{HALF_COMMITTED}: {p.display_archive} exists but {p.display_manifest} does not; "
                "rerun with --supersede --reason <text> to rename it aside",
            )
        if not has_a:
            raise OutputTierError(
                ARCHIVE_CONFLICT,
                f"{HALF_COMMITTED}: {p.display_manifest} exists but {p.display_archive} does not; "
                "rerun with --supersede --reason <text> to rename the pair aside",
            )
    remote_bytes = _get_bytes(leg, p.manifest_rel, env.scratch, env.runner, env.gets)
    if remote_bytes is None and listed is not None:
        raise OutputTierError(
            COPY_UNVERIFIABLE,
            f"{p.display_manifest} is listed but could not be fetched; nothing was uploaded. Rerun when the "
            "host is reachable",
        )
    if remote_bytes is None:
        probe = _verify_one(
            leg, p.archive_rel, built.archive_sha256, built.archive_size_bytes, env.runner
        )
        if probe.status in {"verified", "failed"}:
            raise OutputTierError(
                ARCHIVE_CONFLICT,
                f"{HALF_COMMITTED}: {p.display_archive} exists but {p.display_manifest} does not; "
                "rerun with --supersede --reason <text> to rename it aside",
            )
        return None
    try:
        remote = json.loads(remote_bytes)
        if not isinstance(remote, dict):
            raise ValueError("not an object")
        info = remote["archive"]
        want_sha, want_size = str(info["sha256"]), int(info["size_bytes"])
    except (ValueError, KeyError, TypeError) as e:
        raise OutputTierError(
            ARCHIVE_CONFLICT, f"{p.display_manifest} exists and is not a bathos manifest ({e})"
        ) from None
    if not _manifest_equivalent(built.manifest, remote):
        raise OutputTierError(
            ARCHIVE_CONFLICT,
            f"{p.display_manifest} exists and differs from this run's reference set; nothing was "
            "overwritten. Rerun with --supersede --reason <text> to rename it aside",
        )
    a = _verify_one(leg, p.archive_rel, want_sha, want_size, env.runner)
    if a.status == "unverifiable" and listed is not None:
        raise OutputTierError(
            COPY_UNVERIFIABLE,
            f"{p.display_archive} is listed but could not be verified ({a.message}); nothing was uploaded",
        )
    if a.status == "unverifiable":
        raise OutputTierError(
            ARCHIVE_CONFLICT,
            f"{HALF_COMMITTED}: {p.display_manifest} exists but {p.display_archive} is missing or "
            "unreadable; rerun with --supersede --reason <text> to rename the pair aside",
        )
    if a.status == "failed":
        raise OutputTierError(
            ARCHIVE_CONFLICT,
            f"{p.display_archive} exists and does not match the sha256 its manifest names; nothing "
            "was overwritten",
        )
    manifest_sha = hashlib.sha256(remote_bytes).hexdigest()
    m = _verify_one(leg, p.manifest_rel, manifest_sha, len(remote_bytes), env.runner)
    if m.status == "failed":
        raise OutputTierError(
            ARCHIVE_CONFLICT, f"{p.display_manifest} changed between reading and verifying it"
        )
    if m.status != "verified":
        raise OutputTierError(
            COPY_UNVERIFIABLE, f"{p.display_manifest} could not be re-read: {m.message}"
        )
    return {
        "archive_sha256": want_sha,
        "archive_size_bytes": want_size,
        "manifest_sha256": manifest_sha,
    }


@dataclass
class _Partials:
    """What the destination directory held in the way of ``*.partial-<8 hex>`` files, and what was done about it."""

    listed: bool = False
    list_error: str | None = None
    other_attempts: list[str] = dataclass_field(default_factory=list)
    removed: list[str] = dataclass_field(default_factory=list)
    absent: list[str] = dataclass_field(default_factory=list)
    failed: list[dict[str, str]] = dataclass_field(default_factory=list)
    #: Every entry name ``ls`` returned (empty for ``missing``); ``None`` when not listed. Not reported.
    names: frozenset[str] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "listed": self.listed,
            "list_error": self.list_error,
            "other_attempts": self.other_attempts,
            "removed": self.removed,
            "absent": self.absent,
            "failed": self.failed,
        }


def _list_partials(env: _Env, leg: Leg, p: _Paths, attempt: str) -> _Partials:
    """Spec 6.1 step 3/4: name the other attempts' partials in this run's destination directory. Never removes.
    Without ``ls`` nothing is listed (``listed`` stays False); a failed ``ls`` is recorded, not raised."""
    rep = _Partials()
    if not env.caps.ls:
        return rep
    res = mx.ls(leg.profile.name, p.dest, runner=env.runner)
    if res.status == "missing":
        rep.listed = True
        rep.names = frozenset()
        return rep
    if res.status != "ok":
        rep.list_error = res.message or "myxcel cold ls failed"
        return rep
    rep.listed = True
    rep.names = frozenset(e.name for e in res.entries if not e.lock)
    rep.other_attempts = sorted(
        e.name
        for e in res.entries
        if not e.lock
        and e.name != LOCK_DIR_NAME
        and mx.is_partial_name(e.name)
        and not e.name.endswith(f".partial-{attempt}")
    )
    return rep


def _rm_partial(env: _Env, leg: Leg, rel: str) -> mx.RmPartialResult:
    """One ``rm-partial``. Never raises for a path problem: that is a reported ``error``."""
    try:
        return mx.rm_partial(leg.profile.name, rel, runner=env.runner)
    except OutputTiersConfigError as e:
        return mx.RmPartialResult("error", str(e))


def _clean_listed(env: _Env, leg: Leg, p: _Paths, rep: _Partials, warnings: list[str]) -> None:
    """``--clean-partials``: remove the listed ``*.partial-<8 hex>`` files of this run's own directory. The caller
    holds the lock. A final name or the lock is never passed to ``rm-partial``."""
    if rep.list_error is not None:
        warnings.append(f"--clean-partials could not list {p.display_dest}: {rep.list_error}")
        return
    finals = {Path(p.manifest_rel).name, Path(p.archive_rel).name}
    for name in rep.other_attempts:
        if name in finals or name == LOCK_DIR_NAME or not mx.is_partial_name(name):
            continue
        res = _rm_partial(env, leg, f"{p.dest}/{name}")
        if res.status == "removed":
            rep.removed.append(name)
        elif res.status == "absent":
            rep.absent.append(name)
        else:
            rep.failed.append({"name": name, "status": res.status, "message": res.message})
            warnings.append(
                f"--clean-partials could not remove {p.display_dest}/{name} ({res.status}): {res.message}"
            )


def _cleanup_attempt(env: _Env, leg: Leg, p: _Paths, attempt: str) -> str:
    """Spec 6.1 step 5: remove only THIS attempt's two partials (the caller holds the lock). Returns the sentence
    that goes into the error. Without ``rm-partial`` nothing is removed and the partials are named instead. Never
    raises: a failed removal is reported in the sentence."""
    names = (f"{p.manifest_rel}.partial-{attempt}", f"{p.archive_rel}.partial-{attempt}")
    shown = (f"{p.display_manifest}.partial-{attempt}", f"{p.display_archive}.partial-{attempt}")
    if not env.caps.rm_partial:
        return f"left in place: {shown[0]}, {shown[1]}"
    removed: list[str] = []
    absent: list[str] = []
    failed: list[str] = []
    for rel, display in zip(names, shown, strict=True):
        res = _rm_partial(env, leg, rel)
        if res.status == "removed":
            removed.append(display)
        elif res.status == "absent":
            absent.append(display)
        else:
            failed.append(f"{display} ({res.status}: {res.message})")
    parts = []
    if removed:
        parts.append(f"removed this attempt's partials: {', '.join(removed)}")
    if absent:
        parts.append(f"already absent: {', '.join(absent)}")
    if failed:
        parts.append(f"could not remove: {'; '.join(failed)}")
        logger.warning("partials of attempt %s not removed: %s", attempt, "; ".join(failed))
    return "; ".join(parts)


def _upload_and_commit(env: _Env, leg: Leg, p: _Paths, attempt: str) -> None:
    """Spec 6.1 steps 4 to 6: partial names per attempt, verify, then commit manifest first and archive last."""
    built, prof, runner = env.built, leg.profile.name, env.runner
    m_part = f"{p.manifest_rel}.partial-{attempt}"
    a_part = f"{p.archive_rel}.partial-{attempt}"
    left = f"{p.display_manifest}.partial-{attempt}, {p.display_archive}.partial-{attempt}"

    for local, remote in ((built.manifest_path, m_part), (built.archive_path, a_part)):
        put = mx.put(prof, str(local), remote, runner=runner)
        if put.status != "ok":
            _fail_myxcel(
                f"upload of {Path(remote).name} failed (partials left: {left})", put.message
            )

    vm = _verify_one(leg, m_part, built.manifest_sha256, len(env.manifest_bytes), runner)
    va = _verify_one(leg, a_part, built.archive_sha256, built.archive_size_bytes, runner)
    state = _aggregate(vm, va)
    if state != "verified":
        code = COPY_VERIFY_FAILED if state == "failed" else COPY_UNVERIFIABLE
        raise OutputTierError(
            code,
            f"the partials did not verify ({'manifest ' + vm.status}, {'archive ' + va.status}); "
            f"nothing was committed; {_cleanup_attempt(env, leg, p, attempt)}",
        )

    cm = mx.commit(prof, m_part, p.manifest_rel, runner=runner)
    if cm.status == "exists":
        raise OutputTierError(
            ARCHIVE_CONFLICT,
            f"{p.display_manifest} appeared while this attempt was uploading; nothing was overwritten; "
            f"{_cleanup_attempt(env, leg, p, attempt)}",
        )
    if cm.status != "ok":
        _fail_myxcel(
            "committing the manifest failed",
            f"{cm.message}; {_cleanup_attempt(env, leg, p, attempt)}",
        )
    ca = mx.commit(prof, a_part, p.archive_rel, runner=runner)
    if ca.status == "exists":
        raise OutputTierError(
            ARCHIVE_CONFLICT,
            f"{HALF_COMMITTED}: the manifest was committed but {p.display_archive} already exists; "
            f"nothing was overwritten; {_cleanup_attempt(env, leg, p, attempt)}",
        )
    if ca.status != "ok":
        _fail_myxcel(
            "committing the archive failed",
            f"{ca.message}; {_cleanup_attempt(env, leg, p, attempt)}",
        )


def _write_leg(env: _Env, leg: Leg) -> dict[str, Any]:
    """One leg, start to finish. Releases the lock on every normal exit (a kill leaves it held, which is the
    point)."""
    ctx, built, runner = env.ctx, env.built, env.runner
    p = _paths(leg, ctx)
    warnings: list[str] = []
    attempt = secrets.token_hex(4)
    broken = _acquire_lock(leg, p.dest, p.display_dest, runner, env.break_lock)
    held = True
    uploaded = False
    resumed = False
    superseded: dict[str, Any] | None = None
    partials = _Partials()
    try:
        if env.supersede:
            superseded = _supersede_pair(env, leg, p)
        partials = _list_partials(env, leg, p, attempt)
        if partials.list_error is not None and not env.clean_partials:
            warnings.append(
                f"could not list leftover partials in {p.display_dest}: {partials.list_error}"
            )
        found = _resume_check(env, leg, p, partials.names)
        if env.clean_partials:
            _clean_listed(env, leg, p, partials, warnings)
        if found is None:
            _upload_and_commit(env, leg, p, attempt)
            uploaded = True
            vm = _verify_one(
                leg, p.manifest_rel, built.manifest_sha256, len(env.manifest_bytes), runner
            )
            va = _verify_one(
                leg, p.archive_rel, built.archive_sha256, built.archive_size_bytes, runner
            )
            state = _aggregate(vm, va)
            facts = {
                "archive_sha256": built.archive_sha256,
                "archive_size_bytes": built.archive_size_bytes,
                "manifest_sha256": built.manifest_sha256,
            }
        else:
            resumed = True
            state = "verified"
            facts = found
        _release_lock(leg, p.dest, runner, warnings)
        held = False
    finally:
        if held:
            _release_lock(leg, p.dest, runner, warnings)

    now = _now_iso()
    copy_id = copy_id_for(ctx.run_id, leg.profile.host, leg.profile.name, p.display_archive)
    data = {
        "tier": leg.tier,
        "profile": leg.profile.name,
        "host": leg.profile.host,
        "path": p.display_archive,
        "manifest_path": p.display_manifest,
        "config_layer": leg.layer,
        **facts,
        "state": state,
        "verified_at": now if state == "verified" else None,
        "recorded_at": now,
        "attempt": attempt if uploaded else None,
        "resumed": resumed,
        "broken_lock": {"holder": broken} if broken is not None else None,
        "n_files": len(built.manifest["files"]),
        "project_slug": ctx.project_slug,
    }
    eid = env.emit(KIND_RECORDED, [ctx.run_id, copy_id], data)
    result = {
        "leg": leg.tier,
        "tier": leg.tier,
        "profile": leg.profile.name,
        "host": leg.profile.host,
        "path": p.display_archive,
        "config_layer": leg.layer,
        "copy_id": copy_id,
        "state": state,
        "uploaded": uploaded,
        "resumed": resumed,
        "superseded": superseded,
        "broken_lock": broken,
        "partials": partials.as_dict(),
        "eid": eid,
        "warnings": warnings,
    }
    if state != "verified":
        code = COPY_VERIFY_FAILED if state == "failed" else COPY_UNVERIFIABLE
        raise OutputTierError(
            code,
            f"{p.display_archive} was committed and recorded as {state}; re-read it with "
            "`bth verify --tier outputs`",
        )
    return result


# --------------------------------------------------------------------------------------- one run


def _dry_run(ctx: RunContext, ref: dict[str, ReferenceEntry], legs: list[Leg]) -> dict[str, Any]:
    assert ctx.worktree_root is not None
    states = [_live_state(ctx.worktree_root, ref[rel]) for rel in sorted(ref)]
    bad = [s["relpath"] for s in states if s["status"] != "match"]
    if bad:
        shown = ", ".join(bad[:5]) + (f" (+{len(bad) - 5} more)" if len(bad) > 5 else "")
        raise OutputTierError(OUTPUTS_DRIFTED, f"{shown} differ from the run's reference hashes")
    total = sum(int(s.get("size_bytes") or 0) for s in states)
    return {
        "ok": True,
        "dry_run": True,
        "run_id": ctx.run_id,
        "project_slug": ctx.project_slug,
        "worktree_root": str(ctx.worktree_root),
        "n_files": len(states),
        "total_bytes": total,
        "files": [s["relpath"] for s in states],
        "destinations": [
            {
                "leg": leg.tier,
                "profile": leg.profile.name,
                "host": leg.profile.host,
                "path": leg.profile.display_destination(ctx.project_slug, ctx.run_id),
                "config_layer": leg.layer,
            }
            for leg in legs
        ],
    }


def _cold_one(
    catalog_dir: Path,
    run_prefix: str,
    legs: list[Leg],
    *,
    dry_run: bool,
    supersede: bool,
    reason: str,
    break_lock: bool,
    clean_partials: bool,
    worktree_root: str | None,
    runner: mx.Runner | None,
    progress: Progress | None,
) -> dict[str, Any]:
    from bathos.runlog.emit import emit_event, unit_of_work

    started: list[str] = []  # legs that reached a progress line

    def say(run_id: str, leg: str, state: str, code: str | None) -> None:
        started.append(leg)
        if progress is not None:
            progress({"run_id": run_id, "leg": leg, "state": state, "error_code": code})

    run_id = run_prefix
    with unit_of_work(catalog_dir) as log_mode:
        try:
            if not log_mode:
                require_log_mode(catalog_dir)
            ctx = open_run(catalog_dir, run_prefix, worktree_root=worktree_root)
            run_id = ctx.run_id
            assert ctx.worktree_root is not None
            if not is_terminal(ctx.status):
                raise OutputTierError(
                    RUN_NOT_TERMINAL, f"run {ctx.run_id} has status {ctx.status!r}"
                )
            _check_legacy_resolvable(ctx)
            ref = reference_hashes(ctx.events, imported_root=ctx.imported_root)
            if not ref:
                raise OutputTierError(NO_REFERENCE_HASHES, ctx.run_id)
            if dry_run:
                return _dry_run(ctx, ref, legs)

            caps = mx.cold_capabilities(runner)
            log_cwd = _log_cwd(ctx, None)

            def emit(kind: str, entity: list[str], data: dict[str, Any]) -> str | None:
                out = emit_event(
                    kind=kind,
                    entity=entity,
                    data=data,
                    cwd=log_cwd,
                    hard_fail=True,
                    worktree_root=ctx.worktree_root,
                )
                return (out.envelope or {}).get("eid")

            # Staging is transient scratch (a built archive of the run's reference set), not derived data
            # that outlives the command: it lives under $TMPDIR and is removed on exit.
            with tempfile.TemporaryDirectory(prefix="bth-cold-") as tmp:
                scratch = Path(tmp)
                staging = scratch / "staging"
                staging.mkdir()
                built = build_archive(
                    ctx.run_id,
                    ctx.project_slug,
                    ref,
                    ctx.worktree_root,
                    staging,
                    worktree_root=str(ctx.worktree_root),
                )
                env = _Env(
                    ctx=ctx,
                    built=built,
                    manifest_bytes=built.manifest_path.read_bytes(),
                    scratch=scratch,
                    runner=runner,
                    emit=emit,
                    break_lock=break_lock,
                    supersede=supersede,
                    reason=reason,
                    gets=[0],
                    caps=caps,
                    clean_partials=clean_partials,
                )
                results: list[dict[str, Any]] = []
                for leg in legs:
                    try:
                        res = _write_leg(env, leg)
                    except OutputTierError as e:
                        say(ctx.run_id, leg.tier, "failed", e.error_code)
                        if results:
                            raise OutputTierError(
                                e.error_code,
                                f"{leg.tier} leg failed ({e.detail}); the {results[0]['tier']} leg is "
                                f"already {results[0]['state']} as copy {results[0]['copy_id']}",
                            ) from e
                        raise
                    say(ctx.run_id, leg.tier, res["state"], None)
                    results.append(res)
            return {
                "ok": True,
                "dry_run": False,
                "run_id": ctx.run_id,
                "project_slug": ctx.project_slug,
                "worktree_root": str(ctx.worktree_root),
                "n_files": len(built.manifest["files"]),
                "archive_size_bytes": built.archive_size_bytes,
                "legs": results,
            }
        except OutputTierError as e:
            if not started and not dry_run:
                say(run_id, "cold", "failed", e.error_code)
            raise


# --------------------------------------------------------------------------------------- entry points


def _uncopied_run_ids(catalog_dir: Path, project_slug: str, cold_profile: str) -> list[str]:
    """Completed runs of ``project_slug`` with some reference material and no verified, un-superseded copy on
    ``cold_profile``, oldest first. Read from the index, which under SLURM may be stale: step 3 of the write
    (the remote state) is what makes a repeat pass a record-only no-op."""
    from bathos.index import connect_read
    from bathos.runlog.ingest import ingest_after_write

    ingest_after_write(catalog_dir)
    con = connect_read(catalog_dir)
    try:
        has_ref = (
            "(COALESCE(output_metadata, '') NOT IN ('', '[]', 'null') "
            "OR COALESCE(adopted_outputs, '') NOT IN ('', '[]', 'null'))"
        )
        cols = {
            r[0]
            for r in con.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'runs'"
            ).fetchall()
        }
        if "adopted_outputs" not in cols:
            has_ref = "COALESCE(output_metadata, '') NOT IN ('', '[]', 'null')"
        rows = con.execute(
            f"SELECT id FROM runs WHERE project_slug = ? AND status = 'completed' AND {has_ref} "  # noqa: S608
            "AND id NOT IN (SELECT run_id FROM output_copies WHERE profile = ? "
            "AND state = 'verified' AND superseded_at IS NULL) ORDER BY timestamp, id",
            [project_slug, cold_profile],
        ).fetchall()
    finally:
        con.close()
    return [r[0] for r in rows]


def cold_outputs(
    catalog_dir: Path,
    run_prefix: str = "",
    *,
    all_uncopied: bool = False,
    profile: str | None = None,
    mirror: str | None = None,
    dry_run: bool = False,
    supersede: bool = False,
    reason: str = "",
    break_lock: bool = False,
    clean_partials: bool = False,
    worktree_root: str | None = None,
    project_slug: str | None = None,
    runner: mx.Runner | None = None,
    config_dir: Path | None = None,
    progress: Progress | None = None,
) -> dict[str, Any]:
    """``bth cold``: write one run (``run_prefix``) or every uncopied completed run of the project
    (``all_uncopied``) to the cold profile and the mirror. See the module docstring for the sequence.

    Raises :class:`OutputTierError` (``error_code``) for a refusal of a single run, and
    :class:`~bathos.cluster.MyxcelCapabilityGapError` / :class:`OutputTiersConfigError` for an old myxcel or a bad
    profile. Under ``all_uncopied`` a per-run failure is recorded and the loop continues; the returned dict then
    carries ``error_code == "cold_runs_failed"`` so the command exits 1."""
    if all_uncopied == bool(run_prefix):
        raise OutputTierError(INVALID_PARAM, "give exactly one of --run-id or --all-uncopied")
    if supersede and (not reason or not reason.strip()):
        raise OutputTierError(INVALID_PARAM, "--supersede requires --reason")
    if all_uncopied and (supersede or worktree_root):
        raise OutputTierError(
            INVALID_PARAM, "--supersede and --worktree-root apply to one run, not --all-uncopied"
        )
    require_log_mode(catalog_dir)
    legs = resolve_legs(profile, mirror, config_dir)
    if clean_partials and not dry_run:
        caps = mx.cold_capabilities(runner)
        if not (caps.ls and caps.rm_partial):
            missing = [n for n, ok in (("ls", caps.ls), ("rm-partial", caps.rm_partial)) if not ok]
            raise OutputTierError(
                MYXCEL_CAPABILITY_GAP,
                "--clean-partials needs the `myxcel cold ls` and `myxcel cold rm-partial` primitives "
                f"(spec section 6.2); the installed myxcel lacks: {', '.join(missing)}. Upgrade myxcel, "
                "or remove the .partial-* files on the host by hand",
            )
    emit_progress = progress if progress is not None else default_progress()

    def one(run: str) -> dict[str, Any]:
        return _cold_one(
            catalog_dir,
            run,
            legs,
            dry_run=dry_run,
            supersede=supersede,
            reason=reason,
            break_lock=break_lock,
            clean_partials=clean_partials,
            worktree_root=worktree_root,
            runner=runner,
            progress=emit_progress,
        )

    if not all_uncopied:
        return one(run_prefix)

    slug = project_slug
    if not slug:
        from bathos.config import find_project_config, load_project_config

        cfg = find_project_config(Path.cwd())
        slug = load_project_config(cfg).slug if cfg else None
    if not slug:
        raise OutputTierError(
            INVALID_PARAM, "--all-uncopied needs a project: run it inside one (a .bth.toml)"
        )
    if not dry_run:
        mx.probe_cold_capability(runner)
    from bathos.runlog.emit import RunNotLaunchedError

    results: list[dict[str, Any]] = []
    for run_id in _uncopied_run_ids(catalog_dir, slug, legs[0].profile.name):
        try:
            res = one(run_id)
        except OutputTierError as e:
            if e.error_code == NO_REFERENCE_HASHES:
                results.append({"run_id": run_id, "ok": True, "skipped": NO_REFERENCE_HASHES})
                continue
            results.append(
                {"run_id": run_id, "ok": False, "error_code": e.error_code, "error": e.detail}
            )
        except RunNotLaunchedError as e:
            results.append(
                {"run_id": run_id, "ok": False, "error_code": "catalog_error", "error": str(e)}
            )
        else:
            results.append({"run_id": run_id, "ok": True, "result": res})
    failed = [r for r in results if not r["ok"]]
    out: dict[str, Any] = {
        "ok": not failed,
        "all_uncopied": True,
        "dry_run": dry_run,
        "project_slug": slug,
        "n_runs": len(results),
        "n_failed": len(failed),
        "n_skipped": sum(1 for r in results if r.get("skipped")),
        "results": results,
    }
    if failed:
        named = "; ".join(f"{r['run_id'][:8]} {r['error_code']}" for r in failed)
        out["error_code"] = COLD_RUNS_FAILED
        out["error"] = f"{COLD_RUNS_FAILED}: {len(failed)} of {len(results)} runs failed ({named})"
    return out


# --------------------------------------------------------------------------------------- where


def where_outputs(catalog_dir: Path, run_prefix: str) -> dict[str, Any]:
    """``bth outputs where``: the ``output_copies`` rows of one run, each with the config layer that chose it.
    Works on an ``index.db`` that predates the table (it reads as empty until the next ingest)."""
    from bathos.index import OUTPUT_COPIES_COLUMNS, connect_read

    ctx = open_run(catalog_dir, run_prefix, need_root=False)
    con = connect_read(catalog_dir)
    try:
        cols = ", ".join(OUTPUT_COPIES_COLUMNS)
        rows = con.execute(
            f"SELECT {cols} FROM output_copies WHERE run_id = ? "  # noqa: S608
            "ORDER BY recorded_at, copy_id",
            [ctx.run_id],
        ).fetchall()
    finally:
        con.close()
    copies = [dict(zip(OUTPUT_COPIES_COLUMNS, r, strict=True)) for r in rows]
    live = [c for c in copies if c["superseded_at"] is None]
    return {
        "ok": True,
        "run_id": ctx.run_id,
        "n_copies": len(copies),
        "n_verified": sum(1 for c in live if c["state"] == "verified"),
        "copies": copies,
    }


# Re-exported for the tool layer, which shapes these as error dicts.
__all__ = [
    "COLD_RUNS_FAILED",
    "COPY_UNVERIFIABLE",
    "COPY_VERIFY_FAILED",
    "LOCK_HELD",
    "MYXCEL_ERROR",
    "MyxcelCapabilityGapError",
    "OutputTiersConfigError",
    "cold_outputs",
    "resolve_legs",
    "where_outputs",
]
