"""Findings register, automated downgrade ladder, and reopen guard.

Spec 261106 Phase 2 (D7, OD-6):
- .bth/findings/<slug>.toml CRUD
- Dynamic read-time downgrade ladder:
  - When all supporting runs are hidden, finding status resolves to "needs-recheck"
  - Stored status on disk is NEVER mutated
  - "open" findings with [] supporting_runs stay "open"
  - "superseded" findings stay "superseded"
- Settling gate (status == "settled"):
  - SPIKE_TIER_CANNOT_SETTLE: spike-only active runs cannot settle
  - FINDING_SETTLED_EVIDENCE_INSUFFICIENT: requires >= 1 active confirmatory run
  - ESTIMAND_BOUNDARY_MIXED (OD-6): cannot pool across estimand epochs without allow_mixed_estimand
  - Activating supersession edge on predecessor when settled
- Reopen guard:
  - Creating finding or experiment matching settled question without reopen_reason is refused
  - Preserves settled predecessor until successor reaches settled
"""

from __future__ import annotations

import contextlib
import json
import os
import tomllib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from bathos.config import default_catalog_dir
from bathos.errors import (
    EstimandBoundaryMixedError,
    FindingNotFoundError,
    FindingReopenReasonRequiredError,
    FindingSettledEvidenceInsufficientError,
    SpikeTierCannotSettleError,
    SupersededTargetNotFoundError,
)
from bathos.index import record_acknowledgment
from bathos.supersession import _entity_exists, add_supersession_edge
from bathos.validity import resolve_validity
from bathos.workspace import resolve_workspace


@dataclass
class Finding:
    id: str
    question_slug: str
    question: str
    answer: str
    status: str  # "open" | "tentative" | "settled" | "superseded" | "refuted" | "needs-recheck"
    supporting_runs: list[str] = field(default_factory=list)
    evidence_tier: str = "exploratory"
    supersedes: str = ""
    reopen_reason: str = ""
    allow_mixed_estimand: str = ""
    declared_at: str = ""
    declared_by: str = ""
    last_reviewed: str = ""
    file_path: Path | None = None
    # Dynamic fields
    active_status: str = ""
    superseded_by: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "question_slug": self.question_slug,
            "question": self.question,
            "answer": self.answer,
            "status": self.active_status or self.status,
            "stored_status": self.status,
            "supporting_runs": self.supporting_runs,
            "evidence_tier": self.evidence_tier,
            "supersedes": self.supersedes,
            "superseded_by": self.superseded_by,
            "reopen_reason": self.reopen_reason,
            "allow_mixed_estimand": self.allow_mixed_estimand,
            "declared_at": self.declared_at,
            "declared_by": self.declared_by,
            "last_reviewed": self.last_reviewed,
            "path": str(self.file_path) if self.file_path else "",
        }

    def render_toml(self) -> str:
        lines = [
            "# Schema: bathos-finding-v1.0",
            f'id = "{self.id}"',
            f'question_slug = "{self.question_slug}"',
            f"question = {json.dumps(self.question)}",
            f"answer = {json.dumps(self.answer)}",
            f'status = "{self.status}"',
            f"supporting_runs = {self.supporting_runs!r}",
            f'evidence_tier = "{self.evidence_tier}"',
            f'supersedes = "{self.supersedes}"',
            f"reopen_reason = {json.dumps(self.reopen_reason)}",
            f"allow_mixed_estimand = {json.dumps(self.allow_mixed_estimand)}",
            f'declared_at = "{self.declared_at}"',
            f'declared_by = "{self.declared_by}"',
            f'last_reviewed = "{self.last_reviewed}"',
            "",
        ]
        return "\n".join(lines)


def get_findings_dir(workspace_root: Path | str | None = None) -> Path:
    if workspace_root:
        return Path(workspace_root).resolve() / ".bth" / "findings"
    ws = resolve_workspace()
    return ws.fs_root / ".bth" / "findings"


def parse_finding_file(path: Path) -> Finding:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    st = str(data.get("status", "tentative"))
    return Finding(
        id=str(data.get("id", "")),
        question_slug=str(data.get("question_slug", "")),
        question=str(data.get("question", "")),
        answer=str(data.get("answer", "")),
        status=st,
        supporting_runs=[str(r) for r in data.get("supporting_runs", [])],
        evidence_tier=str(data.get("evidence_tier", "exploratory")),
        supersedes=str(data.get("supersedes", "")),
        reopen_reason=str(data.get("reopen_reason", "")),
        allow_mixed_estimand=str(data.get("allow_mixed_estimand", "")),
        declared_at=str(data.get("declared_at", "")),
        declared_by=str(data.get("declared_by", "")),
        last_reviewed=str(data.get("last_reviewed", "")),
        file_path=path,
        active_status=st,
    )


