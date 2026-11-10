---
name: bathos-mcp
description: bathos MCP tool integration contract — error envelope shape, BathosErrorCode values, and caller patterns
when_to_use: 'Triggers: bathos mcp, BathosErrorCode, error envelope, validation_ok, mcp tool integration, bth-mcp.'
---
# bathos-mcp

Integration contract for programmatic callers of bathos's MCP server (`bth-mcp`). All bathos MCP tools (registered via the `_WIRED = cisternal.wire(...)` list at the bottom of `mcp.py` — 97 as of this writing, see that file for the current enumerated set) return a typed envelope with consistent structure. Understanding the envelope shape and error codes is essential for robust integrations.

## Envelope Shape

Every successful or failed MCP call returns a dictionary with these four standard keys (always present on standard paths; see exceptions below for validation-failure cases):

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
- `error_code` (str | null) — `null` on success; one of 41 BathosErrorCode values on error
- `error` (str | null) — `null` on success; human-readable error message on error
- `resolution_hint` (str | null) — `null` on success; actionable fix suggestion on error
- Additional fields vary by tool (on success only)

### Known Envelope Exceptions

Several tool functions (`get_run`, `run_sql`, `cite_run`, `query_attestation`, `campaign_create`,
`campaign_conclude`, `postmortem_scaffold`/`get`, `claim_attest_parity`, `blast_radius_assess`/
`clear`, and others) catch their own errors internally and return a **bare `{"error": "..."}`**
dict with no `ok`/`error_code`/`resolution_hint` keys on a validation-failure path — the standard
envelope wrapper never sees these since no exception is raised. A caller that indexes
`result["ok"]` unconditionally can hit a real `KeyError` on these tools' failure paths. Defensively
use `result.get("ok", False)` rather than indexing.

### Write Token Requirement

