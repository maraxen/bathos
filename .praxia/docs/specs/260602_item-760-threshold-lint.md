# Spec: Threshold Epistemic Hygiene — Sidecar Lint for Unjustified Numeric Cutoffs

**Item:** #760
**task_id:** 260602_bathos-v08-sprint
**Date:** 2026-06-02
**Status:** Ready to implement
**Track:** implement

---

## Problem Statement

Numeric thresholds embedded in sidecar outcome `condition` strings (e.g., `temp_std < 5`) and in benchmark `regression_threshold` fields (e.g., `regression_threshold = 0.10`) are stored without any required rationale. A researcher writing `condition = "temp_std < 5.0"` may have sound domain justification, a literature citation, or pure guesswork — the sidecar format currently makes all three indistinguishable.

Backlog item #143 and ADR `260601_sprint-audit-threshold-rationale.md` both identify this as calibration debt. The sprint-audit ADR documents six of the seven internal thresholds and notes which are "Uncalibrated" — that discipline should extend to researcher-authored sidecar thresholds.

This spec adds a Tier-2 `bth lint` check that detects bare numeric literals in outcome conditions and benchmark threshold fields, and warns when no accompanying `source` rationale is provided.

---

## Scope

### In scope

1. New optional field `source: str` on `OutcomeSpec` in `sidecar.py` — surfaces rationale for numeric cutoffs in condition strings.
2. New optional field `regression_threshold_basis: str` on `Sidecar` in `sidecar.py` — surfaces rationale for benchmark `regression_threshold` values.
3. New function `check_threshold_basis(project_root: Path) -> list[LintIssue]` in `linter.py` — Tier-2 file-based check, `severity=WARNING`.
4. Wire `check_threshold_basis` into the `lint` command in `cli.py` alongside `check_adversarial_checks`.
5. Tests in `tests/test_linter.py` — positive (bare numeric → warning), negative (source present → no warning), and edge cases.

### Out of scope

- Enforcing that the `source` value is semantically valid (non-empty string is sufficient).
- Modifying `validate.py` (structural validation) — threshold basis is a Tier-2 lint concern, not a structural error.
- Changing `bth run` enforcement or the pre-execution manifest (`.bth.lock.toml`).
- Touching benchmark `outcomes` sections (benchmarks have no `[outcomes]` block in the standard schema; regression threshold is the only numeric cutoff).

---

## Data Model Changes

### `src/bathos/sidecar.py`

#### 1. `OutcomeSpec` — add `source` field

Old:
```python
@dataclass
class OutcomeSpec:
    condition: str
    decision: str
    reasoning: str = ""
    is_residual: bool = False
    adversarial_check: str | None = None
```

New (add one field at the end):
```python
@dataclass
class OutcomeSpec:
    condition: str
    decision: str
    reasoning: str = ""
    is_residual: bool = False
    adversarial_check: str | None = None
    source: str = ""          # citation/rationale for numeric cutoffs in `condition`
```

`source` is an optional free-text string. Examples:
- `"arXiv 2509.08713 Table 3 (10% error rate threshold)"`
- `"internal: domain expert consensus, ±5K stability from standard NVT literature"`
- `"arbitrary — needs calibration"`

#### 2. `_parse_outcomes` — read `source` from TOML

In the `_parse_outcomes` function, add `source=spec.get("source", "")` to the `OutcomeSpec` constructor call.

Old:
```python
label: OutcomeSpec(
    condition=spec.get("condition", ""),
    decision=spec.get("decision", ""),
    reasoning=spec.get("reasoning", ""),
    is_residual=bool(spec.get("is_residual", False)),
    adversarial_check=spec.get("adversarial_check"),
)
```

New:
```python
label: OutcomeSpec(
    condition=spec.get("condition", ""),
    decision=spec.get("decision", ""),
    reasoning=spec.get("reasoning", ""),
    is_residual=bool(spec.get("is_residual", False)),
    adversarial_check=spec.get("adversarial_check"),
    source=spec.get("source", ""),
)
```

#### 3. `Sidecar` — add `regression_threshold_basis` field

