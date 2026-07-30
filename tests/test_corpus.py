"""Tests for the rule-card corpus (bathos.corpus + `bth ref`)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from bathos.cli import app
from bathos.corpus import (
    CONTEXT_COLUMNS,
    CorpusError,
    applicable_cards,
    build_context,
    evaluate_applies_when,
    get_card,
    get_corpus_root,
    load_corpus,
    parse_card,
    search_cards,
)

runner = CliRunner()


def write_card(root: Path, name: str, frontmatter: str, body: str = "Body text.") -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(f"+++\n{frontmatter}\n+++\n\n{body}\n", encoding="utf-8")
    return p


# ── parsing ──────────────────────────────────────────────────────────────────


def test_parse_card_reads_frontmatter_and_body(tmp_path):
    p = write_card(
        tmp_path,
        "x/TEST-001_thing.md",
        'id = "TEST-001"\ntitle = "A thing"\nseverity = "warning"\n'
        'applies_when = "has_sidecar = true"\nsource_check = "check_thing"\n'
        'tags = ["a"]\nsee_also = ["TEST-002"]',
        "The body.",
    )
    card = parse_card(p)
    assert card.id == "TEST-001"
    assert card.title == "A thing"
    assert card.severity == "warning"
    assert card.applies_when == "has_sidecar = true"
    assert card.source_check == "check_thing"
    assert card.tags == ("a",)
    assert card.see_also == ("TEST-002",)
    assert card.body.strip() == "The body."
    assert card.domain == "x"


def test_parse_card_defaults_severity_to_info(tmp_path):
    p = write_card(tmp_path, "d/T-1_x.md", 'id = "T-1"\ntitle = "t"')
    assert parse_card(p).severity == "info"
    assert parse_card(p).applies_when is None


@pytest.mark.parametrize(
    "frontmatter,body,fragment",
    [
        ('id = "T-1"', "b", "missing required key 'title'"),
        ('title = "t"', "b", "missing required key 'id'"),
        ('id = "T-1"\ntitle = ', "b", "malformed TOML"),
    ],
)
def test_parse_card_rejects_bad_frontmatter(tmp_path, frontmatter, body, fragment):
    p = write_card(tmp_path, "d/bad.md", frontmatter, body)
    with pytest.raises(ValueError, match=re.escape(fragment)):
        parse_card(p)


def test_parse_card_requires_delimiters(tmp_path):
    p = tmp_path / "nofm.md"
    p.write_text("no frontmatter here\n", encoding="utf-8")
    with pytest.raises(ValueError, match="missing opening"):
        parse_card(p)

    p2 = tmp_path / "unterminated.md"
    p2.write_text('+++\nid = "X"\ntitle = "t"\n', encoding="utf-8")
    with pytest.raises(ValueError, match="unterminated frontmatter"):
        parse_card(p2)


# ── loading: one bad card must not break the corpus ──────────────────────────


def test_load_corpus_collects_errors_instead_of_raising(tmp_path):
    write_card(tmp_path, "d/GOOD-1_a.md", 'id = "GOOD-1"\ntitle = "good"')
    (tmp_path / "d" / "BAD.md").write_text("not a card\n", encoding="utf-8")

    load = load_corpus(tmp_path)
    assert [c.id for c in load.cards] == ["GOOD-1"]
    assert len(load.errors) == 1
    assert "missing opening" in load.errors[0][1]


def test_load_corpus_flags_duplicate_ids(tmp_path):
    write_card(tmp_path, "d/A.md", 'id = "DUP-1"\ntitle = "first"')
    write_card(tmp_path, "d/B.md", 'id = "DUP-1"\ntitle = "second"')
    load = load_corpus(tmp_path)
    assert len(load.cards) == 1
    assert any("duplicate card id" in e for _, e in load.errors)


def test_get_card_is_case_insensitive_and_raises_on_miss(tmp_path):
    write_card(tmp_path, "d/A.md", 'id = "CASE-1"\ntitle = "t"')
    assert get_card("case-1", tmp_path).id == "CASE-1"
    with pytest.raises(CorpusError, match="No card with id"):
        get_card("NOPE-9", tmp_path)


def test_search_matches_title_tag_and_body(tmp_path):
    write_card(tmp_path, "d/A.md", 'id = "S-1"\ntitle = "Alpha"\ntags = ["zebra"]', "corpus body")
    assert [c.id for c in search_cards("alpha", tmp_path)] == ["S-1"]
    assert [c.id for c in search_cards("zebra", tmp_path)] == ["S-1"]
    assert [c.id for c in search_cards("corpus", tmp_path)] == ["S-1"]
    assert search_cards("nothingmatches", tmp_path) == []
    assert search_cards("   ", tmp_path) == []


# ── applies_when evaluation ──────────────────────────────────────────────────


def test_evaluate_applies_when_over_context_row():
    ctx = {"n_outcome_branches": 4, "has_sidecar": True, "stage_name": "validation"}
    assert evaluate_applies_when("n_outcome_branches > 2", ctx) is True
    assert evaluate_applies_when("n_outcome_branches > 9", ctx) is False
    assert evaluate_applies_when("has_sidecar = true", ctx) is True
    assert evaluate_applies_when("stage_name IN ('validation','production')", ctx) is True


def test_card_without_applies_when_never_fires(tmp_path):
    write_card(tmp_path, "d/REF-1.md", 'id = "REF-1"\ntitle = "reference only"')
    fired, unevaluable = applicable_cards({"has_sidecar": True}, tmp_path)
    assert fired == []
    assert unevaluable == []


def test_unknown_column_makes_a_card_unevaluable_not_fatal(tmp_path):
    """One card referencing a column that does not exist must not break the others."""
    write_card(
        tmp_path, "d/OK-1.md", 'id = "OK-1"\ntitle = "ok"\napplies_when = "has_sidecar = true"'
    )
    write_card(
        tmp_path, "d/BAD-1.md", 'id = "BAD-1"\ntitle = "bad"\napplies_when = "no_such_column > 1"'
    )

    fired, unevaluable = applicable_cards({"has_sidecar": True}, tmp_path)
    assert [c.id for c in fired] == ["OK-1"]
    assert [c.id for c, _ in unevaluable] == ["BAD-1"]


# ── context building ─────────────────────────────────────────────────────────


def test_build_context_always_supplies_every_documented_column(tmp_path):
    """A card must never be unevaluable merely because a fact was unavailable."""
    ctx = build_context(tmp_path / "scripts" / "experiments" / "nothing_here.py")
    assert set(ctx) == set(CONTEXT_COLUMNS)


def test_build_context_reads_a_real_sidecar(tmp_path):
    exp = tmp_path / "scripts" / "experiments"
    exp.mkdir(parents=True)
    (exp / "run_thing.py").write_text("print('x')\n", encoding="utf-8")
    (exp / "run_thing.bth.toml").write_text(
        """
