"""The Review Coverage Gate's *wiring* at conclude — the seam, not the check.

`review_coverage_check()` is unit-tested in test_review_coverage.py, but the conclude-time
wiring and the enforcement flag had no test at all; the spec audit called this out and the
handoff carried it as deferred. It matters more now that the flag is enabled: failed attempt
b1 was exactly this gate firing on every campaign, because no sidecar authored before
build-order step 2 can carry a `[review]` block.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bathos.campaigns import add_run_to_campaign, conclude_campaign, create_campaign
from bathos.catalog import init_catalog, write_run
from bathos.claim import register_claim
from bathos.compact import compact
from bathos.schema import Run

_CLAIM = """
[claim]
headline = "h"
kill_condition = "k"

[[hypotheses]]
id = "H1"
label = "primary"

[claim.union_gate]
"""

_SIDECAR_NO_REVIEW = """
[experiment]
hypothesis = "h"

[outcomes.pass]
condition = "x < 5"
decision = "go"
reasoning = "r"

[outcomes.fail]
condition = "x >= 5"
decision = "stop"
reasoning = "r"
is_residual = true

[result_schema]
x = "float"
"""

_SIDECAR_WITH_REVIEW = _SIDECAR_NO_REVIEW.replace(
    '[experiment]\nhypothesis = "h"\n',
    '[experiment]\nhypothesis = "h"\n\n'
    '[[review.literature]]\nref = "10.1/x"\nclaim = "prior work"\n'
    'bears_on = "H1"\ndisposition = "supports"\n',
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("BTH_REVIEW_COVERAGE_ENFORCE", raising=False)
    for t in ("OUTCOME_FAILED", "CAMPAIGN_CONFOUNDED", "CITATION_CONTRADICTED", "ENFORCE"):
        monkeypatch.delenv(f"BTH_OBLIGATION_{t}", raising=False)


def _campaign(tmp_path: Path, sidecar_body: str, enforce_cfg: bool | None):
    """A confirmation campaign with a registered claim and one member run.

    `enforce_cfg` writes [claim] review_coverage_enforce into the workspace's .bth.toml;
    None omits the key entirely.
    """
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    # compact() is what creates the warm `campaigns` table, so it has to run before any
    # campaign exists -- init_catalog alone leaves the warm tier absent.
    compact(catalog_dir)

    cfg = f'[project]\nslug = "p"\nroot = "{tmp_path}"\n'
    if enforce_cfg is not None:
        cfg += f"\n[claim]\nreview_coverage_enforce = {str(enforce_cfg).lower()}\n"
    (tmp_path / ".bth.toml").write_text(cfg, encoding="utf-8")

    sidecar = tmp_path / "run_x.bth.toml"
    sidecar.write_text(sidecar_body)
    (tmp_path / "claim.bth.toml").write_text(_CLAIM)

    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    campaign = create_campaign(db, "c", "p", "confirmation")
    register_claim(tmp_path / "claim.bth.toml", campaign.id, db, tmp_path)
    db.commit()
    db.close()

    # Written after the campaign exists: a confirmation campaign refuses member runs whose
    # timestamp predates its creation.
    run = Run(
        project_slug="p",
        command="python s.py",
        argv=["python", "s.py"],
        git_hash="a",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        sidecar_path=str(sidecar),
        outcome="pass",
    )
    write_run(run, catalog_dir)
    compact(catalog_dir)

    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    add_run_to_campaign(db, campaign.id, run.id)
    db.commit()
    return db, campaign


def _verdict(db, campaign_id: str) -> str:
    return db.execute("SELECT outcome_label FROM campaigns WHERE id=?", [campaign_id]).fetchone()[0]


def test_uncovered_campaign_is_advisory_when_the_flag_is_absent(tmp_path, capsys):
    """The pre-enablement posture, pinned: the gate runs and reports, verdict untouched."""
    db, campaign = _campaign(tmp_path, _SIDECAR_NO_REVIEW, enforce_cfg=None)
    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)

    out = capsys.readouterr().out
    assert "Review coverage gate" in out
    assert "advisory until" in out
    assert _verdict(db, campaign.id) == "pass"


def test_uncovered_campaign_downgrades_when_config_enables_enforcement(tmp_path, capsys):
    """The seam under test: enforcement read from .bth.toml, not just the env var."""
    db, campaign = _campaign(tmp_path, _SIDECAR_NO_REVIEW, enforce_cfg=True)
    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)

    assert "verdict downgraded to 'confounded'" in capsys.readouterr().out
    assert _verdict(db, campaign.id) == "confounded"


def test_a_covered_campaign_is_not_downgraded(tmp_path):
    """Enforcement must bite only on genuinely uncovered slates — otherwise it is not a gate,
    it is a blanket downgrade (failed attempt b1)."""
    db, campaign = _campaign(tmp_path, _SIDECAR_WITH_REVIEW, enforce_cfg=True)
    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)

    assert _verdict(db, campaign.id) == "pass"


def test_env_var_disables_config_enforcement_at_the_seam(tmp_path, monkeypatch):
    """The escape hatch, exercised end to end rather than at resolve_flag: a run that cannot
    wait for [review] entries can turn the gate off for one invocation."""
    db, campaign = _campaign(tmp_path, _SIDECAR_NO_REVIEW, enforce_cfg=True)
    monkeypatch.setenv("BTH_REVIEW_COVERAGE_ENFORCE", "0")
    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)

    assert _verdict(db, campaign.id) == "pass"


def test_exploration_campaigns_are_untouched_by_the_gate(tmp_path):
    """§7 scopes the gate to confirmation/sequential only."""
    catalog_dir = tmp_path / "catalog"
    catalog_dir.mkdir()
    init_catalog(catalog_dir)
    compact(catalog_dir)
    (tmp_path / ".bth.toml").write_text(
        f'[project]\nslug = "p"\nroot = "{tmp_path}"\n\n[claim]\nreview_coverage_enforce = true\n',
        encoding="utf-8",
    )
    (tmp_path / "claim.bth.toml").write_text(_CLAIM)

    db = duckdb.connect(str(catalog_dir / "bathos.db"))
    campaign = create_campaign(db, "c", "p", "exploration")
    register_claim(tmp_path / "claim.bth.toml", campaign.id, db, tmp_path)
    db.commit()

    conclude_campaign(db, campaign.id, "pass", "note", workspace_root=tmp_path)
    assert _verdict(db, campaign.id) == "pass"
