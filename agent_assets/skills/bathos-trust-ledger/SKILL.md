---
name: bathos-trust-ledger
description: Promote products via the trust ledger — attestation TOML, PASS-before-promote ratchet, trust states, readback trio, pin freshness
triggers: [graduate_product, register_attestation, resolve_pin, get_trust_state, query_attestation, list_candidates, GRADUATION_REFUSED, GraduationRefused, oracle_match, repro_floor, attestation.bth.toml, trust_state]
---

# bathos-trust-ledger

**30-second mental model:** every product is identified by its `content_hash` (a sha256). Products sit in one of three trust states — `unknown`, `candidate`, `promoted`. The ONLY way to `promoted` is `bathos.trust_ledger.graduate_product`, which refuses (`GRADUATION_REFUSED`) unless a **PASS** attestation for that `content_hash` already exists in the catalog. Attestations are TOML sidecars anchored durably by their OWN sha256, pointing AT the product via their `attested.content_hash`. Registering an attestation promotes nothing; graduating without a registered PASS attestation is refused.

## Anchor identity vs product identity (the sha256-vs-content_hash trap)

Every anchor record carries TWO hash fields with different meanings:

- **`sha256` — the anchor's own identity.** The sha256 of the anchored file itself.
- **`content_hash` — an optional POINTER to another product.** For attestations this is the *attested product's* hash; the attestation TOML's own bytes are what `sha256` holds.

| You want | Search |
|---|---|
| Anchors whose identity IS the product (e.g. a figure asset) | `find_anchors(catalog_dir, sha256=content_hash)` |
| Attestations/evidence ABOUT the product | `query_attestation(catalog_dir, content_hash)` (internally `find_anchors(content_hash=...)`) |

Consequences agents get wrong:

1. **Registering an attestation does NOT register the product.** The anchor's own sha256 is the TOML's hash, so `get_trust_state` still reports `unknown` for the attested product unless the product itself was anchored or produced by a run.
2. `get_trust_state` deliberately searches by `sha256` and excludes attestation-kind anchors, so "has a PASS attestation" never looks like "is a promoted product".
3. When you pass `attestation_ref` to `graduate_product`, use the attestation's **own** sha256 (returned as `attestation_sha256` by `query_attestation`). It is recorded for audit only — the gate re-derives PASS-ness from the store and ignores it.

## Trust-state model

```python
from bathos.readback import get_trust_state
get_trust_state(catalog_dir, content_hash)  # -> "unknown" | "candidate" | "promoted"
```

- **`unknown`** — content_hash is None, or it was never anchored-by-own-identity AND never recorded as a run output. A PASS attestation alone leaves the product here.
- **`candidate`** — the hash IS an anchor's own identity (non-attestation kind) or IS some run's recorded output sha256, but has no promotion in the ledger.
- **`promoted`** — the append-only trust ledger has a promotion record (folded latest-wins by `amended_at`). Only `graduate_product` appends one.

List everything not yet promoted for a campaign:

```python
from bathos.readback import list_candidates
list_candidates(catalog_dir, campaign_id)  # [{content_hash, trust_state:"candidate", source:"anchor"|"run", ...}]
```

## Attestation TOML schema

File name convention: `*.attestation.bth.toml`, single `[attestation]` section.

```toml
[attestation]
kind      = "oracle_match"   # or "repro_floor"
verdict   = "PASS"           # the ACTUAL verdict: PASS | WARN | FAIL (never aspirational)
attested  = { run_id = "...", output_path = "...", content_hash = "..." }  # all three REQUIRED
# oracle_match requires ALL of:
oracle_sha256     = "..."    # sha256 of the independent oracle run/output
harness_run_ref   = "..."    # bathos run_id of the harness invocation
max_discrepancy   = 0.0
tolerance_policy  = "description of the tolerance policy applied"
# repro_floor requires ALL of:
seed_pin          = 12345
rerun_count       = 3
rerun_digests     = ["<content_hash>", "<content_hash>", "<content_hash>"]  # len == rerun_count, each == attested.content_hash
created_by        = "agent-or-user-name"    # warning if missing
created_at        = "2026-08-25T12:00:00+00:00"  # warning if missing
```