Mutating tools (`run`, `init`, `compact`, `archive`, `sync`, `repair`, `attestation_register`, the
scaffold/register/conclude family, `blast_radius_assess`/`blast_radius_clear`, `claim_author`/
`new_experiment`, `defect_declare`, `finding_create`, `supersede_edge_add`, `telemetry_rotate`, and others) take a `token=` parameter, which is **not needed over stdio**, the
transport `bth-mcp` always serves today: the caller owns the server process, so do not read
`~/.bth/mcp_token` to call them (debt #2841). The check applies when the tools are served any
other way, or when `BTH_MCP_REQUIRE_TOKEN=1` is set; then `token=` must match
`~/.bth/mcp_token`, and a missing or invalid token raises `auth_error`.

## BathosErrorCode Values (41 total)

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

**Domain-specific codes (8):**
- `catalog_error` — Database or Parquet I/O failure
- `campaign_error` — Campaign query or update failure
- `sidecar_error` — Sidecar parsing or content validation error
- `export_error` — Export (HTML, archive) generation failure
- `invalid_param` — Invalid parameter or argument
- `graduation_refused` — Graduation decision rejected by policy
- `auth_error` — Authentication or token validation failed

**Artifact archive:**
- `archive_error` — Archive operation or format error
- `archive_dirty_tree_refused` — Attempted archive with uncommitted local changes
- `archive_item_not_found` — Archive item not found in catalog
- `archive_bundle_not_found` — Archive bundle not found

**Document authoring:**
- `document_invalid` — Document schema or structure validation failed
- `document_conflict` — Document authoring conflict or lock contention

**Result Validity & Instrument Defect Registry (Spec 261106 P1):**
- `result_void` — Run was produced by a defective instrument version and is void
- `result_affected` — Run is implicated by an active blast-radius ledger entry
- `result_superseded` — Run was superseded by a newer run
- `result_archived` — Run's script or output artifacts were archived
- `result_hidden` — Attempted to access a hidden run without `--include-superseded`
- `result_suspect_unacknowledged` — Run toolchain is unverified/suspect; requires `ack_suspect`
- `result_unverifiable_unacknowledged` — Run provenance unverified against lockfiles; requires `ack_unverifiable`
- `estimand_boundary_mixed` — Attempted to pool runs across estimand change boundary without `allow_mixed_estimand`

**Findings & Evidence Tiers (Spec 261106 P2):**
- `finding_reopen_reason_required` — Question is already settled; requires `reopen_reason`
- `finding_settled_evidence_insufficient` — Settling a finding requires at least one active confirmatory run
- `finding_not_found` — Finding slug or ID not found in `.bth/findings/`
- `superseded_target_not_found` — Target entity in `supersedes` does not exist
- `spike_tier_cannot_settle` — Spike runs are preliminary probes and cannot support a settled finding

**Archive Sweep, Doc Store & Telemetry (Spec 261106 P3):**
- `archive_sweep_unapproved` — Sweep `--apply` passed without an approved proposal
- `archive_proposal_stale` — Proposal SHA256 or git state diverged before apply
- `doc_store_unavailable` — Configured doc store not accessible
- `restore_conflict` — Restore would overwrite an untracked or modified file

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

## Result Validity, Findings & Archive MCP Tools (Spec 261106)

Ten new MCP tools extend bathos with result-validity management, findings registers, archive sweep, and session initialization:

1. **`defect_declare`** (`@require_write_token`):
   - Declares an instrument defect or estimand change in `.bth/instruments/<pkg>.toml` (project) or `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/<pkg>.toml` (user-global).
   - Parameters: `package`, `defect_id`, `introduced`, `fixed`, `severity` (`void` | `suspect`), `kind` (`defect` | `estimand_change`), `summary`, `predicate` (optional DuckDB SQL), `hide_inferred` (OD-1), `user_global` (bool).
   - CLI mirror: `bth instrument declare-defect`.
2. **`defect_list`** (read-only):
   - Lists active instrument defects and estimand changes across project-local and user registries.
   - Parameters: `package` (optional), `catalog_dir` (optional).
   - CLI mirror: `bth instrument list-defects`.
3. **`finding_create`** (`@require_write_token`):
   - Scaffolds and registers a durable finding in `.bth/findings/<slug>.toml`.
   - Parameters: `slug`, `question`, `answer`, `status` (`tentative` | `settled`), `supporting_runs` (list[str]), `reopen_reason` (str, required if question already settled), `supersedes` (str | list[str], optional predecessor slug(s) or UUID(s)).
   - Enforces settling gate (requires completed confirmatory run; `spike` runs cannot settle with `spike_tier_cannot_settle`), reopen guard (`finding_reopen_reason_required`), and OD-6 estimand boundary check (`estimand_boundary_mixed`).
   - CLI mirror: `bth findings create`.
4. **`finding_update`** (`@require_write_token`):
   - Updates an existing finding's status, answer, supporting runs, or supersedes target in `.bth/findings/<slug>.toml`.
   - Parameters: `slug_or_id`, `status` (`tentative` | `settled`), `answer`, `supporting_runs` (list[str]), `supersedes` (str | list[str], optional predecessor slug(s) or UUID(s)), `allow_mixed_estimand` (str), `catalog_dir` (optional).
   - Enforces settling gate (requires completed confirmatory run; `spike` runs cannot settle with `spike_tier_cannot_settle`), predecessor supersession edge activation, and OD-6 estimand boundary check (`estimand_boundary_mixed`).
   - CLI mirror: `bth findings update`.
5. **`finding_get`** (read-only):
   - Retrieves finding details by slug or ID with dynamic status evaluation (downgrades to `needs-recheck` if supporting runs become hidden or suspect; exposes `superseded_by`).
   - Parameters: `slug_or_id`, `catalog_dir` (optional).
   - CLI mirror: `bth findings get`.
6. **`finding_list`** (read-only):
   - Lists registered findings with status filtering and optional superseded inclusion.
   - Parameters: `status` (optional), `include_superseded` (bool), `catalog_dir` (optional).
   - CLI mirror: `bth findings list`.
7. **`supersede_add`** (MCP tool name: `supersede_edge_add`, `@require_write_token`):
   - Records typed supersession edges between runs, findings, or artifacts in `supersession_edges`.
   - Parameters: `from_type`, `from_id`, `superseded_by_type`, `superseded_by_id`, `reason`.
   - CLI mirror: `bth supersede add`.
8. **`session_start_summary`** (MCP tool name: `session_start`, read-only):
   - Produces a fast (<50ms, byte-capped at 2KB) context payload of active beliefs, open questions, and degraded findings (`needs-recheck`) for session-start hooks.
   - Parameters: `byte_cap` (default 2048), `catalog_dir` (optional).
   - CLI mirror: `bth session-start` and `bth-session-start-hook`.
9. **`archive_sweep`** (`@require_write_token_when_applying`):
   - Workspace cleanup tool: runs candidate discovery in `--dry-run` mode to generate a proposal with SHA256 hash, or applies an approved proposal (`--apply --proposal <path>`) with an atomic git commit and catalog archive record.
   - Parameters: `dry_run` (bool), `apply` (bool), `proposal_path` (str), `age_days` (int).
   - CLI mirror: `bth archive-sweep`.
10. **`telemetry_rotate`** (`@require_write_token`):
   - Compresses and rotates raw diagnostic JSONL logs in `~/.bth/catalog/logs/` into `.jsonl.zst`.
   - Parameters: `max_size_mb` (int), `max_age_days` (int), `catalog_dir` (optional).
   - CLI mirror: `bth telemetry rotate`.

## Related

- **using-bathos** — CLI equivalents of these tools (`bth run`, `bth check`, `bth ls`, ...)
- **bathos-campaigns** — `claim_scaffold`/`claim_validate` MCP tools follow this same envelope; `claim_author` and `doc_schema` conform to the standard envelope and use `document_*` error codes for authoring conflicts
- **using-bathos** (document authoring) — `new_experiment` MCP tool and its `bth new-experiment` CLI mirror for experiment scaffolding
- **CLAUDE.md**: `mcp.py` — thin FastMCP layer mirroring `cli.py`, tool-for-tool
