"""Attestation sidecar (S4; build seam S4, spec §3.2, backlog item 3492).

task_id: 260713_figure-eda-build-dag. Owner sign-off on gate #3488 (NO-GO on extending
bathos's claim system) is recorded at
`.praxia/docs/decisions/260714_spike-claim-schema-extensibility.md` (maraxiom repo):
bathos's claim-attestation surface (`bathos.claim.attest_parity`,
`parity_confound_check`, the graded branch of `validate_claim`) is hard-bound to the
literal string ``"literature_parity"`` at both the write and read/grading paths, and
that binding is asserted by an existing test as intended behavior — not a gap. Per
that verdict, this module is a **separate, analogous anchored sidecar kind**, not an
extension of `bathos.claim`. It reuses the claim system's *pattern*
(scaffold/register/validate triad + SHA256-anchor-at-registration) and the S2
anchor-insert seam (`bathos.anchor.register_anchor` / `find_anchors`), but shares no
code path, table, or column with `bathos.claim` — this module does not import
`bathos.claim` and never touches the `campaigns.claim_path` / `campaigns.claim_sha256`
columns. See `tests/test_attestation.py::TestDistinctFromClaimPath` for the structural
proof.

Schema (spec §3.2)
-------------------
An attestation is a TOML sidecar (``*.attestation.bth.toml``)::

    [attestation]
    kind      = "oracle_match" | "repro_floor"
    attested  = { run_id = "...", output_path = "...", content_hash = "..." }
    verdict   = "PASS" | "WARN" | "FAIL"          # the ACTUAL verdict, not aspirational
    # oracle_match only:
    oracle_sha256     = "..."
    harness_run_ref    = "..."
    max_discrepancy    = 0.0
    tolerance_policy    = "..."
    # repro_floor only:
    seed_pin        = 12345
    rerun_count      = 3
    rerun_digests     = ["...", "...", "..."]     # all must == attested.content_hash
    created_by = "..."
    created_at = "..."

Registration anchors the sidecar's own SHA256 (``attestation_sha256``) into the
generic S2 anchor store, with ``kind`` set to the attestation kind and
``content_hash`` set to the *attested product's* content_hash (``attested.content_hash``)
so the S1 read-back seam (`bathos.readback.query_attestation`) can find attestations
by the product they certify. This is the exact same (path, sha256) + content_hash
identity shape `bathos.anchor` already uses for figures (item 3483) — attestation is
just another `kind`, not a schema change to `AnchorRecord`.

Durability: registration defaults to `bathos.anchor.DurableAnchorStore` (not the
plain, non-durable `CatalogAnchorStore` that `bathos.anchor.get_anchor_store` returns
by default), because attestations back promotion decisions (spec §5.1) and must
survive a warm-cache `bathos.compact.compact(force_rebuild=True)`. The canonical copy
of the attestation TOML is also written under
``<catalog_dir>/sidecars/attestations/<attestation_sha256>.attestation.toml`` (mirroring
the existing `sidecars/<campaign_id>/campaign_report.json` convention) so that the full
attestation payload — not just the generic anchor fields — survives a rebuild too and
remains readable by `query_attestation` independent of wherever the caller's original
file lives.

IMPORTANT — inert evidence note (spec item 9 acceptance, backlog #3492): registering a
PASS attestation, by itself, promotes nothing. The trust ledger (build seam S3,
backlog #3491) is what turns a `query_attestation(...) -> PASS` result into a
`candidate -> promoted` transition (spec §5.1 step 4). If S3 does not exist yet (or a
given content_hash has no ledger entry), a PASS attestation recorded here is
observationally inert: `query_attestation` will return it, but nothing consumes that
return value to change `get_trust_state`'s answer. This module makes no promotion
claim — it is query-answerable evidence only.
"""

from __future__ import annotations

import hashlib
import tomllib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from bathos.anchor import AnchorRecord, AnchorStore, DurableAnchorStore, register_anchor
from bathos.telemetry import event

VALID_KINDS = ("oracle_match", "repro_floor")
VALID_VERDICTS = ("PASS", "WARN", "FAIL")

#: Certification strength ranking. Higher = stronger. `oracle_match` (verified against
#: an independent oracle) is strictly stronger evidence than `repro_floor` (proves only
#: seed-pinned determinism, not correctness) — spec §5.1 step 2 / §4.4.
STRENGTH_RANK: dict[str, int] = {
    "repro_floor": 1,
    "oracle_match": 2,
}