[experiment]
hypothesis = "a thing happens"
stage_name = "validation"

[outcomes.pass]
condition = "x < 5"
decision = "proceed"
adversarial_check = "y > 0"
source = "conventional"

[outcomes.fail]
condition = "x >= 5"
decision = "stop"

[result_schema]
x = "float"
""",
        encoding="utf-8",
    )

    ctx = build_context(exp / "run_thing.py")
    assert ctx["has_sidecar"] is True
    assert ctx["script_stem"] == "run_thing"
    assert ctx["script_dir"] == "experiments"
    assert ctx["stage_name"] == "validation"
    assert ctx["n_outcome_branches"] == 2
    assert ctx["n_adversarial_checks"] == 1
    assert ctx["n_threshold_sources"] == 1
    assert ctx["declares_novel"] is False
    # No catalog passed -> documented "0 when unavailable"
    assert ctx["run_count"] == 0


def test_empty_stem_does_not_match_every_run(tmp_path):
    """A blank stem would become LIKE '%%' and count the whole catalog."""
    from bathos.corpus import _catalog_counts

    (tmp_path / "bathos.db").write_text("", encoding="utf-8")
    assert _catalog_counts(Path(""), tmp_path) == {}


# ── the shipped corpus itself ────────────────────────────────────────────────


def test_shipped_corpus_loads_without_errors():
    load = load_corpus()
    assert load.errors == [], f"shipped corpus has malformed cards: {load.errors}"
    assert len(load.cards) >= 16


def test_every_shipped_card_cites_a_real_lint_check():
    """Maintenance trap: a card's `source_check` must name a function that exists.

    The corpus's whole claim is that v1 contains no invented methodology — every card is a
    rule the codebase already enforces. That claim decays silently if a check is renamed or
    removed, so it is asserted here rather than trusted.
    """
    linter_src = (Path(__file__).parent.parent / "src" / "bathos" / "linter.py").read_text()
    defined = set(re.findall(r"^def (check_\w+)", linter_src, re.M))

    load = load_corpus()
    missing = [(c.id, c.source_check) for c in load.cards if c.source_check not in defined]
    assert not missing, f"cards cite non-existent lint checks: {missing}"


def test_every_lint_check_has_a_card():
    """The converse trap: a new lint check should arrive with its card."""
    linter_src = (Path(__file__).parent.parent / "src" / "bathos" / "linter.py").read_text()
    defined = set(re.findall(r"^def (check_\w+)", linter_src, re.M))
    covered = {c.source_check for c in load_corpus().cards}
    assert not (defined - covered), f"lint checks with no rule card: {sorted(defined - covered)}"


def test_shipped_applies_when_fragments_all_evaluate():
    """Every shipped fragment must run against a full context row — no typos, no unknown columns."""
    ctx = build_context(Path("scripts/experiments/does_not_exist.py"))
    load = load_corpus()
    for card in load.cards:
        if card.applies_when:
            evaluate_applies_when(card.applies_when, ctx)  # must not raise


def test_shipped_see_also_ids_resolve():
    load = load_corpus()
    known = {c.id for c in load.cards}
    dangling = {c.id: [s for s in c.see_also if s not in known] for c in load.cards if c.see_also}
    dangling = {k: v for k, v in dangling.items() if v}
    assert not dangling, f"see_also points at unknown cards: {dangling}"


def test_corpus_root_is_locatable():
    assert get_corpus_root().is_dir()


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_ref_list_shows_every_card():
    res = runner.invoke(app, ["ref", "list"])
    assert res.exit_code == 0
    assert "STAT-001" in res.stdout
    assert "cards" in res.stdout


def test_cli_ref_show_renders_body_and_provenance():
    res = runner.invoke(app, ["ref", "show", "STAT-001"])
    assert res.exit_code == 0
    assert "check_multiple_comparisons" in res.stdout
    assert "family-wise" in res.stdout


def test_cli_ref_show_unknown_id_exits_nonzero():
    res = runner.invoke(app, ["ref", "show", "NOPE-999"])
    assert res.exit_code == 1


def test_cli_ref_search_hit_and_miss():
    assert runner.invoke(app, ["ref", "search", "threshold"]).exit_code == 0
    assert runner.invoke(app, ["ref", "search", "zzzznotpresent"]).exit_code == 1


def test_cli_ref_applicable_fires_on_a_validation_sidecar(tmp_path, monkeypatch):
    exp = tmp_path / "scripts" / "experiments"
    exp.mkdir(parents=True)
    (exp / "thing.py").write_text("print(1)\n", encoding="utf-8")
    (exp / "thing.bth.toml").write_text(
        """
