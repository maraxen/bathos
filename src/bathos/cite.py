"""Citation formatting and strict-coordinate markdown verification for bathos."""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
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
_FILE_LINE_BRACKET_RE = re.compile(
    r"^\[\s*source\s*:\s*([^,:\]\s]+)\s*:\s*L?(-?\d+)(?:\s*-\s*L?(-?\d+))?"
    r"(?:\s*,\s*sha256\s*:\s*([^,\]]+?))?\s*\]$",
    re.IGNORECASE,
)
_BARE_FILE_LINE_RE = re.compile(
    r"(?<![A-Za-z0-9_./\\-])"
    r"([A-Za-z0-9_.-]+\.(?:numbered\.txt|txt))"
    r":L?(-?\d+)(?:-L?(-?\d+))?"
    r"(?![A-Za-z0-9_])"
)
_BLOCKQUOTE_LINE_RE = re.compile(r"^\s*>\s?(.*)$")
_ATTRIBUTION_REMAINDER_RE = re.compile(r"^\s*(?:[-—–]\s*)?$")
_DEHYPHENATE_WRAP_RE = re.compile(r"([A-Za-z0-9])-\s*\n\s*([A-Za-z0-9])")

# Extraction guard patterns (#2345)
_WIDE_GUTTER_RE = re.compile(r"\S(?:\s{4,}|\t+)\S")
_GUTTER_SPLIT_RE = re.compile(r"\s{4,}|\t+")
_ALPHA_WORD_RE = re.compile(r"[A-Za-z]{2,}")
_PROSE_COL_JUMP_RE = re.compile(r"[a-z,;:]\s{4,}[A-Z][a-z]+")
_PIPE_TABLE_SEP_RE = re.compile(r"(?:^|\s)\|(?:\s|$)")
_UNIT_PATTERN = (
    r"ms|ns|us|µs|kg|mg|ug|km|cm|mm|nm|GB|MB|KB|TB|Hz|kHz|MHz|GHz|dB|kcal|kJ|mol|mM|uM|nM|pM"
)
_PURE_NUMERIC_CELL_RE = re.compile(
    rf"^(?:[-+\u2212]?\d+(?:,\d{{3}})*(?:\.\d+)?(?:%|\s*(?:{_UNIT_PATTERN}))?"
    rf"(?:\s*[±+/-]\s*\d+(?:\.\d+)?(?:%|\s*(?:{_UNIT_PATTERN}))?)?|n/?a|—|-)$",
    re.IGNORECASE,
)

_INLINE_QUOTE_RE = re.compile(r'"([^"\n]{2,})"|“([^”\n]{2,})”')
_MARKDOWN_HEADING_OR_LIST_RE = re.compile(r"^\s*(?:#+\s+|(?:\d+\.|[-*+])\s+)")
_STRUCTURAL_LABEL_RE = re.compile(
    r"\b(?:Fig(?:ure)?\.?|Table|Tab\.?|Eq(?:uation)?\.?|Section|Sec\.?|Page|Line)\s*\d+[A-Za-z]?\b",
    re.IGNORECASE,
)
_FIGURE_REF_RE = re.compile(
    r"\b(?:Fig(?:ure)?\.?\s*\d+[A-Za-z]?"
    r"|(?:bar|scatter|line|box|violin|roc|pr|loss|training|learning)\s+(?:plot|chart|graph|curve)s?"
    r"|(?:plot|chart|graph|curve)\s*\d+[A-Za-z]?"
    r"|(?:in|from|on|see|of|shown\s+in)\s+(?:the\s+|this\s+|that\s+)?(?:plot|chart|graph|curve)s?"
    r"|(?:the|this|that)\s+(?:plot|chart|graph|curve)s?)\b",
    re.IGNORECASE,
)
_VISUAL_READ_CUE_RE = re.compile(
    r"\b(?:visually(?:\s+read|\s+estimated)?|read\s+off|read\s+from|estimated\s+from|"
    r"eyeballed(?:\s+from)?|from\s+(?:the\s+|this\s+|that\s+)?(?:fig(?:ure)?|plot|chart|graph|curve)s?)\b",
    re.IGNORECASE,
)
_FIGURE_CAPTION_LINE_RE = re.compile(r"^\s*(?:Fig(?:ure)?\.?\s*\d+[A-Za-z]?)\s*[:.\-—]", re.IGNORECASE)
_CAPTION_BREAK_LINE_RE = re.compile(
    r"^\s*(?:#+\s+|(?:Table|Tab\.?|Section|Sec\.?|Algorithm|Listing)\s+\d+|\d+(?:\.\d+)*\s+[A-Z])",
    re.IGNORECASE,
)
_TABLE_LINE_RE = re.compile(r"^\s*(?:Table\s+\d+|Tab\.?\s+\d+|\|.*\|)", re.IGNORECASE)

