"""Correction sweep (#3099): detect stale method assumptions across sidecars and specs.

Success mode S12: when a methodological correction lands (e.g. "replicas are partial
samples to pool, drop per-replica converged=true gates", or "do not use Boltz-2
affinity"), written lessons alone do not stop old specs and sidecars from retaining
the superseded assumption. This module loads landed correction records from
``.bth/corrections.toml`` / ``.bth/corrections/*.toml`` (or explicit definitions)
and sweeps active project sidecars, claims, and specs for stale terms, expressions,
or schema keys matching the superseded assumption.
"""

from __future__ import annotations

import fnmatch
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bathos.walk import iter_project_files

_SIDECAR_SUFFIX = ".bth.toml"
_LOCK_FILE_SUFFIX = ".bth.lock.toml"
_SPEC_EXTENSIONS = frozenset({".md", ".toml", ".yaml", ".yml", ".json"})
_TOML_SKIP_KEY_PREFIXES = ("status.stale_reason", "status.superseded_by", "correction_ack", "deliberate")
_INACTIVE_SPEC_STATUSES = frozenset({"superseded", "archived", "retired", "deprecated"})
_INLINE_DELIBERATE_MARKERS = ("bth:deliberate", "bth:allow-assumption")
_PLAIN_IDENT_RE = re.compile(r"^[A-Za-z0-9_]+$")


@dataclass(frozen=True)
class MethodCorrection:
    """One landed methodological correction to sweep for across workspace artifacts."""

    id: str
    summary: str
    old_assumptions: tuple[str, ...] = ()
    old_patterns: tuple[str, ...] = ()
    replacement: str = ""
    status: str = "active"
    severity: str = "warning"
    scope: str = "all"  # "all" | "sidecars" | "specs"
    exempt_paths: tuple[str, ...] = ()
    source: str = ""
    landed_at: str = ""


@dataclass(frozen=True)
class CorrectionHit:
    """One active sidecar or spec reference still encoding a superseded assumption."""

    correction_id: str
    correction_summary: str
    artifact_path: Path
    relative_path: str
    artifact_kind: str  # "sidecar" | "claim" | "spec"
    matched_term: str
    matched_pattern: str
    location: str
    excerpt: str
    replacement: str = ""
    severity: str = "warning"


@dataclass(frozen=True)
class SweepReport:
    """Result of sweeping a project workspace for stale correction assumptions."""

    project_root: Path
    corrections_checked: int
    artifacts_scanned: int
    hits: tuple[CorrectionHit, ...]
    errors: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not self.hits


def _coerce_str_tuple(val: object) -> tuple[str, ...]:
    if val is None:
        return ()
    if isinstance(val, str):
        s = val.strip()
        return (s,) if s else ()
    if isinstance(val, (list, tuple)):
        return tuple(str(x).strip() for x in val if isinstance(x, str) and x.strip())
    return ()


def _parse_correction_entry(
    raw: dict[str, Any],
    *,
    default_id: str,
    origin: Path,
) -> MethodCorrection | str:
    corr_id = str(raw.get("id") or default_id).strip()
    if not corr_id:
        return f"{origin}: correction entry is missing 'id'"
    summary = str(raw.get("summary") or raw.get("title") or corr_id).strip()
    old_assumptions = _coerce_str_tuple(
        raw.get("old_assumptions")
        if "old_assumptions" in raw
        else raw.get("old_terms", raw.get("old_assumption"))
    )
    old_patterns = _coerce_str_tuple(
        raw.get("old_patterns") if "old_patterns" in raw else raw.get("old_pattern")
    )
    if not old_assumptions and not old_patterns:
        return (
            f"{origin}: correction {corr_id!r} must define at least one "
            "'old_assumptions' or 'old_patterns' entry"
        )
    for pat in old_patterns:
        try:
            re.compile(pat, re.IGNORECASE)
        except re.error as exc:
            return f"{origin}: correction {corr_id!r} invalid regex {pat!r}: {exc}"

    status = str(raw.get("status", "active")).strip().lower() or "active"
    severity = str(raw.get("severity", "warning")).strip().lower() or "warning"
    if severity not in {"error", "warning", "info"}:
        severity = "warning"
    scope = str(raw.get("scope", "all")).strip().lower() or "all"
    if scope not in {"all", "sidecars", "specs"}:
        scope = "all"

    return MethodCorrection(
        id=corr_id,
        summary=summary,
        old_assumptions=old_assumptions,
        old_patterns=old_patterns,
        replacement=str(raw.get("replacement", "")).strip(),
        status=status,
        severity=severity,
        scope=scope,
        exempt_paths=_coerce_str_tuple(raw.get("exempt_paths")),
        source=str(raw.get("source", "")).strip(),
        landed_at=str(raw.get("landed_at", "")).strip(),
    )


