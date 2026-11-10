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

import re
import tomllib
from typing import Any

PURPOSE_SLUG_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
LABEL_TABLE = "charter"
# Reserved label value: real work that serves no charter purpose. It stands alone in a label.
OFF_CHARTER = "off-charter"


def id_token_pattern(pid: str) -> re.Pattern[str]:
    """Whole-token, case-insensitive match for a charter id; hyphen and underscore spellings both count.

    Charter ids are readable slugs, so they are ordinary words that can appear anywhere in a record; the packet
    builder uses this to keep any record naming one away from readers.
    """
    # Only letters and digits bound a token, so `beta_aim_rerun` and `pre-beta-aim` name `beta-aim`, while
    # `xbeta-aim` and `beta-aims` do not.
    body = re.escape(pid).replace(r"\-", "[-_]")
    return re.compile(rf"(?<![A-Za-z0-9]){body}(?![A-Za-z0-9])", re.IGNORECASE)


_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(?:#.*)?$")


class PurposeLabelError(ValueError):
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
