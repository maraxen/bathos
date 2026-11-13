"""The charter purpose a sidecar's work serves, written when the sidecar is written (spec 261104, T7, owner 261109).

A sidecar may declare which charter purpose(s) its run serves::

    [charter]
    purpose = "lagoon-simulation"          # or ["lagoon-simulation", "active-learning"]

The label is the at-the-time-of-work half of AC-9: readers are scored against it later. It must never reach a
reader, so the packet builder strips the ``[charter]`` table before a sidecar enters a packet, and fails closed
(excludes the sidecar) when the table cannot be removed cleanly.

Labels are readable slugs, not opaque ids, so a person reading a sidecar can tell what it is for.
"""

from __future__ import annotations

import html
import re
import tomllib
import unicodedata
from collections.abc import Iterable
from typing import Any

from bathos.errors import BearingsError

PURPOSE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LABEL_TABLE = "charter"
# Reserved label value: real work that serves no charter purpose. It stands alone in a label.
OFF_CHARTER = "off-charter"
# Ids matched as leak needles must be at least this long AND contain a separator: a one-word id (`size`, `mpnn`)
# would match ordinary prose and withhold most of a record (review round 2, 261110).
MIN_ID_CHARS = 4

# Hyphen-like characters outside Unicode's dash category (Pd): soft hyphen (Cf) and hyphen bullet (Po).
_HYPHEN_LIKE = frozenset("­⁃")
# Escaped forms a reader would decode: URL-encoded separators, and any backslash-u escape (decoded below only when
# it encodes a dash, soft hyphen, hyphen bullet or underscore).
_ENCODED_SEP_RE = re.compile(r"%2d|%5f|\\u([0-9a-f]{4})", re.IGNORECASE)
# HTML character references, only when terminated by ';' (so `&shy` in prose stays text; review round 4, 261110).
_ENTITY_RE = re.compile(r"&(?:#\d+|#x[0-9a-f]+|[a-z][a-z0-9]*);", re.IGNORECASE)
# A hyphen broken across a line, between two letters or digits (a lone dash ending a line is not joined).
_BROKEN_HYPHEN_RE = re.compile(r"(?<=[A-Za-z0-9])-[ \t]*\r?\n[ \t>#*]*(?=[A-Za-z0-9])")


def _is_hyphen(ch: str) -> bool:
    return ch in _HYPHEN_LIKE or unicodedata.category(ch) == "Pd"


def _encoded_sep(m: re.Match[str]) -> str:
    s = m.group(0).lower()
    if s == "%2d":
        return "-"
    if s == "%5f":
        return "_"
    ch = chr(int(m.group(1), 16))
    return "-" if _is_hyphen(ch) else "_" if ch == "_" else m.group(0)


def fold_for_ids(text: str) -> str:
    """Normalise text before id matching, so the forms a reader would see as a charter id are matched as one.

    HTML entities and escaped separators are decoded; NFKC is applied; every dash character (Unicode Pd), the soft
    hyphen and the hyphen bullet become ``-``; other invisible format characters (zero-width) are removed; and a
    hyphen broken across a line between two letters or digits is joined (review rounds 2 and 3, 261110).

    Out of scope: deliberate evasion such as homoglyphs from other scripts or combining marks. The threat is a slug
    leaking by accident, from an agent or a person writing records.
    """
    t = text
    if not t.isascii():
        t = unicodedata.normalize(
            "NFKC", t
        )  # first, so a fullwidth ＆ or ％ decodes below (round 4)
    if "&" in t:
        t = _ENTITY_RE.sub(lambda m: html.unescape(m.group(0)), t)
    if "%" in t or "\\" in t:
        t = _ENCODED_SEP_RE.sub(_encoded_sep, t)
    if not t.isascii():
        out = []
        for ch in t:
            if _is_hyphen(ch):
                out.append("-")
            elif unicodedata.category(ch) != "Cf":
                out.append(ch)
        t = "".join(out)
    return _BROKEN_HYPHEN_RE.sub("-", t) if "\n" in t else t


def id_token_pattern(pid: str) -> re.Pattern[str]:
    """Whole-token, case-insensitive match for a charter id; hyphen and underscore spellings both count.

    Charter ids are readable slugs, so they are ordinary words that can appear anywhere in a record; the packet
    builder uses this to keep any record naming one away from readers. Match against ``fold_for_ids(text)``.
    """
    # Only letters and digits bound a token, so `beta_aim_rerun` and `pre-beta-aim` name `beta-aim`, while
    # `xbeta-aim` and `beta-aims` do not. Either separator in the stored id matches either in the text.
    body = re.sub(r"\\-|_", "[-_]", re.escape(pid))
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