Strength ranking (`oracle_match` > `repro_floor`): an oracle match proves correctness against an independent oracle; a repro floor only proves seed-pinned determinism.

Registration rules:

- `register_attestation` validates at write time and REJECTS invalid files with `AttestationValidationFailed` (mapped to error code `sidecar_invalid`). Nothing is written on rejection.
- Registration writes a canonical copy to `<catalog_dir>/sidecars/attestations/<attestation_sha256>.attestation.bth.toml` and anchors via `DurableAnchorStore`, so evidence survives `compact(force_rebuild=True)`.
- WARN/FAIL attestations CAN be registered but `query_attestation` only ever returns verdict == "PASS".

## Warm-tier compact requirement

`output_metadata` (the per-run `{path, sha256, size_bytes, mtime_unix}` records) is populated ONLY at compact time — `write_run` stores runs with `output_metadata = "[]"`, and compact both ingests new runs and refreshes stored hashes (re-hashing only when mtime changed).

**Always run `compact` after tracking runs and BEFORE any readback/pin work**, or `resolve_pin` finds no recorded hash (`content_hash=None`, `fresh=False`) and run-output candidates are invisible:

```bash
bth compact            # CLI (add --force-rebuild only for corruption recovery)
```

## Full promote flow (copy-pasteable)

```python
from pathlib import Path

from bathos.attestation import register_attestation
from bathos.compact import compact
from bathos.readback import (
    get_trust_state,
    list_candidates,
    query_attestation,
    resolve_pin,
)
from bathos.trust_ledger import GraduationRefused, graduate_product

catalog_dir = Path.home() / ".bth" / "catalog"  # or BTH_CATALOG_DIR / .bth.toml [project].catalog_dir

# --- 1. Track the producing run -------------------------------------------
# Prefer the CLI so git/sidecar provenance is captured:
#   bth run --out outputs/model.json -- uv run python scripts/train.py
# Library equivalent (minimal fields shown):
#   from bathos.schema import Run
#   from bathos.catalog import init_catalog, write_run
#   init_catalog(catalog_dir)
#   run = Run(project_slug="myproject", command="scripts/train.py",
#             argv=["scripts/train.py"], git_hash="<sha>", git_branch="main",
#             git_dirty=False, status="complete", exit_code=0,
#             output_paths=["outputs/model.json"])
#   write_run(run, catalog_dir)

run_id = "<run-id-from-step-1>"

# --- 2. Compact: populates warm-tier output_metadata (REQUIRED) ------------
compact(catalog_dir)

# --- 3. Resolve the pin: get content_hash + freshness ----------------------
pin = resolve_pin(catalog_dir, run_id, "outputs/model.json")
print(pin.content_hash, pin.trust_state, pin.fresh)   # e.g. candidate True
assert pin.content_hash and pin.fresh, "file missing or drifted from recorded hash"
content_hash = pin.content_hash

# --- 4. Scaffold + fill + register the attestation -------------------------
#   bth attestation scaffold oracle_match --label model-v1
# Edit .bth/attestations/model-v1.attestation.bth.toml (set verdict + evidence fields)
register_attestation(Path(".bth/attestations/model-v1.attestation.bth.toml"), catalog_dir)

# --- 5. Confirm PASS evidence is queryable ---------------------------------
att = query_attestation(catalog_dir, content_hash, min_strength="oracle_match")
assert att is not None and att["verdict"] == "PASS"
print(get_trust_state(catalog_dir, content_hash))     # "candidate" (not yet promoted)

# --- 6. Graduate through the ratchet gate ----------------------------------
try:
    record = graduate_product(
        catalog_dir,
        content_hash,
        attestation_ref=att["attestation_sha256"],  # audit reference; NOT trusted as proof
        min_strength="oracle_match",                # or "repro_floor"; None = either qualifies
        run_id=run_id,
        output_path="outputs/model.json",
        reason="oracle harness pass, max_discrepancy=0.0",
    )
    print(record.from_state, "->", record.to_state)  # candidate -> promoted
except GraduationRefused as e:
    # No PASS attestation for content_hash at min_strength; NOTHING was appended.
    print("refused:", e)

# --- 7. Verify --------------------------------------------------------------
print(get_trust_state(catalog_dir, content_hash))    # "promoted"
print(list_candidates(catalog_dir, campaign_id))     # no longer lists this hash
```

