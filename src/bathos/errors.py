"""Error codes and resolution hints for bathos MCP tools.

Provides a unified BathosErrorCode enum (61 members) that includes:
- 11 aliased codes from GateErrorCode (pre-registration gate failures)
- 5 codes for domain exceptions (catalog, campaign, sidecar, export, invalid params)
- later families: trust ledger, MCP auth, artifact archival, document authoring, the run log, output tiering,
  and the sidecar-lock family (lock_ref_write_failed, lock_not_found, index_dirty; spec 261026)

All MCP tools return envelopes with error_code values from this enum.
"""

from __future__ import annotations

from enum import Enum
from typing import Any


class BathosErrorCode(str, Enum):  # noqa: UP042 - inheriting str preserves str(Enum) returning Enum member value for serialization
    """Unified error code enumeration for all bathos MCP tools.

    Aliases 11 codes from GateErrorCode, plus the domain-exception codes and the later families listed in the
    module docstring (61 members in all). All codes are lowercase snake_case strings.
    """

    # Aliased from GateErrorCode (11 members)
    SIDECAR_MISSING = "sidecar_missing"
    SIDECAR_INVALID = "sidecar_invalid"
    SIDECAR_HASH_MISMATCH = "sidecar_hash_mismatch"
    NOT_FIRST_OF_KIND = "not_first_of_kind"
    MANIFEST_WRITE_FAILED = "manifest_write_failed"
    ADVERSARIAL_CHECK_MISSING = "adversarial_check_missing"
    HYPOTHESIS_LOCK_MISSING = "hypothesis_lock_missing"
    OUTCOME_EVALUATION_ERROR = "outcome_evaluation_error"
    RESULT_SCHEMA_MISMATCH = "result_schema_mismatch"
    OUTCOME_AMBIGUOUS = "outcome_ambiguous"
    INTERNAL = "internal"

    # New codes for domain exceptions (5 members)
    CATALOG_ERROR = "catalog_error"
    CAMPAIGN_ERROR = "campaign_error"
    SIDECAR_ERROR = "sidecar_error"
    EXPORT_ERROR = "export_error"
    INVALID_PARAM = "invalid_param"

    # Trust ledger (S3, item 3491): a candidate->promoted graduation was refused
    # because the ratchet invariant was not satisfied (no PASS attestation).
    GRADUATION_REFUSED = "graduation_refused"

    # MCP write-seam auth (debt #619): a write-verb MCP tool call was missing
    # or carried an invalid shared-secret token (bathos.mcp_auth).
    AUTH_ERROR = "auth_error"

    # Artifact archival (bathos.artifact_archive): archive-artifact/restore refusals.
    ARCHIVE_ERROR = "archive_error"
    ARCHIVE_DIRTY_TREE = "archive_dirty_tree_refused"
    ARCHIVE_ITEM_NOT_FOUND = "archive_item_not_found"
    ARCHIVE_BUNDLE_NOT_FOUND = "archive_bundle_not_found"

    # Structured document authoring (bathos.authoring): a payload was refused before
    # anything was written, so the target file is untouched.
    DOCUMENT_INVALID = "document_invalid"
    DOCUMENT_CONFLICT = "document_conflict"

    # Project-local run log (bathos.runlog, spec 260925): structured refusals
    # for the D3 gitignore gate, D7 project-id requirement, and the Mode
    # section's BTH_LOG_MODE guard. Inert while the flag is off.
    RUNLOG_NOT_IGNORED = "runlog_not_ignored"
    RUNLOG_PROJECT_ID_MISSING = "runlog_project_id_missing"
    RUNLOG_MODE_REFUSED = "runlog_mode_refused"

    # Output tiering (bathos.output_tiers, spec 261023_output-tiering): refusals of
    # `bth outputs hash` / `adopt`, and of the commands built on the same reference set.
    NO_REFERENCE_HASHES = "no_reference_hashes"
    RUN_NOT_TERMINAL = "run_not_terminal"
    ROOT_OUTSIDE_WORKTREE = "root_outside_worktree"
    ROOT_MISSING = "root_missing"
    RUN_END_CONFLICT = "run_end_conflict"
    ADOPT_CLOSED = "adopt_closed"
    ADOPT_EVENT_NOT_FOUND = "adopt_event_not_found"
    WORKTREE_UNKNOWN = "worktree_unknown"
    WORKTREE_MISSING = "worktree_missing"
    LEGACY_PATH_UNRESOLVED = "legacy_path_unresolved"
    SYMLINK_IN_OUTPUTS = "symlink_in_outputs"
    SPECIAL_FILE_IN_OUTPUTS = "special_file_in_outputs"
    RUN_NOT_FOUND = "run_not_found"
    RUN_PREFIX_AMBIGUOUS = "run_prefix_ambiguous"
    # Distributional breakdown discipline (spec 261026 D3): a run cited by a claim-bound
    # campaign is refused. `BreakdownRefused.code` carries one of these.
    BREAKDOWN_MISSING = "breakdown_missing"
    SIDECAR_UNVERIFIABLE = "sidecar_unverifiable"
    FORK_RECORD_MISSING = "fork_record_missing"
    REQUIRES_LOG_MODE = "requires_log_mode"
    # `bth cold` (step 6).
    OUTPUTS_DRIFTED = "outputs_drifted"
    NO_COLD_ROOT = "no_cold_root"
    ARCHIVE_CONFLICT = "archive_conflict"
    MYXCEL_CAPABILITY_GAP = "myxcel_capability_gap"
    MYXCEL_ERROR = "myxcel_error"
    LOCK_HELD = "lock_held"
    COPY_VERIFY_FAILED = "copy_verify_failed"
    COPY_UNVERIFIABLE = "copy_unverifiable"
    COLD_RUNS_FAILED = "cold_runs_failed"
    # `bth verify --tier outputs` (step 8).
    OUTPUT_COPIES_FAILED = "output_copies_failed"
    OUTPUT_COPIES_UNVERIFIABLE = "output_copies_unverifiable"
    # `bth warm` (step 7).
    WARM_TARGET_EXISTS = "warm_target_exists"
    WARM_MISMATCH = "warm_mismatch"
    NO_VERIFIED_COPY = "no_verified_copy"
    WARM_NOREPLACE_UNSUPPORTED = "warm_noreplace_unsupported"
    WARM_PUBLISH_FAILED = "warm_publish_failed"

    # Sidecar lock as a commit ref (spec 261026): the lock ref could not be written or did not read back.
    LOCK_REF_WRITE_FAILED = "lock_ref_write_failed"
    # `bth lock *` (spec 261026 P3): no refs/bathos/locks/<run_id>, or a command that would stage found a dirty index.
    LOCK_NOT_FOUND = "lock_not_found"
    INDEX_DIRTY = "index_dirty"

    # Step-back reviews & escalation engine (spec 261027).
    STEPBACK_NOT_FOUND = "stepback_not_found"
    STEPBACK_INVALID_SCHEMA = "stepback_invalid_schema"
    STEPBACK_INVALID_RUBRIC = "stepback_invalid_rubric"
    STEPBACK_INDEPENDENCE_VIOLATION = "stepback_independence_violation"
    STEPBACK_INSUFFICIENT_EVIDENCE = "stepback_insufficient_evidence"
    STEPBACK_AGENT_OVERRIDE_REFUSED = "stepback_agent_override_refused"
    STEPBACK_ESCALATION_ACTIVE = "stepback_escalation_active"
    STEPBACK_UNAUTHORIZED_CHAIR = "stepback_unauthorized_chair"
    # Additional stepback codes from spec Section 7.2
    STEPBACK_ACTIVE_HOLD = "stepback_active_hold"
    STEPBACK_CITATION_INVALID = "stepback_citation_invalid"
    RUBRIC_NOT_FOUND = "rubric_not_found"
    RUBRIC_INVALID = "rubric_invalid"
    RUBRIC_VERSION_MISMATCH = "rubric_version_mismatch"
    REVIEWER_CONFLICT_OF_INTEREST = "reviewer_conflict_of_interest"
    ESCALATION_LEVEL_INVALID = "escalation_level_invalid"
    CHAIR_TOKEN_INVALID = "chair_token_invalid"

    # Result validity, supersession & archive (spec 261106).
    RESULT_VOID = "result_void"
    RESULT_AFFECTED = "result_affected"
    RESULT_SUPERSEDED = "result_superseded"
    RESULT_ARCHIVED = "result_archived"
    RESULT_HIDDEN = "result_hidden"
    RESULT_SUSPECT_UNACKNOWLEDGED = "result_suspect_unacknowledged"
    RESULT_UNVERIFIABLE_UNACKNOWLEDGED = "result_unverifiable_unacknowledged"
    ESTIMAND_BOUNDARY_MIXED = "estimand_boundary_mixed"

    # Findings & evidence tiers (spec 261106 Phase 2)
    FINDING_REOPEN_REASON_REQUIRED = "finding_reopen_reason_required"
    FINDING_SETTLED_EVIDENCE_INSUFFICIENT = "finding_settled_evidence_insufficient"
    FINDING_NOT_FOUND = "finding_not_found"
    SUPERSEDED_TARGET_NOT_FOUND = "superseded_target_not_found"
    SPIKE_TIER_CANNOT_SETTLE = "spike_tier_cannot_settle"

    # Archive sweep, doc store & restore (spec 261106 Phase 3)
    ARCHIVE_SWEEP_UNAPPROVED = "archive_sweep_unapproved"
    ARCHIVE_PROPOSAL_STALE = "archive_proposal_stale"
    DOC_STORE_UNAVAILABLE = "doc_store_unavailable"
    RESTORE_CONFLICT = "restore_conflict"


