"""Structured claim/citation appraisals and derived use_tier (spec 261015, §3.3.2 & S3).

Appraisals are append-only ledger records written only through ``create_appraisal``
(``bth appraise`` CLI and ``appraise`` MCP tool), emitting ``appraisal.created`` events
through ``emit_or_legacy``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
import tomllib
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq

from bathos.config import default_catalog_dir, find_project_config, load_project_config
from bathos.errors import BathosError, BathosErrorCode
from bathos.index import connect_read
from bathos.runlog.emit import emit_or_legacy, unit_of_work
from bathos.runlog.mode import is_log_mode
from bathos.workspace import resolve_workspace

# ── Ordinals (total, ascending; spec §3.3.2) ─────────────────────────────────

READ_DEPTH_ORDER: tuple[str, ...] = (
    "abstract",
    "full-text",
    "methods",
    "supplement",
    "data-or-code",
)
READ_DEPTH_RANK: dict[str, int] = {v: i for i, v in enumerate(READ_DEPTH_ORDER)}

BASIS_ORDER: tuple[str, ...] = (
    "asserted",
    "inspected",
    "executed",
    "reproduced",
    "independent",
)
BASIS_RANK: dict[str, int] = {v: i for i, v in enumerate(BASIS_ORDER)}

SEVERITY_ORDER: tuple[str, ...] = ("minor", "major", "decisive")
SEVERITY_RANK: dict[str, int] = {v: i for i, v in enumerate(SEVERITY_ORDER)}

USE_TIER_ORDER: tuple[str, ...] = (
    "refuted-for-use",
    "contested",
    "hypothesis-source",
    "threshold-source",
)
USE_TIER_RANK: dict[str, int] = {v: i for i, v in enumerate(USE_TIER_ORDER)}

USES_AS_MIN_TIER: dict[str, str] = {
    "contrast-context": "contested",
    "hypothesis-source": "hypothesis-source",
    "threshold-source": "threshold-source",
}

APPRAISER_KINDS: frozenset[str] = frozenset({"session", "subagent", "fork"})

# ── Closed category lists (v1; spec §3.3.2) ──────────────────────────────────

AGAINST_CATEGORIES: frozenset[str] = frozenset(
    {
        "small-n",
        "sampling-variance",
        "unconverged-sampling",
        "no-uncertainty-reported",
        "weak-or-untuned-baseline",
        "missing-control",
        "missing-ablation",
        "leakage-or-circularity",
        "researcher-degrees-of-freedom",
        "measurement-validity",
        "outlier-dominance",
        "scope-overreach",
        "not-reproduced",
        "artifact-unavailable",
        "single-source",
        "conflict-of-interest",
        "other",
    }
)

FOR_CATEGORIES: frozenset[str] = frozenset(
    {
        "preregistered",
        "independent-replication",
        "multi-seed-uncertainty",
        "effect-size-uncertainty",
        "controls-present",
        "external-validation",
        "artifact-available",
        "convergent-methods",
        "mechanistic-consistency",
        "other",
    }
)

# Rule 3 (R1): no combined score field may exist on an appraisal payload.
FORBIDDEN_SCORE_FIELDS: frozenset[str] = frozenset(
    {"confidence", "trust", "score", "overall_score", "rating", "lgtm"}
)

MIN_STATEMENT_CHARS = 30

# Rule 1: reject sentinel statements regardless of padding.
_SENTINEL_WHOLE_RE = re.compile(
    r"^\s*(?:none|n/?a|nothing)\s*"
    r"(?:[.!?,;:]+|\b(?:to\s+report|found|here|noted|identified|detected|at\s+all)\b.*)?$",
    re.IGNORECASE,
)
_SENTINEL_PHRASE_RE = re.compile(
    r"\b(?:no\s+(?:concerns?|issues?|problems?|objections?)|n/?a)\b",
    re.IGNORECASE,
)

_APPRAISAL_FRAGMENT_SCHEMA = pa.schema(
    [
        pa.field("id", pa.string()),
        pa.field("project_slug", pa.string()),
        pa.field("ref", pa.string()),
        pa.field("locator", pa.string()),
        pa.field("appraiser", pa.string()),
        pa.field("appraiser_session", pa.string()),
        pa.field("session_attested", pa.bool_()),
        pa.field("appraiser_agent_id", pa.string()),
        pa.field("appraiser_kind", pa.string()),
        pa.field("kind_attested", pa.bool_()),
        pa.field("read_depth", pa.string()),
        pa.field("blind_to_json", pa.string()),
        pa.field("scope_claimed", pa.string()),
        pa.field("scope_supported", pa.string()),
        pa.field("interpretation", pa.string()),
        pa.field("for_json", pa.string()),
        pa.field("against_json", pa.string()),
        pa.field("payload_sha256", pa.string()),
        pa.field("created_at", pa.string()),
        pa.field("eid", pa.string()),
    ]
)


# ── Domain Errors ────────────────────────────────────────────────────────────


class AppraisalError(BathosError):
    """Base error for appraisal validation and storage."""

    code = BathosErrorCode.INVALID_PARAM


class AppraisalValidationError(AppraisalError):
    """Raised when an appraisal payload violates spec §3.3.2 rules."""

    code = BathosErrorCode.DOCUMENT_INVALID


class AppraisalNotFoundError(AppraisalError):
    """Raised when a requested appraisal id does not exist."""

    code = BathosErrorCode.INVALID_PARAM


# ── Dataclasses ──────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ForEntry:
    """One argument supporting trust in the cited claim."""

    id: str
    category: str
    statement: str
    basis: str
    evidence: str = ""
    other_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "statement": self.statement,
            "basis": self.basis,
            "evidence": self.evidence,
        }
        if self.other_label:
            d["other_label"] = self.other_label
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ForEntry:
        return cls(
            id=str(raw.get("id", "")).strip(),
            category=str(raw.get("category", "")).strip(),
            statement=str(raw.get("statement", "")).strip(),
            basis=str(raw.get("basis", "")).strip(),
            evidence=str(raw.get("evidence", "")).strip(),
            other_label=str(raw.get("other_label", "")).strip(),
        )


@dataclass(frozen=True)
class AgainstEntry:
    """One argument against trusting the cited claim."""

    id: str
    category: str
    statement: str
    basis: str
    severity: str
    evidence: str = ""
    addressed_by: tuple[str, ...] = ()
    other_label: str = ""

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "id": self.id,
            "category": self.category,
            "statement": self.statement,
            "basis": self.basis,
            "severity": self.severity,
            "evidence": self.evidence,
            "addressed_by": list(self.addressed_by),
        }
        if self.other_label:
            d["other_label"] = self.other_label
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> AgainstEntry:
        raw_addr = raw.get("addressed_by") or []
        if isinstance(raw_addr, str):
            addr_tuple = tuple(x.strip() for x in raw_addr.split(",") if x.strip())
        else:
            addr_tuple = tuple(str(x).strip() for x in raw_addr if str(x).strip())
        return cls(
            id=str(raw.get("id", "")).strip(),
            category=str(raw.get("category", "")).strip(),
            statement=str(raw.get("statement", "")).strip(),
            basis=str(raw.get("basis", "")).strip(),
            severity=str(raw.get("severity", "")).strip(),
            evidence=str(raw.get("evidence", "")).strip(),
            addressed_by=addr_tuple,
            other_label=str(raw.get("other_label", "")).strip(),
        )


@dataclass(frozen=True)
class AppraisalRecord:
    """One persisted appraisal record in the appraisals ledger."""

    id: str
    project_slug: str
    ref: str
    locator: str
    appraiser: str
    appraiser_session: str
    session_attested: bool
    appraiser_agent_id: str
    appraiser_kind: str
    kind_attested: bool
    read_depth: str
    blind_to: tuple[str, ...]
    scope_claimed: str
    scope_supported: str
    interpretation: str
    for_entries: tuple[ForEntry, ...]
    against_entries: tuple[AgainstEntry, ...]
    payload_sha256: str
    created_at: str
    eid: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "appraisal_id": self.id,
            "project_slug": self.project_slug,
            "ref": self.ref,
            "locator": self.locator,
            "appraiser": self.appraiser,
            "appraiser_session": self.appraiser_session,
            "session_attested": self.session_attested,
            "appraiser_agent_id": self.appraiser_agent_id,
            "appraiser_kind": self.appraiser_kind,
            "kind_attested": self.kind_attested,
            "read_depth": self.read_depth,
            "blind_to": list(self.blind_to),
            "scope_claimed": self.scope_claimed,
            "scope_supported": self.scope_supported,
            "interpretation": self.interpretation,
            "for": [e.to_dict() for e in self.for_entries],
            "against": [e.to_dict() for e in self.against_entries],
            "payload_sha256": self.payload_sha256,
            "created_at": self.created_at,
            "eid": self.eid,
        }


@dataclass(frozen=True)
class UseTierResult:
    """Derived non-compensatory use tier over a citation's appraisals."""

    use_tier: str | None
    appraisal_count: int
    unaddressed_decisive: tuple[str, ...] = ()
    unaddressed_major: tuple[str, ...] = ()
    independence_verified: bool = False
    qualifying_appraisal_ids: tuple[str, ...] = ()
    unmet_for_threshold_source: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "use_tier": self.use_tier,
            "appraisal_count": self.appraisal_count,
            "unaddressed_decisive": list(self.unaddressed_decisive),
            "unaddressed_major": list(self.unaddressed_major),
            "independence_verified": self.independence_verified,
            "qualifying_appraisal_ids": list(self.qualifying_appraisal_ids),
            "unmet_for_threshold_source": list(self.unmet_for_threshold_source),
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


