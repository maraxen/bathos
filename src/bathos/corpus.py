"""Rule-card corpus: machine-addressable methodological guidance shipped with bathos.

A *card* is one rule with an ID that other machinery can cite. That citability is the whole
point — prose in a handbook cannot be referenced by a lint advisory, recorded in a post-mortem
as `violated_cards`, or evaluated against a specific experiment. An ID can.

Cards live in `agent_assets/corpus/<domain>/<ID>_<slug>.md` as markdown with TOML frontmatter:

    +++
    id = "STAT-001"
    title = "Multiple comparisons inflate the false-positive rate"
    severity = "warning"
    applies_when = "n_outcome_branches > 2"
    source_check = "check_multiple_comparisons"
    tags = ["statistics", "outcomes"]
    see_also = ["STAT-002"]
    +++

    Markdown body: what the rule is, why it matters, what to do.

`applies_when` is an optional DuckDB scalar SQL fragment evaluated against a single row of
facts about a script and its sidecar (see `CONTEXT_COLUMNS`). It shares the evaluation
*mechanism* with `[outcomes].condition` — both go through
`sidecar.single_row_projection` — but NOT the columns: outcome conditions see a script's
`result` dict, while `applies_when` sees the context below.

Design note on failure: a malformed card, or one whose `applies_when` references an unknown
column, is **skipped and reported — never fatal**. One bad card must not make `bth ref
applicable` useless for every other script. Callers read `CorpusLoad.errors` to surface them.

Spec: `.praxia/docs/specs/260729_post-mortem-obligations-targeted-review-schema-and-a-shipped-rule-card-corpus.md`
"""

from __future__ import annotations

import importlib.resources
import re
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

import duckdb

import bathos
from bathos.sidecar import single_row_projection

FRONTMATTER_DELIM = "+++"

#: Columns available to an `applies_when` fragment. Documented here because a card author
#: has no other way to know what they may reference — and because an unknown column makes a
#: card silently inapplicable rather than loudly wrong.
CONTEXT_COLUMNS: dict[str, str] = {
    "script_stem": "str  — script filename without extension",
    "script_dir": "str  — directory bucket, e.g. 'experiments', 'benchmarks', 'debug'",
    "sidecar_kind": "str  — 'experiment' | 'benchmark' | 'validation' | 'debug' | '' if none",
    "stage_name": "str  — declared stage, e.g. 'exploration', 'validation'; '' if unset",
    "has_sidecar": "bool — a <stem>.bth.toml exists next to the script",
    "n_outcome_branches": "int  — count of [outcomes.*] tables declared",
    "n_pass_branches": "int  — outcome branches that are neither residual nor marginal/error/unknown",
    "has_popper": "bool — a [popper] block is declared",
    "has_reproduction": "bool — a [reproduction] block is declared",
    "has_controls": "bool — a [controls] block is declared",
    "declares_novel": "bool — [experiment].novel is true",
    "n_adversarial_checks": "int  — outcome branches carrying an adversarial_check",
    "n_threshold_sources": "int  — outcome branches carrying a source justification",
    "run_count": "int  — runs of this script in the catalog (0 when unavailable)",
    "campaign_run_count": "int  — runs in the script's campaign (0 when unavailable)",
}


class CorpusError(Exception):
    """Raised only for failures that make the corpus unusable as a whole."""


@dataclass(frozen=True)
class Card:
    """One rule card."""

    id: str
    title: str
    path: Path
    body: str
    severity: str = "info"
    applies_when: str | None = None
    source_check: str | None = None
    tags: tuple[str, ...] = ()
    see_also: tuple[str, ...] = ()

    @property
    def domain(self) -> str:
        """Directory bucket the card lives in (``stat``, ``design``, …)."""
        return self.path.parent.name


@dataclass
class CorpusLoad:
    """Result of loading the corpus: the cards that parsed, and why any did not."""

    cards: list[Card] = field(default_factory=list)
    errors: list[tuple[Path, str]] = field(default_factory=list)

    def by_id(self) -> dict[str, Card]:
        return {c.id: c for c in self.cards}


def get_corpus_root() -> Path:
    """Locate ``agent_assets/corpus``.

    Mirrors :func:`bathos.export.get_skill_source_path` — repo layout first for editable
    installs, then packaged data. Kept deliberately parallel so there is one way to find
    shipped assets, not two.
    """
    package_dir = Path(bathos.__file__).parent
    candidate = package_dir.parent.parent / "agent_assets" / "corpus"
    if candidate.is_dir():
        return candidate
    try:
        ref = importlib.resources.files("bathos") / "agent_assets" / "corpus"
        with importlib.resources.as_file(ref) as p:
            if p.is_dir():
                return p
    except Exception:
        pass
    raise CorpusError(
        "Could not locate agent_assets/corpus. Ensure bathos is installed in editable mode "
        "or that package data is complete."
    )