_ATTESTATIONS_DIRNAME = "attestations"


class AttestationValidationError:
    """Single attestation validation error. Deliberately NOT `bathos.claim.ValidationError`
    — this module shares no classes with `bathos.claim` (see module docstring)."""

    def __init__(self, message: str):
        self.message = message

    def __repr__(self):
        return f"AttestationValidationError({self.message!r})"


class AttestationValidationFailed(ValueError):
    """Raised by `register_attestation` (debt #638, escalates #629) when the
    attestation at `path` fails `validate_attestation` — e.g. an `oracle_match`
    attestation missing required evidence fields (`oracle_sha256`,
    `harness_run_ref`, `max_discrepancy`, `tolerance_policy`). No anchor is
    written and no ledger promotion can be backed by the rejected attestation:
    this is the fix for the promotion ratchet's evidence-free-attestation gap
    (spec §5.1 — "nothing reaches promoted without an evaluator PASS
    attestation" requires the attestation itself to be internally coherent,
    not just verdict == "PASS")."""


@dataclass
class AttestationValidationResult:
    """Result of `validate_attestation`. Deliberately NOT `bathos.claim.ValidationResult`."""

    ok: bool
    errors: list[AttestationValidationError] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class AttestationFile:
    """Parsed ``*.attestation.bth.toml`` file (spec §3.2)."""

    kind: str
    attested: dict  # {run_id, output_path, content_hash}
    verdict: str
    path: Path
    sha256: str
    # oracle_match fields
    oracle_sha256: str | None = None
    harness_run_ref: str | None = None
    max_discrepancy: float | None = None
    tolerance_policy: str | None = None
    # repro_floor fields
    seed_pin: int | None = None
    rerun_count: int | None = None
    rerun_digests: list[str] = field(default_factory=list)
    created_by: str | None = None
    created_at: str | None = None

    @property
    def content_hash(self) -> str | None:
        """The attested product's content_hash — the key `query_attestation` looks up by."""
        return self.attested.get("content_hash")


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def parse_attestation(path: Path) -> AttestationFile:
    """Parse a ``*.attestation.bth.toml`` file.

    Mirrors `bathos.claim.parse_claim`'s leniency: does not itself enforce required
    fields (that is `validate_attestation`'s job) beyond what's needed to construct the
    dataclass. Raises only on missing file / malformed TOML.

    Args:
        path: Path to the attestation TOML file.

    Returns:
        AttestationFile dataclass.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the file cannot be parsed as TOML.
    """
    if not path.exists():
        raise FileNotFoundError(f"Attestation file not found at {path}")

    try:
        content = path.read_bytes()
        data = tomllib.loads(content.decode("utf-8"))
    except Exception as e:
        raise ValueError(f"Failed to parse attestation TOML at {path}: {e}") from e

    section = data.get("attestation", {})
    sha256_hex = hashlib.sha256(content).hexdigest()

    return AttestationFile(
        kind=section.get("kind", ""),
        attested=section.get("attested", {}) or {},
        verdict=section.get("verdict", ""),
        oracle_sha256=section.get("oracle_sha256"),
        harness_run_ref=section.get("harness_run_ref"),
        max_discrepancy=section.get("max_discrepancy"),
        tolerance_policy=section.get("tolerance_policy"),
        seed_pin=section.get("seed_pin"),
        rerun_count=section.get("rerun_count"),
        rerun_digests=list(section.get("rerun_digests", []) or []),
        created_by=section.get("created_by"),
        created_at=section.get("created_at"),
        path=path,
        sha256=sha256_hex,
    )


