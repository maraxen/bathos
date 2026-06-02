# Results Management Design — Backlog Item #791

**Date:** 2026-06-02
**Status:** Design / Exploratory
**Track:** design (no implementation in this document)
**Item:** Results management — output convention, file-count utilities, management interface

---

## Background and Problem Statement

bathos tracks where experiments write their output artifacts via `Run.output_paths` (a
list of absolute path strings, registered at `bth run` time via `--out`).  The warm-tier
DuckDB catalog enriches these with per-file metadata (`output_metadata` JSON blob:
status, size_bytes, mtime_unix, sha256) collected during `bth compact`.

Despite this data foundation, nothing is usable directly:

- There is no standard location or naming convention for where experiments should write
  output files; callers pass arbitrary absolute paths.
- The experiment scaffold (`bth new-experiment`) emits results as JSON to stdout; it does
  not set `output_paths` at all.
- There is no CLI surface to query output files — counts, disk usage, missing-file rate,
  growth over time — without writing raw DuckDB SQL.
- There is no management interface: no structured `bth outputs list <run_id>`, no
  cross-run summaries, no bulk cleanup of orphaned or superseded artifacts.

This document maps the design space and surfaces key decisions before any implementation
is specced.

---

## Current System Inventory

### Data model (what exists)

| Field | Tier | Type | Set by |
|---|---|---|---|
| `output_paths` | cool + warm | `list[str]` | caller: `bth run --out <path>` |
| `output_metadata` | warm only | JSON blob (`list[{path, status, size_bytes, mtime_unix, sha256}]`) | `compact._collect_output_metadata()` at compact time |

`output_metadata` is collected exactly once: the first time a run is ingested by
`compact`.  Subsequent compactions skip the run (runs are keyed by `id`; existing rows
are not re-ingested).  Postmortem fields are the only warm-tier data that is updated
on re-compact.

### Existing query surface

- `bth find --output-file <glob>` — filters runs by path pattern (uses cool-tier
  `output_paths`; falls back to warm-tier `metadata.output_files` if present — note:
  that fallback key is inconsistent with `output_metadata`).
- `bth sql "SELECT ..."` — raw DuckDB; the only way to query aggregate output stats today.
- `checker.check_output_files(run)` — verifies presence/readability of a single run's
  output paths; exposed in `bth check`.
- `rich_fmt.render_run_detail` — shows up to 3 output paths in the Outcome panel.
- `viz/data.py:RunDisplay` — includes `output_paths: list[str]` in the web display
  TypedDict, but `output_metadata` is not projected through.

### What the scaffold generates

`bth new-experiment` produces a script template that writes results as a JSON blob to
stdout (consumed via `BTH_RESULTS_PATH` for outcome evaluation).  The template has no
`--out` flag and makes no attempt to register output file paths.  This is the canonical
path new experiments take, and it does not participate in results management at all today.

---

## Design Dimensions

### 1. Output Directory Convention

**The gap:** experiments write to arbitrary paths today.  There is no `BTH_OUTPUT_DIR`
env var, no per-run output subdirectory, no automatic naming from run ID or project slug.

**Option A — Fully explicit (status quo):** the caller is responsible for output paths;
bathos only records what it is told.
- Pros: zero friction; works for all script types and environments.
- Cons: fragmented storage; cross-project summaries require all callers to cooperate;
  scripts that don't pass `--out` are invisible to the catalog.

**Option B — Convention-only (soft):** document and scaffold a recommended output
location (`outputs/<run_id>/` relative to project root, or
`~/.bth/outputs/<project_slug>/<run_id>/` as a default out-of-tree location), and inject
`BTH_OUTPUT_DIR` as an env var at `bth run` time so scripts can pick it up without any
CLI flag.  bathos still only records paths explicitly registered via `--out`; the
convention is advisory.
- Pros: interoperable with existing callers; no schema change; easy to land.
- Cons: scripts that ignore `BTH_OUTPUT_DIR` still produce unregistered files; convention
  drift without enforcement.

