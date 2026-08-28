---
name: bathos-blast-radius
description: Trace which past runs, campaigns, and claims a bug fix implicates — blast-radius anchors (commit, commit-range, file, dependency), the blast_radius_ledger flag/clear/propagate flow, and the shadow-mode git-hook trigger that calibrates before it ever acts
triggers: [blast-radius, blast_radius_assess, blast_radius_clear, get_blast_radius_status, blast_radius_ledger, blast-status, shadow-log, install-hook, shadow-check, shadow_trigger]
---

# bathos-blast-radius

**30-second mental model:** a bug is found or fixed in commit/file X — which past
runs does that implicate? `bth blast-radius assess` answers this by diffing an
*anchor* (commit, commit-range, file, or dependency-lock) against every catalogued
run, sorting matches into `affected` / `unverifiable` / (implicitly) `unaffected`,
and durably recording the first two in a `blast_radius_ledger` (same append-only,
composite-keyed, cool-Parquet+warm-DuckDB dual-write shape as
**bathos-trust-ledger** — read that skill first if you haven't; this one assumes
it). Flagging never gates anything (`campaign conclude`/`claim validate` still run)
— it is a report + a durable, queryable annotation, nothing more.

## The four anchor types

`assess_blast_radius` takes exactly one anchor. Three reuse the v1 file-path
heuristic (a changed file "matches" a run if it's a substring of the run's
`command`/`argv`, in either direction — deliberately coarse, see Known noise below);
the fourth is a distinct dependency-lock check:

| Anchor | CLI flag | Ancestry check? | Match logic |
|---|---|---|---|
| `commit` | `--commit <sha>` | Yes — boundary is `<sha>^` | Changed files = `git diff --name-only <sha>^ <sha>`; a run is `affected` only if its `git_hash` is an ancestor of the boundary (predates the fix) AND touches a changed file |
| `commit_range` | `--commit-range <a..b>` | Yes — boundary is `<a>` | Changed files = `git diff --name-only a..b`. **Two-dot only** — three-dot `a...b` (symmetric-difference) is rejected with a `ValueError`, since its base isn't guaranteed to be an ancestor of its tip |
| `file` | `--file <path>` (repeatable) | **No** | Any run touching the given path(s) is `affected` regardless of when it ran — there's no commit boundary to compare against |
| `dependency` | `--dependency` | N/A | Reuses `check_dependency_lock_drift`/`hash_dependency_lock` as-is (whole-lockfile hash, no per-package diff). A run with **no recorded** `dependency_lock_sha256` goes to `unverifiable`, not `unaffected` — deliberately not reusing that checker's own fail-open default, which would be a silent false negative here |

`--project <slug>` optionally scopes the scan to one `project_slug` (bathos's
catalog is shared across projects; without this, a run in project B whose command
happens to substring-match a changed filename in project A can get pulled in —
same accepted heuristic-noise tradeoff as below, not a silent bug).

The scan itself is unbounded (`limit=1_000_000` under the hood, not the usual
`list_runs`/`check_runs` default of 50) — `assess` always considers your *entire*
catalog for that anchor/project, not just recent runs.

## Known noise: this is a heuristic, on purpose (v1)

The file-path substring match over-flags: a run can touch a changed file without
ever executing the changed code path. This was the *named, accepted* pre-mortem
risk when the feature was scoped (spec: `.praxia/docs/specs/260826_blast-radius-assessment-skill.md`)
— the v1 mitigation is not precision, it's **auditability**: every match records
its `match_reason` (which file(s) matched, and for commit/commit-range anchors,
the ancestry math) so a human can see *why* something got flagged and judge for
themselves. Don't expect `assess` to be quiet; expect it to be explainable.

## Running an assessment

```bash
bth blast-radius assess --commit abc1234
bth blast-radius assess --commit-range abc1234..def5678
bth blast-radius assess --file scripts/experiments/train.py --file src/mymodel/loss.py
bth blast-radius assess --dependency
bth blast-radius assess --commit abc1234 --project myproject   # scope to one project
bth blast-radius assess --commit abc1234 --no-flag             # report only, no ledger write
```

Output is the full JSON report (`anchor_kind`, `anchor_value`, `changed_files`,
`affected`, `unverifiable`, `unaffected_run_ids`) printed **before** any ledger
write happens — report-then-flag is load-bearing, not incidental (it's the acceptance
criterion that makes shadow-mode's "verdict logged, never applied" pattern trustworthy
elsewhere in this feature too). Unless `--no-flag`, it then:

1. Appends one `blast_radius_ledger` record per `affected`/`unverifiable` run
2. Propagates to campaign-level records (any campaign with ≥1 affected/unverifiable
   member run — more-severe-state-wins: `affected` beats `unverifiable`)
3. Propagates to claim-level records (any campaign with a *registered* claim whose
   union-gate clauses are backed by one of those runs — same covering-run matching
   `run_union_gate` uses; `positive_control` clauses are skipped, out of scope here)

```bash
$ bth blast-radius assess --commit abc1234
{ ... JSON report ... }

Flagged 1 run(s) in blast_radius_ledger.
Flagged 1 campaign(s).
Flagged 1 claim(s), implicated clauses: [['H1', 'H2']]
```

