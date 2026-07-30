---
session_id: f6e4fb21
topic: Spec for bathos repair tooling — what should `bth repair` (or similar) look like?
task_type: constrained-technical
winner: Composite MVP-3 design (pre-mortem revised): `bth repair` standalone subcommand, thin action-layer over bth verify findings. Sentinel two-track: .tmp auto-delete; .bak → quarantine (not auto-delete). Corrupt non-empty fragments → `~/.bth/catalog/quarantine/<project_slug>/<run_uuid>_<timestamp>.parquet` (namespaced, collision-safe) with JSON manifest recording run_uuid, project_slug, script_name, mtime, detected_error_type. `bth repair restore <uuid>` validates schema compatibility before restoring and schedules compaction. Pre-force_rebuild: rename bathos.db → bathos.db.bak, rotate (keep 1), log size warning if catalog exceeds threshold; --no-backup for CI. CRITICAL gate: if warm DB has postmortem annotations or output_metadata, print explicit irreversibility warning and require --acknowledge-warm-loss in addition to --yes before proceeding with rebuild. Action list re-validated immediately before execution (not at summary time); structured post-action log written to .bth/repair_YYMMDD_HHMMSS.log. MVP scope: gaps 1+2+5. Warm→cool re-export as immediate follow-on backlog item. MCP mirror tool-for-tool.
created_at: 2026-06-08T15:26:18.267133+00:00
---

# Brainstorm: Spec for bathos repair tooling — what should `bth repair` (or similar) look like?

## Problem Frame
seems good

