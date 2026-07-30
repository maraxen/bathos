---
session_id: 3ec99885
topic: Structured gate error taxonomy in MCP tools for bathos (#793): replace natural language errors with typed payloads across all 22 MCP tool surfaces — defining error code enums per domain, a unified ErrorPayload schema, and consistent return contracts that agentic callers can route on
task_type: constrained-technical
winner: COMPOSITE: Single flat BathosErrorCode enum in new src/bathos/errors.py (GateErrorCode codes migrated/aliased) + plain dict envelope {"ok": bool, "error_code": str|None, "error": str|None, "resolution_hint": str|None, **data} + traced_tool centralizes all exception→BathosErrorCode mapping with explicit "first exception wins" policy + static RESOLUTION_HINTS registry keyed by BathosErrorCode + hard cut (no transition window, internal-only interface) + CI assertion that every exception type raised in the codebase has a registered BathosErrorCode (closes maintenance trap) + telemetry TOOL_ERROR event emitted by traced_tool unconditionally when ok=False
created_at: 2026-06-13T20:38:38.350015+00:00
---

# Brainstorm: Structured gate error taxonomy in MCP tools for bathos (#793): replace natural language errors with typed payloads across all 22 MCP tool surfaces — defining error code enums per domain, a unified ErrorPayload schema, and consistent return contracts that agentic callers can route on

## Problem Frame
Fixed constraints: (1) FastMCP is the framework — we cannot change MCP transport; Python exceptions that escape the tool function become opaque MCP protocol errors, so we must catch and shape internally. (2) The existing GateErrorCode enum and GateErrorPayload dataclass in prereg.py must remain the authoritative model for gate errors — no parallel taxonomy. (3) All 22 tools must return a consistent envelope — a caller cannot conditionally handle some tools differently. (4) No breaking changes to the bathos DB schema.

Negotiable: (a) whether there is ONE unified error code enum or per-domain enums that share a common base/prefix, (b) whether the envelope is a typed dataclass or a plain dict (FastMCP serializes both), (c) whether traced_tool centralizes all error shaping or tools handle their own domain errors and traced_tool handles only uncaught exceptions, (d) whether parameter validation moves to a shared validator or stays per-tool, (e) whether telemetry gets error_code emission in this sprint or is deferred.

## Idea Pool
- [user] PEGS mapping:
- [user] PROCESSES: (1) tool invocation → param validation → domain operation → response shaping → return. Today: validation is ad-hoc per-tool, shaping is bare {"error": str(e)}. Future: validation centralized, shaping typed.
- [user] EVENTS: tool succeeds; domain exception raised (GateError, CampaignError, SidecarError, CatalogError, ExportError); param validation fails; completely unexpected exception (division by zero, AttributeError etc).
- [user] GOALS: agentic callers can switch on error_code without parsing strings; error includes resolution_hint so the agent knows the next step; every tool returns an identical envelope shape regardless of success or failure; telemetry can record error_code.
- [user] STATES: success (ok=True, data=...), domain error (ok=False, error_code from domain enum, resolution_hint, human message), param error (ok=False, error_code=INVALID_PARAM), internal error (ok=False, error_code=INTERNAL, debug info).
- [user] Key tension: GateErrorCode already exists in prereg.py with 11 codes. CampaignError, SidecarError, CatalogError have no codes yet. Do we unify into one enum or have domain enums? Unified is simpler for callers (one import, one switch); domain enums preserve separation of concerns and match the existing exception hierarchy.
- [user] The existing idea pool covers the enum shape and envelope format well. Let me push into the angles not yet covered.
- [user] On telemetry: when an error_code is known at the point of exception catch, we have a choice — emit nothing and let the caller log it, OR have traced_tool emit a telemetry TOOL_ERROR event with the error_code inline. Proposal: traced_tool emits telemetry unconditionally on any caught exception, including the error_code string in the event payload. This means telemetry doesn't need a separate patch later — it's baked into the shaping layer. Concrete alternative: telemetry emission is opt-in per call site, passed as a flag to the exception mapper. What's the right coupling here?
- [user] On caller contract: two distinct models exist. Model A — optimistic callers: the contract says tools SHOULD return ok=True on normal paths, so callers can assert ok=True and treat ok=False as an error branch requiring agent remediation. Model B — explicit check required: the contract says callers MUST check ok on every call, and treating a missing ok-check as a bug. These have different implications for how we document the MCP surface and whether we add an "ok_or_raise" helper on the Python client side.
- [user] On parameter validation: option is a shared `validate_params(schema, kwargs)` function that all 22 tools call at entry, returning INVALID_PARAM with field-level detail. Alternative: a decorator `@require_params("run_id", "project")` that wraps the tool function. Alternative: parameter contracts live in FastMCP's own schema validation (reject at transport layer before tool body runs), but we lose the ability to return a shaped error — it becomes a protocol error instead.
- [ai] traced_tool emits telemetry TOOL_ERROR event unconditionally on any caught exception, including error_code in the event payload — telemetry coverage is baked into the shaping layer, no separate patch needed
- [ai] Telemetry error_code emission is opt-in per call site — passed as a flag or kwarg to the exception mapper in traced_tool, allowing tools that want richer context to pass domain-specific metadata
- [ai] Optimistic caller contract (Model A): tools SHOULD return ok=True on normal paths; callers can assert ok=True and treat ok=False as an agent remediation branch — contract enforced by documentation and integration tests
- [ai] Explicit-check caller contract (Model B): callers MUST check ok on every call; an "ok_or_raise" helper on the Python client side converts the dict to an exception for callers that prefer exception semantics
- [ai] Shared validate_params(schema, kwargs) function called at entry in all 22 tools, returning INVALID_PARAM error with field-level detail (missing field name, type mismatch) in the standard envelope
- [ai] @require_params("run_id", "project") decorator wraps the tool function and handles param validation before the tool body runs — validation contract is visible at the function signature level
- [user] Three more angles to push on.
- [user] On schema_version: GateErrorPayload in prereg.py carries a schema_version field. Proposal A: the unified envelope also carries schema_version (e.g., "1.0") — agentic callers can detect envelope format changes without parsing the payload structure. Proposal B: schema_version is omitted from the envelope — it adds noise on every call and envelope changes will be detectable from the error_code set changing anyway. Proposal C: schema_version lives in a separate MCP tool (bth_schema_version) that callers call once at session start to negotiate format.
- [user] On resolution_hint registry: right now resolution_hint is a free-form string per raise site. Proposal: define a static RESOLUTION_HINTS dict keyed by error code (e.g., BathosErrorCode.RUN_NOT_FOUND -> "Call bth_list_runs to enumerate valid run IDs") — hints are centralized, consistent, and testable. Callers can use them directly. Alternative: hints remain per-raise-site strings (more context-specific but inconsistent across callers of the same error code). Alternative: hints are omitted entirely from the envelope and documented externally in a hints.md file.
- [user] On backward compat: today callers get raw exception strings or bare {"error": str}. Proposal: a TRANSITION_MODE flag in config.py that lets the envelope emit both the old "error" key and the new "error_code" key simultaneously for one release cycle, then drop the old key. Alternative: no transition mode — it's an MCP-internal surface used only by agentic callers in this codebase, so breaking the contract on one release is acceptable. Alternative: version the MCP server (bth/v2/) and run both surfaces in parallel until callers migrate.
- [ai] Envelope carries schema_version field (e.g., "1.0") — agentic callers can detect envelope format changes without parsing payload structure; follows GateErrorPayload precedent
- [ai] schema_version omitted from envelope — adds noise on every successful call; envelope changes detectable from error_code set drift; simpler contract for callers
- [ai] Static RESOLUTION_HINTS registry dict keyed by error code (BathosErrorCode -> str) — hints are centralized, consistent, and testable; callers and traced_tool both pull from the same source
- [ai] resolution_hint remains per-raise-site string — more context-specific (can include the actual bad value, the valid range), less consistent across callers of the same code
- [ai] TRANSITION_MODE config flag: envelope emits both legacy "error" key and new "error_code" key simultaneously for one release cycle, then drops legacy key — gives agentic callers a migration window
- [ai] No backward compat transition — MCP tool surface is an internal agentic interface used only within this codebase; hard cut to new envelope on one release is acceptable; no dual-mode complexity
- [user] converge

## Decision Log
- [DEFER] traced_tool centralizes exception→error_code mapping via dispatch table: [CRITIC] idea=traced_tool-dispatch lens=feasibility finding=FastMCP wraps tool functions as async coroutines; if traced_tool is a sync decorator wrapping an async tool body, exception propagation works cleanly — but if any tool uses FastMCP's Context injection (common for logging/progress), the wrapper must also thread the Context argument through, which is non-trivial without introspection of the wrapped function's signature. No evidence the current mcp.py tools use Context injection, but this is an unverified assumption. severity=MAJOR — must audit all 22 tool signatures before claiming traced_tool is a drop-in wrapper.
- [REJECT] traced_tool handles exception dispatch when a tool raises multiple domain exceptions: [CRITIC] idea=traced_tool-dispatch lens=edge-cases finding=When a tool raises two domain exceptions in sequence (e.g., SidecarError during param validation, then CatalogError during write — possible in bth_run_submit which touches both), the outer try/except in traced_tool catches only the FIRST exception to escape the stack frame; the second is swallowed and its error_code is lost. This is not a theoretical edge case: any tool that does multi-step work (validate → read catalog → write campaign) can hit two distinct domain error classes. The composite has no mechanism for chained error codes or partial failure reporting. severity=MAJOR — tools with multi-step operations need explicit ordering and early-exit discipline, or the composite must define a "first exception wins" policy explicitly and enforce it.
- [DEFER] exceptions raised inside list comprehensions or async callbacks inside tool bodies: [CRITIC] idea=traced_tool-dispatch lens=edge-cases finding=Exceptions raised inside list comprehensions propagate normally to the enclosing frame and will be caught by traced_tool — this is fine. However, exceptions raised inside async callbacks (e.g., if a tool uses asyncio.gather or schedules sub-tasks) will NOT be caught by a synchronous try/except wrapper; they surface as unhandled task exceptions and become opaque MCP protocol errors. If any of the 22 tools use asyncio.gather internally (plausible for catalog + telemetry parallel writes), traced_tool's catch block will never see the exception. severity=MAJOR — needs an explicit audit of async usage patterns in mcp.py; if any tool is async internally with sub-tasks, the wrapper needs asyncio-aware exception handling (e.g., return_exceptions=True on gather + manual inspection).
- [ACCEPT] hard cut (no backward compat window) for existing callers of bare {"error": str} envelope: [CRITIC] idea=hard-cut lens=user-impact finding=The session frame confirms this is an MCP-internal surface used only by agentic callers within this codebase. However, "agentic callers in this codebase" is vague — the sprint-audit tool, the orchestration skill, and any external agent sessions that have cached tool schemas all parse the current envelope shape. If ANY of those callers have hardcoded checks for the "error" key (e.g., `if result.get("error"):`) rather than `if not result.get("ok"):`, the hard cut silently breaks their routing logic without a test failure. The composite must include a grep of all callers before shipping. That said, the complexity cost of TRANSITION_MODE (idea seq:24) is real, and for a codebase-internal interface the hard cut is defensible IF caller audit is done first. Verdict: accept the hard cut approach, but only contingent on a confirmed caller audit. severity=INFO — the risk is real but bounded and checkable.
- [REJECT] per-domain error code enums (CatalogErrorCode, CampaignErrorCode, SidecarErrorCode) alongside existing GateErrorCode: [CRITIC] idea=per-domain-enums lens=implementation-cost finding=22 tools across 4+ domains means traced_tool's dispatch table must enumerate `except CatalogError as e: code = CatalogErrorCode.from_exception(e)`, `except CampaignError as e: code = CampaignErrorCode.from_exception(e)`, etc. for each domain. This is O(domains) branching in the wrapper. Worse: when a new domain is added (PostmortemError exists today in postmortem.py), the developer must touch traced_tool, add a new enum class, and wire the mapping — three separate files for one new domain. The runner-up (single flat BathosErrorCode) requires only one file change per new error case. The per-domain approach does NOT improve callers: a caller switching on error_code receives a string (e.g., "CATALOG_RUN_NOT_FOUND"), so whether that string comes from CatalogErrorCode or BathosErrorCode is invisible to the caller. The separation-of-concerns benefit is code-organizational only, not caller-visible. The maintenance trap identified in the forcing beat is compounded by having 4 enums to keep consistent rather than 1. severity=MAJOR — the per-domain approach adds 3x the maintenance surface of the flat enum for zero caller-visible benefit; the runner-up should be reconsidered.
- [REJECT] reversibility: migrating from per-domain enums to unified flat enum post-ship: [CRITIC] idea=per-domain-enums lens=reversibility finding=Error code strings emitted in the envelope (e.g., "CATALOG_RUN_NOT_FOUND" vs "RUN_NOT_FOUND") will be consumed by agentic callers and potentially logged in telemetry JSONL records. If we later flatten the taxonomy, callers that matched on "CATALOG_RUN_NOT_FOUND" break silently (they fall through to unhandled), and historical telemetry records become non-queryable with the new codes. This is a de facto breaking change requiring a second hard cut. The per-domain approach therefore trades complexity now for a likely painful unification later. The flat enum (runner-up) has no such reversibility problem — adding a domain prefix later is purely additive. severity=MAJOR — the leading composite locks in a reversal cost that the runner-up avoids entirely.
- [ACCEPT] static RESOLUTION_HINTS registry keyed by error code: [CRITIC] idea=RESOLUTION_HINTS-registry lens=implementation-cost finding=The registry is a plain dict — low implementation cost, high testability. The only risk is staleness: if a new error code is added without a corresponding hint entry, the envelope silently emits resolution_hint=None. This is not catastrophic (callers must handle null hints anyway) but degrades the agentic UX for new error types. A missing-hint lint check (CI asserts every enum member has a registry entry) would close this gap at minimal cost. Verdict: accept as specified with the lint check noted as a required follow-on. severity=INFO.
- [REJECT] Per-domain enums (CatalogErrorCode, CampaignErrorCode, SidecarErrorCode): No caller-visible benefit over flat enum; callers dispatch on the string value in the dict, not the Python type. 3× maintenance surface. If ever unified, telemetry JSONL codes are a de facto second hard cut. Architectural purity argument is already violated by GateErrorCode living in prereg.py rather than a domain-neutral errors.py.

## Assumptions

| ID | Assumption | Risk if wrong |
|----|-----------|---------------|
| A-1 | All 22 `@app.tool` functions are wrapped by `traced_tool`; any un-decorated helpers that raise will be decorated in this sprint | Missing coverage — exceptions escape as opaque MCP errors |
| A-2 | No external consumers parse the bare `{"error": "string"}` envelope — confirmed by caller audit (AC-8) | Hard cut breaks external callers silently |
| A-3 | `GateErrorCode` values are `str` enum members with lowercase snake_case `.value` strings (e.g. `GateErrorCode.SIDECAR_MISSING.value == "sidecar_missing"`); `BathosErrorCode` aliases them with the same `.value` strings | Telemetry records and callers matching on string values would break |
| A-4 | FastMCP serializes plain Python `dict` return values without modification | Envelope arrives at caller exactly as constructed |
| A-5 | Tools currently returning `json.dumps()` strings must be converted to `dict` returns AND their inner `except Exception` catch-all blocks must be removed — both are prerequisites before AC-3 can be implemented | If inner catches remain, `traced_tool` never sees domain exceptions; if json.dumps remains, `**data` spread fails at runtime |
| A-6 | `PostmortemError` does not exist as a named exception class — `postmortem.py` raises bare `ValueError` (lines 43, 52, 55). Postmortem validation failures therefore map to `INVALID_PARAM` under the coarse-code policy in TBD-2. No `POSTMORTEM_ERROR` code needed for v1. | If a `PostmortemError` class is added post-ship, it will get `INTERNAL` until AC-7 CI test fails and triggers a code addition |

## TBDs

| ID | Open Question | Blocking? | Resolution |
|----|--------------|-----------|-----------|
| TBD-1 | ~~Do any of the 22 tools inject a FastMCP `Context` parameter?~~ | ~~Blocks AC-3 if yes~~ | **CLOSED** — grep of `src/bathos/mcp.py` finds zero `Context` parameters in any tool signature. `traced_tool` rewrite requires no special Context threading. |
| TBD-2 | Subtype granularity of `CatalogError`, `CampaignError`, `SidecarError` — v1 maps each class to one code (`CATALOG_ERROR`, `CAMPAIGN_ERROR`, `SIDECAR_ERROR`, `EXPORT_ERROR`); finer per-subtype codes deferred | Not blocking — coarse codes acceptable for v1 | File follow-on backlog item post-ship if callers need finer dispatch |
| TBD-3 | `bth repair` tool has a separate `# repair() raises SystemExit(1)` path (mcp.py:1340). `traced_tool` must handle `BaseException` (not just `Exception`) for this, or the SystemExit is re-raised as an MCP transport error | Blocks AC-3 for the repair tool path only | Add explicit `except SystemExit` → `INTERNAL` branch in `traced_tool`; confirm SystemExit at repair path maps to `INTERNAL` |

## Pre-mortem Record

**Pre-mortem:** The implementation succeeded technically but failed in practice for two reasons.

First: the CI assertion that "every exception type has a registered BathosErrorCode" was never written — it was in the spec but the fixer agent omitted it. The maintenance trap remained open. Three months later a new `PostmortemError` was added without a corresponding code, and agentic callers started receiving `INTERNAL` for postmortem validation failures with no resolution hint.

Second: the plain dict envelope had no runtime enforcement. Tools occasionally returned `{"ok": True, "data": ...}` without the `error_code` key (permitted to be `None` on success, but some tools omitted it entirely), and callers could not distinguish "no error" from "forgot to include the field."

**Mandatory mitigations built into ACs:**
- (a) The CI maintenance-trap assertion is AC-7 — a concrete test in `tests/test_errors.py`, not a note.
- (b) The envelope test invariant is AC-5/AC-6: all four keys (`ok`, `error_code`, `error`, `resolution_hint`) must be present on both success and failure paths; `error_code=None` on success is explicit, not absent.

## Acceptance Criteria

**Given** the Fixed Constraints:
1. FastMCP is the transport layer — Python exceptions that escape a tool function become opaque MCP protocol errors; all shaping must happen internally.
2. `GateErrorCode` and `GateErrorPayload` in `prereg.py` remain authoritative for gate errors — `BathosErrorCode` aliases their string values; they are not replaced.
3. All 22 MCP tools return a consistent envelope — no per-tool conditional handling.
4. No bathos DB schema changes.

---

**AC-1 — BathosErrorCode enum exists in `src/bathos/errors.py`**

_When_ `from bathos.errors import BathosErrorCode` is executed,

_Then_:
- The module exists at `src/bathos/errors.py` with no other module exports in this sprint
- `BathosErrorCode` is a `str`-subclassing `Enum` with exactly 16 members:
  - 11 aliased from `GateErrorCode` (same `.value` strings, verified against `prereg.py:21-33`):
    `sidecar_missing`, `sidecar_invalid`, `sidecar_hash_mismatch`, `not_first_of_kind`, `manifest_write_failed`, `adversarial_check_missing`, `hypothesis_lock_missing`, `outcome_evaluation_error`, `result_schema_mismatch`, `outcome_ambiguous`, `internal`
  - 5 new codes for domain exceptions not covered by GateErrorCode:
    `catalog_error`, `campaign_error`, `sidecar_error`, `export_error`, `invalid_param`
- `BathosErrorCode.SIDECAR_MISSING.value == GateErrorCode.SIDECAR_MISSING.value` (wire-compatible alias)
- `BathosErrorCode.INTERNAL` and `GateErrorCode.INTERNAL` share the same `.value == "internal"`

---

**AC-2 — `RESOLUTION_HINTS` registry is complete and tested**

_When_ `from bathos.errors import RESOLUTION_HINTS` is executed,

_Then_:
- `RESOLUTION_HINTS` is a `dict[BathosErrorCode, str]`
- Every member of `BathosErrorCode` has a non-empty entry in `RESOLUTION_HINTS`
- The 11 aliased entries copy the corresponding strings from `prereg._RESOLUTION_HINTS` (same text, keyed on `BathosErrorCode` members, not `GateErrorCode` members)
- `tests/test_errors.py::test_resolution_hints_complete` asserts `set(BathosErrorCode) == set(RESOLUTION_HINTS)` and **fails** (not warns) if any member is missing

---

**AC-3 — `traced_tool` catches and shapes all exceptions**

_Prerequisite (must ship in the same PR)_:
1. All inner `*_tool` function catch-all blocks (`except Exception as e: return json.dumps({"error": str(e)})`) are removed from the 22 tool bodies, so domain exceptions propagate to `traced_tool`.
2. All tool return sites are converted from `json.dumps(...)` string to `dict` (prerequisite in A-5).
3. Gate failures from `run_tool` arrive as returned dicts (`runner.py:258-260` serializes `GateErrorPayload` via `dataclasses.asdict`); `run_tool` must detect `not result.get("ok", True)` in the gate-check return and re-raise as `GateError(payload=...)` OR reshape the dict inline into the new envelope — fixer's choice, but must be explicitly handled, not silently passed through.

_When_ a tool wrapped by `traced_tool` raises any exception,

_Then_:
- The exception is caught inside `traced_tool` — it never propagates to the FastMCP transport layer
- The dispatch order is: `GateError` → `CatalogError` → `CampaignError` → `SidecarError` → `ExportError` → `SystemExit` → `BaseException` (first match wins; subsequent exceptions from the same call are not reported)
- The return value is a `dict` with the four mandatory keys (`ok`, `error_code`, `error`, `resolution_hint`) plus any tool-specific data keys as a flat merge; if tool data keys collide with the four mandatory keys, the mandatory keys win (see M-2 note below)
- Validation tools (`postmortem_validate`, `validate_sidecar`) that use `ok` for their own validation status must have that field renamed to `validation_ok` as part of the same PR, to avoid collision

**M-2 collision policy (explicit):** Tools that currently return a top-level `ok` key with validation-specific semantics (e.g., `postmortem_validate` at mcp.py:1038, `validate_sidecar` at mcp.py:1116) must rename their existing `ok` field to `validation_ok` in this sprint. The transport `ok` key is reserved exclusively for the envelope's "did the tool call succeed" boolean.

---

**AC-4 — `TOOL_ERROR` telemetry event emitted on every `ok=False` return**

_When_ `traced_tool` shapes an exception into an `ok=False` envelope,

_Then_:
- A telemetry event is emitted via `telemetry.event()` before `traced_tool` returns
- The event name follows the existing dotted-lowercase convention used in `telemetry.py` — use `"mcp.tool_error"` (not `"TOOL_ERROR"`)
- The event payload includes: `tool_name` (str), `error_code` (str — the `BathosErrorCode` value), `error_class` (str — the exception class name)
- The emission is unconditional — all 22 tools, all `ok=False` paths
- Note: AC-4 is only fully satisfied after AC-3's prerequisite (remove inner catches) is complete; until then, tools that swallow exceptions internally do not reach `traced_tool` and therefore do not emit `mcp.tool_error`

---

**AC-5 — Envelope shape on success**

_When_ any of the 22 MCP tools returns successfully (`ok=True`),

_Then_:
- The return value is a plain `dict` (not a `json.dumps()` string)
- The dict contains all four mandatory keys: `ok=True`, `error_code=None`, `error=None`, `resolution_hint=None`
- Additional tool-specific keys may be present as a flat merge alongside these four
- `tests/test_mcp_envelope.py::test_success_envelope_has_all_four_keys` asserts this invariant for at least one representative tool and checks key presence (not absence) — i.e., `assert all(k in result for k in ["ok", "error_code", "error", "resolution_hint"])`

---

**AC-6 — Envelope shape on error**

_When_ any of the 22 MCP tools returns an error (`ok=False`),

_Then_:
- The return value is a plain `dict` containing all four mandatory keys: `ok=False`, `error_code` (non-null `str` whose value is a member of `BathosErrorCode`), `error` (non-null `str`), `resolution_hint` (non-null `str` from `RESOLUTION_HINTS`)
- `tests/test_mcp_envelope.py::test_error_envelope_has_all_four_keys` asserts this by injecting a forced `CatalogError` exception through `traced_tool` and checking all four keys

---

**AC-7 — CI maintenance-trap assertion (AST-based, scoped to domain exceptions)**

_When_ the test suite runs `tests/test_errors.py::test_every_domain_exception_has_registered_code`,

_Then_:
- The test uses `ast.walk` to scan `.py` files under `src/bathos/` **excluding `cli.py`** (CLI control-flow exceptions are out of scope for the MCP error taxonomy)
- It collects all distinct exception class names that appear in `raise <ExceptionName>(...)` nodes where `ExceptionName` is NOT a Python builtin (`ValueError`, `RuntimeError`, `KeyError`, `NotImplementedError`, `TypeError`, `AttributeError`, `IndexError`, `SystemExit`) and NOT `typer.Exit`
- It asserts that every collected non-builtin exception class name has a corresponding entry in a hardcoded `EXCEPTION_TO_CODE` registry dict in `errors.py` mapping class name → `BathosErrorCode`
- The test fails (not warns) on any unmapped non-builtin exception class
- Builtin exceptions (`ValueError`, `RuntimeError`, etc.) are not subject to this assertion — they implicitly map to `INTERNAL` or `INVALID_PARAM` at `traced_tool` dispatch time via the `BaseException` catch-all branch
- This test runs in CI on every push

---

**AC-8 — Caller audit pre-ship gate**

_When_ the implementation is ready to merge,

_Then_:
- A grep of all `.py` files under `src/bathos/` for the pattern `\.get\("error"\)` (the old-envelope check pattern) finds zero matches that are NOT inside a function that constructs the old envelope (i.e., no consumer call sites remain)
- The inner `*_tool` functions that previously constructed `{"error": str(e)}` are all gone (they were removed as part of AC-3's prerequisite) — this audit verifies removal
- This grep is documented in the PR description as a manual pre-merge checklist step; CI does not need to run it automatically
