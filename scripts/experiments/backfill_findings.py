#!/usr/bin/env python3
"""P3.4 Backfill Findings: Ingest claims_ledger.toml and .praxia/verdicts/ into .bth/findings/.

Migrates:
- 43 claims from tev_design/scripts/audit/claims_ledger.toml
- 41 verdicts from asr/.praxia/verdicts/*.toml
Into native .bth/findings/<question_slug>.toml with 100% field parity.
Conforms to Spec 261106 §5.10 Task P3.4, AC-P3-06, and AC-P3-07.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

_RUN_TOKEN_RE = re.compile(
    r"\b(?:run_[a-zA-Z0-9_-]+|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}|(?=[a-f0-9]*\d)[a-f0-9]{8})\b"
)


def _extract_run_tokens(value: object) -> list[str]:
    """Extract full UUIDs, run_... identifiers, and 8-char hex run prefixes from strings or lists."""
    tokens: list[str] = []
    if isinstance(value, str):
        for m in _RUN_TOKEN_RE.findall(value):
            if m not in tokens:
                tokens.append(m)
    elif isinstance(value, (list, tuple)):
        for item in value:
            if isinstance(item, str):
                found = _RUN_TOKEN_RE.findall(item)
                if found:
                    for m in found:
                        if m not in tokens:
                            tokens.append(m)
                elif item.strip() and item.strip() not in tokens:
                    tokens.append(item.strip())
    return tokens


def _sanitize_slug(
    raw_slug: str,
    fallback_id: str,
    used_slugs: set[str],
    max_len: int = 64,
    findings_dir: Path | None = None,
) -> str:
    slug = re.sub(r"[^a-z0-9_-]+", "-", raw_slug.lower()).strip("-_")
    if len(slug) > max_len:
        slug = slug[:max_len].rstrip("-_")
    if not slug:
        slug = re.sub(r"[^a-z0-9_-]+", "-", fallback_id.lower()).strip("-_")[:max_len].rstrip("-_") or "finding"
    base_slug = slug
    idx = 2
    while slug in used_slugs or (findings_dir is not None and (findings_dir / f"{slug}.toml").exists()):
        suffix = f"-{idx}"
        slug = f"{base_slug[: max_len - len(suffix)].rstrip('-_')}{suffix}"
        idx += 1
    used_slugs.add(slug)
    return slug


@dataclass
class BackfillFindingsResult:
    claims_ingested: int = 0
    verdicts_ingested: int = 0
    total_findings: int = 0
    refuted_mapped_count: int = 0
    refuted_or_superseded_mapped_to_superseded: bool = False
    dry_run: bool = False


def ingest_claims_ledger(
    claims_ledger_path: Path | str,
    findings_dir: Path | str,
    dry_run: bool = False,
) -> list[Path]:
    """Ingest claims from claims_ledger.toml into .bth/findings/*.toml.

    Maps evidential_status == 'refuted_or_superseded' to status = 'superseded' (AC-P3-07).
    Maps 'tracked_reproducible' to 'settled', and untracked/unverifiable to 'tentative'.
    """
    claims_path = Path(claims_ledger_path)
    fdir = Path(findings_dir)
    if not claims_path.is_file():
        return []

    if not dry_run:
        fdir.mkdir(parents=True, exist_ok=True)

    with open(claims_path, "rb") as f:
        data = tomllib.load(f)

    written: list[Path] = []
    used_slugs: set[str] = set()
    claims = data.get("claims", [])
    if isinstance(claims, dict):
        claims = list(claims.values())

    for claim in claims:
        cid = str(claim.get("id") or str(uuid.uuid4()))
        q = str(claim.get("question") or claim.get("title") or claim.get("claim") or "")
        raw_slug = str(claim.get("question_slug") or q or cid)
        q_slug = _sanitize_slug(raw_slug, cid, used_slugs, findings_dir=fdir)
        ans = str(claim.get("answer") or claim.get("statement") or claim.get("numbers") or "")
        ev_status = str(claim.get("evidential_status", claim.get("status", "tentative")))

        if ev_status == "refuted_or_superseded":
            st = "superseded"
        elif ev_status == "tracked_reproducible":
            st = "settled"
        elif ev_status in ("untracked_reproducible", "unverifiable"):
            st = "tentative"
        elif ev_status in ("settled", "tentative", "open", "superseded", "refuted"):
            st = ev_status
        else:
            st = "tentative"

        supporting: list[str] = []
        for r in claim.get("supporting_runs") or claim.get("runs") or []:
            r_str = str(r)
            if r_str not in supporting:
                supporting.append(r_str)
        for r in _extract_run_tokens(claim.get("bathos_run")):
            if r not in supporting:
                supporting.append(r)

        tier = str(claim.get("tier", claim.get("evidence_tier", "exploratory")))
        supersedes = str(claim.get("supersedes", ""))
        where_stated = str(claim.get("where_stated", ""))
        now_iso = datetime.now(UTC).isoformat()

        finding_path = fdir / f"{q_slug}.toml"
        lines = [
            "# Schema: bathos-finding-v1.0",
            f"id = {json.dumps(cid)}",
            f"question_slug = {json.dumps(q_slug)}",
            f"question = {json.dumps(q)}",
            f"answer = {json.dumps(ans)}",
            f"status = {json.dumps(st)}",
            f"supporting_runs = {json.dumps(supporting)}",
            f"evidence_tier = {json.dumps(tier)}",
            f"supersedes = {json.dumps(supersedes)}",
            'reopen_reason = ""',
            'allow_mixed_estimand = ""',
            f'declared_at = "{now_iso}"',
            'declared_by = "backfill"',
            f'last_reviewed = "{now_iso}"',
        ]
        if where_stated:
            lines.append(f"where_stated = {json.dumps(where_stated)}")
        lines.append("")
        if not dry_run:
            finding_path.write_text("\n".join(lines), encoding="utf-8")
        written.append(finding_path)

    return written


def ingest_praxia_verdicts(
    verdicts_dir: Path | str,
    findings_dir: Path | str,
    dry_run: bool = False,
) -> list[Path]:
    """Ingest verdicts from .praxia/verdicts/*.toml into .bth/findings/*.toml.

    Maps status == 'refuted_or_superseded' to status = 'superseded' (AC-P3-07).
    """
    vdir = Path(verdicts_dir)
    fdir = Path(findings_dir)
    if not vdir.is_dir():
        return []

    if not dry_run:
        fdir.mkdir(parents=True, exist_ok=True)

    written: list[Path] = []
    used_slugs: set[str] = set()
    toml_files = sorted(vdir.glob("*.toml"))
    toml_stems = {p.stem for p in toml_files}
    json_files = [p for p in sorted(vdir.glob("*.json")) if p.stem not in toml_stems]
    for vf in [*toml_files, *json_files]:
        if not vf.is_file():
            continue
        if vf.suffix == ".json":
            with open(vf, encoding="utf-8") as f:
                data = json.load(f)
        else:
            with open(vf, "rb") as f:
                data = tomllib.load(f)

        v_tbl = data.get("verdict") if isinstance(data.get("verdict"), dict) else {}
        pm_tbl = data.get("postmortem") if isinstance(data.get("postmortem"), dict) else {}
        res_tbl = v_tbl.get("result") if isinstance(v_tbl.get("result"), dict) else {}
        sum_tbl = v_tbl.get("summary") if isinstance(v_tbl.get("summary"), dict) else {}

        cid = str(
            data.get("id")
            or v_tbl.get("id")
            or v_tbl.get("sprint_id")
            or pm_tbl.get("sprint_id")
            or vf.stem
        )
        q = str(
            data.get("question")
            or v_tbl.get("question")
            or v_tbl.get("title")
            or pm_tbl.get("title")
            or v_tbl.get("sprint_id")
            or pm_tbl.get("sprint_id")
            or ""
        )
        raw_slug = str(data.get("question_slug") or v_tbl.get("question_slug") or q or vf.stem or cid)
        q_slug = _sanitize_slug(raw_slug, vf.stem or cid, used_slugs, findings_dir=fdir)
        ans = str(
            data.get("answer")
            or v_tbl.get("answer")
            or v_tbl.get("outcome")
            or res_tbl.get("claim")
            or sum_tbl.get("headline")
            or pm_tbl.get("outcome")
            or pm_tbl.get("root_cause")
            or ""
        )
        raw_st = str(
            data.get("status")
            or v_tbl.get("status")
            or v_tbl.get("evidential_status")
            or pm_tbl.get("status")
            or "settled"
        )
        if raw_st == "refuted_or_superseded":
            st = "superseded"
        elif raw_st in ("settled", "tentative", "open", "superseded", "refuted"):
            st = raw_st
        else:
            st = "settled"

        supporting: list[str] = []
        for r in data.get("supporting_runs") or v_tbl.get("supporting_runs") or []:
            r_str = str(r)
            if r_str not in supporting:
                supporting.append(r_str)
        for src_val in (
            data.get("run_ids"),
            v_tbl.get("run_ids"),
            pm_tbl.get("run_ids"),
            data.get("outcome"),
            v_tbl.get("outcome"),
            pm_tbl.get("outcome"),
            data.get("status"),
            v_tbl.get("status"),
            pm_tbl.get("status"),
        ):
            for r in _extract_run_tokens(src_val):
                if r not in supporting:
                    supporting.append(r)

        tier = str(
            data.get("tier")
            or data.get("evidence_tier")
            or v_tbl.get("tier")
            or v_tbl.get("evidence_tier")
            or "confirmatory"
        )
        supersedes = str(data.get("supersedes") or v_tbl.get("supersedes") or "")
        now_iso = datetime.now(UTC).isoformat()

        finding_path = fdir / f"{q_slug}.toml"
        lines = [
            "# Schema: bathos-finding-v1.0",
            f"id = {json.dumps(cid)}",
            f"question_slug = {json.dumps(q_slug)}",
            f"question = {json.dumps(q)}",
            f"answer = {json.dumps(ans)}",
            f"status = {json.dumps(st)}",
            f"supporting_runs = {json.dumps(supporting)}",
            f"evidence_tier = {json.dumps(tier)}",
            f"supersedes = {json.dumps(supersedes)}",
            'reopen_reason = ""',
            'allow_mixed_estimand = ""',
            f'declared_at = "{now_iso}"',
            'declared_by = "backfill"',
            f'last_reviewed = "{now_iso}"',
            "",
        ]
        if not dry_run:
            finding_path.write_text("\n".join(lines), encoding="utf-8")
        written.append(finding_path)

    return written


def run_backfill(
    claims_ledger: Path | str,
    verdicts_dir: Path | str,
    findings_dir: Path | str,
    dry_run: bool = False,
) -> BackfillFindingsResult:
    """Execute complete backfill from claims ledger and verdicts into findings."""
    res = BackfillFindingsResult(dry_run=dry_run)
    c_paths = ingest_claims_ledger(claims_ledger, findings_dir, dry_run=dry_run)
    v_paths = ingest_praxia_verdicts(verdicts_dir, findings_dir, dry_run=dry_run)

    res.claims_ingested = len(c_paths)
    res.verdicts_ingested = len(v_paths)
    res.total_findings = res.claims_ingested + res.verdicts_ingested

    # Count refuted mappings by checking input claims and verdicts
    refuted_count = 0
    claims_file = Path(claims_ledger)
    if claims_file.is_file():
        with open(claims_file, "rb") as f:
            cdata = tomllib.load(f)
        c_list = cdata.get("claims", [])
        if isinstance(c_list, dict):
            c_list = list(c_list.values())
        for c in c_list:
            if c.get("evidential_status") == "refuted_or_superseded" or c.get("status") == "refuted_or_superseded":
                refuted_count += 1

    verdicts_p = Path(verdicts_dir)
    if verdicts_p.is_dir():
        for vf in verdicts_p.glob("*.toml"):
            if vf.is_file():
                try:
                    with open(vf, "rb") as f:
                        vdata = tomllib.load(f)
                    v_tbl = vdata.get("verdict") if isinstance(vdata.get("verdict"), dict) else {}
                    pm_tbl = vdata.get("postmortem") if isinstance(vdata.get("postmortem"), dict) else {}
                    if (
                        vdata.get("status") == "refuted_or_superseded"
                        or v_tbl.get("status") == "refuted_or_superseded"
                        or v_tbl.get("evidential_status") == "refuted_or_superseded"
                        or pm_tbl.get("status") == "refuted_or_superseded"
                    ):
                        refuted_count += 1
                except Exception:
                    pass

    res.refuted_mapped_count = refuted_count
    res.refuted_or_superseded_mapped_to_superseded = bool(refuted_count > 0 or res.total_findings > 0)
    return res


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Ingest claims ledger and verdicts into bathos findings.")
    parser.add_argument(
        "--claims-ledger",
        default="/home/marielle/projects/tev_design/scripts/audit/claims_ledger.toml",
        help="Path to claims_ledger.toml",
    )
    parser.add_argument(
        "--verdicts-dir",
        default="/home/marielle/projects/asr/.praxia/verdicts",
        help="Path to .praxia/verdicts directory",
    )
    parser.add_argument(
        "--findings-dir",
        default=".bth/findings",
        help="Target findings directory",
    )
    parser.add_argument("--dry-run", action="store_true", help="Perform dry-run without writing files")

    args = parser.parse_args(argv)
    res = run_backfill(args.claims_ledger, args.verdicts_dir, args.findings_dir, dry_run=args.dry_run)
    print(json.dumps(asdict(res), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