def _normalize_supersedes(
    supersedes: str | list[str] | None,
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> list[str]:
    """Resolve supersedes argument (string, comma-separated string, or list of strings) to canonical finding UUIDs."""
    if not supersedes:
        return []
    if isinstance(supersedes, str):
        raw_items = [s.strip() for s in supersedes.split(",") if s.strip()]
    else:
        raw_items = []
        for s in supersedes:
            if isinstance(s, str):
                raw_items.extend([x.strip() for x in s.split(",") if x.strip()])
    resolved_ids: list[str] = []
    for item in raw_items:
        if not item:
            continue
        try:
            target_finding = get_finding(item, catalog_dir=catalog_dir, workspace_root=workspace_root)
            resolved_ids.append(target_finding.id)
        except Exception:
            resolved_ids.append(item)
    return resolved_ids


def resolve_finding_status(
    finding: Finding,
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> str:
    """Dynamically resolve finding status at read time without mutating disk."""
    try:
        from bathos.supersession import get_superseded_by

        succs = get_superseded_by("finding", finding.id, catalog_dir=catalog_dir)
        if not succs and finding.question_slug:
            succs = get_superseded_by("finding", finding.question_slug, catalog_dir=catalog_dir)
        if succs:
            finding.superseded_by = succs[0]["superseded_by_id"]
            return "superseded"
    except Exception:
        pass

    if finding.status == "superseded":
        return "superseded"

    if finding.status == "open" and not finding.supporting_runs:
        return "open"

    if finding.supporting_runs:
        cd = Path(catalog_dir or default_catalog_dir())
        validity_map = resolve_validity(
            finding.supporting_runs, catalog_dir=cd, workspace_root=workspace_root
        )
        if all(v.is_hidden for v in validity_map.values()):
            return "needs-recheck"

    return finding.status


def create_finding(
    question_slug: str,
    question: str,
    answer: str,
    supporting_runs: list[str] | None = None,
    status: str = "tentative",
    supersedes: str | list[str] = "",
    reopen_reason: str = "",
    allow_mixed_estimand: str = "",
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
    declared_by: str | None = None,
) -> Finding:
    runs = list(supporting_runs or [])
    cd = Path(catalog_dir or default_catalog_dir())
    ws_root = Path(workspace_root) if workspace_root else None
    fdir = get_findings_dir(ws_root)
    fdir.mkdir(parents=True, exist_ok=True)

    # 1. Reopen Guard: Check for settled findings on same question
    existing_settled: Finding | None = None
    if fdir.exists():
        for p in fdir.glob("*.toml"):
            try:
                f = parse_finding_file(p)
                if f.question_slug == question_slug or (
                    f.question and f.question.strip().lower() == question.strip().lower()
                ):
                    active_st = resolve_finding_status(
                        f, catalog_dir=cd, workspace_root=ws_root
                    )
                    if active_st == "settled":
                        existing_settled = f
                        break
            except Exception:
                pass

    if existing_settled is not None:
        if not reopen_reason.strip():
            raise FindingReopenReasonRequiredError(
                f"Question '{question_slug}' is already settled: '{existing_settled.answer}'. "
                "Provide --reopen-reason explaining why prior confirmatory evidence is being challenged.",
                question_slug=question_slug,
                settled_finding_id=existing_settled.id,
                settled_answer=existing_settled.answer,
            )
        supersedes = existing_settled.id
        if status != "settled":
            status = "tentative"

    resolved_supersedes_ids = _normalize_supersedes(supersedes, catalog_dir=cd, workspace_root=ws_root)
    for s_id in resolved_supersedes_ids:
        if not _entity_exists("finding", s_id, cd, ws_root):
            raise SupersededTargetNotFoundError("finding", s_id)

    supersedes_stored = (
        resolved_supersedes_ids[0]
        if len(resolved_supersedes_ids) == 1
        else ",".join(resolved_supersedes_ids)
        if resolved_supersedes_ids
        else ""
    )

    finding_id = f"finding-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}-{uuid.uuid4().hex[:4]}"
    now_iso = datetime.now(UTC).isoformat()
    actor_str = declared_by or os.environ.get("USER", "unknown")

    # 2. Settling Gate Checks
    if status == "settled":
        if not runs:
            raise FindingSettledEvidenceInsufficientError(
                "Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled.",
                finding_id=finding_id,
                supporting_runs=[],
            )

        validity_map = resolve_validity(runs, catalog_dir=cd, workspace_root=ws_root)
        active_runs = [v for v in validity_map.values() if v.state == "current"]

        # Spike tier cannot settle
        if active_runs and all(v.evidence_tier == "spike" for v in active_runs):
            raise SpikeTierCannotSettleError(
                "Spike runs are preliminary probes and cannot support a settled finding. Elevate evidence via a confirmatory run.",
                finding_id=finding_id,
                spike_runs=[v.run_id for v in active_runs],
            )

        # Require >= 1 active confirmatory run
        if not any(v.evidence_tier == "confirmatory" for v in active_runs):
            raise FindingSettledEvidenceInsufficientError(
                "Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled.",
                finding_id=finding_id,
                supporting_runs=runs,
            )

        # OD-6 Estimand Boundary Check
        markers_by_pkg: dict[str, set[str]] = {}
        for mid, v in validity_map.items():
            if v.estimand_marker:
                pkg = v.estimand_marker.split(":")[0]
                markers_by_pkg.setdefault(pkg, set()).add(v.estimand_marker)

        for pkg, markers in markers_by_pkg.items():
            if len(markers) > 1:
                if not allow_mixed_estimand.strip():
                    raise EstimandBoundaryMixedError(
                        f"Supporting runs span multiple estimand epochs for {pkg}: {sorted(markers)}. "
                        "Pass --allow-mixed-estimand '<reason>' to justify pooling across the boundary.",
                        epochs=sorted(markers),
                    )
                record_acknowledgment(
                    cd,
                    kind="validity.estimand_override_recorded",
                    entity_type="finding",
                    entity_id=finding_id,
                    run_ids=runs,
                    surface="finding_settle",
                    reason=allow_mixed_estimand.strip(),
                )

    # Resolve overall evidence_tier
    evidence_tier = "exploratory"
    if runs:
        validity_map = resolve_validity(runs, catalog_dir=cd, workspace_root=ws_root)
        tiers = {v.evidence_tier for v in validity_map.values() if not v.is_hidden}
        if "confirmatory" in tiers:
            evidence_tier = "confirmatory"
        elif "calibrating" in tiers:
            evidence_tier = "calibrating"
        elif "spike" in tiers:
            evidence_tier = "spike"

    target_file = fdir / f"{question_slug}.toml"
    if target_file.exists():
        try:
            existing = parse_finding_file(target_file)
            if existing.id != finding_id:
                target_file = fdir / f"{question_slug}-{finding_id[8:12]}.toml"
        except Exception:
            target_file = fdir / f"{question_slug}-{finding_id[8:12]}.toml"

    finding = Finding(
        id=finding_id,
        question_slug=question_slug,
        question=question,
        answer=answer,
        status=status,
        supporting_runs=runs,
        evidence_tier=evidence_tier,
        supersedes=supersedes_stored,
        reopen_reason=reopen_reason,
        allow_mixed_estimand=allow_mixed_estimand,
        declared_at=now_iso,
        declared_by=actor_str,
        last_reviewed=now_iso,
        file_path=target_file,
        active_status=status,
    )
    target_file.write_text(finding.render_toml(), encoding="utf-8")

    # Activate supersession on predecessor if specified and settled
    if status == "settled" and resolved_supersedes_ids:
        for s_id in resolved_supersedes_ids:
            add_supersession_edge(
                "finding",
                s_id,
                "finding",
                finding.id,
                reason=f"superseded by settled finding {finding.id}",
                catalog_dir=cd,
                workspace_root=workspace_root,
            )

    return finding


def update_finding(
    slug_or_id: str,
    *,
    status: str | None = None,
    answer: str | None = None,
    supporting_runs: list[str] | None = None,
    supersedes: str | list[str] | None = None,
    allow_mixed_estimand: str = "",
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
    reviewed_by: str | None = None,
) -> Finding:
    """Update an existing finding and persist changes to disk.

    Enforces settling gate (Spec §1.4.A, §4.4, §4.8, OD-6):
    - When transitioning to 'settled':
      - Requires >= 1 active confirmatory run (FindingSettledEvidenceInsufficientError)
      - Spike runs cannot settle (SpikeTierCannotSettleError)
      - Enforces OD-6 estimand boundary check across supporting runs (EstimandBoundaryMixedError)
      - Activates predecessor supersession edge on 'supersedes' finding
    """
    cd = Path(catalog_dir or default_catalog_dir())
    ws_root = Path(workspace_root) if workspace_root else None

    # Load existing finding
    finding = get_finding(slug_or_id, catalog_dir=cd, workspace_root=ws_root)
    if finding.file_path is None or not finding.file_path.exists():
        raise FindingNotFoundError(f"Finding '{slug_or_id}' file not found.", slug_or_id=slug_or_id)

    target_status = status if status is not None else finding.status
    target_answer = answer if answer is not None else finding.answer
    runs = list(supporting_runs) if supporting_runs is not None else list(finding.supporting_runs)
    mixed_estimand_reason = allow_mixed_estimand if allow_mixed_estimand else finding.allow_mixed_estimand

    resolved_supersedes_ids: list[str] = []
    if supersedes is not None:
        resolved_supersedes_ids = _normalize_supersedes(supersedes, catalog_dir=cd, workspace_root=ws_root)
        for s_id in resolved_supersedes_ids:
            if not _entity_exists("finding", s_id, cd, ws_root):
                raise SupersededTargetNotFoundError("finding", s_id)
        finding.supersedes = (
            resolved_supersedes_ids[0]
            if len(resolved_supersedes_ids) == 1
            else ",".join(resolved_supersedes_ids)
            if resolved_supersedes_ids
            else ""
        )
    elif finding.supersedes:
        resolved_supersedes_ids = _normalize_supersedes(finding.supersedes, catalog_dir=cd, workspace_root=ws_root)

    # Settling Gate Checks
    if target_status == "settled":
        if not runs:
            raise FindingSettledEvidenceInsufficientError(
                "Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled.",
                finding_id=finding.id,
                supporting_runs=[],
            )

        validity_map = resolve_validity(runs, catalog_dir=cd, workspace_root=ws_root)
        active_runs = [v for v in validity_map.values() if v.state == "current"]

        # Spike tier cannot settle
        if active_runs and all(v.evidence_tier == "spike" for v in active_runs):
            raise SpikeTierCannotSettleError(
                "Spike runs are preliminary probes and cannot support a settled finding. Elevate evidence via a confirmatory run.",
                finding_id=finding.id,
                spike_runs=[v.run_id for v in active_runs],
            )

        # Require >= 1 active confirmatory run
        if not any(v.evidence_tier == "confirmatory" for v in active_runs):
            raise FindingSettledEvidenceInsufficientError(
                "Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled.",
                finding_id=finding.id,
                supporting_runs=runs,
            )

        # OD-6 Estimand Boundary Check
        markers_by_pkg: dict[str, set[str]] = {}
        for mid, v in validity_map.items():
            if v.estimand_marker:
                pkg = v.estimand_marker.split(":")[0]
                markers_by_pkg.setdefault(pkg, set()).add(v.estimand_marker)

        for pkg, markers in markers_by_pkg.items():
            if len(markers) > 1:
                if not mixed_estimand_reason.strip():
                    raise EstimandBoundaryMixedError(
                        f"Supporting runs span multiple estimand epochs for {pkg}: {sorted(markers)}. "
                        "Pass --allow-mixed-estimand '<reason>' to justify pooling across the boundary.",
                        epochs=sorted(markers),
                    )
                record_acknowledgment(
                    cd,
                    kind="validity.estimand_override_recorded",
                    entity_type="finding",
                    entity_id=finding.id,
                    run_ids=runs,
                    surface="finding_settle",
                    reason=mixed_estimand_reason.strip(),
                )

    # Resolve overall evidence_tier
    evidence_tier = finding.evidence_tier
    if runs:
        validity_map = resolve_validity(runs, catalog_dir=cd, workspace_root=ws_root)
        tiers = {v.evidence_tier for v in validity_map.values() if not v.is_hidden}
        if "confirmatory" in tiers:
            evidence_tier = "confirmatory"
        elif "calibrating" in tiers:
            evidence_tier = "calibrating"
        elif "spike" in tiers:
            evidence_tier = "spike"
        else:
            evidence_tier = "exploratory"

    now_iso = datetime.now(UTC).isoformat()
    actor_str = reviewed_by or os.environ.get("USER", finding.declared_by or "unknown")

    finding.status = target_status
    finding.answer = target_answer
    finding.supporting_runs = runs
    finding.evidence_tier = evidence_tier
    finding.allow_mixed_estimand = mixed_estimand_reason
    finding.last_reviewed = now_iso
    finding.declared_by = actor_str

    finding.file_path.write_text(finding.render_toml(), encoding="utf-8")

    # Activate supersession on predecessor if specified and settled
    if target_status == "settled" and resolved_supersedes_ids:
        for s_id in resolved_supersedes_ids:
            add_supersession_edge(
                "finding",
                s_id,
                "finding",
                finding.id,
                reason=f"superseded by settled finding {finding.id}",
                catalog_dir=cd,
                workspace_root=ws_root,
            )

    finding.active_status = resolve_finding_status(
        finding, catalog_dir=cd, workspace_root=ws_root
    )
    return finding


def get_finding(
    slug_or_id: str,
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> Finding:
    fdir = get_findings_dir(workspace_root)
    if not fdir.exists():
        raise FindingNotFoundError(f"Finding '{slug_or_id}' not found.", slug_or_id=slug_or_id)

    matched_path: Path | None = None
    direct = fdir / f"{slug_or_id}.toml"
    if direct.exists():
        matched_path = direct
    else:
        for p in fdir.glob("*.toml"):
            try:
                f = parse_finding_file(p)
                if f.id == slug_or_id or f.question_slug == slug_or_id:
                    matched_path = p
                    break
            except Exception:
                pass

    if matched_path is None:
        raise FindingNotFoundError(f"Finding '{slug_or_id}' not found.", slug_or_id=slug_or_id)

    finding = parse_finding_file(matched_path)
    finding.active_status = resolve_finding_status(
        finding, catalog_dir=catalog_dir, workspace_root=workspace_root
    )
    return finding


def list_findings(
    status: str = "",
    include_superseded: bool = False,
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> list[Finding]:
    fdir = get_findings_dir(workspace_root)
    if not fdir.exists():
        return []

    parsed_findings: list[Finding] = []
    for p in sorted(fdir.glob("*.toml")):
        with contextlib.suppress(Exception):
            parsed_findings.append(parse_finding_file(p))

    if not parsed_findings:
        return []

    cd = Path(catalog_dir or default_catalog_dir())
    superseded_finding_map: dict[str, str] = {}
    with contextlib.suppress(Exception):
        from bathos.index import connect_read

        con = connect_read(cd, missing="empty", wire_validity=False)
        try:
            has_table = con.execute(
                "SELECT count(*) FROM duckdb_tables() WHERE table_name = 'supersession_edges'"
            ).fetchone()
            if has_table and has_table[0] > 0:
                cols_info = [
                    c[1] for c in con.execute("PRAGMA table_info('supersession_edges')").fetchall()
                ]
                if "entity_type" in cols_info:
                    rows = con.execute(
                        "SELECT entity_id, superseded_by_id FROM supersession_edges "
                        "WHERE entity_type = 'finding' ORDER BY declared_at ASC"
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT from_id, to_id FROM supersession_edges "
                        "WHERE from_type = 'finding' ORDER BY declared_at ASC"
                    ).fetchall()
                for ent_id, succ_id in rows:
                    if ent_id and succ_id and ent_id not in superseded_finding_map:
                        superseded_finding_map[ent_id] = succ_id
        finally:
            con.close()

    all_supporting_runs: set[str] = set()
    for f in parsed_findings:
        succ = superseded_finding_map.get(f.id) or (
            superseded_finding_map.get(f.question_slug) if f.question_slug else None
        )
        if succ:
            f.superseded_by = succ
            f.active_status = "superseded"
            continue
        if f.status == "superseded":
            f.active_status = "superseded"
            continue
        if f.status == "open" and not f.supporting_runs:
            f.active_status = "open"
            continue
        if f.supporting_runs:
            all_supporting_runs.update(f.supporting_runs)

    validity_map = (
        resolve_validity(sorted(all_supporting_runs), catalog_dir=cd, workspace_root=workspace_root)
        if all_supporting_runs
        else {}
    )

    results: list[Finding] = []
    for f in parsed_findings:
        if f.superseded_by or f.status == "superseded":
            f.active_status = "superseded"
        elif f.status == "open" and not f.supporting_runs:
            f.active_status = "open"
        elif f.supporting_runs:
            if all(
                validity_map.get(r) and validity_map[r].is_hidden
                for r in f.supporting_runs
            ):
                f.active_status = "needs-recheck"
            else:
                f.active_status = f.status
        else:
            f.active_status = f.status

        if not include_superseded and f.active_status == "superseded":
            continue
        if status and f.active_status != status:
            continue
        results.append(f)

    return results
