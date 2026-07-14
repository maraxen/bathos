"""Tests for the S4 attestation sidecar (bathos.attestation), backlog item 3492.

task_id: 260713_figure-eda-build-dag. Built per owner sign-off on gate #3488
(`.praxia/docs/decisions/260714_spike-claim-schema-extensibility.md`, maraxiom repo):
a SEPARATE, analogous anchored sidecar kind, NOT an extension of bathos.claim's
literature_parity attestation path.

Covers: parse/scaffold/validate/register, kind-specific required fields, the
repro_floor determinism invariant (all rerun_digests == attested.content_hash), and a
structural proof that this module shares no code path with bathos.claim.
"""

from __future__ import annotations

import pytest

from bathos.anchor import CatalogAnchorStore, get_anchor
from bathos.attestation import (
    STRENGTH_RANK,
    VALID_KINDS,
    VALID_VERDICTS,
    parse_attestation,
    register_attestation,
    scaffold_attestation,
    validate_attestation,
)


@pytest.fixture
def catalog_dir(tmp_path):
    cat = tmp_path / "catalog"
    cat.mkdir(parents=True)
    return cat


def _write_attestation(tmp_path, content: str, name: str = "attest.attestation.bth.toml"):
    path = tmp_path / name
    path.write_text(content)
    return path


ORACLE_MATCH_TOML = """
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = {{ run_id = "run-001", output_path = "out/result.zarr", content_hash = "{content_hash}" }}
oracle_sha256 = "{oracle_sha}"
harness_run_ref = "run-harness-001"
max_discrepancy = 0.001
tolerance_policy = "abs<=1e-3"
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""

REPRO_FLOOR_TOML = """
[attestation]
kind = "repro_floor"
verdict = "PASS"
attested = {{ run_id = "run-002", output_path = "out/result2.zarr", content_hash = "{content_hash}" }}
seed_pin = 42
rerun_count = 3
rerun_digests = ["{content_hash}", "{content_hash}", "{content_hash}"]
created_by = "test-suite"
created_at = "2026-07-14T00:00:00Z"
"""


class TestParseAttestation:
    def test_parse_oracle_match(self, tmp_path):
        content_hash = "c" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="o" * 64)
        path = _write_attestation(tmp_path, toml_text)

        attestation = parse_attestation(path)

        assert attestation.kind == "oracle_match"
        assert attestation.verdict == "PASS"
        assert attestation.attested["run_id"] == "run-001"
        assert attestation.attested["content_hash"] == content_hash
        assert attestation.content_hash == content_hash
        assert attestation.oracle_sha256 == "o" * 64
        assert attestation.max_discrepancy == 0.001
        assert attestation.sha256  # computed, non-empty

    def test_parse_repro_floor(self, tmp_path):
        content_hash = "d" * 64
        toml_text = REPRO_FLOOR_TOML.format(content_hash=content_hash)
        path = _write_attestation(tmp_path, toml_text)

        attestation = parse_attestation(path)

        assert attestation.kind == "repro_floor"
        assert attestation.seed_pin == 42
        assert attestation.rerun_count == 3
        assert attestation.rerun_digests == [content_hash] * 3

    def test_parse_missing_file_raises(self, tmp_path):
        with pytest.raises(FileNotFoundError):
            parse_attestation(tmp_path / "nope.toml")

    def test_parse_malformed_toml_raises(self, tmp_path):
        path = tmp_path / "bad.toml"
        path.write_text("not [ valid toml")
        with pytest.raises(ValueError):
            parse_attestation(path)

    def test_sha256_matches_actual_file_content(self, tmp_path):
        import hashlib

        content_hash = "e" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="f" * 64)
        path = _write_attestation(tmp_path, toml_text)

        attestation = parse_attestation(path)

        assert attestation.sha256 == hashlib.sha256(path.read_bytes()).hexdigest()


class TestScaffoldAttestation:
    def test_scaffold_oracle_match(self, tmp_path):
        path = scaffold_attestation("oracle_match", tmp_path)
        assert path.exists()
        assert path.parent == tmp_path / ".bth" / "attestations"
        text = path.read_text()
        assert 'kind = "oracle_match"' in text
        assert "oracle_sha256" in text

    def test_scaffold_repro_floor(self, tmp_path):
        path = scaffold_attestation("repro_floor", tmp_path, label="my-repro")
        assert path.name == "my-repro.attestation.bth.toml"
        text = path.read_text()
        assert 'kind = "repro_floor"' in text
        assert "rerun_digests" in text

    def test_scaffold_rejects_unknown_kind(self, tmp_path):
        with pytest.raises(ValueError, match="kind"):
            scaffold_attestation("bogus_kind", tmp_path)

    def test_scaffold_output_is_parseable_even_with_placeholders(self, tmp_path):
        # Scaffold output alone won't validate (placeholders), but must at least parse.
        path = scaffold_attestation("oracle_match", tmp_path)
        attestation = parse_attestation(path)
        assert attestation.kind == "oracle_match"


class TestValidateAttestation:
    def test_valid_oracle_match_passes(self, tmp_path):
        toml_text = ORACLE_MATCH_TOML.format(content_hash="a" * 64, oracle_sha="b" * 64)
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert result.ok, [e.message for e in result.errors]

    def test_valid_repro_floor_passes(self, tmp_path):
        toml_text = REPRO_FLOOR_TOML.format(content_hash="a" * 64)
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert result.ok, [e.message for e in result.errors]

    def test_unknown_kind_fails(self, tmp_path):
        toml_text = ORACLE_MATCH_TOML.format(content_hash="a" * 64, oracle_sha="b" * 64).replace(
            'kind = "oracle_match"', 'kind = "bogus"'
        )
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert not result.ok
        assert any("kind" in e.message for e in result.errors)

    def test_bad_verdict_fails(self, tmp_path):
        toml_text = ORACLE_MATCH_TOML.format(content_hash="a" * 64, oracle_sha="b" * 64).replace(
            'verdict = "PASS"', 'verdict = "MAYBE"'
        )
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert not result.ok
        assert any("verdict" in e.message for e in result.errors)

    def test_oracle_match_missing_required_field_fails(self, tmp_path):
        toml_text = """
