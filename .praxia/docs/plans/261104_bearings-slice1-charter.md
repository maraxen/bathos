---
title: 'Bearings v1 slice 1: charter and confirmation'
description: 'Build plan for Bearings slice 1 (AC-1, AC-2, AC-3 ext half): charter file, human-only hash-bound confirmation, purpose-ledger pivot, folds; dispatch brief for agy.'
status: active
task_id: 261104_multiscale-goal-awareness
date: '261104'
sprint: ''
backlog_ids: ''
---
# Bearings v1 slice 1: charter and confirmation

Spec: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md`, Design items 1 to 3. This slice delivers AC-1, AC-2 and the `ext` half of AC-3. The owner's build plan (spec Decision Log, `[OWNER 261104] Build plan`) runs three staged slices, each ending with tests and a review gate.

## Out of this slice

- The `atom:` half of AC-3. It needs `bth bearings record`, which is slice 2, and moves to slice 2's acceptance criteria.
- Drafting a charter from the record. That needs the packet (slice 2). In this slice, `bth charter draft` only re-emits or scaffolds (see C4).
- Anything under `bth bearings`, the due notice, and AC-7.

## Decisions this plan settles (the spec left them open)

| # | Question | Decision |
|---|---|---|
| P1 | Is `bth charter pivot` chair-gated? | **No.** Agents may pivot. A pivot is advisory until the owner confirms the resulting charter (Design 3), and the confirm step is the gate. |
| P2 | Does pivot change the file? | **Yes.** It sets `state = "retired"` and `retired_reason` on each retired purpose in `.bth/charter.toml`, then appends the event. Editing the file unconfirms the charter by construction (the sha256 changes), so the owner must re-confirm. |
| P3 | Does pivot need a confirmed charter? | **No.** It needs a charter file. The live purposes are those marked `live` in the file as it stands. The event records `charter_sha256_before`, plus `confirmed_before` (bool). |
| P4 | Mandate immutability | `bth charter confirm` refuses (`CHARTER_MANDATE_CHANGED`) when the mandate text differs from the last confirmed mandate, unless `--amend-mandate` is passed. That flag is chair-gated like every confirm, and the event records the old and new mandate sha256. |
| P5 | What does "byte-identical `ext`" mean? | Canonical JSON (`json.dumps(ext, sort_keys=True, separators=(",", ":"))`) of each purpose's parsed `ext` table is equal after parse, after event fold, and after draft re-emit. Value types are kept (int, float, bool, str, list, nested table). TOML file bytes are **not** required to match. |
| P6 | What is the sha256 taken over? | The raw bytes of `.bth/charter.toml`, read in binary. |
| P7 | MCP surface | MCP mirrors `charter_status`, `charter_draft` and `charter_pivot`. MCP `charter_confirm` exists only to refuse, unconditionally (`CHARTER_AGENT_CONFIRM_REFUSED`), the way `stepback_resolve` refuses Level 3 (`mcp.py` ~6074). MCP callers are agents. |

## Components

**C1. A shared human-chair helper** in `src/bathos/stepback/escalation.py`: `require_human_chair(*, interactive_tty: bool | None = None, chair_token: str | None = None) -> str`. It raises if `is_agent_environment()` is true and no valid chair token is present, or if there is neither a TTY nor a valid token. It returns the mode used (`"tty"` or `"token"`). Refactor the escalation transition's two inline checks to call it, keeping their exception types and messages so the existing step-back tests stay green. Step-back behaviour must not change.

**C2. `src/bathos/charter.py`** (new). It contains:
- the dataclasses `Charter(mandate: str, purposes: dict[str, Purpose])` and `Purpose(id, text, state, retired_reason, ext: dict)`;
- `parse_charter(path)`, using `tomllib`, with validation: `state` must be `live` or `retired`, and `retired_reason` is required when retired;
- `dump_charter(charter) -> str`, a deterministic TOML writer: purposes sorted by id, `ext` emitted as `[purpose.<id>.ext]` with nested tables. Check whether `tomli_w` is already a dependency in `pyproject.toml` before writing by hand. **Do not add a dependency.**
- `charter_sha256(path)`;
- `confirm(project_root, *, amend_mandate=False, interactive_tty=None, chair_token=None)`;
- `pivot(project_root, *, reason, dispositions: dict[str, Literal["reaffirm", "retire"]], retired_reasons: dict[str, str], cheapest_next_test: str)`, where `cheapest_next_test` must match `executed:<run>`, `cancelled:<reason>` or `none_pending`;
- `status(project_root) -> {"state": "confirmed" | "unconfirmed" | "never_confirmed", "current_sha256", "last_confirmed_sha256", "confirmed_at"}`;
- `draft(project_root) -> Path`.

**C3. Events and fold.**
- `charter.confirmed`, emitted through `bathos.runlog.emit.emit_event`. Entity `[<project slug>]`. Data: `charter_sha256`, `mandate_sha256`, `mandate_text`, `purposes` (the full parsed purposes, `ext` included), `auth_mode`, and `amended_mandate_from` (sha256 or null).
- `charter.pivot`. Entity `[<project slug>]`. Data: `reason`, `dispositions`, `retired_reasons`, `cheapest_next_test`, `charter_sha256_before`, `charter_sha256_after`, `confirmed_before`.
- `src/bathos/runlog/fold_charter.py` (new), modelled on `fold_output_copies.py`. Add `CHARTER_KINDS`, and fold functions into two index tables:
  - `charter_confirmations(project, eid, ts, charter_sha256, mandate_sha256, mandate_text, purposes_json, auth_mode, amended_mandate_from)`;
  - `charter_pivots(project, eid, ts, reason, dispositions_json, retired_reasons_json, cheapest_next_test, charter_sha256_before, charter_sha256_after, confirmed_before)`.
  - Both are append-only, one row per event, idempotent on `eid`.
- Wiring:
  - a dispatch branch in `src/bathos/runlog/ingest.py` beside the `_OUTPUT_COPY_KINDS` branch (~line 1213);
  - DDL in `src/bathos/index.py` beside `output_copies` (~line 314), including the empty-view fallback (~lines 500 to 507), so an existing `index.db` without the tables still opens.
- `status()` reads the latest `charter_confirmations` row for the project through the same read path other folded tables use, after ingest. In log mode a write is visible after its own ingest step. Follow how `output_copies` readers do it.

**C4. `bth charter draft`** writes `.bth/charter.draft.toml`. It never touches `.bth/charter.toml`.
- With an existing charter, it re-emits the charter through `dump_charter(parse_charter(...))`, keeping every purpose and `ext`.
- Without one, it writes a scaffold: an empty mandate with a comment saying the owner must fill it in verbatim, and one example purpose that is commented out.
- It prints that a draft has no standing until it is copied to `charter.toml` and confirmed.

**C5. CLI and errors.**
- `charter_app = cyclopts.App(name="charter")` in `src/bathos/cli_cyclopts.py`, modelled on `stepback_app` (~line 1940). Subcommands: `draft`, `confirm [--amend-mandate]`, `status`, and `pivot --reason ... --reaffirm ID ... --retire ID=REASON ... --cheapest-next-test ...`.
- `--project-root` defaults to the resolved workspace.
- Exit non-zero on every refusal.
- New `BathosErrorCode` members in `src/bathos/errors.py`, each with a `RESOLUTION_HINTS` entry:
  - `CHARTER_NOT_FOUND`
  - `CHARTER_INVALID`
  - `CHARTER_PIVOT_INCOMPLETE` (the message lists the undisposed live purposes)
  - `CHARTER_MANDATE_CHANGED`
  - `CHARTER_AGENT_CONFIRM_REFUSED`
  - `CHARTER_UNAUTHORIZED`

**C6. MCP**, in `src/bathos/mcp.py`, per P7.

## Acceptance tests

New files: `tests/test_charter.py` and `tests/runlog/test_fold_charter.py`. Every test sets `BTH_CATALOG_DIR` and `HOME`-dependent paths to `tmp_path`, and **must never touch the real `~/.bth`** (the repo guard enforces this under pytest; do not bypass it). The chair token comes from a 0600 file at `BTH_CHAIR_TOKEN_PATH` in `tmp_path`.

**AC-1**
- With `CLAUDE_AGENT=1` and no chair token, `confirm` exits non-zero, raises `CHARTER_UNAUTHORIZED` or the agent-refusal code, and **no `charter.confirmed` event is appended** (count the events in the log).
- Success path: monkeypatch away every var in `AGENT_ENV_VARS` (the agy session itself sets `ANTIGRAVITY_AGENT`) and pass `interactive_tty=True` **or** a valid token. One event is appended and folds into `charter_confirmations`.
- After confirm, edit the file. `status` reports `unconfirmed` and gives `last_confirmed_sha256` equal to the pre-edit sha256.
- A changed mandate without `--amend-mandate` is refused. With the flag, the event records `amended_mandate_from`.
- MCP `charter_confirm` always refuses.
- A step-back regression: `tests/test_stepback_escalation.py` passes unchanged.

**AC-2**
- With a charter of live purposes P1 and P2, a pivot naming only P1 is refused with `CHARTER_PIVOT_INCOMPLETE`, and the message contains `P2`. No event is appended and the file is unchanged.
- A pivot naming both, with a valid `cheapest_next_test`, appends exactly one `charter.pivot` event that folds into `charter_pivots`. The retired purpose has `state = "retired"` and its reason in the file, and `status` is now `unconfirmed`.
- A malformed `cheapest_next_test` is refused.

**AC-3, `ext` half**
- `[purpose.p1.ext]` holds arbitrary keys: nested table, list, int, float, bool, and a string with unicode.
- The P5 canonical JSON is equal for: the parsed charter, `purposes_json` from the folded `charter_confirmations` row, and `parse_charter(draft output)`.

## Commands (narrow only)

```
uv run pytest tests/test_charter.py tests/runlog/test_fold_charter.py tests/test_stepback_escalation.py tests/test_stepback_mcp.py tests/test_stepback_cli.py tests/test_errors.py -q
uv run ruff check src/bathos/charter.py src/bathos/runlog/fold_charter.py src/bathos/stepback/escalation.py src/bathos/errors.py src/bathos/cli_cyclopts.py src/bathos/mcp.py src/bathos/index.py src/bathos/runlog/ingest.py tests/test_charter.py tests/runlog/test_fold_charter.py
```

**Never run the whole suite** (`uv run pytest` with no paths, or `pytest tests/`). It exhausts this machine's memory.

## Claimed files (exactly these)

```
src/bathos/charter.py                 (new)
src/bathos/runlog/fold_charter.py     (new)
src/bathos/runlog/ingest.py
src/bathos/index.py
src/bathos/stepback/escalation.py
src/bathos/errors.py
src/bathos/cli_cyclopts.py
src/bathos/mcp.py
tests/test_charter.py                 (new)
tests/runlog/test_fold_charter.py     (new)
```

## Review gate before slice 2

1. The orchestrator reads `admission`, then `claim_verified`.
2. The orchestrator reruns the narrow commands above.
3. The orchestrator reads the diff.
4. A Sonnet reviewer checks the C1 refactor (does step-back behaviour match?) and the AC tests (can each one fail?).
5. Slice 2 starts only after that.
