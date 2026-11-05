"""Project charter and confirmation engine (spec 261104, slice 1).

Implements:
- Charter & Purpose dataclasses
- TOML parsing with validation (tomllib)
- Deterministic TOML dumping with ext preservation (tomlkit)
- Raw binary SHA256 calculation
- Human-chair-only confirmation gate and mandate immutability check
- Purpose ledger pivot with live-purpose completeness verification
- Charter status reporting
- Charter draft generation (re-emit or scaffold)
"""

from __future__ import annotations

import hashlib
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

import tomlkit

from bathos.errors import (
    CharterInvalidError,
    CharterLedgerUnavailableError,
    CharterMandateChangedError,
    CharterNotFoundError,
    CharterPivotIncompleteError,
    CharterUnauthorizedError,
    StepBackAgentOverrideRefusedError,
    StepBackUnauthorizedChairError,
)
from bathos.index import catalog_readable, connect_read, default_catalog_dir
from bathos.runlog.emit import emit_event, unit_of_work
from bathos.stepback.escalation import require_human_chair

SCAFFOLD_TOML = """# Project Charter Draft
# The owner must fill in the mandate verbatim below.
mandate = ""

# Example purpose (commented out):
# [purpose.example]
# text = "Example purpose description"
# state = "live"
"""


@dataclass
class Purpose:
    id: str
    text: str
    state: str  # "live" | "retired"
    retired_reason: str | None = None
    ext: dict[str, Any] = field(default_factory=dict)


@dataclass
class Charter:
    mandate: str
    purposes: dict[str, Purpose] = field(default_factory=dict)


def _dict_to_tomlkit_table(d: dict[str, Any]) -> tomlkit.items.Table:
    table = tomlkit.table()
    for k in sorted(d.keys()):
        v = d[k]
        if isinstance(v, dict):
            table[k] = _dict_to_tomlkit_table(v)
        else:
            table[k] = v
    return table


def parse_charter(path: Path | str) -> Charter:
    """Parse a charter file using tomllib and validate required schema."""
    p = Path(path)
    if not p.is_file():
        raise CharterNotFoundError(f"Charter file not found: {p}")
    try:
        raw_text = p.read_text(encoding="utf-8")
        data = tomllib.loads(raw_text)
    except Exception as e:
        raise CharterInvalidError(f"Failed to parse charter TOML at {p}: {e}") from e

    mandate = data.get("mandate")
    if not isinstance(mandate, str):
        raise CharterInvalidError("Charter missing required 'mandate' string")

    purposes_raw = data.get("purpose", {})
    if not isinstance(purposes_raw, dict):
        raise CharterInvalidError("Charter 'purpose' section must be a table")

    purposes: dict[str, Purpose] = {}
    for pid, pdata in purposes_raw.items():
        if not isinstance(pdata, dict):
            raise CharterInvalidError(f"Purpose '{pid}' must be a table")
        text = pdata.get("text")
        if not isinstance(text, str):
            raise CharterInvalidError(f"Purpose '{pid}' missing required 'text' string")
        state = pdata.get("state")
        if state not in ("live", "retired"):
            raise CharterInvalidError(
                f"Purpose '{pid}' state must be 'live' or 'retired', got '{state}'"
            )
        retired_reason = pdata.get("retired_reason")
        if state == "retired" and (
            not retired_reason or not isinstance(retired_reason, str) or not retired_reason.strip()
        ):
            raise CharterInvalidError(
                f"Purpose '{pid}' is retired but missing required 'retired_reason'"
            )
        ext = pdata.get("ext", {})
        if not isinstance(ext, dict):
            raise CharterInvalidError(f"Purpose '{pid}' 'ext' must be a table")

        purposes[pid] = Purpose(
            id=pid,
            text=text,
            state=state,
            retired_reason=retired_reason if state == "retired" else None,
            ext=ext,
        )

    return Charter(mandate=mandate, purposes=purposes)