def _normalize_scope(text: str) -> str:
    """Normalize case, whitespace, and punctuation for Rule 5 scope comparison (C12)."""
    nfkc = unicodedata.normalize("NFKC", text).casefold()
    stripped_punct = "".join(
        " " if unicodedata.category(ch).startswith("P") else ch for ch in nfkc
    )
    return " ".join(stripped_punct.split())


def _validate_statement(stmt: str, label: str) -> None:
    s = stmt.strip()
    if len(s) < MIN_STATEMENT_CHARS:
        raise AppraisalValidationError(
            f"{label} statement must be at least {MIN_STATEMENT_CHARS} characters (got {len(s)})"
        )
    if _SENTINEL_WHOLE_RE.match(s) or _SENTINEL_PHRASE_RE.search(s):
        raise AppraisalValidationError(
            f"{label} statement looks like a 'none/N/A/no concerns' sentinel; "
            "name the strongest concrete or generic point instead"
        )


def _validate_entry_category(
    category: str,
    other_label: str,
    allowed: frozenset[str],
    label: str,
) -> None:
    if category not in allowed:
        raise AppraisalValidationError(
            f"{label} category {category!r} is not in closed list: {sorted(allowed)}"
        )
    if category == "other" and not other_label.strip():
        raise AppraisalValidationError(
            f"{label} with category='other' requires a non-empty 'other_label'"
        )
    if category != "other" and other_label.strip():
        raise AppraisalValidationError(
            f"{label} has other_label={other_label!r} but category is {category!r} (not 'other')"
        )