# Resolution hints registry: every BathosErrorCode member must have a non-empty entry
RESOLUTION_HINTS: dict[BathosErrorCode, str] = {
    # Aliased from GateErrorCode (copied from prereg._RESOLUTION_HINTS)
    BathosErrorCode.SIDECAR_MISSING: "Create a .bth.toml sidecar adjacent to the script",
    BathosErrorCode.SIDECAR_INVALID: "Fix the sidecar TOML syntax or missing required sections",
    BathosErrorCode.SIDECAR_HASH_MISMATCH: "Re-run 'bth hypothesis lock' to regenerate the manifest",
    BathosErrorCode.NOT_FIRST_OF_KIND: "Use --derived-from to link to the parent run",
    BathosErrorCode.MANIFEST_WRITE_FAILED: "Check write permissions where the lock is written (the repo's .git for lock_location=git, the lock_dir fallback, or the script directory for lock_location=tree)",
    BathosErrorCode.ADVERSARIAL_CHECK_MISSING: "Add adversarial_check to all outcomes.pass blocks in the sidecar",
    BathosErrorCode.HYPOTHESIS_LOCK_MISSING: "Run 'bth hypothesis lock <script>' before executing",
    BathosErrorCode.OUTCOME_EVALUATION_ERROR: "Fix the DuckDB SQL condition in the sidecar outcomes block",
    BathosErrorCode.RESULT_SCHEMA_MISMATCH: "Ensure script output JSON matches the result_schema in the sidecar",
    BathosErrorCode.OUTCOME_AMBIGUOUS: "Ensure exactly one outcome condition evaluates to true",
    BathosErrorCode.INTERNAL: "File a bug report with the full error message",
    # New codes for domain exceptions
    BathosErrorCode.CATALOG_ERROR: "Check catalog integrity with 'bth verify'",
    BathosErrorCode.CAMPAIGN_ERROR: "Verify campaign state and re-run the campaign operation",
    BathosErrorCode.SIDECAR_ERROR: "Verify sidecar file exists and is valid TOML",
    BathosErrorCode.EXPORT_ERROR: "Check write permissions and available disk space",
    BathosErrorCode.INVALID_PARAM: "Verify all required parameters are provided with correct types",
    BathosErrorCode.GRADUATION_REFUSED: "Register a PASS attestation for the product (bth attestation register) before graduating it to promoted",
    BathosErrorCode.AUTH_ERROR: "Supply a valid token= matching ~/.bth/mcp_token (run any `bth` command locally to create it if missing)",
    BathosErrorCode.ARCHIVE_ERROR: "Check the script path and item id; see the error message for the specific refusal",
    BathosErrorCode.ARCHIVE_DIRTY_TREE: "Commit or stash the uncommitted bundle-path changes, then retry",
    BathosErrorCode.ARCHIVE_ITEM_NOT_FOUND: "Verify the item id, or pass stub_path pointing at one of the stub files bth archive-artifact wrote (its embedded pre_archive_sha recovers it without the ledger)",
    BathosErrorCode.ARCHIVE_BUNDLE_NOT_FOUND: "The archive_bundles/<slug>/<item_id>.bundle file referenced by the ledger is missing on this machine -- restore what's recoverable from git history and recover the bundle from a backup or another clone",
    BathosErrorCode.DOCUMENT_INVALID: "The document was NOT written. Fix the reported field errors and unknown keys, then retry -- call doc_schema for the field list and legal values",
    BathosErrorCode.DOCUMENT_CONFLICT: "A document already exists at that path. Pass force=true to overwrite it, or author to a different path",
    BathosErrorCode.RUNLOG_NOT_IGNORED: "Add '/.bth/log/' to .gitignore in the main checkout (or run `bth init` there) before recording runs",
    BathosErrorCode.RUNLOG_PROJECT_ID_MISSING: "Run `bth init --assign-id` in the main checkout, then commit the updated .bth.toml",
    BathosErrorCode.RUNLOG_MODE_REFUSED: "Unset BTH_LOG_MODE, or run `bth migrate --to-log` to write the real cut-over marker",
    BathosErrorCode.NO_REFERENCE_HASHES: "The run has no reference hash set: declare [outputs] roots in its sidecar and re-run, or (a pre-standard run only) `bth outputs adopt` its output roots",
    BathosErrorCode.RUN_NOT_TERMINAL: "Wait for the run to finish (or reap it) before adopting outputs; a running run's files are still changing",
    BathosErrorCode.ROOT_OUTSIDE_WORKTREE: "Pass --root paths under the run's recorded worktree root (relative paths resolve against it)",
    BathosErrorCode.ROOT_MISSING: "The --root path does not exist under the run's worktree root; check the path or restore the files first",
    BathosErrorCode.RUN_END_CONFLICT: "A live file differs from the hash the run recorded at its end; adopt never re-references a drifted run_end path. Re-run instead",
    BathosErrorCode.ADOPT_CLOSED: "This run declared [outputs] roots, so its reference set is closed to adopt. The fix is a new run",
    BathosErrorCode.ADOPT_EVENT_NOT_FOUND: "No live adopt event with that eid on this run; list the run's adopt events (bth sql over events) and pass the exact eid",
    BathosErrorCode.WORKTREE_UNKNOWN: "This run was imported and its worktree root is not trustworthy: pass --worktree-root <path> to the directory its outputs live under",
    BathosErrorCode.WORKTREE_MISSING: "The run's recorded worktree root does not exist on this machine; restore it, or (imported runs) pass --worktree-root",
    BathosErrorCode.LEGACY_PATH_UNRESOLVED: "A pre-standard output path cannot be placed under the worktree root; pass --worktree-root for an imported run, or accept that path is unrecoverable",
    BathosErrorCode.SYMLINK_IN_OUTPUTS: "A symlink sits under the output roots; replace it with the real file or narrow the roots (links are never followed)",
    BathosErrorCode.SPECIAL_FILE_IN_OUTPUTS: "A FIFO, socket or device sits under the output roots; remove it or narrow the roots",
    BathosErrorCode.RUN_NOT_FOUND: "No run matches that id or prefix; check it with `bth list`",
    BathosErrorCode.RUN_PREFIX_AMBIGUOUS: "The run-id prefix matches several runs; pass more characters of the id",
    BathosErrorCode.BREAKDOWN_MISSING: "Re-run with [breakdown] declared for each worst-case aggregate the verdict reads (or a [breakdown] waive reason written BEFORE the run), or narrow the claim; a waiver added afterwards does not count",
    BathosErrorCode.SIDECAR_UNVERIFIABLE: "The run-time sidecar cannot be recovered (the live file changed since the run and no pinned git ref holds it); re-run the experiment so its sidecar is recorded",
    BathosErrorCode.FORK_RECORD_MISSING: "Validate a format-2 postmortem with a [fork] record that discharges the near_bound obligation (bth postmortem validate <path>)",
    BathosErrorCode.REQUIRES_LOG_MODE: "Output tiering reads the run log; run `bth migrate --to-log` first (legacy catalogs are not supported)",
    BathosErrorCode.OUTPUTS_DRIFTED: "A live output file differs from (or is missing against) the hash the run recorded; nothing was uploaded. A drifted run_end path is never re-referenced: re-run instead (see `bth outputs hash`)",
    BathosErrorCode.NO_COLD_ROOT: "No cold profile resolves, or its myxcel profile has no absolute cold_root: pass --profile, set $BTH_COLD_PROFILE, [tool.bathos.outputs] cold_profile, or ~/.config/bathos/config.toml [outputs]",
    BathosErrorCode.ARCHIVE_CONFLICT: "A final cold name already exists with different content (or only one of the pair exists: half_committed). Nothing was overwritten; to replace it, rerun with --supersede --reason <text>, which renames the old pair aside",
    BathosErrorCode.MYXCEL_CAPABILITY_GAP: "The installed myxcel lacks the `cold` subcommands, or --clean-partials needs `myxcel cold ls` and `myxcel cold rm-partial` and one is missing; upgrade myxcel",
    BathosErrorCode.MYXCEL_ERROR: "A myxcel cold call failed; read the message, check `myxcel socket-status`, then rerun (a rerun resumes, nothing is overwritten)",
    BathosErrorCode.LOCK_HELD: "Another writer (or a killed one) holds <dest>/.bth-lock; if you are sure no writer is alive, rerun with --break-lock (removes the lock only, then resumes)",
    BathosErrorCode.COPY_VERIFY_FAILED: "The remote bytes did not match what was sent; nothing was committed and this attempt's partials were removed (an older myxcel without `cold rm-partial` leaves them under their .partial-<attempt> names, named in the message). Check the host's disk and quota, then rerun with a fresh attempt",
    BathosErrorCode.COPY_UNVERIFIABLE: "The remote bytes could not be re-read (host unreachable, Duo, sandbox); nothing was certified. Fix the connection and rerun, which resumes",
    BathosErrorCode.COLD_RUNS_FAILED: "At least one run failed under --all-uncopied; the message names each run's error_code. Fix those runs and rerun, which skips the copied ones",
    BathosErrorCode.OUTPUT_COPIES_FAILED: "A cold or mirror copy was re-read and its bytes differ from the recorded sha256 (or the cold and mirror records disagree). The measured state is recorded; see `bth outputs where <run>`. Nothing was changed on the host: rewrite the copy with `bth cold <run> --supersede --reason <text>`",
    BathosErrorCode.OUTPUT_COPIES_UNVERIFIABLE: "--strict: at least one copy could not be re-read (host unreachable, Duo, sandbox), so nothing was certified; its state is recorded as unverifiable, not failed. Check `myxcel socket-status` and rerun `bth verify --tier outputs`",
    BathosErrorCode.WARM_TARGET_EXISTS: "`bth warm --to <dir>` writes a new directory only and never replaces one, even an empty one: pick a path that does not exist (nothing at the existing path was touched)",
    BathosErrorCode.WARM_MISMATCH: "The fetched archive, manifest or extracted files do not match the copy record, the manifest or the run's reference hashes (or the archive holds an unsafe or extra member); bathos deleted only its own staging directory and wrote nothing at --to. Run `bth verify --tier outputs`, and supersede the cold copy if it is bad",
    BathosErrorCode.NO_VERIFIED_COPY: "The run has no verified, non-superseded cold or mirror copy to restore from; run `bth cold <run>` or `bth verify --tier outputs` (see `bth outputs where`)",
    BathosErrorCode.WARM_NOREPLACE_UNSUPPORTED: "This kernel or filesystem lacks renameat2 RENAME_NOREPLACE, so bathos will not publish by a replacing rename; the verified copy is kept in the <dir>.bth-warm-<attempt>.partial directory named in the message: move it into place by hand, or use a filesystem that supports the flag",
    BathosErrorCode.LOCK_REF_WRITE_FAILED: "The lock commit-ref under refs/bathos/locks/ could not be written or did not read back identically; check write access to the repo's .git (and that git is installed). `bth run` falls back to the lock_dir and records ref_error in run.lock_written",
    BathosErrorCode.LOCK_NOT_FOUND: "No lock ref refs/bathos/locks/<run_id> in this repo: check the run id, or run `bth lock fetch --apply` to bring locks from a remote clone",
    BathosErrorCode.INDEX_DIRTY: "The git index has staged changes and this `bth lock` command would stage: commit or `git restore --staged` them, then rerun",
    BathosErrorCode.WARM_PUBLISH_FAILED: "The final rename of the verified staging directory failed (permissions, space, a vanished parent); the verified copy is kept in the .partial directory named in the message: fix the cause and move it into place by hand",
    # Step-back reviews & escalation engine (spec 261027).
    BathosErrorCode.STEPBACK_NOT_FOUND: "Verify the review or escalation ID with `bth stepback status <campaign_id>`",
    BathosErrorCode.STEPBACK_INVALID_SCHEMA: "Correct fields in review payload; ensure recommendation is provided for Path B and narrative sections are non-empty",
    BathosErrorCode.STEPBACK_INVALID_RUBRIC: "Fix syntax errors in the rubric TOML definition; ensure all 5 dimensions and weights sum to 1.0",
    BathosErrorCode.STEPBACK_INDEPENDENCE_VIOLATION: "Assign review to an independent agent or researcher who did not author runs in this campaign",
    BathosErrorCode.STEPBACK_INSUFFICIENT_EVIDENCE: "Include at least 2 verified primary catalog citations (e.g. run:<id>, sidecar:<path>#clause) in the review",
    BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED: "Ask the human committee chair to run `bth stepback resolve --as-human-chair` from an interactive TTY; AI agents cannot resolve Level 3 escalations",
    BathosErrorCode.STEPBACK_ESCALATION_ACTIVE: "Address the active escalation or hold by conducting an independent step-back review before concluding",
    BathosErrorCode.STEPBACK_UNAUTHORIZED_CHAIR: "Verify the chair token in `~/.bth/chair_token` or invoke from an interactive TTY",
    BathosErrorCode.STEPBACK_ACTIVE_HOLD: "Address the active Level 2 hold by conducting an independent step-back review before concluding",
    BathosErrorCode.STEPBACK_CITATION_INVALID: "Verify that all cited runs and sidecars belong to the active campaign under review",
    BathosErrorCode.RUBRIC_NOT_FOUND: "Specify a valid rubric ID from `agent_assets/rubrics/` or install the required rubric file",
    BathosErrorCode.RUBRIC_INVALID: "Fix syntax errors in the rubric TOML definition; ensure all 5 dimensions and weights sum to 1.0",
    BathosErrorCode.RUBRIC_VERSION_MISMATCH: "Use the canonical rubric version matching the content hash; do not modify existing rubric files without bumping version",
    BathosErrorCode.REVIEWER_CONFLICT_OF_INTEREST: "Assign review to an independent agent or researcher who did not author runs in this campaign",
    BathosErrorCode.ESCALATION_LEVEL_INVALID: "Specify a valid escalation level between 0 and 3",
    BathosErrorCode.CHAIR_TOKEN_INVALID: "Verify the chair token in `~/.bth/chair_token` or invoke from an interactive TTY",
    BathosErrorCode.RESULT_VOID: "The requested run was produced by a defective instrument version and is void. Query runs_current or use a replacement run. Pass --include-superseded to inspect history.",
    BathosErrorCode.RESULT_AFFECTED: "The requested run is implicated by an active blast-radius ledger entry. Re-run or clear the flag with audit justification via 'bth blast-radius clear'.",
    BathosErrorCode.RESULT_SUPERSEDED: "The requested run was superseded by a newer run. Use --include-superseded to inspect, or query the successor run.",
    BathosErrorCode.RESULT_ARCHIVED: "The requested script or outputs were archived. Use 'bth restore --item-id <id>' to restore files to workspace.",
    BathosErrorCode.RESULT_HIDDEN: "The requested run is hidden by default due to void, affected, superseded, or archived status. Pass --include-superseded to inspect.",
    BathosErrorCode.RESULT_SUSPECT_UNACKNOWLEDGED: "Run was produced by an unverified toolchain matching a defect range or lacks a required predicate field. To proceed, pass --ack-suspect '<rationale>' (ack_suspect='<rationale>').",
    BathosErrorCode.RESULT_UNVERIFIABLE_UNACKNOWLEDGED: "Run provenance cannot be verified against repository lockfiles. To proceed, pass --ack-unverifiable '<rationale>' (ack_unverifiable='<rationale>').",
    BathosErrorCode.ESTIMAND_BOUNDARY_MIXED: "Attempted to combine runs across an estimand change boundary at a confirmatory gate. Results compute different quantities; separate by epoch or pass --allow-mixed-estimand '<rationale>'.",
    BathosErrorCode.FINDING_REOPEN_REASON_REQUIRED: "The question is already settled. Provide --reopen-reason explaining why prior confirmatory evidence is being challenged.",
    BathosErrorCode.FINDING_SETTLED_EVIDENCE_INSUFFICIENT: "Settling a finding requires at least one active confirmatory run. Elevate supporting evidence before setting status to settled.",
    BathosErrorCode.FINDING_NOT_FOUND: "The specified finding slug or ID was not found in .bth/findings/. Query finding_list to inspect available findings.",
    BathosErrorCode.SUPERSEDED_TARGET_NOT_FOUND: "The target entity specified in supersedes does not exist. Verify the predecessor ID or slug.",
    BathosErrorCode.SPIKE_TIER_CANNOT_SETTLE: "Spike runs are preliminary probes and cannot support a settled finding. Elevate evidence via a confirmatory run.",
    BathosErrorCode.ARCHIVE_SWEEP_UNAPPROVED: "Archive sweep requires an approved proposal file when --apply is set. Run with --dry-run first to generate a proposal.",
    BathosErrorCode.ARCHIVE_PROPOSAL_STALE: "The archive proposal references files or git states that have changed since proposal generation. Re-run --dry-run to regenerate.",
    BathosErrorCode.DOC_STORE_UNAVAILABLE: "The configured documentation store path is not accessible or writable. Check permissions or configuration.",
    BathosErrorCode.RESTORE_CONFLICT: "Restoring this item would overwrite an existing modified or untracked file. Move or rename the destination file first.",
}


