---
title: 'Bearings v1 slice 2: blind packet and reading record'
description: 'Build plan for Bearings slice 2 (AC-4, AC-5, AC-3 atom half): deterministic blind packet read from a git ref, citation-checked reading record, separate mapper step that produces mapped/unmapped/silent items.'
status: active
task_id: 261104_multiscale-goal-awareness
date: '261104'
sprint: ''
backlog_ids: ''
---
# Bearings v1 slice 2: blind packet and reading record

Spec: `.praxia/docs/specs/261104_bearings-multiscale-goal-awareness.md`, Design items 4 and 5. This slice delivers AC-4, AC-5 and the `atom:` half of AC-3. It builds on slice 1 (`wt/261104_bearings-slice1`, commit `0cf872a8`). Surface: an in-session Claude fixer, then a Sonnet review (spec Decision Log).

## Out of this slice

- `bth bearings brief`, `label`, `due`, the due notice and sprint-audit signal (slice 3).
- AC-7 and any replay. The orchestrator drafts that sidecar after this slice, and the owner approves it.
- Running a reader. Bathos builds the packet and checks what comes back; it never dispatches an agent (T3).
- `bth charter draft` from the record. Slice 1 moved this here because it needs the packet, but it also needs a reader to propose purposes. It stays a scaffold in v1 unless the owner asks otherwise.

## Decisions this plan settles (the spec left them open)

| # | Question | Decision |
|---|---|---|
| Q1 | **How does a blind reader's free text get compared with the charter?** Design 5 says "Bathos then compares", but a mechanical text match would be a judgment proxy, which the frame rules out, and the reader cannot see charter ids. | **A separate mapper step.** `bth bearings record` stores the blind reading. `bth bearings compare --mapping FILE` takes a mapping written by a *different* agent, one that sees the reading and the charter but never wrote the reading. Bathos checks completeness and validity and computes `mapped`, `unmapped` and `silent`. This splits the spec's single step in two, and it is flagged for owner review. |
| Q2 | Where do docs and git subjects come from? | **The git object store at `--ref`, never the working tree.** That gives determinism (a dirty tree cannot change the hash) and the commit cut AC-7 requires. |
| Q3 | Window | `--since` and `--until` are required ISO-8601 timestamps. Nothing reads the clock, so the same arguments give the same hash. Slice 3's cadence computes them. |
| Q4 | Output location | `--out DIR` is required and must be empty or absent. No default path (global rule on derived-data directories). |
| Q5 | What is the blindness check applied to? | **Every text in the packet:** docs, sidecars, backlog titles, git subjects and serialised run records, not only docs. The spec says "any doc", but a sidecar hypothesis or commit subject can quote a purpose just as easily. |
| Q6 | Which texts are the needles? | The mandate and every purpose `text` (live **and** retired) from the current charter file, plus those in the latest `charter_confirmations` row, so a pivot cannot leak an old purpose. `--charter-file PATH` adds a charter to scan against, which AC-7 needs because its cut predates the charter. |
| Q7 | Needle edge cases | Comparison collapses whitespace runs to one space and is case-sensitive. **An empty needle is skipped** (an empty mandate would otherwise match everything). A needle under 12 characters after normalising is skipped and listed under `short_needles_skipped` in the manifest, because a one-word purpose would empty the packet. |
| Q8 | Which doc categories are in? | **An allowlist**: `.praxia/docs/decisions/` and `.praxia/docs/specs/`, recursively. `handoffs/` and `daily/` get reason `narrative`. Everything else under `.praxia/docs`, including `INDEX.md`, gets reason `not_primary_category`. A new category is out until someone adds it. |
| Q9 | Harness files | Excluded by basename anywhere in scope (`CLAUDE.md`, `AGENTS.md`, `GEMINI.md`), with reason `harness_file`. `.bth/charter.toml` and `.bth/charter.draft.toml` get reason `charter`. |
| Q10 | Backlog titles | `.praxia/backlog.jsonl` is empty because the backlog lives in praxia's database, and bathos must not depend on that. `--backlog-titles FILE` is optional, one title per line. Without it, the manifest says `backlog: not_provided`. |
| Q11 | Sidecars | For each run in the window, read `sidecar_path` at the run's own `git_hash` (what actually ran), falling back to `--ref`. Absolute paths are made relative to the repo root. Anything unresolvable is listed in the manifest as `sidecar_unresolved`. |
| Q12 | Reachability | `--reachable-only` drops runs whose `git_hash` is not an ancestor of `--ref`, with reason `unreachable_from_ref`. AC-7 needs it. Off by default, because a cadence reading should see work on feature branches, which is where colliculix's drift sat. |
| Q13 | Partial rejection | `record` is atomic. If any purpose has fewer than 2 resolvable citations, it raises `STEPBACK_INSUFFICIENT_EVIDENCE` naming every failing purpose, and appends nothing. |
| Q14 | What counts as resolvable? | **Resolves against this packet**, not the catalog. A citation to a doc the packet excluded must not resolve. Do **not** change `stepback/analysis.py`; its resolver is keyed on campaign membership. |
| Q15 | Open lines | Each needs at least 1 resolvable citation, and each becomes an `unmapped` item at compare. |
| Q16 | Mapping onto a retired purpose | Stored as `unmapped` with `matched_retired = <id>`. Work toward a retired purpose serves no live purpose, and it is the most useful signal this feature can give. |
| Q17 | No confirmed charter at compare time | `compare` refuses (`BEARINGS_NO_CONFIRMED_CHARTER`). The reading stays stored and can be compared after the owner confirms. `record` needs no charter at all, so a reading never waits on confirmation (pre-mortem failure path A10). |
| Q18 | Reading identity | `reading_id = sha256(canonical JSON of the reading file)[:16]`. Recording the same reading again returns `already_recorded` and appends nothing. |

