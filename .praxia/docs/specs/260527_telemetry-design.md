# bathos telemetry design

**Date:** 2026-05-27
**Status:** Approved (oracle round 2: APPROVED_WITH_NITS — nits applied)
**Slug:** `260527_telemetry-design`
**Related:** [260515_bathos-design](260515_bathos-design.md)

## Context

bathos currently has **no logging infrastructure**. A scan of `src/bathos/` (25 modules) finds zero uses of `logging`, `tracing`, or `aiologger`; the only output is Rich `console.print` for CLI UI and a couple of bare `print()` statements. There is no audit trail for:

- `bth run` subprocess hangs or silent failures
- FastMCP tool-call errors / latency
- `bth sync` rsync hangs against the cluster
- DuckDB lock contention during cool→warm compaction
- Sidecar / prereg / postmortem gate denials (which currently print to stderr and exit)

The user wants a single audit surface to diagnose bugs and hang-ups, viewable from the laptop even for cluster-side events. This spec defines that telemetry layer.

## Decisions (fixed before design)

| # | Decision | Choice | Rationale |
|---|---|---|---|
| D1 | Substrate | **stdlib `logging` + `QueueHandler` → `QueueListener` → multi-process-safe file handler (see D6)** | aiologger is async-only; structlog is the more idiomatic JSONL/contextvar choice but pulls a new dep. Stdlib + custom `JsonFormatter` is zero-dep and meets every need below. |
| D2 | Format | **JSON Lines** (one JSON object per line) under `<catalog_dir>/logs/` (default `~/.bth/catalog/logs/`) | grep/jq/duckdb friendly; lives *inside* the catalog so existing `bth sync` ships it (closes F6). |
| D3 | Consumer interface | **JSONL on disk only.** No `bth logs` subcommand in this milestone. | YAGNI; schema is documented so a future `bth logs` is additive. |
| D4 | Correlation | **Contextvars captured on the producer thread via `QueueHandler.prepare()` and stamped onto the `LogRecord` as plain attributes.** Read by the formatter in the listener thread from `record.__dict__`, NOT via `var.get()`. (F2.) | Listener thread runs in its own context — naive `var.get()` would return empty in 100% of records. |
| D5 | Scope | **All four surfaces** + their gate sub-events: runner, mcp, sync/remote/archive, catalog/compact/query, **plus** prereg, sidecar, postmortem, campaign, lineage. (F4.) | These already exist in code and are exactly the bugs the user wants to audit. |
| D6 | Multi-process write safety | **Per-process file: `events.<hostname>.<pid>.jsonl`** under `<catalog_dir>/logs/`. Rotation is per-file. A `bth logs merge` helper (out of scope but documented) concatenates and sorts by `ts` on demand. (F5.) | `RotatingFileHandler` is not multi-process safe; SLURM arrays will interleave/corrupt a single file. Per-process files are simple, lockless, and ride existing `bth sync` semantics. |
| D7 | Hang detection mechanism | **Parse `rsync --info=progress2`** stream from a `Popen` to emit `sync.rsync_progress` with real bytes/files counts. No-progress-for-N-seconds is the genuine hang signal. (F3.) | A `subprocess.poll()` heartbeat thread reports "alive" for the full TCP timeout on a wedged connection — useless. |

## Architecture

### Module

A single module `src/bathos/telemetry.py` owns setup.

```
init_telemetry(level=None, log_dir=None) -> None     # idempotent; lazy on first event() if not called
get_logger(name: str) -> logging.Logger              # bathos.<name>
event(name: str, **fields) -> None                   # one-shot structured event; lazy-inits on first call
span(name: str, **fields) -> ContextManager          # see API contract below
```

**`span(name, **fields)` API (F9):**
- On enter: emits `name.start` with given fields and a fresh `span_id` (uuid4 hex).
- On normal exit: emits `name.end` with `span_id`, `duration_ms` (from `time.monotonic_ns()` for monotonic precision), `ok=true`.
- On uncaught exception: emits `name.end` with `ok=false`, `exc_type`, `exc_msg`, `traceback` (truncated to 8 KB), and re-raises.
- `name.error` is reserved for explicit non-fatal errors emitted by callers, not for span failure.

**`event(name, **fields)` before `init_telemetry()`:**
- Lazy-inits with defaults (warns once to stderr).
- Never silently drops.

### Internals

