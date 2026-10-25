"""Error codes and resolution hints for bathos MCP tools.

Provides a unified BathosErrorCode enum that includes:
- 11 aliased codes from GateErrorCode (pre-registration gate failures)
- 5 new codes for domain exceptions (catalog, campaign, sidecar, export, invalid params)

All MCP tools return envelopes with error_code values from this enum.
"""

from __future__ import annotations

from enum import Enum


class BathosErrorCode(str, Enum):  # noqa: UP042 - inheriting str preserves str(Enum) returning Enum member value for serialization
    """Unified error code enumeration for all bathos MCP tools.

    Aliases 11 codes from GateErrorCode, plus 5 new codes for domain exceptions.
    All codes are lowercase snake_case strings.
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


# Resolution hints registry: every BathosErrorCode member must have a non-empty entry
RESOLUTION_HINTS: dict[BathosErrorCode, str] = {
    # Aliased from GateErrorCode (copied from prereg._RESOLUTION_HINTS)
    BathosErrorCode.SIDECAR_MISSING: "Create a .bth.toml sidecar adjacent to the script",
    BathosErrorCode.SIDECAR_INVALID: "Fix the sidecar TOML syntax or missing required sections",
    BathosErrorCode.SIDECAR_HASH_MISMATCH: "Re-run 'bth hypothesis lock' to regenerate the manifest",
    BathosErrorCode.NOT_FIRST_OF_KIND: "Use --derived-from to link to the parent run",
    BathosErrorCode.MANIFEST_WRITE_FAILED: "Check write permissions in the script directory",
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
}
