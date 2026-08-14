---
name: bathos-mcp
description: bathos MCP tool integration contract — error envelope shape, BathosErrorCode values, and caller patterns
triggers: [bathos mcp, BathosErrorCode, mcp tool integration, error envelope, validation_ok, bth-mcp]
---

# bathos-mcp

Integration contract for programmatic callers of bathos's MCP server (`bth-mcp`). All 22 bathos MCP tools return a typed envelope with consistent structure. Understanding the envelope shape and error codes is essential for robust integrations.

## Envelope Shape

Every successful or failed MCP call returns a dictionary with these four keys (always present; KeyError is impossible):

```json
{
  "ok": true,
  "error_code": null,
  "error": null,
  "resolution_hint": null,
  "data_field_1": "...",
  "data_field_2": "..."
}
```

**Fields:**
- `ok` (bool) — `true` if call succeeded; `false` if error
- `error_code` (str | null) — `null` on success; one of 16 BathosErrorCode values on error
- `error` (str | null) — `null` on success; human-readable error message on error
- `resolution_hint` (str | null) — `null` on success; actionable fix suggestion on error
- Additional fields vary by tool (on success only)

## BathosErrorCode Values (16 total)

**Gate-derived codes (11, aliased from GateErrorCode):**
- `sidecar_missing` — Sidecar `.bth.toml` not found
- `sidecar_invalid` — TOML syntax error or missing required sections
- `sidecar_hash_mismatch` — Sidecar content changed; hash mismatch detected
- `not_first_of_kind` — Run of this script already exists; use `--derived-from`
- `manifest_write_failed` — Failed to write `.bth.postmortem.toml` manifest
- `adversarial_check_missing` — Missing `adversarial_check` in `outcomes.pass` blocks
- `hypothesis_lock_missing` — Hypothesis lock file not found
- `outcome_evaluation_error` — DuckDB SQL condition parsing or evaluation failed
- `result_schema_mismatch` — Result JSON doesn't match declared schema
- `outcome_ambiguous` — Multiple outcome conditions matched (exactly one expected)
- `internal` — Unexpected internal error

**Domain-specific codes (5):**
- `catalog_error` — Database or Parquet I/O failure
- `campaign_error` — Campaign query or update failure
- `sidecar_error` — Sidecar parsing or content validation error
- `export_error` — Export (HTML, archive) generation failure
- `invalid_param` — Invalid parameter or argument

## Caller Pattern (Standard Case)

For most tools, check `ok` and extract data:

```python
result = await session.call_tool("bathos", "list_runs", {"project_slug": "myproject"})
if not result["ok"]:
    raise RuntimeError(
        f"[{result['error_code']}] {result['error']}\n"
        f"Hint: {result['resolution_hint']}"
    )

# On success, access tool-specific data
runs = result["runs"]
for run in runs:
    print(f"{run['id']}: {run['outcome']}")
```

## Special Case: validation_ok (postmortem_validate, validate_sidecar)

Two tools use a different validation result field:

```python
result = await session.call_tool("bathos", "validate_sidecar", {"script_path": "scripts/experiments/train.bth.toml"})

# Transport always succeeds (ok=True)
assert result["ok"] is True

# Validation result is in validation_ok (NOT ok)
if not result["validation_ok"]:
    for error_msg in result.get("errors", []):
        print(f"Validation error: {error_msg}")
else:
    print("Sidecar is valid")
```

**Envelope shape for these tools (success):**
```json
{
  "ok": true,
  "error_code": null,
  "error": null,
  "resolution_hint": null,
  "validation_ok": true,
  "path": "..."
}
```

**Envelope shape for these tools (failure):**
```json
{
  "ok": true,
  "error_code": null,
  "error": null,
  "resolution_hint": null,
  "validation_ok": false,
  "errors": ["field1: error message", "field2: error message"]
}
```

**Why two validation fields?**
- `ok` indicates transport success (the MCP call itself worked)
- `validation_ok` indicates semantic success (the sidecar/postmortem is structurally valid)
- `errors` is absent on success; present as a list of human-readable validation issues on failure

If a validation fails for reasons outside the tool (missing file, permission denied), both `ok` and `validation_ok` are `false`, and the error message is in `error`, not `errors`.

## Related

- **using-bathos** — CLI equivalents of these tools (`bth run`, `bth check`, `bth ls`, ...)
- **bathos-campaigns** — `claim_scaffold`/`claim_validate` MCP tools follow this same envelope
- **CLAUDE.md**: `mcp.py` — thin FastMCP layer mirroring `cli.py`, tool-for-tool
