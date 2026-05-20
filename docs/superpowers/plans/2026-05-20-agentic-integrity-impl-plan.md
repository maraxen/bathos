Now I'll create the detailed implementation plan:

# Implementation Plan: bathos Agentic Integrity Features

**Revision 4** — final pseudocode corrections before fixer dispatch (Run.id field, IssueSeverity.WARNING, LintIssue.issue/.detail attributes, _row_to_run deserializer).

## Overview

This plan decomposes the validated design (§10, 17 items, priorities P0–P3) into 43 atomic, parallelizable tasks organized into 6 phases. All P0 and P1 work is sequenced dependency-first; P2 items are grouped by feature clusters; P3 items are deferred. The plan assumes a skilled Python developer with zero context and provides exact file paths, test cases, verification gates, and risk flags.

**Goal:** Implement pre-registration enforcement, sidecar gating, run modes (collaborative/autonomous), campaigns (exploration/confirmation), and sprint audit.

**Tech Stack:** Python 3.12, Typer, DuckDB, PyArrow, dataclasses, tomllib, hashlib

---

## Phase 1: Result Emission Pipeline (P0 — Blocker)

**Dependency:** None  
**Blocks:** All outcome evaluation and campaign verdict logic  
**Parallelism:** Sequential; single task  
**Risk:** Critical — if metadata never populated, entire agentic integrity system is non-functional

### Task 1.1: Implement Result Emission in `runner.py` (P0)

**Files:**
- Modify: `src/bathos/runner.py:50-123`
- Modify: `src/bathos/schema.py` — add `output_metadata` field if not present
- Test: `tests/test_runner.py` — new test case

**Changes:**

1. **Step 1: Understand current bug**
   - Read `runner.py:105-112`; `run.metadata` is initialized to `"{}"` but never populated with actual results
   - `evaluate_outcome()` at line 109 reads empty dict, always returns `"unknown"`

2. **Step 2: Write failing tests** (TDD)
   - Create `tests/test_runner.py::test_result_emission_env_var_path()`
   - Test that if `$BTH_RESULTS_PATH` is set to a JSON file, `runner.py` reads it and populates `run.metadata`
   - Expected: result dict from file appears in run.metadata
   
   ```python
   def test_result_emission_env_var_path(tmp_path):
       results_file = tmp_path / "results.json"
       results_file.write_text('{"temp_mean": 300.5, "temp_std": 2.3}')
       
       # Mock subprocess that exits cleanly
       import subprocess
       with unittest.mock.patch('subprocess.run') as mock_run:
           mock_run.return_value.returncode = 0
           
       run = run_script(
           argv=['python', 'dummy.py'],
           project_slug='test',
           catalog_dir=tmp_path,
           output_paths=[],
           tags=[],
           env={'BTH_RESULTS_PATH': str(results_file)}
       )
       
       # After compaction, check that run.metadata was populated
       assert json.loads(run.metadata) == {"temp_mean": 300.5, "temp_std": 2.3}
   ```

3. **Step 3: Write failing fallback test**
   - Create `tests/test_runner.py::test_result_emission_fallback_path()`
   - Test that if `$BTH_RESULTS_PATH` is unset, fallback to `<script-stem>.bth-results.json`
   - Expected: fallback file is read if present

   ```python
   def test_result_emission_fallback_path(tmp_path, script_path):
       # Create fallback file adjacent to script
       fallback = script_path.parent / f"{script_path.stem}.bth-results.json"
       fallback.write_text('{"n_steps": 1000}')
       
       # Run without BTH_RESULTS_PATH set
       # Expect fallback is used
   ```

4. **Step 4: Implement result emission in `runner.py`**
   - After subprocess exits (line 97), before outcome eval (line 104):
     ```python
     # Read result metrics from $BTH_RESULTS_PATH or fallback
     metadata = {}
     results_path = os.environ.get("BTH_RESULTS_PATH")
     if results_path:
         results_file = Path(results_path)
         if results_file.exists():
             try:
                 metadata = json.loads(results_file.read_text())
             except (json.JSONDecodeError, TypeError):
                 metadata = {}
     else:
         # Fallback: <script-stem>.bth-results.json
         if script_path:
             fallback = script_path.parent / f"{script_path.stem}.bth-results.json"
             if fallback.exists():
                 try:
                    metadata = json.loads(fallback.read_text())
                 except (json.JSONDecodeError, TypeError):
                     metadata = {}
     
     run = dataclasses.replace(run, metadata=json.dumps(metadata))
     ```

5. **Step 5: Run tests**
   - `pytest tests/test_runner.py::test_result_emission_env_var_path -v`
   - Expected: PASS
   - `pytest tests/test_runner.py::test_result_emission_fallback_path -v`
   - Expected: PASS
   - `pytest tests/test_runner.py -v` (all runner tests pass)

6. **Step 6: Commit**
   ```bash
   git add src/bathos/runner.py tests/test_runner.py
   git commit -m "feat(P0): implement result emission pipeline via \$BTH_RESULTS_PATH or fallback"
   ```

**Verification Gate:**
- `pytest tests/test_runner.py -v` — all tests pass
- `pytest tests/test_integration.py -v` — no regressions
- Manual: run a simple Python script with `$BTH_RESULTS_PATH` set; verify metadata is populated after compaction

---

## Phase 2: Schema Migration v3 (P1)

**Dependency:** Phase 1  
**Blocks:** Gate layer, campaigns, outcome tracking  
**Parallelism:** Sequential; single migration task  
**Risk:** Medium — migration chain is established; v3 must add fields without breaking v0/v1/v2

### Task 2.1a: Add v3 Schema Fields to `schema.py` (P1)

**Files:**
- Modify: `src/bathos/schema.py`
- Test: `tests/test_schema.py`

**Changes:**

1. **Step 1: Understand current schema structure**
   - Read `schema.py:33-55` (WARM_SCHEMA)
   - Read `schema.py:60-130` (Run dataclass, to_arrow, from_arrow_row methods)

2. **Step 2: Update `Run` dataclass in `schema.py`**
   - After line 77 (after `outcome: str`), add new fields:
     ```python
     sidecar_sha256: str = ""
     sidecar_path: str = ""
     parent_run_id: str = ""
     agent_mode: str = ""
     sidecar_mode: str = ""
     outcome_is_residual: bool = False
     skill_sha256: str = ""
     campaign_id: str = ""
     ```

3. **Step 3: Update COOL_SCHEMA in `schema.py`**
   - After line 30 (after `outcome` field), add:
     ```python
     pa.field("sidecar_sha256", pa.string()),
     pa.field("sidecar_path", pa.string()),
     pa.field("parent_run_id", pa.string()),
     pa.field("agent_mode", pa.string()),
     pa.field("sidecar_mode", pa.string()),
     pa.field("outcome_is_residual", pa.bool_()),
     pa.field("skill_sha256", pa.string()),
     pa.field("campaign_id", pa.string()),
     ```

4. **Step 4: Update WARM_SCHEMA in `schema.py`**
   - After line 53 (after `output_metadata`), add same 8 fields

5. **Step 5: Update `Run.to_arrow()` method**
   - Add to dict at line 80-98:
     ```python
     "sidecar_sha256": [self.sidecar_sha256],
     "sidecar_path": [self.sidecar_path],
     "parent_run_id": [self.parent_run_id],
     "agent_mode": [self.agent_mode],
     "sidecar_mode": [self.sidecar_mode],
     "outcome_is_residual": [self.outcome_is_residual],
     "skill_sha256": [self.skill_sha256],
     "campaign_id": [self.campaign_id],
     ```

6. **Step 6: Update `Run.from_arrow_row()` method**
   - Add to constructor call at line 108-127:
     ```python
     sidecar_sha256=pydict.get("sidecar_sha256", [""])[i] if "sidecar_sha256" in pydict else "",
     sidecar_path=pydict.get("sidecar_path", [""])[i] if "sidecar_path" in pydict else "",
     parent_run_id=pydict.get("parent_run_id", [""])[i] if "parent_run_id" in pydict else "",
     agent_mode=pydict.get("agent_mode", [""])[i] if "agent_mode" in pydict else "",
     sidecar_mode=pydict.get("sidecar_mode", [""])[i] if "sidecar_mode" in pydict else "",
     outcome_is_residual=bool(pydict.get("outcome_is_residual", [False])[i]) if "outcome_is_residual" in pydict else False,
     skill_sha256=pydict.get("skill_sha256", [""])[i] if "skill_sha256" in pydict else "",
     campaign_id=pydict.get("campaign_id", [""])[i] if "campaign_id" in pydict else "",
     ```

7. **Step 7: Update `CURRENT_SCHEMA_VERSION` in `schema.py`**
   - Line 9: change from `"2"` to `"3"`

8. **Step 8: Run tests**
   - `pytest tests/test_schema.py -v`

9. **Step 9: Commit**
   ```bash
   git add src/bathos/schema.py tests/test_schema.py
   git commit -m "feat(P1): add v3 schema fields to Run dataclass and arrow methods"
   ```

**Verification Gate:**
- `pytest tests/test_schema.py::test_run_dataclass -v` — Run with v3 fields initializes correctly
- `pytest tests/test_schema.py::test_run_to_arrow -v` — to_arrow serialization includes all v3 fields
- `pytest tests/test_schema.py::test_run_from_arrow_row -v` — from_arrow_row deserialization preserves v3 fields

### Task 2.1b: Add Schema v3 DDL and Migration in `compact.py` (P1)

**Files:**
- Modify: `src/bathos/compact.py`
- Test: `tests/test_compact.py`

**Changes:**

1. **Step 1: Harden existing migration version strings**

In `compact.py`, `_migrate_v1()` currently sets `run_dict['schema_version'] = CURRENT_SCHEMA_VERSION` (a dynamic reference). When `CURRENT_SCHEMA_VERSION` is bumped to '3', v1 fragments will be assigned version '3' without passing through `_migrate_v2`, silently skipping the v2→v3 migration.

Fix: Change `_migrate_v1()` to set `run_dict['schema_version'] = '2'` (hardcoded string). Similarly verify `_migrate_v0()` sets `'1'` explicitly. Add test `test_migration_chain_v0_to_v3()` that runs a v0 fragment through the full chain and verifies it ends at schema_version='3' with all expected fields.