# Exception class name to BathosErrorCode registry (AC-7 maintenance trap).
# Maps domain exception class names to their error codes for CI assertion.
EXCEPTION_TO_CODE: dict[str, BathosErrorCode] = {
    # Gate errors
    "GateError": BathosErrorCode.INTERNAL,
    # Project config: an invalid `.bth.toml [enforcement]` block (protamer #1960 / bathos #2210).
    # Raised, never swallowed, so a typo cannot silently leave the sidecar gate off.
    "EnforcementConfigError": BathosErrorCode.INVALID_PARAM,
    # Project config: a malformed `track_packages` list (#2946). Refuses the run, never guesses.
    "DependencyProvenanceConfigError": BathosErrorCode.INVALID_PARAM,
    # Catalog errors
    "CatalogError": BathosErrorCode.CATALOG_ERROR,
    "CorruptDatabaseError": BathosErrorCode.CATALOG_ERROR,
    # The ingest lock stayed held past a caller's bounded wait (run-end ingest, PR #111).
    "IngestLockTimeout": BathosErrorCode.CATALOG_ERROR,
    # Campaign errors
    "CampaignError": BathosErrorCode.CAMPAIGN_ERROR,
    # Claim-bound breakdown refusal (spec 261026 D3). Carries its own, more specific `.code`
    # (breakdown_missing / sidecar_unverifiable / fork_record_missing); this is the class fallback.
    "BreakdownRefused": BathosErrorCode.CAMPAIGN_ERROR,
    # A run-id prefix matching several runs (bathos.query.lookup_run).
    "AmbiguousRunId": BathosErrorCode.RUN_PREFIX_AMBIGUOUS,
    # Sidecar errors
    "SidecarError": BathosErrorCode.SIDECAR_ERROR,
    # Export errors
    "ExportError": BathosErrorCode.EXPORT_ERROR,
    # Figure registry errors (S7, item 3490): a figure_entry payload carried a
    # forbidden inline field (verdict/strength/content_hash/outcome/gate).
    "FigureEntrySchemaError": BathosErrorCode.INVALID_PARAM,
    # Trust ledger errors (S3, item 3491): a graduation was refused because the
    # ratchet invariant (a PASS attestation must exist) was not satisfied.
    "GraduationRefused": BathosErrorCode.GRADUATION_REFUSED,
    # Attestation errors (S4, item 3492; debt #638 fix): register_attestation
    # rejected an attestation that failed validate_attestation (e.g. missing
    # required evidence fields) rather than silently anchoring it.
    "AttestationValidationFailed": BathosErrorCode.SIDECAR_INVALID,
    # MCP write-seam auth (debt #619): missing/invalid shared-secret token on a
    # write-verb MCP tool call.
    "McpAuthError": BathosErrorCode.AUTH_ERROR,
    # ── backlog #4077 ────────────────────────────────────────────────────────
    # Five exceptions that were raised in src/bathos/ with no entry here, so
    # test_every_domain_exception_has_registered_code had been failing on main.
    #
    # All five map onto EXISTING codes rather than new enum members. Adding a
    # code changes the public error taxonomy every MCP caller sees, which is a
    # design decision, not a test fix — and the two are deliberately not made in
    # the same change. Where the fit below is imperfect it is called out rather
    # than papered over.
    #
    # Rule-card corpus (step 1): raised for a malformed card *and* for an
    # unknown card id. The user-facing case is `bth ref show BADID`, hence
    # INVALID_PARAM; a genuinely corrupt corpus is the rarer path and would read
    # better as its own code.
    "CorpusError": BathosErrorCode.INVALID_PARAM,
    # cisternal plugin-bundle export failure — same family as ExportError.
    "PluginExportError": BathosErrorCode.EXPORT_ERROR,
    # Bad caller input to a stats gate (wrong shape, insufficient n).
    "StatsGateInputError": BathosErrorCode.INVALID_PARAM,
    # Campaign DAG edge would introduce a cycle (campaign_edges.py).
    "CycleRejectedError": BathosErrorCode.CAMPAIGN_ERROR,
    # IMPERFECT FIT, deliberately: the `stats` extra (scipy) is not installed.
    # That is an environment/packaging condition, not an internal fault, and
    # INTERNAL is being used as the catch-all because no DEPENDENCY_MISSING code
    # exists. Worth revisiting alongside any future taxonomy change.
    "ScipyUnavailableError": BathosErrorCode.INTERNAL,
    # Reaper errors (bathos.reap): --older-than-h floor validation or other orphan-reaping issues.
    "ReapError": BathosErrorCode.INVALID_PARAM,
    # bth backfill-declarations outside log mode.
    "BackfillError": BathosErrorCode.INVALID_PARAM,
    # Artifact archival (bathos.artifact_archive): archive-artifact/restore refusals.
    "ArchiveError": BathosErrorCode.ARCHIVE_ERROR,
    "DirtyTreeError": BathosErrorCode.ARCHIVE_DIRTY_TREE,
    "ArtifactNotFoundError": BathosErrorCode.ARCHIVE_ITEM_NOT_FOUND,
    "BundleNotFoundError": BathosErrorCode.ARCHIVE_BUNDLE_NOT_FOUND,
    # Cluster catalog identity mismatch (cluster_catalog.py): the local
    # .bth/catalog disagrees with the remote root's catalog -- same
    # integrity family as CatalogError.
    "CatalogIdentityError": BathosErrorCode.CATALOG_ERROR,
    # Structured document authoring (bathos.authoring.ledger): the append-only
    # mutation ledger could not be written inside a repository where it should
    # have been. The document write is rolled back, so this surfaces as an
    # invalid-document refusal rather than a partially-recorded success.
    "LedgerAppendError": BathosErrorCode.DOCUMENT_INVALID,
    # Project-local run log (bathos.runlog, spec 260925).
    "LogNotIgnoredError": BathosErrorCode.RUNLOG_NOT_IGNORED,
    "ProjectIdMissingError": BathosErrorCode.RUNLOG_PROJECT_ID_MISSING,
    "LogModeRefusedError": BathosErrorCode.RUNLOG_MODE_REFUSED,
    # ── debt 1996 ────────────────────────────────────────────────────────────
    # Five more exceptions raised in src/bathos/ with no entry here, which kept
    # test_every_domain_exception_has_registered_code red on main. Same rule as
    # backlog #4077 above: map onto EXISTING codes (a new code changes the public
    # error taxonomy every MCP caller sees, which is a design decision, not a test
    # fix) and call out the imperfect fits instead of papering over them.
    #
    # AC-19: a `.wal` remained after closing the staged index copy, so the swap of
    # the real index.db was refused. Catalog integrity family.
    "IngestWalRemainsError": BathosErrorCode.CATALOG_ERROR,
    # `~/.bth/projects.toml` exists but does not parse, and is deliberately never
    # overwritten. IMPERFECT FIT: a corrupt project registry is not the catalog
    # proper, but it is the same never-clobber integrity family and no REGISTRY code
    # exists.
    "RegistryUnreadableError": BathosErrorCode.CATALOG_ERROR,
    # D6: `run.started` could not be appended to the project log or its fallback, so
    # the script is deliberately not launched. The run log is the catalog's write
    # path. IMPERFECT FIT: nothing is wrong with the caller's input.
    "RunNotLaunchedError": BathosErrorCode.CATALOG_ERROR,
    # `squeue` could not be queried at all (unreachable host, timeout, non-zero
    # exit) and migration fails closed. IMPERFECT FIT, deliberately: an
    # environment/cluster condition, not an internal fault; INTERNAL is the
    # catch-all, as for ScipyUnavailableError, because no DEPENDENCY_UNAVAILABLE
    # code exists.
    "SqueueUnavailableError": BathosErrorCode.INTERNAL,
    # The installed myxcel lacks a capability bathos needs (debt 1993): not
    # transient, and only fixable in myxcel. IMPERFECT FIT, for the same reason as
    # above; the INTERNAL hint ("file a bug report") at least points the right way.
    "MyxcelCapabilityGapError": BathosErrorCode.INTERNAL,
    # The bth-preflight-hook could not run or parse `bth preflight` (spec 261016 P1).
    # Caught inside the hook and reported on stderr; never crosses the MCP boundary.
    "HookInternalError": BathosErrorCode.INTERNAL,
    # Output tiering (spec 261023). Both carry their own, more specific `error_code` attribute
    # (one of the output-tiering members above, or `invalid_param`); this is the fallback for a
    # caller that only has the class.
    "OutputTierError": BathosErrorCode.INVALID_PARAM,
    "OutputTiersConfigError": BathosErrorCode.INVALID_PARAM,
    # Lock location (spec 261026): bad lock_location / lock_dir / ignore_outputs configuration.
    "LockConfigError": BathosErrorCode.INVALID_PARAM,
    "LockRefError": BathosErrorCode.LOCK_REF_WRITE_FAILED,
    # `bth lock *`: carries its own, more specific `code`; this is the fallback for a caller with only the class.
    "LockAdminError": BathosErrorCode.INVALID_PARAM,
    # Step-back reviews & escalation engine (spec 261027).
    "BathosError": BathosErrorCode.INTERNAL,
    "StepBackError": BathosErrorCode.INTERNAL,
    "StepBackNotFoundError": BathosErrorCode.STEPBACK_NOT_FOUND,
    "StepBackInvalidSchemaError": BathosErrorCode.STEPBACK_INVALID_SCHEMA,
    "StepBackInvalidRubricError": BathosErrorCode.STEPBACK_INVALID_RUBRIC,
    "RubricInvalidError": BathosErrorCode.RUBRIC_INVALID,
    "RubricNotFoundError": BathosErrorCode.RUBRIC_NOT_FOUND,
    "RubricVersionMismatchError": BathosErrorCode.RUBRIC_VERSION_MISMATCH,
    "StepBackIndependenceViolationError": BathosErrorCode.STEPBACK_INDEPENDENCE_VIOLATION,
    "ReviewerConflictError": BathosErrorCode.REVIEWER_CONFLICT_OF_INTEREST,
    "StepBackInsufficientEvidenceError": BathosErrorCode.STEPBACK_INSUFFICIENT_EVIDENCE,
    "StepBackCitationInvalidError": BathosErrorCode.STEPBACK_CITATION_INVALID,
    "StepBackAgentOverrideRefusedError": BathosErrorCode.STEPBACK_AGENT_OVERRIDE_REFUSED,
    "StepBackEscalationActiveError": BathosErrorCode.STEPBACK_ESCALATION_ACTIVE,
    "StepBackActiveHoldError": BathosErrorCode.STEPBACK_ACTIVE_HOLD,
    "StepBackUnauthorizedChairError": BathosErrorCode.STEPBACK_UNAUTHORIZED_CHAIR,
    "ChairTokenInvalidError": BathosErrorCode.CHAIR_TOKEN_INVALID,
    "EscalationLevelInvalidError": BathosErrorCode.ESCALATION_LEVEL_INVALID,
    # Result validity (spec 261106)
    "ResultHiddenError": BathosErrorCode.RESULT_HIDDEN,
    "ResultVoidError": BathosErrorCode.RESULT_VOID,
    "ResultAffectedError": BathosErrorCode.RESULT_AFFECTED,
    "ResultSupersededError": BathosErrorCode.RESULT_SUPERSEDED,
    "ResultArchivedError": BathosErrorCode.RESULT_ARCHIVED,
    "ResultSuspectUnacknowledgedError": BathosErrorCode.RESULT_SUSPECT_UNACKNOWLEDGED,
    "ResultUnverifiableUnacknowledgedError": BathosErrorCode.RESULT_UNVERIFIABLE_UNACKNOWLEDGED,
    "EstimandBoundaryMixedError": BathosErrorCode.ESTIMAND_BOUNDARY_MIXED,
    # Findings & evidence tiers (spec 261106 Phase 2)
    "FindingError": BathosErrorCode.INVALID_PARAM,
    "FindingReopenReasonRequiredError": BathosErrorCode.FINDING_REOPEN_REASON_REQUIRED,
    "FindingSettledEvidenceInsufficientError": BathosErrorCode.FINDING_SETTLED_EVIDENCE_INSUFFICIENT,
    "FindingNotFoundError": BathosErrorCode.FINDING_NOT_FOUND,
    "SupersededTargetNotFoundError": BathosErrorCode.SUPERSEDED_TARGET_NOT_FOUND,
    "SpikeTierCannotSettleError": BathosErrorCode.SPIKE_TIER_CANNOT_SETTLE,
    # Archive sweep, doc store & restore (spec 261106 Phase 3)
    "ArchiveSweepError": BathosErrorCode.INVALID_PARAM,
    "ArchiveSweepUnapprovedError": BathosErrorCode.ARCHIVE_SWEEP_UNAPPROVED,
    "ArchiveProposalStaleError": BathosErrorCode.ARCHIVE_PROPOSAL_STALE,
    "DocStoreUnavailableError": BathosErrorCode.DOC_STORE_UNAVAILABLE,
    "RestoreConflictError": BathosErrorCode.RESTORE_CONFLICT,
}