**Known limit of Q1.** `reader_id` and `mapper_id` are self-reported strings. Bathos refuses when they are equal, but it cannot prove two distinct agents ran. A mapper that maps everything hides drift; the owner's labels in slice 3 are what measure that.

## Components

**C1. `src/bathos/bearings/__init__.py`** (new, a package like `stepback/`).

**C2. `src/bathos/bearings/packet.py`** (new). `build_packet(project_root, *, out, since, until, ref="HEAD", project=None, charter_file=None, backlog_titles=None, reachable_only=False, catalog_dir=None) -> dict`.

Layout:

```
<out>/reader/index.json        what the reader may cite: project, ref commit sha, window, docs [{path, blob_sha}], sidecars [{path, blob_sha}]
<out>/reader/runs.jsonl        one record per run
<out>/reader/git_log.jsonl     {sha, committed_at, subject}
<out>/reader/backlog_titles.txt  only if provided
<out>/reader/docs/<path>       bytes from the object store
<out>/reader/sidecars/<blob_sha>/<path>
<out>/manifest.json            NEVER shown to the reader: exclusions [{path, reason}], packet_sha256, short_needles_skipped, backlog, sidecar_unresolved
```

- `packet_sha256` is sha256 over canonical JSON of the sorted list of `[relative path under reader/, sha256 of bytes]`. `verify_packet(out)` recomputes it.
- **No timestamps, hostnames, absolute paths or `out` paths anywhere under `reader/`.** All lists sorted. JSON is canonical (`sort_keys=True`, compact separators), lines end `\n`.
- Run record fields, from `connect_read`, filtered by `project_slug` and `since <= timestamp < until`, ordered by `(timestamp, id)`: `id, timestamp, command, git_hash, git_branch, status, outcome, exit_code, tags, campaign_id, sidecar_path, sidecar_sha256`. Check the column names against `RUNS` DDL in `src/bathos/index.py` (~line 95) and leave out any that do not exist. Do not include `metadata`, postmortem fields or anything free-form that an agent narrated. `sidecar_path` is made repo-relative.
- Git access: `subprocess.run([...], check=True, capture_output=True)` with explicit argv, no shell. Resolve `--ref` once to a full sha and use that sha everywhere. Docs: `git ls-tree -r <sha> -- .praxia/docs`, then `git cat-file blob`. Log: `git log <sha> --format=%H%x00%cI%x00%s`, filtered to the window in Python rather than with `--since`, whose behaviour with non-monotonic dates is not exact. Binary or non-UTF-8 docs get reason `not_text`.
- `--project` defaults to the slug from `.bth.toml`, using the slice 1 helper `_resolve_project_slug` in `charter.py`.

**C3. `src/bathos/bearings/reading.py`** (new).

Reading file (JSON), produced by the blind reader:

```json
{"packet_sha256": "...", "reader_id": "...",
 "purposes": [{"id": "r1", "text": "...", "citations": ["run:abcd1234", "doc:.praxia/docs/specs/x.md@<blob>#L3-L9"]}],
 "open_lines": [{"text": "...", "citations": ["git:abc1234"]}]}
```

Citation kinds, resolved only against the packet's `reader/` contents:
- `run:<id or unique prefix of 8 or more>` in `runs.jsonl`. An ambiguous prefix does not resolve.
- `sidecar:<path>[#clause]` in `index.json`.
- `git:<sha of 7 or more>` in `git_log.jsonl`.
- `doc:<path>@<blob-sha>#Lx-Ly`, where path and blob match `index.json` and `1 <= x <= y <= line count`.
- `atom:` always gives `flag_for_review`. It is never dropped, never fails and never counts toward the minimum. A citation may be a string beginning `atom:`, or an object `{"kind": "atom", ...}` with any extra fields, such as a `context` list of `{start_line, end_line, role}` (allostery draft spec). Stored verbatim.
- Anything else, or an object of another kind, is unresolvable.

