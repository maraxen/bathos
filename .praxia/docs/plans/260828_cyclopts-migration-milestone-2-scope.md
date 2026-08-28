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

## The one real risk Milestone 1 uncovered

The codegen's `inner_fn` field is read from the MCP (`registry="bathos"`) partition, which is almost always the `async def mcp_x_tool(...)` wrapper `@traced_tool` decorates — **not** the plain sync function CLI wiring needs (`wire()`'s CLI path calls the target directly, not via `asyncio.run`; pointing it at an async function silently returns an unawaited coroutine instead of running). The campaign pilot's 7 commands split 6-plain-delegate / 1-needs-extraction. **This ratio is unknown for the other 49 registry-drivable commands and is the main source of estimation risk below.**

Before batching work, run one audit pass: for each `verified` row's `inner_fn`, check with `inspect.iscoroutinefunction` whether it's async, and if so, grep the same file for a plausibly-related plain sync function it might delegate to (same body, different name — the `campaign_add_tool` → `mcp_campaign_add_tool` pattern). This is a ~30-minute extension to the existing codegen script, not new infrastructure, and it converts "56 unknowns" into a concrete per-command work estimate before any batch starts.

## Batching

### Registry-driven batches (need the `@cisternal.tool(registry="bathos-cli", ...)` treatment, same pattern as campaign)

| Batch | Commands | Count | Notes |
|---|---|---|---|
| Top-level singletons | `run`, `check`, `sync`, `verify`, `lint`, `compact`, `init`, `repair` (exact matches) + `ls`, `find`, `show`, `cite`, `lineage`, `sql`, `archive`, `archive-artifact`, `new-experiment`, `validate-sidecar`, `restore` | 19 | Highest-value batch — mature, well-tested commands; no group-mounting logic needed (`cli_group=None`), so it's the cheapest place to validate the async-wrapper audit's predictions before touching grouped commands. |
| `anchor` | `figure-register`, `find`, `get`, `insert` | 4 | All 4 verified. |
| `attestation` | `register`, `scaffold`, `validate` | 3 | All 3 verified. |
| `claim` | `author`, `register`, `scaffold`, `validate` | 4 | `claim author` takes the one non-flat argument in the whole 62-tool registry (nested `ClaimPayload` Pydantic model) — see the dedicated item below; do the other 3 first. |
| `gate` | `stamp`, `status` | 2 | |
| `outputs` | `list`, `summary` | 2 | |
| `postmortem` | `scaffold`, `show`, `validate` | 3 | |
| `query` | `attestation`, `blast-status`, `candidates`, `figures`, `resolve-pin`, `trust-state` | 6 | `shadow-log` (7th query command) is CLI-only, see below. |
| `ref` | `applicable`, `list`, `search`, `show` | 4 | |
| `blast-radius` (partial) | `assess`, `clear` | 2 | The other 3 blast-radius commands (`install-hook`, `shadow-check`, `uninstall-hook`) are CLI-only — see below. |

**30 grouped + 19 top-level = 49 commands**, the bulk of the remaining work.

### CLI-only batch (no MCP equivalent — stay hand-written cyclopts commands, ported directly onto the eventual real `bth` app; zero cisternal registry work)

`remote` (4: `add`/`list`/`remove`/`test`), `report` (3: `emit`/`show`/`show-manifest`), `provenance` (3: `show`/`diff`/`import`), `blast-radius`'s `install-hook`/`shadow-check`/`uninstall-hook` (3), `query shadow-log` (1), plus top-level `migrate`, `migrate-to-project-subdirs`, `classify`, `sprint-audit`, `catalog-version`, `view`, `export`, `submit` (8) — **22 commands total**. These have no dependency on the registry-driven batches and no `inner_fn` risk; they can be ported whenever, and are natural candidates to parallelize against the registry-driven batches rather than sequence after them.

### Explicit one-offs (need design, not just porting)

- **`claim author`'s `ClaimPayload` CLI mapping.** The one MCP tool in scope whose argument isn't a flat scalar/`Optional`. Options: flatten `ClaimPayload`'s fields into CLI flags (cyclopts has some dataclass/attrs support worth checking against Pydantic models specifically), accept a `--claim-file`/`--claim-json` blob, or leave `claim author` CLI-only (hand-written) and only migrate the other 3 `claim` commands through the registry. Needs a decision before the `claim` batch, not blocking the other 8 batches.
- **The `fg="dim"` bug** at `cli.py:721` (`check --check-outputs`) — still live, unrelated to the migration mechanism, fine to fix opportunistically whenever `check`'s batch lands (`check` is one of the 8 exact-match top-level commands) or standalone.

## Sequencing recommendation

1. Extend the codegen script with the async-wrapper audit (30 min, de-risks everything downstream).
2. Top-level singletons batch (19 commands) — highest command-count-per-batch, lowest structural risk (no grouping), and the first real test of the audit's predictions at scale.
3. The 8 grouped registry-driven batches (30 commands) — mechanically identical to the campaign pilot once the audit has flagged which `inner_fn`s need extraction vs. direct decoration.
4. CLI-only batch (22 commands) — can run in parallel with 2-3 since it's independent; no ordering constraint.
5. `claim author`'s payload design decision, folded into the `claim` batch.
6. **Cutover**: once all 78 commands exist behind the registry-driven or hand-written cyclopts surface, retire the preview entry point, mount everything on the real `bth` cyclopts app, delete the old Typer `cli.py` surface, drop `typer` from `pyproject.toml`/`runner.py`. This is the point where `tests/test_cli.py`'s ~350 `CliRunner`-based assertions finally need to move onto `_cyclopts_runner.py` (or the equivalent shim, promoted to a permanent test utility at that point) — doing this as one coordinated cutover, not per-batch, avoids running two parallel CLI surfaces (typer + cyclopts) in the shipped binary at any point.

## Sizing

Registry-driven work (49 commands) is the pilot's proven, repeatable pattern — 1 batch (campaign, 7 commands, 1 extraction needed) took roughly a full session including building the reusable infrastructure (shim + codegen). With that infrastructure now built, subsequent batches should be faster per-command, but the async-wrapper audit's finding rate is the swing factor: if most of the 49 need extraction (like `campaign_attest_parity_tool` did) rather than direct decoration (like the other 6 campaign commands), this is meaningfully more work than the pilot's 6:1 ratio suggests. Recommend running the audit first and re-sizing before committing to a batch order beyond "top-level singletons first."