def _resolve_session_and_kind(
    *,
    appraiser: str,
    appraiser_session: str = "",
    appraiser_agent_id: str = "",
    appraiser_kind: str = "",
) -> tuple[str, bool, str, str, bool]:
    """Resolve appraiser session, agent id, and kind with attestation flags (spec §3.3.2, §7.8)."""
    env_session = (
        os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("BTH_AGENT_SESSION") or ""
    ).strip()
    explicit_session = appraiser_session.strip()

    if explicit_session:
        resolved_session = explicit_session
        session_attested = not (env_session and explicit_session == env_session)
    elif env_session:
        resolved_session = env_session
        session_attested = False
    elif appraiser.startswith("human:") and len(appraiser.split(":", 1)[1].strip()) > 0:
        resolved_session = appraiser
        session_attested = True
    else:
        raise AppraisalValidationError(
            "No CLAUDE_CODE_SESSION_ID or BTH_AGENT_SESSION in environment; "
            "pass appraiser_session explicitly (recorded as session_attested=true)"
        )

    env_agent_id = (
        os.environ.get("BTH_AGENT_ID")
        or os.environ.get("CLAUDE_AGENT_ID")
        or os.environ.get("SUBAGENT_ID")
        or ""
    ).strip()
    resolved_agent_id = appraiser_agent_id.strip() or env_agent_id

    env_kind = os.environ.get("BTH_APPRAISER_KIND", "").strip()
    explicit_kind = appraiser_kind.strip()
    if explicit_kind:
        resolved_kind = explicit_kind
    elif env_kind:
        resolved_kind = env_kind
    elif os.environ.get("CLAUDE_AGENT_ID") or os.environ.get("SUBAGENT_ID"):
        resolved_kind = "subagent"
    else:
        resolved_kind = "session"

    if resolved_kind not in APPRAISER_KINDS:
        raise AppraisalValidationError(
            f"appraiser_kind must be one of {sorted(APPRAISER_KINDS)}, got {resolved_kind!r}"
        )

    # Per §7.8: until hook injection of agent_id is verified, a subagent appraisal without a
    # distinct hook-injected agent_id is recorded as kind_attested=True.
    kind_attested = resolved_kind == "subagent" and not bool(env_agent_id)
    return resolved_session, session_attested, resolved_agent_id, resolved_kind, kind_attested


# ── Parsing & Validation ─────────────────────────────────────────────────────


def parse_appraisal_toml(path: Path | str) -> dict[str, Any]:
    """Load and parse an appraisal TOML file into a raw payload dict."""
    p = Path(path)
    if not p.is_file():
        raise AppraisalValidationError(f"Appraisal file not found: {p}")
    try:
        data = tomllib.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:
        raise AppraisalValidationError(f"Invalid appraisal TOML at {p}: {exc}") from exc
    if not isinstance(data, dict):
        raise AppraisalValidationError(f"Appraisal TOML at {p} must be a table")
    return data


