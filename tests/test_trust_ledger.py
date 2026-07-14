"""Durable trust ledger tests (S3, spec §3.1/§5.1, backlog item 3491).

task_id: 260713_figure-eda-build-dag. This is the promotion state-of-record: an
append-only ledger of `candidate -> promoted` transitions, durable on the same
proven substrate as anchors (cool-tier Parquet fragment + compact-time re-ingest,
mirroring `bathos.anchor`'s `write_anchor_fragment` / `read_anchor_fragments` /
`_ingest_anchor_fragments`, merged in #4).

`graduate_product` is the ONLY candidate->promoted path, and it enforces the
ratchet invariant: it independently calls the merged `bathos.readback.query_attestation`
(S4, #3492) and refuses to append a promotion record unless a PASS attestation
exists for the content_hash. Nothing reaches `promoted` without an evaluator PASS
attestation (spec §5.1 step 4).

Test layout:

- TestAppendAndFoldLatestWins: append+supersede a promotion record; trust_state
  folds latest-wins by amended_at.
- TestGraduateProductRatchetInvariant: graduate_product REFUSES to promote
  without a PASS attestation (WARN/FAIL/absent all refused); succeeds with PASS.
- TestGraduationSurvivesForceRebuild: durability, templated from
  tests/test_anchor_durability.py — a graduation record written through the
  durable substrate survives `compact(catalog_dir, force_rebuild=True)` and
  remains queryable.
"""

from __future__ import annotations

import pytest

