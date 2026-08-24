---
title: 'Post-conclude hook-discovery mechanism for campaign conclusion'
description: Entry-point-based hook group (bathos.campaign_conclude_hooks) letting external packages react to a concluded campaign without bathos depending on them
status: shipped
task_id: 260824_campaign-conclude-hooks
date: '260824'
supersedes: ''
backlog_ids: ''
---

# Post-conclude hook-discovery mechanism for campaign conclusion

## Context

`conclude_campaign()` (`src/bathos/campaigns.py`) is bathos's single binding site for marking
a campaign concluded — it runs the Union Gate, parity/synthetic-recovery confound checks, the
review-coverage gate, and the obligation gate, any of which can downgrade the researcher's
`outcome_label` before the verdict is durably recorded. Nothing downstream previously reacted
to a campaign concluding.

affigit-wire (`~/projects/affigit-mono/packages/affigit-wire`), a sibling package that already
depends on bathos for its own gate checks, needs to attempt a real promotion (constructing an
`affigit_core.Pin` per concluded member run) at exactly this point. bathos must not take a hard
dependency on affigit-wire (or any other downstream consumer) to make that possible — the
dependency edge must point one way only: affigit-wire → bathos.

## Decision: `importlib.metadata` entry points, not a callback registry or event bus

Chose Python's standard `importlib.metadata.entry_points(group="bathos.campaign_conclude_hooks")`
over:

- **A bathos-side callback registry** (`bathos.register_conclude_hook(fn)`) — would require the
  consumer package to run import-time registration code inside bathos's process before
  `conclude_campaign()` is ever called, i.e. some other module must `import affigit_wire` for
  its registration side effect to fire. That's exactly the reverse dependency direction we're
  avoiding, just moved into application wiring instead of `pyproject.toml`.
- **A message bus / file-based event log** — over-engineered for an in-process, synchronous,
  single-consumer-class need; adds a persistence format to design and version.

Entry points are the standard-library-native way for an *installed* package to advertise "I
provide a hook for this extension point" purely via its own `pyproject.toml`
(`[project.entry-points."bathos.campaign_conclude_hooks"]`), discoverable by anything with zero
import-time coupling. bathos's only touchpoint is a group-name string constant.

## Call signature

```python
@dataclass(frozen=True)
class ConcludedRunInfo:
    run_id: str
    output_path: str | None
    sidecar_path: str | None
    content_hash: str | None


@dataclass(frozen=True)
class CampaignConcludeEvent:
    campaign_id: str
    outcome_label: str
    members: tuple[ConcludedRunInfo, ...]


def hook(event: CampaignConcludeEvent) -> None: ...
```

A single typed payload object (not positional args) was chosen so new fields can be added later
without breaking every existing hook's call signature. `outcome_label` is read *after* all four
downgrade gates have run — a hook always sees the final verdict, matching what a human reading
`bth campaign show` would see, not the researcher's original (possibly since-overridden) label.

`members` reuses `union_campaign_member_ids()` (warm `campaign_runs` union cool-tier parquet
rows) — the same membership resolution the Union Gate itself uses — rather than re-deriving
membership from scratch. When the Union Gate has already resolved membership earlier in the
same `conclude_campaign()` call (claim registered), that list is threaded through
(`precomputed_member_ids`) instead of scanning the catalog a second time for an identical
result; the no-claim path still resolves it fresh at the hook call site.

Per-member `output_path`/`sidecar_path`/`content_hash` are resolved via
`_resolve_member_run_infos()` with a single batched `WHERE id IN (...)` query against the
already-open warm-tier `runs` table (not one query per member — membership can be large), giving
`output_paths[0]` and `sidecar_path` directly. `content_hash` is looked up by **matching the
`output_metadata` JSON array entry whose `"path"` equals the resolved `output_path`** — not "the
first entry with any recorded hash". `output_metadata` entries are produced 1:1 with
`output_paths` at compaction time, and a large (>100MB) or unreadable output is recorded with no
`sha256` at all (`bathos.compact._collect_output_metadata`); for a multi-output run, scanning for
the first truthy hash instead of matching by path would silently pair `output_path` with a
*different* file's hash. Any of `output_path`/`sidecar_path`/`content_hash` may still be `None`
— a run that predates compaction, or whose output was never hashed, gets a member entry with the
identity (`run_id`) and null pointers, rather than being dropped from `members`.