**Option C — Convention + auto-registration:** bathos creates a per-run output directory
(`BTH_OUTPUT_DIR`), injects the path, and at run end scans the directory to
auto-register all files written there as `output_paths`.
- Pros: zero friction for compliant scripts; comprehensive catalog coverage without any
  explicit `--out` calls.
- Cons: scans may pick up large, transient, or unexpected files; SLURM jobs writing to
  per-node scratch paths would not benefit from the local convention; auto-registration
  may surprise users who write temporary files mid-run.

**Option D — Convention + per-run manifest opt-in:** bathos injects `BTH_OUTPUT_DIR`
(created lazily), registers only the files the script explicitly lists in a per-run
manifest at a known path (`$BTH_OUTPUT_DIR/outputs.json`), and does not auto-scan.
- Pros: predictable; no surprise file captures; explicit contract between script and
  catalog.
- Cons: requires scripts to write the manifest file; new pattern to learn.

**Key constraint — SLURM/cluster:** cluster jobs write to cluster scratch
(`/scratch/$SLURM_JOB_ID/`, `/tmp/`, or project-specific paths on remote nodes).  Any
convention that assumes a local path will be empty on the submission node.  A per-run
directory under `~/.bth/outputs/` is meaningful only for local runs.  The cluster output
story likely needs to be scoped separately.

### 2. File-Count and Disk-Usage Utilities

**The gap:** `output_metadata` contains `size_bytes` per file per run but is only
accessible via raw SQL today.  There is no `bth outputs summary` command.

**Option A — Warm-catalog aggregate query (`bth outputs summary`):**
```
bth outputs summary [--project <slug>] [--since 30d]
```
Reads and parses `output_metadata` JSON blobs from DuckDB; computes:
- Total registered file count
- Total bytes (registered, present, missing)
- Missing-file rate (count/fraction)
- Largest files / runs by output size
- Growth over time

Requires warm catalog (`bth compact` first).  Fast.  May be stale if files moved/deleted
since last compact.

**Option B — Live filesystem scan:** re-check current filesystem state rather than
querying the catalog snapshot.  More accurate at the cost of disk I/O; could be a
`--live` flag on the summary command that re-stats each registered path.

**Option C — Integrate into `bth verify`:** add an `outputs` tier to `bth verify
--tier outputs` that checks file presence and size drift against the compact-time snapshot.
This keeps the "integrity checking" concept unified with the existing verify command.

**Recommended composite:** a dedicated `bth outputs` subcommand group is cleaner than
extending verify; verify remains for catalog integrity, outputs for file management.
A `--live` flag on `bth outputs summary` / `bth outputs list` bridges the freshness gap.

**Stale-snapshot problem:** any summary that only reads `output_metadata` will report
files as present that may have been deleted.  This should be documented clearly in help
text; the `--live` flag addresses it for interactive use.

### 3. Management Interface

**The gap:** there is no way to list, inspect, or delete output files associated with a
run or set of runs other than raw SQL.

**Option A — `bth outputs list <run_id>`:** structured output of registered files with
status from the compact-time snapshot.  Simple; read-only; uses existing `output_metadata`.

**Option B — `bth outputs list <run_id> --live`:** same but re-checks the filesystem for
current status.  More accurate; adds I/O.

**Option C — `bth outputs prune`:** bulk deletion of output files for runs matching a
filter (e.g., `--outcome fail --older-than 30d`).  High risk: destructive, irreversible.
Would require `--dry-run` by default, printed diff, and explicit `--confirm` flag.
The catalog update question is non-trivial: if files are deleted, should `output_paths`
and `output_metadata` be nulled out in the warm tier?  A warm-tier UPDATE path for
output fields does not currently exist (only postmortem fields are updated post-ingest).

**Option D — `bth outputs link <run_id> <path>`:** retroactive registration of output
paths not captured at run time (useful when scripts wrote files before output path
tracking was adopted, or when `--out` was omitted).  Requires a warm-tier UPDATE path
for `output_paths` and a re-collection of `output_metadata` for the new path.

**Destructive operation constraint:** any deletion command must:
- Default to `--dry-run` (print what would be deleted without deleting).
- Print a clear diff of files to be removed.
- Require explicit `--confirm` (not a `--yes` flag — an actual typed confirmation for
  destructive multi-run operations).
