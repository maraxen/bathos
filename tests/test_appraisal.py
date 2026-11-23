"""Tests for structured claim/citation appraisals and derived use_tier (spec 261015, §3.3.2 & S3)."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from bathos import mcp_auth
from bathos.appraisal import (
    AGAINST_CATEGORIES,
    FOR_CATEGORIES,
    AppraisalNotFoundError,
    AppraisalValidationError,
    compute_use_tier,
    create_appraisal,
    get_appraisal,
    list_appraisals,
    summarize_other_labels,
    validate_appraisal_payload,
    validate_uses_as,
)
from bathos.cli_cyclopts import app
from bathos.errors import BathosErrorCode
from bathos.index import connect_read
from bathos.mcp import mcp_appraise_tool
from bathos.runlog.fold_observations import fold_appraisal
from bathos.runlog.writer import reset_writers_for_test
from tests._cyclopts_runner import CyclopticRunner

runner = CyclopticRunner()


@pytest.fixture
def appr_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / "home"
    (home / ".bth").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    reset_writers_for_test()

    cat_dir = tmp_path / "catalog"
    cat_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(cat_dir))
    monkeypatch.setenv("BTH_LOG_MODE", "1")

    proj_dir = tmp_path / "project"
    (proj_dir / ".bth").mkdir(parents=True, exist_ok=True)
    (proj_dir / ".bth.toml").write_text(
        f'[project]\nslug = "appr_test"\nroot = "{proj_dir}"\n'
        'id = "aaaaaaaa-1111-2222-3333-444444444444"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("BTH_PROJECT_SLUG", "appr_test")
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(proj_dir))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-appraiser-1")
    for var in ("BTH_AGENT_SESSION", "BTH_AGENT_ID", "CLAUDE_AGENT_ID", "SUBAGENT_ID", "BTH_APPRAISER_KIND"):
        monkeypatch.delenv(var, raising=False)

    token_file = tmp_path / "mcp_token"
    monkeypatch.setenv("BTH_MCP_TOKEN_PATH", str(token_file))
    monkeypatch.setattr(mcp_auth, "_stdio_transport", False)
    token = mcp_auth.get_or_create_token()

    yield {
        "tmp": tmp_path,
        "cat": cat_dir,
        "proj": proj_dir,
        "token": token,
    }
    reset_writers_for_test()


def _valid_payload(**overrides) -> dict:
    base = {
        "ref": "doi:10.1101/2024.01.02.123456",
        "locator": "Fig 3B; Results p4",
        "appraiser": "agent:claude-opus-5-5",
        "read_depth": "methods",
        "blind_to": [],
        "scope_claimed": "PDZ3 allostery generally across all domains",
        "scope_supported": "PDZ3 (1BE9), one ligand, 3x100 ns, one force field",
        "interpretation": "Heatmap lacks colour-bar zero; apo cell is pale rather than zero.",
        "for": [
            {
                "id": "f1",
                "category": "multi-seed-uncertainty",
                "statement": "Three independent replicas per state, with bootstrap CIs shown in Fig S4.",
                "basis": "inspected",
                "evidence": "SI p.6, Fig S4",
            }
        ],
        "against": [
            {
                "id": "a1",
                "category": "single-source",
                "statement": "No independent replication found; the only other report is from the same group.",
                "basis": "inspected",
                "severity": "minor",
                "evidence": "search: PDZ3 H71 holo coupling, 0 independent hits",
                "addressed_by": [],
            }
        ],
    }
    base.update(overrides)
    return base


def _write_toml(path: Path, text: str) -> Path:
    path.write_text(text, encoding="utf-8")
    return path


# ── Positive creation, folding, CLI, MCP ─────────────────────────────────────


def test_create_appraisal_from_toml_and_query_index(appr_env):
    toml_path = _write_toml(
        appr_env["proj"] / "appr.toml",
        """
appraiser = "agent:claude-opus-5-5"
read_depth = "methods"
blind_to = []
scope_claimed = "PDZ3 allostery generally"
scope_supported = "PDZ3 (1BE9), one ligand, 3x100 ns, one force field"
interpretation = "Read 'exclusively' off a heatmap with no colour-bar values."

[[for]]
id = "f1"
category = "multi-seed-uncertainty"
statement = "Three independent replicas per state, with bootstrap CIs shown in Fig S4."
basis = "inspected"
evidence = "SI p.6, Fig S4"