def load_corrections(project_root: Path) -> tuple[list[MethodCorrection], list[str]]:
    """Load landed corrections from ``.bth/corrections.toml`` and ``.bth/corrections/*.toml``."""
    root = Path(project_root)
    corrections: list[MethodCorrection] = []
    errors: list[str] = []

    sources: list[Path] = []
    single_file = root / ".bth" / "corrections.toml"
    if single_file.is_file():
        sources.append(single_file)
    corr_dir = root / ".bth" / "corrections"
    if corr_dir.is_dir():
        sources.extend(sorted(p for p in corr_dir.glob("*.toml") if p.is_file()))

    for path in sources:
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            errors.append(f"{path}: failed to parse correction registry: {exc}")
            continue

        entries: list[tuple[str, dict[str, Any]]] = []
        if isinstance(data.get("corrections"), list):
            for idx, item in enumerate(data["corrections"], start=1):
                if isinstance(item, dict):
                    entries.append((f"{path.stem}-{idx}", item))
                else:
                    errors.append(f"{path}: [[corrections]] entry #{idx} must be a table")
        elif isinstance(data.get("corrections"), dict):
            for key, item in data["corrections"].items():
                if isinstance(item, dict):
                    entries.append((str(key), item))
                else:
                    errors.append(f"{path}: [corrections.{key}] must be a table")
        elif isinstance(data.get("correction"), dict):
            entries.append((path.stem, data["correction"]))
        else:
            entries.append((path.stem, data))

        for default_id, raw in entries:
            parsed = _parse_correction_entry(raw, default_id=default_id, origin=path)
            if isinstance(parsed, str):
                errors.append(parsed)
            else:
                corrections.append(parsed)

    return corrections, errors


def _compile_matcher(term: str, *, is_regex: bool) -> re.Pattern[str]:
    if is_regex:
        return re.compile(term, re.IGNORECASE)
    if _PLAIN_IDENT_RE.match(term):
        return re.compile(rf"(?<![A-Za-z0-9_]){re.escape(term)}(?![A-Za-z0-9_])", re.IGNORECASE)
    # Collapse runs of whitespace so multi-word phrases match across formatting differences.
    parts = [re.escape(part) for part in term.split()]
    if len(parts) > 1:
        return re.compile(r"\s+".join(parts), re.IGNORECASE)
    return re.compile(re.escape(term), re.IGNORECASE)


def _iter_spec_dirs(root: Path) -> list[Path]:
    candidates = [
        root / ".bth" / "specs",
        root / ".bth" / "claims",
        root / "specs",
        root / "docs" / "specs",
        root / "scripts",
    ]
    return [d for d in candidates if d.is_dir()]


def iter_sweep_artifacts(
    project_root: Path,
    *,
    extra_paths: Sequence[Path] = (),
) -> list[tuple[Path, str]]:
    """Collect active sidecars, claims, and spec files under ``project_root``."""
    root = Path(project_root)
    seen: set[Path] = set()
    artifacts: list[tuple[Path, str]] = []

    def _add(path: Path, kind: str) -> None:
        resolved = path.resolve()
        if resolved in seen:
            return
        seen.add(resolved)
        artifacts.append((path, kind))

    for sidecar_path in iter_project_files(root, _SIDECAR_SUFFIX, event_name="sweep.boundary_pruned"):
        if sidecar_path.name == ".bth.toml" or sidecar_path.name.endswith(_LOCK_FILE_SUFFIX):
            continue
        _add(sidecar_path, "sidecar")

    for spec_dir in _iter_spec_dirs(root):
        for ext in sorted(_SPEC_EXTENSIONS):
            for p in iter_project_files(spec_dir, ext, event_name="sweep.boundary_pruned"):
                if p.name.endswith(_SIDECAR_SUFFIX) or p.name.endswith(_LOCK_FILE_SUFFIX):
                    continue
                if "archive" in p.relative_to(root).parts:
                    continue
                kind = "claim" if ".bth/claims" in p.relative_to(root).as_posix() else "spec"
                _add(p, kind)

    for p in extra_paths:
        if p.is_file():
            if p.name.endswith(_SIDECAR_SUFFIX):
                _add(p, "sidecar")
            else:
                _add(p, "spec")
        elif p.is_dir():
            for ext in sorted(_SPEC_EXTENSIONS):
                for sub in iter_project_files(p, ext, event_name="sweep.boundary_pruned"):
                    if sub.name.endswith(_SIDECAR_SUFFIX):
                        _add(sub, "sidecar")
                    elif not sub.name.endswith(_LOCK_FILE_SUFFIX):
                        _add(sub, "spec")

    artifacts.sort(key=lambda item: item[0])
    return artifacts