Add immediately after the `regression_threshold` field in the dataclass:

Old:
```python
    regression_threshold: float = 0.0
    target: str = ""
```

New:
```python
    regression_threshold: float = 0.0
    regression_threshold_basis: str = ""   # citation/rationale for regression_threshold
    target: str = ""
```

#### 4. `parse_sidecar` — read `regression_threshold_basis` from benchmark section

In the `elif "benchmark" in data:` branch, add `regression_threshold_basis=section.get("regression_threshold_basis", "")` to the `Sidecar(...)` constructor call, immediately after `regression_threshold=...`:

Old:
```python
sidecar = Sidecar(
    kind=kind,
    baseline_ref=section.get("baseline_ref", ""),
    metric=section.get("metric", ""),
    regression_threshold=section.get("regression_threshold", 0.0),
    target=section.get("target", ""),
    result_schema=data.get("result_schema", {}),
    agent_mode=section.get("agent_mode", ""),
)
```

New:
```python
sidecar = Sidecar(
    kind=kind,
    baseline_ref=section.get("baseline_ref", ""),
    metric=section.get("metric", ""),
    regression_threshold=section.get("regression_threshold", 0.0),
    regression_threshold_basis=section.get("regression_threshold_basis", ""),
    target=section.get("target", ""),
    result_schema=data.get("result_schema", {}),
    agent_mode=section.get("agent_mode", ""),
)
```

---

## Linter Changes

### `src/bathos/linter.py`

#### New module-level constant `_NUMERIC_LITERAL_RE`

Add after the existing `_SLURM_VERB_NOUN_RE` constant (line ~31) and before `_DIR_RULES`:

```python
# Matches numeric literals (int, float, scientific notation) in DuckDB SQL condition strings.
# Negative lookbehind prevents matching numeric suffixes in identifiers (e.g. "node4007").
_NUMERIC_LITERAL_RE = re.compile(
    r"(?<![a-zA-Z_])\b-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?\b"
)
```

#### New function: `check_threshold_basis`

Add after `check_adversarial_checks` at the end of the file:

```python
def check_threshold_basis(project_root: Path) -> list[LintIssue]:
    """Tier-2: Warn when numeric literals in outcome conditions or benchmark
    regression_threshold lack an accompanying source/basis rationale.

    Scans all .bth.toml files in the project. For each sidecar:
    - outcome conditions containing numeric literals: warn if OutcomeSpec.source is empty
    - benchmark regression_threshold != 0.0: warn if regression_threshold_basis is empty

    Args:
        project_root: Root directory of the project.

    Returns:
        List of LintIssue objects with severity WARNING.
    """
    issues: list[LintIssue] = []

    for sidecar_path in project_root.rglob("*.bth.toml"):
        try:
            with open(sidecar_path, "rb") as f:
                data = tomllib.load(f)
        except Exception:
            continue

        # --- Check outcome conditions ---
        outcomes = data.get("outcomes", {})
        for label, outcome in outcomes.items():
            condition = outcome.get("condition", "")
            source = outcome.get("source", "")
            if condition and _NUMERIC_LITERAL_RE.search(condition) and not source:
                issues.append(LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="unjustified_threshold",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"outcomes.{label}.condition contains numeric literal "
                        f"({condition!r}) with no source rationale — "
                        f"add outcomes.{label}.source = \"<citation or 'arbitrary'>\""
                    ),
                ))

        # --- Check benchmark regression_threshold ---
        benchmark = data.get("benchmark", {})
        if benchmark:
            reg_threshold = benchmark.get("regression_threshold")
            reg_basis = benchmark.get("regression_threshold_basis", "")
            if reg_threshold is not None and float(reg_threshold) != 0.0 and not reg_basis:
                issues.append(LintIssue(
                    path=sidecar_path,
                    directory="sidecar",
                    issue="unjustified_threshold",
                    severity=IssueSeverity.WARNING,
                    detail=(
                        f"benchmark.regression_threshold = {reg_threshold} has no basis — "
                        "add benchmark.regression_threshold_basis = \"<citation or 'arbitrary'>\""
                    ),
                ))

    return issues
```