[[against]]
id = "a1"
category = "single-source"
statement = "No independent replication found; the only other report is from the same group."
basis = "inspected"
severity = "minor"
evidence = "search 0 hits"
addressed_by = []
""",
    )
    rec = create_appraisal(
        ref="doi:10.1101/2024.01.02.123456",
        locator="Fig 3B",
        file=toml_path,
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    assert rec.id.startswith("appr_")
    assert rec.appraiser_session == "sess-appraiser-1"
    assert rec.session_attested is False
    assert rec.appraiser_kind == "session"
    assert len(rec.payload_sha256) == 64

    fetched = get_appraisal(rec.id, catalog_dir=appr_env["cat"])
    assert fetched is not None
    assert fetched.id == rec.id
    assert fetched.payload_sha256 == rec.payload_sha256
    fetched_prefix = get_appraisal(rec.id[:9], catalog_dir=appr_env["cat"])
    assert fetched_prefix is not None
    assert fetched_prefix.id == rec.id

    con = connect_read(appr_env["cat"])
    try:
        rows = con.execute("SELECT id, ref, read_depth, payload_sha256 FROM appraisals").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == rec.id
        assert rows[0][2] == "methods"
    finally:
        con.close()


def test_appraisal_legacy_mode_parquet_roundtrip(appr_env, monkeypatch):
    monkeypatch.setenv("BTH_LOG_MODE", "0")
    rec = create_appraisal(
        payload=_valid_payload(),
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    frag = appr_env["cat"] / "appraisals" / "appr_test" / f"{rec.id}.parquet"
    assert frag.is_file()
    listed = list_appraisals(ref="doi:10.1101/2024.01.02.123456", catalog_dir=appr_env["cat"])
    assert [r.id for r in listed] == [rec.id]


def test_cli_appraise_and_mcp_appraise_roundtrip(appr_env):
    toml_path = _write_toml(
        appr_env["proj"] / "cli_appr.toml",
        """
appraiser = "agent:claude-opus-5-5"
read_depth = "methods"
scope_claimed = "Broad generalization"
scope_supported = "Single protein 1BE9 under AMBER99SB"

[[for]]
id = "f1"
category = "controls-present"
statement = "Positive and negative ligand controls included in replicate panels."
basis = "inspected"

