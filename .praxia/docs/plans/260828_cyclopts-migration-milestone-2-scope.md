---
title: Cyclopts CLI migration — Milestone 2 scope (backlog #4702)
category: plans
task_id: 260828_cisternal_cyclopts_adapter
status: proposed
---

# Cyclopts CLI migration — Milestone 2 scope

## Where Milestone 1 left off

Milestone 1 (PR: cisternal#33, bathos commit `4c5f8f9b` on `feat/provenance-cisternal-shim`) proved the pattern end to end for the `campaign` group (7 commands) via a preview entry point (`bth-campaign-preview`), without touching the shipped `bth` binary. It also shipped two pieces of durable infrastructure Milestone 2 builds directly on:

- `tests/_cyclopts_runner.py` — the cyclopts equivalent of `typer.testing.CliRunner`.
- `scripts/analysis/migrate_cli_to_cyclopts.py` — parses `cli.py`'s typer surface + `bathos.mcp`'s registry, fuzzy-joins them, and emits `scripts/analysis/cli_migration_map.toml`. Current output: **56 verified, 22 no_mcp_equivalent, 0 needs_review** (of 78 total commands, 7 already done as the campaign pilot).

## The one real risk Milestone 1 uncovered — now resolved

The codegen's `inner_fn` field is read from the MCP (`registry="bathos"`) partition, which is almost always the `async def mcp_x_tool(...)` wrapper `@traced_tool` decorates — **not** the plain sync function CLI wiring needs (`wire()`'s CLI path calls the target directly, not via `asyncio.run`; pointing it at an async function silently returns an unawaited coroutine instead of running). The campaign pilot's 7 commands split 6-plain-delegate / 1-needs-extraction.

**Resolved (260828):** `migrate_cli_to_cyclopts.py` now runs an AST-based audit — for each verified row's `inner_fn`, it checks whether it's `async def` and, if so, whether its body is a single `return <plain_fn>(...)` delegation to a non-async top-level function (confirmed pattern: `mcp_list_runs_tool`/`mcp_verify_tool`/`mcp_check_tool` and 6 of 7 campaign commands). Spot-checked against real ground truth (`gate_stamp`'s genuinely inline logic, `reference_get`'s genuinely inline async body, `claim_register`'s correctly-resolved private delegate `_claim_register_sync`) — accurate. Result, of the 49 remaining registry-drivable commands: **32 need direct decoration, 17 need extraction** (a ~65/35 split, more extraction-heavy than the pilot's 6:1 but fully known rather than estimated). Per-group breakdown in the table below.

## Batching

### Registry-driven batches (need the `@cisternal.tool(registry="bathos-cli", ...)` treatment, same pattern as campaign)

| Batch | Commands | Direct | Extraction | Notes |
|---|---|---|---|---|
| Top-level singletons | `run`, `check`, `sync`, `verify`, `lint`, `compact`, `init`, `repair`, `ls`, `find`, `show`, `sql`, `archive`, `archive-artifact`, `restore` (direct) + `cite`, `lineage`, `new-experiment`, `validate-sidecar` (extraction) | 14 | 5 | No group-mounting logic needed (`cli_group=None`) — cheapest batch to start with. |
| `anchor` | `figure-register`, `find`, `get`, `insert` | 4 | 0 | All 4 already delegate to a plain sync fn. |
| `attestation` | `register`, `scaffold`, `validate` | 3 | 0 | All 3 already delegate. |
| `blast-radius` (partial) | `assess`, `clear` | 2 | 0 | Both already delegate. The other 3 blast-radius commands (`install-hook`, `shadow-check`, `uninstall-hook`) are CLI-only — see below. |
| `outputs` | `list`, `summary` | 2 | 0 | Both already delegate. |
| `query` | `attestation`, `blast-status`, `candidates`, `figures`, `resolve-pin`, `trust-state` | 6 | 0 | All 6 already delegate. `shadow-log` (7th query command) is CLI-only, see below. |
| `claim` | `author`, `register`, `scaffold`, `validate` (all extraction) | 0 | 4 | `claim author` also takes the one non-flat argument in the whole 62-tool registry (nested `ClaimPayload` Pydantic model) — see the dedicated item below. `claim register` was corrected from the codegen audit's "direct" classification during implementation (260828) — see below. |
| `gate` | `stamp`, `status` | 0 | 2 | Both have business logic inline on the async MCP function — no delegate exists. |
| `postmortem` | `scaffold`, `show`, `validate` | 0 | 3 | All 3 have inline async business logic. |
| `ref` | `applicable`, `list`, `search`, `show` | 0 | 4 | All 4 have inline async business logic. |

**49 commands total: 31 direct decoration, 18 need extraction** (revised 260828 from the codegen audit's original 32/17 split — `claim register` was reclassified during implementation, see the correction note below). Recommended order follows the table — highest direct-ratio batches first (top-level, anchor, attestation, blast-radius, outputs, query = 31 direct / 5 extraction combined), saving the fully-extraction-heavy batches (`claim`, `gate`, `postmortem`, `ref` = 13 commands, all extraction) for last since each needs a genuinely new plain sync function written and reviewed, not just a decorator pasted on.

**Correction (260828, during implementation): `claim register`'s codegen classification was wrong.** The audit's `ast.walk`-based delegate scan found `claim_register`'s trailing `return _claim_register_sync(...)` and flagged it "direct" — but `claim_register`'s async body resolves `catalog_dir`/`workspace_root` (env-var fallback, live workspace resolution) *before* that return, and `_claim_register_sync` itself requires already-resolved `Path` arguments with no defaults. `ast.walk` finds a `Return` node anywhere in the body regardless of what precedes it; it doesn't verify the whole body is a bare delegation. Decorating `_claim_register_sync` directly would have silently dropped the CLI's env-var/auto-resolution behavior. Treated as an extraction instead (`claim_register_tool`, mirroring the other 3 `claim` commands) — see its docstring in `src/bathos/mcp.py` for the full explanation. This is a real limitation of the codegen script's heuristic, not yet fixed there; the other 30 "direct" rows were spot-checked via passing tests and are unaffected, but a future full re-audit of every remaining "direct" row before decorating it is worth doing rather than trusting the classification blindly.

**Design decision (260828): `claim author`'s CLI mapping.** Resolved by looking at the *already-shipped* Typer `claim_author_cmd` (`cli.py:1121`), which never flattened `ClaimPayload` into individual flags — it already takes `--from-json <file-or-'-'>` and passes a raw dict straight to `author_claim` (which accepts `ClaimPayload | dict` and validates internally). The cyclopts migration preserves that exact UX: a new shared `claim_author_tool(claim: ClaimPayload | dict, ...)` is the common core; the MCP async wrapper passes its typed `claim` through unchanged (do NOT call `.model_dump()` — a raw dict has no such method, and a direct in-process caller bypassing the FastMCP protocol boundary genuinely passes a raw dict, as `tests/test_authoring_parity.py` proved when this was tried and broke 2 tests); a new `claim_author_cli_tool` reads `--from-json`/stdin and delegates to the shared core, registered as the `bathos-cli` tool for `claim author`.

### CLI-only batch (no MCP equivalent — stay hand-written cyclopts commands, ported directly onto the eventual real `bth` app; zero cisternal registry work)

`remote` (4: `add`/`list`/`remove`/`test`), `report` (3: `emit`/`show`/`show-manifest`), `provenance` (3: `show`/`diff`/`import`), `blast-radius`'s `install-hook`/`shadow-check`/`uninstall-hook` (3), `query shadow-log` (1), plus top-level `migrate`, `migrate-to-project-subdirs`, `classify`, `sprint-audit`, `catalog-version`, `view`, `export`, `submit` (8) — **22 commands total**. These have no dependency on the registry-driven batches and no `inner_fn` risk; they can be ported whenever, and are natural candidates to parallelize against the registry-driven batches rather than sequence after them.

### Explicit one-offs (need design, not just porting)

- **`claim author`'s `ClaimPayload` CLI mapping.** The one MCP tool in scope whose argument isn't a flat scalar/`Optional`. Options: flatten `ClaimPayload`'s fields into CLI flags (cyclopts has some dataclass/attrs support worth checking against Pydantic models specifically), accept a `--claim-file`/`--claim-json` blob, or leave `claim author` CLI-only (hand-written) and only migrate the other 3 `claim` commands through the registry. Needs a decision before the `claim` batch, not blocking the other 8 batches.
- **The `fg="dim"` bug** at `cli.py:721` (`check --check-outputs`) — still live, unrelated to the migration mechanism, fine to fix opportunistically whenever `check`'s batch lands (`check` is one of the 8 exact-match top-level commands) or standalone.

## Sequencing recommendation

1. ~~Extend the codegen script with the async-wrapper audit~~ — **done 260828**, see above.
2. ~~Top-level singletons batch (19 commands: 14 direct + 5 extraction)~~ — **done 260828** (commits `61f69a00`, `3706543b`).
3. ~~High-direct-ratio grouped batches (`anchor`, `attestation`, `blast-radius`, `outputs`, `query` = 15 direct, 0 extraction)~~ — **done 260828**: `anchor` (4), `attestation` (3), `blast-radius` partial (`assess`/`clear`, 2), `outputs` (2), `query` (6). 21 new CLI-level tests (`tests/test_registry_group_cli_cyclopts.py`), full targeted regression + Python-API-layer suite green.
4. ~~Extraction-heavy grouped batches (`claim`, `gate`, `postmortem`, `ref` = 13 commands, all needing a new plain sync function)~~ — **done 260828**: `claim` (author/register/scaffold/validate, all 4 extraction — `register` reclassified during implementation, see correction note above), `gate` (stamp/status), `postmortem` (scaffold/show/validate), `ref` (applicable/list/search/show). 21 new CLI-level tests (`tests/test_extraction_batch_cli_cyclopts.py`), plus 2 pre-existing structural tests in `tests/test_authoring_parity.py` updated to match the new `claim_author` → `claim_author_tool` → `author_claim` call chain (the architectural invariant they guard — CLI and MCP share one core — still holds, just with one more hop). Full targeted + Python-API-layer regression green.
5. ~~CLI-only batch (22 commands)~~ — **done 260829**: `remote` (add/list/remove/test), `report` (emit/show/show-manifest), `provenance` (show/diff/import), `blast-radius`'s 3 hook commands (install-hook/uninstall-hook/shadow-check), `query shadow-log`, plus 8 top-level commands with no MCP equivalent (`migrate`, `migrate-to-project-subdirs`, `classify`, `sprint-audit`, `catalog-version`, `view`, `export`, `submit`). All 22 are hand-written cyclopts commands (no `@cisternal.tool` decoration — no MCP tool to share), ported directly from `cli.py`'s Typer bodies with the mechanical `typer.echo`→`print`/`typer.Exit`→`SystemExit` substitution used throughout this migration. Shared helpers (`_catalog_dir`/`_require_project_slug`/`_soft_project_slug`) extracted into a new `src/bathos/cli_common.py` so `cli.py` and `cli_cyclopts.py` both call the same implementation rather than duplicating it (avoids stranding a second copy at the eventual cutover). The `query shadow-log` and `blast-radius` hook commands attach to the *same* cyclopts sub-apps `wire()` already created for those groups' registry-driven commands, via cyclopts' own public `App.__getitem__` (`app["query"].command(...)`) — no private cisternal internals touched. 32 new CLI-level tests (`tests/test_cli_only_batch_cyclopts.py`), several fixtures/mocks ported directly from the equivalent pre-existing Typer-CLI tests (`test_bth_submit.py`'s cluster mocking, `test_blast_radius_cli.py`'s hook-command tests, `test_cli.py`'s report-emit setup). `tests/test_cli.py` (the shipped Typer `bth` binary's suite) untouched and green throughout — all 78 commands now exist on the cyclopts preview app (`bth-preview`), zero blast radius on the real `bth` binary.
6. ~~`claim author`'s payload design decision~~ — **done 260828**, folded into step 4's `claim` work; see the correction note above.
7. **Cutover** (only remaining step): once all 78 commands exist behind the registry-driven or hand-written cyclopts surface (true as of step 5), retire the preview entry point, mount everything on the real `bth` cyclopts app, delete the old Typer `cli.py` surface, drop `typer` from `pyproject.toml`/`runner.py`. This is the point where `tests/test_cli.py`'s ~350 `CliRunner`-based assertions finally need to move onto `_cyclopts_runner.py` (or the equivalent shim, promoted to a permanent test utility at that point) — doing this as one coordinated cutover, not per-batch, avoids running two parallel CLI surfaces (typer + cyclopts) in the shipped binary at any point.

## Sizing

Registry-driven work is the pilot's proven, repeatable pattern — the campaign batch (7 commands, 1 extraction needed) took roughly a full session including building the reusable infrastructure (shim + codegen). With that infrastructure built and the audit now giving exact per-command classification, the remaining 49 commands split 32 direct (fast — decorator + `--emit-decorators`-generated boilerplate + test) / 17 extraction (slower — each needs a new function written, reviewed against the existing CLI's and MCP wrapper's behavior, and tested like `campaign_attest_parity_tool` was). The 9-command `gate`/`postmortem`/`ref` tail is the single most expensive remaining chunk of work in the whole migration.