# ── Step-Back Review & Escalation Domain Exceptions (spec 261027) ─────────────


class BathosError(Exception):
    """Base class for bathos domain exceptions."""


class StepBackError(BathosError):
    """Base class for step-back review and escalation errors."""


class StepBackNotFoundError(StepBackError):
    """Requested review or escalation record does not exist."""


class StepBackInvalidSchemaError(StepBackError):
    """Review payload or configuration violates required schema."""


class StepBackInvalidRubricError(StepBackError):
    """Rubric TOML is malformed or invalid."""


class RubricInvalidError(StepBackInvalidRubricError):
    """Rubric TOML syntax error or invalid dimension weights."""


class RubricNotFoundError(StepBackError):
    """Specified rubric asset could not be found."""


class RubricVersionMismatchError(StepBackError):
    """Rubric content hash does not match recorded version hash."""


class StepBackIndependenceViolationError(StepBackError):
    """Reviewer is not independent from campaign authoring."""


class ReviewerConflictError(StepBackIndependenceViolationError):
    """Reviewer has a conflict of interest with the campaign."""


class StepBackInsufficientEvidenceError(StepBackError):
    """Review lacks required minimum valid primary evidence citations."""


class StepBackCitationInvalidError(StepBackError):
    """A cited run or sidecar does not belong to the campaign under review."""