def _extract_toml_fields(data: Any, prefix: str = "") -> list[tuple[str, str]]:
    """Flatten TOML values (plus ``result_schema`` key names) to ``(location, text)`` pairs."""
    out: list[tuple[str, str]] = []
    if isinstance(data, dict):
        for k, v in data.items():
            loc = f"{prefix}.{k}" if prefix else str(k)
            if prefix == "result_schema":
                out.append((f"{loc} (schema key)", str(k)))
            out.extend(_extract_toml_fields(v, loc))
    elif isinstance(data, list):
        for idx, item in enumerate(data):
            out.extend(_extract_toml_fields(item, f"{prefix}[{idx}]"))
    elif isinstance(data, str):
        out.append((prefix, data))
    elif isinstance(data, bool):
        out.append((prefix, "true" if data else "false"))
    elif isinstance(data, (int, float)):
        out.append((prefix, str(data)))
    return out


def _toml_is_acknowledged_or_stale(
    data: dict[str, Any],
    correction_id: str,
    *,
    include_stale: bool,
) -> bool:
    if not include_stale:
        status_tbl = data.get("status")
        if (
            isinstance(status_tbl, dict)
            and status_tbl.get("stale") is True
            and (
                str(status_tbl.get("stale_reason", "")).strip()
                or str(status_tbl.get("superseded_by", "")).strip()
            )
        ):
            return True
        claim_tbl = data.get("claim")
        if isinstance(claim_tbl, dict):
            c_status = str(claim_tbl.get("status", "")).strip().lower()
            if c_status in _INACTIVE_SPEC_STATUSES:
                return True

    ack_vals: list[str] = []
    for key in ("correction_ack", "deliberate_assumptions"):
        raw = data.get(key)
        ack_vals.extend(_coerce_str_tuple(raw))
    meta_tbl = data.get("corrections")
    if isinstance(meta_tbl, dict):
        ack_vals.extend(_coerce_str_tuple(meta_tbl.get("acknowledged")))
        ack_vals.extend(_coerce_str_tuple(meta_tbl.get("deliberate")))
    return correction_id.lower() in {a.lower() for a in ack_vals}


def _parse_text_frontmatter(text: str) -> dict[str, str]:
    """Read simple key: value / key = value frontmatter from markdown specs."""
    lines = text.lstrip().splitlines()
    if not lines:
        return {}
    delim = lines[0].strip()
    if delim not in ("---", "+++"):
        return {}
    meta: dict[str, str] = {}
    for line in lines[1:]:
        stripped = line.strip()
        if stripped == delim:
            break
        m = re.match(r"^([A-Za-z0-9_]+)\s*[:=]\s*(.+)$", stripped)
        if m:
            meta[m.group(1).lower()] = m.group(2).strip().strip("\"'")
    return meta


def _is_exempt_path(rel_posix: str, exempt_patterns: tuple[str, ...]) -> bool:
    name = Path(rel_posix).name
    for pat in exempt_patterns:
        norm = pat.strip().replace("\\", "/").lstrip("./")
        if not norm:
            continue
        if "/" in norm and fnmatch.fnmatchcase(rel_posix, norm):
            return True
        if fnmatch.fnmatchcase(name, norm):
            return True
    return False