def validate_attestation(attestation: AttestationFile) -> AttestationValidationResult:
    """Validate an attestation's kind-specific required fields and invariants.

    Args:
        attestation: Parsed AttestationFile.

    Returns:
        AttestationValidationResult with `ok` + any errors/warnings.
    """
    errors: list[AttestationValidationError] = []
    warnings: list[str] = []

    if attestation.kind not in VALID_KINDS:
        errors.append(
            AttestationValidationError(
                f"kind must be one of {VALID_KINDS}, got {attestation.kind!r}"
            )
        )

    if attestation.verdict not in VALID_VERDICTS:
        errors.append(
            AttestationValidationError(
                f"verdict must be one of {VALID_VERDICTS}, got {attestation.verdict!r}"
            )
        )

    attested = attestation.attested
    for required in ("run_id", "output_path", "content_hash"):
        if not attested.get(required):
            errors.append(AttestationValidationError(f"attested.{required} is required"))

    if attestation.kind == "oracle_match":
        if not attestation.oracle_sha256:
            errors.append(AttestationValidationError("oracle_match requires oracle_sha256"))
        if not attestation.harness_run_ref:
            errors.append(AttestationValidationError("oracle_match requires harness_run_ref"))
        if attestation.max_discrepancy is None:
            errors.append(AttestationValidationError("oracle_match requires max_discrepancy"))
        if not attestation.tolerance_policy:
            errors.append(AttestationValidationError("oracle_match requires tolerance_policy"))
    elif attestation.kind == "repro_floor":
        if attestation.seed_pin is None:
            errors.append(AttestationValidationError("repro_floor requires seed_pin"))
        if not attestation.rerun_count or attestation.rerun_count < 1:
            errors.append(AttestationValidationError("repro_floor requires rerun_count >= 1"))
        if not attestation.rerun_digests:
            errors.append(AttestationValidationError("repro_floor requires rerun_digests"))
        elif attestation.rerun_count and len(attestation.rerun_digests) != attestation.rerun_count:
            errors.append(
                AttestationValidationError(
                    f"rerun_digests has {len(attestation.rerun_digests)} entries, "
                    f"expected rerun_count={attestation.rerun_count}"
                )
            )
        content_hash = attested.get("content_hash")
        if content_hash and attestation.rerun_digests:
            mismatched = [d for d in attestation.rerun_digests if d != content_hash]
            if mismatched:
                errors.append(
                    AttestationValidationError(
                        "repro_floor proves determinism only if every rerun_digest == "
                        f"attested.content_hash ({content_hash!r}); found mismatched "
                        f"digest(s): {mismatched!r}"
                    )
                )

    if not attestation.created_by:
        warnings.append("created_by not set")
    if not attestation.created_at:
        warnings.append("created_at not set")

    return AttestationValidationResult(ok=not errors, errors=errors, warnings=warnings)


def scaffold_attestation(
    kind: str,
    workspace_root: Path,
    *,
    label: str | None = None,
) -> Path:
    """Create an attestation.bth.toml template.

    Args:
        kind: "oracle_match" or "repro_floor".
        workspace_root: Root of project workspace.
        label: Optional filename label (defaults to kind).

    Returns:
        Path to the created template file.

    Raises:
        ValueError: If kind is not a recognized attestation kind.
    """
    if kind not in VALID_KINDS:
        raise ValueError(f"kind must be one of {VALID_KINDS}, got {kind!r}")

    attestations_dir = workspace_root / ".bth" / "attestations"
    attestations_dir.mkdir(parents=True, exist_ok=True)

    if kind == "oracle_match":
        kind_fields = """oracle_sha256 = "REQUIRED: sha256 of the oracle run/output"
harness_run_ref = "REQUIRED: bathos run_id of the Flow P harness invocation"
max_discrepancy = 0.0
tolerance_policy = "REQUIRED: description of the tolerance policy applied"
"""
    else:
        kind_fields = """seed_pin = 0
rerun_count = 3
rerun_digests = []  # EDIT: N digests, all == attested.content_hash
"""

    template = f"""# Attestation ({kind})
# Generated via bth attestation scaffold

[attestation]
kind = "{kind}"
verdict = "REQUIRED: PASS | WARN | FAIL"
attested = {{ run_id = "REQUIRED: run_id of the attested product", output_path = "REQUIRED: output_path of the attested product", content_hash = "REQUIRED: content_hash of the attested product" }}
{kind_fields}created_by = "REQUIRED: attesting agent/user"
created_at = "{_now_iso()}"
"""

    slug = label or kind
    attestation_path = attestations_dir / f"{slug}.attestation.bth.toml"
    attestation_path.write_text(template)

    event("attestation.scaffold", kind=kind, path=str(attestation_path))

    return attestation_path


def _canonical_attestation_path(catalog_dir: Path | str, attestation_sha256: str) -> Path:
    return (
        Path(catalog_dir)
        / "sidecars"
        / _ATTESTATIONS_DIRNAME
        / f"{attestation_sha256}.attestation.bth.toml"
    )