class StepBackAgentOverrideRefusedError(StepBackError):
    """Agent attempted to resolve or override a Level 3 Human Chair escalation."""


class StepBackEscalationActiveError(StepBackError):
    """Operation blocked because campaign has an active escalation or hold."""


class StepBackActiveHoldError(StepBackEscalationActiveError):
    """Campaign has an active Level 2 committee hold."""


class StepBackUnauthorizedChairError(StepBackError):
    """Caller is not authorized to act as human committee chair."""


class ChairTokenInvalidError(StepBackUnauthorizedChairError):
    """Invalid or missing chair token."""


class EscalationLevelInvalidError(StepBackError):
    """Specified escalation level is outside 0-3 range."""


# ── Result Validity Domain Exceptions (spec 261106) ──────────────────────────


class ResultHiddenError(BathosError):
    """Raised when an operation encounters or requests a hidden run without permission."""

    def __init__(
        self,
        message: str = "Result is hidden",
        *,
        run_id: str | None = None,
        state: str = "void",
        defect_id: str | None = None,
        reason: str | None = None,
        replacement_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.state = state
        self.defect_id = defect_id
        self.reason = reason
        self.replacement_id = replacement_id


class ResultVoidError(ResultHiddenError):
    """Run was produced by a defective toolchain version and is void."""


class ResultAffectedError(ResultHiddenError):
    """Run is implicated by an active blast-radius ledger entry."""


class ResultSupersededError(ResultHiddenError):
    """Run was superseded by a newer run."""


class ResultArchivedError(ResultHiddenError):
    """Run scripts or outputs were archived."""


class ResultSuspectUnacknowledgedError(BathosError):
    """Run is suspect (inferred version or missing predicate field) and lacked required acknowledgment."""

    def __init__(
        self,
        message: str = "Suspect result requires acknowledgment",
        *,
        run_id: str | None = None,
        defect_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.defect_id = defect_id
        self.reason = reason


class ResultUnverifiableUnacknowledgedError(BathosError):
    """Run toolchain version is unverifiable against repository lockfiles and lacked required acknowledgment."""

    def __init__(
        self,
        message: str = "Unverifiable result requires acknowledgment",
        *,
        run_id: str | None = None,
        defect_id: str | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.run_id = run_id
        self.defect_id = defect_id
        self.reason = reason


class EstimandBoundaryMixedError(BathosError):
    """Runs across an estimand change boundary were pooled at a confirmatory gate without override."""

    def __init__(
        self,
        message: str = "Cannot pool runs across estimand change boundary without override",
        *,
        estimand_markers: list[str] | None = None,
        conflicting_runs: dict[str, list[str]] | None = None,
        epochs: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(message)
        _ = kwargs
        self.estimand_markers = estimand_markers or epochs or []
        self.epochs = self.estimand_markers
        self.conflicting_runs = conflicting_runs or {}


# ── Findings & Supersession Domain Exceptions (spec 261106 P2) ────────────────


class FindingError(BathosError):
    """Base class for findings register errors."""


class FindingReopenReasonRequiredError(FindingError):
    """Attempted to create finding or experiment for settled question without reopen_reason."""

    def __init__(
        self,
        message: str = "Settled question requires reopen_reason",
        *,
        question_slug: str | None = None,
        settled_finding_id: str | None = None,
        settled_answer: str | None = None,
    ) -> None:
        super().__init__(message)
        self.question_slug = question_slug
        self.settled_finding_id = settled_finding_id
        self.settled_answer = settled_answer


class FindingSettledEvidenceInsufficientError(FindingError):
    """Settling a finding requires at least one active confirmatory run."""

    def __init__(
        self,
        message: str = "Settling a finding requires at least one active confirmatory run",
        *,
        finding_id: str | None = None,
        supporting_runs: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.finding_id = finding_id
        self.supporting_runs = supporting_runs or []


class FindingNotFoundError(FindingError):
    """Requested finding slug or ID does not exist."""

    def __init__(
        self,
        message: str = "Finding not found",
        *,
        slug_or_id: str | None = None,
    ) -> None:
        super().__init__(message)
        self.slug_or_id = slug_or_id


class SupersededTargetNotFoundError(BathosError):
    """The target entity specified in supersedes does not exist."""

    def __init__(
        self,
        message: str = "Superseded target entity does not exist",
        entity_id_pos: str | None = None,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
    ) -> None:
        if entity_id_pos is not None:
            # Called as SupersededTargetNotFoundError("finding", "target_id")
            resolved_type = entity_type or message
            resolved_id = entity_id or entity_id_pos
            msg = f"Target entity '{resolved_id}' of type '{resolved_type}' not found"
        else:
            resolved_type = entity_type
            resolved_id = entity_id
            msg = message
        super().__init__(msg)
        self.entity_type = resolved_type
        self.entity_id = resolved_id


class SpikeTierCannotSettleError(FindingError):
    """Spike runs are preliminary probes and cannot support a settled finding."""

    def __init__(
        self,
        message: str = "Spike runs cannot support a settled finding",
        *,
        finding_id: str | None = None,
        spike_runs: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.finding_id = finding_id
        self.spike_runs = spike_runs or []


HIDDEN_STATE_TO_ERROR_CODE: dict[str, BathosErrorCode] = {
    "void": BathosErrorCode.RESULT_VOID,
    "affected": BathosErrorCode.RESULT_AFFECTED,
    "superseded": BathosErrorCode.RESULT_SUPERSEDED,
    "archived": BathosErrorCode.RESULT_ARCHIVED,
}

HIDDEN_STATE_TO_EXCEPTION: dict[str, type[ResultHiddenError]] = {
    "void": ResultVoidError,
    "affected": ResultAffectedError,
    "superseded": ResultSupersededError,
    "archived": ResultArchivedError,
}


# ── Archive Sweep & Doc Store Domain Exceptions (spec 261106 Phase 3) ────────


class ArchiveSweepError(BathosError):
    """Base error for archive sweep operations."""


class ArchiveSweepUnapprovedError(ArchiveSweepError):
    """Raised when --apply is passed without an approved proposal."""


class ArchiveProposalStaleError(ArchiveSweepError):
    """Raised when proposal file SHA256 or git state diverges."""


class DocStoreUnavailableError(BathosError):
    """Raised when configured documentation store is not accessible."""


class RestoreConflictError(BathosError):
    """Raised when restore would overwrite an existing modified/untracked file."""