## Idea Pool
- [user] Here's the PEGS map from recon:
- [user] **PROCESSES:**
- [user] P1: write_run() — atomic .tmp→rename, no backup, POSIX-safe
- [user] P2: compact() — reads cool fragments into DuckDB, advisory lock, CorruptDatabaseError on bad header; force_rebuild deletes bathos.db before rebuilding (no pre-deletion backup)
- [user] P3: migrate_catalog() — rewrites fragments for schema drift; has .bak backup + .tmp write + rename + .bak cleanup; restores .bak on failure; logs CRITICAL if restore fails
- [user] P4: archive() — per-partition .tmp→rename, manifest with SHA256 (schema_version ≥ 2)
- [user] P5: verify_cool/warm/archive() — read-only integrity checks, detect sentinels and corruption
- [user] P6: repair (partial) — compact --force-rebuild, manual sentinel cleanup, sync re-pull
- [user] **EVENTS (failure triggers):**
- [user] E1: disk full / SLURM kill during write → orphaned .tmp
- [user] E2: crash mid-compaction → partial INSERTs (pre-fix: committed; post-fix: rolled back via transaction)
- [user] E3: concurrent compact → race (pre-fix) or CompactionLockedError (post-fix)
- [user] E4: corrupt bathos.db header or _schema_meta inaccessible
- [user] E5: interrupted migration → .bak + .tmp left on disk
- [user] E6: fragment schema mismatch (drift)
- [user] E7: archive SHA256 mismatch (post-archive mutation)
- [user] E8: archive partition file missing
- [user] E9: truncated sync fragment (rsync --ignore-existing skips re-download)
- [user] E10: stale .bth_compact.lock (PID dead)
- [user] **GOALS:**
- [user] G1: Atomicity — writes complete or leave no trace; compaction transactional
- [user] G2: Idempotency — compact/migrate/archive re-runnable safely
- [user] G3: Auditability — log all recoveries; telemetry on critical paths
- [user] G4: Minimal UX surface — no multi-step procedures; ≤2 commands to recover
- [user] G5: Data integrity — never silently lose rows; always detectable corruption
- [user] G6: Graceful degradation — cool is source of truth; warm always rebuildable from cool
- [user] G7: Backward compat — old archives (no SHA256) warn, don't error
- [user] **STATES (10 defined):**
- [user] Healthy — all tiers clean
- [user] Compaction-hungry — cool ahead of warm, no sentinels
- [user] Interrupted compaction — stale lock, partial warm
- [user] Interrupted migration — .bak + .tmp present
- [user] Corrupt warm (header) — IOException on connect
- [user] Corrupt warm (structural) — _schema_meta inaccessible
- [user] Archive mismatch — SHA256/row-count wrong
- [user] Truncated sync fragment — incomplete rsync transfer
- [user] Abandoned lock — stale .bth_compact.lock
- [user] Empty catalog — fresh install
- [user] All states except 7 (archive) transition back to State 1 via: remove sentinel + re-run operation, OR compact --force-rebuild.
- [user] **ANALOGUES:**
- [user] git: fsck (detect) + reflog (recover) + repack (rebuild) — bth verify ≈ fsck, force-rebuild ≈ repack
- [user] SQLite: PRAGMA integrity_check + RECOVER extension + WAL mode for crash safety
- [user] rsync: --partial + --checksum; bathos post-fix-6 mirrors this with pq.read_metadata() post-pull scan
- [user] cargo: .lock files for advisory locking; re-download on checksum mismatch
- [user] All tools share: atomic write-then-rename, checksums, sentinels to detect interruption, a "rebuild from source of truth" escape hatch
- [user] **Key insight from analogues:** bathos already has most primitives (atomic writes, checksums, sentinels, advisory locks, transactions). What's missing is the *unifying repair layer* that reads verify output and applies the right fix per state — the equivalent of git fsck | xargs git prune or sqlite3 RECOVER.
- [ai] UX-A: `bth repair` as standalone subcommand with --tier cool/warm/archive/all mirroring bth verify's flag structure — explicit, discoverable, consistent with noun-verb pattern
- [ai] UX-B: `bth verify --fix` flag — co-locates detection and repair on one command, consistent with eslint/ruff --fix convention; risk: conflates read-only audit with mutation
- [ai] UX-C: Two-phase plan/apply — `bth verify` emits `.bth/repair-plan.toml`, then `bth repair --apply` executes it; gives user a reviewable artifact before any mutation
- [ai] UX-D: Per-command --repair flags (bth compact --repair, bth migrate --repair, bth sync --repair) — each command owns its tier's repair; scattered but composable
- [ai] UX-E: `bth doctor` — diagnostic-and-fix subcommand in style of brew doctor / cargo fix; higher ceremony, signals "recovery workflow not normal operation"
- [ai] BACKUP-A: Timestamped snapshot copy — `shutil.copy2(bathos.db, bathos.db.bak.YYYYMMDD_HHMMSS)` before deletion; keep 3 most recent; recovery is manual cp + rename
- [ai] BACKUP-B: Content-addressed backup — SHA256 bathos.db before deletion, write to `.bth/backups/<sha256[:12]>.db` only if hash not already present; zero waste on repeated rebuilds; `bth compact --restore <sha256-prefix>`
- [ai] BACKUP-C: Fixed `latest.db` symlink — always copy to `.bth/backups/latest.db` before deletion; one extra DB on disk, obvious recovery UX; `bth compact --restore-last`
- [ai] BACKUP-D: In-place rename to `.bak` — rename bathos.db → bathos.db.bak instead of deleting; rebuild writes fresh bathos.db; .bak survives until next force_rebuild; `--no-backup` flag for CI
- [ai] BACKUP-E: DuckDB `EXPORT DATABASE` to portable per-table CSV/Parquet before deletion — human-readable, diff-able, survives schema migrations; higher disk use; recovery via `IMPORT DATABASE`
- [ai] BACKUP-F: No file backup; instead print diff of warm-only data (postmortem annotations, output metadata) that would be lost vs cool tier; require explicit --yes before proceeding
- [ai] REEXPORT-A: `bth repair --from-warm` writes reconstructed cool fragments to repair_staging/, then atomically moves to cool/ only if no existing fragment present; --overwrite flag to replace
- [ai] REEXPORT-B: Selective re-export via --run-id / --project slug filters on DuckDB query — targeted recovery without touching full cool tier
- [ai] REEXPORT-C: Schema-downcast registry in schema.py — explicit named mapping from warm schema fields to cool schema fields (including metadata JSON extraction); version-aware, testable
- [ai] REEXPORT-D: Round-trip fidelity test in CI — after bth run + bth compact, assert re-exporting warm row reproduces field-identical cool fragment; catches schema drift at CI time not recovery time
- [ai] QUARANTINE-A: Move bad fragments to `~/.bth/catalog/quarantine/` with JSON manifest (original path, failure reason, timestamp, run UUID); `bth verify --restore <uuid>` moves them back
- [ai] QUARANTINE-B: Rename in-place to `.parquet.bad` / `.parquet.empty` with a companion `.reason` sidecar file (error message + timestamp); keeps fragments co-located, rsync-safe
- [ai] QUARANTINE-C: Two-track repair — sentinels (.tmp/.bak) auto-delete immediately (they're incomplete writes, not data); corrupt non-empty fragments go to quarantine; different risk profile for each class
- [ai] QUARANTINE-D: Atomic batch repair with rollback manifest — write `repair_YYMMDD.json` undo log before touching anything, execute moves/deletes, `bth repair --undo repair_YYMMDD.json` reverses it
- [ai] QUARANTINE-E: Per-fragment interactive triage `bth verify --triage` — shows run UUID, script name, timestamp per bad fragment; prompt [q]uarantine / [d]elete / [s]kip / [a]ll-remaining
- [ai] CONFIRM-A: Dry-run is default, --apply required to execute — flip current pattern; first invocation always prints plan and exits
- [ai] CONFIRM-B: Tiered safety model — classify ops as `safe` (additive, auto-apply), `reversible` (auto-backup then proceed), `destructive` (requires --yes + named target); different defaults per tier
- [ai] CONFIRM-C: Print repair plan as executable shell script to stdout (`bth repair --plan > repair.sh`); user inspects and runs manually; no interactive prompt path
- [ai] CONFIRM-D: Single confirmation after summary diff — "3 sentinels will be deleted, 1 fragment quarantined. Apply? [y/N]"; --yes skips prompt for automation
- [ai] CONFIRM-E: Auto-backup-then-proceed — any destructive op snapshots target to `~/.bth/catalog/backups/<timestamp>_<name>` before mutating, prints backup path, proceeds without prompt; --no-backup for CI
- [ai] CONFIRM-F: Structured plan file workflow — --dry-run emits JSON plan with action/target/reversible fields per operation; `bth repair apply <plan-file>` ingests and executes it; fully auditable apply-from-file
- [ai] MVP-1: Sentinel-only (gap 1) — --fix on bth verify removes .tmp/.bak and empty fragments; zero new modules, purely additive to verify.py; unambiguous remediation for all findings
- [ai] MVP-2: Safety-first (gaps 1 + 5) — sentinel cleanup + pre-rebuild DB backup; two isolated changes to verify.py and compact.py; addresses highest-consequence silent failure
- [ai] MVP-3: Cool-tier complete (gaps 1 + 2 + 5) — sentinel cleanup, corrupt fragment quarantine, DB backup; all three harden the cool tier and its safety net; natural unit since cool is the unattended SLURM write path
- [ai] MVP-4: repair.py stub (gaps 1 + 2 + 5 + skeleton for 3 + 6) — introduces repair.py module and `bth repair` command; gaps 1/2/5 fully implemented, archive and re-export as NotImplementedError stubs with clear messages
- [ai] MVP-5: Non-re-export (gaps 1 + 2 + 3 + 5 + 6) — everything except warm→cool re-export; archive SHA256 mismatch handled as quarantine-and-log only; excludes the one gap that mutates authoritative warm with derived data
- [ai] MVP-6: Minimal-surface (gaps 1 + 6 shell) — `bth repair` command exists and dispatches to sentinel cleanup; no repair module, just wiring; wins user-facing affordance (memorable entry point) with minimal implementation debt; gaps 2-5 land incrementally
- [user] converge

## Decision Log
- [ACCEPT] Quarantine manifest includes run_uuid, project_slug, script_name, mtime, detected_error_type (full provenance): Pre-mortem scenario 1: SLURM in-flight fragments quarantined as corrupt had no provenance in manifest; impossible to determine which run they belonged to or whether they were valid. Full provenance required for any meaningful restore decision.
- [ACCEPT] bathos.db.bak rotation policy: keep 1, delete on next force_rebuild; log warning if catalog dir exceeds quota threshold: Pre-mortem scenario 2: no rotation filled HPC home dir quota (50GB); SLURM writes then failed silently with ENOSPC. .bak proliferation is a latent data-loss vector on cluster environments.
- [ACCEPT] --acknowledge-warm-loss gate prints count of postmortem records and output_metadata entries at risk before DB rebuild proceeds: Pre-mortem scenario 3: researcher typed through irreversibility warning without understanding 40 postmortem records would be gone; .bak was the corrupt file so no recovery path. Concrete count at confirmation time makes the loss legible.
- [ACCEPT] bth repair restore validates schema version compatibility before accepting fragment back into cool tier: Pre-mortem scenario 4: restoring a v5 fragment after v6 compaction caused next bth compact to crash on schema mismatch. Restore must gate on schema compatibility or the repair leaves the catalog in a worse state than before.
- [ACCEPT] bth repair adds recency guard: refuse to quarantine fragments with mtime < 60s (likely in-flight SLURM writes): Pre-mortem scenario 1 root cause: repair ran concurrently with active SLURM array jobs. A 60s mtime window is a cheap guard against classifying in-progress atomic writes as corrupt.
- [DEFER] Warm→cool re-export deferred to immediate follow-on backlog item: Pre-mortem scenario 3 shows this gap causes permanent warm-only data loss on DB rebuild. Deferred because it requires schema-downcast registry and round-trip fidelity testing that exceed MVP scope — but the --acknowledge-warm-loss gate explicitly surfaces this limitation at the moment of risk.

## Assumptions

| # | Assumption | Consequence if false |
|---|---|---|
| A1 | POSIX `rename()` is atomic on the target filesystem (Linux ext4/xfs, macOS APFS) | Sentinel cleanup or quarantine moves could leave catalog in half-moved state |
| A2 | 60s mtime recency guard is sufficient to exclude all in-flight SLURM writes | Recent-but-stuck fragments (job hung mid-write > 60s) would still be misclassified as corrupt |
| A3 | A single `.bak` rotation slot is sufficient (one previous state is enough for recovery) | User runs two force_rebuilds in rapid succession and loses the first backup; mitigated by logging backup path at each rotation |
| A4 | Postmortem annotations and `output_metadata` (warm DB `metadata` JSON column) are the only warm-tier data not recoverable from cool fragments | If other warm-only fields exist, `--acknowledge-warm-loss` warning would be incomplete |
| A5 | A fragment that fails `pq.read_table()` with mtime > 60s is corrupt (not a transient filesystem error) | Rare transient errors quarantine valid fragments; mitigated by quarantine manifest + restore path |
| A6 | `bth repair` will not run concurrently with active SLURM jobs for the same project | Recency guard reduces but does not eliminate race; user is responsible for not running repair during active submissions |

## TBDs

| # | TBD | Blocking |
|---|---|---|
| T1 | Exact schema-compatibility check for `bth repair restore` — field name comparison only, or compare `schema_version` integer? | Needed before restore is implemented |
| T2 | Quota-warning threshold value — hardcoded (e.g. catalog dir > 5GB) or read from `.bth/config.toml`? | Needed before backup rotation is implemented |
| T3 | Whether `mcp__bathos__repair` mirrors both scan-and-quarantine and restore, or scan-only | Needed before MCP mirror is implemented |
| T4 | Whether `repair.py` calls `verify_cool()` / `verify_warm()` as imported functions or shares detection logic inline | Architecture decision; avoid duplication |
| T5 | Archive repair (gap 3) scope: quarantine-and-log only, or full SHA256-recompute and manifest update? | Backlogged; not blocking MVP |
| T6 | Warm→cool re-export (gap 4): schema-downcast registry design, round-trip fidelity test, staging dir lifecycle | Backlogged; immediate follow-on ticket |

## Pre-mortem Record

Six failure scenarios surfaced by adversarial subagent panel (all incorporated into decision log):

1. **SLURM race / quarantine misclassification** — repair classifies in-flight fragments as corrupt during an active SLURM array job; manifest lacks provenance to identify them. *Fix: recency guard (mtime < 60s), full provenance in manifest.*
2. **Backup accumulation fills HPC quota** — no `.bak` rotation; repeated force_rebuilds fill 50GB home dir; SLURM writes fail silently with ENOSPC. *Fix: rotate-keep-1 policy, catalog-size warning.*
3. **Warm-only data silently destroyed** — researcher types through rebuild warning without understanding 40 postmortem records are gone; `.bak` is the corrupt file. *Fix: `--acknowledge-warm-loss` prints concrete record count.*
4. **Stale restore crashes compaction** — restoring a v5 fragment after v6 compaction causes next `bth compact` to crash on schema mismatch. *Fix: restore validates schema compatibility before accepting fragment.*
5. **Stale confirmation diff** — slow SSH partial render; researcher confirms stale summary; valid in-progress `.tmp` deleted. *Fix: re-validate action list immediately before execution; write structured post-action log.*
6. **Quarantine path collision** — two projects share relative fragment path; second quarantine silently overwrites first. *Fix: quarantine path namespaced by `<project_slug>/<run_uuid>_<timestamp>`.*

## Acceptance Criteria

**Sentinel cleanup (.tmp files)**
- Given the cool tier contains `.tmp.parquet` files with mtime > 60s, When `bth repair --yes` runs, Then all such files are deleted and each deletion is recorded in `~/.bth/catalog/repair_<timestamp>.log`
- Given a `.tmp.parquet` file has mtime < 60s, When `bth repair` runs, Then it is listed in the plan as "skipped (in-flight)" and is not deleted

**Sentinel quarantine (.bak files)**
- Given the cool tier contains `.bak` files, When `bth repair --yes` runs, Then each is moved to `~/.bth/catalog/quarantine/<project_slug>/<run_uuid>_<timestamp>.parquet` with a JSON manifest entry (not deleted)
- Given a `.bak` file is quarantined, When `bth repair restore <uuid>` runs and schema is compatible, Then the file is moved back to its original cool-tier path

**Corrupt fragment quarantine**
- Given a `.parquet` file fails `pq.read_table()` AND has mtime > 60s, When `bth repair --yes` runs, Then it is moved to quarantine with manifest entry recording `run_uuid`, `project_slug`, `script_name`, `mtime`, `detected_error_type`
- Given a quarantined fragment has a schema version incompatible with current `COOL_SCHEMA`, When `bth repair restore <uuid>` runs, Then the command exits with a schema-mismatch error and the fragment remains in quarantine

**Pre-rebuild backup**
- Given `bathos.db` exists, When `bth compact --force-rebuild` runs, Then `bathos.db` is renamed to `bathos.db.bak` before the DB is deleted
- Given a previous `bathos.db.bak` exists, When `bth compact --force-rebuild` runs again, Then the previous `.bak` is deleted and a new one is written (rotation=1)
- Given `--no-backup` is passed, Then no `.bak` is created and the DB is deleted directly

**Warm-only data protection gate**
- Given `bathos.db` contains ≥1 postmortem annotation or `output_metadata` entry, When `bth compact --force-rebuild --yes` runs WITHOUT `--acknowledge-warm-loss`, Then the command exits non-zero with a message stating the count of at-risk records
- Given the above, When `--acknowledge-warm-loss` is also passed, Then the rebuild proceeds

**Post-action log**
- Given `bth repair --yes` takes any actions, Then a structured log is written to `~/.bth/catalog/repair_<YYMMDD_HHMMSS>.log` listing each action with: action type, source path, destination path (if move), outcome

**MCP mirror**
- Given `bth repair` is implemented, Then `mcp__bathos__repair` MCP tool exists with equivalent parameters

**Runner-up considered:** MVP-4 (repair.py with NotImplementedError stubs for archive repair and re-export). Steelmanned: stubs lock in a stable CLI surface before three separate follow-on PRs diverge on flag names. Rejected: stubs without tests create silent interface debt; `--acknowledge-warm-loss` may need revision once warm→cool re-export ships, making early surface-freezing counterproductive.