def parse_card(path: Path) -> Card:
    """Parse one card file. Raises ValueError with a path-anchored message on bad input."""
    text = path.read_text(encoding="utf-8")
    if not text.startswith(FRONTMATTER_DELIM):
        raise ValueError(f"{path}: missing opening '{FRONTMATTER_DELIM}' frontmatter delimiter")

    rest = text[len(FRONTMATTER_DELIM) :]
    end = rest.find(f"\n{FRONTMATTER_DELIM}")
    if end == -1:
        raise ValueError(f"{path}: unterminated frontmatter (no closing '{FRONTMATTER_DELIM}')")

    raw_fm = rest[:end]
    body = rest[end + len(FRONTMATTER_DELIM) + 1 :].lstrip("\n")

    try:
        meta = tomllib.loads(raw_fm)
    except tomllib.TOMLDecodeError as e:
        raise ValueError(f"{path}: malformed TOML frontmatter: {e}") from e

    for required in ("id", "title"):
        if not meta.get(required):
            raise ValueError(f"{path}: frontmatter is missing required key '{required}'")

    return Card(
        id=str(meta["id"]),
        title=str(meta["title"]),
        path=path,
        body=body.rstrip() + "\n",
        severity=str(meta.get("severity", "info")),
        applies_when=meta.get("applies_when") or None,
        source_check=meta.get("source_check") or None,
        tags=tuple(meta.get("tags", ())),
        see_also=tuple(meta.get("see_also", ())),
    )


def load_corpus(root: Path | None = None) -> CorpusLoad:
    """Load every card, collecting per-card failures instead of raising on the first one."""
    root = root or get_corpus_root()
    load = CorpusLoad()
    seen: dict[str, Path] = {}

    for path in sorted(root.rglob("*.md")):
        try:
            card = parse_card(path)
        except ValueError as e:
            load.errors.append((path, str(e)))
            continue
        if card.id in seen:
            load.errors.append((path, f"duplicate card id '{card.id}' (also in {seen[card.id]})"))
            continue
        seen[card.id] = path
        load.cards.append(card)

    load.cards.sort(key=lambda c: c.id)
    return load


def get_card(card_id: str, root: Path | None = None) -> Card:
    """Fetch one card by exact id (case-insensitive)."""
    load = load_corpus(root)
    for card in load.cards:
        if card.id.lower() == card_id.lower():
            return card
    raise CorpusError(
        f"No card with id '{card_id}'. Known ids: {', '.join(c.id for c in load.cards)}"
    )


def search_cards(query: str, root: Path | None = None) -> list[Card]:
    """Case-insensitive substring search over id, title, tags and body."""
    load = load_corpus(root)
    q = query.lower().strip()
    if not q:
        return []
    hits = []
    for card in load.cards:
        haystack = " ".join([card.id, card.title, " ".join(card.tags), card.body]).lower()
        if q in haystack:
            hits.append(card)
    return hits


def evaluate_applies_when(fragment: str, context: dict) -> bool:
    """Evaluate one `applies_when` fragment against a context row.

    Raises ``duckdb.Error`` when the fragment is malformed or names an unknown column;
    :func:`applicable_cards` converts that into a skip so one bad card cannot break the run.
    """
    cols = single_row_projection(context)
    rows = duckdb.execute(f"SELECT ({fragment}) FROM (SELECT {cols})").fetchall()
    return bool(rows and rows[0][0])


def applicable_cards(
    context: dict, root: Path | None = None
) -> tuple[list[Card], list[tuple[Card, str]]]:
    """Return ``(fired, unevaluable)`` for the given context.

    A card with no ``applies_when`` never fires — it is reference material, reachable via
    ``show``/``search`` but not claimed to apply to any particular experiment. Silence from
    this function means "no card matched", which is why unevaluable cards are returned
    separately rather than being dropped: a card that *could not* be checked must not be
    indistinguishable from one that was checked and did not match.
    """
    load = load_corpus(root)
    fired: list[Card] = []
    unevaluable: list[tuple[Card, str]] = []

    for card in load.cards:
        if not card.applies_when:
            continue
        try:
            if evaluate_applies_when(card.applies_when, context):
                fired.append(card)
        except Exception as e:
            unevaluable.append((card, str(e).strip().splitlines()[0]))

    return fired, unevaluable


