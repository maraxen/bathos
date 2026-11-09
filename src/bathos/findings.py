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
    # Dynamic field
    active_status: str = ""

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


def resolve_finding_status(
    finding: Finding,
    catalog_dir: Path | str | None = None,
    workspace_root: Path | str | None = None,
) -> str:
    """Dynamically resolve finding status at read time without mutating disk."""
    if finding.status == "superseded":
        return "superseded"

    try:
        from bathos.supersession import get_superseded_by

        succs = get_superseded_by("finding", finding.id, catalog_dir=catalog_dir)
        if succs:
            return "superseded"
    except Exception:
        pass

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
    supersedes: str = "",
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

    if supersedes and not _entity_exists("finding", supersedes, cd, ws_root):
        raise SupersededTargetNotFoundError("finding", supersedes)

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
        supersedes=supersedes,
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
    if status == "settled" and supersedes:
        add_supersession_edge(
            "finding",
            supersedes,
            "finding",
            finding.id,
            reason=f"superseded by settled finding {finding.id}",
            catalog_dir=cd,
            workspace_root=workspace_root,
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

    results: list[Finding] = []
    for p in sorted(fdir.glob("*.toml")):
        try:
            f = parse_finding_file(p)
            f.active_status = resolve_finding_status(
                f, catalog_dir=catalog_dir, workspace_root=workspace_root
            )
            if not include_superseded and f.active_status == "superseded":
                continue
            if status and f.active_status != status:
                continue
            results.append(f)
        except Exception:
            pass
    return results