def _validate_and_build_entries(
    raw_for: Any,
    raw_against: Any,
    *,
    lookup_appraisals: dict[str, AppraisalRecord] | None = None,
) -> tuple[tuple[ForEntry, ...], tuple[AgainstEntry, ...]]:
    if not isinstance(raw_for, list) or not raw_for:
        raise AppraisalValidationError("Appraisal must have a non-empty [[for]] list (Rule 1)")
    if not isinstance(raw_against, list) or not raw_against:
        raise AppraisalValidationError("Appraisal must have a non-empty [[against]] list (Rule 1)")

    for_entries: list[ForEntry] = []
    seen_for_ids: set[str] = set()
    for idx, item in enumerate(raw_for):
        if not isinstance(item, dict):
            raise AppraisalValidationError(f"for[{idx}] must be a table")
        if "severity" in item and str(item.get("severity") or "").strip():
            raise AppraisalValidationError(
                f"for[{idx}] must not declare 'severity' (Rule 4: strength on 'for' is carried by basis)"
            )
        for bad_key in FORBIDDEN_SCORE_FIELDS & set(item.keys()):
            raise AppraisalValidationError(
                f"for[{idx}] contains forbidden score field {bad_key!r} (Rule 3 / R1)"
            )
        entry = ForEntry.from_dict(item)
        if not entry.id:
            raise AppraisalValidationError(f"for[{idx}] missing required non-empty 'id'")
        if entry.id in seen_for_ids:
            raise AppraisalValidationError(f"Duplicate for entry id {entry.id!r}")
        seen_for_ids.add(entry.id)
        _validate_entry_category(entry.category, entry.other_label, FOR_CATEGORIES, f"for[{entry.id}]")
        if entry.basis not in BASIS_RANK:
            raise AppraisalValidationError(
                f"for[{entry.id}] basis {entry.basis!r} not in {BASIS_ORDER}"
            )
        _validate_statement(entry.statement, f"for[{entry.id}]")
        for_entries.append(entry)

    against_entries: list[AgainstEntry] = []
    seen_against_ids: set[str] = set()
    for idx, item in enumerate(raw_against):
        if not isinstance(item, dict):
            raise AppraisalValidationError(f"against[{idx}] must be a table")
        for bad_key in FORBIDDEN_SCORE_FIELDS & set(item.keys()):
            raise AppraisalValidationError(
                f"against[{idx}] contains forbidden score field {bad_key!r} (Rule 3 / R1)"
            )
        entry = AgainstEntry.from_dict(item)
        if not entry.id:
            raise AppraisalValidationError(f"against[{idx}] missing required non-empty 'id'")
        if entry.id in seen_against_ids:
            raise AppraisalValidationError(f"Duplicate against entry id {entry.id!r}")
        seen_against_ids.add(entry.id)
        _validate_entry_category(
            entry.category, entry.other_label, AGAINST_CATEGORIES, f"against[{entry.id}]"
        )
        if entry.basis not in BASIS_RANK:
            raise AppraisalValidationError(
                f"against[{entry.id}] basis {entry.basis!r} not in {BASIS_ORDER}"
            )
        if entry.severity not in SEVERITY_RANK:
            raise AppraisalValidationError(
                f"against[{entry.id}] severity {entry.severity!r} not in {SEVERITY_ORDER}"
            )
        _validate_statement(entry.statement, f"against[{entry.id}]")

        for target_ref in entry.addressed_by:
            if ":" in target_ref:
                target_appr_id, target_for_id = target_ref.split(":", 1)
                if not target_appr_id or not target_for_id:
                    raise AppraisalValidationError(
                        f"against[{entry.id}] malformed cross-record addressed_by {target_ref!r}"
                    )
                if lookup_appraisals is not None:
                    target_appr = lookup_appraisals.get(target_appr_id)
                    if target_appr is None:
                        raise AppraisalValidationError(
                            f"against[{entry.id}] addressed_by references unknown appraisal {target_appr_id!r}"
                        )
                    if not any(fe.id == target_for_id for fe in target_appr.for_entries):
                        raise AppraisalValidationError(
                            f"against[{entry.id}] addressed_by references unknown for entry "
                            f"{target_for_id!r} in appraisal {target_appr_id!r}"
                        )
            elif target_ref not in seen_for_ids:
                raise AppraisalValidationError(
                    f"against[{entry.id}] addressed_by references unknown local for entry {target_ref!r}"
                )

        against_entries.append(entry)

    return tuple(for_entries), tuple(against_entries)


def validate_appraisal_payload(
    payload: dict[str, Any],
    *,
    ref: str = "",
    locator: str = "",
    lookup_appraisals: dict[str, AppraisalRecord] | None = None,
) -> dict[str, Any]:
    """Validate an appraisal payload dict against spec §3.3.2 Rules 1-6 and return normalized fields."""
    if not isinstance(payload, dict):
        raise AppraisalValidationError("Appraisal payload must be a dict/table")

    forbidden = FORBIDDEN_SCORE_FIELDS & set(payload.keys())
    if forbidden:
        raise AppraisalValidationError(
            f"Combined score field(s) {sorted(forbidden)} are forbidden on appraisals (Rule 3 / R1)"
        )

    eff_ref = (ref or str(payload.get("ref", ""))).strip()
    if not eff_ref:
        raise AppraisalValidationError("Appraisal requires a non-empty 'ref'")
    eff_locator = (locator or str(payload.get("locator", ""))).strip()

    appraiser = str(payload.get("appraiser", "")).strip()
    if not appraiser or ":" not in appraiser:
        raise AppraisalValidationError(
            "Appraisal requires 'appraiser' in 'agent:<model>' or 'human:<handle>' format"
        )
    prefix, suffix = appraiser.split(":", 1)
    if prefix not in ("agent", "human") or not suffix.strip():
        raise AppraisalValidationError(
            f"Appraisal 'appraiser' must start with 'agent:' or 'human:', got {appraiser!r}"
        )

    read_depth = str(payload.get("read_depth", "")).strip()
    if read_depth not in READ_DEPTH_RANK:
        raise AppraisalValidationError(
            f"Appraisal 'read_depth' {read_depth!r} must be one of {READ_DEPTH_ORDER}"
        )

    raw_blind = payload.get("blind_to") or []
    if not isinstance(raw_blind, list):
        raise AppraisalValidationError("'blind_to' must be a list of appraisal ids")
    blind_to = tuple(str(x).strip() for x in raw_blind if str(x).strip())

    scope_claimed = str(payload.get("scope_claimed", "")).strip()
    scope_supported = str(payload.get("scope_supported", "")).strip()
    if not scope_claimed:
        raise AppraisalValidationError("Appraisal requires non-empty 'scope_claimed'")
    if not scope_supported:
        raise AppraisalValidationError("Appraisal requires non-empty 'scope_supported'")

    if read_depth in ("abstract", "full-text") and _normalize_scope(scope_supported) == _normalize_scope(
        scope_claimed
    ):
        raise AppraisalValidationError(
            "scope_supported must differ from scope_claimed (after case/whitespace/punctuation "
            "normalization) unless read_depth >= 'methods' (Rule 5)"
        )

    interpretation = str(payload.get("interpretation", "")).strip()
    raw_for = payload.get("for") if "for" in payload else payload.get("for_entries")
    raw_against = payload.get("against") if "against" in payload else payload.get("against_entries")
    for_entries, against_entries = _validate_and_build_entries(
        raw_for,
        raw_against,
        lookup_appraisals=lookup_appraisals,
    )

    return {
        "ref": eff_ref,
        "locator": eff_locator,
        "appraiser": appraiser,
        "read_depth": read_depth,
        "blind_to": blind_to,
        "scope_claimed": scope_claimed,
        "scope_supported": scope_supported,
        "interpretation": interpretation,
        "for_entries": for_entries,
        "against_entries": against_entries,
    }