def build_context(script: Path, catalog_dir: Path | None = None) -> dict:
    """Assemble the `applies_when` context row for a script.

    Every key in :data:`CONTEXT_COLUMNS` is always present — a card must never fail merely
    because a fact was unavailable. Catalog-derived counts fall back to 0 when the catalog
    cannot be read, which is why ``run_count``/``campaign_run_count`` are documented as
    "0 when unavailable" rather than as trustworthy zeroes.
    """
    from bathos.sidecar import SidecarError, find_sidecar, parse_sidecar

    script = Path(script)
    ctx: dict = {
        "script_stem": script.stem,
        "script_dir": script.parent.name,
        "sidecar_kind": "",
        "stage_name": "",
        "has_sidecar": False,
        "n_outcome_branches": 0,
        "n_pass_branches": 0,
        "has_popper": False,
        "has_reproduction": False,
        "has_controls": False,
        "declares_novel": False,
        "n_adversarial_checks": 0,
        "n_threshold_sources": 0,
        "run_count": 0,
        "campaign_run_count": 0,
    }

    sidecar_path = None
    try:
        sidecar_path = find_sidecar(script)
    except Exception:
        sidecar_path = None

    if sidecar_path and Path(sidecar_path).exists():
        ctx["has_sidecar"] = True
        try:
            sc = parse_sidecar(Path(sidecar_path))
        except (SidecarError, ValueError, OSError):
            return ctx
        ctx["sidecar_kind"] = getattr(getattr(sc, "kind", None), "value", "") or ""
        ctx["stage_name"] = getattr(sc, "stage_name", "") or ""
        outcomes = getattr(sc, "outcomes", {}) or {}
        ctx["n_outcome_branches"] = len(outcomes)
        ctx["n_pass_branches"] = sum(
            1
            for label, spec in outcomes.items()
            if not getattr(spec, "is_residual", False)
            and label not in ("marginal", "error", "unknown")
        )
        ctx["n_adversarial_checks"] = sum(
            1 for spec in outcomes.values() if getattr(spec, "adversarial_check", None)
        )
        ctx["n_threshold_sources"] = sum(
            1 for spec in outcomes.values() if getattr(spec, "source", "")
        )
        ctx["has_popper"] = getattr(sc, "popper_null_pass_rate", None) is not None
        ctx["has_reproduction"] = bool(
            getattr(sc, "reproduces_paper", "") or getattr(sc, "reproduces_run", "")
        )
        ctx["has_controls"] = bool(
            getattr(sc, "control_positive_outcome", None)
            or getattr(sc, "control_negative_outcome", None)
        )
        ctx["declares_novel"] = bool(getattr(sc, "novel", False))

    if catalog_dir is not None:
        ctx.update(_catalog_counts(script, Path(catalog_dir)))

    return ctx


def _catalog_counts(script: Path, catalog_dir: Path) -> dict:
    """Best-effort run counts from the warm catalog.

    Failure is silent and returns zeroes *by design*: a missing or unreadable catalog is the
    normal case for a fresh project, and a card must not become unevaluable because of it.
    The cost is that a zero is ambiguous between "no runs" and "could not read", which is why
    CONTEXT_COLUMNS documents these as "0 when unavailable" rather than as counts.
    """
    db_path = Path(catalog_dir) / "bathos.db"
    if not db_path.exists():
        return {}
    stem = script.stem
    # An empty stem would become LIKE '%%', which matches every run in the catalog. Returning
    # zeroes is the honest answer for "no identifiable script", not a count of everything.
    if not stem:
        return {}
    try:
        con = duckdb.connect(str(db_path), read_only=True)
    except Exception:
        return {}
    try:
        row = con.execute(
            "SELECT COUNT(*) FROM runs WHERE command LIKE ?", [f"%{stem}%"]
        ).fetchone()
        run_count = int(row[0]) if row else 0
        row = con.execute(
            "SELECT COUNT(*) FROM runs WHERE campaign_id IS NOT NULL AND campaign_id != '' "
            "AND campaign_id IN (SELECT campaign_id FROM runs WHERE command LIKE ?)",
            [f"%{stem}%"],
        ).fetchone()
        campaign_run_count = int(row[0]) if row else 0
        return {"run_count": run_count, "campaign_run_count": campaign_run_count}
    except Exception:
        return {}
    finally:
        con.close()


def slugify(text: str) -> str:
    """Lowercase-hyphen slug, for card filenames."""
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