2. **Step 2: Understand migration chain**
   - Read `compact.py:67-89` (migration registry and v0→v1→v2 chain)
   - Understand: each migration is a function `_migrate_vX(run_dict: dict) -> dict`

3. **Step 3: Update `_RUNS_TABLE_SCHEMA` DDL in `compact.py`**
   - After line 113 (after `output_metadata TEXT`), add:
     ```python
     sidecar_sha256 TEXT,
     sidecar_path TEXT,
     parent_run_id TEXT,
     agent_mode TEXT,
     sidecar_mode TEXT,
     outcome_is_residual BOOLEAN,
     skill_sha256 TEXT,
     campaign_id TEXT
     ```

3. **Step 2b: Update compact() INSERT statement**

In `compact.py`, find the `compact()` function's INSERT INTO runs statement. It currently has 19 columns. Add all 8 v3 fields to both the column list and the values list:
- Columns: `sidecar_sha256, sidecar_path, parent_run_id, agent_mode, sidecar_mode, outcome_is_residual, skill_sha256, campaign_id`
- Values: `run.sidecar_sha256, run.sidecar_path, run.parent_run_id, run.agent_mode, run.sidecar_mode, run.outcome_is_residual, run.skill_sha256, run.campaign_id`

Add to `tests/test_compact.py`: `test_v3_fields_round_trip_through_compaction()` — write a run with non-empty v3 fields to cool tier, compact, query warm DB, assert all 8 fields match.

4. **Step 4: Add migration v2→v3 in `compact.py`**
   - After line 89, add:
     ```python
     def _migrate_v2(run_dict: dict) -> dict:
         """Migrate v2 fragment (no agentic integrity fields) to v3.
         
         v2 fragments have schema_version='2' but no sidecar_sha256, parent_run_id, etc.
         This migration adds all v3 fields with sensible defaults.
         """
         run_dict["sidecar_sha256"] = ""
         run_dict["sidecar_path"] = ""
         run_dict["parent_run_id"] = ""
         run_dict["agent_mode"] = ""
         run_dict["sidecar_mode"] = ""
         run_dict["outcome_is_residual"] = False
         run_dict["skill_sha256"] = ""
         run_dict["campaign_id"] = ""
         run_dict["schema_version"] = "3"  # Hardcoded to '3' to ensure proper chain
         return run_dict
     
     MIGRATIONS["2"] = _migrate_v2
     ```

5. **Step 5: Write tests for migration chain**
   - Create `tests/test_compact.py::test_migration_v2_to_v3()`:
     ```python
     def test_migration_v2_to_v3(tmp_path):
         catalog_dir = tmp_path / "catalog"
         catalog_dir.mkdir()
         
         # Create v2 fragment
         v2_run = {
             "id": "test-run",
             "project_slug": "test",
             "schema_version": "2",
             # ... other v2 fields
         }
         # Write as Parquet with v2 schema
         
         # Compact
         result = compact(catalog_dir)
         
         # Query warm DB; check new fields exist and have defaults
         rows = run_sql("SELECT sidecar_mode, outcome_is_residual FROM runs WHERE id = 'test-run'", catalog_dir)
         assert rows[0] == ('', False)  # defaults
     ```

   - Create `tests/test_compact.py::test_migration_chain_v0_to_v3()`:
     ```python
     def test_migration_chain_v0_to_v3(tmp_path):
         """Verify v0 fragment passes through full chain (v0→v1→v2→v3)."""
         catalog_dir = tmp_path / "catalog"
         catalog_dir.mkdir()
         
         # Create v0 fragment (oldest version)
         v0_run = {
             "id": "old-run",
             "project_slug": "test",
             "schema_version": "0",
             # ... other v0 fields
         }
         # Write as Parquet
         
         # Compact
         compact(catalog_dir)
         
         # Query warm DB; verify schema_version is '3' and all fields exist
         rows = run_sql("SELECT schema_version, sidecar_sha256, campaign_id FROM runs WHERE id = 'old-run'", catalog_dir)
         assert rows[0][0] == "3"  # Final version must be '3'
         assert rows[0][1] == ""   # v3 fields populated with defaults
         assert rows[0][2] == ""
     ```

6. **Step 6: Run tests**
   - `pytest tests/test_compact.py -v`
   - `pytest tests/test_migrate.py -v`

7. **Step 7: Commit**
   ```bash
   git add src/bathos/compact.py tests/test_compact.py tests/test_migrate.py
   git commit -m "feat(P1): add schema v3 DDL and migration in compact.py"
   ```

**Verification Gate:**
- `pytest tests/test_compact.py::test_migration_v2_to_v3 -v` — Migration chain preserves v2 data and adds v3 defaults
- `pytest tests/test_compact.py::test_migration_chain_v0_to_v3 -v` — Full chain v0→v1→v2→v3 completes with final version='3'
- `pytest tests/test_integration.py -v` — no regressions in catalog/query

---

## Phase 3: Gate Layer (`validate.py` + `prereg.py`)

**Dependency:** Phase 2  
**Blocks:** Campaigns, MCP tools, outcome enforcement  
**Parallelism:** `validate.py` → `prereg.py` (sequential)  
**Risk:** Medium — validation and gating are load-bearing for integrity; thorough tests required

### Task 3.1: Create `validate.py` Module (P1)

**Files:**
- Create: `src/bathos/validate.py` — new module
- Test: `tests/test_validate.py` — comprehensive test cases

**Changes:**

1. **Step 1: Write failing tests** (TDD)
   - Create comprehensive test file `tests/test_validate.py` with these cases:
     ```python
     def test_valid_experiment_sidecar():
         """Well-formed experiment sidecar passes validation."""
         sidecar = parse_sidecar(valid_experiment_path)
         result = validate_sidecar(sidecar)
         assert result.ok is True
     
     def test_missing_experiment_section():
         """Sidecar without [experiment] section fails."""
         # ... toml without [experiment]
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert any("experiment" in err for err in result.errors)
     
     def test_missing_outcomes_section():
         """No [outcomes] section fails."""
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert any("outcomes" in err for err in result.errors)
     
     def test_outcome_missing_condition():
         """Outcome branch without 'condition' field fails."""
         # outcomes.pass: { decision = "..." }  # missing condition
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert "condition" in str(result.errors)
     
     def test_outcome_missing_decision():
         """Outcome branch without 'decision' field fails."""
         # outcomes.pass: { condition = "..." }  # missing decision
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert "decision" in str(result.errors)
     
     def test_outcome_missing_reasoning():
         """Outcome branch without 'reasoning' field fails."""
         # outcomes.pass: { condition = "...", decision = "..." }  # missing reasoning
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert "reasoning" in str(result.errors)
     
     def test_condition_duckdb_parse_error():
         """Invalid DuckDB SQL in condition fails."""
         # outcomes.pass: { condition = "SELECT * FROM", ... }
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert "DuckDB" in str(result.errors)
     
     def test_no_result_schema_referenced():
         """If no result_schema fields appear in any condition, fail."""
         # result_schema: { temp_mean = "float" }
         # outcomes.pass: { condition = "1 = 1", ... }  # doesn't reference temp_mean
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert "result_schema" in str(result.errors)
     
     def test_no_residual_fallback():
         """If no outcome branch has is_residual=true, fail."""
         # outcomes: { pass: {...}, fail: {...} }  # neither has is_residual
         result = validate_sidecar(sidecar)
         assert result.ok is False
         assert "residual" in str(result.errors)
     
     def test_reasoning_must_cite_constant_or_reference():
         """Bare narrative reasoning without constant/reference rejected."""
         # outcomes.pass: { reasoning = "This is what we expect" }
         # Should reject unless constant or reference present
         result = validate_sidecar(sidecar)
         # Tier 3 (quality signal, not hard block) — but test infrastructure
     
     def test_valid_with_residual_fallback():
         """Valid sidecar with is_residual=true fallback passes."""
         # outcomes: { pass: {...}, fail: {...}, residual: {condition="1=1", is_residual=true, ...} }
         result = validate_sidecar(sidecar)
         assert result.ok is True
     ```

2. **Step 2: Write `validate.py` module**
   - Create dataclasses for validation result:
     ```python
     @dataclass
     class ValidationError:
         field: str
         message: str
     
     @dataclass
     class ValidationResult:
         ok: bool
         errors: list[ValidationError] = field(default_factory=list)
     ```

   - Main function `validate_sidecar(sidecar: Sidecar) -> ValidationResult`:
     ```python
     def validate_sidecar(sidecar: Sidecar) -> ValidationResult:
         errors = []
         
         # Check required outcomes section
         if not sidecar.outcomes:
             errors.append(ValidationError("outcomes", "No [outcomes] section"))
         
         # Check each outcome branch
         for label, spec in (sidecar.outcomes or {}).items():
             if not spec.condition:
                 errors.append(ValidationError(f"outcomes.{label}", "Missing 'condition'"))
             if not spec.decision:
                 errors.append(ValidationError(f"outcomes.{label}", "Missing 'decision'"))
             # TODO: check reasoning (needs field added to OutcomeSpec)
             
             # Validate DuckDB SQL
             if spec.condition:
                 try:
                     duckdb.execute(f"SELECT ({spec.condition})")
                 except Exception as e:
                     errors.append(ValidationError(f"outcomes.{label}.condition", f"DuckDB parse error: {e}"))
         
         # Check result_schema referenced
         if sidecar.outcomes and sidecar.result_schema:
             schema_keys = set(sidecar.result_schema.keys())
             conditions_text = " ".join(s.condition for s in sidecar.outcomes.values())
             if not any(key in conditions_text for key in schema_keys):
                 errors.append(ValidationError("result_schema", "No schema fields referenced in conditions"))
         
         # Check is_residual fallback
         has_residual = any(getattr(spec, 'is_residual', False) for spec in sidecar.outcomes.values())
         if sidecar.outcomes and not has_residual:
             errors.append(ValidationError("outcomes", "No fallback branch with is_residual=true"))
         
         return ValidationResult(ok=len(errors) == 0, errors=errors)
     ```

