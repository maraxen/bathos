"""Citation formatting and strict-coordinate markdown verification for bathos."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from bathos.errors import CitationVerificationError
from bathos.schema import Run

_SHA256_LOWER_HEX_RE = re.compile(r"^[0-9a-f]{64}$")
_NUMBERED_LINE_RE = re.compile(r"^\[Page\s+(\d+),\s+Line\s+(\d+)\]\s*(.*)$")
_ANY_SOURCE_BRACKET_RE = re.compile(r"\[\s*source\s*:[^\]]*\]", re.IGNORECASE)
_COORD_TAG_RE = re.compile(
    r"^\[\s*source\s*:\s*([^,\]]+?)\s*,\s*sha256\s*:\s*([^,\]]+?)\s*,"
    r"\s*Page\s+(-?\d+)\s*,\s*Line\s+(-?\d+)(?:\s*-\s*(-?\d+))?\s*\]$",
    re.IGNORECASE,
)
_BLOCKQUOTE_LINE_RE = re.compile(r"^\s*>\s?(.*)$")
_ATTRIBUTION_REMAINDER_RE = re.compile(r"^\s*(?:[-—–]\s*)?$")
_DEHYPHENATE_WRAP_RE = re.compile(r"([A-Za-z0-9])-\s*\n\s*([A-Za-z0-9])")


@dataclass(frozen=True)
class CitationVerificationResult:
    """Result returned by :func:`verify_citations`."""

    ok: bool
    doc_path: str
    sources_dir: str
    strict_coords: bool
    checked_quotes: int
    checked_coords: int
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ParsedCoordTag:
    line_index: int
    start_col: int
    end_col: int
    raw_text: str
    source_name: str | None = None
    page_num: int | None = None
    line_start: int | None = None
    line_end: int | None = None
    normalized_window: str | None = None
    is_valid: bool = False


@dataclass(frozen=True)
class _LoadedSource:
    sha256: str
    lines: dict[tuple[int, int], str]
    normalized_full_text: str


def format_citation(run: Run, fmt: str = "markdown") -> str:
    """Format a structured citation for a run.

    Args:
        run: The Run object to cite.
        fmt: Output format. Options: "markdown" (default), "json".

    Returns:
        Formatted citation string.
    """
    # Handle pre-v0.6 runs with missing manifest fields
    manifest_hash = run.manifest_sha256 if run.manifest_sha256 else "not recorded — pre-v0.6"
    manifest_path_display = run.manifest_path if run.manifest_path else "not recorded — pre-v0.6"
    sidecar_hash = run.sidecar_sha256 if run.sidecar_sha256 else "not recorded — pre-v0.6"

    if fmt == "json":
        return json.dumps(
            {
                "run_id": run.id,
                "project_slug": run.project_slug,
                "command": run.command,
                "sidecar_sha256": sidecar_hash,
                "manifest_sha256": run.manifest_sha256,
                "manifest_path": run.manifest_path,
                "git_sha": run.git_hash,
                "outcome": run.outcome or "unknown",
                "timestamp": str(run.timestamp),
            },
            indent=2,
        )

    # markdown (default)
    lines = [
        f"Run {run.id[:8]}",
        f"  Project:          {run.project_slug}",
        f"  Hypothesis hash:  {sidecar_hash[:16] + '...' if sidecar_hash != 'not recorded — pre-v0.6' else sidecar_hash}",
        f"  Manifest hash:    {manifest_hash[:16] + '...' if manifest_hash != 'not recorded — pre-v0.6' else manifest_hash}",
        f"  Manifest path:    {manifest_path_display}",
        f"  Git SHA:          {run.git_hash[:8] if run.git_hash else 'unknown'}",
        f"  Outcome:          {run.outcome or 'unknown'}",
        f"  Timestamp:        {run.timestamp}",
    ]
    return "\n".join(lines)


def _normalize_citation_text(text: str, *, strip_outer_quotes: bool) -> str:
    """Apply the normative 5-step normalization pipeline from spec FR-3."""
    normalized = unicodedata.normalize("NFKC", text)
    normalized = (
        normalized.replace("\u2018", "'")
        .replace("\u2019", "'")
        .replace("\u201c", '"')
        .replace("\u201d", '"')
        .replace("\u00ad", "")
    )
    normalized = _DEHYPHENATE_WRAP_RE.sub(r"\1\2", normalized)
    normalized = " ".join(normalized.split())
    if strip_outer_quotes:
        normalized = normalized.strip()
        while (
            len(normalized) >= 2
            and normalized[0] == normalized[-1]
            and normalized[0] in ('"', "'")
        ):
            normalized = normalized[1:-1].strip()
    return normalized


def _load_manifest_and_sources(sources_root: Path) -> dict[str, _LoadedSource]:
    if not sources_root.exists() or not sources_root.is_dir():
        raise CitationVerificationError(f"Sources directory not found: {sources_root}")

    manifest_path = sources_root / "manifest.json"
    if not manifest_path.is_file():
        raise CitationVerificationError(f"Missing citation manifest: {manifest_path}")

    try:
        raw_manifest = manifest_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CitationVerificationError(f"Cannot read {manifest_path}: {exc}") from exc

    if not raw_manifest.strip():
        raise CitationVerificationError(f"Empty citation manifest: {manifest_path}")

    try:
        manifest = json.loads(raw_manifest)
    except json.JSONDecodeError as exc:
        raise CitationVerificationError(f"Invalid JSON in {manifest_path}: {exc}") from exc

    if not isinstance(manifest, dict):
        raise CitationVerificationError(f"{manifest_path}: manifest must be a JSON object")

    schema_version = manifest.get("schema_version")
    if isinstance(schema_version, bool) or schema_version != 1:
        raise CitationVerificationError(
            f"{manifest_path}: schema_version must equal 1, got {schema_version!r}"
        )

    sources_obj = manifest.get("sources")
    if not isinstance(sources_obj, dict):
        raise CitationVerificationError(f"{manifest_path}: 'sources' must be a JSON object")

    loaded: dict[str, _LoadedSource] = {}
    for source_name, entry in sources_obj.items():
        if (
            not isinstance(source_name, str)
            or "/" in source_name
            or "\\" in source_name
            or ".." in source_name
            or not source_name.endswith(".numbered.txt")
            or len(source_name) <= len(".numbered.txt")
        ):
            raise CitationVerificationError(
                f"{manifest_path}: invalid source filename key {source_name!r}; "
                "must be a bare '<name>.numbered.txt' filename"
            )
        if not isinstance(entry, dict):
            raise CitationVerificationError(
                f"{manifest_path}: manifest entry for {source_name!r} must be an object"
            )
        expected_sha = entry.get("sha256")
        if not isinstance(expected_sha, str) or not _SHA256_LOWER_HEX_RE.match(expected_sha):
            raise CitationVerificationError(
                f"{manifest_path}: sha256 for {source_name!r} must be 64 lowercase hex chars"
            )

        source_path = sources_root / source_name
        if not source_path.is_file():
            raise CitationVerificationError(f"Missing numbered source extract: {source_path}")

        try:
            raw_bytes = source_path.read_bytes()
        except OSError as exc:
            raise CitationVerificationError(f"Cannot read {source_path}: {exc}") from exc

        actual_sha = hashlib.sha256(raw_bytes).hexdigest()
        if actual_sha != expected_sha:
            raise CitationVerificationError(
                f"SHA-256 mismatch for {source_name}: manifest={expected_sha}, actual={actual_sha}"
            )

        try:
            source_text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise CitationVerificationError(
                f"{source_path}: source file is not valid UTF-8: {exc}"
            ) from exc

        lines_map: dict[tuple[int, int], str] = {}
        for idx, raw_line in enumerate(source_text.splitlines(), start=1):
            clean_line = raw_line.rstrip("\r\n")
            if not clean_line.strip():
                continue
            match = _NUMBERED_LINE_RE.match(clean_line)
            if match is None:
                raise CitationVerificationError(
                    f"{source_path}:{idx}: malformed numbered line {clean_line!r}; "
                    "expected '[Page <p>, Line <l>] <text>'"
                )
            page_num = int(match.group(1))
            line_num = int(match.group(2))
            if page_num < 1 or line_num < 1:
                raise CitationVerificationError(
                    f"{source_path}:{idx}: page_num ({page_num}) and line_num ({line_num}) must be >= 1"
                )
            coord = (page_num, line_num)
            if coord in lines_map:
                raise CitationVerificationError(
                    f"{source_path}:{idx}: duplicate coordinate (Page {page_num}, Line {line_num})"
                )
            lines_map[coord] = match.group(3)

        ordered_text = "\n".join(lines_map[coord] for coord in sorted(lines_map))
        loaded[source_name] = _LoadedSource(
            sha256=expected_sha,
            lines=lines_map,
            normalized_full_text=_normalize_citation_text(ordered_text, strip_outer_quotes=False),
        )

    return loaded


def _validate_coord_tag(
    line_index: int,
    start_col: int,
    end_col: int,
    raw_tag: str,
    sources: dict[str, _LoadedSource],
    errors: list[str],
) -> _ParsedCoordTag:
    match = _COORD_TAG_RE.match(raw_tag)
    if match is None:
        errors.append(f"Line {line_index + 1}: malformed citation tag {raw_tag!r}")
        return _ParsedCoordTag(
            line_index=line_index,
            start_col=start_col,
            end_col=end_col,
            raw_text=raw_tag,
        )

    source_name = match.group(1).strip()
    sha_hex = match.group(2).strip()
    page_num = int(match.group(3))
    line_start = int(match.group(4))
    line_end = int(match.group(5)) if match.group(5) is not None else line_start

    tag_valid = True
    if source_name not in sources:
        errors.append(
            f"Line {line_index + 1}: cited source {source_name!r} is not present in manifest.json"
        )
        tag_valid = False
    else:
        source_obj = sources[source_name]
        if not _SHA256_LOWER_HEX_RE.match(sha_hex):
            errors.append(
                f"Line {line_index + 1}: invalid sha256 {sha_hex!r} for {source_name}"
            )
            tag_valid = False
        elif sha_hex != source_obj.sha256:
            errors.append(
                f"Line {line_index + 1}: sha256 mismatch for {source_name}: "
                f"cited={sha_hex}, manifest={source_obj.sha256}"
            )
            tag_valid = False

    if page_num < 1:
        errors.append(f"Line {line_index + 1}: invalid Page {page_num} (< 1)")
        tag_valid = False
    if line_start < 1 or line_end < line_start:
        errors.append(
            f"Line {line_index + 1}: invalid line range Line {line_start}-{line_end}"
        )
        tag_valid = False

    normalized_window: str | None = None
    if tag_valid:
        source_obj = sources[source_name]
        missing_lines = [
            line_num
            for line_num in range(line_start, line_end + 1)
            if (page_num, line_num) not in source_obj.lines
        ]
        if missing_lines:
            errors.append(
                f"Line {line_index + 1}: missing line(s) {missing_lines} in "
                f"{source_name} Page {page_num}"
            )
            tag_valid = False
        else:
            window_lines = [
                source_obj.lines[(page_num, line_num)]
                for line_num in range(line_start, line_end + 1)
            ]
            normalized_window = _normalize_citation_text(
                "\n".join(window_lines), strip_outer_quotes=False
            )

    return _ParsedCoordTag(
        line_index=line_index,
        start_col=start_col,
        end_col=end_col,
        raw_text=raw_tag,
        source_name=source_name,
        page_num=page_num,
        line_start=line_start,
        line_end=line_end,
        normalized_window=normalized_window,
        is_valid=tag_valid,
    )


def _adjacent_nonblank_tags(
    lines: list[str],
    start_idx: int,
    step: int,
    tags_by_line: list[list[_ParsedCoordTag]],
) -> list[_ParsedCoordTag]:
    idx = start_idx + step
    while 0 <= idx < len(lines):
        if not lines[idx].strip():
            idx += step
            continue
        if _BLOCKQUOTE_LINE_RE.match(lines[idx]) is not None:
            return []
        return tags_by_line[idx]
    return []


def verify_citations(
    doc_path: Path | str,
    sources_dir: Path | str,
    strict_coords: bool = True,
) -> CitationVerificationResult:
    """Verify markdown blockquotes and coordinate tags against numbered sources and manifest."""
    doc = Path(doc_path)
    sources_root = Path(sources_dir)

    if not doc.is_file():
        raise CitationVerificationError(f"Citation document not found: {doc}")
    try:
        doc_text = doc.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        raise CitationVerificationError(f"Cannot read {doc}: {exc}") from exc

    sources = _load_manifest_and_sources(sources_root)

    raw_lines = doc_text.splitlines()
    errors: list[str] = []
    warnings: list[str] = []

    tags_by_line: list[list[_ParsedCoordTag]] = [[] for _ in raw_lines]
    checked_coords = 0
    for line_idx, raw_line in enumerate(raw_lines):
        bq_match = _BLOCKQUOTE_LINE_RE.match(raw_line)
        search_text = bq_match.group(1) if bq_match is not None else raw_line
        for tag_match in _ANY_SOURCE_BRACKET_RE.finditer(search_text):
            checked_coords += 1
            parsed_tag = _validate_coord_tag(
                line_index=line_idx,
                start_col=tag_match.start(),
                end_col=tag_match.end(),
                raw_tag=tag_match.group(0),
                sources=sources,
                errors=errors,
            )
            tags_by_line[line_idx].append(parsed_tag)

    checked_quotes = 0
    idx = 0
    while idx < len(raw_lines):
        bq_match = _BLOCKQUOTE_LINE_RE.match(raw_lines[idx])
        if bq_match is None:
            idx += 1
            continue

        block_start = idx
        block_inner_lines: list[str] = []
        while idx < len(raw_lines):
            inner_match = _BLOCKQUOTE_LINE_RE.match(raw_lines[idx])
            if inner_match is None:
                break
            block_inner_lines.append(inner_match.group(1))
            idx += 1
        block_end = idx - 1

        checked_quotes += 1
        final_line_tags = [
            tag for tag in tags_by_line[block_end] if _COORD_TAG_RE.match(tag.raw_text) is not None
        ]
        if final_line_tags:
            parts: list[str] = []
            cursor = 0
            final_line = block_inner_lines[-1]
            for tag in final_line_tags:
                parts.append(final_line[cursor : tag.start_col])
                cursor = tag.end_col
            parts.append(final_line[cursor:])
            remainder = "".join(parts)
            if _ATTRIBUTION_REMAINDER_RE.match(remainder):
                body_lines = block_inner_lines[:-1]
            else:
                body_lines = [*block_inner_lines[:-1], remainder]
        else:
            body_lines = block_inner_lines

        normalized_quote = _normalize_citation_text(
            "\n".join(body_lines), strip_outer_quotes=True
        )
        if len(normalized_quote) == 0:
            errors.append(
                f"Line {block_start + 1}: empty blockquote body after normalization"
            )
            continue

        bound_tags = [
            *tags_by_line[block_end],
            *_adjacent_nonblank_tags(raw_lines, block_start, -1, tags_by_line),
            *_adjacent_nonblank_tags(raw_lines, block_end, +1, tags_by_line),
        ]
        if not bound_tags:
            uncited_msg = (
                f"Line {block_start + 1}: blockquote has no bound [source: ...] coordinate tag"
            )
            if strict_coords:
                errors.append(uncited_msg)
            else:
                warnings.append(uncited_msg)
            continue

        valid_tags = [tag for tag in bound_tags if tag.is_valid]
        if not valid_tags:
            # Invalid coordinate / SHA / missing-line error was already recorded per tag.
            continue

        if any(
            tag.normalized_window is not None and normalized_quote in tag.normalized_window
            for tag in valid_tags
        ):
            continue

        if strict_coords:
            coord_summary = ", ".join(
                f"{tag.source_name} Page {tag.page_num}, Line {tag.line_start}-{tag.line_end}"
                for tag in valid_tags
            )
            errors.append(
                f"Line {block_start + 1}: quoted text not found verbatim inside cited coordinate "
                f"window(s) [{coord_summary}]"
            )
        else:
            matched_tag = next(
                (
                    tag
                    for tag in valid_tags
                    if tag.source_name is not None
                    and normalized_quote in sources[tag.source_name].normalized_full_text
                ),
                None,
            )
            if matched_tag is not None:
                warnings.append(
                    f"Line {block_start + 1}: quote matches {matched_tag.source_name} outside "
                    f"cited coordinate window Page {matched_tag.page_num}, "
                    f"Line {matched_tag.line_start}-{matched_tag.line_end}"
                )
            else:
                errors.append(
                    f"Line {block_start + 1}: quoted text not found verbatim anywhere in cited "
                    f"source file(s)"
                )

    return CitationVerificationResult(
        ok=len(errors) == 0,
        doc_path=str(doc),
        sources_dir=str(sources_root),
        strict_coords=strict_coords,
        checked_quotes=checked_quotes,
        checked_coords=checked_coords,
        errors=tuple(errors),
        warnings=tuple(warnings),
    )