_NUMBER_METRIC_RE = re.compile(
    r"(?<![A-Za-z0-9_.])"
    r"[-+\u2212]?\d+(?:,\d{3})*(?:\.\d+)?"
    rf"(?:[eE][-+\u2212]?\d+|%|-fold\b|[x×]\b|\s*(?:{_UNIT_PATTERN})\b)?"
    r"(?![A-Za-z0-9_])"
)
_NUMBER_UNIT_SPLIT_RE = re.compile(rf"^(?P<num>.+?)\s*(?P<unit>{_UNIT_PATTERN})$")


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
    checked_numbers: int = 0


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
    is_file_line: bool = False
    window_file_lines: tuple[int, ...] = ()
    window_coords: tuple[tuple[int, int], ...] = ()


@dataclass(frozen=True)
class _LoadedSource:
    sha256: str
    lines: dict[tuple[int, int], str]
    normalized_full_text: str
    file_lines: dict[int, str] = field(default_factory=dict)
    coord_to_file_line: dict[tuple[int, int], int] = field(default_factory=dict)
    two_column_lines: dict[int, str] = field(default_factory=dict)
    table_lines: frozenset[int] = field(default_factory=frozenset)
    figure_caption_lines: frozenset[int] = field(default_factory=frozenset)
    verified_numbers: frozenset[str] = field(default_factory=frozenset)
    manifest_two_column: bool = False


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
        .replace("\u2212", "-")
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


def _is_two_column_interleaved_line(line: str) -> str | None:
    """Return a diagnostic reason if ``line`` looks like interleaved two-column PDF text."""
    stripped = line.strip()
    if not stripped:
        return None
    if _TABLE_LINE_RE.match(stripped) is not None or _PIPE_TABLE_SEP_RE.search(stripped) is not None:
        return None
    if _WIDE_GUTTER_RE.search(stripped) is None:
        return None

    segments = [seg.strip() for seg in _GUTTER_SPLIT_RE.split(stripped) if seg.strip()]
    if len(segments) < 2:
        return None

    # Predominantly numeric multi-column rows or aligned tables with numeric cells are not prose interleaving.
    numeric_segments = sum(1 for seg in segments if _PURE_NUMERIC_CELL_RE.match(seg))
    if numeric_segments >= 2 or (len(segments) >= 3 and numeric_segments >= 1):
        return None

    # Aligned 3+ column table headers of short labels without sentence punctuation.
    if len(segments) >= 3 and all(
        len(seg.split()) <= 3 and not re.search(r"[.?!]", seg) for seg in segments
    ):
        return None

    prose_segments = [seg for seg in segments if len(_ALPHA_WORD_RE.findall(seg)) >= 2]
    if len(prose_segments) >= 2:
        return "wide column gutter separating multi-word prose segments"

    if _PROSE_COL_JUMP_RE.search(stripped) is not None and any(
        len(_ALPHA_WORD_RE.findall(seg)) >= 1 for seg in segments
    ):
        return "mid-line prose column jump across wide whitespace gutter"

    return None


