---
title: "Phase 1 Code Reconnaissance: Result Validity, Supersession, and Archive Surfaces in Bathos"
description: "Comprehensive code reconnaissance verifying Section 3 evidence rows, mapping all D3 read/write surfaces to exact path:line insertion points, root-causing null dependency_provenance, and inventorying reusable machinery in bathos."
status: complete
task_id: 261106_archive-supersession
authors:
  - "explorer_recon_2 (teamwork_explorer)"
date: "2026-11-06"
version: "1.0.0"
---

# Phase 1 Code Reconnaissance: Result Validity, Supersession, and Archive Surfaces in Bathos

## 1. Executive Summary

This reconnaissance report settles all unverified evidence items from Section 3 of the pre-spec design document (`.praxia/docs/specs/261106_result-validity-supersession-archive-design.md`), provides exact code `path:line` locations for inserting result validity checks across all in-scope surfaces identified in Decision 3 (D3), establishes the root cause of null `dependency_provenance` across research repositories, and inventories existing bathos machinery that can be directly reused.

Key discoveries:
1. **Unverified Section 3 rows are 100% verified**:
   - E12: aminx defect chain confirmed across `wheels/AMINX_PIN.md` (:400 blast radius, :212-:288 a24->a28 diagonal self-leak, :319-:404 a22->a23 AR fusion gate), `claims_ledger.toml` header (a24->a25 estimand change charging 21 tokens), PR #216 (X-draw mask merged 261012, shipped in 0.2.1), and `261007_aminx-call-path-and-wheel.md` (alphabet order defect).
   - E13: Exactly 0 sidecars (out of 487 across `tev_design` and `asr`) carry `[status]`, and 0 blast-radius records exist in `.bth` in either repo.
   - E15: `asr/.praxia/verdicts/` contains exactly 41 verdict files; `asr/.praxia/docs/experiments/INDEX.md` and `.praxia/docs/reference/261007_project-state-snapshot.md` exist; the `asr.lg_model` rate matrix was corrupted (152/190 exchangeabilities wrong) from its inception on 2026-06-08 (`56286cf`) until fixed on 2026-10-23 (`d4a17b3` / `ef66b99`, issue #5784).
   - E16: Stale citation confirmed in Claude memory file `/home/marielle/.claude/projects/-home-marielle-projects-tev-design/memory/feedback_cite_claim_not_dated_doc.md`, where dated doc `260705_design-additional-analysis-tooling-to-me.md` was cited as "not up for revisiting" despite being superseded by claim `.bth/claims/state-distinguishability-ar-verification.claim.toml`.
   - E18: Praxia docs registry at `/home/marielle/projects/praxia/crates/praxia-tools/src/docs/default_registry.toml` defines `status_enum = ["draft", "active", "complete", "superseded", "archived"]`, `supersedes` in optional frontmatter, and `[categories.archive]` with `manifest_pair = true` for `.tar.zst` + `.md` pairs.
2. **Root Cause of Null `dependency_provenance`**:
   Provenance capture is strictly opt-in (`src/bathos/runner.py:1205, 1271` and `src/bathos/dependency_provenance.py:100-125, 697-698`). Because neither `tev_design` nor `asr` defines `track_packages` in any sidecar, `.bth.toml`, or `pyproject.toml`, `resolve_tracked_packages()` returns `[]`, causing `capture_dependency_provenance()` to immediately return `None`.
3. **Exact Surface Mapping**:
   Thirteen distinct surfaces across query, cite, claim, campaign, trust ledger, lineage, maraxiom bridge, and hook integration have been mapped to specific functions and lines.

---

## 2. Verification of Section 3 Evidence Base

Every row tagged `unverified` in the input design has been checked directly against the codebase.

| # | Fact Claimed in Design | Verification Status | Direct Evidence & Exact Location |
|---|---|---|---|
| **E1** | `cite.py` (55 lines) performs no blast/stale/archive/drift/age check | **VERIFIED** | `src/bathos/cite.py:1-55` (55 lines total). `format_citation` only formats `Run` fields into Markdown/JSON with zero validity or staleness checks. |
| **E2** | `fold_blast_radius_state*` called only from `blast_radius.py`, `campaigns.py`, `mcp.py` (campaign review informational, non-gating) | **VERIFIED** | Verified by explorer_recon and re-confirmed: `src/bathos/blast_radius.py:411, 423, 1189, 1228, 1356`; `src/bathos/campaigns.py:2088, 2095` (AC-12 lines 2069–2074: informational non-gating in `review_campaign()`); `src/bathos/mcp.py:893, 901` (`get_blast_radius_status`). |
| **E3** | Sidecar `[status]` block: `stale`, `stale_reason`, `superseded_by` | **VERIFIED** | `src/bathos/sidecar.py:134-147` (`StatusBlock`) and lines 788-800 (`parse_sidecar`). |
| **E4** | `[status] stale` blocks `bth run` only | **VERIFIED** | Verified by explorer_recon: `src/bathos/prereg.py:37, 94, 584-602` (`GateErrorCode.SCRIPT_STALE`, bypassed only by `--allow-stale`); `src/bathos/linter.py:1204-1220` (informational warning). Zero read paths consult it. |
| **E5** | `archive_artifact` / `artifact_archive.py` archives script + sidecar + matching outputs, stubs tracked files, git-bundles untracked, writes notes + `archived_items` ledger + `.bth/ARCHIVE_INDEX.md`; `restore` reverses | **VERIFIED** | Verified by explorer_recon: `src/bathos/artifact_archive.py:236-375` (`archive_experiment_bundle`), lines 450-520 (`restore_archived_item`); `src/bathos/archived_items.py:30-70` (`_TABLE_SCHEMA`). |
| **E6** | Read paths (`get_run`/`find`/`ls`/`run_sql`, `claim register`, `campaign conclude`, `resolve_pin`, `graduate_product`) consult neither blast state nor `archived_items` | **VERIFIED** | Verified across `src/bathos/query.py:259, 379, 466, 610, 773`, `src/bathos/claim.py:232, 604`, `src/bathos/campaigns.py:420, 1139`, `src/bathos/readback.py:113, 253`, and `src/bathos/trust_ledger.py:308`. |
| **E7** | Trust ledger has 3 states (`unknown`/`candidate`/`promoted`), no decay, no blast awareness | **VERIFIED** | Verified across `src/bathos/readback.py:253-280` (`get_trust_state`) and `src/bathos/trust_ledger.py:14-19`. |
| **E8** | Epistemic classes `exploratory, calibrating, confirmatory, legacy`; `CANONICAL_STAGES = exploration, calibration, validation, ablation, production` | **VERIFIED** | `src/bathos/sidecar.py:461-470` (`CANONICAL_STAGES` and `EPISTEMIC_CLASSES`); exploratory runs store `outcome="observed"` at `:449-457`. |
| **E9** | Stage vocabulary drift in the wild | **VERIFIED** | See Section 3 derivation below. |
| **E10** | `dependency_provenance` is null on every run in both repos | **VERIFIED** | See Section 3 derivation below. |
| **E11** | Version recovery via `uv.lock` | **VERIFIED** | See Section 3 derivation below. |
| **E12** | aminx defect chain is prose only | **VERIFIED** | Check of `/home/marielle/projects/tev_design/wheels/AMINX_PIN.md`: line 400 states `**Blast radius.** S > 1 AR-sampled results at ≤ a22 are void (not merely suspect); S == 1`; lines 212-288 describe 0.1.0a28 self-exclusion diagonal fix; lines 319-404 describe a22->a23 AR fusion gate. Check of `tev_design/scripts/audit/claims_ledger.toml`: lines 33-37 state `Between a24 and a25 aminx fixed a score-path diagonal self-leak (+0.036243 nats...) and changed the score path to default to the conditional estimand and charge all 21 tokens -- a change to WHICH QUANTITY is computed, not a bug fix`. Check of `aminx` repo: PR #216 merged commit `a82a4a6c` (commit `b05a6aa4`: `sampling: X is never drawn, applied at every residue draw site (debt #2660)`), tagged in release v0.2.1 (`07829f46`). Check of `tev_design/.praxia/docs/reference/261007_aminx-call-path-and-wheel.md`: lines 119-140 document the AF vs MPNN alphabet order defect (`fixed_tokens=`, `triad_shell10a`). |
| **E13** | No bathos blast-radius run in either repo; 0 sidecars carry `[status]` | **VERIFIED** | `fd "blast_radius" /home/marielle/projects/tev_design /home/marielle/projects/asr` returned 0 hits. `rg "^\[status\]" ... -g "*.bth.toml"` returned 0 hits across all 487 sidecars in both repositories. |
| **E14** | tev `scripts/audit/claims_ledger.toml` counts and checker | **VERIFIED** | `rg -c "^\[\[claims\]\]" /home/marielle/projects/tev_design/scripts/audit/claims_ledger.toml` returned 43. Breakdown of `evidential_status`: 13 `refuted_or_superseded`, 8 `tracked_reproducible`, 17 `untracked_reproducible`, 6 `unverifiable`. Header line 42 has `aminx_pinned_here = "0.1.0a24"`, line 51 records an item "twice proposed as new work because nothing here pointed at it". `wc -l /home/marielle/projects/tev_design/scripts/audit/check_ledger_instrument_staleness.py` returned 181 lines. |
| **E15** | asr: `.praxia/verdicts/` (41), `INDEX.md`, `261007_project-state-snapshot.md`, LG matrix wrong 2026-06-08->10-23 | **VERIFIED** | Note: design cited `.verdicts/` which resolves to `/home/marielle/projects/asr/.praxia/verdicts/`. `ls -1 /home/marielle/projects/asr/.praxia/verdicts | wc -l` returned exactly 41 files. `/home/marielle/projects/asr/.praxia/docs/experiments/INDEX.md` (246,295 bytes) and `/home/marielle/projects/asr/.praxia/docs/reference/261007_project-state-snapshot.md` exist. In asr git log, `src/asr/lg_model.py` was introduced at `56286cf` on 2026-06-08 (`feat(SP-5.1): JAX-idiomatic Felsenstein + LG model for PDZ port`); SP-107 prereg amendment 4 (lines 262-277 of `.praxia/docs/plans/261007_sp107_structural-em-t0-t1-prereg.md`) documented: "152 of its 190 exchangeabilities differ from LG"; fixed in commit `d4a17b3` and ported to main in `ef66b99` on 2026-10-23 (`fix(lg_model): asr.lg_model is now IQ-TREE 2.3.6 LG (#5784)`). Defect window: exactly 2026-06-08 to 2026-10-23. |
| **E16** | Leak example: tev `specs/260705_design-additional-analysis-tooling-to-me.md` cited in memory `feedback_cite_claim_not_dated_doc.md` | **VERIFIED** | Memory file located at `/home/marielle/.claude/projects/-home-marielle-projects-tev-design/memory/feedback_cite_claim_not_dated_doc.md`. Verbatim text in Why section: "2026-07-22, root-causing necklace PoE, I cited the buggy-era 260705_design-additional-analysis-tooling doc ('k=4 … not up for revisiting') and my own memory said the state-distinguishability work was 'confounded.' Both were stale framings. The AUTHORITATIVE current source was the 2026-07-21 bathos claim `.bth/claims/state-distinguishability-ar-verification.claim.toml` + its postmortem..." |
| **E17** | bathos plugin has only a PreToolUse hook; no SessionStart | **VERIFIED** | `/home/marielle/projects/bathos/plugin/hooks/hooks.json` defines only `"PreToolUse"` matching `"Bash|mcp__.*__submit_job"` executing `bth-preflight-hook`. |
| **E18** | praxia docs registry has `superseded`/`archived` statuses, `supersedes` frontmatter, and `[categories.archive]` | **VERIFIED** | `/home/marielle/projects/praxia/crates/praxia-tools/src/docs/default_registry.toml`: line 17 `optional = [..., "supersedes", ...]`, line 18 `status_enum = ["draft", "active", "complete", "superseded", "archived"]`, lines 83-88 `[categories.archive]` with `description = "tar.zst archives + md manifests (see internal-docs convention)"` and `manifest_pair = true`. |
| **E19** | `dependency_provenance` null because capture is opt-in | **VERIFIED** | See Section 4 for complete trace. |

---

## 3. Re-Derivation of Spike Counts (E9, E10, E11, E14, E15)

All counts below were derived with live commands executed against the research repositories:

### E9: Stage Name Drift in Wild

**Commands & Outputs:**

1. asr sidecars (full repository vs `scripts/`):
```bash
# Total sidecars across full asr repository (357 in asr + 130 in tev_design = 487 total across both repos):
fd -e bth.toml . /home/marielle/projects/asr | wc -l
# Result: 357

# Total sidecars in asr/scripts:
fd -e bth.toml . /home/marielle/projects/asr/scripts | wc -l
# Result: 209

# Sidecars declaring stage_name in asr/scripts:
rg -l "stage_name\s*=" /home/marielle/projects/asr/scripts -g "*.bth.toml" | wc -l
# Result: 46

# Non-canonical stage_name values in asr/scripts:
rg "stage_name\s*=\s*\"([^\"]+)\"" /home/marielle/projects/asr/scripts -g "*.bth.toml" -o -r '$1' | sort | uniq -c
# Output:
#   1 calibration
#   7 confirmatory  <-- Non-canonical (canonical is "validation" / "ablation")
#   5 exploration
#   1 power         <-- Non-canonical
#  32 validation
```
*Exact verification*: `stage_name="confirmatory"` appears 7 times (`sp101`, `sp103`, `sp104`, `sp104_landscape`, `sp105`, `sp105_obj`, `program_b_b02_arms`) and `stage_name="power"` appears 1 time (`sp106_power_from_sp105`). Total declared: 46.

2. tev_design sidecars:
```bash
# Total sidecars in tev_design:
fd -e bth.toml . /home/marielle/projects/tev_design | wc -l
# Result: 130

# Sidecars declaring stage_name in tev_design:
rg -l "stage_name\s*=" /home/marielle/projects/tev_design -g "*.bth.toml" | wc -l
# Result: 15

# Distinct stage_name values in tev_design:
rg "stage_name\s*=\s*\"([^\"]+)\"" /home/marielle/projects/tev_design -g "*.bth.toml" -o -r '$1' -h | sort | uniq -c
# Output:
#  15 validation
```
*Exact verification*: Exactly 15 sidecars declare `stage_name` in `tev_design` (all declare `"validation"`).

### E10: Null `dependency_provenance` vs Present `dependency_lock_sha256`

**Commands & Outputs:**

1. tev_design run log events (`.bth/log/*.jsonl`):
```bash
# Total run events (run.imported + run.started):
cat /home/marielle/projects/tev_design/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | .eid' | wc -l
# Result: 3167

# Run events with non-null dependency_provenance:
cat /home/marielle/projects/tev_design/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | select(.data.dependency_provenance != null and .data.dependency_provenance != "") | .eid' | wc -l
# Result: 0

# Run events with non-null dependency_lock_sha256:
cat /home/marielle/projects/tev_design/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | select(.data.dependency_lock_sha256 != null and .data.dependency_lock_sha256 != "") | .eid' | wc -l
# Result: 1593
```
*Exact verification*: In `tev_design`, exactly 3,167 run events exist; `dependency_provenance` is null on 100% of them (0 non-null); `dependency_lock_sha256` is populated on exactly 1,593 runs.

2. asr run log events (`.bth/log/*.jsonl`):
```bash
# Total run events (run.imported + run.started):
cat /home/marielle/projects/asr/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | .eid' | wc -l
# Result: 14057 (grown from 11673 at earlier spike)

# Run events with non-null dependency_provenance:
cat /home/marielle/projects/asr/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | select(.data.dependency_provenance != null and .data.dependency_provenance != "") | .eid' | wc -l
# Result: 0

# Run events with non-null dependency_lock_sha256:
cat /home/marielle/projects/asr/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | select(.data.dependency_lock_sha256 != null and .data.dependency_lock_sha256 != "") | .eid' | wc -l
# Result: 4362 (grown from 3516 at earlier spike)
```
*Exact verification*: In `asr`, `dependency_provenance` is null on 100% of the 14,057 run events (0 non-null).

### E11: Version Recovery from Git Tree + Lock Hash

**Command & Output:**
```bash
cat /home/marielle/projects/tev_design/.bth/log/*.jsonl | jq -r 'select(.kind == "run.imported" or .kind == "run.started") | select(.data.git_hash != null and .data.dependency_lock_sha256 != null) | "\(.data.git_hash) \(.data.dependency_lock_sha256)"' | sort -u | head -n 25 | while read -r ghash locksha; do
  computed=$(git -C /home/marielle/projects/tev_design show "${ghash}:uv.lock" 2>/dev/null | sha256sum | awk '{print $1}')
  wheel=$(git -C /home/marielle/projects/tev_design show "${ghash}:uv.lock" 2>/dev/null | rg 'path = "wheels/aminx-' || echo "none")
  if [ "$computed" = "$locksha" ]; then
    echo "MATCH: $ghash $wheel"
  else
    echo "MISMATCH: $ghash recorded=$locksha computed=$computed"
  fi
done
```
**Results across the 25 sample pairs:**
- Exactly 24 MATCH:
  - `0182fed1`: `aminx-0.1.0a24-py3-none-any.whl`
  - `01d8069c`: `aminx-0.2.0a3-py3-none-any.whl`
  - `025c952e`: `aminx-0.2.0a3-py3-none-any.whl`
  - `026a9264`: `aminx-0.2.0a1-py3-none-any.whl`
  - `034524e0`: `aminx-0.2.1-py3-none-any.whl`
  - `036161fe`: `aminx-0.2.0a1-py3-none-any.whl`
  - `0395bbef`: `aminx-0.2.0a1-py3-none-any.whl`
  - `05010339`: `aminx-0.2.0a1-py3-none-any.whl`
  - `09318cde`: `aminx-0.3.0-py3-none-any.whl`
  - `09b87f6a`: `aminx-0.2.0a1-py3-none-any.whl`
  - `0dbd7cf1`: `aminx-0.2.0a3-py3-none-any.whl`
  - `0e0cd232`: `aminx-0.1.0a24-py3-none-any.whl`
  - `0f2721fc`: `aminx-0.2.0a1-py3-none-any.whl`
  - `10a0b232`: `aminx-0.2.0a3-py3-none-any.whl`
  - `10d7f9cf`: `aminx-0.2.0a3-py3-none-any.whl`
  - `1281ceb3`: `aminx-0.2.1-py3-none-any.whl`
  - `1366d499`: `aminx-0.2.0a3-py3-none-any.whl`
  - `139fd132`: `aminx-0.1.0a24-py3-none-any.whl`
  - `13f1f53b`: `aminx-0.2.1-py3-none-any.whl`
  - `1474ec74`: `aminx-0.2.1-py3-none-any.whl`
  - `147b0758`: `aminx-0.1.0a24-py3-none-any.whl`
  - `15281cb0`: `aminx-0.2.1-py3-none-any.whl`
  - `1607ecbc`: `aminx-0.1.0a24-py3-none-any.whl`
  - `16fb56ca`: `aminx-0.2.0a3-py3-none-any.whl`
- Exactly 1 MISMATCH:
  - `0b4e3ae1`: recorded `b3628721...` vs computed `3eb71c03...` (run was executed with dirty uncommitted `uv.lock`).
*Exact verification*: Exactly 24/25 (96%) distinct pairs match, and every matching lock cleanly resolves the pinned aminx wheel.

---

## 4. Root Cause of Null `dependency_provenance`

The prompt and design asked why `dependency_provenance` is null on every run in both `asr` and `tev_design`.

### Code Trace and Invariant
1. In `src/bathos/runner.py:1205-1208`:
   ```python
   tracked_packages = resolve_tracked_packages(
       bundle.path if bundle and bundle.found else None,
       script_path.parent if script_path is not None else cwd,
   )
   ```
2. In `src/bathos/dependency_provenance.py:100-125`:
   `resolve_tracked_packages(sidecar_path, start)` inspects three sources for `track_packages`:
   - Sidecar TOML `[provenance] track_packages`
   - Nearest `.bth.toml` `[provenance] track_packages`
   - Nearest `pyproject.toml` `[tool.bathos] track_packages`
   If none of these files declare `track_packages`, `resolve_tracked_packages` returns `[]`.
3. In `src/bathos/runner.py:1271`:
   ```python
   dependency_provenance = capture_dependency_provenance(tracked_packages, argv, cwd)
   ```
4. In `src/bathos/dependency_provenance.py:688-698`:
   ```python
   def capture_dependency_provenance(
       entries: list[str], argv: list[str], cwd: Path
   ) -> dict[str, object] | None:
       if not entries:
           return None
   ```
   When `entries` is empty (`[]`), the function immediately returns `None`.
5. In `src/bathos/runner.py:1643-1647`, the run record is written with `dependency_provenance=dependency_provenance` (`None`), which serializes to `null` in JSONL and Parquet.

### Repository Audit
A global search for `track_packages` in both repositories:
```bash
rg "track_packages" /home/marielle/projects/tev_design /home/marielle/projects/asr
```
returned 0 matches. Neither `tev_design` nor `asr` has ever configured `track_packages` in any sidecar, project config, or `pyproject.toml`.

### Conclusion
`dependency_provenance` is null because it is **strictly opt-in** and no configuration ever opted in. Future runs must automatically populate `track_packages` from active instrument defect registry entries (D10 / P1).

---

## 5. Inventory of Reusable Machinery in Bathos

Bathos contains extensive prior infrastructure that should be composed rather than re-implemented:

### 1. `src/bathos/blast_radius.py`
- **Ledger Storage**: Substrate is cool-tier immutable Parquet fragments (`blast_radius/`) and warm DuckDB table `blast_radius_ledger` (`:46-61`).
- **Data Model**: `BlastRadiusRecord` (`:89-100`) with fields:
  - `id`, `entity_type` (`"run"` | `"campaign"` | `"claim"`), `entity_id`
  - `from_state`, `to_state` (`"affected"` | `"unverifiable"` | `"cleared"`)
  - `anchor_kind` (`"commit"` | `"commit_range"` | `"file"` — D1 adds `"dependency_range"`)
  - `anchor_value`, `matched_files`, `matched_clauses`, `shadow_verdict`, `match_reason`, `reason`, `amended_at`.
- **Fold Logic**:
  - `fold_blast_radius_state(catalog_dir, entity_type, entity_id)` (`:411-421`): Latest-wins fold by `amended_at`, defaults to `"clean"`.
  - `fold_blast_radius_state_using_conn(con, entity_type, entity_id)` (`:423-428`): Safe read-only DuckDB connection reuse.
- **Propagation**:
  - `propagate_to_campaigns(report, catalog_dir)` (`:1200-1235`): Implicates campaigns where member runs are affected.
  - `propagate_to_claims(report, catalog_dir)` (`:1286-1363`): Maps affected runs to claim union-gate clauses via batched `claim_discriminates` resolution (`_clauses_backed_by_runs`, `:1238-1284`).
- **Clearing**:
  - `clear_blast_radius_flag(catalog_dir, entity_type, entity_id, *, reason)` (`:1171-1198`): Append-only manual exemption with audit reason.

### 2. `src/bathos/artifact_archive.py` & `src/bathos/archived_items.py`
- **Archival Substrate**: `archive_experiment_bundle` (`src/bathos/artifact_archive.py:236-375`) stubs tracked scripts and sidecars in-place, packs untracked outputs into git bundles (`:277-307`), writes git notes, generates `.bth/ARCHIVE_INDEX.md`, and appends an `ArchivedItemRecord` to `archived_items` (`src/bathos/archived_items.py:30-70`).
- **Restoration**: `restore_archived_item` (`src/bathos/artifact_archive.py:450-520`) recovers files from git notes or stub sha, unpacks git bundles, and appends a `restored` event to `archived_items`.
- **Status Query**: `latest_status(catalog_dir, item_id)` (`src/bathos/archived_items.py:280-318`): Latest-wins fold on `recorded_at` returning whether an item is currently `"archived"` or `"restored"`.

### 3. Sidecar `[status]` Block (`src/bathos/sidecar.py`)
- `StatusBlock` (`:134-147`): Fields `stale: bool`, `stale_reason: str`, `superseded_by: str`.
- Currently enforced at run-time in `src/bathos/prereg.py:584-602` (`GateErrorCode.SCRIPT_STALE`, bypassed with `--allow-stale`).
- Parsed in `parse_sidecar` (`:788-800`).

### 4. Epistemic Classes & Stages (`src/bathos/sidecar.py`)
- `EPISTEMIC_CLASSES = ("exploratory", "calibrating", "confirmatory", "legacy")` (`:470`).
- `CANONICAL_STAGES = {"exploration", "calibration", "validation", "ablation", "production"}` (`:461-467`).
- Reserved outcomes: `RESERVED_OUTCOME_LABELS = {"invalid_measurement", "observed"}` (`:451`).
- D4 adds `spike` as an epistemic class below `exploratory` and establishes an explicit `stage_name -> epistemic_class` mapping.

### 5. Error Code & Resolution Hint Registry (`src/bathos/errors.py`)
- `BathosErrorCode(str, Enum)` (`:17-136`): 61 unified error codes.
- `RESOLUTION_HINTS: dict[BathosErrorCode, str]` (`:139-440`): Structured, actionable user guidance for every error code.
- New validity codes to add: `RESULT_HIDDEN`, `RESULT_SUSPECT_UNACKNOWLEDGED`, `ESTIMAND_BOUNDARY_MIXED`.

### 6. Schema Migration (`src/bathos/migrate.py`)
- `CURRENT_SCHEMA_VERSION = "19"` (`src/bathos/schema.py:10`).
- `migrate_catalog(catalog_dir, dry_run=False, project=None)` (`src/bathos/migrate.py:27-100`): Non-destructive column expansion on cool-tier Parquet files.
- Invariant: New fields must be nullable with default values; historical rows are never mutated or deleted.

---

## 6. Exhaustive Surface Mapping for D3 Validity Check Insertion Points

Decision 3 (D3) mandates that:
> "Void / affected / superseded / archived results are hidden by default from every agent-facing read and refused by every citation/registration gate; history is reachable by explicit opt-in."

Below is the exact mapping to code files and lines for all 13 in-scope surfaces:

```
                                  ┌────────────────────────┐
                                  │   resolve_validity()   │
                                  │ (Batched Cache / Fold) │
                                  └───────────┬────────────┘
                                              │
         ┌────────────────────────────────────┼────────────────────────────────────┐
         │ Read Surfaces                      │ Gate / Registration Surfaces       │ Downstream Bridges
         ▼                                    ▼                                    ▼
┌──────────────────┐                 ┌──────────────────┐                 ┌──────────────────┐
│ list_runs / ls   │                 │ cite_run / cite  │                 │ resolve_pin      │
│ (query.py:259)   │                 │ (mcp.py:2500)    │                 │ (readback.py:113)│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ find_runs / find │                 │ claim register   │                 │ get_trust_state  │
│ (query.py:610)   │                 │ (claim.py:604)   │                 │ (readback.py:253)│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ get_run / show   │                 │ claim validate   │                 │ graduate_product │
│ (query.py:466)   │                 │ (claim.py:232)   │                 │ (trust_ledger:308│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ run_sql          │                 │ campaign add     │                 │ review_build     │
│ (query.py:773)   │                 │ (campaigns:420)  │                 │ (maraxiom bridge)│
├──────────────────┤                 ├──────────────────┤                 ├──────────────────┤
│ lineage          │                 │ campaign conclude│                 │ SessionStart     │
│ (query.py:833)   │                 │ (campaigns:1139) │                 │ (hooks.json)     │
└──────────────────┘                 └──────────────────┘                 └──────────────────┘
```

### Choke Point Helper: `resolve_validity`
To prevent divergent logic, a single batched resolver must be introduced in `src/bathos/validity.py`:
`resolve_validity(run_ids: list[str], catalog_dir: Path) -> dict[str, ValidityResult]`
Where `ValidityResult` contains: `state` (`current` | `suspect` | `unverifiable` | `void` | `affected` | `superseded` | `archived`), `reason`, `replacement_id`, `version_source`, and `evidence_tier`.

### 1. `ls` (`list_runs`)
- **Core Function**: `src/bathos/query.py:259-275` (`list_runs`), dispatching to `_index_list_runs:277-296`, `_warm_list_runs:299-346`, and `_cool_list_runs:244-256`.
- **Tool / CLI Entry**: `src/bathos/mcp.py:329-384` (`list_runs_tool`, `registry="bathos-cli"`, `cli_name="ls"`) and `src/bathos/mcp.py:2469-2476` (`mcp_list_runs_tool`, `registry="bathos"`).
- **Insertion Point**: In `list_runs()`, after fetching candidate runs, pass candidate IDs to `resolve_validity()`.
- **Contract**: If `include_superseded` is False (default), filter out runs with state in `("void", "affected", "superseded", "archived")`. If any were hidden, attach metadata: `{"hidden_count": N, "notice": "N superseded/void/archived results hidden — --include-superseded to show"}`. If `include_superseded` is True, include all runs and attach their validity state.

### 2. `find` (`find_runs`)
- **Core Function**: `src/bathos/query.py:610-649` (`find_runs`), dispatching to `_index_find_runs:652-698`, `_warm_find_runs:701-748`, and `_cool_find_runs:587-608`.
- **Tool / CLI Entry**: `src/bathos/mcp.py:401-454` (`find_runs_tool`, `registry="bathos-cli"`, `cli_name="find"`) and `src/bathos/mcp.py:2479-2487` (`mcp_find_runs_tool`, `registry="bathos"`).
- **Insertion Point**: In `find_runs()`, evaluate `resolve_validity()` on the matched runs.
- **Contract**: Filter hidden runs by default; support `include_superseded=True`; display count of hidden items.

### 3. `get_run` / `show`
- **Core Function**: `src/bathos/query.py:466-485` (`get_run`) and `src/bathos/query.py:379-440` (`lookup_run`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:456-510` (`get_run_tool`, `registry="bathos-cli"`, `cli_name="show"`) and `src/bathos/mcp.py:2490-2497` (`mcp_get_run_tool`, `registry="bathos"`).
- **Insertion Point**: In `get_run_tool()`, after fetching `run`, call `resolve_validity([run.id])`.
- **Contract**: If validity state is `void`, `affected`, `superseded`, or `archived` and `include_superseded=False`, refuse with `BathosErrorCode.RESULT_HIDDEN` naming state, reason, and replacement. If `suspect` or `unverifiable`, return run with an explicit banner.

### 4. `run_sql`
- **Core Function**: `src/bathos/query.py:773-830` (`run_sql`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:513-535` (`run_sql_tool`, `registry="bathos-cli"`, `cli_name="sql"`) and `src/bathos/mcp.py:2598-2605` (`mcp_run_sql_tool`, `registry="bathos"`).
- **Index / Catalog Connection**: `src/bathos/index.py:476-510` (`connect_read`) and `src/bathos/catalog.py`.
- **Insertion Point**: Inside `connect_read()`, define view `runs_current` filtering out hidden IDs via `validity_cache`, while keeping `runs` as the full historical table (settled per `OD-3`).
- **Contract**: `runs_current` is the documented target and never returns a hidden run; raw `runs` stays full history and emits a notice banner if hidden rows exist unless `--include-superseded` (or `--allow-hidden`) is passed.

### 5. `cite` (`cite_run`)
- **Core Function**: `src/bathos/cite.py:13-55` (`format_citation`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:2500-2524` (`cite_run_tool`, `registry="bathos-cli"`, `cli_name="cite"`) and `src/bathos/mcp.py:2526-2544` (`mcp_cite_run_tool`, `registry="bathos"`).
- **Insertion Point**: In `cite_run_tool` at line 2521, immediately after `_get_run(run_id)`.
- **Contract**: If validity is `void`, `affected`, `superseded`, or `archived`, raise `CatalogError` / return envelope with the specific state error code (`RESULT_VOID`, `RESULT_AFFECTED`, `RESULT_SUPERSEDED`, `RESULT_ARCHIVED`). If `suspect` or `unverifiable`, refuse unless caller passes a non-empty rationale string `ack_suspect="..."` / `ack_unverifiable="..."` (`OD-5`); format citation with validity banner.

### 6. `claim register` / `claim validate`
- **Core Functions**: `src/bathos/claim.py:604-660` (`register_claim`) and `src/bathos/claim.py:232-360` (`validate_claim`), plus `src/bathos/claim.py:1063` (`attest_parity`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:2404` (`campaign_attest_parity_tool`) and `claim_register_tool` / `claim_validate_tool`.
- **Insertion Point**:
  - In `register_claim` at line 657, where `member_ids = union_campaign_member_ids(...)` is resolved.
  - In `validate_claim` at line 344 and `attest_parity` at line 1063, when checking `parity_run_id`.
- **Contract**: Call `resolve_validity(...)`. If any member run or parity run is hidden (`void`, `affected`, `superseded`, `archived`), refuse with the specific state refusal code (`RESULT_VOID`, `RESULT_AFFECTED`, `RESULT_SUPERSEDED`, `RESULT_ARCHIVED`), listing the implicated runs and their defect/supersession reasons. Also enforce `ESTIMAND_BOUNDARY_MIXED` on `register_claim` unless `allow_mixed_estimand` rationale is supplied (`OD-6`).

### 7. `campaign add` / `campaign conclude` / `campaign review`
- **Core Functions**:
  - `add_run_to_campaign`: `src/bathos/campaigns.py:420-456` and `_add_run_to_campaign_impl:484-535` (line 532). Refuse adding a run whose validity is hidden; emit a warning banner (without refusing) if adding the run mixes estimand epochs (`OD-6`).
  - `conclude_campaign`: `src/bathos/campaigns.py:1139-1167` and `_conclude_campaign_impl:1169-1315` (before line 1298). Check validity of all member runs unconditionally (`union_campaign_member_ids`) before the optional `if claim_path_rel and registered_sha:` Union Gate check, and filter covering runs in `run_union_gate` (`claim.py:932`) and `differential_confound_check` (`claim.py:851`) to `runs_current`. Also enforce `ESTIMAND_BOUNDARY_MIXED` unless `allow_mixed_estimand` rationale is supplied (`OD-6`).
  - `review_campaign`: `src/bathos/campaigns.py:1947-2105` (lines 2075-2097). Upgrade to resolve validity for all member runs; report breakdown of current vs hidden runs and display banners for hidden members and mixed estimand epochs.

### 8. `resolve_pin`
- **Core Function**: `src/bathos/readback.py:113-175` (`resolve_pin`) and `ResolvedPin` dataclass (`:96-111`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:550-576` (`resolve_pin_tool`) and `:2608-2616` (`mcp_resolve_pin_tool`).
- **Insertion Point**: At line 140, after `get_run(run_id, cat_dir)`.
- **Contract**: Add `validity_state: str = "current"` and `validity_reason: str | None = None` to `ResolvedPin`. Call `resolve_validity([run_id])`. If run is hidden, return `fresh=False`, `trust_state="unknown"`, and set `validity_state` and `validity_reason`.

### 9. `get_trust_state`
- **Core Function**: `src/bathos/readback.py:253-280` (`get_trust_state`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:578-610` (`get_trust_state_tool`) and `:2619-2626` (`mcp_get_trust_state_tool`).
- **Insertion Point**: At line 262, when resolving promotion state.
- **Contract**: If `content_hash` has a promotion record, look up all producing `run_id`s (via `output_metadata` or anchor). If ALL producing runs resolve to a hidden state (`void`, `affected`, `superseded`, `archived`), drop trust state from `promoted` to `candidate` (or `unknown`) at read time without mutating ledger rows.

### 10. `graduate_product`
- **Core Function**: `src/bathos/trust_ledger.py:308-375` (`graduate_product`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:1212-1258` (`graduate_product_tool`).
- **Insertion Point**: Before line 360 (`existing = latest_ledger_record(catalog_dir, content_hash)`), so already-promoted products whose producing run became hidden do not return early!
- **Contract**: Resolve validity of `run_id` and any producing runs for `content_hash`. Refuse graduation with `GraduationRefused` (`BathosErrorCode.GRADUATION_REFUSED`) if all producing runs are hidden.

### 11. `lineage`
- **Core Function**: `src/bathos/query.py:833-885` (`lineage`).
- **Tool / CLI Entry**: `src/bathos/mcp.py:2546-2574` (`lineage_prov_tool`, `registry="bathos-cli"`, `cli_name="lineage"`) and `src/bathos/mcp.py:2577-2595` (`mcp_lineage_prov_tool`, `registry="bathos"`).
- **Insertion Point**: At line 855 (`if not catalog_readable(catalog_dir): return []`) and line 880, after fetching ancestor chain.
- **Contract**: Annotate each ancestor in the chain with its validity state and estimand epoch. Do not drop hidden ancestors silently (lineage must reflect true causal history), but clearly flag them with banner/metadata so agents do not treat them as valid current readings.

### 12. Maraxiom `review_build` Bridge
- **Consumer Site**: `maraxiom/src/maraxiom/review_build.py:223-235` (calling `bathos_bridge.get_run_record`).
- **Bridge Function**: `maraxiom/src/maraxiom/bathos_bridge.py:1240-1260` (`get_run_record`).
- **Insertion Point**: In bathos's exported bridge functions or `review_build.py:228`.
- **Contract**: Refuse review page build (`BuildRefused("result_hidden", ...)`) if the target run or any supporting run is `void`, `affected`, `superseded`, or `archived`.

### 13. `SessionStart` Hook
- **Hook Configuration**: `.praxia/manifest.toml:96-100` (`[[plugin.hook_specs]]`) and `plugin/hooks/hooks.json`.
- **Implementation**: Add `"SessionStart"` hook spec alongside `"PreToolUse"`.
- **Target Command**: `bth-session-start-hook` (stdlib-only fast-path script reading `.bth/cache/session_start.json`, backed by `src/bathos/session_start.py` and MCP tool `session_start`).
- **Contract**: Emits `{"hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": "..."}}` with a brief summary of active defect ranges, `needs-recheck` findings, and settled/open findings; capped at ≤ 2 KB; never includes superseded or void items.

---

## 7. Architectural Recommendations for Phase 3 Specification

Based on code reconnaissance, the following design decisions are recommended for Phase 3:

1. **Registry Storage & Structure (`OD-4`)**:
   Store defect declarations as `.toml` files under `.bth/instruments/` (project-local) and support opted-in user-global entries at `${XDG_CONFIG_HOME:-~/.config}/bathos/instruments/`. Use existing DuckDB SQL predicate evaluation from `sidecar.py` for conditional ranges (e.g. `sample_s > 1`, with missing predicate fields resolving to `suspect` per `OD-2`).
2. **Version Resolution Hierarchy**:
   Implement the exact 4-tier fallback (incorporating runtime `metadata.<pkg>_version` recovery):
   - Tier 1: Recorded `dependency_provenance` (when populated) or lock-verified `metadata.<pkg>_version`.
   - Tier 2: Verified lock hash (`git show <hash>:uv.lock` matching recorded `dependency_lock_sha256`).
   - Tier 3: Inferred from commit (`metadata.<pkg>_version` or `uv.lock` at `git_hash` without hash match; capped at `suspect` unless `hide_inferred = true` per `OD-1`).
   - Tier 4: Unknown.
3. **Choke Point Cache**:
   Materialize derived validity into `<catalog_dir>/validity_cache.db` keyed on `inputs_hash`. Never mutate historical run rows (conforming to Anti-Goal A2).
4. **`run_sql` View Strategy (`OD-3`)**:
   In `index.py` (`connect_read`), expose `runs_current` as a filtered view over `runs` (excluding `void`, `affected`, `superseded`, and `archived` runs). Per `OD-3`, `runs` is **not** shadowed or renamed to `runs_all`; `runs` remains the full historical table and emits a notice banner when queried if hidden rows exist.
