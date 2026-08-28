"""attest_parity must refuse rather than guess when the bind target is ambiguous.

Regression: the write path was ``str.replace(...)`` with no ``count``, so a claim
carrying two unbound ``parity_run_id = ""`` fields had BOTH bound to the same run --
silently attributing a literature-parity attestation to a confound that run was never
executed against. The in-memory loop above it ``break``s after the first confound, so
intent and effect disagreed; the in-memory mutation was dead code that never reached
disk, which is why no existing test noticed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import duckdb
import pytest

from bathos.claim import attest_parity, parse_claim, register_claim

TWO_PARITY_CLAIM = """[claim]
headline = "Claim with two parity confounds"
kill_condition = "Outcome != expected"
kill_condition_satisfiable_by_null = false

[[hypotheses]]
id = "H_primary"
label = "Primary hypothesis"

[[hypotheses]]
id = "H_null"
label = "Null hypothesis"

[[assumptions]]
id = "A1"
label = "Test assumption"

[[confounds]]
id = "C_parity_a"
label = "First literature parity confound"
[confounds.reference_parity]
reference_paper = "Example 2026"
reference_metric = "metric_a"
reference_value = 1.0
equivalence_bound = 0.05
parity_run_id = ""

[[confounds]]
id = "C_parity_b"
label = "Second literature parity confound"
[confounds.reference_parity]
reference_paper = "Other 2026"
reference_metric = "metric_b"
reference_value = 2.0
equivalence_bound = 0.05
parity_run_id = ""

[[claim.discriminability]]
hypothesis_a = "H_primary"
hypothesis_b = "H_null"
planned_run_label = "main"
predicted_outcome = "discriminates"

[claim.union_gate]
[[claim.union_gate.clauses]]
id = "C_main"
description = "Main clause"
hypothesis_ids = ["H_primary", "H_null"]
"""

CAMPAIGN_ID = "camp_two_parity"
PARITY_RUN_ID = "run_parity_two"


@pytest.fixture
def parity_db(tmp_path):
    """DuckDB with the campaigns/runs columns attest_parity reads."""
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
    db.execute("""
        CREATE TABLE runs (
            id TEXT PRIMARY KEY,
            campaign_id TEXT,
            claim_discriminates TEXT,
            outcome TEXT,
            metadata TEXT,
            parity_run_type TEXT
        )
    """)
    db.commit()
    yield db
    db.close()


@pytest.fixture
def two_parity_claim(parity_db, tmp_path):
    """A registered claim carrying two unbound reference_parity confounds."""
    claim_path = tmp_path / "two_parity.claim.toml"
    claim_path.write_text(TWO_PARITY_CLAIM)

    parity_db.execute(
        "INSERT INTO campaigns (id, project_slug, name, mode, status, started_at, claim_path) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [
            CAMPAIGN_ID,
            "test",
            CAMPAIGN_ID,
            "confirmation",
            "open",
            datetime.now(UTC).isoformat(),
            claim_path.name,
        ],
    )
    parity_db.execute(
        "INSERT INTO runs (id, campaign_id, outcome, metadata, parity_run_type) "
        "VALUES (?, ?, ?, ?, ?)",
        [
            PARITY_RUN_ID,
            CAMPAIGN_ID,
            "pass",
            json.dumps({"metric_a": 1.0, "parity_run_type": "literature_parity"}),
            "literature_parity",
        ],
    )
    register_claim(Path(claim_path.name), CAMPAIGN_ID, parity_db, tmp_path, force=False)
    parity_db.commit()
    return claim_path


def _reregister(parity_db, claim_path, tmp_path):
    register_claim(Path(claim_path.name), CAMPAIGN_ID, parity_db, tmp_path, force=True)
    parity_db.commit()


def test_refuses_when_two_parity_run_ids_are_unbound(parity_db, tmp_path, two_parity_claim):
    before = two_parity_claim.read_text()

    with pytest.raises(ValueError, match="cannot determine which"):
        attest_parity(
            campaign_id=CAMPAIGN_ID,
            parity_run_id=PARITY_RUN_ID,
            db=parity_db,
            workspace_root=tmp_path,
        )

    assert two_parity_claim.read_text() == before, (
        "a refused attestation must leave the claim file byte-identical"
    )


def test_binds_only_the_single_unbound_confound(parity_db, tmp_path, two_parity_claim):
    """Once the ambiguity is resolved, exactly one field changes."""
    two_parity_claim.write_text(
        two_parity_claim.read_text().replace(
            'parity_run_id = ""', 'parity_run_id = "run_other_999"', 1
        )
    )
    _reregister(parity_db, two_parity_claim, tmp_path)

    attest_parity(
        campaign_id=CAMPAIGN_ID,
        parity_run_id=PARITY_RUN_ID,
        db=parity_db,
        workspace_root=tmp_path,
    )

    bound = [
        c["reference_parity"]["parity_run_id"] for c in parse_claim(two_parity_claim).confounds
    ]
    assert bound == ["run_other_999", PARITY_RUN_ID]


def test_refuses_non_canonical_spacing(parity_db, tmp_path, two_parity_claim):
    """The text edit keys on an exact literal; a spacing variant must fail loudly."""
    two_parity_claim.write_text(
        two_parity_claim.read_text().replace('parity_run_id = ""', 'parity_run_id=""')
    )
    _reregister(parity_db, two_parity_claim, tmp_path)

    with pytest.raises(ValueError, match="already set or TOML format mismatch"):
        attest_parity(
            campaign_id=CAMPAIGN_ID,
            parity_run_id=PARITY_RUN_ID,
            db=parity_db,
            workspace_root=tmp_path,
        )
