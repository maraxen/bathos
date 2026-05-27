# Bathos Telemetry Schema

## Overview

Bathos emits structured event logs as JSON Lines (one JSON object per line) to `~/.bth/catalog/logs/events.<hostname>.<pid>.jsonl`. This document describes the schema for all event types across all surfaces.

Each event carries a common envelope with timestamp, level, process/thread IDs, and contextual correlation fields. Per-surface event types add domain-specific fields.

## Common Envelope

Every event record includes these fields:

| Field | Type | Notes |
|-------|------|-------|
| `ts` | string | ISO 8601 UTC timestamp with microsecond precision (e.g., `2026-05-27T19:32:18.156703+00:00`) |
| `level` | string | Log level: `debug`, `info`, `warning`, `error` |
| `pid` | integer | Process ID |
| `tid` | integer | Thread ID |
| `host` | string | Hostname (from `socket.gethostname()`) — disambiguates cluster vs laptop |
| `surface` | string | Event surface/category (first component of event name): `run`, `sidecar`, `prereg`, `postmortem`, `campaign`, `lineage`, `mcp`, `sync`, `catalog`, `telemetry` |
| `event` | string | Full event name (e.g., `run.start`, `catalog.write.end`) — format: `<surface>.<verb>[.<stage>]` |
| `msg` | string | Human-readable message (may be empty for structured events) |
| `run_uuid` | string? | If set: correlation ID for a `bth run` invocation |
| `mcp_request_id` | string? | If set: correlation ID for a FastMCP tool call |
| `task_id` | string? | If set: praxia/orchestrator task ID from `$BTH_TASK_ID` |

> **Note on contextvar fields:** `run_uuid`, `mcp_request_id`, and `task_id` are only included in the JSON if set. Null/unset values are omitted to avoid noise.

---

## Surface: `run.*` — Experiment Execution

Events emitted by `bth run` in `runner.py`.

| Event | Fields | Notes |
|-------|--------|-------|
| `run.start` | `run_uuid`, `script_path`, `script_sha256`, `argv` (list), `cwd`, `campaign_id`?, `agent_mode` | Experiment execution begins |
| `run.subprocess_spawn` | `pid`, `cmd` | Subprocess spawned for the script |
| `run.heartbeat` | `pid`, `elapsed_ms` | Emitted every 60 s while subprocess alive (only after wall-clock > 60 s) |
| `run.subprocess_exit` | `exit_code`, `duration_ms`, `stdout_bytes`, `stderr_bytes` | Subprocess terminated |
| `run.parquet_written` | `path`, `bytes`, `duration_ms` | Parquet result file written |
| `run.error` | `phase` (validate/spawn/wait/persist), `exc_type`, `exc_msg` | Fatal error during run |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.156703+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "run",
  "event": "run.start",
  "msg": "",
  "run_uuid": "a1b2c3d4e5f6g7h8",
  "script_path": "scripts/experiments/train_model.py",
  "script_sha256": "abc123def456...",
  "argv": ["--epochs", "100"],
  "cwd": "/home/user/projects/myproject",
  "agent_mode": false
}
```

---

## Surface: `sidecar.*` — Pre-Registration & Validation

Events from `sidecar.py` and `validate.py` covering sidecar file parsing and validation.

| Event | Fields | Notes |
|-------|--------|-------|
| `sidecar.parsed` | `path`, `sha256`, `outcomes` (list of labels), `kind` (experiment/benchmark/debug) | Sidecar TOML successfully parsed |
| `sidecar.parse_error` | `path`, `exc_type`, `exc_msg` | Sidecar parsing failed (e.g., invalid TOML) |
| `sidecar.validate_error` | `path`, `field`, `reason` | Sidecar validation failed (missing outcome, bad DuckDB condition, etc.) |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.200000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "sidecar",
  "event": "sidecar.parsed",
  "msg": "",
  "run_uuid": "a1b2c3d4e5f6g7h8",
  "path": "scripts/experiments/train_model.bth.toml",
  "sha256": "def789ghi012...",
  "outcomes": ["pass", "marginal", "fail"],
  "kind": "experiment"
}
```

---

## Surface: `prereg.*` — Agentic Integrity Gate

Events from `prereg.py` covering the agentic pre-registration gate.