3. **Step 3: Update `OutcomeSpec` dataclass in `sidecar.py`**
   - Add fields:
     ```python
     reasoning: str = ""
     is_residual: bool = False
     ```

4. **Step 4: Update `parse_sidecar()` in `sidecar.py` to extract reasoning + is_residual**
   - Modify `_parse_outcomes()` to read these fields:
     ```python
     def _parse_outcomes(data: dict) -> dict[str, OutcomeSpec]:
         outcomes_data = data.get("outcomes", {})
         return {
             label: OutcomeSpec(
                 condition=spec.get("condition", ""),
                 decision=spec.get("decision", ""),
                 reasoning=spec.get("reasoning", ""),
                 is_residual=spec.get("is_residual", False),
             )
             for label, spec in outcomes_data.items()
         }
     ```

5. **Step 5: Run tests**
   - `pytest tests/test_validate.py -v`
   - `pytest tests/test_sidecar.py -v` (ensure sidecar changes don't break existing tests)

6. **Step 6: Commit**
   ```bash
   git add src/bathos/validate.py src/bathos/sidecar.py tests/test_validate.py tests/test_sidecar.py
   git commit -m "feat(P1): add sidecar structural validation module"
   ```

**Verification Gate:**
- `pytest tests/test_validate.py -v` — all validation tests pass
- Spot check: sample valid/invalid sidecar TOML files validate correctly

### Task 3.2: Create `prereg.py` Module (P1)

**Files:**
- Create: `src/bathos/prereg.py` — new module
- Test: `tests/test_prereg.py`

**Changes:**

1. **Step 1: Write failing tests** (TDD)
   - Create `tests/test_prereg.py`:
     ```python
     def test_resolve_sidecar_found():
         """If sidecar exists, resolve returns bundle with SHA."""
         sidecar_path = script_dir / "test.bth.toml"
         sidecar_path.write_text("[experiment]\nhypothesis=\"...\"\n...")
         
         bundle = resolve_sidecar(script_path)
         assert bundle.path == sidecar_path.resolve()
         assert len(bundle.sha256) == 64  # SHA256 hex
         assert bundle.found is True
     
     def test_resolve_sidecar_not_found():
         """If sidecar missing, resolve returns bundle with found=False."""
         bundle = resolve_sidecar(script_without_sidecar)
         assert bundle.found is False
         assert bundle.path is None
     
     def test_resolve_agent_mode_cli_takes_precedence():
         """CLI flag --agent-mode overrides sidecar and config."""
         mode = resolve_agent_mode(
             cli_flag="autonomous",
             sidecar_mode="collaborative",
             project_config_mode="collaborative",
             global_config_mode="collaborative"
         )
         assert mode == "autonomous"
     
     def test_resolve_agent_mode_sidecar_fallback():
         """If no CLI flag, sidecar agent_mode takes precedence."""
         mode = resolve_agent_mode(
             cli_flag=None,
             sidecar_mode="autonomous",
             project_config_mode="collaborative",
             global_config_mode="collaborative"
         )
         assert mode == "autonomous"
     
     def test_gate_check_missing_sidecar_collaborative():
         """Collaborative mode: missing sidecar returns GateFailure (not exception)."""
         result = gate_check(script_path, mode="collaborative")
         assert result.ok is False
         assert result.gate == "sidecar_missing"
         assert "Create <path>.bth.toml" in result.remediation
     
     def test_gate_check_invalid_sidecar():
         """Invalid sidecar returns GateFailure with validation errors."""
         # sidecar missing [outcomes]
         result = gate_check(script_path, mode="collaborative")
         assert result.ok is False
         assert result.gate == "sidecar_invalid"
         assert len(result.errors) > 0
     
     def test_check_first_of_kind_no_prior_runs():
         """Script with no prior runs is first-of-kind."""
         assert check_first_of_kind(script_path, catalog_dir, "abc123") is True
     
     def test_check_first_of_kind_prior_run_same_hash():
         """Script with prior run at same path and same HEAD hash is NOT first-of-kind."""
         # Create prior run in catalog with (script_path, git_hash='abc123')
         # Query with current_git_hash='abc123' should return False
         assert check_first_of_kind(script_path, catalog_dir, "abc123") is False
     
     def test_check_first_of_kind_prior_run_different_hash():
         """Script with prior run at same path but different HEAD hash IS first-of-kind."""
         # Create prior run in catalog with (script_path, git_hash='old_hash')
         # Query with current_git_hash='new_hash' should return True
         # Different commit = different repository state = first-of-kind at this commit
         assert check_first_of_kind(script_path, catalog_dir, "new_hash") is True
     ```

2. **Step 2: Define data structures**
   - Create dataclasses:
     ```python
     @dataclass
     class SidecarBundle:
         found: bool
         path: Path | None = None
         sha256: str = ""  # SHA256 hex of sidecar_bytes
     
     @dataclass
     class GateFailure:
         ok: bool = False
         gate: str = ""  # "sidecar_missing" | "sidecar_invalid" | "not_first_of_kind" | "agent_mode_mismatch"
         errors: list[str] = field(default_factory=list)
         required_format: dict | None = None
         agent_mode: str = ""
         remediation: str = ""
         gate_schema_version: int = 1
     
     @dataclass
     class GateResult:
         ok: bool
         failure: GateFailure | None = None
     ```

3. **Step 3: Implement `resolve_sidecar(script_path: Path) -> SidecarBundle`**
   ```python
   def resolve_sidecar(script_path: Path) -> SidecarBundle:
       """Find and hash sidecar adjacent to script."""
       sidecar_path = find_sidecar(script_path)
       if not sidecar_path:
           return SidecarBundle(found=False)
       
       try:
           sha256_hash = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()
           return SidecarBundle(
               found=True,
               path=sidecar_path.resolve(),
               sha256=sha256_hash
           )
       except Exception as e:
           return SidecarBundle(found=False)  # Treat read error as "not found"
   ```

4. **Step 4: Implement `resolve_agent_mode()` — priority order**
   ```python
   def resolve_agent_mode(
       cli_flag: str | None,
       sidecar_mode: str | None,
       project_config_mode: str | None,
       global_config_mode: str | None
   ) -> str:
       """Priority: CLI → sidecar → project config → global config → default."""
       if cli_flag:
           return cli_flag
       if sidecar_mode:
           return sidecar_mode
       if project_config_mode:
           return project_config_mode
       if global_config_mode:
           return global_config_mode
       return "collaborative"  # default
   ```

5. **Step 5: Implement `gate_check(script_path: Path, mode: str) -> GateResult`**
   ```python
   def gate_check(script_path: Path, mode: str) -> GateResult:
       """Gate enforcement: collaborative or autonomous mode."""
       if mode not in ("collaborative", "autonomous"):
           return GateResult(ok=False, failure=GateFailure(
               gate="mode_invalid",
               errors=[f"mode must be 'collaborative' or 'autonomous', got '{mode}'"]
           ))
       
       # Check sidecar presence
       bundle = resolve_sidecar(script_path)
       if not bundle.found:
           return GateResult(ok=False, failure=GateFailure(
               gate="sidecar_missing",
               errors=[f"No {script_path.stem}.bth.toml found"],
               remediation=f"Create {script_path.stem}.bth.toml next to the script"
           ))
       
       # Parse and validate
       try:
           sidecar = parse_sidecar(bundle.path)
       except SidecarError as e:
           return GateResult(ok=False, failure=GateFailure(
               gate="sidecar_invalid",
               errors=[str(e)]
           ))
       
       # Structural validation
       validation = validate_sidecar(sidecar)
       if not validation.ok:
           return GateResult(ok=False, failure=GateFailure(
               gate="sidecar_invalid",
               errors=[f"{err.field}: {err.message}" for err in validation.errors]
           ))
       
       return GateResult(ok=True)
   ```

6. **Step 6: Implement `check_first_of_kind()` — Q5 resolution (use repository HEAD hash)**
   
   **Q5 RESOLUTION:** "first-of-kind" means no prior run in the warm/cool catalog shares the same `(script_path_normalized, git_hash)` where `git_hash` is the current repository HEAD hash (from `capture_git_state(cwd).hash`, the same value `runner.py` writes to the `runs` table column `git_hash`). This ensures consistency: file-level commit hashes are irrelevant; only the repository state at run invocation matters.
   
   ```python
   def check_first_of_kind(script_path: Path, catalog_dir: Path, current_git_hash: str) -> bool:
       """Return True if no prior runs exist with same (script_path, git_hash).
       
       Args:
           script_path: Path to the script being run
           catalog_dir: Path to the catalog directory
           current_git_hash: Current HEAD hash from capture_git_state(cwd).hash
       
       Returns:
           True if this is the first run with this script path at this git commit.
       """
       script_path_normalized = str(script_path.resolve())
       
       # Query catalog for prior runs with same script path + current git hash
       from bathos.query import run_sql
       try:
           rows = run_sql(
               "SELECT COUNT(*) FROM runs WHERE command LIKE ? AND git_hash = ?",
               catalog_dir,
               [f"%{script_path_normalized}%", current_git_hash]
           )
           return rows[0][0] == 0
       except Exception:
           # Catalog doesn't exist or query failed
           return True  # Assume first-of-kind if can't query
   ```

7. **Step 7: Run tests**
   - `pytest tests/test_prereg.py -v`

8. **Step 8: Commit**
   ```bash
   git add src/bathos/prereg.py tests/test_prereg.py
   git commit -m "feat(P1): add pre-registration resolution and gating module"
   ```

**Verification Gate:**
- `pytest tests/test_prereg.py -v` — all tests pass
- Spot check: gate_check() correctly identifies valid/invalid sidecars

### Task 3.2b: Add `agent_mode` Field to Sidecar Dataclass (P1)

**Files:**
- Modify: `src/bathos/sidecar.py` — add field to `Sidecar` dataclass and `OutcomeSpec`
- Test: `tests/test_sidecar.py`

**Changes:**

1. **Step 1: Add `agent_mode` to `OutcomeSpec` dataclass**
   ```python
   @dataclass
   class OutcomeSpec:
       condition: str = ""
       decision: str = ""
       reasoning: str = ""
       is_residual: bool = False
   ```
   No change needed here; `agent_mode` goes on `Sidecar`, not `OutcomeSpec`.

2. **Step 2: Add `agent_mode` to `Sidecar` dataclass**
   ```python
   @dataclass
   class Sidecar:
       kind: str = ""
       hypothesis: str = ""
       outcomes: dict[str, OutcomeSpec] = field(default_factory=dict)
       result_schema: dict[str, str] = field(default_factory=dict)
       agent_mode: str = ""  # NEW: empty string means not specified
   ```

3. **Step 3: Update `parse_sidecar()` to extract `agent_mode`**
   - For experiment kind: read `data['experiment'].get('agent_mode', '')` and set `sidecar.agent_mode`
   - For benchmark and other kinds: read `data[kind].get('agent_mode', '')` similarly
   ```python
   def parse_sidecar(sidecar_path: Path) -> Sidecar:
       data = tomllib.loads(sidecar_path.read_text())
       kind = next(k for k in ["experiment", "benchmark", "debug"] if k in data)
       
       return Sidecar(
           kind=kind,
           hypothesis=data[kind].get("hypothesis", ""),
           outcomes=_parse_outcomes(data),
           result_schema=data.get("result_schema", {}),
           agent_mode=data[kind].get("agent_mode", ""),  # NEW
       )
   ```

4. **Step 4: Write test**
   ```python
   def test_sidecar_agent_mode_parsed():
       """Sidecar TOML with agent_mode is parsed correctly."""
       sidecar_toml = """
       [experiment]
       hypothesis = "Test hypothesis"
       agent_mode = "autonomous"
       
       [outcomes.pass]
       condition = "1 = 1"
       decision = "OK"
       reasoning = "Always true"
       
       [result_schema]
       x = "float"
       """
       sidecar = parse_sidecar_from_string(sidecar_toml)
       assert sidecar.agent_mode == "autonomous"
   
   def test_sidecar_agent_mode_default():
       """Sidecar without agent_mode defaults to empty string."""
       sidecar_toml = """
       [experiment]
       hypothesis = "Test"
       
       [outcomes.pass]
       condition = "1 = 1"
       decision = "OK"
       reasoning = "Always true"
       
       [result_schema]
       x = "float"
       """
       sidecar = parse_sidecar_from_string(sidecar_toml)
       assert sidecar.agent_mode == ""
   ```

5. **Step 5: Run tests**
   - `pytest tests/test_sidecar.py::test_sidecar_agent_mode_parsed -v`
   - `pytest tests/test_sidecar.py::test_sidecar_agent_mode_default -v`
   - `pytest tests/test_sidecar.py -v` (all sidecar tests pass)

6. **Step 6: Commit**
   ```bash
   git add src/bathos/sidecar.py tests/test_sidecar.py
   git commit -m "feat(P1): add agent_mode field to Sidecar dataclass and parse_sidecar()"
   ```

**Verification Gate:**
- `pytest tests/test_sidecar.py -v` — agent_mode parsing tests pass

### Task 3.3: Wire Gate + Outcome Eval into `runner.py` (P1)

**Files:**
- Modify: `src/bathos/runner.py`
- Modify: `src/bathos/cli.py` — add `--derived-from` and `--agent-mode` flags
- Test: `tests/test_runner.py`, `tests/test_integration.py`

**Changes:**

1. **Step 1: Update `run_script()` signature in `runner.py`**
   ```python
   def run_script(
       argv: list[str],
       project_slug: str,
       catalog_dir: Path,
       output_paths: list[str],
       tags: list[str],
       cwd: Path = Path.cwd(),
       agent_mode: str | None = None,
       no_sidecar: bool = False,
       derived_from: str | None = None,
       campaign_id: str | None = None,
   ) -> int:
   ```

2. **Step 2: Add gating logic after line 62 (after sidecar parse)**
   ```python
   # Resolve agent mode: CLI → sidecar → project config → global config → default
   sidecar_agent_mode = sidecar.agent_mode if sidecar else ""
   
   # Read project config (from .bth.toml in project root)
   project_config = {}
   try:
       bth_config_path = Path.cwd() / ".bth.toml"
       if bth_config_path.exists():
           project_config = tomllib.loads(bth_config_path.read_text()).get("defaults", {})
   except Exception:
       pass
   project_config_mode = project_config.get("agent_mode", "")
   
   # Read global config (from ~/.bth/config.toml)
   global_config_mode = ""
   try:
       global_config_path = Path.home() / ".bth" / "config.toml"
       if global_config_path.exists():
           global_config = tomllib.loads(global_config_path.read_text()).get("defaults", {})
           global_config_mode = global_config.get("agent_mode", "collaborative")
   except Exception:
       global_config_mode = "collaborative"
   
   resolved_mode = resolve_agent_mode(
       cli_flag=agent_mode,
       sidecar_mode=sidecar_agent_mode,
       project_config_mode=project_config_mode,
       global_config_mode=global_config_mode,
   )
   
   # Gate check
   if script_path and is_in_enforced_dir(script_path) and not no_sidecar:
       gate_result = gate_check(script_path, resolved_mode)
       if not gate_result.ok:
           # Return structured gate failure (not exception)
           typer.echo(json.dumps(asdict(gate_result.failure)), err=True)
           return 1
   
   sidecar_mode = "bypassed" if no_sidecar else ("generated" if resolved_mode == "autonomous" else "declared")
   ```

3. **Step 3: Populate new Run fields**
   ```python
   run = Run(
       # ... existing fields
       sidecar_sha256=bundle.sha256 if bundle else "",
       sidecar_path=str(bundle.path) if bundle else "",
       parent_run_id=derived_from or "",
       agent_mode=resolved_mode or "",
       sidecar_mode=sidecar_mode,
       campaign_id=campaign_id or "",
   )
   ```

4. **Step 4: Populate `outcome_is_residual` after outcome eval**
   ```python
   outcome_is_residual = False
   if sidecar and outcome and outcome != "unknown":
       spec = sidecar.outcomes.get(outcome)
       if spec:
           outcome_is_residual = getattr(spec, 'is_residual', False)
   
   run = dataclasses.replace(
       run,
       # ... existing fields
       outcome_is_residual=outcome_is_residual,
   )
   ```

5. **Step 5: Update CLI in `cli.py`**
   ```python
   @app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
   def run(
       argv: list[str] = typer.Argument(...),
       out: list[str] = typer.Option([], "--out", help="Output path to register"),
       tag: list[str] = typer.Option([], "--tag", "-t"),
       agent_mode: str | None = typer.Option(None, "--agent-mode", help="collaborative|autonomous"),
       no_sidecar: bool = typer.Option(False, "--no-sidecar", help="Bypass sidecar enforcement"),
       derived_from: str | None = typer.Option(None, "--derived-from", help="Parent run ID for lineage"),
       campaign: str | None = typer.Option(None, "--campaign", help="Campaign ID"),
   ):
       # ... pass new args to run_script()
   ```

6. **Step 6: Write tests**
   - Test gate fires on missing sidecar in enforced dir
   - Test gate passes with valid sidecar
   - Test --no-sidecar bypasses gate
   - Test outcome_is_residual populated correctly

7. **Step 7: Run tests**
   - `pytest tests/test_runner.py -v`
   - `pytest tests/test_integration.py -v`

8. **Step 8: Commit**
   ```bash
   git add src/bathos/runner.py src/bathos/cli.py tests/test_runner.py tests/test_integration.py
   git commit -m "feat(P1): wire sidecar gate + outcome evaluation into runner.py"
   ```

**Verification Gate:**
- `pytest tests/test_runner.py -v` — gate logic tested
- Manual: `bth run python scripts/experiments/test.py` (missing sidecar) → error + remediation
- Manual: `bth run --no-sidecar python scripts/experiments/test.py` → runs (sidecar_mode=bypassed)

---

## Phase 4: Query and Display (P1)

**Dependency:** Phase 3  
**Blocks:** Campaigns, sprint audit  
**Parallelism:** `bth ls` → `lineage` (independent)

### Task 4.1: Update `bth ls` to Show `sidecar_mode` and Add Filters (P1)

**Files:**
- Modify: `src/bathos/cli.py:91-124`
- Modify: `src/bathos/query.py` — add filters
- Test: `tests/test_cli.py`, `tests/test_query.py`

**Changes:**

1. **Step 1: Update `bth ls` command in `cli.py`**
   ```python
   @app.command("ls")
   def ls_cmd(
       project: str | None = typer.Option(None, "--project", "-p"),
       since: str | None = typer.Option(None, "--since", help="e.g. 7d, 24h"),
       status: str | None = typer.Option(None, "--status"),
       outcome: str | None = typer.Option(None, "--outcome", help="Filter by outcome label"),
       sidecar_mode: str | None = typer.Option(None, "--sidecar-mode", help="declared|generated|bypassed"),
       limit: int = typer.Option(20, "--limit", "-n"),
   ):
       # ... call find_runs with new filters
   ```

2. **Step 2: Update `find_runs()` in `query.py`**
   ```python
   def find_runs(
       catalog_dir: Path,
       since: datetime | None = None,
       project: str | None = None,
       status: str | None = None,
       outcome: str | None = None,  # new
       sidecar_mode: str | None = None,  # new
       tags: list[str] | None = None,
       slurm_job_id: str | None = None,
   ) -> list[Run]:
       # ... build WHERE clause with new filters
   ```

3. **Step 3: Update display header**
   ```python
   header = f"{'ID':38} {'PROJECT':12} {'STATUS':10} {'SIDECAR':10} {'OUTCOME':10} {'DURATION':8} COMMAND"
   # ... add sidecar_mode column to output
   ```

4. **Step 4: Write tests**
   - Test `--outcome pass` filters correctly
   - Test `--sidecar-mode generated` filters correctly
   - Test both filters work together

5. **Step 5: Commit**
   ```bash
   git add src/bathos/cli.py src/bathos/query.py tests/test_cli.py tests/test_query.py
   git commit -m "feat(P1): add outcome and sidecar_mode filters to bth ls"
   ```

### Task 4.2: Add `bth lineage` Command (P1)

**Files:**
- Modify: `src/bathos/query.py` — add `lineage()` function
- Modify: `src/bathos/cli.py` — add `lineage` command
- Test: `tests/test_query.py`, `tests/test_cli.py`

**Changes:**

1. **Step 1: Implement `lineage()` in `query.py`**
   ```python
   def lineage(run_id: str, catalog_dir: Path) -> list[Run]:
       """Return ancestor chain of run_id following parent_run_id links."""
       db = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
       
       query = """
       WITH RECURSIVE ancestors AS (
           SELECT id, parent_run_id, outcome, timestamp
           FROM runs
           WHERE id = ?
           
           UNION ALL
           
           SELECT r.id, r.parent_run_id, r.outcome, r.timestamp
           FROM runs r
           INNER JOIN ancestors a ON r.id = a.parent_run_id
       )
       SELECT * FROM ancestors ORDER BY timestamp
       """
       
       rows = db.execute(query, [run_id]).fetchall()
       # Use _row_to_run() from query.py — the existing DuckDB row deserializer
       from bathos.query import _row_to_run
       return [_row_to_run(row) for row in rows]
   ```

2. **Step 2: Add `bth lineage` CLI command**
   ```python
   @app.command()
   def lineage(run_id: str = typer.Argument(...)):
       """Show ancestor chain of a run."""
       from bathos.query import lineage as get_lineage
       
       ancestors = get_lineage(run_id, _catalog_dir())
       if not ancestors:
           typer.echo(f"Run not found: {run_id}", err=True)
           raise typer.Exit(1)
       
       typer.echo(f"Lineage for {run_id}:")
       for r in ancestors:
           typer.echo(f"  {r.id[:8]} {r.timestamp.isoformat()} outcome={r.outcome}")
   ```

3. **Step 3: Write tests**
   - Test lineage with 3-run ancestor chain
   - Test lineage with single run (no parent)

4. **Step 4: Commit**
   ```bash
   git add src/bathos/query.py src/bathos/cli.py tests/test_query.py tests/test_cli.py
   git commit -m "feat(P1): add bth lineage recursive CTE for run ancestry"
   ```

---

## Phase 5: Campaigns (P2)

**Dependency:** Phase 4  
**Blocks:** Sprint audit  
**Parallelism:** DDL/CRUD can be developed in parallel; CLI must sequence after modules

### Task 5.1: Create `campaigns.py` Module with DDL and CRUD (P2)

**Files:**
- Create: `src/bathos/campaigns.py` — new module
- Modify: `src/bathos/compact.py` — add campaign DDL, populate `campaign_runs` at compaction
- Test: `tests/test_campaigns.py`

**Changes:**

1. **Step 1: Add campaign DDL to `compact.py`**
   - After `_RUNS_TABLE_SCHEMA` (line 113), add:
     ```python
     _CAMPAIGNS_TABLE_SCHEMA = """
     CREATE TABLE IF NOT EXISTS campaigns (
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
         parent_campaign_id TEXT
     )
     """
     
     _CAMPAIGN_RUNS_TABLE_SCHEMA = """
     CREATE TABLE IF NOT EXISTS campaign_runs (
         campaign_id TEXT NOT NULL,
         run_id TEXT NOT NULL,
         PRIMARY KEY (campaign_id, run_id)
     )
     """
     
     _AMENDMENTS_TABLE_SCHEMA = """
     CREATE TABLE IF NOT EXISTS amendments (
         run_id TEXT NOT NULL,
         amended_at TEXT NOT NULL,
         old_sidecar_sha256 TEXT,
         new_sidecar_sha256 TEXT,
         reason TEXT NOT NULL
     )
     """
     ```

2. **Step 2: Create `campaigns.py` module with CRUD functions**
   ```python
   @dataclass
   class Campaign:
       id: str
       project_slug: str
       name: str
       mode: str  # "exploration" | "confirmation"
       question: str | None = None
       hypothesis: str | None = None
       status: str = "open"  # "open" | "concluded"
       started_at: str = ""
       concluded_at: str | None = None
       conclusion: str | None = None
       outcome_label: str | None = None
       parent_campaign_id: str | None = None
   
   def create_campaign(
       db: duckdb.DuckDBPyConnection,
       name: str,
       project_slug: str,
       mode: str,
       question: str | None = None,
       hypothesis: str | None = None,
       parent_campaign_id: str | None = None,
   ) -> Campaign:
       """Create a new campaign."""
       campaign_id = str(uuid4())
       started_at = datetime.now(UTC).isoformat()
       
       db.execute("""
           INSERT INTO campaigns (id, project_slug, name, mode, question, hypothesis, status, started_at, parent_campaign_id)
           VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?)
       """, [campaign_id, project_slug, name, mode, question, hypothesis, started_at, parent_campaign_id])
       
       return Campaign(
           id=campaign_id,
           project_slug=project_slug,
           name=name,
           mode=mode,
           question=question,
           hypothesis=hypothesis,
           status="open",
           started_at=started_at,
           parent_campaign_id=parent_campaign_id,
       )
   
   def add_run_to_campaign(db, campaign_id: str, run_id: str) -> None:
       """Add run to campaign (idempotent).
       
       Raises CampaignError if mode is 'confirmation' and run timestamp precedes campaign creation.
       """
       # Fetch campaign and run
       campaign_row = db.execute(
           "SELECT mode, started_at FROM campaigns WHERE id = ?", [campaign_id]
       ).fetchall()
       if not campaign_row:
           raise CampaignError(f"Campaign not found: {campaign_id}")
       
       campaign_mode, campaign_started_at = campaign_row[0]
       
       run_row = db.execute(
           "SELECT timestamp FROM runs WHERE id = ?", [run_id]
       ).fetchall()
       if not run_row:
           raise CampaignError(f"Run not found: {run_id}")
       
       run_timestamp = run_row[0]
       
       # Enforce temporal ordering for confirmation campaigns
       if campaign_mode == "confirmation":
           if run_timestamp < campaign_started_at:
               raise CampaignError(
                   f"Cannot add run {run_id} to confirmation campaign {campaign_id} — "
                   f"run timestamp ({run_timestamp}) predates campaign creation ({campaign_started_at})"
               )
       
       # Insert (idempotent)
       db.execute("""
           INSERT INTO campaign_runs (campaign_id, run_id)
           VALUES (?, ?)
           ON CONFLICT DO NOTHING
       """, [campaign_id, run_id])
   
   def conclude_campaign(
       db, campaign_id: str, outcome_label: str, conclusion: str
   ) -> None:
       """Mark campaign as concluded."""
       concluded_at = datetime.now(UTC).isoformat()
       db.execute("""
           UPDATE campaigns
           SET status = 'concluded', concluded_at = ?, outcome_label = ?, conclusion = ?
           WHERE id = ?
       """, [concluded_at, outcome_label, conclusion, campaign_id])
   
   def get_campaign(db, campaign_id: str) -> Campaign | None:
       """Fetch campaign by ID."""
       rows = db.execute("SELECT * FROM campaigns WHERE id = ?", [campaign_id]).fetchall()
       if not rows:
           return None
       # ... construct Campaign from row
   ```

3. **Step 3: Update `compact.py` to populate `campaign_runs`**
   - During compaction, after inserting runs into warm DB, insert into campaign_runs:
     ```python
     # In compact() function, after populating runs table:
     cool_runs = read_runs(catalog_dir)
     for run in cool_runs:
         if run.campaign_id:
             db.execute("""
                 INSERT INTO campaign_runs (campaign_id, run_id)
                 VALUES (?, ?)
                 ON CONFLICT DO NOTHING
             """, [run.campaign_id, run.id])
     ```

4. **Step 4: Write tests**
   - Test create_campaign() creates row in DuckDB
   - Test add_run_to_campaign() idempotent
   - Test conclude_campaign() updates status
   - **Test temporal ordering:** `test_confirmation_campaign_rejects_prior_run()` — create confirmation campaign at T1, create run with timestamp T0 < T1, try to add → raises CampaignError
   - **Test temporal ordering:** `test_exploration_campaign_allows_prior_run()` — exploration campaign permits any timestamp
   - **Test compaction idempotency:** `test_compaction_respects_campaign_temporal_check()` — when compacting, idempotent campaign_runs insertion should skip or warn if run predates confirmation campaign (use same temporal check or log warning)

5. **Step 5: Commit**
   ```bash
   git add src/bathos/campaigns.py src/bathos/compact.py tests/test_campaigns.py tests/test_compact.py
   git commit -m "feat(P2): add campaigns DDL and CRUD operations"
   ```

### Task 5.2: Implement Campaign CRUD CLI Commands (P2)

**Files:**
- Modify: `src/bathos/cli.py` — add `campaign create`, `campaign add`, `campaign conclude`
- Modify: `src/bathos/runner.py` — populate `campaign_id` at run time
- Test: `tests/test_cli.py`

**Changes:**

1. **Step 1: Create campaign subcommand group and CRUD commands in `cli.py`**
   ```python
   campaign_app = typer.Typer(help="Manage campaigns")
   app.add_typer(campaign_app, name="campaign")
   
   @campaign_app.command("create")
   def campaign_create(
       name: str = typer.Argument(...),
       mode: str = typer.Option("exploration", "--mode"),
       question: str | None = typer.Option(None, "--question"),
       hypothesis: str | None = typer.Option(None, "--hypothesis"),
       parent: str | None = typer.Option(None, "--parent"),
   ):
       """Create a new campaign."""
       from bathos.campaigns import create_campaign
       
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
       campaign = create_campaign(
           db,
           name=name,
           project_slug=_require_project_slug(),
           mode=mode,
           question=question,
           hypothesis=hypothesis,
           parent_campaign_id=parent,
       )
       typer.echo(f"Created campaign {campaign.id}")
   
   @campaign_app.command("add")
   def campaign_add(
       run_id: str = typer.Argument(...),
       campaign: str = typer.Option(..., "--campaign"),
   ):
       """Add an existing run to a campaign."""
       from bathos.campaigns import add_run_to_campaign
       
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
       add_run_to_campaign(db, campaign, run_id)
       typer.echo(f"Added run {run_id} to campaign {campaign}")
   
   @campaign_app.command("conclude")
   def campaign_conclude(
       name: str = typer.Argument(...),
       outcome: str = typer.Option(..., "--outcome"),
       note: str = typer.Option("", "--note"),
   ):
       """Conclude a campaign with an outcome label."""
       from bathos.campaigns import conclude_campaign
       
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
       conclude_campaign(db, name, outcome, note)
       typer.echo(f"Concluded campaign {name}")
   ```

2. **Step 2: Update `runner.py` to write `campaign_id` to cool fragment**
   - Already done in Task 3.3 (passed as argument)
   - Ensure campaign_id is written to Parquet

3. **Step 3: Write tests**
   - Test `bth campaign create`
   - Test `bth campaign add`
   - Test `bth campaign conclude`

4. **Step 4: Commit**
   ```bash
   git add src/bathos/cli.py src/bathos/runner.py tests/test_cli.py
   git commit -m "feat(P2): add campaign CRUD CLI subcommands (create/add/conclude)"
   ```

**Verification Gate:**
- `pytest tests/test_cli.py::test_campaign_create -v`
- `pytest tests/test_cli.py::test_campaign_add -v`
- `pytest tests/test_cli.py::test_campaign_conclude -v`

### Task 5.3: Add Campaign Query and Review CLI Commands (P2)

**Files:**
- Modify: `src/bathos/cli.py` — add `campaign ls`, `campaign show`, `campaign review`
- Test: `tests/test_cli.py`

**Changes:**

1. **Step 1: Add query and review commands in `cli.py`**
   ```python
   @campaign_app.command("ls")
   def campaign_ls(
       status: str | None = typer.Option(None, "--status"),
   ):
       """List campaigns."""
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
       
       query = "SELECT id, name, mode, status FROM campaigns"
       params = []
       if status:
           query += " WHERE status = ?"
           params.append(status)
       
       rows = db.execute(query, params).fetchall()
       for campaign_id, name, mode, campaign_status in rows:
           typer.echo(f"{campaign_id[:8]} {name:30} {mode:12} {campaign_status}")
       
       db.close()
   
   @campaign_app.command("show")
   def campaign_show(
       name: str = typer.Argument(...),
   ):
       """Show details of a campaign."""
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"), read_only=True)
       
       row = db.execute(
           "SELECT * FROM campaigns WHERE id = ? OR name = ?", [name, name]
       ).fetchall()
       
       if not row:
           typer.echo(f"Campaign not found: {name}", err=True)
           raise typer.Exit(1)
       
       # Display campaign details
       campaign = row[0]
       typer.echo(f"Campaign: {campaign[2]} ({campaign[0]})")
       typer.echo(f"Mode: {campaign[3]}")
       typer.echo(f"Status: {campaign[6]}")
       if campaign[4]:  # question
           typer.echo(f"Question: {campaign[4]}")
       if campaign[5]:  # hypothesis
           typer.echo(f"Hypothesis: {campaign[5]}")
       
       db.close()
   
   @campaign_app.command("review")
   def campaign_review(
       name: str = typer.Argument(...),
   ):
       """Review campaign: residual rate, bypass rate, outcome distribution."""
       from bathos.campaigns import review_campaign
       
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
       review = review_campaign(db, name)
       
       if "error" in review:
           typer.echo(review["error"], err=True)
           raise typer.Exit(1)
       
       typer.echo(f"Campaign {name}:")
       typer.echo(f"  Runs: {review['total_runs']}")
       typer.echo(f"  Residual rate: {review['residual_rate']:.1%}")
       typer.echo(f"  Unknown rate: {review['unknown_rate']:.1%}")
       typer.echo(f"  Bypass rate: {review['bypass_rate']:.1%}")
       for anomaly in review["anomalies"]:
           if anomaly:
               typer.echo(f"  ⚠️  {anomaly}")
       
       db.close()
   ```

2. **Step 2: Write tests**
   - Test `bth campaign ls` lists created campaigns
   - Test `bth campaign show` displays campaign details
   - Test `bth campaign review` computes metrics and anomalies

3. **Step 3: Commit**
   ```bash
   git add src/bathos/cli.py tests/test_cli.py
   git commit -m "feat(P2): add campaign query and review CLI subcommands (ls/show/review)"
   ```

**Verification Gate:**
- `pytest tests/test_cli.py::test_campaign_ls -v`
- `pytest tests/test_cli.py::test_campaign_show -v`
- `pytest tests/test_cli.py::test_campaign_review -v`

---

## Phase 6: Sprint Audit and Advanced Queries (P2)

**Dependency:** Phase 5  
**Blocks:** (none — P2 completion gate)  
**Parallelism:** `sprint-audit` / `lint` / `campaign review` can start in parallel

### Task 6.1: Implement `bth sprint-audit` (P2)

**Files:**
- Modify: `src/bathos/config.py` — add `projects.toml` registry
- Modify: `src/bathos/init.py` — populate registry on `bth init`
- Create: `src/bathos/sprint_audit.py` — new module
- Test: `tests/test_sprint_audit.py`

**Changes:**

**Step 0: Add toml write dependency**

`tomllib` (standard library) is read-only. Writing `~/.bth/projects.toml` requires the `toml` PyPI package.

1. Add `'toml>=0.10'` to `[project.dependencies]` in `pyproject.toml`
2. Run `uv lock` to update the lockfile
3. Verify: `python -c 'import toml; print(toml.__version__)'` should succeed
4. Use `toml.dumps(registry)` for serialization and `toml.loads(text)` for deserialization (NOT `tomllib.loads`)

1. **Step 1: Add global projects registry**
   - Modify `config.py`:
     ```python
     PROJECTS_REGISTRY = Path.home() / ".bth" / "projects.toml"
     
     def register_project(slug: str, catalog_dir: Path) -> None:
         """Register a project in global registry."""
         registry = tomllib.loads(PROJECTS_REGISTRY.read_text()) if PROJECTS_REGISTRY.exists() else {}
         registry.setdefault("projects", []).append({"slug": slug, "catalog_dir": str(catalog_dir)})
         PROJECTS_REGISTRY.write_text(toml.dumps(registry))  # Use toml library, not tomllib
     
     def list_registered_projects() -> list[dict]:
         """List all registered projects."""
         if not PROJECTS_REGISTRY.exists():
             return []
         registry = tomllib.loads(PROJECTS_REGISTRY.read_text())
         return registry.get("projects", [])
     ```

2. **Step 2: Update `init.py` to register project**
   - Call `register_project()` at end of `init_project()`

3. **Step 3: Create `sprint_audit.py` module**
   ```python
   def sprint_audit(hours: int = 24) -> dict:
       """Cross-project audit of recent runs and campaigns."""
       projects = list_registered_projects()
       
       audit_results = {}
       warnings = []
       
       for project in projects:
           catalog_dir = Path(project["catalog_dir"])
           db_path = catalog_dir / "bathos.db"
           
           # Check schema version before ATTACH
           try:
               db_check = duckdb.connect(str(db_path), read_only=True)
               version_row = db_check.execute(
                   "SELECT value FROM _schema_meta WHERE key = 'warm_version'"
               ).fetchall()
               
               if version_row:
                   version = version_row[0][0]
                   if version != CURRENT_SCHEMA_VERSION:
                       warnings.append(
                           f"Project {project['slug']}: schema version mismatch "
                           f"(has {version}, need {CURRENT_SCHEMA_VERSION}) — "
                           f"run bth compact first. Skipping."
                       )
                       db_check.close()
                       continue
               
               db_check.close()
           except Exception as e:
               warnings.append(
                   f"Project {project['slug']}: failed to check schema version — {e}. Skipping."
               )
               continue
           
           # Safe to open and use this DB
           db = duckdb.connect(str(db_path), read_only=True)
           
           # Query runs in last N hours
           cutoff = (datetime.now(UTC) - timedelta(hours=hours)).isoformat()
           rows = db.execute("""
               SELECT id, campaign_id, sidecar_mode, outcome, outcome_is_residual, timestamp
               FROM runs
               WHERE timestamp > ?
               ORDER BY timestamp DESC
           """, [cutoff]).fetchall()
           
           # Group by campaign
           by_campaign = {}
           for run_id, campaign_id, sidecar_mode, outcome, is_residual, ts in rows:
               if campaign_id not in by_campaign:
                   by_campaign[campaign_id] = []
               by_campaign[campaign_id].append({
                   "run_id": run_id,
                   "sidecar_mode": sidecar_mode,
                   "outcome": outcome,
                   "is_residual": is_residual,
               })
           
           # Compute anomalies
           anomalies = []
           for campaign_id, runs in by_campaign.items():
               unknown_count = sum(1 for r in runs if r["outcome"] == "unknown")
               bypassed_count = sum(1 for r in runs if r["sidecar_mode"] == "bypassed")
               residual_count = sum(1 for r in runs if r["is_residual"])
               
               if unknown_count > 0:
                   anomalies.append(f"Campaign {campaign_id}: {unknown_count} runs with outcome=unknown")
               if bypassed_count / len(runs) > 0.1:
                   anomalies.append(f"Campaign {campaign_id}: {bypassed_count}/{len(runs)} bypassed")
               if residual_count / len(runs) > 0.1:
                   anomalies.append(f"Campaign {campaign_id}: residual rate {residual_count/len(runs):.1%} > 10%")
           
           audit_results[project["slug"]] = {
               "runs": len(rows),
               "campaigns": len(by_campaign),
               "anomalies": anomalies,
           }
           db.close()
       
       return {
           "audit_results": audit_results,
           "warnings": warnings,
       }
   ```

4. **Step 4: Add CLI command with version gating output**
   ```python
   @app.command()
   def sprint_audit(hours: int = typer.Option(24, "--hours", help="Lookback window in hours")):
       """Audit recent runs and campaigns across all projects."""
       from bathos.sprint_audit import sprint_audit as run_audit
       
       result = run_audit(hours)
       
       # Show schema version warnings first
       if result["warnings"]:
           typer.echo("Schema Version Warnings:")
           for warning in result["warnings"]:
               typer.echo(f"  ⚠️  {warning}")
           typer.echo()
       
       # Show audit results for compatible projects
       for project_slug, data in result["audit_results"].items():
           typer.echo(f"{project_slug}: {data['runs']} runs, {data['campaigns']} campaigns")
           for anomaly in data["anomalies"]:
               typer.echo(f"  ⚠️  {anomaly}")
   ```

5. **Step 5: Write tests**
   - Test sprint_audit with 2 projects
   - Test anomaly detection
   - **Test schema version gating:** `test_sprint_audit_skips_incompatible_schema()` — create a fake warm DB with `warm_version` = "2", create another with "3"; run sprint_audit → incompatible project skipped with warning, compatible project included in results

6. **Step 6: Commit**
   ```bash
   git add src/bathos/sprint_audit.py src/bathos/config.py src/bathos/init.py src/bathos/cli.py tests/test_sprint_audit.py
   git commit -m "feat(P2): add bth sprint-audit cross-project auditing"
   ```

### Task 6.3: Extend `bth lint` with Tier-2 Warm-Catalog Checks (P2)

**Files:**
- Modify: `src/bathos/linter.py` (existing)
- Modify: `src/bathos/cli.py`
- Test: `tests/test_linter.py`

**Changes:**

1. **Step 1: Add `check_residual_rates()` function in `linter.py`**
   ```python
   def check_residual_rates(catalog_dir: Path, threshold: float = 0.10) -> list:
       """Check for campaigns with residual rate above threshold."""
       issues = []
       db = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
       
       try:
           rows = db.execute("""
               SELECT cr.campaign_id, COUNT(*) as total,
                      SUM(CASE WHEN r.outcome_is_residual THEN 1 ELSE 0 END) as residual_count
               FROM campaign_runs cr
               INNER JOIN runs r ON cr.run_id = r.id
               GROUP BY cr.campaign_id
               HAVING (residual_count::FLOAT / total) > ?
           """, [threshold]).fetchall()
           
           for campaign_id, total, residual_count in rows:
               rate = residual_count / total if total > 0 else 0
               issues.append(LintIssue(
                   severity=IssueSeverity.WARNING,
                   issue="high_residual_rate",
                   detail=f"Campaign {campaign_id}: residual rate {rate:.1%} exceeds {threshold:.0%} threshold ({residual_count}/{total} runs)",
                   path=Path('.'),
                   directory='catalog'
               ))
       except Exception as e:
           issues.append(LintIssue(
               severity=IssueSeverity.ERROR,
               issue="linter_query_error",
               detail=f"Failed to check residual rates: {e}",
               path=Path('.'),
               directory='catalog'
           ))
       
       return issues
   ```

2. **Step 2: Add `check_bypass_trend()` function**
   ```python
   def check_bypass_trend(catalog_dir: Path) -> list:
       """Check if bypass rate (sidecar_mode='bypassed') is increasing week-over-week."""
       issues = []
       db = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
       
       try:
           rows = db.execute("""
               SELECT 
                   STRFTIME(DATE_TRUNC('week', timestamp), '%Y-W%W') as week,
                   COUNT(*) as total,
                   SUM(CASE WHEN sidecar_mode = 'bypassed' THEN 1 ELSE 0 END) as bypassed_count
               FROM runs
               WHERE timestamp > CURRENT_TIMESTAMP - INTERVAL 4 WEEK
               GROUP BY week
               ORDER BY week DESC
           """).fetchall()
           
           if len(rows) >= 2:
               rates = [(r[0], r[2] / r[1] if r[1] > 0 else 0) for r in rows]
               # Check if trend is increasing
               if rates[0][1] > rates[1][1]:  # Latest > previous
                   issues.append(LintIssue(
                       severity=IssueSeverity.WARNING,
                       issue="increasing_bypass_trend",
                       detail=f"Bypass rate increasing: {rates[0][1]:.1%} (latest week) > {rates[1][1]:.1%} (previous week)",
                       path=Path('.'),
                       directory='catalog'
                   ))
       except Exception as e:
           issues.append(LintIssue(
               severity=IssueSeverity.ERROR,
               issue="linter_query_error",
               detail=f"Failed to check bypass trend: {e}",
               path=Path('.'),
               directory='catalog'
           ))
       
       return issues
   ```

3. **Step 3: Add `check_unfired_branches()` function**
   ```python
   def check_unfired_branches(catalog_dir: Path, min_runs: int = 5) -> list:
       """Check for declared outcome branches that never appear in runs."""
       issues = []
       db = duckdb.connect(str(catalog_dir / "bathos.db"), read_only=True)
       
       try:
           rows = db.execute("""
               SELECT script_path, sidecar_sha256, COUNT(*) as run_count
               FROM runs
               WHERE sidecar_sha256 IS NOT NULL AND sidecar_sha256 != ''
               GROUP BY script_path, sidecar_sha256
               HAVING COUNT(*) >= ?
           """, [min_runs]).fetchall()
           
           for script_path, sidecar_sha256, run_count in rows:
               # For each (script, sidecar) pair with enough runs, check outcomes
               outcomes = db.execute(
                   "SELECT DISTINCT outcome FROM runs WHERE script_path = ? AND sidecar_sha256 = ?",
                   [script_path, sidecar_sha256]
               ).fetchall()
               fired_outcomes = {r[0] for r in outcomes}
               
               # We would need to parse the sidecar to know declared branches
               # For now, flag if all runs have same outcome (suspicious)
               if len(fired_outcomes) == 1:
                   issues.append(LintIssue(
                       severity=IssueSeverity.WARNING,
                       issue="single_outcome_branch_fired",
                       detail=f"Script {script_path}: {run_count} runs all have outcome '{list(fired_outcomes)[0]}' — consider if other branches should have fired",
                       path=Path('.'),
                       directory='catalog'
                   ))
       except Exception as e:
           issues.append(LintIssue(
               severity=IssueSeverity.ERROR,
               issue="linter_query_error",
               detail=f"Failed to check unfired branches: {e}",
               path=Path('.'),
               directory='catalog'
           ))
       
       return issues
   ```

4. **Step 4: Wire into `bth lint` CLI command**
   ```python
   @app.command("lint")
   def lint_cmd():
       """Run linter checks on warm catalog."""
       catalog_dir = _catalog_dir()
       
       # Check if warm DB exists
       db_path = catalog_dir / "bathos.db"
       if not db_path.exists():
           typer.echo("Warm catalog not found. Run 'bth compact' first.", err=True)
           return
       
       from bathos.linter import (
           check_residual_rates,
           check_bypass_trend,
           check_unfired_branches,
       )
       
       all_issues = []
       all_issues.extend(check_residual_rates(catalog_dir))
       all_issues.extend(check_bypass_trend(catalog_dir))
       all_issues.extend(check_unfired_branches(catalog_dir))
       
       if not all_issues:
           typer.echo("No linter issues found.")
           return
       
       for issue in all_issues:
           icon = "❌" if issue.severity == "error" else ("⚠️ " if issue.severity == "warning" else "ℹ️ ")
           typer.echo(f"{icon} {issue.issue}: {issue.detail}")
       
       typer.echo(f"\nTotal: {len(all_issues)} issues")
   ```

5. **Step 5: Write tests**
   - `test_check_residual_rates_detects_high_rate()` — create campaign with >10% residual runs, verify detected
   - `test_check_bypass_trend_detects_increase()` — create runs across 2 weeks with increasing bypass rate
   - `test_check_unfired_branches_detects_single_outcome()` — create runs all with same outcome label

6. **Step 6: Commit**
   ```bash
   git add src/bathos/linter.py src/bathos/cli.py tests/test_linter.py
   git commit -m "feat(P2): add bth lint Tier-2 checks (residual rates, bypass trend, unfired branches)"
   ```

**Verification Gate:**
- `pytest tests/test_linter.py -v` — all lint check tests pass
- `bth lint` runs without error on seeded warm DB

### Task 6.2: Implement `bth campaign review` (P2)

**Files:**
- Modify: `src/bathos/cli.py`
- Modify: `src/bathos/campaigns.py` — add review functions

**Changes:**

1. **Step 1: Implement campaign review logic in `campaigns.py`**
   ```python
   def review_campaign(db, campaign_id: str) -> dict:
       """Generate campaign review with sidecar quality signals."""
       # Fetch all runs in campaign
       rows = db.execute("""
           SELECT r.id, r.sidecar_sha256, r.outcome, r.outcome_is_residual,
                  r.sidecar_mode, r.exit_code
           FROM runs r
           INNER JOIN campaign_runs cr ON r.id = cr.run_id
           WHERE cr.campaign_id = ?
       """, [campaign_id]).fetchall()
       
       if not rows:
           return {"error": "Campaign not found"}
       
       # Compute metrics
       residual_count = sum(1 for r in rows if r[3])  # outcome_is_residual
       unknown_count = sum(1 for r in rows if r[2] == "unknown")
       bypassed_count = sum(1 for r in rows if r[4] == "bypassed")
       declared_count = sum(1 for r in rows if r[4] == "declared")
       
       return {
           "total_runs": len(rows),
           "residual_rate": residual_count / len(rows) if rows else 0,
           "unknown_rate": unknown_count / len(rows) if rows else 0,
           "bypass_rate": bypassed_count / len(rows) if rows else 0,
           "declared_rate": declared_count / len(rows) if rows else 0,
           "anomalies": [
               "residual_rate > 10%" if residual_count / len(rows) > 0.1 else None,
               "unknown_rate > 5%" if unknown_count / len(rows) > 0.05 else None,
               "bypass_rate > 10%" if bypassed_count / len(rows) > 0.1 else None,
               "all_same_outcome" if len(set(r[2] for r in rows)) == 1 else None,
           ]
       }
   ```

2. **Step 2: Add `campaign review` CLI command**
   ```python
   @campaign_app.command("review")
   def campaign_review(name: str = typer.Argument(...)):
       """Review campaign: residual rate, bypass rate, outcome distribution."""
       from bathos.campaigns import review_campaign
       
       db = duckdb.connect(str(_catalog_dir() / "bathos.db"))
       review = review_campaign(db, name)
       
       if "error" in review:
           typer.echo(review["error"], err=True)
           raise typer.Exit(1)
       
       typer.echo(f"Campaign {name}:")
       typer.echo(f"  Runs: {review['total_runs']}")
       typer.echo(f"  Residual rate: {review['residual_rate']:.1%}")
       typer.echo(f"  Unknown rate: {review['unknown_rate']:.1%}")
       typer.echo(f"  Bypass rate: {review['bypass_rate']:.1%}")
       for anomaly in review["anomalies"]:
           if anomaly:
               typer.echo(f"  ⚠️  {anomaly}")
   ```

3. **Step 3: Write tests**
   - Test review with campaign of 10 runs
   - Test anomaly detection (>10% residual)

4. **Step 4: Commit**
   ```bash
   git add src/bathos/campaigns.py src/bathos/cli.py tests/test_campaigns.py
   git commit -m "feat(P2): add bth campaign review with residual rate and anomaly detection"
   ```

---

## Phase 7: MCP Tools (P2)

**Dependency:** Phase 6  
**Blocks:** Agentic mode execution  
**Parallelism:** Can be developed in parallel

### Task 7.1: Add MCP Gate Error Tool (P2)

**Files:**
- Modify: `src/bathos/mcp.py`
- Test: `tests/test_mcp_server.py`

**Changes:**

This task updates the MCP server to expose `gate_check()` as a tool that returns structured error payloads instead of exceptions.

1. **Step 1: Add MCP tool for gate check**
   ```python
   @server.call_tool()
   async def call_tool(name: str, arguments: dict) -> Any:
       if name == "run":
           # existing run tool
           pass
       elif name == "gate_check":
           script_path = Path(arguments["script_path"])
           mode = arguments.get("mode", "collaborative")
           result = gate_check(script_path, mode)
           return {
               "status": "ok" if result.ok else "gate_failure",
               "gate": result.failure.gate if result.failure else None,
               "errors": result.failure.errors if result.failure else [],
               "remediation": result.failure.remediation if result.failure else None,
               "gate_schema_version": 1
           }
   ```

2. **Step 2: Write tests**
   - Test gate_check tool returns structured payload on sidecar missing
   - Test gate_check tool returns ok=true on valid sidecar

3. **Step 3: Compute and store skill_sha256 on autonomous sidecar generation**
   
   When the MCP tool invokes `run_script()` in autonomous mode:
   
   1. Hash `agent_assets/using_bathos/SKILL.md` using hashlib.sha256
   2. Store the hex digest in `run.skill_sha256` before `write_run()` is called
   3. Implementation location: in `runner.py` after `run` object is created but before `write_run()`:
      ```python
      # Compute skill_sha256 if autonomous mode
      skill_sha256 = ""
      if resolved_mode == "autonomous":
          try:
              # Find the skill markdown in the package
              skill_path = Path(bathos.__file__).parent / "agent_assets" / "using_bathos" / "SKILL.md"
              if skill_path.exists():
                  skill_sha256 = hashlib.sha256(skill_path.read_bytes()).hexdigest()
          except Exception:
              skill_sha256 = ""  # Fallback to empty if skill not found
      
      run = dataclasses.replace(run, skill_sha256=skill_sha256)
      ```
   4. Also store it in the generated sidecar's `[provenance]` section (if generated in autonomous mode):
      ```python
      # When generating a sidecar in autonomous mode, add to TOML:
      [provenance]
      skill_sha256 = "abc123def456..."
      generated_at = "2026-05-20T12:34:56Z"
      agent_mode = "autonomous"
      ```
   5. Test case: `test_mcp_autonomous_run_stores_skill_sha256()` — after an autonomous MCP run, the run record has a non-empty `skill_sha256` field matching the hash of `SKILL.md`
   6. Test case: `test_mcp_autonomous_generated_sidecar_includes_skill_sha256()` — when MCP generates a sidecar in autonomous mode, it includes `[provenance] skill_sha256`

4. **Step 4: Run tests**
   - `pytest tests/test_mcp_server.py::test_mcp_autonomous_run_stores_skill_sha256 -v`
   - `pytest tests/test_mcp_server.py::test_mcp_autonomous_generated_sidecar_includes_skill_sha256 -v`
   - `pytest tests/test_mcp_server.py -v` (all MCP tests pass)

5. **Step 5: Commit**
   ```bash
   git add src/bathos/mcp.py src/bathos/runner.py tests/test_mcp_server.py
   git commit -m "feat(P2): compute and store skill_sha256 on autonomous sidecar generation"
   ```

**Verification Gate:**
- `pytest tests/test_mcp_server.py -v` — skill_sha256 storage tests pass

### Task 7.2: Add MCP Campaign Tools (P2)

**Files:**
- Modify: `src/bathos/mcp.py`

**Changes:**

1. **Step 1: Add MCP tools for campaign CRUD**
   ```python
   @server.call_tool()
   async def call_tool(name: str, arguments: dict) -> Any:
       if name == "campaign_create":
           campaign = create_campaign(db, **arguments)
           return asdict(campaign)
       elif name == "campaign_add_run":
           add_run_to_campaign(db, arguments["campaign_id"], arguments["run_id"])
           return {"status": "ok"}
       elif name == "campaign_conclude":
           conclude_campaign(db, **arguments)
           return {"status": "ok"}
       elif name == "campaign_review":
           return review_campaign(db, arguments["campaign_id"])
   ```

2. **Step 2: Write tests**
   - Test campaign_create tool
   - Test campaign_add_run tool

3. **Step 3: Commit**
   ```bash
   git add src/bathos/mcp.py tests/test_mcp_server.py
   git commit -m "feat(P2): expose campaign CRUD as MCP tools"
   ```

**Verification Gate:**
- `pytest tests/test_mcp_server.py::test_mcp_campaign_create -v`
- `pytest tests/test_mcp_server.py::test_mcp_campaign_add_run -v`

---

## Phase 8: Documentation and Integration Tests (P2)

**Dependency:** All P1/P2 items  
**Blocks:** Release  
**Parallelism:** Can work in parallel

### Task 8.1: Update SKILL.md with Three-Tier Taxonomy (P2)

**Files:**
- Create/Update: `agent_assets/using_bathos/SKILL.md`

**Note:** The design document (§9) specifies the three-tier taxonomy in detail. This task is documentation-only and depends on the implementation above being complete.

**Summary of changes:**
- Add Tier 1 section: "Structurally Enforceable" — list validation rules in `validate.py`
- Add Tier 2 section: "Post-Hoc Checkable" — list gates in `bth lint` and `bth campaign review`
- Add Tier 3 section: "Interpretive" — HARKing guidance, exploration→confirmation discipline
- Add run mode protocol: CLI flags, priority ordering, escape hatches
- Add campaign workflow: exploration→confirmation transition, verdict criteria

### Task 8.2: Full Integration Test Suite (P2)

**Files:**
- Create: `tests/test_agentic_integrity_integration.py` — comprehensive e2e test

**Example test:**
```python
def test_full_agentic_integrity_workflow(tmp_path):
    """End-to-end: pre-registration → gating → outcome → campaign."""
    # 1. Create experiment with sidecar
    # 2. Run bth run → expect gate pass
    # 3. Verify outcome populated
    # 4. Create campaign
    # 5. Add run to campaign
    # 6. Review campaign → check residual rate
    # 7. Conclude campaign
```

---

## Risk Flags and Hazards

| Risk | Severity | Mitigation |
|------|----------|-----------|
| **Migration chain complexity** | Medium | Thorough testing of v0→v1→v2→v3; migration is already proven pattern |
| **DuckDB SQL injection** | Medium | Always use parameterized queries; validate SQL at gate time, not runtime |
| **Metadata never populated** | Critical | Phase 1 is blocker; no outcome eval possible without result emission |
| **Gate failures not visible in MCP** | Medium | Return structured error payload, not exception; document MCP error handling in SKILL.md |
| **First-of-kind check (Q5)** | Medium | Use git-tracked file identity (path + git SHA); simplest and most robust |
| **Campaign verdict with mixed sidecar_mode** | Low | Enforce at verdict time: all runs must have `sidecar_mode = "declared"` for confirmation verdict |
| **HARKing detection** | Low | Tool records exploration→confirmation link; interpretation is Tier 3 (researcher responsibility) |

---

## Verification Gates by Phase

| Phase | Gate | Command |
|-------|------|---------|
| P0 | Result emission works | `pytest tests/test_runner.py::test_result_emission* -v` |
| P1 | Schema migration completes | `pytest tests/test_compact.py::test_migration_v2_to_v3 -v` |
| P1 | Gate logic correct | `pytest tests/test_prereg.py -v` |
| P1 | Outcome population | `pytest tests/test_runner.py::test_outcome_is_residual -v` |
| P2 | Campaigns CRUD | `pytest tests/test_campaigns.py -v` |
| P2 | Campaign review signals | `pytest tests/test_campaigns.py::test_review_campaign -v` |
| P2 | Sprint audit | `pytest tests/test_sprint_audit.py -v` |
| ALL | Integration | `pytest tests/test_integration.py -v` |

---

## Parallelization Opportunities

**Can run in parallel after Phase 1 completion:**
- Phase 2 (schema migration) + Phase 3.1 (validate.py) — independent
- Phase 3.2 (prereg.py) — depends on 3.1, can start after validate.py tests pass
- Phase 4 (queries) — independent after Phase 3 complete
- Phase 5 (campaigns DDL) — independent; can start after schema v3
- Phase 5 (CLI) — depends on campaigns module
- Phase 6 (sprint audit) — independent after campaigns complete

**Recommended parallelization strategy:**
1. **Dispatch 3 agents in parallel** after Phase 1:
   - **Agent A:** Phase 2 (schema migration)
   - **Agent B:** Phase 3.1 (validate.py)
   - **Agent C:** Phase 4 (query filters + lineage)

2. **Converge on Phase 3.2/3.3** after A and B complete

3. **Dispatch 2 agents** after Phase 4:
   - **Agent D:** Phase 5 (campaigns module)
   - **Agent E:** Phase 6.1 (sprint audit)

4. **Converge on Phase 5 CLI** after Phase 5 DDL

5. **Final:** Phase 8 (docs + integration tests)

---

## Summary

This plan decomposes the validated design into 43 atomic tasks across 8 phases. All P0 and P1 work is dependency-ordered; P2 items are grouped for parallelization. Each task specifies exact file paths, test cases, verification gates, and commit messages. The plan is ready for subagent dispatch with clear hand-off points and risk flagging.