1. Creates the root `bathos` logger; sets `propagate=False`.
2. Attaches a `QueueHandler` backed by `queue.Queue(-1)` (unbounded).
3. **Overrides `QueueHandler.prepare()`** to (a) snapshot all bathos contextvars onto `record.__dict__` and (b) return the record unformatted (formatting happens on the listener thread to keep the producer fast). This is the F2 fix.
4. Starts a `QueueListener` in a daemon thread with one file handler writing to `<log_dir>/events.<hostname>.<pid>.jsonl`. Rotation: 10 MB × 5 backups per file (50 MB worst case per process — F7).
5. Default `log_dir` = `<catalog_dir>/logs/` so `bth sync` picks it up. Overridable via `$BTH_LOG_DIR` or `[telemetry].log_dir` in `~/.bth/config.toml`.
6. Formats records through `JsonFormatter` reading `record.__dict__` (envelope + per-event fields + captured contextvars). `json.dumps(default=repr)` for non-serialisable values; emits `telemetry.serialise_error` warning if `default` is invoked.
7. Level: `INFO` default; `$BTH_LOG_LEVEL=debug` overrides.
8. `atexit` hook drains the queue with a 2 s timeout and stops the listener cleanly.

### Subprocess / fork safety (F1)

`bth run` invokes `subprocess.run([...])` (`runner.py`) — Python uses `fork+exec`, so the listener thread is replaced by the new program. Safe. **This spec forbids switching to `multiprocessing` with the `fork` start method** in any future code that uses telemetry — `forkserver` or `spawn` only. One-line note will live in `telemetry.py` docstring.

### Why a queue even for local-file writes

The MCP server runs on an asyncio event loop. We do not want handler I/O (rotation, fsync) blocking the loop or the CLI. `QueueHandler.emit()` is `queue.put_nowait` — sub-microsecond. The listener thread does all formatting and I/O off the hot path.

### Correlation via contextvars (rewritten per F2)

Three `contextvars.ContextVar` instances:

- `run_uuid_var` — set in `runner.py` at the start of `bth run` (sync code; `var.set(...)` is fine).
- `mcp_request_id_var` — set inside the `@traced_tool` async wrapper. Each FastMCP tool call runs as its own `asyncio.Task`, which already gets a fresh context copy at task creation — so `var.set(...)` inside the wrapper is automatically request-scoped (F8). No `copy_context().run(...)` needed.
- `task_id_var` — read from `$BTH_TASK_ID` once at `init_telemetry()` for praxia/orchestrator correlation.

`QueueHandler.prepare()` snapshots `var.get(None)` for each onto `record.run_uuid`, `record.mcp_request_id`, `record.task_id`. The listener-thread formatter reads them as plain attributes. **Empty values are omitted from the JSON record** (no `null` noise).

## Event taxonomy

All event names are `<surface>.<verb>`. Filter friendly: `jq 'select(.event | startswith("run."))'`.

### `run.*` — bth run lifecycle (`runner.py`)

| event | fields |
|---|---|
| `run.start` | `run_uuid`, `script_path`, `script_sha256`, `argv`, `cwd`, `campaign_id?`, `agent_mode` |
| `run.subprocess_spawn` | `pid`, `cmd` |
| `run.heartbeat` | `pid`, `elapsed_ms` — emitted every 60 s while subprocess alive **only after wall-clock > 60 s** (cheap; multi-hour MD runs become visible) |
| `run.subprocess_exit` | `exit_code`, `duration_ms`, `stdout_bytes`, `stderr_bytes` |
| `run.parquet_written` | `path`, `bytes`, `duration_ms` |
| `run.error` | `phase` (validate/spawn/wait/persist), `exc_type`, `exc_msg` |

### `sidecar.*` — sidecar validation (`sidecar.py`, `validate.py`) — added per F4

| event | fields |
|---|---|
| `sidecar.parsed` | `path`, `sha256`, `outcomes` (label list), `kind` (experiment/benchmark/debug) |
| `sidecar.parse_error` | `path`, `exc_type`, `exc_msg` |
| `sidecar.validate_error` | `path`, `field`, `reason` (e.g. missing residual outcome, bad DuckDB condition) |

### `prereg.*` — agentic integrity gate (`prereg.py`) — added per F4

| event | fields |
|---|---|
| `prereg.gate_pass` | `script_path`, `sidecar_sha256`, `agent_mode` |
| `prereg.gate_deny` | `script_path`, `reason`, `agent_mode` — *single most valuable event for debugging agentic runs* |