| Event | Fields | Notes |
|-------|--------|-------|
| `prereg.gate_pass` | `script_path`, `sidecar_sha256`, `agent_mode` | Script passed pre-registration gate (agent-mode check) |
| `prereg.gate_deny` | `script_path`, `reason`, `agent_mode` | Script denied by pre-registration gate (single most valuable event for debugging agentic runs) |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.250000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "prereg",
  "event": "prereg.gate_deny",
  "msg": "",
  "run_uuid": "a1b2c3d4e5f6g7h8",
  "task_id": "260527_task_xyz",
  "script_path": "scripts/analysis/broken.py",
  "reason": "agent_mode=true but no sidecar found",
  "agent_mode": true
}
```

---

## Surface: `postmortem.*` — Postmortem Operations

Events from `postmortem.py` covering postmortem validation and mutations.

| Event | Fields | Notes |
|-------|--------|-------|
| `postmortem.validated` | `path`, `run_id`?, `sprint_id`? | Postmortem TOML successfully validated |
| `postmortem.validate_error` | `path`, `reason` | Postmortem validation failed |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.300000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "postmortem",
  "event": "postmortem.validated",
  "msg": "",
  "path": "scripts/experiments/train.260527.bth.postmortem.toml",
  "run_id": "run_abc123"
}
```

---

## Surface: `campaign.*` — Campaign Lifecycle

Events from `campaigns.py` covering campaign creation and conclusion.

| Event | Fields | Notes |
|-------|--------|-------|
| `campaign.create` | `campaign_id`, `name` | Campaign created |
| `campaign.conclude` | `campaign_id`, `verdict` | Campaign concluded with verdict |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.350000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "campaign",
  "event": "campaign.create",
  "msg": "",
  "task_id": "260527_campaign_xyz",
  "campaign_id": "camp_xyz123",
  "name": "model-training-v2"
}
```

---

## Surface: `lineage.*` — Derived-From Resolution

Events from runner.py / lineage code covering parent-child run relationships.

| Event | Fields | Notes |
|-------|--------|-------|
| `lineage.resolved` | `child_run_uuid`, `parent_run_uuid` | Derived-from link successfully resolved |
| `lineage.resolve_error` | `child_run_uuid`, `derived_from`, `reason` | Failed to resolve parent run |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.400000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "lineage",
  "event": "lineage.resolved",
  "msg": "",
  "child_run_uuid": "run_xyz789",
  "parent_run_uuid": "run_abc123"
}
```

---

## Surface: `mcp.*` — FastMCP Tool Calls

Events from `mcp.py` covering tool invocations via the FastMCP server.

| Event | Fields | Notes |
|-------|--------|-------|
| `mcp.call_start` | `tool`, `request_id`, `arg_keys` (list of argument names only, no values) | Tool call started |
| `mcp.call_end` | `tool`, `request_id`, `duration_ms`, `ok`, `result_bytes` | Tool call completed |
| `mcp.call_error` | `tool`, `request_id`, `exc_type`, `exc_msg`, `traceback` | Tool call raised an exception |

> **Privacy note:** `arg_keys` contains only parameter names, never values, to avoid leaking payloads in logs.

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.450000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "mcp",
  "event": "mcp.call_end",
  "msg": "",
  "mcp_request_id": "req_uuid123",
  "tool": "list_runs",
  "duration_ms": 125.45,
  "ok": true,
  "result_bytes": 5423
}
```

---

## Surface: `sync.*` — Rsync & Remote Operations

Events from `sync.py`, `remote.py`, and `archive.py` covering catalog synchronization and archival.

| Event | Fields | Notes |
|-------|--------|-------|
| `sync.rsync_start` | `direction` (push/pull), `remote`, `src`, `dst`, `filters` (list) | Rsync operation starts |
| `sync.rsync_progress` | `bytes_transferred`, `files_transferred`, `pct`, `xfer_rate` | Progress update parsed from `--info=progress2` stream |
| `sync.rsync_stall` | `elapsed_since_progress_ms` | No progress line received for N seconds (hang detection) |
| `sync.rsync_end` | `exit_code`, `duration_ms`, `bytes_transferred`, `files_transferred` | Rsync operation complete |
| `sync.remote_test` | `remote`, `success`, `latency_ms`? (success only), `error`? (failure only) | SSH connectivity probe via `bth remote test` |
| `archive.export` | `partition`, `rows`, `duration_ms` | Parquet partition exported to cold storage |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.500000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "sync",
  "event": "sync.rsync_progress",
  "msg": "",
  "bytes_transferred": 1234567890,
  "files_transferred": 342,
  "pct": 45,
  "xfer_rate": "12.5 MB/s"
}
```

**`sync.remote_test` example (success):**
```json
{
  "ts": "2026-05-27T19:45:00.000000+00:00",
  "level": "info",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "sync",
  "event": "sync.remote_test",
  "msg": "",
  "remote": "engaging",
  "success": true,
  "latency_ms": 42
}
```

---

## Surface: `catalog.*` — DuckDB & Compaction

Events from `catalog.py`, `compact.py`, and `query.py` covering the warm/cool tier and queries.