def id_patterns(ids: Iterable[str]) -> tuple[list[tuple[str, re.Pattern[str]]], list[str]]:
    """Patterns for the ids that can be matched safely, plus ``off-charter``; and the ids left unmatched.

    The packet builder and the AC-9 scorer both use this, so they agree on which ids are needles.
    """
    pats: list[tuple[str, re.Pattern[str]]] = []
    unmatched: list[str] = []
    for pid in sorted(set(ids) | {OFF_CHARTER}):
        if len(pid) < MIN_ID_CHARS or not re.search(r"[A-Za-z0-9][-_][A-Za-z0-9]", pid):
            unmatched.append(pid)
        else:
            pats.append((pid, id_token_pattern(pid)))
    return pats, unmatched


def ids_named(text: str, pats: list[tuple[str, re.Pattern[str]]]) -> set[str]:
    """The ids from ``pats`` that ``text`` names, after ``fold_for_ids``."""
    folded = fold_for_ids(text)
    return {pid for pid, p in pats if p.search(folded)}


_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


class PurposeLabelError(BearingsError, ValueError):
    """A ``[charter]`` table that is malformed, or that cannot be stripped without touching anything else."""


def read_purpose_label(data: dict[str, Any]) -> list[str] | None:
    """The declared purpose slugs from parsed sidecar TOML, or None when the sidecar declares none.

    Raises ``PurposeLabelError`` on a malformed table: a scorer must never guess what a broken label meant.
    """
    table = data.get(LABEL_TABLE)
    if table is None:
        return None
    if not isinstance(table, dict):
        raise PurposeLabelError(f"[{LABEL_TABLE}] must be a table")
    unknown = sorted(set(table) - {"purpose"})
    if unknown:
        raise PurposeLabelError(
            f"[{LABEL_TABLE}] has unknown key(s) {unknown}; only 'purpose' is allowed"
        )
    if "purpose" not in table:
        raise PurposeLabelError(f"[{LABEL_TABLE}] needs 'purpose'")
    value = table["purpose"]
    slugs = [value] if isinstance(value, str) else value
    if not isinstance(slugs, list) or not slugs or not all(isinstance(s, str) for s in slugs):
        raise PurposeLabelError(
            f"[{LABEL_TABLE}] purpose must be a slug or a non-empty list of slugs; got {value!r}"
        )
    bad = [s for s in slugs if not PURPOSE_SLUG_RE.match(s)]
    if bad:
        raise PurposeLabelError(
            f"[{LABEL_TABLE}] purpose {bad!r} is not a readable slug (lowercase letters, digits, single hyphens)"
        )
    if len(set(slugs)) != len(slugs):
        raise PurposeLabelError(f"[{LABEL_TABLE}] purpose repeats a slug: {slugs!r}")
    if OFF_CHARTER in slugs and len(slugs) > 1:
        raise PurposeLabelError(
            f"[{LABEL_TABLE}] purpose {OFF_CHARTER!r} means the work serves no charter purpose; "
            f"it cannot be combined with others: {slugs!r}"
        )
    return sorted(slugs)


def _is_label_header(name: str) -> bool:
    head = name.split(".", 1)[0].strip().strip('"').strip("'")
    return head == LABEL_TABLE


def strip_purpose_label(text: str) -> tuple[str, bool]:
    """Remove the ``[charter]`` table from sidecar text. Returns ``(text, stripped)``.

    Text without the table is returned unchanged. The result is re-parsed and must equal the original minus the
    table; anything else (an inline table, a dotted key, a multi-line string that fools the line scan, text that
    does not parse) raises ``PurposeLabelError`` so the caller can exclude the sidecar instead of leaking the label.
    """
    try:
        original = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        if LABEL_TABLE in text:
            raise PurposeLabelError(
                f"sidecar does not parse, so a label cannot be ruled out: {e}"
            ) from e
        return text, False
    if LABEL_TABLE not in original:
        return text, False
    # A malformed label ([[charter]], [charter.note], purpose = "L5") is refused here, so the sidecar is excluded
    # at build time instead of passing through stripped and later failing the scorer.
    read_purpose_label(original)

    kept: list[str] = []
    in_label = False
    for line in text.splitlines(keepends=True):
        header = _HEADER_RE.match(line)
        if header:
            in_label = _is_label_header(header.group(1))
        if not in_label:
            kept.append(line)
    stripped = "".join(kept)

    try:
        after = tomllib.loads(stripped)
    except tomllib.TOMLDecodeError as e:
        raise PurposeLabelError(
            f"removing [{LABEL_TABLE}] left text that does not parse: {e}"
        ) from e
    expected = {k: v for k, v in original.items() if k != LABEL_TABLE}
    if after != expected:
        raise PurposeLabelError(
            f"[{LABEL_TABLE}] could not be removed cleanly (inline table, dotted key or a string spanning lines)"
        )
    return stripped, True