`record_reading(project_root, *, reading_file, packet_dir, catalog_dir=None)`: runs `verify_packet`, checks `packet_sha256` matches, resolves citations, then appends `bearings.reading_recorded` with entity `[slug, reading_id]`. Data: `reading_id, packet_sha256, ref_sha, since, until, reader_id, purposes, open_lines`, with each citation stored alongside its resolution (`resolved` or `flag_for_review`).

`compare_reading(project_root, *, reading_id, mapping_file, catalog_dir=None)`. Mapping file:

```json
{"mapper_id": "...", "mapping": {"r1": "P1", "r2": null}}
```

- Refuse if `mapper_id == reader_id`, if `status()` from `charter.py` is not `confirmed`, if the mapping does not cover every reading purpose exactly once, or if it names a charter purpose id that is not in the confirmed purposes.
- Purposes come from the latest `charter_confirmations.purposes_json`, not the file. They are the same bytes by the sha check, but the row is what was confirmed.
- Appends `bearings.compared` with entity `[slug, reading_id]`. Data: `reading_id, mapper_id, charter_sha256, items`.
- Items: `{item_id, kind, reading_purpose_id | open_line_index | charter_purpose_id, matched_retired}`, where `item_id = sha256(canonical JSON of [reading_id, charter_sha256, kind, subject])[:16]`. Slice 3's labels key on `item_id`, so it must be stable.
- `mapped` = mapped to a live purpose. `unmapped` = mapped to null, mapped to a retired purpose (Q16), or an open line (Q15). `silent` = a live confirmed purpose that no reading purpose mapped to.

Both writers go through the slice 1 helper. Rename `_emit_charter_event` in `charter.py` to the public `emit_ledger_event` and call it from both modules, so the log-mode refusal and the failed-append check apply here too. Do not write a second emit path.

**C4. `src/bathos/runlog/fold_bearings.py`** (new), modelled on `fold_charter.py`. Add to `src/bathos/runlog/ingest.py` (beside `_CHARTER_KINDS`, entity length 2) and `src/bathos/index.py` (DDL plus the empty-view fallback):
- `bearings_readings(project, reading_id, eid, ts, packet_sha256, ref_sha, since, until, reader_id, purposes_json, open_lines_json)`
- `bearings_items(project, reading_id, item_id, compare_eid, ts, mapper_id, charter_sha256, kind, subject_json, matched_retired)`

Append-only, idempotent on `eid` / `(compare_eid, item_id)`.

**C5. CLI.** `bearings_app = cyclopts.App(name="bearings")` in `src/bathos/cli_cyclopts.py`, beside `charter_app`, with `packet`, `record` and `compare`. Non-zero exit on every refusal.

**C6. MCP**, in `src/bathos/mcp.py`: `bearings_packet`, `bearings_record`, `bearings_compare`.

**C7. Errors**, in `src/bathos/errors.py`. New `BathosErrorCode` members, `RESOLUTION_HINTS` entries and registry mappings: `BEARINGS_PACKET_INVALID`, `BEARINGS_PACKET_MISMATCH`, `BEARINGS_READING_INVALID`, `BEARINGS_READING_NOT_FOUND`, `BEARINGS_NO_CONFIRMED_CHARTER`, `BEARINGS_MAPPING_INVALID`, `BEARINGS_MAPPER_IS_READER`. `tests/test_errors.py` catches an unmapped class. AC-5's rejection reuses the existing `StepBackInsufficientEvidenceError`.

## Acceptance tests

New files: `tests/test_bearings_packet.py`, `tests/test_bearings_reading.py`, `tests/runlog/test_fold_bearings.py`. Reuse the isolation pattern of the `charter_env` fixture in `tests/test_charter.py`: `HOME`, `BTH_CATALOG_DIR` and the token path all under `tmp_path`, `BTH_LOG_MODE=1`, `reset_writers_for_test()`. **Never touch the real `~/.bth`.** Fixtures build a real git repo in `tmp_path`, with commits made using a pinned author and date. Set `user.name` and `user.email` with `git -c` so the machine's config does not matter.