- Address the catalog update question explicitly before implementation.

### 4. Scaffold Gap

**The gap:** `bth new-experiment` generates scripts that write scalar metrics to stdout
(for outcome evaluation via `BTH_RESULTS_PATH`) but do not register output file paths.
The two mechanisms — scalar metrics vs. artifact files — are not documented together.

**Option A — Add `--out` documentation to the scaffold template:** add a commented
example showing how to pass `--out` to `bth run` at the call site.  Low-code change;
does not change the script template itself.

**Option B — Inject `BTH_OUTPUT_DIR` usage into template:** the template reads
`os.environ.get("BTH_OUTPUT_DIR", ".")` and demonstrates writing an output file there.
Closes the gap end-to-end when combined with auto-registration (Option C above) or
the manifest pattern (Option D above).

**Option C — Explicit two-phase documentation:** document the "scalar metrics" (stdout
JSON → outcome evaluation) vs. "artifact files" (registered paths → output catalog)
distinction explicitly in the scaffold template, CLAUDE.md, and the `bth run` help text.
This is complementary to any code change and should happen regardless.

### 5. Output Metadata Refresh

Currently `output_metadata` is captured exactly once at compact time.  If files move,
grow, or are deleted after the first compact, the catalog is stale.

**Options:**
- **Accept stale snapshots (status quo):** document the limitation; provide `--live` flag
  on listing commands.  No new catalog writes needed.
- **`bth outputs refresh <run_id>`:** on-demand re-stat of registered files, updating
  `output_metadata` in the warm tier.  Requires a new UPDATE path in compact/catalog.
- **Refresh on every compact:** always re-stat all registered files for all runs during
  `bth compact`.  Comprehensive but potentially expensive for catalogs with many runs
  and large output file sets; may slow compact significantly.
- **Background refresh:** lazy refresh triggered by `bth outputs list --live` or
  `bth outputs summary --live`; writes updated `output_metadata` back to the warm tier
  as a side effect.

### 6. MCP Surface

The MCP server currently has no output-management tools.  The mirror-CLI-tool-for-tool
design principle (CLAUDE.md) applies.

**Consideration:** destructive MCP tools (file deletion) are harder to gate than CLI
commands; the user cannot interactively confirm a deletion in an agentic pipeline.  The
design should decide upfront whether prune/link tools will be exposed to agents, and if
so what confirmation contract they use.

One option: expose only read-only MCP tools (`list_outputs`, `outputs_summary`) in phase 1;
defer destructive MCP tools until the confirmation contract is designed.

---

## Interaction with Existing Subsystems

| Subsystem | Touch points |
|---|---|
| `runner.py` | Any `BTH_OUTPUT_DIR` injection happens here; auto-registration of directory scan results at run end |
| `compact.py` | `_collect_output_metadata` would need an update path for refresh; currently a private helper not callable post-ingest |
| `checker.py` | `check_output_files(run)` already does per-run file verification; natural engine for `bth outputs check` |
| `verify.py` | Could absorb an `outputs` tier for catalog-vs-filesystem drift checking |
| `rich_fmt.py` | Truncates at 3 paths today; needs a dedicated `render_output_list` for `bth outputs list` |
| `new_experiment.py` | Scaffold template needs updating to surface the two-phase pattern |
| `mcp.py` | Mirror tools needed for new CLI additions; destructive tools need explicit design |
| `schema.py` | No schema change likely required; `output_metadata` is already a warm-tier JSON blob |
| `viz/data.py` | `RunDisplay` includes `output_paths` but not `output_metadata`; may need extension for rich web display |

---

## Dependency #139

The CLAUDE.md backlog table notes item #791 depends on #139, but #139 is not present in
the current backlog table (it was presumably concluded or archived before the table was
trimmed).  The scope of #139 is unknown from the source code alone.

Before scoping the implementation of #791, the dependency should be clarified:
- What was #139 (the predecessor item)?
- Was it completed, and if so what contract or artifact did it establish?
- If it was not completed, does #791 block on it?

---

## Summary of Key Decisions

