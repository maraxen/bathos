"""#2195: unknown keys in [experiment] (e.g. `stage` for `stage_name`) must warn."""

from __future__ import annotations

import textwrap
from pathlib import Path

_BODY = """
    [outcomes.pass]
    condition = "x < 5"
    decision = "proceed"
    reasoning = "fine"
    [outcomes.fail]
    condition = "x >= 5"
    decision = "debug"
    reasoning = "bad"
    is_residual = true
    [result_schema]
    x = "float"
"""


def _write(tmp_path: Path, experiment_lines: str) -> Path:
    p = tmp_path / "s.bth.toml"
    p.write_text(
        '[experiment]\nhypothesis = "h"\n'
        + textwrap.dedent(experiment_lines)
        + textwrap.dedent(_BODY)
    )
    return p


def _validate(path: Path):
    from bathos.sidecar import parse_sidecar
    from bathos.validate import validate_sidecar

    return validate_sidecar(parse_sidecar(path), sidecar_path=path)


def test_stage_key_warns_with_did_you_mean(tmp_path):
    result = _validate(_write(tmp_path, 'stage = "confirmation"\n'))
    assert result.ok is True  # warning, not error
    assert result.errors == []
    assert len(result.warnings) == 1
    assert "stage" in result.warnings[0]
    assert "did you mean 'stage_name'" in result.warnings[0]


def test_unknown_key_without_close_match_warns_without_suggestion(tmp_path):
    result = _validate(_write(tmp_path, 'zzzqqq = 1\n'))
    assert result.ok is True
    assert len(result.warnings) == 1
    assert "zzzqqq" in result.warnings[0]
    assert "did you mean" not in result.warnings[0]


def test_known_keys_produce_no_warnings(tmp_path):
    result = _validate(
        _write(
            tmp_path,
            'stage_name = "confirmation"\nnovel = true\nagent_mode = "autonomous"\n'
            'claim_discriminates = ["H1"]\nclaim_isolates = ["C1"]\n',
        )
    )
    assert result.ok is True
    assert result.warnings == []


def test_validate_sidecar_tool_surfaces_warnings(tmp_path):
    from bathos.mcp import validate_sidecar_tool

    out = validate_sidecar_tool(path=str(_write(tmp_path, 'stage = "confirmation"\n')))
    assert out["validation_ok"] is True
    assert any("stage_name" in w for w in out["warnings"])