def dump_charter(charter: Charter) -> str:
    """Deterministically dump charter to TOML, sorting purposes and emitting ext as subtables."""
    doc = tomlkit.document()
    doc["mandate"] = charter.mandate
    doc.add(tomlkit.nl())

    purpose_table = tomlkit.table()
    for pid in sorted(charter.purposes.keys()):
        p = charter.purposes[pid]
        p_table = tomlkit.table()
        p_table["text"] = p.text
        p_table["state"] = p.state
        if p.state == "retired" and p.retired_reason:
            p_table["retired_reason"] = p.retired_reason
        if p.ext:
            p_table["ext"] = _dict_to_tomlkit_table(p.ext)
        purpose_table[pid] = p_table

    doc["purpose"] = purpose_table
    return tomlkit.dumps(doc)


def charter_sha256(path: Path | str) -> str:
    """Calculate SHA256 of raw bytes of charter file."""
    p = Path(path)
    return hashlib.sha256(p.read_bytes()).hexdigest()


def _resolve_project_slug(project_root: Path | str) -> str:
    env_slug = os.environ.get("BTH_PROJECT_SLUG")
    if env_slug:
        return env_slug
    p = Path(project_root)
    cfg_path = p / ".bth.toml"
    if cfg_path.is_file():
        try:
            from bathos.config import load_project_config

            return load_project_config(cfg_path).slug
        except Exception:
            pass
    return p.name


def _resolve_catalog_dir(catalog_dir: Path | None = None) -> Path:
    if catalog_dir is not None:
        return catalog_dir
    env_cat = os.environ.get("BTH_CATALOG_DIR")
    if env_cat:
        return Path(env_cat)
    return default_catalog_dir()


def emit_ledger_event(
    *,
    kind: str,
    slug: str,
    data: dict[str, Any],
    root: Path,
    cat_dir: Path,
    entity: list[str] | None = None,
) -> None:
    """Append one ledger event (charter or bearings) or raise.

    ``entity`` defaults to ``[slug]``; bearings events pass ``[slug, reading_id]``.

    Charter state lives only in the event log and its fold, so a catalog that is not in
    log mode could record the event but never fold it (status would stay
    ``never_confirmed`` and mandate immutability would not hold). Refuse instead.
    """
    with unit_of_work(cat_dir) as log_mode:
        if not log_mode:
            raise CharterLedgerUnavailableError(
                f"{kind}: the catalog at {cat_dir} is not in log mode, so the event "
                "could not be folded into the charter ledger"
            )
        outcome = emit_event(kind=kind, entity=entity or [slug], data=data, cwd=root)
        if not outcome.ok:
            raise CharterLedgerUnavailableError(
                f"{kind}: event could not be appended "
                f"(project_error={outcome.project_error!r}, "
                f"fallback_error={outcome.fallback_error!r})"
            )