**AC-4**
- A handoff and a daily log are committed. Neither is in `reader/`, and the manifest names both with reason `narrative`.
- Two builds with the same arguments into different `--out` dirs give equal `packet_sha256` and byte-identical `reader/` trees.
- Editing a tracked doc in the working tree without committing leaves the hash unchanged (Q2). Committing the change and passing the new ref changes it. This is the positive control that the hash can move.
- A confirmed charter is present. An ADR quotes a purpose with **different line wrapping**: it is absent from `reader/` and in the manifest with reason `quotes_charter`. A spec with no quote is present.
- The same check for the mandate, for a retired purpose's text, and for a purpose that appears only in the confirmed row and not in the current file.
- A git subject and a sidecar that quote a purpose are excluded too (Q5).
- `--charter-file` excludes an ADR when no charter is committed (the AC-7 case).
- An empty mandate does not exclude everything. A 5-character purpose text is listed in `short_needles_skipped` (Q7).
- A root `AGENTS.md`, a nested `.praxia/docs/specs/CLAUDE.md` and `.bth/charter.toml` are all absent. No file under `reader/` contains any purpose text.
- `manifest.json` is outside `reader/`, and `reader/` contains no exclusion path.
- `--reachable-only` drops a run from an unmerged branch (Q12).
- A non-empty `--out` is refused.

**AC-5**
- A purpose with 1 resolvable citation is refused with `STEPBACK_INSUFFICIENT_EVIDENCE`, naming it. **No event is appended** even though the other purpose is valid (Q13).
- Each citation kind resolves when it should. Each of these does not: an unknown run id, an ambiguous prefix, a doc with the wrong blob, a line range past the end, a doc the packet excluded, a sidecar not in the packet.
- A reading whose `packet_sha256` differs, and a packet with one tampered byte, are refused (`BEARINGS_PACKET_MISMATCH`).
- Identical re-record returns `already_recorded` with one event.
- With a confirmed charter of P1, P2, P3 (P3 retired before confirming) and a reading r1, r2, r3 mapped to P1, null, P3, plus one open line: items are r1 `mapped`; r2 `unmapped`; r3 `unmapped` with `matched_retired = P3`; the open line `unmapped`; P2 `silent`. They fold into `bearings_items`.
- `compare` refuses: `mapper_id == reader_id`; an unconfirmed charter (edit the file after confirming); a mapping that misses a reading purpose; an unknown charter id. Each appends nothing.
- `item_id` is equal across two runs with the same inputs.

**AC-3, `atom:` half**
- A string `atom:` citation and an object one with a `context` list are both stored verbatim with `flag_for_review`.
- A purpose with 1 resolvable citation plus 2 `atom:` citations is refused: atoms do not count.
- A malformed `atom:` string does not fail the reading.

**Wiring**
- One end-to-end test through the CLI: `packet`, `record`, `compare`, then read `bearings_items`.
- A test that an existing `index.db` without the new tables still opens and `bearings_items` reads empty (the empty-view fallback). Slice 1 has no such test for its tables; add one for those as well, in `tests/test_charter.py`.

## Commands (narrow only)

```
uv run pytest tests/test_bearings_packet.py tests/test_bearings_reading.py tests/runlog/test_fold_bearings.py tests/test_charter.py tests/runlog/test_fold_charter.py tests/test_stepback_escalation.py tests/test_errors.py -q
uv run ruff check src/bathos/bearings src/bathos/charter.py src/bathos/runlog/fold_bearings.py src/bathos/runlog/ingest.py src/bathos/index.py src/bathos/errors.py src/bathos/cli_cyclopts.py src/bathos/mcp.py tests/test_bearings_packet.py tests/test_bearings_reading.py tests/runlog/test_fold_bearings.py tests/test_charter.py
```

**Never run the whole suite** (`uv run pytest` with no paths, or `pytest tests/`). It exhausts this machine's memory.

## Claimed files (exactly these)

```
src/bathos/bearings/__init__.py        (new)
src/bathos/bearings/packet.py          (new)
src/bathos/bearings/reading.py         (new)
src/bathos/runlog/fold_bearings.py     (new)
src/bathos/runlog/ingest.py
src/bathos/index.py
src/bathos/charter.py                  (rename only: emit_ledger_event)
src/bathos/errors.py
src/bathos/cli_cyclopts.py
src/bathos/mcp.py
tests/test_bearings_packet.py          (new)
tests/test_bearings_reading.py         (new)
tests/runlog/test_fold_bearings.py     (new)
tests/test_charter.py                  (empty-view test only)
```

## Review gate before slice 3

1. The orchestrator reruns the narrow commands above and reads the diff; the fixer's summary is not evidence.
2. A Sonnet reviewer checks: can blindness leak (any path from a charter text into `reader/`)? Can each AC test fail? Is the packet really deterministic? It also reviews slice 1's fix commit `0cf872a8`, which has had no second review.
3. The orchestrator drafts the AC-7 sidecar for owner approval. No replay runs before the owner approves and it is committed.