**Regex design notes:**
- `(?<![a-zA-Z_])` negative lookbehind prevents matching numeric suffix of identifiers like `node4007`.
- `\b` word boundary prevents matching mid-word digits in strings like `"col_1"`.
- Matches integers, floats, scientific notation, and negative literals.
- Does NOT match `TRUE`, `FALSE`, `NULL`, or bare column names.
- Intentionally fires on `count > 0`: even trivial cutoffs benefit from a comment. The researcher can add `source = "zero is the natural lower bound"` to suppress it.
- Residual catch-all outcomes typically use `TRUE` or `1=1` — no numeric literals, no warning.

---

## CLI Changes

### `src/bathos/cli.py`

In the `lint` command body (function defined at line 929), make two changes:

**1. Add `check_threshold_basis` to the import block inside the function body:**

Old:
```python
    from bathos.linter import (
        IssueSeverity,
        check_adversarial_checks,
        check_bypass_trend,
        check_residual_rates,
        check_unfired_branches,
        lint_project,
    )
```

New:
```python
    from bathos.linter import (
        IssueSeverity,
        check_adversarial_checks,
        check_bypass_trend,
        check_residual_rates,
        check_threshold_basis,
        check_unfired_branches,
        lint_project,
    )
```

**2. Call `check_threshold_basis` after `check_adversarial_checks`:**

Old:
```python
    issues = lint_project(project_root.resolve())

    # Add Tier-2 file-based checks
    issues.extend(check_adversarial_checks(project_root.resolve()))

    # Add warm-catalog Tier-2 checks if catalog exists
```

New:
```python
    issues = lint_project(project_root.resolve())

    # Add Tier-2 file-based checks
    issues.extend(check_adversarial_checks(project_root.resolve()))
    issues.extend(check_threshold_basis(project_root.resolve()))

    # Add warm-catalog Tier-2 checks if catalog exists
```

No change to CLI output formatting — existing `warning: <path> — unjustified_threshold: <detail>` output pattern is correct.

---

## Test Cases

### `tests/test_linter.py`

Add the following helper and nine test functions. All are independent and use `tmp_path`.

#### Helper

```python
def _make_sidecar_toml(path: Path, content: str) -> Path:
    path.write_text(content)
    return path
```

---

#### TC-1: Bare numeric in condition, no source → WARNING

```python
def test_check_threshold_basis_bare_numeric_warns(tmp_path):
    from bathos.linter import check_threshold_basis, IssueSeverity
    toml_content = """
[experiment]
hypothesis = "test"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 5.0"
decision = "debug"
reasoning = "too noisy"
is_residual = true

[result_schema]
temp_std = "float"
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    assert any(i.issue == "unjustified_threshold" for i in issues)
    assert all(
        i.severity == IssueSeverity.WARNING
        for i in issues if i.issue == "unjustified_threshold"
    )
```

#### TC-2: Numeric in condition WITH source → no warning

```python
def test_check_threshold_basis_source_suppresses_warning(tmp_path):
    from bathos.linter import check_threshold_basis
    toml_content = """
[experiment]
hypothesis = "test"

[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "good enough"
source = "NVT standard: ±5K from Frenkel & Smit, §4.2"
is_residual = false

[outcomes.fail]
condition = "temp_std >= 5.0"
decision = "debug"
reasoning = "too noisy"
source = "complement of pass condition"
is_residual = true

[result_schema]
temp_std = "float"
"""
    _make_sidecar_toml(tmp_path / "run_nvt.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []
```

#### TC-3: Condition with no numeric literals → no warning

```python
def test_check_threshold_basis_no_numeric_no_warning(tmp_path):
    from bathos.linter import check_threshold_basis
    toml_content = """
[experiment]
hypothesis = "test"

[outcomes.pass]
condition = "reproduced = TRUE"
decision = "proceed"
reasoning = "boolean check"
is_residual = false

[outcomes.fail]
condition = "reproduced = FALSE"
decision = "debug"
reasoning = "did not reproduce"
is_residual = true

[result_schema]
reproduced = "bool"
"""
    _make_sidecar_toml(tmp_path / "run_repro.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []
```