# ── Persistence (emit_or_legacy + Parquet legacy fragments + index reads) ────


def _appraisal_fragments_dir(catalog_dir: Path, project_slug: str) -> Path:
    return catalog_dir / "appraisals" / project_slug


def _write_appraisal_fragment(record: AppraisalRecord, catalog_dir: Path) -> None:
    frag_dir = _appraisal_fragments_dir(catalog_dir, record.project_slug)
    frag_dir.mkdir(parents=True, exist_ok=True)
    target = frag_dir / f"{record.id}.parquet"
    tmp = frag_dir / f"{record.id}.tmp.parquet"

    table = pa.table(
        {
            "id": [record.id],
            "project_slug": [record.project_slug],
            "ref": [record.ref],
            "locator": [record.locator],
            "appraiser": [record.appraiser],
            "appraiser_session": [record.appraiser_session],
            "session_attested": [record.session_attested],
            "appraiser_agent_id": [record.appraiser_agent_id],
            "appraiser_kind": [record.appraiser_kind],
            "kind_attested": [record.kind_attested],
            "read_depth": [record.read_depth],
            "blind_to_json": [_canonical_json(list(record.blind_to))],
            "scope_claimed": [record.scope_claimed],
            "scope_supported": [record.scope_supported],
            "interpretation": [record.interpretation],
            "for_json": [_canonical_json([e.to_dict() for e in record.for_entries])],
            "against_json": [_canonical_json([e.to_dict() for e in record.against_entries])],
            "payload_sha256": [record.payload_sha256],
            "created_at": [record.created_at],
            "eid": [record.eid],
        },
        schema=_APPRAISAL_FRAGMENT_SCHEMA,
    )
    t_start = time.monotonic()
    pq.write_table(table, tmp)
    tmp.rename(target)
    _ = time.monotonic() - t_start


def _record_from_row(row: dict[str, Any]) -> AppraisalRecord:
    blind_raw = row.get("blind_to_json") or "[]"
    for_raw = row.get("for_json") or "[]"
    against_raw = row.get("against_json") or "[]"

    blind_list = json.loads(blind_raw) if isinstance(blind_raw, str) else list(blind_raw)
    for_list = json.loads(for_raw) if isinstance(for_raw, str) else list(for_raw)
    against_list = json.loads(against_raw) if isinstance(against_raw, str) else list(against_raw)

    return AppraisalRecord(
        id=str(row.get("id") or ""),
        project_slug=str(row.get("project_slug") or ""),
        ref=str(row.get("ref") or ""),
        locator=str(row.get("locator") or ""),
        appraiser=str(row.get("appraiser") or ""),
        appraiser_session=str(row.get("appraiser_session") or ""),
        session_attested=bool(row.get("session_attested", False)),
        appraiser_agent_id=str(row.get("appraiser_agent_id") or ""),
        appraiser_kind=str(row.get("appraiser_kind") or "session"),
        kind_attested=bool(row.get("kind_attested", False)),
        read_depth=str(row.get("read_depth") or ""),
        blind_to=tuple(str(x) for x in blind_list),
        scope_claimed=str(row.get("scope_claimed") or ""),
        scope_supported=str(row.get("scope_supported") or ""),
        interpretation=str(row.get("interpretation") or ""),
        for_entries=tuple(ForEntry.from_dict(e) for e in for_list),
        against_entries=tuple(AgainstEntry.from_dict(e) for e in against_list),
        payload_sha256=str(row.get("payload_sha256") or ""),
        created_at=str(row.get("created_at") or ""),
        eid=str(row.get("eid") or ""),
    )


def _read_legacy_appraisal_fragments(catalog_dir: Path) -> list[AppraisalRecord]:
    root = catalog_dir / "appraisals"
    if not root.is_dir():
        return []
    files = sorted(root.rglob("appr_*.parquet"))
    if not files:
        return []
    records: list[AppraisalRecord] = []
    for f in files:
        table = pq.read_table(f)
        d = table.to_pydict()
        for i in range(table.num_rows):
            row = {k: d[k][i] for k in d}
            records.append(_record_from_row(row))
    return records