Notes:

- `graduate_product` is idempotent: if the hash is already promoted it returns the EXISTING ledger record instead of appending a duplicate (the returned record reflects whichever call performed the promotion).
- The ratchet check re-queries the attestation store itself; a plausible-looking but unregistered `attestation_ref` cannot bypass it.
- The ledger is durable by construction: every append writes a cool-tier Parquet fragment plus a warm DuckDB row, and `compact(force_rebuild=True)` re-ingests fragments rather than losing history.

## Readback trio semantics

- **`resolve_pin(catalog_dir, run_id, output_path) -> ResolvedPin{content_hash, trust_state, fresh, live_content_hash}`** — raises `CatalogError` if the run doesn't exist. If `output_path` was never recorded for the run: `content_hash=None`, `trust_state="unknown"`, `fresh=False`.
- **`fresh` flag + DEBT-RF1 (recorded-vs-live gap):** `content_hash` returned is the RECORDED hash from warm-tier `output_metadata`; `fresh=True` means the LIVE on-disk file was re-hashed right now and matches that recorded value. Comparing two recorded values (e.g. payload.sha256 vs pin.content_hash) compares recorded-vs-recorded and can pass even after the file was edited on disk. **Any verification decision must also require `pin.fresh is True`** (tracked as DEBT-RF1 in affigit spec 260825_figure-component-hierarchy-and-pin-variants; ResolvedPin.live_content_hash was added 2026-08-26 (commit 2c40332d) so a not-fresh diagnostic can report the actual on-disk hash instead of re-quoting the recorded one — still check `pin.fresh` explicitly for the pass/fail decision itself, since a live hash alone doesn't tell you which value is "correct").
- **`get_trust_state(catalog_dir, content_hash)`** — pure fold, see trust-state model above.
- **`query_attestation(catalog_dir, content_hash, min_strength=None)`** — returns the strongest/most recent PASS attestation dict (`kind`, `attested`, `verdict`, kind-specific fields, `attestation_sha256`, `campaign_id`, `anchored_at`) or None. WARN/FAIL are never returned.

## Error codes

Mapped through `bathos.errors.EXCEPTION_TO_CODE` → MCP envelope `error_code`:

| Exception / code | Meaning | Fix |
|---|---|---|
| `GraduationRefused` → `graduation_refused` | Ratchet invariant unmet: no PASS attestation for the content_hash at `min_strength`; nothing appended | Register a valid PASS attestation first (`bth attestation register`) |
| `AttestationValidationFailed` → `sidecar_invalid` | Attestation failed validation at registration (missing evidence fields, digest mismatch); nothing written | Fix the TOML per the schema above; dry-run with `bth attestation validate` |
| `CatalogError` → `catalog_error` | e.g. resolve_pin on a nonexistent run_id | Verify run_id (`bth find`), compact if the run isn't visible |

MCP tools mirror every step: `compact`, `resolve_pin`, `get_trust_state`, `query_attestation`, `list_candidates`, `attestation_scaffold`, `attestation_validate`, `attestation_register`, `graduate_product` (envelope contract in **bathos-mcp**).

## CLI quick reference

```bash
bth compact                                              # populate/refresh warm-tier hashes FIRST
bth attestation scaffold oracle_match --label mylabel    # template in .bth/attestations/
bth attestation validate .bth/attestations/mylabel.attestation.bth.toml   # dry-run check
bth attestation register .bth/attestations/mylabel.attestation.bth.toml [--campaign-id ID]
bth query resolve-pin <run_id> <output_path>             # content_hash/trust_state/fresh JSON
bth query trust-state <content_hash>
bth query attestation <content_hash> [--min-strength oracle_match]
bth query candidates <campaign_id>
```

## Related

- **using-bathos** — daily-driver workflow: `bth run`, sidecars, outcomes, catalog tiers
- **bathos-campaigns** — campaigns, claim-tier pre-registration, figure manifests, lineage
- **bathos-mcp** — MCP error envelope and `BathosErrorCode` integration contract