1. **Output directory convention:** choose between fully explicit (status quo), soft
   convention + `BTH_OUTPUT_DIR` injection, or convention + auto-registration / manifest.
   The SLURM cluster constraint (files on remote scratch) limits options that assume a
   local output directory.

2. **Metadata freshness:** accept stale compact-time snapshots (document the limitation)
   or build a refresh mechanism.  Refresh-on-compact is safer but expensive for large
   catalogs.

3. **Management interface scope:** read-only listing and summary first (low risk), or
   include destructive pruning and retroactive linking from the start.  A phased rollout
   is lower risk.

4. **Scaffold update:** close the gap between scalar-metrics emission (stdout JSON) and
   artifact registration (output_paths) by updating the template; decide whether to use
   `BTH_OUTPUT_DIR` injection or explicit `--out` documentation.

5. **MCP surface:** mirror all new CLI subcommands as MCP tools; decide whether
   destructive tools (prune, link) should be exposed to agents at all.

6. **Single subcommand group vs. extensions:** `bth outputs` as a dedicated subcommand
   group is cleaner than piecemeal additions to `bth find`, `bth show`, and `bth verify`.
   Confirm the user wants a new top-level group before scaffolding it.

7. **`bth find --output-file` fate:** the existing glob filter in `bth find` overlaps
   with any new `bth outputs` surface.  Decide whether to keep it as-is, deprecate it,
   or migrate it to use `output_metadata` for richer warm-tier queries.

---

## Open Questions

1. What was dependency #139, and is it complete?  The CLAUDE.md backlog table does not
   describe it, and the source code has no obvious trace.

2. Should this sprint item deliver a design document only, or also an initial CLI
   implementation?  The item title says "management interface" but the sprint track says
   "design", which implies spec-only for this session.

3. Is the SLURM/cluster output file story in scope for v0.8?  `BTH_OUTPUT_DIR` would
   be meaningless for cluster jobs writing to remote scratch.  Should the convention be
   scoped to local-only runs, with cluster output tracking as a separate future item?

4. Should `output_metadata` be refreshed on every `bth compact`, or only on first ingest?
   Refreshing has a wall-clock cost; accepting stale snapshots has a correctness cost.

5. Is destructive output pruning in scope for this item?  If so, should it target this
   sprint or a subsequent one?

6. Should `bth outputs link <run_id> <path>` (retroactive registration) be in scope?
   This is useful for pre-#791 runs but requires a warm-tier UPDATE path not currently
   used for output fields.

7. Should the MCP tool for output listing be a new `list_outputs` tool, or should
   `get_run` be enriched to return parsed `output_metadata` in a structured field rather
   than a raw JSON string?

8. How does the `--output-file` filter in `bth find` relate to the new `bth outputs`
   subcommand?  There is also an inconsistency in `query._filter_runs_by_output_file`:
   it checks `metadata.output_files` as a fallback key, but the warm-tier column is
   `output_metadata`.  This inconsistency should be resolved regardless of the broader
   design direction.

---

## Recommended Phasing

Before writing an implementation spec, the user should answer questions 1, 2, and 3 above.
With those answers in hand, a narrower phase-1 spec can be written:

**Phase 1 (read-only, local) — v0.8 candidate:**
- Fix the `bth find` / `output_metadata` key inconsistency in `query.py`.
- `bth outputs list <run_id>` — structured per-file listing with live/snapshot modes.
- `bth outputs summary [--project] [--since]` — aggregate counts and disk usage.
- Scaffold template update to document the two-phase (metrics vs. artifacts) pattern.
- MCP mirror tools for list and summary (read-only).

**Phase 2 (convention) — future sprint:**
- `BTH_OUTPUT_DIR` env var injection in runner.
- Soft convention documentation and scaffold update.
- Decide auto-registration vs. manifest opt-in based on cluster constraints.

**Phase 3 (management) — future sprint:**
- `bth outputs prune` (destructive, dry-run default).
- `bth outputs link` (retroactive registration).
- Refresh mechanism for `output_metadata`.
- Destructive MCP tool design with confirmation contract.

This phasing keeps v0.8 tractable and defers the higher-risk and cluster-dependent
decisions to future sprints where they can be designed with full context.