def list_appraisals(
    *,
    ref: str | None = None,
    locator: str | None = None,
    category: str | None = None,
    catalog_dir: Path | str | None = None,
) -> list[AppraisalRecord]:
    """List appraisal records, optionally filtered by ref, locator, or category."""
    cd = _resolve_catalog_dir(catalog_dir)
    by_id: dict[str, AppraisalRecord] = {}

    con = connect_read(cd, wire_validity=False)
    try:
        cur = con.execute("SELECT * FROM appraisals")
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
        for rec in _read_legacy_appraisal_fragments(cd):
            if rec.id and rec.id not in by_id:
                by_id[rec.id] = rec

    out = list(by_id.values())
    if ref:
        out = [r for r in out if r.ref == ref]
    if locator:
        out = [r for r in out if r.locator == locator]
    if category:
        out = [
            r
            for r in out
            if any(e.category == category for e in r.for_entries)
            or any(e.category == category for e in r.against_entries)
        ]
    out.sort(key=lambda r: (r.created_at, r.id))
    return out


def get_appraisal(
    appraisal_id: str,
    *,
    catalog_dir: Path | str | None = None,
) -> AppraisalRecord | None:
    """Fetch a single appraisal by exact id or unambiguous prefix, or None."""
    if not appraisal_id:
        return None
    all_records = list_appraisals(catalog_dir=catalog_dir)
    for rec in all_records:
        if rec.id == appraisal_id:
            return rec
    matches = [rec for rec in all_records if rec.id.startswith(appraisal_id)]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AppraisalError(
            f"Appraisal id prefix {appraisal_id!r} is ambiguous: matches {[m.id for m in matches]}"
        )
    return None


def create_appraisal(
    *,
    ref: str = "",
    locator: str = "",
    file: Path | str | None = None,
    payload: dict[str, Any] | str | None = None,
    appraiser_session: str = "",
    appraiser_agent_id: str = "",
    appraiser_kind: str = "",
    project_root: Path | str | None = None,
    catalog_dir: Path | str | None = None,
) -> AppraisalRecord:
    """Validate and append an appraisal record to the appraisals ledger."""
    cd = _resolve_catalog_dir(catalog_dir)
    ws_root = _resolve_workspace_root(project_root)
    project_slug = _resolve_project_slug(ws_root)

    raw_payload: dict[str, Any]
    if file:
        file_p = Path(file)
        if not file_p.is_file() and not file_p.is_absolute() and (ws_root / file_p).is_file():
            file_p = ws_root / file_p
        raw_payload = parse_appraisal_toml(file_p)
    elif isinstance(payload, dict):
        raw_payload = dict(payload)
    elif isinstance(payload, str) and payload.strip():
        try:
            parsed = json.loads(payload)
        except json.JSONDecodeError:
            try:
                parsed = tomllib.loads(payload)
            except Exception as exc:
                raise AppraisalValidationError(f"Could not parse appraisal payload: {exc}") from exc
        if not isinstance(parsed, dict):
            raise AppraisalValidationError("Parsed appraisal payload must be an object/table")
        raw_payload = parsed
    else:
        raise AppraisalValidationError("Provide either 'file' (TOML path) or 'payload' (dict/JSON)")

    existing_map = {a.id: a for a in list_appraisals(catalog_dir=cd)}
    norm = validate_appraisal_payload(
        raw_payload,
        ref=ref,
        locator=locator,
        lookup_appraisals=existing_map,
    )

    (
        resolved_session,
        session_attested,
        resolved_agent_id,
        resolved_kind,
        kind_attested,
    ) = _resolve_session_and_kind(
        appraiser=norm["appraiser"],
        appraiser_session=appraiser_session,
        appraiser_agent_id=appraiser_agent_id,
        appraiser_kind=appraiser_kind,
    )

    appraisal_id = f"appr_{uuid.uuid4().hex[:12]}"
    created_at = _now_iso()

    for_dicts = [e.to_dict() for e in norm["for_entries"]]
    against_dicts = [e.to_dict() for e in norm["against_entries"]]
    canonical_payload = _canonical_json(
        {
            "ref": norm["ref"],
            "locator": norm["locator"],
            "appraiser": norm["appraiser"],
            "read_depth": norm["read_depth"],
            "blind_to": list(norm["blind_to"]),
            "scope_claimed": norm["scope_claimed"],
            "scope_supported": norm["scope_supported"],
            "interpretation": norm["interpretation"],
            "for": for_dicts,
            "against": against_dicts,
        }
    )
    payload_sha256 = hashlib.sha256(canonical_payload.encode("utf-8")).hexdigest()

    record = AppraisalRecord(
        id=appraisal_id,
        project_slug=project_slug,
        ref=norm["ref"],
        locator=norm["locator"],
        appraiser=norm["appraiser"],
        appraiser_session=resolved_session,
        session_attested=session_attested,
        appraiser_agent_id=resolved_agent_id,
        appraiser_kind=resolved_kind,
        kind_attested=kind_attested,
        read_depth=norm["read_depth"],
        blind_to=norm["blind_to"],
        scope_claimed=norm["scope_claimed"],
        scope_supported=norm["scope_supported"],
        interpretation=norm["interpretation"],
        for_entries=norm["for_entries"],
        against_entries=norm["against_entries"],
        payload_sha256=payload_sha256,
        created_at=created_at,
    )

    event_data: dict[str, Any] = {
        "id": record.id,
        "appraisal_id": record.id,
        "project_slug": record.project_slug,
        "ref": record.ref,
        "locator": record.locator,
        "appraiser": record.appraiser,
        "appraiser_session": record.appraiser_session,
        "session_attested": record.session_attested,
        "appraiser_agent_id": record.appraiser_agent_id,
        "appraiser_kind": record.appraiser_kind,
        "kind_attested": record.kind_attested,
        "read_depth": record.read_depth,
        "blind_to": list(record.blind_to),
        "scope_claimed": record.scope_claimed,
        "scope_supported": record.scope_supported,
        "interpretation": record.interpretation,
        "for_entries": for_dicts,
        "against_entries": against_dicts,
        "payload_sha256": record.payload_sha256,
        "created_at": record.created_at,
    }

    with unit_of_work(cd):
        outcome = emit_or_legacy(
            kind="appraisal.created",
            entity=[record.id],
            data=event_data,
            legacy_write=lambda: _write_appraisal_fragment(record, cd),
            cwd=ws_root,
        )
    if outcome is not None and not outcome.ok:
        raise AppraisalError(
            f"appraisal.created could not be recorded: "
            f"project_error={outcome.project_error!r}, fallback_error={outcome.fallback_error!r}"
        )

    return record


