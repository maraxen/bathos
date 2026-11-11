---
title: 'Bearings slice 3: shadow integration with cisternal 0.1.7'
description: 'Plan for wiring Bearings into cisternal shadow mode: at-work labels from bth run, expanded binary subjects, mode-state TOMLs, isolation test, shadow log location.'
status: draft
task_id: 261104_multiscale-goal-awareness
date: '261111'
sprint: ''
backlog_ids: ''
---
# Bearings slice 3: shadow integration with cisternal 0.1.7


Spec: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md`, T7 (owner decisions 261110 on using cisternal shadow mode). Pattern: cisternal skill `developing-in-shadow`; cisternal 0.1.7 `cisternal.shadow`. Floor bumped in e5caf5ae.

Scope (owner 261110): emitters, mode-state TOMLs and an isolation test only. Bearings shows nobody anything; `current_stage = "shadow"` everywhere; no `bth bearings brief`, no advisory output.

## Assumptions, tagged

| # | Assumption | Tag |
|---|---|---|
| A1 | `emit_verdict`/`emit_label`/`emit_isolation` return False and record nothing when no cisternal pipeline exists | verified, `cisternal/shadow/events.py:21-55` |
| A2 | In bathos the cisternal pipeline starts only when `CISTERNAL_TELEMETRY` is set | verified, `telemetry_bridge.py:13,53` |
| A3 | `cisternal.init(log_dir=...)` is idempotent and global (one pipeline per process) | verified, `cisternal/__init__.py:88-113` docstring and `init_pipeline` |
| A4 | The scorer filters on `feature` (and `feature_version` for verdicts), joins on `subject_key`, keeps the latest verdict, picks a label by source rank then ts | verified, `scoring.py:332` `collect_records` |
| A5 | `cisternal shadow check` and `evaluate_gate` accept `--verdicts-file` / `--labels-file` in place of a records dir | verified, `shadow/cli.py:148`, `mode_state.py:267,482` |
| A6 | `bth run` parses the sidecar before the script runs and knows its path and sha256 of bytes | verified, `runner.py:1106-1109`, `sidecar.py:730` |
| A7 | `read_purpose_label` returns the slug list or None and raises on a malformed label | verified, `purpose_label.py:127` |
| A8 | The packet builder reads only the catalog index, the charter and the repo, never `~/.bth/catalog/logs` | verified by reading `bearings/packet.py` (spike 261110); re-checked by the isolation test (C6) |
| A9 | Heartbeat thread from `cisternal.init` inside `bth run` is harmless to SLURM jobs | unverified; covered by C1 being off unless configured, and a test that a run with no shadow dir starts no pipeline |

## The design correction (T7 fit note)

T7 said the `subject_key` must include the reader and packet. That holds only for the *derived* subjects. A label written at `bth run` time cannot know any reader or packet, so the raw label is keyed on the sidecar alone, and bathos expands it at check time (C4) into subjects that carry packet and reader. The reader arm (readers vs mapper-only) goes in the feature name.

## Components

**C1. Shadow pipeline init** (`src/bathos/bearings/shadow.py`, new). `ensure_shadow_pipeline()` resolves the shadow log dir and calls `cisternal.init(log_dir=...)` once. Resolution per the global derived-data rule: explicit argument > `BTH_SHADOW_LOG_DIR` (`none`/empty disables) > `[tool.bathos] shadow_log_dir` in the nearest `pyproject.toml` > `${XDG_CONFIG_HOME:-~/.config}/bathos/config.toml` `shadow_log_dir` > off. It returns the layer that decided; a malformed config file fails loudly. When cutover (`CISTERNAL_TELEMETRY`) already started a pipeline, emits go to that pipeline and the function reports `pipeline=cutover`. **Pending D1.**

**C2. At-work label at `bth run`.** After `parse_sidecar` succeeds (`runner.py:1109`), if the sidecar carries `[charter] purpose`: read the project's charter state (`charter.status`). Emit only when the charter is confirmed and every slug is a confirmed purpose id or `off-charter`; otherwise log `bearings.label_skipped` with the reason (unconfirmed, unknown slug) and emit nothing. Emit:
- `feature = "bearings.purpose_label"`, `subject_key = "sidecar:<project>:<repo-relative path>@<sha256 of bytes>"`, `label = sorted slug list`, `label_source = "at_work"`, `labeller = "bth-run"`, extra fields `charter_sha256`, `project`, `run_id`.
Never fails the run: any error is logged and swallowed. The emit is a no-op when C1 resolves off.

**C3. Verdict emission from the shadow reader pass.** Wherever a comparison is produced (`reading.compare_reading`, and the AC-9 harness's `comparison_from_mapping`), a new `emit_comparison_verdicts(comparison, packet_dir, reader_id, arm)` writes one raw `shadow.verdict` per cited sidecar and per charter purpose:
- `feature = "bearings.<arm>.raw"` (`arm` = `readers` or `mapper_only`), `feature_version = sha256(mapper prompt)[:16] + "-" + charter_sha256[:16]`, `subject_key = "packet:<packet_sha>|reader:<reader_id>|sidecar:<...>"` or `"...|purpose:<id>"`, verdict = predicted targets / reported state.
Raw verdicts are never scored directly.

**C4. Expansion into binary subjects** (`bearings/shadow.py`, `expand(records_dir, out_dir)`). Reads raw verdicts and raw labels, joins sidecar subjects through the packet manifest's sha256 list, and writes `verdicts.jsonl` and `labels.jsonl` per gate feature in cisternal's record shape. Gate features per arm:

| Feature | Subject | Verdict true when | Label true when | Metric read |
|---|---|---|---|---|
| `bearings.<arm>.agreement` | packet x reader x sidecar x purpose | the reading purpose citing the sidecar maps to the purpose | the sidecar's at-work label names the purpose | precision = gold agreement |
| `bearings.<arm>.off_charter` | packet x reader x sidecar | the citing reading purpose is unmapped | the label is `off-charter` | recall = off-charter left unmapped |
| `bearings.<arm>.silent` | packet x reader x live purpose | the comparison reports the purpose silent | no shown sidecar in the packet is labelled with it | recall = silent recall; 1 - precision = false-silent |

The silent label is a deterministic function of at-work labels over the packet's shown sidecars. Indeterminate purposes (withheld evidence, `ac9.answer_key`) get no label, so they drop out as before. Expanded labels keep `label_source = "at_work"` and carry `derived_from` (the raw subject keys). **Pending D2.** Labels emitted before the charter confirmation, and the 311 overlay labels (`retrospective`), are excluded by `allowed_label_sources = ["at_work", "owner_confirmed", "frozen_gold"]` and a `charter_sha256` filter in C4.

**C5. Mode-state TOMLs**, `.praxia/mode_state/bearings.<arm>.<metric>.toml`, six files, `current_stage = "shadow"`, `target_stage = "advisory"`, `writes_isolated_enforced_by` naming C6, `threshold_basis` citing `scripts/analysis/replay_bearings_ac9.bth.toml` (AC-9 bars: 0.8 / 0.2 / 0.8 / 0.8). Gate form **pending D3**. Committed before any verdict is emitted: these are a pre-registration.

**C6. Isolation test** `tests/test_bearings_shadow_isolation.py::test_shadow_reader_isolation`: `prove_isolation(actor_live_fn=<packet build + record_reading>, actor_shadow_fn=<same + emit_comparison_verdicts>, live_roots=[project, catalog], shadow_log_dir=tmp logs, feature=...)`, one per feature. Plus bathos-side blindness checks: the shadow log dir must not resolve inside the project, the catalog index or anything the packet builder reads; `ensure_shadow_pipeline` refuses such a path.

**C7. `bth bearings shadow-check`** (CLI + MCP mirror): runs C4 into a temp dir under the shadow log dir, then `check_mode_state` per feature with the expanded files, and prints each feature's `promotion_permitted` and `blocking_reasons`. Read-only; never edits a mode-state file.

## Tests (written first)

- C1: each resolution layer, `none` disables, malformed config raises, a cutover pipeline is reused, no pipeline is started when off.
- C2: labelled sidecar + confirmed charter emits one label with the right key; unconfirmed charter, unknown slug, no label each emit nothing and log the skip; an emit failure never changes the run's exit code.
- C3/C4: a synthetic packet with known labels and a known comparison gives the expected confusion counts per feature; a degenerate mapper (`ac9.degenerate_mappings`) fails every gate; an indeterminate purpose has no label; a retrospective label is not counted.
- C5: every TOML passes `load_mode_state`; `check` with no records exits 2.
- C6: isolation passes, and fails when the shadow arm writes into the project.

## Out of scope

The mapper or charter-wording fix that AC-9's failure calls for; showing anything to the owner; schedules for shadow reader passes; promotion.

## Owner decisions 261111 (AskUserQuestion)

- D1: a separate shadow log (C1). Owner also asked whether bathos should move to cisternal telemetry everywhere. Answer: the cutover is already wired behind `CISTERNAL_TELEMETRY` (`telemetry.py:222,349,391`), so a full move is a default flip with its own checks (SLURM heartbeat thread, log location); it is kept out of this slice as a follow-up. C1 reuses the cutover pipeline when one is running, so nothing here is redone.
- D2: derived silent labels in cisternal (C4 as written).
- D3: point estimate at the AC-9 bar **and** Wilson 95% lower bound at least 0.65: `min_recall`/`min_precision` 0.8 with `*_lower_bound` 0.65; false-silent at most 0.2 is `max_fp_rate = 0.2` on the silent feature (AC-9's false-silent divides by present purposes, so it is the false-positive rate, not 1 - precision; corrected while building).

## Built 261111 (not yet reviewed)

`src/bathos/bearings/shadow.py`, the `runner.py` hook after `run_uuid_var.set`, `bth bearings shadow-check`, `--shadow-packet-dir` / `--shadow-arm` / `--mapper-version` on `bth bearings compare`, six TOMLs in `.praxia/mode_state/`, `tests/test_bearings_shadow.py` (18 tests), and error codes for `ShadowConfigError` plus three exceptions from the AC-9 rounds that `test_errors` found unregistered (`PacketLeakError`, `PurposeLabelError`, `Ac9LabelError`).

Changes from the design above, found while building:
- **Items follow AC-9's units, not sidecars.** AC-9 scores gold agreement per (reading purpose, target) pair and off-charter per reading purpose, so the derived subjects are those (with packet and reader), not per sidecar. The raw verdict freezes `answer_key`, `reading_gold` and the comparison items at emit time, and lists every labelled blob it rests on by at-work subject (`required_labels`). `derive` uses a raw verdict only when each of those labels was logged at work against the same charter sha with the same slugs. Otherwise it is skipped with `labels_not_logged_at_work`.
- **Pooling matches `ac9.pooled_metrics`**: silent uses the intersection of the readers' silent sets per packet; off-charter and agreement are per reading. A test asserts the derived confusion counts equal the pooled hits and denominators.
- `min_verdicts = min_labels = 8`: the fewest items at which a perfect record clears a 0.65 Wilson lower bound.
- No MCP mirror: `bearings` has no MCP surface yet.
- The default `--mode-state-dir` is bathos's own `.praxia/mode_state`, found relative to the source tree. From an installed tool, pass it explicitly.

Before any gate can pass (owner items):
- confirm the tev_design charter;
- set `BTH_SHADOW_LOG_DIR` (or `[tool.bathos] shadow_log_dir`) for `bth run` in tev_design;
- reinstall bth with cisternal 0.1.7;
- run the isolation proof once against the real shadow log, so a passing `shadow.isolation` event exists per feature (`cisternal shadow check` blocks with `no_isolation_evidence` until then).

Follow-up, not built: moving all bathos telemetry onto cisternal by default (owner question 261111).