| Event | Fields | Notes |
|-------|--------|-------|
| `catalog.write_parquet` | `path`, `rows`, `duration_ms` | Parquet file written to cool tier |
| `catalog.compact_start` | `cool_files`, `warm_rows_before` | Cool-to-warm compaction begins |
| `catalog.compact_end` | `cool_files`, `warm_rows_before`, `warm_rows_after`, `duration_ms` | Compaction complete |
| `catalog.duckdb_lock_wait` | `waited_ms`, `db_path` | DuckDB connection lock acquisition exceeded 500 ms (contention signal) |
| `catalog.query` | `query_kind` (ls/find/sql), `duration_ms`, `rows` | Query executed |

**Example:**
```json
{
  "ts": "2026-05-27T19:32:18.550000+00:00",
  "level": "warning",
  "pid": 12345,
  "tid": 123456789,
  "host": "laptop.local",
  "surface": "catalog",
  "event": "catalog.duckdb_lock_wait",
  "msg": "",
  "waited_ms": 750,
  "db_path": "/home/user/.bth/catalog/bathos.db"
}
```

---

## Querying Recipes

All examples assume events are in `~/.bth/catalog/logs/events.*.jsonl`.

### Live Tail with jq

```bash
tail -f ~/.bth/catalog/logs/events.*.jsonl | jq '.ts, .event, .ok'
```

### Last Hour of Errors

```bash
jq "select(.ts > \"$(date -u -d '1 hour ago' +'%Y-%m-%dT%H:%M:%S')\" and .level == \"error\")" \
  ~/.bth/catalog/logs/events.*.jsonl
```

### DuckDB Analysis

Load and query all events with DuckDB (auto-infer types):

```bash
duckdb <<SQL
SELECT ts, surface, event, level, duration_ms, ok, run_uuid
FROM read_json_auto('~/.bth/catalog/logs/events.*.jsonl')
WHERE level = 'error'
ORDER BY ts DESC
LIMIT 10;
SQL
```

### Hung-Run Detection

Find `run.start` events without corresponding `run.subprocess_exit` within N minutes:

```bash
duckdb <<SQL
WITH runs AS (
  SELECT run_uuid, event, ts
  FROM read_json_auto('~/.bth/catalog/logs/events.*.jsonl')
  WHERE surface = 'run'
)
SELECT r1.ts as start_time, r1.run_uuid
FROM runs r1
LEFT JOIN runs r2 ON r1.run_uuid = r2.run_uuid 
  AND r2.event = 'run.subprocess_exit'
WHERE r1.event = 'run.start'
  AND r2.run_uuid IS NULL
  AND r1.ts > now() - interval '30 minutes'
ORDER BY r1.ts DESC;
SQL
```

### Cross-Process Event Merge

Merge logs from multiple processes and sort by timestamp:

```bash
cat ~/.bth/catalog/logs/events.*.jsonl | jq -s 'sort_by(.ts)' | jq '.[]'
```

### RSync Hang Detection

Find rsync operations that stalled (emitted `rsync_stall` without `rsync_end`):

```bash
duckdb <<SQL
SELECT ts, remote, duration_since_last_progress_ms
FROM read_json_auto('~/.bth/catalog/logs/events.*.jsonl')
WHERE event = 'sync.rsync_stall'
ORDER BY ts DESC
LIMIT 5;
SQL
```

### Agentic Gate Denials

Useful for debugging why pre-registration failed in agent mode:

```bash
jq "select(.event == \"prereg.gate_deny\")" \
  ~/.bth/catalog/logs/events.*.jsonl
```

### Latency Distribution (MCP)

Tool call latency histogram:

```bash
duckdb <<SQL
SELECT 
  tool,
  QUANTILE_CONT(duration_ms, [0.5, 0.95, 0.99]) as p50_p95_p99,
  COUNT(*) as n_calls
FROM read_json_auto('~/.bth/catalog/logs/events.*.jsonl')
WHERE event = 'mcp.call_end'
GROUP BY tool
ORDER BY n_calls DESC;
SQL
```

---

## Notes

- **Correlation:** Use `run_uuid` to link all events for a single run. Use `mcp_request_id` for tool calls. Use `task_id` for orchestrated tasks.
- **Timestamps:** All `ts` values are in UTC. Safe to merge logs across time zones.
- **Null values:** Context variables (`run_uuid`, `mcp_request_id`, `task_id`) are omitted from JSON if unset (not included as `null`).
- **Serialization fallback:** Non-serializable field values are converted with `repr()` and a `telemetry.serialise_error` warning is emitted.
- **Multi-process safety:** Each process writes to its own file (`events.<hostname>.<pid>.jsonl`). Use the cross-process merge recipe to combine.
- **Rotation:** Each file rotates after 10 MB (5 backups, 50 MB max per process).

