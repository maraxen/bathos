"""Every scaffold must emit a document the corresponding reader can parse.

Regression guard for the class of bug where a scaffold template is edited by hand,
becomes syntactically invalid TOML, and ships undetected because the only tests
assert substring containment rather than parsing.

The concrete instance this was written for: ``scaffold_claim`` emitted a
``[claim.discriminability]`` table header immediately followed by a
``[[claim.discriminability]]`` array-of-tables. TOML forbids redefining a table as
an array of tables, so ``bth claim scaffold`` wrote a file that ``bth claim validate``
could never read -- on both the CLI and the MCP surface.

Three tiers, deliberately ordered cheapest-first so a failure names the layer:
  1. the bytes are valid TOML                (catches the template-syntax class)
  2. the kind's own reader accepts them      (catches schema/nesting drift)
  3. validation reports only placeholder gaps (catches a scaffold that is parseable
     but structurally incomplete in ways unrelated to the REQUIRED:/TODO: markers)
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import duckdb
import pytest

# Substrings marking a field the author is expected to fill in. A validation error
# mentioning one of these is the scaffold working as intended, not a defect.
PLACEHOLDER_MARKERS = ("REQUIRED:", "TODO:", "??", "EDIT:")


@pytest.fixture
def campaign_db(tmp_path):
    """A DuckDB connection with the columns scaffold_claim reads."""
    db = duckdb.connect(str(tmp_path / "test.db"))
    db.execute("""
        CREATE TABLE campaigns (
            id TEXT PRIMARY KEY,
            project_slug TEXT NOT NULL,
            name TEXT NOT NULL,
            mode TEXT NOT NULL,
            question TEXT,
            hypothesis TEXT,
            status TEXT NOT NULL,
            started_at TEXT NOT NULL,
            concluded_at TEXT,
            conclusion TEXT,
            outcome_label TEXT,
            parent_campaign_id TEXT,
            stopping_threshold REAL,
            claim_path TEXT,
            claim_sha256 TEXT,
            claim_mode TEXT
        )
    """)
    db.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, hypothesis, status, started_at) "
        "VALUES ('camp_test01', 'proj', 'testcamp', 'confirmation', "
        "'H: x beats y', 'open', '2026-08-28T00:00:00Z')"
    )
    db.commit()
    yield db
    db.close()


# All four helpers share one signature so the parametrization can call them
# uniformly; only the claim scaffold actually needs the database.
def _scaffold_claim(tmp_path, campaign_db) -> Path:
    from bathos.claim import scaffold_claim

    return scaffold_claim("camp_test01", campaign_db, tmp_path)


def _scaffold_experiment(tmp_path, _campaign_db) -> Path:
    from bathos.new_experiment import scaffold_experiment

    return scaffold_experiment("measure_thing", tmp_path).sidecar


def _scaffold_postmortem(tmp_path, _campaign_db) -> Path:
    from bathos.postmortem import scaffold_postmortem_template

    return scaffold_postmortem_template("scripts/experiments/x.py", "run_abc123", tmp_path)


def _scaffold_attestation(tmp_path, _campaign_db) -> Path:
    from bathos.attestation import scaffold_attestation

    return scaffold_attestation("oracle_match", tmp_path, label="probe")


# (id, scaffold callable, reader callable or None)
SCAFFOLDS = [
    pytest.param("claim", _scaffold_claim, id="claim"),
    pytest.param("experiment_sidecar", _scaffold_experiment, id="experiment_sidecar"),
    pytest.param("postmortem", _scaffold_postmortem, id="postmortem"),
    pytest.param("attestation", _scaffold_attestation, id="attestation"),
]


@pytest.mark.parametrize("kind,scaffold", SCAFFOLDS)
def test_scaffold_output_is_valid_toml(kind, scaffold, tmp_path, campaign_db):
    """Tier 1: the scaffold writes syntactically valid TOML.

    This is the assertion that would have caught the claim-scaffold regression on
    the day it landed.
    """
    path = scaffold(tmp_path, campaign_db)
    raw = Path(path).read_bytes()
    assert raw, f"{kind} scaffold wrote an empty file"

    try:
        tomllib.loads(raw.decode("utf-8"))
    except tomllib.TOMLDecodeError as e:
        pytest.fail(
            f"{kind} scaffold emitted TOML that cannot be parsed: {e}\n"
            f"--- written to {path} ---\n{raw.decode('utf-8')}"
        )


@pytest.mark.parametrize("kind,scaffold", SCAFFOLDS)
def test_scaffold_output_round_trips_through_its_reader(kind, scaffold, tmp_path, campaign_db):
    """Tier 2: the kind's own parser accepts what its scaffold wrote."""
    path = Path(scaffold(tmp_path, campaign_db))

    if kind == "claim":
        from bathos.claim import parse_claim

        parse_claim(path)
    elif kind == "experiment_sidecar":
        from bathos.sidecar import parse_sidecar

        parse_sidecar(path)
    elif kind == "postmortem":
        from bathos.postmortem import parse_postmortem

        parse_postmortem(path)
    elif kind == "attestation":
        from bathos.attestation import parse_attestation

        parse_attestation(path)
    else:  # pragma: no cover - parametrization guard
        pytest.fail(f"no reader wired for kind {kind!r}")


def test_claim_scaffold_validates_modulo_placeholders(tmp_path, campaign_db):
    """Tier 3: a freshly scaffolded claim's only complaints are unfilled placeholders.

    Scoped to claims because it is the kind whose scaffold->validate round trip is
    the documented agent workflow (`bth claim scaffold` then `bth claim validate`).
    """
    from bathos.claim import parse_claim, validate_claim

    path = _scaffold_claim(tmp_path, campaign_db)
    result = validate_claim(parse_claim(path))

    unexpected = [
        e.message
        for e in result.errors
        if not any(marker in e.message for marker in PLACEHOLDER_MARKERS)
    ]
    assert not unexpected, (
        "freshly scaffolded claim reports errors unrelated to unfilled placeholders:\n  "
        + "\n  ".join(unexpected)
    )
