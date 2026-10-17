"""Regression for #5199: a [benchmark] sidecar must carry its [outcomes.*] through parsing.

parse_sidecar's benchmark branch used to build the Sidecar without calling _parse_outcomes,
so validate_sidecar (which requires non-empty outcomes) could never accept a benchmark sidecar.
"""

import textwrap
from pathlib import Path

from bathos.sidecar import SidecarKind, parse_sidecar
from bathos.validate import validate_sidecar


def _write_toml(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "bench.bth.toml"
    path.write_text(textwrap.dedent(body))
    return path


BENCHMARK_WITH_OUTCOMES = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
regression_threshold_basis = "conventional 5% noise floor"
target = "> 50 ns/day on pi_so3"

[outcomes.pass]
condition = "ns_per_day >= 50"
decision = "accept benchmark"
reasoning = "meets target"

[outcomes.fail]
condition = "ns_per_day < 50"
decision = "investigate regression"
reasoning = "below target"
is_residual = true

[result_schema]
ns_per_day = "float"
"""


def test_benchmark_sidecar_parses_outcomes(tmp_path):
    s = parse_sidecar(_write_toml(tmp_path, BENCHMARK_WITH_OUTCOMES))
    assert s.kind == SidecarKind.BENCHMARK
    assert s.outcomes, "benchmark sidecar lost its [outcomes.*] tables during parsing"
    assert set(s.outcomes) == {"pass", "fail"}
    assert s.outcomes["pass"].condition == "ns_per_day >= 50"


def test_benchmark_sidecar_with_outcomes_passes_validation(tmp_path):
    s = parse_sidecar(_write_toml(tmp_path, BENCHMARK_WITH_OUTCOMES))
    result = validate_sidecar(s)
    assert result.ok, [str(e) for e in result.errors]