# ── Evaluation: addressed_by, independence, use_tier (spec §3.3.2) ───────────


def _resolve_for_ref(
    ref_str: str,
    owning_appraisal: AppraisalRecord,
    lookup: dict[str, AppraisalRecord],
) -> tuple[AppraisalRecord, ForEntry] | None:
    if ":" in ref_str:
        appr_id, for_id = ref_str.split(":", 1)
        target_appr = lookup.get(appr_id)
        if target_appr is None:
            return None
        for fe in target_appr.for_entries:
            if fe.id == for_id:
                return target_appr, fe
        return None
    for fe in owning_appraisal.for_entries:
        if fe.id == ref_str:
            return owning_appraisal, fe
    return None


def is_entry_addressed(
    entry: AgainstEntry,
    owning_appraisal: AppraisalRecord,
    lookup: dict[str, AppraisalRecord],
) -> bool:
    """True iff ``entry`` satisfies Rule 4 (incl. C12 self-rebuttal limit)."""
    if not entry.addressed_by:
        return False

    inspected_rank = BASIS_RANK["inspected"]
    executed_rank = BASIS_RANK["executed"]
    high_severity = entry.severity in ("major", "decisive")

    for target_ref in entry.addressed_by:
        resolved = _resolve_for_ref(target_ref, owning_appraisal, lookup)
        if resolved is None:
            return False
        target_appr, for_entry = resolved
        if BASIS_RANK.get(for_entry.basis, -1) < inspected_rank:
            return False
        if high_severity:
            # C12: decisive or major is addressed only by a `for` entry from a different
            # appraisal record with a different appraiser_session, OR basis >= executed.
            different_independent_record = (
                target_appr.id != owning_appraisal.id
                and target_appr.appraiser_session != owning_appraisal.appraiser_session
            )
            executed_or_better = BASIS_RANK.get(for_entry.basis, -1) >= executed_rank
            if not (different_independent_record or executed_or_better):
                return False

    return True


def is_independent_appraisal(
    appraisal: AppraisalRecord,
    citing_session: str | None,
    *,
    citing_agent_id: str | None = None,
) -> bool:
    """True iff ``appraisal`` is independent of ``citing_session`` per §3.3.2 & §7.8.

    - A ``fork`` does NOT count for its parent session (when citing_session matches or is unknown).
    - A fresh ``subagent`` counts even when sharing ``CLAUDE_CODE_SESSION_ID`` (unless same agent_id).
    - A ``session`` appraisal counts iff ``citing_session`` is known and differs.
    """
    if appraisal.appraiser_kind == "fork":
        return bool(citing_session and appraisal.appraiser_session != citing_session)

    if appraisal.appraiser_kind == "subagent":
        return not (
            citing_agent_id
            and appraisal.appraiser_agent_id
            and appraisal.appraiser_agent_id == citing_agent_id
        )

    # appraiser_kind == "session"
    if not citing_session:
        return False
    return appraisal.appraiser_session != citing_session