### `postmortem.*` — postmortem ops (`postmortem.py`) — added per F4

| event | fields |
|---|---|
| `postmortem.validated` | `path`, `run_id?`, `sprint_id?` |
| `postmortem.validate_error` | `path`, `reason` |

### `campaign.*` — campaign mutations (`campaigns.py`) — added per F4

| event | fields |
|---|---|
| `campaign.create` | `campaign_id`, `name` |
| `campaign.conclude` | `campaign_id`, `verdict` |

### `lineage.*` — derived_from resolution (`runner.py`/lineage) — added per F4

| event | fields |
|---|---|
| `lineage.resolved` | `child_run_uuid`, `parent_run_uuid` |
| `lineage.resolve_error` | `child_run_uuid`, `derived_from`, `reason` |

### `mcp.*` — FastMCP tool calls (`mcp.py`)

| event | fields |
|---|---|
| `mcp.call_start` | `tool`, `request_id`, `arg_keys` (keys only; no values, to avoid leaking payloads) |
| `mcp.call_end` | `tool`, `request_id`, `duration_ms`, `ok`, `result_bytes` |
| `mcp.call_error` | `tool`, `request_id`, `exc_type`, `exc_msg`, `traceback` |

### `sync.*` — sync / remote / archive (`sync.py`, `remote.py`, `archive.py`)

| event | fields |
|---|---|
| `sync.rsync_start` | `direction` (push/pull), `remote`, `src`, `dst`, `filters` |
| `sync.rsync_progress` | parsed from `--info=progress2` stream — `bytes_transferred`, `files_transferred`, `pct`, `xfer_rate` |
| `sync.rsync_stall` | emitted if no progress line for 30 s while subprocess alive — `elapsed_since_progress_ms` |
| `sync.rsync_end` | `exit_code`, `duration_ms`, `bytes_transferred`, `files_transferred` |
| `sync.remote_test` | `remote`, `success`, `latency_ms`? (success only), `error`? (failure only) — SSH connectivity probe emitted by `remote.py:test_remote()` |
| `archive.export` | `partition`, `rows`, `duration_ms` |

Implementation change required (acknowledged per F3): `sync.py:94` switches from `subprocess.run(..., capture_output=True)` to `Popen` with `--info=progress2 --out-format='%i %n'` on stderr, streaming-parse. The existing 120 s kill switch becomes an honest hang detector via `sync.rsync_stall`.

### `catalog.*` — DuckDB / compaction / queries (`catalog.py`, `compact.py`, `query.py`)

| event | fields |
|---|---|
| `catalog.write_parquet` | `path`, `rows`, `duration_ms` |
| `catalog.compact_start` / `catalog.compact_end` | `cool_files`, `warm_rows_before`, `warm_rows_after`, `duration_ms` |
| `catalog.duckdb_lock_wait` | emitted if lock acquisition exceeds 500 ms — `waited_ms`, `db_path` |
| `catalog.query` | `query_kind` (ls/find/sql), `duration_ms`, `rows` |

## Schema

### Common envelope (every record)

| field | type | notes |
|---|---|---|
| `ts` | string | ISO 8601 UTC, microsecond precision |
| `level` | string | `debug` / `info` / `warning` / `error` |
| `pid` | int | process id |
| `tid` | int | thread id |
| `host` | string | `socket.gethostname()` — disambiguates cluster vs laptop |
| `surface` | string | `run` / `sidecar` / `prereg` / `postmortem` / `campaign` / `lineage` / `mcp` / `sync` / `catalog` / `telemetry` |
| `event` | string | `<surface>.<verb>` |
| `msg` | string | human-readable; may be empty |
| `span_id` | string? | present on `*.start` / `*.end` pairs |
| `run_uuid` | string? | contextvar; omitted if unset |
| `mcp_request_id` | string? | contextvar; omitted if unset |
| `task_id` | string? | from `$BTH_TASK_ID`; omitted if unset |

### Public schema doc

A user-facing copy lives at `docs/telemetry-schema.md` (under the public docs root) with:

- Common envelope table
- One subsection per event with field table and a worked example JSON line
- "Querying recipes": `tail -f | jq`, last-hour errors, duckdb `read_json_auto` snippet, hung-run detection (start without exit within N minutes), cross-process merge (`cat events.*.jsonl | jq -s 'sort_by(.ts)'`)

The recipes section makes a future `bth logs` subcommand cheap.

