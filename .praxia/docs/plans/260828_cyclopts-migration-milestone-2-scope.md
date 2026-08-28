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
| `claim` | `register` (direct, resolves to `_claim_register_sync`) + `author`, `scaffold`, `validate` (extraction) | 1 | 3 | `claim author` also takes the one non-flat argument in the whole 62-tool registry (nested `ClaimPayload` Pydantic model) — see the dedicated item below. |
| `gate` | `stamp`, `status` | 0 | 2 | Both have business logic inline on the async MCP function — no delegate exists. |
| `postmortem` | `scaffold`, `show`, `validate` | 0 | 3 | All 3 have inline async business logic. |
| `ref` | `applicable`, `list`, `search`, `show` | 0 | 4 | All 4 have inline async business logic. |

**49 commands total: 32 direct decoration, 17 need extraction.** Recommended order follows the table — highest direct-ratio batches first (top-level, anchor, attestation, blast-radius, outputs, query = 31 direct / 5 extraction combined), saving the fully-extraction-heavy batches (`gate`, `postmortem`, `ref` = 9 commands, all extraction) for last since each of those 9 commands needs a genuinely new plain sync function written and reviewed, not just a decorator pasted on.

### CLI-only batch (no MCP equivalent — stay hand-written cyclopts commands, ported directly onto the eventual real `bth` app; zero cisternal registry work)

`remote` (4: `add`/`list`/`remove`/`test`), `report` (3: `emit`/`show`/`show-manifest`), `provenance` (3: `show`/`diff`/`import`), `blast-radius`'s `install-hook`/`shadow-check`/`uninstall-hook` (3), `query shadow-log` (1), plus top-level `migrate`, `migrate-to-project-subdirs`, `classify`, `sprint-audit`, `catalog-version`, `view`, `export`, `submit` (8) — **22 commands total**. These have no dependency on the registry-driven batches and no `inner_fn` risk; they can be ported whenever, and are natural candidates to parallelize against the registry-driven batches rather than sequence after them.

### Explicit one-offs (need design, not just porting)

- **`claim author`'s `ClaimPayload` CLI mapping.** The one MCP tool in scope whose argument isn't a flat scalar/`Optional`. Options: flatten `ClaimPayload`'s fields into CLI flags (cyclopts has some dataclass/attrs support worth checking against Pydantic models specifically), accept a `--claim-file`/`--claim-json` blob, or leave `claim author` CLI-only (hand-written) and only migrate the other 3 `claim` commands through the registry. Needs a decision before the `claim` batch, not blocking the other 8 batches.
- **The `fg="dim"` bug** at `cli.py:721` (`check --check-outputs`) — still live, unrelated to the migration mechanism, fine to fix opportunistically whenever `check`'s batch lands (`check` is one of the 8 exact-match top-level commands) or standalone.

## Sequencing recommendation

1. ~~Extend the codegen script with the async-wrapper audit~~ — **done 260828**, see above.
2. Top-level singletons batch (19 commands: 14 direct + 5 extraction) — highest command-count-per-batch, lowest structural risk (no grouping). **In progress.**
3. High-direct-ratio grouped batches (`anchor`, `attestation`, `blast-radius`, `outputs`, `query` = 15 direct, 0 extraction) — mechanically identical to the campaign pilot's direct-decoration commands.
4. Extraction-heavy grouped batches (`claim`, `gate`, `postmortem`, `ref` = 10 commands, 9 of them needing a new plain sync function) — sequenced last since each needs real review, not just decorator-pasting.
5. CLI-only batch (22 commands) — can run in parallel with 2-4 since it's independent; no ordering constraint.
6. `claim author`'s payload design decision, folded into step 4's `claim` work.
7. **Cutover**: once all 78 commands exist behind the registry-driven or hand-written cyclopts surface, retire the preview entry point, mount everything on the real `bth` cyclopts app, delete the old Typer `cli.py` surface, drop `typer` from `pyproject.toml`/`runner.py`. This is the point where `tests/test_cli.py`'s ~350 `CliRunner`-based assertions finally need to move onto `_cyclopts_runner.py` (or the equivalent shim, promoted to a permanent test utility at that point) — doing this as one coordinated cutover, not per-batch, avoids running two parallel CLI surfaces (typer + cyclopts) in the shipped binary at any point.

## Sizing

Registry-driven work is the pilot's proven, repeatable pattern — the campaign batch (7 commands, 1 extraction needed) took roughly a full session including building the reusable infrastructure (shim + codegen). With that infrastructure built and the audit now giving exact per-command classification, the remaining 49 commands split 32 direct (fast — decorator + `--emit-decorators`-generated boilerplate + test) / 17 extraction (slower — each needs a new function written, reviewed against the existing CLI's and MCP wrapper's behavior, and tested like `campaign_attest_parity_tool` was). The 9-command `gate`/`postmortem`/`ref` tail is the single most expensive remaining chunk of work in the whole migration.
