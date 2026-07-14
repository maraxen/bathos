"""Tests for the S6 harness-as-bathos-run wrapper (bathos.harness_run).

Backlog #3493 (task 260713_figure-eda-build-dag), design locked by gate #3489
(NO-GO on retargeting to empirical_oracle; owner sign-off in
decisions/260714_spike-harness-supportability.md — S6 targets xtrax's port/
T1-T5 harness, a real numeric-tolerance parity oracle).

``run_harness_as_bathos_run`` wraps an invocation of that harness as a
bathos-tracked subprocess run (via the existing bathos.runner.run_script
"add-data verb runs a subprocess" path) and produces a verdict sidecar
anchored via the S2 anchor-insert seam (bathos.anchor.register_anchor) so a
future S4 attestation step (#3492, not yet built — see mcp.py's
query_attestation_tool NULL-STUB) has a stable harness_run_ref to look up:
the sidecar's own (path, sha256) identity, discoverable via
find_anchors(catalog_dir, kind="harness_run").

These tests use small inline-script "fake harnesses" that write the exact
JSONL shape xtrax's port/tests/conftest.py produces into
<xtrax_root>/.praxia/audits.jsonl, standing in for a real xtrax checkout
(xtrax/jax are not bathos dependencies) — this is the harness-adapter's own
audits.jsonl diffing contract exercised directly in test_port_parity_adapter.py;
here we exercise the layer above it (bathos.runner integration + sidecar +
anchor).
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from bathos.anchor import find_anchors, get_anchor
from bathos.harness_run import run_harness_as_bathos_run

PASS_SCRIPT = (
    "import json, pathlib\n"
    "p = pathlib.Path('.praxia/audits.jsonl')\n"
    "p.parent.mkdir(parents=True, exist_ok=True)\n"
    "with p.open('a') as fh:\n"
    "    fh.write(json.dumps({'domain': 'port', 'tier_verdict': "
    "{'status': 'PASS', 'tolerance_policy': 'rtol=1e-4'}}) + chr(10))\n"
)

FAIL_SCRIPT = (
    "import json, pathlib, sys\n"
    "p = pathlib.Path('.praxia/audits.jsonl')\n"
    "p.parent.mkdir(parents=True, exist_ok=True)\n"
    "with p.open('a') as fh:\n"
    "    fh.write(json.dumps({'domain': 'port', 'tier_verdict': "
    "{'status': 'FAIL', 'tolerance_policy': 'rtol=1e-4', 'max_discrepancy': 0.75}}) + chr(10))\n"
    "sys.exit(1)\n"
)

CRASH_SCRIPT = "import sys; sys.exit(2)\n"


@pytest.fixture
def xtrax_root(tmp_path: Path) -> Path:
    root = tmp_path / "xtrax"
    root.mkdir()
    return root


@pytest.fixture
def catalog_dir(tmp_path: Path) -> Path:
    cat = tmp_path / "catalog"
    cat.mkdir()
    return cat


class TestPassVerdict:
    def test_pass_writes_sidecar_and_anchors_it(
        self, xtrax_root: Path, catalog_dir: Path
    ) -> None:
        result = run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", PASS_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        assert result.verdict == "PASS"
        assert result.anchored is True
        assert result.sidecar_path is not None
        assert result.sidecar_path.exists()

        sidecar = json.loads(result.sidecar_path.read_text())
        assert sidecar["verdict"] == "PASS"
        assert sidecar["tolerance_policy"] == "rtol=1e-4"
        assert sidecar["harness_run_id"] == result.run_id

        anchor = get_anchor(catalog_dir, str(result.sidecar_path), result.sidecar_sha256)
        assert anchor is not None
        assert anchor.kind == "harness_run"
        assert anchor.label == result.run_id


class TestFailVerdict:
    def test_fail_carries_real_max_discrepancy_and_still_anchors(
        self, xtrax_root: Path, catalog_dir: Path
    ) -> None:
        result = run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", FAIL_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        assert result.verdict == "FAIL"
        assert result.max_discrepancy == 0.75
        assert result.tolerance_policy == "rtol=1e-4"
        # FAIL is a real, recorded outcome (not a crash) — it still anchors,
        # so a future attestation step can see and refuse to attest a FAIL.
        assert result.anchored is True


class TestCrashContract:
    """Backlog #3493 acceptance: harness crash -> verdict ERROR, no
    attestation, product stays candidate."""

    def test_crash_yields_error_verdict(self, xtrax_root: Path, catalog_dir: Path) -> None:
        result = run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", CRASH_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        assert result.verdict == "ERROR"

    def test_crash_is_not_anchored(self, xtrax_root: Path, catalog_dir: Path) -> None:
        run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", CRASH_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        # Nothing is anchored under kind="harness_run" for an ERROR verdict —
        # a future S4 attestation step has no harness_run_ref to find, so it
        # structurally cannot attest. This is the testable proxy for "no
        # attestation" now, since attestation.py (#3492) doesn't exist yet.
        assert find_anchors(catalog_dir, kind="harness_run") == []

    def test_crash_sidecar_is_still_written_to_disk_for_debuggability(
        self, xtrax_root: Path, catalog_dir: Path
    ) -> None:
        result = run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", CRASH_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        assert result.sidecar_path is not None
        assert result.sidecar_path.exists()
        sidecar = json.loads(result.sidecar_path.read_text())
        assert sidecar["verdict"] == "ERROR"
        assert result.anchored is False


class TestStdoutHash:
    def test_stdout_hash_populated_and_anchored_as_content_hash(
        self, xtrax_root: Path, catalog_dir: Path
    ) -> None:
        result = run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", PASS_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        assert result.stdout_hash is not None
        assert result.stdout_hash.startswith("sha256:")
        anchor = get_anchor(catalog_dir, str(result.sidecar_path), result.sidecar_sha256)
        assert anchor is not None
        assert anchor.content_hash == result.stdout_hash


class TestHarnessRunRefDiscoverability:
    """Proves the anchored sidecar is discoverable the way a future S4
    attestation.py would need to: by (path, sha256) identity, or by kind."""

    def test_anchored_sidecar_is_findable_by_kind(
        self, xtrax_root: Path, catalog_dir: Path
    ) -> None:
        result = run_harness_as_bathos_run(
            harness_argv=[sys.executable, "-c", PASS_SCRIPT],
            xtrax_root=xtrax_root,
            catalog_dir=catalog_dir,
        )
        found = find_anchors(catalog_dir, kind="harness_run")
        assert len(found) == 1
        assert found[0].path == str(result.sidecar_path)
        assert found[0].sha256 == result.sidecar_sha256