**Gotcha — claim-level `entity_id` is a `campaign_id`, not a separate claim id.**
There's no standalone claim identifier anywhere in bathos; a claim is always
resolved via its owning campaign. So `entity_type="claim"` and
`entity_type="campaign"` records for the *same* campaign share the identical
`entity_id` — the composite key `(entity_type, entity_id)` is what tells them
apart. Querying `bth query blast-status campaign <id>` and
`bth query blast-status claim <id>` (same `<id>`) can legitimately return
different states.

## Clearing a flag

```bash
bth blast-radius clear run <run_id> --reason "reproduced with box=6nm, forces unaffected"
bth blast-radius clear campaign <campaign_id> --reason "..."
bth blast-radius clear claim <campaign_id> --reason "..."   # claim entity_id is the campaign_id — see gotcha above
```

Unlike **bathos-trust-ledger**'s `graduate_product`, clearing here is **not**
attestation-gated — it requires only a non-empty `--reason` string. This is a
deliberate Phase-1 scope cut, not an oversight — don't expect the rigor of the
trust-ledger's PASS-before-promote ratchet here.

## Shadow auto-clear verdict — observability only, NEVER auto-applied

Every `affected` run-level record also gets a computed `shadow_verdict`:
`{"kind": "output_sha_still_matches", "verdict": "clean" | "drifted" |
"no_outputs_recorded", "checked_at": ...}`, based on whether the run's catalogued
output files still hash-match what was recorded (the same AC-20 check
`bth check --check-outputs` uses).

**This verdict is stored for later human review and has zero effect on `to_state`.**
A `verdict: "clean"` shadow value does NOT clear the flag and is not read by
`fold_blast_radius_state` at all — it sits alongside the record as data. Get this
backwards in downstream tooling and you'd silently auto-clear real flags on a proxy
signal ("output still matches" ≠ "the bug doesn't apply") — exactly the risk this
was built to avoid.

## Querying state

```bash
bth query blast-status run <run_id>          # clean | affected | unverifiable | cleared
bth query blast-status campaign <campaign_id>
bth query blast-status claim <campaign_id>    # entity_id is the campaign_id
```

`clean` is the fold's default for "no ledger record at all" — an entity is
implicitly clean until something flags it.

`bth campaign review <campaign_id>` surfaces `blast_radius_status` and
`claim_blast_radius_status` automatically — informational and **non-gating**, same
semantics as above.

## Shadow-mode git-hook trigger (backlog #4555)

Bathos's first-ever OS-level git-hook integration. Calibrates whether an automatic
trigger is reliable enough to trust before it's ever allowed to write anything real.

```bash
bth blast-radius install-hook      # wraps core.hooksPath, chains/symlinks existing hooks
bth blast-radius uninstall-hook    # restores prior core.hooksPath exactly
bth blast-radius shadow-check <sha>  # what the hook runs in the background; safe to call directly
bth query shadow-log --limit 20    # calibration review
```

**Install mechanics:** points `git config core.hooksPath` at `.bth/hooks/` — never
writes into `.git/hooks/` directly, so it never clobbers another tool's hooks. Any
pre-existing `post-commit` is chained to (run first, `|| true`); every other hook
name is symlinked through unchanged. `uninstall-hook` restores the exact prior
`core.hooksPath` value.

**The trigger:** on every commit, a cheap inline shell keyword pre-filter
(`fix`/`fixes`/`fixed`/`bug`/`bugfix`/`hotfix`/`regression`/`patch`,
case-insensitive, hardcoded) checks the commit message. On a match, it spawns
`bth blast-radius shadow-check "$sha"` **detached** — `git commit` is never slowed
down.

**Why "shadow" — never durable, never gating, ever:** `shadow-check` calls
`assess_blast_radius` internally but **never** calls `flag_blast_radius` or either
`propagate_to_*` function. It writes one `entity_type="shadow_trigger"` record
(`entity_id` = commit sha, `to_state="shadow_only"`) — structurally invisible to
every real run/campaign/claim read path. The point is purely calibration: review
`shadow-log` over time to decide whether the trigger is accurate enough to
eventually wire to something real (it currently is not).

## MCP surface

Three tools registered and callable now: `blast_radius_assess`, `blast_radius_clear`
(both write-verb, gated by `@require_write_token`), `get_blast_radius_status`
(read-only, ungated). Arg shape differs from CLI: `commit`/`commit_range`/`files`
default to `""` not `None`; `files` is comma-separated, not repeatable.
`blast_radius_assess` returns `{"error": ...}` for a malformed anchor rather than
raising. **Note:** unlike most MCP write-verb tools, these do not yet appear to use
the typed `BathosErrorCode` envelope described in bathos-mcp — verify current
behavior before assuming full conformance.

## Related

- **bathos-trust-ledger** — the ledger dual-write shape this feature mirrors exactly.
- **bathos-campaigns** — union-gate clauses, claim registration that claim-level propagation reads.
- **bathos-mcp** — MCP error envelope conventions; conformance for these three tools is unverified.