#### TC-4: Benchmark regression_threshold without basis → WARNING

```python
def test_check_threshold_basis_benchmark_threshold_warns(tmp_path):
    from bathos.linter import check_threshold_basis, IssueSeverity
    toml_content = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
target = "> 50 ns/day"

[result_schema]
ns_per_day = "float"
"""
    _make_sidecar_toml(tmp_path / "bench_perf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert len(threshold_issues) == 1
    assert threshold_issues[0].severity == IssueSeverity.WARNING
    assert "regression_threshold" in threshold_issues[0].detail
```

#### TC-5: Benchmark regression_threshold WITH basis → no warning

```python
def test_check_threshold_basis_benchmark_with_basis_suppresses_warning(tmp_path):
    from bathos.linter import check_threshold_basis
    toml_content = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
regression_threshold_basis = "5% is standard GROMACS regression gate (internal policy)"
target = "> 50 ns/day"

[result_schema]
ns_per_day = "float"
"""
    _make_sidecar_toml(tmp_path / "bench_perf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []
```

#### TC-6: Benchmark regression_threshold = 0.0 (default) → no warning

```python
def test_check_threshold_basis_benchmark_zero_threshold_no_warning(tmp_path):
    from bathos.linter import check_threshold_basis
    toml_content = """
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.0
target = "> 50 ns/day"

[result_schema]
ns_per_day = "float"
"""
    _make_sidecar_toml(tmp_path / "bench_perf.bth.toml", toml_content)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert threshold_issues == []
```

#### TC-7: Unparseable TOML → silent skip, no crash

```python
def test_check_threshold_basis_invalid_toml_skips(tmp_path):
    from bathos.linter import check_threshold_basis
    bad = tmp_path / "broken.bth.toml"
    bad.write_text("[experiment\nhypothesis = broken toml")
    issues = check_threshold_basis(tmp_path)
    assert all("broken" not in str(i.path) for i in issues)
```

#### TC-8: Multiple sidecars, mixed — only bare-numeric ones warn

```python
def test_check_threshold_basis_multiple_sidecars_mixed(tmp_path):
    from bathos.linter import check_threshold_basis
    good_toml = """
[experiment]
hypothesis = "good"
[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed"
reasoning = "fine"
source = "domain knowledge"
is_residual = false
[outcomes.fail]
condition = "temp_std >= 5.0"
decision = "fix"
reasoning = "noisy"
source = "complement"
is_residual = true
[result_schema]
temp_std = "float"
"""
    bad_toml = """
[experiment]
hypothesis = "bad"
[outcomes.pass]
condition = "accuracy > 0.95"
decision = "deploy"
reasoning = "good enough"
is_residual = false
[outcomes.fail]
condition = "accuracy <= 0.95"
decision = "retrain"
reasoning = "too low"
is_residual = true
[result_schema]
accuracy = "float"
"""
    _make_sidecar_toml(tmp_path / "run_good.bth.toml", good_toml)
    _make_sidecar_toml(tmp_path / "run_bad.bth.toml", bad_toml)
    issues = check_threshold_basis(tmp_path)
    threshold_issues = [i for i in issues if i.issue == "unjustified_threshold"]
    assert len(threshold_issues) >= 1
    paths_warned = {str(i.path) for i in threshold_issues}
    assert any("run_bad" in p for p in paths_warned)
    assert not any("run_good" in p for p in paths_warned)
```

#### TC-9: CLI `bth lint` surfaces unjustified_threshold warnings

```python
def test_cli_lint_threshold_warning_appears(tmp_path, monkeypatch):
    from bathos.cli import app
    from typer.testing import CliRunner
    monkeypatch.chdir(tmp_path)
    catalog_dir = tmp_path / ".bth" / "catalog"
    catalog_dir.mkdir(parents=True)
    monkeypatch.setenv("BTH_CATALOG_DIR", str(catalog_dir))

    toml_content = """
[experiment]
hypothesis = "test"
[outcomes.pass]
condition = "err < 0.01"
decision = "proceed"
reasoning = "good"
is_residual = false
[outcomes.fail]
condition = "err >= 0.01"
decision = "fix"
reasoning = "bad"
is_residual = true
[result_schema]
err = "float"
"""
    (tmp_path / "run_test.bth.toml").write_text(toml_content)

    runner = CliRunner()
    result = runner.invoke(app, ["lint", "--project-root", str(tmp_path)])
    assert "unjustified_threshold" in result.output
    assert "warning" in result.output.lower()
```