def register_attestation(
    path: Path,
    catalog_dir: Path | str,
    *,
    campaign_id: str | None = None,
    store: AnchorStore | None = None,
) -> AnchorRecord:
    """Register an attestation file: anchor it by its own SHA256 (durable by default).

    Parses `path` (raising if missing/malformed — see `parse_attestation`), copies its
    content into the durable canonical location
    `<catalog_dir>/sidecars/attestations/<attestation_sha256>.attestation.bth.toml`
    (so it survives independent of the caller's original file lifetime), and anchors
    that canonical copy via `bathos.anchor.register_anchor` with:

    - `kind` = the attestation's own kind (`oracle_match` / `repro_floor`)
    - `sha256` = the attestation file's own SHA256 (`attestation_sha256`)
    - `content_hash` = the *attested product's* content_hash (`attested.content_hash`)
    - `label` = the attestation's verdict (PASS/WARN/FAIL) — a legitimate short
      human-readable label, and how `query_attestation` cheaply pre-filters before
      reading the full canonical file back.

    Enforces validity at registration time (debt #638, escalates #629; fixes a
    confirmed promotion-ratchet defeat found by adversarial audit 260714):
    internally calls `validate_attestation` and REJECTS — raising
    `AttestationValidationFailed`, writing nothing — an attestation that fails
    validation (e.g. an `oracle_match` attestation missing required evidence
    fields). This used to be a separate, optional step (mirroring
    `bathos.claim.register_claim`'s registration/validation split), but nothing in
    the read/promote path (`bathos.readback.query_attestation`,
    `bathos.trust_ledger.graduate_product`) re-validates on the way out — both
    trust `register_attestation` to have anchored only internally-coherent
    evidence. A well-behaved caller could always call `validate_attestation`
    first, but an evidence-free or malformed attestation registered by a
    careless or adversarial caller was silently anchored and then accepted by
    `query_attestation`/`graduate_product` to promote a fabricated content_hash.
    So `register_attestation` now enforces the invariant itself rather than
    assuming a separate validate step ran. The `bth attestation scaffold` /
    `bth attestation validate` tools remain useful for iterating on a draft
    attestation *before* registering it — `attestation_validate_tool` is the
    dry-run check; this function is the enforced gate at the actual write seam.

    Note (debt #619, tracked separately, NOT fixed here): this only checks that
    the attestation is internally coherent (real evidence fields, correct
    shape) — it does not authenticate WHO is allowed to call this function or
    the `attestation_register_tool` MCP wrapper. A caller could still construct
    a fully-valid-*shaped* attestation for a run they don't actually control.

    Args:
        path: Path to the attestation TOML file to register.
        catalog_dir: Path to the bathos catalog root.
        campaign_id: Optional campaign this attestation belongs to.
        store: Explicit AnchorStore override (defaults to
            `bathos.anchor.DurableAnchorStore(catalog_dir)` — NOT the plain
            non-durable default `bathos.anchor.get_anchor_store` uses for generic
            anchors, because attestations must survive `compact(force_rebuild=True)`
            to back promotion decisions).

    Returns:
        The inserted AnchorRecord (its `.path` is the canonical copy, not the
        caller's original `path`).

    Raises:
        FileNotFoundError: If `path` does not exist.
        ValueError: If `path` cannot be parsed as TOML.
        AttestationValidationFailed: If the attestation fails
            `validate_attestation` (subclass of ValueError). No anchor is
            written and no canonical copy is created.
    """
    attestation = parse_attestation(path)

    validation = validate_attestation(attestation)
    if not validation.ok:
        messages = "; ".join(e.message for e in validation.errors)
        raise AttestationValidationFailed(
            f"Attestation at {path} failed validation and was NOT registered: "
            f"{messages}"
        )

    canonical_path = _canonical_attestation_path(catalog_dir, attestation.sha256)
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    canonical_path.write_bytes(path.read_bytes())

    active_store = store if store is not None else DurableAnchorStore(catalog_dir)

    record = register_anchor(
        catalog_dir,
        str(canonical_path),
        attestation.sha256,
        attestation.kind,
        label=attestation.verdict,
        content_hash=attestation.content_hash,
        campaign_id=campaign_id,
        store=active_store,
    )

    event(
        "attestation.register",
        kind=attestation.kind,
        verdict=attestation.verdict,
        attestation_sha256=attestation.sha256,
        content_hash=attestation.content_hash,
        campaign_id=campaign_id,
    )

    return record