def sweep_corrections(
    project_root: Path,
    corrections: Sequence[MethodCorrection] | None = None,
    *,
    include_stale: bool = False,
    extra_paths: Sequence[Path] = (),
) -> SweepReport:
    """Scan workspace sidecars, claims, and specs for stale assumptions matching landed corrections."""
    root = Path(project_root)
    load_errors: list[str] = []
    if corrections is None:
        loaded, load_errors = load_corrections(root)
        active = [c for c in loaded if c.status == "active"]
    else:
        active = [c for c in corrections if c.status == "active"]

    if not active:
        return SweepReport(
            project_root=root,
            corrections_checked=0,
            artifacts_scanned=0,
            hits=(),
            errors=tuple(load_errors),
        )

    compiled: list[tuple[MethodCorrection, list[tuple[str, str, re.Pattern[str]]]]] = []
    for corr in active:
        matchers: list[tuple[str, str, re.Pattern[str]]] = []
        for term in corr.old_assumptions:
            matchers.append((term, "", _compile_matcher(term, is_regex=False)))
        for pat in corr.old_patterns:
            try:
                matchers.append((pat, pat, _compile_matcher(pat, is_regex=True)))
            except re.error as exc:
                load_errors.append(f"correction {corr.id!r} invalid regex {pat!r}: {exc}")
        if matchers:
            compiled.append((corr, matchers))

    artifacts = iter_sweep_artifacts(root, extra_paths=extra_paths)
    hits: list[CorrectionHit] = []

    for artifact_path, artifact_kind in artifacts:
        try:
            rel_posix = artifact_path.resolve().relative_to(root.resolve()).as_posix()
        except ValueError:
            rel_posix = str(artifact_path)

        try:
            raw_text = artifact_path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

        is_toml = artifact_path.suffix == ".toml"
        toml_data: dict[str, Any] | None = None
        if is_toml:
            try:
                parsed_toml = tomllib.loads(raw_text)
                if isinstance(parsed_toml, dict):
                    toml_data = parsed_toml
            except tomllib.TOMLDecodeError:
                toml_data = None

        frontmatter = {} if is_toml else _parse_text_frontmatter(raw_text)
        spec_inactive = (
            not include_stale
            and frontmatter.get("status", "").lower() in _INACTIVE_SPEC_STATUSES
        )
        ack_raw = frontmatter.get("correction_ack", "") or frontmatter.get("deliberate", "")
        text_acks = {s.strip().lower() for s in ack_raw.strip("[]").split(",") if s.strip()}

        for corr, matchers in compiled:
            if corr.scope == "sidecars" and artifact_kind != "sidecar":
                continue
            if corr.scope == "specs" and artifact_kind == "sidecar":
                continue
            if _is_exempt_path(rel_posix, corr.exempt_paths):
                continue

            if toml_data is not None:
                if _toml_is_acknowledged_or_stale(toml_data, corr.id, include_stale=include_stale):
                    continue
                seen_hits: set[tuple[str, str]] = set()
                for loc, text_val in _extract_toml_fields(toml_data):
                    if any(loc.startswith(skip) for skip in _TOML_SKIP_KEY_PREFIXES):
                        continue
                    for term, pat, rx in matchers:
                        if rx.search(text_val):
                            key = (loc, term)
                            if key in seen_hits:
                                continue
                            seen_hits.add(key)
                            hits.append(
                                CorrectionHit(
                                    correction_id=corr.id,
                                    correction_summary=corr.summary,
                                    artifact_path=artifact_path,
                                    relative_path=rel_posix,
                                    artifact_kind=artifact_kind,
                                    matched_term=term,
                                    matched_pattern=pat,
                                    location=loc,
                                    excerpt=text_val[:160].strip(),
                                    replacement=corr.replacement,
                                    severity=corr.severity,
                                )
                            )
                continue

            if spec_inactive or corr.id.lower() in text_acks:
                continue

            seen_text_hits: set[tuple[str, str]] = set()
            for lineno, line in enumerate(raw_text.splitlines(), start=1):
                lower_line = line.lower()
                if any(marker in lower_line for marker in _INLINE_DELIBERATE_MARKERS):
                    continue
                for term, pat, rx in matchers:
                    if rx.search(line):
                        loc = f"line:{lineno}"
                        key = (loc, term)
                        if key in seen_text_hits:
                            continue
                        seen_text_hits.add(key)
                        hits.append(
                            CorrectionHit(
                                correction_id=corr.id,
                                correction_summary=corr.summary,
                                artifact_path=artifact_path,
                                relative_path=rel_posix,
                                artifact_kind=artifact_kind,
                                matched_term=term,
                                matched_pattern=pat,
                                location=loc,
                                excerpt=line.strip()[:160],
                                replacement=corr.replacement,
                                severity=corr.severity,
                            )
                        )

    return SweepReport(
        project_root=root,
        corrections_checked=len(compiled),
        artifacts_scanned=len(artifacts),
        hits=tuple(hits),
        errors=tuple(load_errors),
    )