def detect_two_column_interleaving(lines: Iterable[str]) -> list[tuple[int, str]]:
    """Detect two-column PDF interleaving artifacts across an iterable of source lines."""
    findings: list[tuple[int, str]] = []
    for line_num, raw_line in enumerate(lines, start=1):
        match = _NUMBERED_LINE_RE.match(raw_line.strip())
        content = match.group(3) if match is not None else raw_line
        reason = _is_two_column_interleaved_line(content)
        if reason is not None:
            findings.append((line_num, reason))
    return findings


def _is_table_grounding_line(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if _TABLE_LINE_RE.match(stripped) is not None:
        return True
    if _WIDE_GUTTER_RE.search(stripped) is not None:
        segments = [seg.strip() for seg in _GUTTER_SPLIT_RE.split(stripped) if seg.strip()]
        if sum(1 for seg in segments if _PURE_NUMERIC_CELL_RE.match(seg)) >= 2:
            return True
    return False


def _normalize_number_token(token: str) -> str:
    norm = unicodedata.normalize("NFKC", token).replace("\u2212", "-").strip()
    norm = re.sub(r"(?<=\d),(?=\d{3}\b)", "", norm)
    norm = re.sub(r"\s+", " ", norm)
    return norm


def _extract_cited_numbers(text: str) -> list[str]:
    """Extract cited numeric/metric tokens from prose after stripping structural labels."""
    cleaned = _MARKDOWN_HEADING_OR_LIST_RE.sub("", text)
    cleaned = _STRUCTURAL_LABEL_RE.sub(" ", cleaned)
    cleaned = unicodedata.normalize("NFKC", cleaned).replace("\u2212", "-")
    seen: list[str] = []
    for match in _NUMBER_METRIC_RE.finditer(cleaned):
        tok = _normalize_number_token(match.group(0))
        if tok and tok not in seen:
            seen.append(tok)
    return seen


def _escape_numeric_core(num_part: str) -> str:
    """Escape a normalized numeric token while allowing case-insensitive scientific 'e'/'E'."""
    escaped = re.escape(num_part)
    return re.sub(r"(?<=\d)[eE](?=(?:\\[-+])?\d)", "[eE]", escaped)


def _number_present_in_text(token: str, normalized_text: str) -> bool:
    """Return True if ``token`` matches a numeric/metric occurrence in ``normalized_text``."""
    target = _normalize_number_token(token)
    norm_haystack = re.sub(
        r"(?<=\d),(?=\d{3}\b)",
        "",
        unicodedata.normalize("NFKC", normalized_text).replace("\u2212", "-"),
    )
    has_explicit_sign = target.startswith(("-", "+"))
    lookbehind = r"(?<![A-Za-z0-9_.])" if has_explicit_sign else r"(?<![A-Za-z0-9_.+-])"
    trailing_guard = r"(?![A-Za-z0-9_%×]|-fold\b|\.\d|\s*%)"

    if target.endswith("%"):
        num_part = _escape_numeric_core(target[:-1].strip())
        pattern = re.compile(rf"{lookbehind}{num_part}\s*(?:%|(?i:percent)\b){trailing_guard}")
    else:
        unit_match = _NUMBER_UNIT_SPLIT_RE.match(target)
        if unit_match is not None:
            num_part = _escape_numeric_core(unit_match.group("num").strip())
            unit_part = re.escape(unit_match.group("unit"))
            pattern = re.compile(rf"{lookbehind}{num_part}\s*{unit_part}{trailing_guard}")
        else:
            core = _escape_numeric_core(target)
            pattern = re.compile(rf"{lookbehind}{core}{trailing_guard}")
    return pattern.search(norm_haystack) is not None


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
        is_numbered = isinstance(source_name, str) and source_name.endswith(".numbered.txt")
        is_plain_txt = (
            isinstance(source_name, str)
            and source_name.endswith(".txt")
            and not is_numbered
        )
        valid_ext = (is_numbered and len(source_name) > len(".numbered.txt")) or (
            is_plain_txt and len(source_name) > len(".txt")
        )
        if (
            not isinstance(source_name, str)
            or "/" in source_name
            or "\\" in source_name
            or ".." in source_name
            or not valid_ext
        ):
            raise CitationVerificationError(
                f"{manifest_path}: invalid source filename key {source_name!r}; "
                "must be a bare '<name>.numbered.txt' or '<name>.txt' filename"
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
        file_lines: dict[int, str] = {}
        coord_to_file_line: dict[tuple[int, int], int] = {}
        two_column_lines: dict[int, str] = {}
        table_lines: set[int] = set()
        figure_caption_lines: set[int] = set()

        in_figure_caption = False
        prev_coord: tuple[int, int] | None = None
        for idx, raw_line in enumerate(source_text.splitlines(), start=1):
            clean_line = raw_line.rstrip("\r\n")
            if is_numbered:
                if not clean_line.strip():
                    in_figure_caption = False
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
                if prev_coord is not None and (
                    prev_coord[0] != page_num or line_num != prev_coord[1] + 1
                ):
                    in_figure_caption = False
                prev_coord = coord
                line_body = match.group(3)
                lines_map[coord] = line_body
                coord_to_file_line[coord] = idx
            else:
                line_body = clean_line

            file_lines[idx] = line_body
            two_col_reason = _is_two_column_interleaved_line(line_body)
            if two_col_reason is not None:
                two_column_lines[idx] = two_col_reason
            is_table = _is_table_grounding_line(line_body)
            if is_table:
                table_lines.add(idx)
            if _FIGURE_CAPTION_LINE_RE.match(line_body):
                in_figure_caption = True
                figure_caption_lines.add(idx)
            elif in_figure_caption:
                if (
                    not line_body.strip()
                    or is_table
                    or _CAPTION_BREAK_LINE_RE.match(line_body) is not None
                ):
                    in_figure_caption = False
                else:
                    figure_caption_lines.add(idx)

        if is_numbered:
            ordered_text = "\n".join(lines_map[coord] for coord in sorted(lines_map))
        else:
            ordered_text = "\n".join(file_lines[i] for i in sorted(file_lines))

        raw_verified_numbers = entry.get("verified_numbers") or ()
        verified_numbers = frozenset(
            _normalize_number_token(str(item))
            for item in raw_verified_numbers
            if str(item).strip()
        )

        loaded[source_name] = _LoadedSource(
            sha256=expected_sha,
            lines=lines_map,
            normalized_full_text=_normalize_citation_text(ordered_text, strip_outer_quotes=False),
            file_lines=file_lines,
            coord_to_file_line=coord_to_file_line,
            two_column_lines=two_column_lines,
            table_lines=frozenset(table_lines),
            figure_caption_lines=frozenset(figure_caption_lines),
            verified_numbers=verified_numbers,
            manifest_two_column=bool(entry.get("two_column_interleaved", False)),
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
        fl_match = _FILE_LINE_BRACKET_RE.match(raw_tag)
        if fl_match is not None:
            return _validate_file_line_tag(
                line_index=line_index,
                start_col=start_col,
                end_col=end_col,
                raw_tag=raw_tag,
                source_name=fl_match.group(1).strip(),
                line_start=int(fl_match.group(2)),
                line_end=int(fl_match.group(3)) if fl_match.group(3) is not None else int(fl_match.group(2)),
                sha_hex=fl_match.group(4).strip() if fl_match.group(4) is not None else None,
                sources=sources,
                errors=errors,
            )
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
    window_file_lines: tuple[int, ...] = ()
    window_coords: tuple[tuple[int, int], ...] = ()
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
            window_coords = tuple((page_num, line_num) for line_num in range(line_start, line_end + 1))
            window_file_lines = tuple(
                source_obj.coord_to_file_line[coord]
                for coord in window_coords
                if coord in source_obj.coord_to_file_line
            )
            window_lines = [source_obj.lines[coord] for coord in window_coords]
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
        window_file_lines=window_file_lines,
        window_coords=window_coords,
    )


def _validate_file_line_tag(
    *,
    line_index: int,
    start_col: int,
    end_col: int,
    raw_tag: str,
    source_name: str,
    line_start: int,
    line_end: int,
    sha_hex: str | None,
    sources: dict[str, _LoadedSource],
    errors: list[str],
) -> _ParsedCoordTag:
    tag_valid = True
    if source_name not in sources:
        errors.append(
            f"Line {line_index + 1}: cited source {source_name!r} is not present in manifest.json"
        )
        tag_valid = False
    elif sha_hex is not None:
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

    if line_start < 1 or line_end < line_start:
        errors.append(
            f"Line {line_index + 1}: invalid line range {source_name}:{line_start}-{line_end}"
        )
        tag_valid = False

    normalized_window: str | None = None
    window_file_lines: tuple[int, ...] = ()
    if tag_valid:
        source_obj = sources[source_name]
        missing_lines = [
            ln for ln in range(line_start, line_end + 1) if ln not in source_obj.file_lines
        ]
        if missing_lines:
            errors.append(
                f"Line {line_index + 1}: missing line(s) {missing_lines} in {source_name}"
            )
            tag_valid = False
        else:
            window_file_lines = tuple(range(line_start, line_end + 1))
            window_lines = [source_obj.file_lines[ln] for ln in window_file_lines]
            normalized_window = _normalize_citation_text(
                "\n".join(window_lines), strip_outer_quotes=False
            )

    return _ParsedCoordTag(
        line_index=line_index,
        start_col=start_col,
        end_col=end_col,
        raw_text=raw_tag,
        source_name=source_name,
        page_num=None,
        line_start=line_start,
        line_end=line_end,
        normalized_window=normalized_window,
        is_valid=tag_valid,
        is_file_line=True,
        window_file_lines=window_file_lines,
    )


def _format_tag_coord(tag: _ParsedCoordTag) -> str:
    if tag.is_file_line or tag.page_num is None:
        if tag.line_start == tag.line_end:
            return f"{tag.source_name}:{tag.line_start}"
        return f"{tag.source_name}:{tag.line_start}-{tag.line_end}"
    return f"{tag.source_name} Page {tag.page_num}, Line {tag.line_start}-{tag.line_end}"


def _check_two_column_artifacts_for_tags(
    line_idx: int,
    valid_tags: list[_ParsedCoordTag],
    sources: dict[str, _LoadedSource],
    errors: list[str],
    reported_tag_keys: set[tuple[int, str, tuple[int, ...]]],
) -> None:
    for tag in valid_tags:
        if tag.source_name is None:
            continue
        src = sources[tag.source_name]
        key = (line_idx, tag.source_name, tag.window_file_lines)
        if key in reported_tag_keys:
            continue
        if src.manifest_two_column:
            reported_tag_keys.add(key)
            errors.append(
                f"Line {line_idx + 1}: two-column PDF interleaving artifact flagged on cited "
                f"source {tag.source_name}; re-extract with column-aware PDF parser"
            )
            continue
        for fl in tag.window_file_lines:
            reason = src.two_column_lines.get(fl)
            if reason is not None:
                reported_tag_keys.add(key)
                errors.append(
                    f"Line {line_idx + 1}: two-column PDF interleaving artifact detected at "
                    f"{tag.source_name}:{fl} ({reason}); re-extract with column-aware PDF parser"
                )
                break


def _has_table_or_numeric_grounding(
    token: str,
    valid_tags: list[_ParsedCoordTag],
    sources: dict[str, _LoadedSource],
    *,
    visual_cue: bool,
) -> bool:
    """Return True if a figure-referenced number has verified table or numeric text grounding."""
    norm_tok = _normalize_number_token(token)
    if valid_tags:
        for tag in valid_tags:
            if tag.source_name is None or tag.normalized_window is None:
                continue
            src = sources[tag.source_name]
            if norm_tok in src.verified_numbers:
                return True
            if not _number_present_in_text(norm_tok, tag.normalized_window):
                continue
            # If the doc explicitly says "visually read from Figure X", require verified table
            # grounding or non-caption numeric text rather than a bare caption mention.
            in_table_line = any(fl in src.table_lines for fl in tag.window_file_lines)
            only_caption_lines = bool(tag.window_file_lines) and all(
                fl in src.figure_caption_lines for fl in tag.window_file_lines
            )
            if visual_cue:
                if in_table_line or not only_caption_lines:
                    return True
            else:
                return True
        return False

    # Uncited figure-read number: must appear in a verified table or manifest verified_numbers.
    for src in sources.values():
        if norm_tok in src.verified_numbers:
            return True
        for fl in src.table_lines:
            if _number_present_in_text(norm_tok, src.file_lines.get(fl, "")):
                return True
    return False


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
    """Verify markdown citations, inline quotes/numbers, and extraction guards against sources."""
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
        bracket_spans: list[tuple[int, int]] = []
        for tag_match in _ANY_SOURCE_BRACKET_RE.finditer(search_text):
            checked_coords += 1
            bracket_spans.append((tag_match.start(), tag_match.end()))
            parsed_tag = _validate_coord_tag(
                line_index=line_idx,
                start_col=tag_match.start(),
                end_col=tag_match.end(),
                raw_tag=tag_match.group(0),
                sources=sources,
                errors=errors,
            )
            tags_by_line[line_idx].append(parsed_tag)

        for fl_match in _BARE_FILE_LINE_RE.finditer(search_text):
            start_col, end_col = fl_match.start(), fl_match.end()
            if any(bs <= start_col and end_col <= be for bs, be in bracket_spans):
                continue
            checked_coords += 1
            parsed_fl = _validate_file_line_tag(
                line_index=line_idx,
                start_col=start_col,
                end_col=end_col,
                raw_tag=fl_match.group(0),
                source_name=fl_match.group(1),
                line_start=int(fl_match.group(2)),
                line_end=int(fl_match.group(3)) if fl_match.group(3) is not None else int(fl_match.group(2)),
                sha_hex=None,
                sources=sources,
                errors=errors,
            )
            tags_by_line[line_idx].append(parsed_fl)

        tags_by_line[line_idx].sort(key=lambda t: t.start_col)

    reported_two_col: set[tuple[int, str, tuple[int, ...]]] = set()
    checked_quotes = 0
    checked_numbers = 0
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
            tag
            for tag in tags_by_line[block_end]
            if (
                _COORD_TAG_RE.match(tag.raw_text) is not None
                or _FILE_LINE_BRACKET_RE.match(tag.raw_text) is not None
                or tag.is_file_line
            )
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

        _check_two_column_artifacts_for_tags(
            block_start, valid_tags, sources, errors, reported_two_col
        )

        if any(
            tag.normalized_window is not None and normalized_quote in tag.normalized_window
            for tag in valid_tags
        ):
            continue

        if strict_coords:
            coord_summary = ", ".join(_format_tag_coord(tag) for tag in valid_tags)
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
                    f"cited coordinate window {_format_tag_coord(matched_tag)}"
                )
            else:
                errors.append(
                    f"Line {block_start + 1}: quoted text not found verbatim anywhere in cited "
                    f"source file(s)"
                )

    # Verify inline quotes, cited numbers/metrics, two-column guards, and figure-read guards
    # on non-blockquote lines (#2345).
    for line_idx, raw_line in enumerate(raw_lines):
        if _BLOCKQUOTE_LINE_RE.match(raw_line) is not None:
            continue
        line_tags = tags_by_line[line_idx]
        valid_tags = [tag for tag in line_tags if tag.is_valid]

        # Strip coordinate tags from the line to inspect the prose claim.
        parts = []
        cursor = 0
        for tag in line_tags:
            parts.append(raw_line[cursor : tag.start_col])
            cursor = tag.end_col
        parts.append(raw_line[cursor:])
        prose = "".join(parts)

        if valid_tags:
            _check_two_column_artifacts_for_tags(
                line_idx, valid_tags, sources, errors, reported_two_col
            )

            # Check inline quoted strings "..." or “...” when cited on this line.
            quote_spans: list[tuple[int, int]] = []
            for q_match in _INLINE_QUOTE_RE.finditer(prose):
                quote_spans.append((q_match.start(), q_match.end()))
                raw_q = q_match.group(1) if q_match.group(1) is not None else q_match.group(2)
                norm_q = _normalize_citation_text(raw_q, strip_outer_quotes=True)
                if not norm_q:
                    continue
                checked_quotes += 1
                if any(
                    tag.normalized_window is not None and norm_q in tag.normalized_window
                    for tag in valid_tags
                ):
                    continue
                if strict_coords:
                    coord_summary = ", ".join(_format_tag_coord(tag) for tag in valid_tags)
                    errors.append(
                        f"Line {line_idx + 1}: inline quoted text {norm_q!r} not found at cited "
                        f"coordinate window(s) [{coord_summary}]"
                    )
                else:
                    matched_tag = next(
                        (
                            tag
                            for tag in valid_tags
                            if tag.source_name is not None
                            and norm_q in sources[tag.source_name].normalized_full_text
                        ),
                        None,
                    )
                    if matched_tag is not None:
                        warnings.append(
                            f"Line {line_idx + 1}: inline quote matches {matched_tag.source_name} "
                            f"outside cited coordinate window {_format_tag_coord(matched_tag)}"
                        )
                    else:
                        errors.append(
                            f"Line {line_idx + 1}: inline quoted text {norm_q!r} not found "
                            "anywhere in cited source file(s)"
                        )

            # Remove inline quotes (already checked verbatim) before extracting numeric claims.
            unquoted_parts: list[str] = []
            q_cursor = 0
            for qs, qe in quote_spans:
                unquoted_parts.append(prose[q_cursor:qs])
                q_cursor = qe
            unquoted_parts.append(prose[q_cursor:])
            prose_for_numbers = "".join(unquoted_parts)

            cited_numbers = _extract_cited_numbers(prose_for_numbers)
            for num_tok in cited_numbers:
                checked_numbers += 1
                if any(
                    tag.normalized_window is not None
                    and _number_present_in_text(num_tok, tag.normalized_window)
                    for tag in valid_tags
                ):
                    continue
                if strict_coords:
                    coord_summary = ", ".join(_format_tag_coord(tag) for tag in valid_tags)
                    errors.append(
                        f"Line {line_idx + 1}: cited number/metric {num_tok!r} not found at "
                        f"cited coordinate window(s) [{coord_summary}]"
                    )
                else:
                    matched_tag = next(
                        (
                            tag
                            for tag in valid_tags
                            if tag.source_name is not None
                            and _number_present_in_text(
                                num_tok, sources[tag.source_name].normalized_full_text
                            )
                        ),
                        None,
                    )
                    if matched_tag is not None:
                        warnings.append(
                            f"Line {line_idx + 1}: cited number/metric {num_tok!r} matches "
                            f"{matched_tag.source_name} outside cited coordinate window "
                            f"{_format_tag_coord(matched_tag)}"
                        )
                    else:
                        errors.append(
                            f"Line {line_idx + 1}: cited number/metric {num_tok!r} not found "
                            "anywhere in cited source file(s)"
                        )

        # Figure-read numeric guard (#2345): flag numeric claims attributed to figures/plots
        # lacking verified table or numeric grounding.
        if not line_tags or valid_tags:
            fig_match = _FIGURE_REF_RE.search(prose)
            if fig_match is not None:
                fig_numbers = _extract_cited_numbers(prose)
                if fig_numbers:
                    visual_cue = _VISUAL_READ_CUE_RE.search(prose) is not None
                    ungrounded = [
                        num
                        for num in fig_numbers
                        if not _has_table_or_numeric_grounding(
                            num, valid_tags, sources, visual_cue=visual_cue
                        )
                    ]
                    if ungrounded:
                        errors.append(
                            f"Line {line_idx + 1}: figure-read numeric value(s) {ungrounded} "
                            f"referencing {fig_match.group(0)!r} lack verified table/numeric grounding"
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
        checked_numbers=checked_numbers,
    )
