---
title: "Result validity, supersession and archive: stop stale readings reaching agents"
description: "Design (pre-spec) for task 261106_archive-supersession: instrument defect ranges with version backfill from uv.lock, hidden-by-default reads/cites for void/affected/superseded/archived results, evidence tiers incl. spike, distance-from-present annotations, a lightweight findings register with supersession edges, session-start current-beliefs view, and a batch archive sweep with a self-contained doc store that can be pointed at .praxia/docs. Phased: P1 stop the leak, P2 findings + tiers, P3 archive sweep."
status: DESIGN_APPROVED_PENDING_SPEC
task_id: 261106_archive-supersession
authors:
  - "Claude Opus 5.5 (brainstorming session, decisions by the owner)"
date: "2026-11-06"
version: "0.1.0"
---

# Result validity, supersession and archive

## 1. Intent and why

Two failure modes recur in asr and tev_design (the owner's words: "constantly happening"):

1. **Contamination.** Results produced by a defective or since-changed instrument (chiefly the
   aminx MPNN wrapper) are dredged up and cited as current. Agents over-index on an old reading
   instead of treating it with proportionate skepticism.
2. **Circling.** Settled questions get reopened, and nothing records "what we currently believe
   and why", so each session rebuilds the picture from scattered dated docs and lands somewhere
   different. The owner rates the two halves of circling (re-running settled questions; having no
   current-best answer) as roughly equal.

The tooling that should prevent (1) exists only on the write side. bathos can record that a run
is blast-affected or that a script is stale, but **no read, cite, claim or graduate path consults
that record**, so a flagged result is indistinguishable from a good one at the moment an agent
touches it. The core of this design is therefore not moving files; it is making every read path
tell the truth about validity *at read time*, and then giving cleanup (archive) and current belief
(findings) first-class homes.

Success, in the owner's terms: an agent opening asr or tev_design cannot accidentally cite a
void/superseded reading; it sees the current belief per question with its evidence tier and how
far that evidence is from the present; the historical ledger is intact and one flag away.

## 2. Goals, non-goals, anti-goals

**Goals**
- G1. Declare known-bad instrument ranges once; every affected run is flagged automatically.
- G2. Void / affected / superseded / archived results are hidden by default from every agent-facing
  read and refused by every citation/registration gate; history is reachable by explicit opt-in.
- G3. Every run read shows its evidence tier and its distance from the present.
- G4. A lightweight findings register: per question, the current answer, its supporting runs,
  status, and what it superseded, with machine-followable supersession edges.
- G5. A short, byte-capped "current beliefs" view surfaced at session start.
- G6. A batch, dry-run-first archive sweep over scripts, sidecars, outputs, docs and telemetry,
  built on the existing `archive_artifact` machinery, with restore.
- G7. Backfill asr and tev_design: defect ranges first, then seeded findings, then archive triage.

**Non-goals**
- Automatic time decay or expiry of valid results (owner chose "show distance, don't decay").
- Replacing the claim tier. Claims stay the confirmatory machinery; claims may cite findings.
- Deleting anything from any ledger.

**Anti-goals (failure even if it "works")**
- A1. Hiding so aggressively that low-provenance history vanishes: "unverifiable" must not hide.
- A2. Mutating or deleting historical ledger rows to implement hiding.
- A3. Adding context clutter: session-start output that grows unbounded or lists superseded items.
- A4. A third parallel register beside tev's `claims_ledger.toml` / asr's `.verdicts/` instead of
  a migration target for them.
- A5. bathos writing into another tool's data dirs without explicit configuration.
- A6. Arbitrary thresholds presented as principled (memory: threshold-cutoff skepticism).

## 3. Evidence base (tags: verified = read at file:line or probed; unverified = subagent report only)

| # | Fact | Tag |
|---|---|---|
| E1 | `cite.py` (55 lines) performs no blast/stale/archive/drift/age check | verified (grep) |
| E2 | `fold_blast_radius_state*` is called only from `blast_radius.py`, `campaigns.py`, `mcp.py` (status tool + campaign review, "informational, non-gating") | verified (grep); the campaign-review-only claim is unverified |
| E3 | Sidecar `[status]` block: `stale`, `stale_reason`, `superseded_by` (free text) — `sidecar.py:135-146, 788-800` | verified |
| E4 | `[status] stale` blocks `bth run` only (`prereg.py` SCRIPT_STALE, `--allow-stale`) | unverified |
| E5 | `archive_artifact` / `artifact_archive.py` archives one script + sidecar + matching-sha outputs, stubs tracked files, git-bundles untracked, writes git note + `archived_items` ledger + `.bth/ARCHIVE_INDEX.md`; `restore` reverses | unverified (commit 202c7da4) |
| E6 | `get_run`/`find`/`ls`/`run_sql`, `claim register`, `campaign conclude`, `resolve_pin`, `graduate_product` consult neither blast state nor `archived_items` | unverified (E1/E2 make it likely) |
| E7 | Trust ledger has 3 states (unknown/candidate/promoted), no decay, no blast awareness | unverified |
| E8 | Epistemic classes `exploratory, calibrating, confirmatory, legacy` (`sidecar.py:470`); `CANONICAL_STAGES = exploration, calibration, validation, ablation, production` (`sidecar.py:461`), "advisory"; exploratory runs store `outcome="observed"` (`sidecar.py:449-457`) | verified |
| E9 | Stage vocabulary drift in the wild: asr has `stage_name="confirmatory"` ×7 and `"power"` ×1 (not canonical); declared on 46/367 asr sidecars, 15/131 tev sidecars | verified (probe) |
| E10 | **`dependency_provenance` is null on every run** in both repos (tev 3,167 run events; asr 11,673); `dependency_lock_sha256` present on ~half (tev 1,593; asr 3,516) | verified (probe of `.bth/log/*.jsonl`) |
| E11 | Version recovery: for 24/25 sampled distinct `(git_hash, dependency_lock_sha256)` pairs in tev, `sha256(git show <hash>:uv.lock)` equals the recorded lock hash, and that lock names the aminx wheel (`0.1.0a24` … `0.2.1`, `source = {path = "wheels/aminx-*.whl"}`) | verified (probe; spike, not a citable number) |
| E12 | aminx defect chain is prose only: `wheels/AMINX_PIN.md:400` "S > 1 AR-sampled results at ≤ a22 are void"; alphabet order fix; a24→a25 diagonal self-leak fix *and* estimand change ("changed WHICH QUANTITY is computed", `claims_ledger.toml` header); X-token sampling fixed in 0.2.1 | :400 verified; rest unverified |
| E13 | No bathos blast-radius was ever run in either repo; 0 sidecars carry `[status]` | [status] verified; blast unverified |
| E14 | tev `scripts/audit/claims_ledger.toml`: 43 `[[claims]]`, `evidential_status` ∈ {tracked_reproducible 8, untracked_reproducible 17, unverifiable 6, refuted_or_superseded 13}, `instrument_wheel`, `[instrument] aminx_pinned_here="0.1.0a24"` (stale; pin is 0.3.0); header records an item "twice proposed as new work because nothing here pointed at it"; checker `check_ledger_instrument_staleness.py` (181 lines) | verified |
| E15 | asr: `.verdicts/` (41), `.praxia/docs/experiments/INDEX.md`, `reference/261007_project-state-snapshot.md`; LG matrix wrong 2026-06-08→10-23 (a second, non-aminx defect) | unverified |
| E16 | Leak example: tev `specs/260705_design-additional-analysis-tooling-to-me.md` (pre-a22) cited by an agent on 2026-07-22 as "not up for revisiting" (memory `feedback_cite_claim_not_dated_doc.md`) | unverified |
| E17 | bathos plugin has only a PreToolUse hook (`plugin/hooks/hooks.json`); no SessionStart | verified |
| E18 | praxia docs registry has `superseded`/`archived` statuses, `supersedes` frontmatter, an `archive` category of `.tar.zst` + `.md` manifest pairs | unverified |
| E19 | `dependency_provenance` is null because capture is **opt-in**: `runner.py:1205,1271` calls `capture_dependency_provenance(tracked_packages, …)`, and `resolve_tracked_packages` (`dependency_provenance.py:100`) unions `track_packages` from sidecar `[provenance]`, `.bth.toml` and `[tool.bathos]`; neither asr nor tev_design sets `track_packages` anywhere | verified |

The spec phase must re-verify every *unverified* row by reading code before building on it.

## 4. Decisions (made by the owner unless marked "builder's call" or "designer's guess")

### D1. Instrument defect registry (owner: declared defect ranges)
A small registry of known instrument problems, each entry:
- `package` (e.g. `aminx`), a version range and/or source-commit range (fixes landed without a
  version bump — `d930bbad` while pinned at a24 — so ranges must accept commits too);
- optional `predicate`: a DuckDB SQL fragment over run fields/params (e.g. the `S > 1` condition),
  same mechanism as `[outcomes]` conditions — so a range can be narrower than "every run on ≤a22";
- `kind`: `defect` (results wrong) vs `estimand_change` (results not comparable across the boundary
  but not wrong — E12's a24→a25). These must be distinct: an estimand change does not void a
  result; it makes cross-boundary comparisons invalid;
- `severity` → resulting state: `void` | `affected` | `suspect`;
- `fixed_in`, `evidence` (pointer to postmortem/commit), `declared_at`, `declared_by`.

Matching a registry entry flags runs via the existing blast-radius ledger (new anchor type:
`dependency_range`), so `clear`, propagation to campaigns/claims, and the audit trail are reused.
*Builder's call:* where the registry lives (sidecar-like TOML under `.bth/instruments/` is the
designer's guess) and whether it can be shared across projects (aminx defects affect both repos;
a per-user registry the projects opt into is attractive).

### D2. Instrument version resolution and backfill (from E10/E11)
Because `dependency_provenance` is empty everywhere, the version a run used is resolved in order:
1. recorded `dependency_provenance` (future runs; P1 must make the runner actually populate it —
   why it is null today is an open question for the spec phase);
2. **verified-from-lock**: `git show <git_hash>:uv.lock` whose sha256 equals the recorded
   `dependency_lock_sha256` → parse package version + source (wheel path; wheel sha if the wheel is
   tracked);
3. **inferred-from-commit**: no recorded lock hash, but the lock at `git_hash` parses → version is
   *inferred*; dirty trees make it weaker still (record `git_dirty`);
4. otherwise **unknown**.

The resolution source is stored with the result (`version_source`), never silently upgraded. A
defect match on source 3 yields at most `suspect`, never `void` (A1). Backfill writes derived
records; it never edits historical run rows (A2).

### D3. Validity states and hidden-by-default (owner: hidden, explicit opt-in)
One resolved `validity` per run (and per finding), computed at read time from the ledgers:

| State | Source | Default reads | Cite / claim register / conclude / graduate |
|---|---|---|---|
| `void` | defect range, severity void | hidden | refused |
| `affected` | blast-radius flag / defect range | hidden | refused |
| `superseded` | supersession edge (D5) | hidden | refused |
| `archived` | `archived_items` latest-wins | hidden | refused |
| `suspect` | defect range on inferred version, severity suspect | shown + banner | allowed with an explicit acknowledgment field |
| `unverifiable` | blast assessment could not decide | shown + banner | allowed with acknowledgment |
| `current` | none of the above | shown | allowed |

- Hidden reads print one line: `N superseded/void/archived results hidden — --include-superseded to show`.
- `--include-superseded` (CLI) / `include_superseded=true` (MCP) shows them with a banner naming
  the state, reason, and replacement.
- `run_sql`: a `runs_current` view is the documented default target and is what hints point to;
  raw `runs` stays queryable, and results from raw `runs` carry a banner if any hidden rows are
  present (designer's guess; the spec phase must settle the exact mechanism).
- **Invariant (tested):** hiding is read-time only; no ledger row is mutated or deleted.
- Surfaces in scope (each needs a test): `ls`, `find`, `get_run`/`show`, `run_sql` hints, `cite`,
  `claim register`/`validate`, `campaign add`/`conclude`/`review`, `resolve_pin`,
  `get_trust_state`, `graduate_product`, `lineage`, review-page bridge used by maraxiom
  (`review_build` must refuse a hidden run), and the session-start view (D7).
- Downstream propagation: a promoted product whose producing run becomes hidden drops to a
  non-promoted trust state *at read time* (no ledger rewrite).

### D4. Evidence tiers incl. spike (owner: fold in)
- Add `spike` below `exploratory` as an epistemic class: a throwaway probe of an assumption. Spike
  runs can be recorded (provenance) but can never support a finding above `tentative` and are
  never shown in session-start views.
- Reconcile the two vocabularies instead of adding a third: one documented mapping
  `stage_name → epistemic_class` (e.g. exploration→exploratory, calibration→calibrating,
  validation/ablation/production→confirmatory, spike→spike). Decide the fate of the in-the-wild
  non-canonical values (`"confirmatory"`, `"power"`, E9) — accept as aliases or lint-migrate.
- Undeclared stage: today defaults silently to `exploration`. Keep the default but *show* it as
  `undeclared` on reads so it is not mistaken for a choice. (Designer's guess.)
- Every run read shows its tier.

### D5. Supersession edges
`superseded_by` stops being free text. Edges are typed and validated:
run→run, finding→finding, sidecar→sidecar, doc→doc (P3), each with a reason and an actor, stored
append-only with an inverse index ("what replaced X", "what X replaced"). Existing free-text
`[status] superseded_by` values are parsed when they resolve to an id, otherwise reported by lint.

### D6. Distance from present (owner: show, don't decay)
Each run read carries facts, not a score: age; instrument versions behind current (per tracked
package, using D2); commits on the producing script since the run; whether it was ever reproduced
(another `current` run of the same sidecar sha with the same outcome — exact definition is a
builder's call); n where the result schema exposes it; `version_source`. No automatic state change.

### D7. Findings register and session-start view (owner: lightweight findings register)
- A **finding**: `question` (stable slug + text), `answer`, `status` ∈ {open, tentative, settled,
  superseded, refuted}, supporting run ids, evidence tier summary, `supersedes`, `last_reviewed`,
  author. Cheap to write after any run (CLI + MCP). Status is capped by evidence: `settled` needs at
  least one `current` confirmatory/validation run; spike/exploratory-only evidence caps at
  `tentative`.
- A finding whose supporting runs all become hidden flips (at read time) to `needs-recheck`, and
  appears as such in the session-start view — this is how a defect declaration reaches belief.
- Claims may cite findings; findings are not claims.
- **Session-start view:** generated, byte-capped (designer's guess: ≤ 2 KB), lists open and
  settled findings with tier and distance, plus `needs-recheck` items and active defect ranges.
  Never lists superseded items. Delivered by a new bathos plugin SessionStart hook (E17) and an
  MCP/CLI command for agents without the hook.
- Re-opening guard: creating a finding or experiment whose `question` matches a settled finding
  prints the settled answer and requires an explicit "reopen because …" reason. (Targets "twice
  proposed as new work", E14.) Matching method is a builder's call; exact-slug first.

### D8. Archive sweep and self-contained store (owner: bathos self-contained, configurable to share with praxia)
- `bth archive sweep` (name is builder's call): dry-run by default; proposes candidates from:
  `[status] stale`, hidden validity, superseded edges, unreferenced scripts (no runs in N days *and*
  not cited by any current finding — N must be declared and justified, A6), docs citing only
  hidden runs/findings, rotated telemetry. Applies in owner-approved batches through the existing
  `archive_experiment_bundle` path; `restore` keeps working.
- bathos owns its stores (docs/notes, archive bundles) by default inside the project's `.bth/`
  (primary data, like `archive_bundles` today — so the derived-data "default off" rule does not
  apply; the spec must state this classification explicitly).
- **Sharing with praxia (owner):** config can point the doc store at a project's `.praxia/docs`,
  in which case bathos uses praxia's frontmatter and status vocabulary (`superseded`, `archived`,
  `supersedes`) and its `archive/` tar.zst + manifest convention, so both tools see one set of
  docs. Location resolves: explicit argument > env var (`none`/empty disables) >
  `[tool.bathos]` in nearest `pyproject.toml` > `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml`
  > project `.bth/` default; expose which layer decided; fail loudly on a malformed file.
  bathos writes into `.praxia/docs` only when so configured (A5).
- Telemetry: rotate/compress by age into the archive store; never delete; keep an index.

### D9. Backfill order for asr and tev_design (owner: defect ranges first, then triage)
1. Declare the known aminx ranges (E12) and the asr LG-matrix window (E15) → run D2 resolution →
   auto-flag. Report counts by state before anything is hidden (dry-run), owner approves.
2. Seed findings from tev `claims_ledger.toml` (migration target, A4: map `evidential_status`,
   `instrument_wheel`, `refuted_or_superseded`) and asr `.verdicts/`; owner approves each batch.
   Retire the hand ledgers to read-only pointers once parity is shown.
3. Archive sweep proposals in batches, owner approves.

### D10. Resolutions from the builder check (all designer's calls; the owner should skim these)
- **Choke point.** One core `resolve_validity(run_ids) -> {id: Validity}` used by every surface
  (CLI and MCP share it); surfaces never re-implement the rule. Batched, not one fold per run.
- **Derived validity cache is allowed.** A2 forbids mutating history, not caching. Validity may be
  materialized into a rebuildable derived table (keyed by run id + hashes of the ledgers and
  registry it was computed from; rebuilt when any input hash changes). Version resolution (D2,
  `git show` per distinct `(git_hash, lock)` pair) is computed once and cached the same way. The
  spec phase must name the run source of truth (catalog vs project run log) the fold reads.
- **Clear semantics.** A defect-range flag cleared by the owner is a per-run exemption tied to the
  registry entry's id + revision; re-resolution respects it until the entry is revised.
- **`estimand_change` is not a blast state.** It is a comparability marker: both sides stay
  `current`; any finding, campaign or claim mixing runs across the boundary is refused/bannered
  as non-comparable. It never hides a run.
- **Partial membership.** `campaign conclude`, `claim register` and `graduate` refuse if any
  load-bearing run is hidden and list those runs; `lineage` and `campaign review` show hidden
  members with a banner. Nothing drops members silently.
- **`run_sql`.** To make "cannot accidentally cite" true for arbitrary SQL, the default
  connection exposes `runs` as the *current-only* view; full history is `runs_all` (and the
  explicit opt-in flag). Acceptance test: a raw `SELECT * FROM runs` never returns a hidden run.
  This changes the meaning of existing saved queries — the spec must list callers that depend on
  `runs` (migrate, repair, verify, compact use the base tables directly, not the view).
- **Predicate scope.** Predicates evaluate over run columns + `metadata` JSON params + the
  resolved package versions exposed as columns. A run lacking a field the predicate needs
  resolves to `suspect`, never to a silent non-match and never to `void`.
- **Commit ranges.** Matched only when the run's package source commit is resolvable (recorded
  provenance, or a wheel whose metadata/sha maps to a commit). Otherwise the entry's version range
  applies and an inferred match caps at `suspect`.
- **Registry scope in P1.** A per-user registry (resolved via the D8 config chain) that projects
  opt into, plus optional project-local entries; aminx entries are shared by asr and tev_design.
  Packages named in active registry entries are added to `track_packages` automatically (E19),
  and P1 also sets `track_packages` explicitly in both repos' config.
- **`archived` identity.** An archived script hides the runs whose `script_sha256` matches the
  archived bundle (E5's rule); the spec phase verifies the key and the latest-wins fold.

## 5. Phasing (owner: stop the leak first; full design now)
- **P1 — stop the leak:** D1, D2, D3 (all surfaces), tier + distance display (D4 display part, D6),
  D9 step 1, runner populates `dependency_provenance`.
- **P2 — belief:** D4 (spike tier, vocabulary reconciliation), D5, D7, D9 step 2.
- **P3 — cleanup:** D8, D9 step 3, doc supersession.

Each phase ships independently with its own spec section, tests and PR.

## 6. Constraints
- Python 3.13, cyclopts CLI + FastMCP mirror tool-for-tool; core logic in modules, thin layers.
- Ledgers append-only, latest-wins folds; SLURM-safe atomic writes for any new fragment type.
- Schema migration via `bth migrate`; new fields nullable; old catalogs keep working.
- MCP errors use the existing `BathosErrorCode` envelope; new refusal codes registered with
  resolution hints (e.g. `RESULT_HIDDEN`, `RESULT_SUSPECT_UNACKNOWLEDGED`).
- Tests never touch the real `~/.bth` (guard enforced under pytest). No whole-suite local runs.
- Everything that produces a citable count during backfill is a tracked script + sidecar.

## 7. Left to the builder / spec phase
- Exact registry file format and location; cross-project sharing of defect ranges.
- `run_sql` hiding mechanism; acknowledgment field shape for `suspect`/`unverifiable`.
- "Reproduced" definition; question-matching method for the reopen guard.
- Why `dependency_provenance` is null today and how the runner should fill it.
- Telemetry rotation thresholds (must be justified).
- Session-start byte cap and content ordering.

## 8. Open questions only the owner can answer (none blocking P1)
- Should `suspect` (inferred-version defect match) be hidden instead of bannered, once the owner
  has seen P1 dry-run counts?