## Call-site integration plan

One surface per commit so each is independently revertable.

1. **`telemetry.py` + schema doc + tests.** Module, `JsonFormatter`, queue setup with `prepare()` override, contextvars, `event()`/`span()`. Tests: JSONL parses; **contextvars actually appear in listener-thread output** (regression for F2); rotation works per-process; queue listener shuts down cleanly on `atexit`; SLURM-array simulation (spawn 4 subprocesses writing concurrently, assert no interleaved lines).
2. **`runner.py`** — `span("run")`, set `run_uuid_var` at entry, emit `run.parquet_written` / `run.heartbeat` (and trigger `sidecar.parsed` from the sidecar load path in step 3). ~25 lines diff (heartbeat thread adds a few).
3. **`sidecar.py` / `prereg.py` / `validate.py`** — emit `sidecar.*` and `prereg.*` events from existing failure paths.
4. **`postmortem.py` / `campaigns.py` / lineage code** — emit one event per mutation/validation path.
5. **`mcp.py`** — `@traced_tool` decorator on every FastMCP tool; sets `mcp_request_id_var`, emits start/end/error. Init telemetry in MCP server entry point.
6. **`sync.py` / `remote.py` / `archive.py`** — refactor to `Popen` + `--info=progress2` stream-parse; emit `sync.*` events including the `sync.rsync_stall` hang signal.
7. **`catalog.py` / `compact.py` / `query.py`** — `span()` around DuckDB connect/execute; `catalog.duckdb_lock_wait` from try/except around `connect()` retry loop.
8. **`cli.py`** — call `init_telemetry()` once at Typer app startup.

### Testing

Per surface: spawn the operation, read back `events.<host>.<pid>.jsonl`, assert event sequence, contextvar correlation, and span pairing. No mocking — real files in `tmp_path`. SLURM-array correctness test under D6 is a hard requirement (must pass before merging the substrate commit).

## Configuration surface

`~/.bth/config.toml`:

```toml
[telemetry]
level = "info"
log_dir = ""             # empty = <catalog_dir>/logs/ (default; rides bth sync)
max_bytes = 10485760     # 10 MB per file
backup_count = 5
heartbeat_seconds = 60   # run.heartbeat cadence (after first 60 s wall-clock)
rsync_stall_seconds = 30 # sync.rsync_stall threshold
```

Env overrides: `BTH_LOG_LEVEL`, `BTH_LOG_DIR`, `BTH_TASK_ID`.

## Cluster / SLURM operation (F5 + F6)

- Per-process file naming `events.<hostname>.<pid>.jsonl` makes N parallel SLURM array tasks lockless and corruption-free.
- Default `log_dir` under `<catalog_dir>/logs/` means `bth sync` already ships cluster-side events to the laptop. No new sync code.
- `host` field on every record disambiguates which node produced an event after merge.
- Recipe in the public docs shows the canonical merge: `cat <catalog_dir>/logs/events.*.jsonl | jq -s 'sort_by(.ts)'`.

## Out of scope (explicit YAGNI)

- `bth logs` subcommand (deferred; schema + recipes make it additive)
- Joining events into the warm DuckDB catalog (no migration burden in this milestone)
- Per-surface log levels (single root level only)
- Redaction policy beyond MCP arg-key-only (no user data scrubbing framework)
- aiologger / structlog (rejected: see D1)

## Risks

| Risk | Mitigation |
|---|---|
| Queue listener thread dies silently | `atexit` flush + assert; if listener thread is dead at flush time, write a synchronous `telemetry.listener_died` record to **stderr** (never stdout — FastMCP uses stdout for protocol framing) |
| Log volume during long sync swamps disk | Per-file 50 MB cap (10 MB × 5 backups); `heartbeat_seconds` and `rsync_stall_seconds` configurable |
| ContextVar leakage across MCP requests | Each FastMCP tool invocation runs as its own `asyncio.Task` with a fresh context copy; `var.set` is automatically request-scoped (F8) |
| `JsonFormatter` chokes on non-serialisable fields | `json.dumps(default=repr)` fallback; emit `telemetry.serialise_error` warning |
| `BTH_LOG_DIR` non-writable | Fall back to `tempfile.gettempdir()`; emit one stderr warning at init |
| Future `multiprocessing` use breaks fork+threads | Spec forbids `fork` start method in `telemetry.py` docstring; lint rule deferred |

## Open questions

None at design time.