---

## Sidecar TOML Schema Addendum

The following optional fields are now valid in `.bth.toml` files.

**Experiment / validation / debug sidecars — within any `[outcomes.<label>]` block:**
```toml
[outcomes.pass]
condition = "temp_std < 5.0"
decision = "proceed to NPT validation"
reasoning = "NVT temperature stability criterion"
source = "Frenkel & Smit §4.2: ±5K is standard NVT acceptance"   # NEW optional
is_residual = false
```

**Benchmark sidecars — within the `[benchmark]` block:**
```toml
[benchmark]
baseline_ref = "run_abc123"
metric = "ns_per_day"
regression_threshold = 0.05
regression_threshold_basis = "5% internal policy threshold, uncalibrated"  # NEW optional
target = "> 50 ns/day"
```

Both fields are optional. Absence triggers a Tier-2 `WARNING` when a numeric literal is detected; presence suppresses it. Any non-empty string is accepted — including `"arbitrary"` or `"uncalibrated — needs calibration"`.

---

## Regression Test Coverage

The existing `test_clean_project_returns_no_issues` test uses a minimal sidecar (`[experiment]\nhypothesis='h'\n[result_schema]\n`) with no `[outcomes]` block. The new check will not fire on it because there are no outcome conditions to inspect. No existing test requires updating.

---

## Implementation Order

1. `src/bathos/sidecar.py` — add `source` to `OutcomeSpec`; update `_parse_outcomes`; add `regression_threshold_basis` to `Sidecar`; update `parse_sidecar` benchmark branch.
2. `src/bathos/linter.py` — add `_NUMERIC_LITERAL_RE` constant and `check_threshold_basis` function.
3. `src/bathos/cli.py` — add `check_threshold_basis` to import and call it after `check_adversarial_checks`.
4. `tests/test_linter.py` — add helper `_make_sidecar_toml` and TC-1 through TC-9.

No database migrations, no schema version bumps, no changes to `validate.py`, `compact.py`, or the cool-tier Parquet schema.

---

## Key Decisions

1. **Tier-2 (warn, not error):** Numeric cutoffs without source rationale are a hygiene concern, not a structural defect. Escalating to error would block `bth run` on legacy sidecars.

2. **`source` not `threshold_basis` on `OutcomeSpec`:** The field name `source` is general enough to serve as a citation anchor for any rationale in the outcome block. It mirrors the pattern used in academic pre-registration (CONSORT, OSF) where `source` annotates any outcome criterion.

3. **Separate `regression_threshold_basis` on `Sidecar`:** Benchmark sidecars have no `[outcomes]` block and their threshold lives at the `[benchmark]` level, so a separate field at that level is the correct placement. The `_basis` suffix (rather than `_source`) matches the ADR's "threshold basis" terminology and distinguishes it from `OutcomeSpec.source`.

4. **Conservative regex — fires on `count > 0`:** Zero and small integers trigger the warning intentionally. The cost of a false positive (researcher adds `source = "zero is the natural lower bound"`) is lower than the cost of a false negative (researcher writes `n_steps >= 1` with no rationale and it passes lint).

5. **Silent skip on TOML parse errors:** Consistent with `check_adversarial_checks`. Malformed files are already flagged by `bth lint` Tier-1 when `parse_sidecar` fails.

6. **No `validate.py` change:** `validate.py` enforces structural correctness (SQL parses, is_residual present). Threshold justification is epistemic hygiene, a distinct concern. Mixing them in `validate.py` would create false ERROR-level failures on valid sidecars that merely lack source annotations.

---

## Open Questions

None. All design decisions are resolved above.