[[against]]
id = "a1"
category = "other"
other_label = "force-field-bias"
statement = "Only one force field was tested without cross-validation against CHARMM."
basis = "inspected"
severity = "minor"
""",
    )
    res = runner.invoke(
        app,
        [
            "appraise",
            "--ref",
            "doi:10.1101/cli",
            "--locator",
            "Table 1",
            "--file",
            str(toml_path),
            "--json",
            "--project-root",
            str(appr_env["proj"]),
            "--catalog-dir",
            str(appr_env["cat"]),
        ],
    )
    assert res.exit_code == 0, res.output
    data = json.loads(res.output)
    assert data["ok"] is True
    appr_id = data["appraisal"]["id"]

    # CLI --show and --other-labels
    show_res = runner.invoke(
        app,
        ["appraise", "--show", appr_id, "--json", "--catalog-dir", str(appr_env["cat"])],
    )
    assert show_res.exit_code == 0
    assert json.loads(show_res.output)["appraisal"]["id"] == appr_id

    missing_res = runner.invoke(
        app,
        ["appraise", "--show", "appr_missing", "--catalog-dir", str(appr_env["cat"])],
    )
    assert missing_res.exit_code != 0
    assert "AppraisalNotFoundError" in missing_res.output or "not found" in missing_res.output.lower()
    with pytest.raises(AppraisalNotFoundError):
        from bathos.mcp import appraise_tool

        appraise_tool(show="appr_missing", catalog_dir=str(appr_env["cat"]))

    other_res = runner.invoke(
        app,
        ["appraise", "--other-labels", "--json", "--catalog-dir", str(appr_env["cat"])],
    )
    assert other_res.exit_code == 0
    other_data = json.loads(other_res.output)
    assert other_data["count"] == 1
    assert other_data["other_labels"][0]["other_label"] == "force-field-bias"

    # MCP auth gate negative + positive
    unauth = asyncio.run(
        mcp_appraise_tool(
            payload=_valid_payload(ref="doi:10.1101/mcp"),
            project_root=str(appr_env["proj"]),
            catalog_dir=str(appr_env["cat"]),
            token="",
        )
    )
    assert unauth["ok"] is False
    assert unauth["error_code"] == BathosErrorCode.AUTH_ERROR.value

    auth_ok = asyncio.run(
        mcp_appraise_tool(
            payload=_valid_payload(ref="doi:10.1101/mcp"),
            project_root=str(appr_env["proj"]),
            catalog_dir=str(appr_env["cat"]),
            token=appr_env["token"],
        )
    )
    assert auth_ok["ok"] is True
    assert auth_ok["appraisal"]["ref"] == "doi:10.1101/mcp"


# ── Negative & Mutation Validation Tests (Rules 1-6) ─────────────────────────


def test_rule1_rejects_empty_for_or_against():
    with pytest.raises(AppraisalValidationError, match="non-empty \\[\\[for\\]\\]"):
        validate_appraisal_payload(_valid_payload(**{"for": []}))
    with pytest.raises(AppraisalValidationError, match="non-empty \\[\\[against\\]\\]"):
        validate_appraisal_payload(_valid_payload(against=[]))


@pytest.mark.parametrize(
    "bad_stmt",
    [
        "too short",
        "None",
        "N/A",
        "No concerns found in this paper at all.",
        "No issues were identified during review.",
        "Nothing to report for this citation.",
    ],
)
def test_rule1_rejects_short_or_sentinel_statements(bad_stmt: str):
    payload = _valid_payload(
        against=[
            {
                "id": "a1",
                "category": "single-source",
                "statement": bad_stmt,
                "basis": "asserted",
                "severity": "minor",
            }
        ]
    )
    with pytest.raises(AppraisalValidationError):
        validate_appraisal_payload(payload)


@pytest.mark.parametrize(
    "score_key",
    ["confidence", "trust", "score", "overall_score", "rating", "lgtm"],
)
def test_rule3_rejects_combined_score_fields(score_key: str):
    with pytest.raises(AppraisalValidationError, match="forbidden"):
        validate_appraisal_payload(_valid_payload(**{score_key: 0.8}))


def test_rule4_severity_rules_and_addressed_by_checks():
    # `for` must NOT have severity
    bad_for = _valid_payload(
        **{
            "for": [
                {
                    "id": "f1",
                    "category": "controls-present",
                    "statement": "Positive and negative controls were executed properly here.",
                    "basis": "inspected",
                    "severity": "minor",
                }
            ]
        }
    )
    with pytest.raises(AppraisalValidationError, match="must not declare 'severity'"):
        validate_appraisal_payload(bad_for)

    # `against` requires valid severity
    bad_against = _valid_payload(
        against=[
            {
                "id": "a1",
                "category": "single-source",
                "statement": "No independent replication found anywhere in the literature.",
                "basis": "asserted",
                "severity": "",
            }
        ]
    )
    with pytest.raises(AppraisalValidationError, match="severity"):
        validate_appraisal_payload(bad_against)

    # unknown local addressed_by fails
    bad_addr = _valid_payload(
        against=[
            {
                "id": "a1",
                "category": "single-source",
                "statement": "No independent replication found anywhere in the literature.",
                "basis": "asserted",
                "severity": "minor",
                "addressed_by": ["f999"],
            }
        ]
    )
    with pytest.raises(AppraisalValidationError, match="unknown local for entry"):
        validate_appraisal_payload(bad_addr)


def test_rule5_scope_normalization_when_shallow_read_depth():
    # Identical after case/whitespace/punctuation normalization with read_depth='full-text' -> fail
    shallow_same = _valid_payload(
        read_depth="full-text",
        scope_claimed="PDZ3 allostery generally!",
        scope_supported="  pdz3   allostery generally. ",
    )
    with pytest.raises(AppraisalValidationError, match="scope_supported must differ"):
        validate_appraisal_payload(shallow_same)

    # Same scope IS allowed when read_depth >= 'methods'
    deep_same = _valid_payload(
        read_depth="methods",
        scope_claimed="PDZ3 allostery generally!",
        scope_supported="pdz3 allostery generally.",
    )
    norm = validate_appraisal_payload(deep_same)
    assert norm["read_depth"] == "methods"


def test_rule6_category_closed_list_and_other_label_tracking(appr_env):
    assert "other" in FOR_CATEGORIES
    assert "other" in AGAINST_CATEGORIES

    with pytest.raises(AppraisalValidationError, match="not in closed list"):
        validate_appraisal_payload(
            _valid_payload(
                against=[
                    {
                        "id": "a1",
                        "category": "invented-category",
                        "statement": "This category is not part of the closed v1 appraisal taxonomy.",
                        "basis": "inspected",
                        "severity": "minor",
                    }
                ]
            )
        )

    # category="other" requires non-empty other_label
    with pytest.raises(AppraisalValidationError, match="other_label"):
        validate_appraisal_payload(
            _valid_payload(
                against=[
                    {
                        "id": "a1",
                        "category": "other",
                        "statement": "A domain-specific concern that needs an explicit other_label.",
                        "basis": "inspected",
                        "severity": "minor",
                    }
                ]
            )
        )

    # non-"other" with other_label is rejected
    with pytest.raises(AppraisalValidationError, match="other_label"):
        validate_appraisal_payload(
            _valid_payload(
                against=[
                    {
                        "id": "a1",
                        "category": "single-source",
                        "other_label": "extra",
                        "statement": "Only one group reported this effect without external replication.",
                        "basis": "inspected",
                        "severity": "minor",
                    }
                ]
            )
        )

    # Aggregate recurring other_labels
    for i in range(2):
        create_appraisal(
            payload=_valid_payload(
                ref=f"doi:10.1101/other/{i}",
                against=[
                    {
                        "id": "a1",
                        "category": "other",
                        "other_label": "Force-Field-Bias",
                        "statement": "Only one force field was evaluated in the trajectory suite.",
                        "basis": "inspected",
                        "severity": "minor",
                    }
                ],
            ),
            project_root=appr_env["proj"],
            catalog_dir=appr_env["cat"],
        )
    summary = summarize_other_labels(catalog_dir=appr_env["cat"])
    assert len(summary) == 1
    assert summary[0]["side"] == "against"
    assert summary[0]["count"] == 2


# ── Session fallback, Independence, C12 Self-rebuttal, & Use Tier ────────────


def test_session_fallback_and_attested_flags(appr_env, monkeypatch):
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # Agent without session env or explicit appraiser_session is rejected
    with pytest.raises(AppraisalValidationError, match="appraiser_session"):
        create_appraisal(
            payload=_valid_payload(),
            project_root=appr_env["proj"],
            catalog_dir=appr_env["cat"],
        )

    # Explicit appraiser_session succeeds with session_attested=True
    rec = create_appraisal(
        payload=_valid_payload(),
        appraiser_session="manual-sess-42",
        appraiser_kind="subagent",
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    assert rec.appraiser_session == "manual-sess-42"
    assert rec.session_attested is True
    assert rec.appraiser_kind == "subagent"
    assert rec.kind_attested is True


def test_use_tier_non_compensatory_and_c12_self_rebuttal(appr_env, monkeypatch):
    # 1. Self-authored `for` with basis='inspected' does NOT address major/decisive against (C12)
    appr_major_self = create_appraisal(
        payload=_valid_payload(
            against=[
                {
                    "id": "a1",
                    "category": "unconverged-sampling",
                    "statement": "100 ns per replica; block analysis in Fig S5 has not plateaued.",
                    "basis": "inspected",
                    "severity": "major",
                    "addressed_by": ["f1"],
                }
            ]
        ),
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    tier_self = compute_use_tier(
        [appr_major_self],
        value_provenance="published",
        citing_session="other-sess",
    )
    assert tier_self.use_tier == "contested"
    assert f"{appr_major_self.id}:a1" in tier_self.unaddressed_major

    # 2. Same-session `for` with basis='executed' DOES address major against
    appr_executed = create_appraisal(
        payload=_valid_payload(
            **{
                "for": [
                    {
                        "id": "f1",
                        "category": "artifact-available",
                        "statement": "Re-ran block analysis across full 500 ns trajectory and confirmed plateau.",
                        "basis": "executed",
                    }
                ],
                "against": [
                    {
                        "id": "a1",
                        "category": "unconverged-sampling",
                        "statement": "100 ns per replica; initial block analysis appeared unconverged.",
                        "basis": "inspected",
                        "severity": "major",
                        "addressed_by": ["f1"],
                    }
                ],
            }
        ),
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    tier_exec = compute_use_tier(
        [appr_executed],
        value_provenance="published",
        citing_session="other-sess",
    )
    assert tier_exec.use_tier == "threshold-source"

    # 3. Cross-record addressed_by from a DIFFERENT appraiser_session with basis='inspected' works
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "sess-appraiser-2")
    appr_cross = create_appraisal(
        payload=_valid_payload(
            against=[
                {
                    "id": "a1",
                    "category": "unconverged-sampling",
                    "statement": "100 ns per replica initially looked unconverged without extended SI check.",
                    "basis": "inspected",
                    "severity": "decisive",
                    "addressed_by": [f"{appr_major_self.id}:f1"],
                }
            ]
        ),
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    tier_cross = compute_use_tier(
        [appr_cross],
        value_provenance="published",
        citing_session="sess-citing-3",
        lookup_appraisals={appr_major_self.id: appr_major_self, appr_cross.id: appr_cross},
    )
    assert tier_cross.use_tier == "threshold-source"

    # 4. Non-compensatory across appraisals: unaddressed decisive in one appraisal refutes even with
    #    another glowing appraisal
    appr_decisive = create_appraisal(
        payload=_valid_payload(
            against=[
                {
                    "id": "a1",
                    "category": "leakage-or-circularity",
                    "statement": "Test set trajectories were included in normalization scaling fit.",
                    "basis": "inspected",
                    "severity": "decisive",
                    "addressed_by": [],
                }
            ]
        ),
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    tier_combined = compute_use_tier(
        [appr_executed, appr_decisive],
        value_provenance="published",
        citing_session="sess-citing-3",
    )
    assert tier_combined.use_tier == "refuted-for-use"


def test_independence_fork_vs_subagent_and_provenance_caps(appr_env):
    # Same session as citing run:
    # - session -> NOT independent -> capped at hypothesis-source
    # - fork -> NOT independent -> capped at hypothesis-source
    # - subagent -> independent -> reaches threshold-source (when value_provenance in published/derived)
    appr_same_sess = create_appraisal(
        payload=_valid_payload(),
        appraiser_session="sess-parent",
        appraiser_kind="session",
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    appr_fork = create_appraisal(
        payload=_valid_payload(),
        appraiser_session="sess-parent",
        appraiser_kind="fork",
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )
    appr_subagent = create_appraisal(
        payload=_valid_payload(),
        appraiser_session="sess-parent",
        appraiser_kind="subagent",
        project_root=appr_env["proj"],
        catalog_dir=appr_env["cat"],
    )

    assert (
        compute_use_tier(
            [appr_same_sess], value_provenance="published", citing_session="sess-parent"
        ).use_tier
        == "hypothesis-source"
    )
    assert (
        compute_use_tier(
            [appr_fork], value_provenance="published", citing_session="sess-parent"
        ).use_tier
        == "hypothesis-source"
    )
    sub_tier = compute_use_tier(
        [appr_subagent], value_provenance="published", citing_session="sess-parent"
    )
    assert sub_tier.use_tier == "threshold-source"
    assert sub_tier.independence_verified is True

    # figure-read and agent-summary NEVER reach threshold-source
    for prov in ("figure-read", "agent-summary"):
        res = compute_use_tier(
            [appr_subagent], value_provenance=prov, citing_session="sess-parent"
        )
        assert res.use_tier == "hypothesis-source"
        with pytest.raises(AppraisalValidationError):
            validate_uses_as("threshold-source", res)

    validate_uses_as("hypothesis-source", sub_tier)
    validate_uses_as("threshold-source", sub_tier)


def test_fold_appraisal_idempotent_and_ignores_foreign():
    ev = {
        "v": 1,
        "eid": "eid-1",
        "kind": "appraisal.created",
        "entity": ["appr_abc"],
        "ts": "2026-11-23T12:00:00Z",
        "project": "p1",
        "data": {
            "id": "appr_abc",
            "ref": "doi:10.1/x",
            "locator": "Fig 1",
            "appraiser": "human:alice",
            "appraiser_session": "human:alice",
            "read_depth": "methods",
            "scope_claimed": "A",
            "scope_supported": "B",
            "for_entries": [],
            "against_entries": [],
            "payload_sha256": "0" * 64,
        },
    }
    foreign = {**ev, "eid": "eid-0", "kind": "observation.created"}
    row = fold_appraisal([foreign, ev, ev])
    assert row is not None
    assert row["id"] == "appr_abc"
    assert row["ref"] == "doi:10.1/x"
    assert fold_appraisal([]) is None