from bathos.attestation import register_attestation
from bathos.compact import compact as compact_catalog
from bathos.trust_ledger import (
    GraduationRefused,
    TrustLedgerRecord,
    append_ledger_record,
    fold_trust_state,
    graduate_product,
)

ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "{verdict}"
attested = {{ run_id = "run-001", output_path = "out/result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "run-harness-001"
max_discrepancy = 0.001
tolerance_policy = "abs<=1e-3"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


def _register_attestation(tmp_path, catalog_dir, content_hash, verdict, name, oracle_sha="f" * 64):
    path = tmp_path / name
    path.write_text(
        ORACLE_MATCH_TOML.format(
            content_hash=content_hash, verdict=verdict, oracle_sha=oracle_sha
        )
    )
    return register_attestation(path, catalog_dir)


class TestAppendAndFoldLatestWins:
    """Append-only ledger; trust_state for a content_hash folds latest-wins by
    amended_at. Re-graduating (supersede) the same content_hash with an updated
    attestation_ref/reason must resolve to the newest record, not the first."""

    def test_fold_returns_none_when_never_graduated(self, catalog_dir):
        assert fold_trust_state(catalog_dir, "a" * 64) is None

    def test_single_append_is_visible_via_fold(self, catalog_dir):
        record = TrustLedgerRecord(
            content_hash="b" * 64,
            from_state="candidate",
            to_state="promoted",
            attestation_ref="attn-1",
        )
        append_ledger_record(record, catalog_dir)

        assert fold_trust_state(catalog_dir, "b" * 64) == "promoted"

    def test_supersede_resolves_to_latest_record(self, catalog_dir):
        content_hash = "c" * 64
        first = TrustLedgerRecord(
            content_hash=content_hash,
            from_state="candidate",
            to_state="promoted",
            attestation_ref="attn-first",
            reason="initial graduation",
            amended_at="2026-07-14T00:00:00+00:00",
        )
        second = TrustLedgerRecord(
            content_hash=content_hash,
            from_state="candidate",
            to_state="promoted",
            attestation_ref="attn-second",
            reason="re-graduated with stronger attestation",
            amended_at="2026-07-14T01:00:00+00:00",
        )
        append_ledger_record(first, catalog_dir)
        append_ledger_record(second, catalog_dir)

        assert fold_trust_state(catalog_dir, content_hash) == "promoted"

        from bathos.trust_ledger import latest_ledger_record

        latest = latest_ledger_record(catalog_dir, content_hash)
        assert latest is not None
        assert latest.attestation_ref == "attn-second"
        assert latest.reason == "re-graduated with stronger attestation"

    def test_ledger_is_append_only_both_records_present(self, catalog_dir):
        from bathos.trust_ledger import read_ledger_fragments

        content_hash = "d" * 64
        append_ledger_record(
            TrustLedgerRecord(
                content_hash=content_hash, from_state="candidate", to_state="promoted",
                attestation_ref="attn-1", amended_at="2026-07-14T00:00:00+00:00",
            ),
            catalog_dir,
        )
        append_ledger_record(
            TrustLedgerRecord(
                content_hash=content_hash, from_state="candidate", to_state="promoted",
                attestation_ref="attn-2", amended_at="2026-07-14T01:00:00+00:00",
            ),
            catalog_dir,
        )

        all_records = read_ledger_fragments(catalog_dir)
        matching = [r for r in all_records if r.content_hash == content_hash]
        assert len(matching) == 2, "append-only: both records must survive, neither overwritten"


class TestGraduateProductRatchetInvariant:
    """graduate_product is the ONLY candidate->promoted path and MUST refuse to
    promote unless a PASS attestation exists for the content_hash (spec §5.1
    step 4). WARN, FAIL, and absent attestations are all refused identically."""

    def test_refuses_when_no_attestation_registered(self, catalog_dir):
        content_hash = "1" * 64

        with pytest.raises(GraduationRefused):
            graduate_product(catalog_dir, content_hash, "nonexistent-attn-ref")

        assert fold_trust_state(catalog_dir, content_hash) is None

    def test_refuses_when_only_warn_attestation_exists(self, tmp_path, catalog_dir):
        content_hash = "2" * 64
        _register_attestation(tmp_path, catalog_dir, content_hash, "WARN", "warn.attestation.bth.toml")

        with pytest.raises(GraduationRefused):
            graduate_product(catalog_dir, content_hash, "warn-ref")

        assert fold_trust_state(catalog_dir, content_hash) is None

    def test_refuses_when_only_fail_attestation_exists(self, tmp_path, catalog_dir):
        content_hash = "3" * 64
        _register_attestation(tmp_path, catalog_dir, content_hash, "FAIL", "fail.attestation.bth.toml")

        with pytest.raises(GraduationRefused):
            graduate_product(catalog_dir, content_hash, "fail-ref")

        assert fold_trust_state(catalog_dir, content_hash) is None

    def test_succeeds_when_pass_attestation_exists(self, tmp_path, catalog_dir):
        content_hash = "4" * 64
        _register_attestation(tmp_path, catalog_dir, content_hash, "PASS", "pass.attestation.bth.toml")

        record = graduate_product(catalog_dir, content_hash, "pass-ref")

        assert record.to_state == "promoted"
        assert record.from_state == "candidate"
        assert fold_trust_state(catalog_dir, content_hash) == "promoted"

    def test_min_strength_still_refuses_repro_floor_below_oracle_match(self, tmp_path, catalog_dir):
        """A repro_floor PASS does not satisfy an oracle_match minimum — same
        ratchet gate query_attestation already enforces (S4), composed here."""
        from bathos.attestation import register_attestation as _reg

        content_hash = "5" * 64
        repro_toml = tmp_path / "repro.attestation.bth.toml"
        repro_toml.write_text(f"""
[attestation]
kind = "repro_floor"
verdict = "PASS"
attested = {{ run_id = "run-r", output_path = "out/x.zarr", content_hash = "{content_hash}" }}
seed_pin = 1
rerun_count = 2
rerun_digests = ["{content_hash}", "{content_hash}"]
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
""")
        _reg(repro_toml, catalog_dir)

        with pytest.raises(GraduationRefused):
            graduate_product(catalog_dir, content_hash, "ref", min_strength="oracle_match")

        assert fold_trust_state(catalog_dir, content_hash) is None
        # But it does satisfy no minimum / the weaker minimum.
        record = graduate_product(catalog_dir, content_hash, "ref", min_strength="repro_floor")
        assert record.to_state == "promoted"

    def test_graduating_an_already_promoted_product_twice_is_idempotent(
        self, tmp_path, catalog_dir
    ):
        """Debt #640 regression: calling graduate_product twice for the same
        content_hash used to append TWO distinct ledger records (a fresh UUID each
        time, no existing-promotion check) — wasteful, a data-hygiene smell (not a
        correctness bug per se, since fold_trust_state still resolves correctly
        via latest-wins either way). graduate_product must now no-op on the second
        call: no second record appended, and it returns successfully (the existing
        promotion record) rather than erroring or silently duplicating."""
        from bathos.trust_ledger import read_ledger_fragments

        content_hash = "a" * 64
        _register_attestation(tmp_path, catalog_dir, content_hash, "PASS", "idempotent.attestation.bth.toml")

        first = graduate_product(catalog_dir, content_hash, "first-ref")
        second = graduate_product(catalog_dir, content_hash, "second-ref")

        assert second.to_state == "promoted"
        assert second.id == first.id, (
            "second call must return the EXISTING promotion record, not mint a new one"
        )

        all_records = read_ledger_fragments(catalog_dir)
        matching = [r for r in all_records if r.content_hash == content_hash]
        assert len(matching) == 1, (
            "graduate_product must not append a second ledger record for an "
            "already-promoted content_hash"
        )
        assert fold_trust_state(catalog_dir, content_hash) == "promoted"


class TestGraduationSurvivesForceRebuild:
    """Durability proof, templated from
    tests/test_anchor_durability.py::TestDurableAnchorSurvivesForceRebuild. A
    graduation record must survive `compact(catalog_dir, force_rebuild=True)` and
    remain queryable via fold_trust_state (and, by composition, get_trust_state)."""

    def test_graduation_survives_force_rebuild(self, tmp_path, catalog_dir):
        content_hash = "6" * 64
        _register_attestation(tmp_path, catalog_dir, content_hash, "PASS", "durable.attestation.bth.toml")
        graduate_product(catalog_dir, content_hash, "durable-ref")

        assert fold_trust_state(catalog_dir, content_hash) == "promoted"

        result = compact_catalog(catalog_dir, force_rebuild=True)
        assert result is not None  # compact ran to completion, did not raise

        assert fold_trust_state(catalog_dir, content_hash) == "promoted"

    def test_graduation_survives_force_rebuild_via_get_trust_state(self, tmp_path, catalog_dir):
        """Queryable via the S1 read-back API specifically (get_trust_state), not
        just the low-level ledger fold — proves the full composition survives."""
        from bathos.readback import ProductTrustState, get_trust_state

        content_hash = "7" * 64
        _register_attestation(tmp_path, catalog_dir, content_hash, "PASS", "durable2.attestation.bth.toml")
        graduate_product(catalog_dir, content_hash, "durable-ref-2")

        compact_catalog(catalog_dir, force_rebuild=True)

        assert get_trust_state(catalog_dir, content_hash) == ProductTrustState.PROMOTED

    def test_multiple_graduations_and_multiple_rebuilds_all_survive(self, tmp_path, catalog_dir):
        h1, h2 = "8" * 64, "9" * 64
        _register_attestation(tmp_path, catalog_dir, h1, "PASS", "m1.attestation.bth.toml")
        graduate_product(catalog_dir, h1, "ref-1")
        compact_catalog(catalog_dir, force_rebuild=True)

        _register_attestation(tmp_path, catalog_dir, h2, "PASS", "m2.attestation.bth.toml")
        graduate_product(catalog_dir, h2, "ref-2")
        compact_catalog(catalog_dir, force_rebuild=True)

        assert fold_trust_state(catalog_dir, h1) == "promoted"
        assert fold_trust_state(catalog_dir, h2) == "promoted"