def compute_use_tier(
    appraisals: list[AppraisalRecord],
    *,
    value_provenance: str = "",
    citing_session: str | None = None,
    citing_agent_id: str | None = None,
    lookup_appraisals: dict[str, AppraisalRecord] | None = None,
) -> UseTierResult:
    """Compute derived, non-compensatory ``use_tier`` over a citation's appraisals (spec §3.3.2)."""
    if not appraisals:
        return UseTierResult(
            use_tier=None,
            appraisal_count=0,
            unmet_for_threshold_source=("no appraisals recorded",),
        )

    lookup: dict[str, AppraisalRecord] = dict(lookup_appraisals or {})
    for a in appraisals:
        lookup[a.id] = a

    unaddressed_decisive: list[str] = []
    unaddressed_major: list[str] = []
    for a in appraisals:
        for ag in a.against_entries:
            if not is_entry_addressed(ag, a, lookup):
                ref_id = f"{a.id}:{ag.id}"
                if ag.severity == "decisive":
                    unaddressed_decisive.append(ref_id)
                elif ag.severity == "major":
                    unaddressed_major.append(ref_id)

    if unaddressed_decisive:
        return UseTierResult(
            use_tier="refuted-for-use",
            appraisal_count=len(appraisals),
            unaddressed_decisive=tuple(unaddressed_decisive),
            unaddressed_major=tuple(unaddressed_major),
            unmet_for_threshold_source=("unaddressed decisive objection(s)",),
        )

    if unaddressed_major:
        return UseTierResult(
            use_tier="contested",
            appraisal_count=len(appraisals),
            unaddressed_major=tuple(unaddressed_major),
            unmet_for_threshold_source=("unaddressed major objection(s)",),
        )

    # Otherwise at least hypothesis-source; check threshold-source requirements.
    methods_rank = READ_DEPTH_RANK["methods"]
    inspected_rank = BASIS_RANK["inspected"]

    qualifying_ids: list[str] = []
    any_depth_ok = False
    any_basis_ok = False
    any_independent = False

    for a in appraisals:
        depth_ok = READ_DEPTH_RANK.get(a.read_depth, -1) >= methods_rank
        basis_ok = any(BASIS_RANK.get(fe.basis, -1) >= inspected_rank for fe in a.for_entries)
        indep_ok = is_independent_appraisal(
            a, citing_session, citing_agent_id=citing_agent_id
        )
        if depth_ok:
            any_depth_ok = True
        if basis_ok:
            any_basis_ok = True
        if indep_ok:
            any_independent = True
        if depth_ok and basis_ok and indep_ok:
            qualifying_ids.append(a.id)

    provenance_ok = value_provenance in ("published", "derived")
    unmet: list[str] = []
    if not provenance_ok:
        unmet.append(
            f"value_provenance={value_provenance!r} must be 'published' or 'derived'"
        )
    if not any_depth_ok:
        unmet.append("requires read_depth >= 'methods'")
    if not any_basis_ok:
        unmet.append("requires at least one 'for' entry with basis >= 'inspected'")
    if not any_independent:
        unmet.append("requires independent appraiser session (or fresh subagent, not fork)")
    elif not qualifying_ids:
        unmet.append(
            "no single independent appraisal meets both read_depth >= 'methods' and for.basis >= 'inspected'"
        )

    tier = "threshold-source" if (provenance_ok and qualifying_ids) else "hypothesis-source"
    return UseTierResult(
        use_tier=tier,
        appraisal_count=len(appraisals),
        independence_verified=bool(qualifying_ids),
        qualifying_appraisal_ids=tuple(qualifying_ids),
        unmet_for_threshold_source=tuple(unmet),
    )


def validate_uses_as(uses_as: str, tier_result: UseTierResult) -> None:
    """Fail if ``uses_as`` demands a higher tier than ``tier_result.use_tier`` licenses."""
    if not uses_as:
        return
    if uses_as not in USES_AS_MIN_TIER:
        raise AppraisalValidationError(
            f"uses_as {uses_as!r} must be one of {sorted(USES_AS_MIN_TIER)}"
        )
    required_tier = USES_AS_MIN_TIER[uses_as]
    actual_tier = tier_result.use_tier
    if actual_tier is None or USE_TIER_RANK.get(actual_tier, -1) < USE_TIER_RANK[required_tier]:
        reasons = "; ".join(tier_result.unmet_for_threshold_source) or "insufficient appraisals"
        raise AppraisalValidationError(
            f"uses_as={uses_as!r} requires tier >= {required_tier!r}, "
            f"but appraisals yield {actual_tier!r} ({reasons})"
        )


def summarize_other_labels(
    *,
    catalog_dir: Path | str | None = None,
) -> list[dict[str, Any]]:
    """Aggregate ``category='other'`` + ``other_label`` counts across all appraisals (spec §3.3.2 Rule 6)."""
    records = list_appraisals(catalog_dir=catalog_dir)
    labels: dict[tuple[str, str], str] = {}
    counts: dict[tuple[str, str], int] = {}
    appr_ids: dict[tuple[str, str], list[str]] = {}
    for rec in records:
        for side, entries in (("for", rec.for_entries), ("against", rec.against_entries)):
            for entry in entries:
                if entry.category != "other":
                    continue
                norm_label = entry.other_label.strip()
                key = (side, norm_label.casefold())
                labels.setdefault(key, norm_label)
                counts[key] = counts.get(key, 0) + 1
                id_list = appr_ids.setdefault(key, [])
                if rec.id not in id_list:
                    id_list.append(rec.id)

    summary = [
        {
            "side": key[0],
            "other_label": labels[key],
            "count": counts[key],
            "appraisal_ids": appr_ids[key],
        }
        for key in counts
    ]
    return sorted(summary, key=lambda x: (-int(x["count"]), str(x["side"]), str(x["other_label"])))


__all__ = [
    "AGAINST_CATEGORIES",
    "APPRAISER_KINDS",
    "BASIS_ORDER",
    "BASIS_RANK",
    "FORBIDDEN_SCORE_FIELDS",
    "FOR_CATEGORIES",
    "MIN_STATEMENT_CHARS",
    "READ_DEPTH_ORDER",
    "READ_DEPTH_RANK",
    "SEVERITY_ORDER",
    "SEVERITY_RANK",
    "USES_AS_MIN_TIER",
    "USE_TIER_ORDER",
    "USE_TIER_RANK",
    "AgainstEntry",
    "AppraisalError",
    "AppraisalNotFoundError",
    "AppraisalRecord",
    "AppraisalValidationError",
    "ForEntry",
    "UseTierResult",
    "compute_use_tier",
    "create_appraisal",
    "get_appraisal",
    "is_entry_addressed",
    "is_independent_appraisal",
    "list_appraisals",
    "parse_appraisal_toml",
    "summarize_other_labels",
    "validate_appraisal_payload",
    "validate_uses_as",
]