[experiment]
hypothesis = "h"
stage_name = "validation"

[outcomes.pass]
condition = "x < 5"
decision = "go"

[result_schema]
x = "float"
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))

    res = runner.invoke(app, ["ref", "applicable", str(exp / "thing.py")])
    assert res.exit_code == 0
    # validation stage, no [reproduction], not novel -> DSGN-002 must fire
    assert "DSGN-002" in res.stdout
    # no adversarial_check on any branch -> DSGN-003 must fire
    assert "DSGN-003" in res.stdout


def test_cli_ref_applicable_reports_no_match_plainly(tmp_path, monkeypatch):
    monkeypatch.setenv("BTH_CATALOG_DIR", str(tmp_path / "catalog"))
    res = runner.invoke(app, ["ref", "applicable", str(tmp_path / "absent.py")])
    assert res.exit_code == 0
    assert "No cards fired" in res.stdout


def test_invalid_severity_is_rejected_and_skipped(tmp_path):
    """The spec says severity reuses IssueSeverity; an unknown value must not parse silently."""
    write_card(tmp_path, "d/OK.md", 'id = "S-OK"\ntitle = "t"\nseverity = "warning"')
    write_card(tmp_path, "d/BAD.md", 'id = "S-BAD"\ntitle = "t"\nseverity = "critical"')

    load = load_corpus(tmp_path)
    assert [c.id for c in load.cards] == ["S-OK"]
    assert any("severity 'critical' is not one of" in e for _, e in load.errors)


def test_every_shipped_card_severity_is_a_real_issue_severity():
    from bathos.linter import IssueSeverity

    valid = {s.value for s in IssueSeverity}
    bad = [(c.id, c.severity) for c in load_corpus().cards if c.severity not in valid]
    assert not bad, f"cards with severity outside IssueSeverity: {bad}"


# ── regression: nested sidecar blocks must actually be read ──────────────────