## Non-propagation is structural, not just tested

The hard requirement was that a downstream consumer's bug can never break bathos's own
conclusion contract. Three failure surfaces are wrapped separately, all `except Exception`, all
`print()`-based to match this function's existing warning style (not `logging` — see
`conclude_campaign()`'s existing `print(f"WARNING: ...")` calls for parity/review/obligation
gates):

1. `importlib.metadata.entry_points(group=...)` itself, in case discovery fails (corrupt
   package metadata, etc.) — caught in `_run_campaign_conclude_hooks()`, degrades to "no hooks".
2. Membership/member-info resolution (`union_campaign_member_ids()` when not precomputed, plus
   `_resolve_member_run_infos()`) — caught together, degrades to "don't invoke any hook this
   call" rather than invoking hooks with a partial or fabricated payload. This surface was
   originally *not* guarded (an unguarded `read_runs()` inside `union_campaign_member_ids()` can
   raise on a corrupt cool-tier parquet fragment) — caught in adversarial review before merge;
   see `tests/test_campaign_conclude_hooks.py::test_hooks_survive_member_resolution_failure`.
3. Each hook's `.load()` and the call itself — caught per-hook, so one broken entry point
   cannot prevent a second, well-behaved entry point from running.

`_run_campaign_conclude_hooks()` is invoked as the last statement in `conclude_campaign()`,
after `db.commit()`, the cool-tier JSON re-sync (`write_campaign_cool`), and the
`campaign.conclude` telemetry event — i.e. after every side effect that makes "concluded"
durable and observable. A hook can immediately re-query bathos (CLI, MCP, or direct DB read)
and see the campaign as concluded; there is no window where hooks see a not-yet-committed state.

## What this does NOT do

- No new bathos dependency on affigit-wire or `affigit_core` — `bathos/campaigns.py` contains no
  reference to either name. affigit-wire is expected to depend on bathos and register itself
  under the `bathos.campaign_conclude_hooks` entry-point group in its own `pyproject.toml`; that
  wiring lives entirely in affigit-wire's package metadata, not here.
- No ordering guarantee across multiple hooks beyond "whatever `importlib.metadata` returns them
  in" — ties are unspecified. A consumer needing ordering relative to another hook needs a
  different mechanism (out of scope here; no current registrant needs it).
- No retry, no async dispatch, no return-value contract — a hook's return value is discarded.
  This is a fire-and-forget notification point, not a pipeline stage.

## Recommendation

Human review before merge, not merge-on-green. Rationale: this function is bathos's single
binding site for campaign conclusion — every `bth campaign conclude` (CLI) and both MCP tool
surfaces (`campaign_conclude_tool`, `mcp_campaign_conclude_tool`) call through it, and the
change, while purely additive (~200 lines, 0 deletions, verified via `git diff --stat`), sits at
the end of a function with four pre-existing downgrade gates whose interaction with a first-time
hook consumer (affigit-wire) hasn't been exercised in production yet.

Two independent adversarial reviews (security/robustness lens, correctness/design lens) ran
against this diff before this recommendation was written. Both found real, fixed-before-merge
defects: an unguarded member-resolution path that could have let a corrupt catalog fragment
propagate an exception out of an already-committed `conclude_campaign()` call, and a
content_hash/output_path mismatch bug for multi-output runs. That two structured reviews each
found something real is itself evidence for human review, not against it — green tests confirm
the mechanism behaves as designed in isolation; they don't substitute for a human judgment call
on whether affigit-wire's actual hook implementation (not authored here) does something safe
with `ConcludedRunInfo.content_hash` being `None` for uncompacted runs, or on residual risk this
round of review didn't surface.
