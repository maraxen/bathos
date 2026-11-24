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

from bathos.sidecar import is_parity_or_discrepancy_toml
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
    stripped = term.strip()
    if not stripped:
        return re.compile(re.escape(term), re.IGNORECASE)
    parts = [re.escape(part) for part in stripped.split()]
    body = r"\s+".join(parts) if len(parts) > 1 else parts[0]
    prefix = r"(?<![A-Za-z0-9_-])" if re.match(r"^[A-Za-z0-9_]", stripped) else ""
    suffix = r"(?![A-Za-z0-9_-])" if re.search(r"[A-Za-z0-9_]$", stripped) else ""
    return re.compile(f"{prefix}{body}{suffix}", re.IGNORECASE)


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
        if (
            sidecar_path.name == ".bth.toml"
            or sidecar_path.name.endswith(_LOCK_FILE_SUFFIX)
            or is_parity_or_discrepancy_toml(sidecar_path)
        ):
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
        is_schema_scope = prefix == "result_schema" or prefix.startswith(
            ("result_schema.", "result_schema[")
        )
        for k, v in data.items():
            loc = f"{prefix}.{k}" if prefix else str(k)
            if is_schema_scope:
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


def _strip_matching_quotes(val: str) -> str:
    s = val.strip()
    while len(s) >= 2 and s[0] == s[-1] and s[0] in ('"', "'"):
        s = s[1:-1].strip()
    return s


def _parse_frontmatter_ack_ids(raw: str) -> set[str]:
    inner = raw.strip()
    if inner.startswith("[") and inner.endswith("]"):
        inner = inner[1:-1]
    acks: set[str] = set()
    for part in inner.split(","):
        cleaned = _strip_matching_quotes(part)
        if cleaned:
            acks.add(cleaned.lower())
    return acks


def _split_toml_comment(line: str) -> tuple[str, str]:
    """Split ``line`` into ``(code_part, comment_part)`` respecting TOML quotes."""
    in_single = False
    in_double = False
    escaped = False
    for idx, ch in enumerate(line):
        if escaped:
            escaped = False
            continue
        if in_double and ch == "\\":
            escaped = True
            continue
        if ch == '"' and not in_single:
            in_double = not in_double
        elif ch == "'" and not in_double:
            in_single = not in_single
        elif ch == "#" and not in_single and not in_double:
            return line[:idx], line[idx + 1 :]
    return line, ""


def _extract_toml_deliberate_annotations(
    raw_text: str,
) -> tuple[set[str], set[str], list[str]]:
    """Collect dotted keys, section prefixes, and line snippets annotated with deliberate markers."""
    deliberate_locs: set[str] = set()
    deliberate_prefixes: set[str] = set()
    deliberate_snippets: list[str] = []
    current_section = ""
    multiline_delim: str | None = None
    open_key_loc = ""

    for line in raw_text.splitlines():
        if multiline_delim is not None:
            has_marker = any(m in line.lower() for m in _INLINE_DELIBERATE_MARKERS)
            if has_marker and open_key_loc:
                deliberate_locs.add(open_key_loc)
            end_pos = line.find(multiline_delim)
            if end_pos != -1:
                after = line[end_pos + 3 :]
                multiline_delim = None
                if any(m in after.lower() for m in _INLINE_DELIBERATE_MARKERS) and open_key_loc:
                    deliberate_locs.add(open_key_loc)
                open_key_loc = ""
            continue

        code, comment = _split_toml_comment(line)
        stripped = code.strip()
        has_marker = bool(comment) and any(
            m in comment.lower() for m in _INLINE_DELIBERATE_MARKERS
        )

        # Detect table headers [section] or [[section]]
        if stripped.startswith("[[") and stripped.endswith("]]"):
            sec = stripped[2:-2].strip()
            current_section = sec
            if has_marker and sec:
                deliberate_prefixes.add(sec)
            continue
        if stripped.startswith("[") and stripped.endswith("]") and "=" not in stripped:
            sec = stripped[1:-1].strip()
            current_section = sec
            if has_marker and sec:
                deliberate_prefixes.add(sec)
            continue

        m = re.match(r'^([A-Za-z0-9_-]+|"[^"]+"|\'[^\']+\')\s*=\s*(.*)$', stripped)
        if m:
            raw_key = _strip_matching_quotes(m.group(1))
            rhs = m.group(2).strip()
            loc = f"{current_section}.{raw_key}" if current_section else raw_key
            if has_marker:
                deliberate_locs.add(loc)
            if rhs.startswith('"""') and rhs.count('"""') == 1:
                multiline_delim = '"""'
                open_key_loc = loc
            elif rhs.startswith("'''") and rhs.count("'''") == 1:
                multiline_delim = "'''"
                open_key_loc = loc
        elif has_marker and stripped:
            deliberate_snippets.append(stripped)

    return deliberate_locs, deliberate_prefixes, deliberate_snippets


def _is_toml_loc_deliberate(
    loc: str,
    text_val: str,
    deliberate_locs: set[str],
    deliberate_prefixes: set[str],
    deliberate_snippets: list[str],
) -> bool:
    base_loc = loc.removesuffix(" (schema key)")
    loc_no_idx = re.sub(r"\[\d+\]", "", base_loc)
    if base_loc in deliberate_locs or loc_no_idx in deliberate_locs:
        return True
    for prefix in deliberate_prefixes:
        if base_loc == prefix or base_loc.startswith(f"{prefix}."):
            return True
    if "[" in base_loc and deliberate_snippets:
        for snippet in deliberate_snippets:
            if text_val and text_val in snippet:
                return True
    return False


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
            raw_val = m.group(2).strip()
            if not (raw_val.startswith("[") and raw_val.endswith("]")):
                raw_val = _strip_matching_quotes(raw_val)
            meta[m.group(1).lower()] = raw_val
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
            except tomllib.TOMLDecodeError as exc:
                load_errors.append(f"{rel_posix}: failed to parse TOML: {exc}")
                continue

        deliberate_locs: set[str] = set()
        deliberate_prefixes: set[str] = set()
        deliberate_snippets: list[str] = []
        if is_toml:
            deliberate_locs, deliberate_prefixes, deliberate_snippets = (
                _extract_toml_deliberate_annotations(raw_text)
            )

        frontmatter = {} if is_toml else _parse_text_frontmatter(raw_text)
        spec_inactive = (
            not include_stale
            and frontmatter.get("status", "").lower() in _INACTIVE_SPEC_STATUSES
        )
        text_acks: set[str] = set()
        for ack_key in ("correction_ack", "deliberate", "deliberate_assumptions"):
            if frontmatter.get(ack_key):
                text_acks.update(_parse_frontmatter_ack_ids(frontmatter[ack_key]))

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
                    if _is_toml_loc_deliberate(
                        loc, text_val, deliberate_locs, deliberate_prefixes, deliberate_snippets
                    ):
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