def _sidecar_with(tmp_path: Path, extra: str) -> Path:
    exp = tmp_path / "scripts" / "experiments"
    exp.mkdir(parents=True, exist_ok=True)
    (exp / "s.py").write_text("print(1)\n", encoding="utf-8")
    (exp / "s.bth.toml").write_text(
        '[experiment]\nhypothesis = "h"\nstage_name = "validation"\n\n'
        '[outcomes.pass]\ncondition = "x < 5"\ndecision = "go"\n\n'
        '[outcomes.fail]\ncondition = "x >= 5"\ndecision = "stop"\n\n'
        f'{extra}\n\n[result_schema]\nx = "float"\n',
        encoding="utf-8",
    )
    return exp / "s.py"


def test_has_reproduction_reads_the_nested_block():
    """Regression: read sc.reproduction.*, not a flat sc.reproduces_paper that does not exist.

    The original code used getattr(sc, "reproduces_paper", "") — an attribute Sidecar has
    never had — so getattr silently returned the default and this flag was False for every
    script, making the ERROR-severity DSGN-002 fire on everything.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        script = _sidecar_with(Path(d), '[reproduction]\nreproduces_paper = "10.1234/x"')
        assert build_context(script)["has_reproduction"] is True

    with tempfile.TemporaryDirectory() as d:
        script = _sidecar_with(Path(d), "")
        assert build_context(script)["has_reproduction"] is False


def test_has_controls_requires_a_positive_control():
    """A negative control alone must NOT satisfy it — the lint only accepts positive_outcome."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        script = _sidecar_with(Path(d), '[controls]\npositive_outcome = ["pass"]')
        assert build_context(script)["has_controls"] is True

    with tempfile.TemporaryDirectory() as d:
        script = _sidecar_with(Path(d), '[controls]\nnegative_outcome = ["fail"]')
        assert build_context(script)["has_controls"] is False


def test_null_capable_and_pass_branch_flags():
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        ctx = build_context(_sidecar_with(Path(d), ""))
        assert ctx["has_null_capable_outcome"] is True  # a `fail` branch exists
        assert ctx["has_pass_branch"] is True


def test_dsgn002_does_not_fire_when_reproduction_is_declared():
    """End-to-end guard on the bug: the card must go quiet once [reproduction] is present."""
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        script = _sidecar_with(Path(d), '[reproduction]\nreproduces_paper = "10.1234/x"')
        fired, _ = applicable_cards(build_context(script))
        assert "DSGN-002" not in [c.id for c in fired]

    with tempfile.TemporaryDirectory() as d:
        script = _sidecar_with(Path(d), "")
        fired, _ = applicable_cards(build_context(script))
        assert "DSGN-002" in [c.id for c in fired]


def test_unreadable_card_is_skipped_not_fatal(tmp_path):
    """OSError, not just ValueError — a directory named *.md must not break the whole load."""
    write_card(tmp_path, "d/OK.md", 'id = "O-1"\ntitle = "t"')
    (tmp_path / "d" / "trap.md").mkdir()
    load = load_corpus(tmp_path)
    assert [c.id for c in load.cards] == ["O-1"]
    assert len(load.errors) == 1


def test_like_wildcards_in_stem_are_escaped():
    """`_` is a LIKE wildcard; snake_case stems must not match `fit-model`/`fitXmodel`."""
    import tempfile

    import duckdb

    from bathos.corpus import _catalog_counts

    with tempfile.TemporaryDirectory() as d:
        db = Path(d) / "bathos.db"
        con = duckdb.connect(str(db))
        con.execute("CREATE TABLE runs (command VARCHAR, campaign_id VARCHAR)")
        con.execute("INSERT INTO runs VALUES ('uv run fit_model.py', 'c1')")
        con.execute("INSERT INTO runs VALUES ('uv run fit-model.py', 'c2')")
        con.execute("INSERT INTO runs VALUES ('uv run fitXmodel.py', 'c3')")
        con.close()

        counts = _catalog_counts(Path("fit_model.py"), Path(d))
        assert counts["run_count"] == 1, f"wildcard leak: {counts}"


def test_mcp_reference_tools_are_actually_exposed_on_the_server():
    """Registering a @cisternal.tool AFTER cisternal.wire() leaves it invisible to the server.

    The pre-existing wiring test compares app.list_tools() against _WIRED.mcp_tools — both
    sides exclude a late-registered tool, so it passes either way. This asserts against the
    literal expected names instead.
    """
    import asyncio

    import bathos.mcp as m

    exposed = {t.name for t in asyncio.run(m.app.list_tools())}
    for name in ("reference_list", "reference_get", "reference_search", "reference_applicable"):
        assert name in exposed, f"{name} is registered but not wired onto the server"