[attestation]
kind = "oracle_match"
verdict = "PASS"
[attestation.attested]
run_id = "r1"
output_path = "o.zarr"
content_hash = "aaaa"
created_by = "x"
created_at = "2026-01-01"
"""
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert not result.ok
        messages = " ".join(e.message for e in result.errors)
        assert "oracle_sha256" in messages
        assert "harness_run_ref" in messages
        assert "max_discrepancy" in messages
        assert "tolerance_policy" in messages

    def test_repro_floor_mismatched_digest_fails(self, tmp_path):
        """The core repro_floor invariant: every rerun_digest must == attested.content_hash."""
        content_hash = "a" * 64
        toml_text = REPRO_FLOOR_TOML.format(content_hash=content_hash).replace(
            f'"{content_hash}", "{content_hash}", "{content_hash}"',
            f'"{content_hash}", "{content_hash}", "{"z" * 64}"',
        )
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert not result.ok
        assert any("determinism" in e.message or "mismatched" in e.message for e in result.errors)

    def test_repro_floor_digest_count_mismatch_fails(self, tmp_path):
        content_hash = "a" * 64
        toml_text = REPRO_FLOOR_TOML.format(content_hash=content_hash).replace(
            "rerun_count = 3", "rerun_count = 5"
        )
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert not result.ok

    def test_missing_attested_fields_fail(self, tmp_path):
        toml_text = """