def status(
    project_root: Path | str,
    *,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Query confirmation status of project's charter."""
    root = Path(project_root)
    charter_p = root / ".bth" / "charter.toml"
    if not charter_p.is_file():
        raise CharterNotFoundError(f"Charter file not found: {charter_p}")

    current_sha = charter_sha256(charter_p)
    cat_dir = _resolve_catalog_dir(catalog_dir)
    slug = _resolve_project_slug(root)

    state = "never_confirmed"
    last_confirmed_sha256 = None
    confirmed_at = None

    if catalog_readable(cat_dir):
        con = connect_read(cat_dir)
        try:
            row = con.execute(
                "SELECT charter_sha256, ts FROM charter_confirmations WHERE project = ? "
                "ORDER BY ts DESC, eid DESC LIMIT 1",
                [slug],
            ).fetchone()
            if row:
                last_confirmed_sha256 = row[0]
                confirmed_at = row[1]
                state = "confirmed" if last_confirmed_sha256 == current_sha else "unconfirmed"
        finally:
            con.close()

    return {
        "state": state,
        "current_sha256": current_sha,
        "last_confirmed_sha256": last_confirmed_sha256,
        "confirmed_at": confirmed_at,
    }


def confirm(
    project_root: Path | str,
    *,
    amend_mandate: bool = False,
    interactive_tty: bool | None = None,
    chair_token: str | None = None,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Confirm a project charter. Human-chair only and hash-bound."""
    try:
        auth_mode = require_human_chair(
            interactive_tty=interactive_tty, chair_token=chair_token
        )
    except (StepBackUnauthorizedChairError, StepBackAgentOverrideRefusedError) as e:
        raise CharterUnauthorizedError(str(e)) from e

    root = Path(project_root)
    charter_p = root / ".bth" / "charter.toml"
    if not charter_p.is_file():
        raise CharterNotFoundError(f"Charter file not found: {charter_p}")

    charter = parse_charter(charter_p)
    curr_sha = charter_sha256(charter_p)
    curr_mandate_sha = hashlib.sha256(charter.mandate.encode("utf-8")).hexdigest()
    slug = _resolve_project_slug(root)
    cat_dir = _resolve_catalog_dir(catalog_dir)

    amended_mandate_from = None
    if catalog_readable(cat_dir):
        con = connect_read(cat_dir)
        try:
            row = con.execute(
                "SELECT mandate_sha256 FROM charter_confirmations WHERE project = ? "
                "ORDER BY ts DESC, eid DESC LIMIT 1",
                [slug],
            ).fetchone()
            if row and row[0]:
                last_mandate_sha = row[0]
                if curr_mandate_sha != last_mandate_sha:
                    if not amend_mandate:
                        raise CharterMandateChangedError(
                            f"Mandate has changed from previously confirmed mandate "
                            f"({last_mandate_sha[:8]} -> {curr_mandate_sha[:8]}). "
                            "Pass --amend-mandate to confirm."
                        )
                    amended_mandate_from = last_mandate_sha
        finally:
            con.close()

    purposes_payload = {
        pid: {
            "id": p.id,
            "text": p.text,
            "state": p.state,
            "retired_reason": p.retired_reason,
            "ext": p.ext,
        }
        for pid, p in sorted(charter.purposes.items())
    }

    emit_ledger_event(
        kind="charter.confirmed",
        slug=slug,
        data={
            "charter_sha256": curr_sha,
            "mandate_sha256": curr_mandate_sha,
            "mandate_text": charter.mandate,
            "purposes": purposes_payload,
            "auth_mode": auth_mode,
            "amended_mandate_from": amended_mandate_from,
        },
        root=root,
        cat_dir=cat_dir,
    )

    return {
        "ok": True,
        "state": "confirmed",
        "charter_sha256": curr_sha,
        "auth_mode": auth_mode,
        "amended_mandate_from": amended_mandate_from,
    }


def _validate_cheapest_next_test(cnt: str) -> None:
    if cnt == "none_pending":
        return
    if cnt.startswith("executed:") and len(cnt) > len("executed:") and cnt[len("executed:"):].strip():
        return
    if cnt.startswith("cancelled:") and len(cnt) > len("cancelled:") and cnt[len("cancelled:"):].strip():
        return
    raise CharterInvalidError(
        f"Invalid cheapest_next_test {cnt!r}. Must match 'executed:<run>', 'cancelled:<reason>', or 'none_pending'."
    )


def pivot(
    project_root: Path | str,
    *,
    reason: str,
    dispositions: dict[str, Literal["reaffirm", "retire"]],
    retired_reasons: dict[str, str] | None = None,
    cheapest_next_test: str,
    catalog_dir: Path | None = None,
) -> dict[str, Any]:
    """Execute purpose ledger pivot. Every live purpose must be reaffirmed or retired."""
    _validate_cheapest_next_test(cheapest_next_test)

    root = Path(project_root)
    charter_p = root / ".bth" / "charter.toml"
    if not charter_p.is_file():
        raise CharterNotFoundError(f"Charter file not found: {charter_p}")

    charter = parse_charter(charter_p)
    charter_sha256_before = charter_sha256(charter_p)

    live_ids = {pid for pid, p in charter.purposes.items() if p.state == "live"}
    missing = sorted(live_ids - set(dispositions.keys()))
    if missing:
        raise CharterPivotIncompleteError(
            f"Pivot is incomplete: live purpose(s) {', '.join(missing)} not disposed. "
            "Every live purpose must be reaffirmed or retired."
        )

    unknown = sorted(set(dispositions.keys()) - set(charter.purposes.keys()))
    if unknown:
        raise CharterInvalidError(f"Unknown purpose(s) in dispositions: {', '.join(unknown)}")

    clean_retired_reasons: dict[str, str] = {}
    for pid, disp in dispositions.items():
        if disp not in ("reaffirm", "retire"):
            raise CharterInvalidError(
                f"Disposition for purpose '{pid}' must be 'reaffirm' or 'retire', got '{disp}'"
            )
        if disp == "retire":
            ret_reason = (retired_reasons or {}).get(pid, "")
            if not ret_reason or not str(ret_reason).strip():
                raise CharterInvalidError(
                    f"Purpose '{pid}' marked as retired but missing retired_reason"
                )
            clean_retired_reasons[pid] = str(ret_reason).strip()

    st_before = status(root, catalog_dir=catalog_dir)
    confirmed_before = st_before["state"] == "confirmed"

    # Edit the owner's file in place with tomlkit so comments, key order and any keys
    # bathos does not interpret survive the pivot. Only state / retired_reason change.
    doc = tomlkit.parse(charter_p.read_text(encoding="utf-8"))
    for pid, disp in dispositions.items():
        if disp == "retire":
            doc["purpose"][pid]["state"] = "retired"
            doc["purpose"][pid]["retired_reason"] = clean_retired_reasons[pid]
    new_bytes = tomlkit.dumps(doc).encode("utf-8")
    charter_sha256_after = hashlib.sha256(new_bytes).hexdigest()

    slug = _resolve_project_slug(root)
    cat_dir = _resolve_catalog_dir(catalog_dir)

    # Stage the new file beside the old one, record the event, then rename. A refusal
    # (non-log catalog) or a failed append leaves charter.toml untouched, so the file
    # and the ledger cannot diverge.
    tmp_p = charter_p.with_name(f".{charter_p.name}.pivot.tmp")
    tmp_p.write_bytes(new_bytes)
    try:
        emit_ledger_event(
            kind="charter.pivot",
            slug=slug,
            data={
                "reason": reason,
                "dispositions": dispositions,
                "retired_reasons": clean_retired_reasons,
                "cheapest_next_test": cheapest_next_test,
                "charter_sha256_before": charter_sha256_before,
                "charter_sha256_after": charter_sha256_after,
                "confirmed_before": confirmed_before,
            },
            root=root,
            cat_dir=cat_dir,
        )
    except BaseException:
        tmp_p.unlink(missing_ok=True)
        raise
    os.replace(tmp_p, charter_p)

    return {
        "ok": True,
        "state": "unconfirmed",
        "charter_sha256_before": charter_sha256_before,
        "charter_sha256_after": charter_sha256_after,
        "confirmed_before": confirmed_before,
    }


def draft(project_root: Path | str) -> Path:
    """Generate .bth/charter.draft.toml from existing charter or scaffold."""
    root = Path(project_root)
    draft_p = root / ".bth" / "charter.draft.toml"
    charter_p = root / ".bth" / "charter.toml"
    draft_p.parent.mkdir(parents=True, exist_ok=True)

    if charter_p.is_file():
        charter = parse_charter(charter_p)
        content = dump_charter(charter)
        draft_p.write_text(content, encoding="utf-8")
    else:
        draft_p.write_text(SCAFFOLD_TOML, encoding="utf-8")

    return draft_p