[attestation]
kind = "oracle_match"
verdict = "PASS"
attested = { run_id = "", output_path = "", content_hash = "" }
oracle_sha256 = "x"
harness_run_ref = "y"
max_discrepancy = 0.0
tolerance_policy = "z"
"""
        path = _write_attestation(tmp_path, toml_text)
        result = validate_attestation(parse_attestation(path))
        assert not result.ok
        messages = " ".join(e.message for e in result.errors)
        assert "attested.run_id" in messages
        assert "attested.output_path" in messages
        assert "attested.content_hash" in messages


class TestRegisterAttestation:
    def test_register_oracle_match_anchors_by_own_sha256(self, tmp_path, catalog_dir):
        content_hash = "a" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="b" * 64)
        path = _write_attestation(tmp_path, toml_text)
        attestation = parse_attestation(path)

        record = register_attestation(path, catalog_dir)

        assert record.kind == "oracle_match"
        assert record.sha256 == attestation.sha256
        assert record.content_hash == content_hash
        assert record.label == "PASS"

    def test_register_repro_floor_anchors_by_own_sha256(self, tmp_path, catalog_dir):
        content_hash = "b" * 64
        toml_text = REPRO_FLOOR_TOML.format(content_hash=content_hash)
        path = _write_attestation(tmp_path, toml_text)

        record = register_attestation(path, catalog_dir)

        assert record.kind == "repro_floor"
        assert record.content_hash == content_hash

    def test_register_records_actual_verdict_not_always_pass(self, tmp_path, catalog_dir):
        content_hash = "c" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="d" * 64).replace(
            'verdict = "PASS"', 'verdict = "FAIL"'
        )
        path = _write_attestation(tmp_path, toml_text)

        record = register_attestation(path, catalog_dir)

        assert record.label == "FAIL"

    def test_register_uses_durable_store_by_default(self, tmp_path, catalog_dir):
        """Default store must be DurableAnchorStore, not the plain non-durable
        CatalogAnchorStore, because attestations back promotion decisions and must
        survive compact(force_rebuild=True)."""
        content_hash = "e" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="f" * 64)
        path = _write_attestation(tmp_path, toml_text)

        register_attestation(path, catalog_dir)

        frag_dir = catalog_dir / "anchors"
        assert frag_dir.exists()
        assert list(frag_dir.glob("anchor_*.parquet"))

    def test_register_writes_canonical_durable_copy_under_catalog_dir(self, tmp_path, catalog_dir):
        content_hash = "1" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="2" * 64)
        path = _write_attestation(tmp_path, toml_text)

        record = register_attestation(path, catalog_dir)

        canonical = catalog_dir / "sidecars" / "attestations" / f"{record.sha256}.attestation.bth.toml"
        assert canonical.exists()
        assert record.path == str(canonical)

    def test_register_respects_explicit_store_override(self, tmp_path, catalog_dir):
        content_hash = "3" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="4" * 64)
        path = _write_attestation(tmp_path, toml_text)

        plain_store = CatalogAnchorStore(catalog_dir)
        record = register_attestation(path, catalog_dir, store=plain_store)

        # No cool-tier fragment written when a non-durable store is explicitly forced.
        frag_dir = catalog_dir / "anchors"
        assert not frag_dir.exists() or not list(frag_dir.glob("anchor_*.parquet"))
        assert get_anchor(catalog_dir, record.path, record.sha256, store=plain_store) is not None

    def test_register_missing_file_raises(self, tmp_path, catalog_dir):
        with pytest.raises(FileNotFoundError):
            register_attestation(tmp_path / "nope.toml", catalog_dir)

    def test_register_with_campaign_id(self, tmp_path, catalog_dir):
        content_hash = "5" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="6" * 64)
        path = _write_attestation(tmp_path, toml_text)

        record = register_attestation(path, catalog_dir, campaign_id="camp-xyz")

        assert record.campaign_id == "camp-xyz"


class TestStrengthRank:
    def test_oracle_match_stronger_than_repro_floor(self):
        assert STRENGTH_RANK["oracle_match"] > STRENGTH_RANK["repro_floor"]

    def test_valid_kinds_and_verdicts(self):
        assert set(VALID_KINDS) == {"oracle_match", "repro_floor"}
        assert set(VALID_VERDICTS) == {"PASS", "WARN", "FAIL"}


class TestDistinctFromClaimPath:
    """Structural proof this is a SEPARATE kind, not a bathos.claim extension —
    matches gate #3488's NO-GO recommendation
    (.praxia/docs/decisions/260714_spike-claim-schema-extensibility.md)."""

    def test_attestation_module_does_not_import_claim(self):
        """AST-based (not substring) check: the module's docstring legitimately
        *discusses* bathos.claim/literature_parity in prose to explain the
        separation — a naive substring check would false-positive on that
        documentation. What must actually be true is that no import statement in the
        module targets bathos.claim."""
        import ast
        import inspect

        import bathos.attestation as attestation_module

        source = inspect.getsource(attestation_module)
        tree = ast.parse(source)

        imported_modules: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported_modules.add(node.module)

        assert not any(m == "bathos.claim" or m.startswith("bathos.claim.") for m in imported_modules)
        assert "claim" not in imported_modules  # no "from bathos import claim" either

    def test_attestation_module_never_compares_against_literature_parity_string(self):
        """The claim system's hard-bind is a literal string-equality check against
        "literature_parity" (gate #3488 finding #2). This module must contain no such
        comparison — i.e. no string literal "literature_parity" appears anywhere in
        actual code (as opposed to the module docstring's prose, which legitimately
        names it while explaining the separation)."""
        import ast
        import inspect

        import bathos.attestation as attestation_module

        source = inspect.getsource(attestation_module)
        tree = ast.parse(source)
        docstring = ast.get_docstring(tree, clean=False) or ""

        # Strip the module docstring (its own AST node) before scanning remaining
        # string constants for the literal.
        code_without_docstring = source.replace(docstring, "", 1)
        assert "literature_parity" not in code_without_docstring

    def test_register_attestation_does_not_touch_campaigns_table(self, tmp_path, catalog_dir):
        """Registering an attestation must not write to campaigns.claim_path /
        campaigns.claim_sha256 — it goes through the generic sidecar_anchors table
        only (bathos.anchor), never bathos.claim.register_claim's campaigns UPDATE."""
        import duckdb

        content_hash = "7" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="8" * 64)
        path = _write_attestation(tmp_path, toml_text)

        register_attestation(path, catalog_dir)

        db_path = catalog_dir / "bathos.db"
        assert db_path.exists()
        con = duckdb.connect(str(db_path), read_only=True)
        try:
            tables = {row[0] for row in con.execute("SHOW TABLES").fetchall()}
            if "campaigns" in tables:
                rows = con.execute(
                    "SELECT claim_path, claim_sha256 FROM campaigns"
                ).fetchall()
                assert all(r[0] is None and r[1] is None for r in rows)
            # The attestation must be visible in sidecar_anchors, not campaigns.
            anchored = con.execute(
                "SELECT kind, content_hash FROM sidecar_anchors WHERE content_hash = ?",
                [content_hash],
            ).fetchall()
            assert len(anchored) == 1
            assert anchored[0][0] == "oracle_match"
        finally:
            con.close()

    def test_new_kind_rejected_by_literature_parity_attest_parity_would_be_accepted_here(
        self, tmp_path, catalog_dir
    ):
        """Sanity anchor for the NO-GO finding itself: a bogus non-literature_parity
        kind that bathos.claim.attest_parity would hard-reject registers fine through
        this separate seam (proving the two paths really are decoupled, not merely
        undocumented as such)."""
        content_hash = "9" * 64
        toml_text = ORACLE_MATCH_TOML.format(content_hash=content_hash, oracle_sha="0" * 64)
        path = _write_attestation(tmp_path, toml_text)

        # Would raise ValueError("...expected 'literature_parity'") if routed through
        # bathos.claim.attest_parity; register_attestation has no such gate at all.
        record = register_attestation(path, catalog_dir)
        assert record.kind == "oracle_match"
