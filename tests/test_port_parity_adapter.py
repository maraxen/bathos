"""Tests for the S6 harness-adapter script (bathos.harness_adapters.port_parity_adapter).

Backlog #3493 (task 260713_figure-eda-build-dag), design locked by gate #3489
(NO-GO on retargeting to empirical_oracle — S6 targets xtrax's port/ T1-T5
harness instead). This adapter is the thing bathos's run_script actually
spawns as its subprocess: it shells out to the *real* harness command (e.g.
``uv run pytest port/tests/test_parity_safe_map.py``) inside the target
repo's own cwd/venv, diffs the port-domain records newly appended to that
repo's audits.jsonl during the run, computes an overall verdict, and writes
the bathos ``$BTH_RESULTS_PATH`` JSON contract so bathos.harness_run can read
it back as ``run.metadata``.

Deliberately stdlib-only (no ``import bathos``) — it must run correctly
inside a *different* project's uv-managed venv (the target repo's), not
bathos's own, mirroring the existing "target script writes to
$BTH_RESULTS_PATH, never imports bathos" convention used throughout
bathos.runner.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from bathos.harness_adapters import port_parity_adapter as adapter

_ADAPTER_PATH = Path(adapter.__file__)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        for rec in records:
            fh.write(json.dumps(rec))
            fh.write("\n")


class TestCountLines:
    def test_missing_file_is_zero(self, tmp_path: Path) -> None:
        assert adapter.count_lines(tmp_path / "nope.jsonl") == 0

    def test_counts_existing_lines(self, tmp_path: Path) -> None:
        p = tmp_path / "a.jsonl"
        p.write_text("a\nb\nc\n", encoding="utf-8")
        assert adapter.count_lines(p) == 3


class TestReadNewPortRecords:
    def test_flat_schema_filters_by_domain_and_slices_from_start_line(
        self, tmp_path: Path
    ) -> None:
        p = tmp_path / "audits.jsonl"
        _write_jsonl(
            p,
            [
                {"domain": "other", "tier_verdict": {"status": "PASS"}},
                {"domain": "port", "tier_verdict": {"status": "PASS"}},
            ],
        )
        recs = adapter.read_new_port_records(p, start_line=1)
        assert len(recs) == 1
        assert recs[0]["tier_verdict"]["status"] == "PASS"

    def test_enveloped_schema_unwraps_payload(self, tmp_path: Path) -> None:
        # bathos.harness_adapters emit's real shape once xtrax.devtools.emit
        # is available: top-level dim/payload envelope, domain nested inside
        # payload (see port/emit/port_emit.py::_try_delegate_append).
        p = tmp_path / "audits.jsonl"
        _write_jsonl(
            p,
            [
                {
                    "dim": "port",
                    "payload": {
                        "domain": "port",
                        "tier_verdict": {"status": "FAIL", "max_discrepancy": 0.5},
                    },
                }
            ],
        )
        recs = adapter.read_new_port_records(p, start_line=0)
        assert len(recs) == 1
        assert recs[0]["tier_verdict"]["max_discrepancy"] == 0.5

    def test_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert adapter.read_new_port_records(tmp_path / "nope.jsonl", 0) == []

    def test_malformed_lines_are_skipped(self, tmp_path: Path) -> None:
        p = tmp_path / "audits.jsonl"
        good = json.dumps({"domain": "port", "tier_verdict": {"status": "PASS"}})
        p.write_text(f"not json\n{good}\n", encoding="utf-8")
        recs = adapter.read_new_port_records(p, 0)
        assert len(recs) == 1

    def test_non_port_domain_records_are_excluded(self, tmp_path: Path) -> None:
        p = tmp_path / "audits.jsonl"
        _write_jsonl(p, [{"domain": "correctness", "tier_verdict": {"status": "FAIL"}}])
        assert adapter.read_new_port_records(p, 0) == []


class TestComputeVerdict:
    def test_pass_when_all_pass_and_exit_zero(self) -> None:
        records = [
            {"tier_verdict": {"status": "PASS"}},
            {"tier_verdict": {"status": "PASS"}},
        ]
        assert adapter.compute_verdict(records, 0) == "PASS"

    def test_fail_when_any_fail_regardless_of_exit_code(self) -> None:
        records = [
            {"tier_verdict": {"status": "PASS"}},
            {"tier_verdict": {"status": "FAIL"}},
        ]
        assert adapter.compute_verdict(records, 0) == "FAIL"
        assert adapter.compute_verdict(records, 1) == "FAIL"

    def test_error_when_no_records_and_nonzero_exit(self) -> None:
        assert adapter.compute_verdict([], 1) == "ERROR"

    def test_warn_when_no_records_but_exit_zero(self) -> None:
        # Harness ran "cleanly" but produced no parity findings at all — too
        # suspicious to call PASS, but there's no crash evidence either.
        assert adapter.compute_verdict([], 0) == "WARN"

    def test_warn_when_records_all_pass_but_exit_nonzero(self) -> None:
        records = [{"tier_verdict": {"status": "PASS"}}]
        assert adapter.compute_verdict(records, 1) == "WARN"


class TestComputeMaxDiscrepancy:
    def test_none_when_no_values_present(self) -> None:
        records = [{"tier_verdict": {"status": "PASS"}}]
        assert adapter.compute_max_discrepancy(records) is None

    def test_takes_max_of_non_null_values(self) -> None:
        records = [
            {"tier_verdict": {"status": "FAIL", "max_discrepancy": 0.1}},
            {"tier_verdict": {"status": "FAIL", "max_discrepancy": 0.75}},
        ]
        assert adapter.compute_max_discrepancy(records) == 0.75

    def test_empty_records_is_none(self) -> None:
        assert adapter.compute_max_discrepancy([]) is None


class TestComputeTolerancePolicy:
    def test_empty_when_no_records(self) -> None:
        assert adapter.compute_tolerance_policy([]) == ""

    def test_takes_first_nonempty_policy(self) -> None:
        records = [
            {"tier_verdict": {"status": "PASS", "tolerance_policy": ""}},
            {"tier_verdict": {"status": "PASS", "tolerance_policy": "rtol=1e-4"}},
        ]
        assert adapter.compute_tolerance_policy(records) == "rtol=1e-4"


class TestMainSubprocessSmoke:
    """End-to-end: run the adapter as a real subprocess against fake harnesses
    representative of xtrax's port/ audits.jsonl contract (real xtrax/jax is
    not a bathos test dependency, so these fixture harnesses stand in for it —
    they write the exact same JSONL shape port/tests/conftest.py produces)."""

    def _run_adapter(
        self, tmp_path: Path, harness_argv: list[str]
    ) -> tuple[subprocess.CompletedProcess, Path, Path]:
        xtrax_root = tmp_path / "xtrax"
        xtrax_root.mkdir(exist_ok=True)
        audits_path = xtrax_root / ".praxia" / "audits.jsonl"
        results_path = tmp_path / "results.json"
        env = dict(os.environ)
        env["BTH_RESULTS_PATH"] = str(results_path)
        proc = subprocess.run(
            [
                sys.executable,
                str(_ADAPTER_PATH),
                "--cwd",
                str(xtrax_root),
                "--audits-path",
                str(audits_path),
                "--",
                *harness_argv,
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        return proc, results_path, audits_path

    def test_pass_case_exits_zero_and_writes_pass_verdict(self, tmp_path: Path) -> None:
        fake_harness = tmp_path / "fake_pass.py"
        fake_harness.write_text(
            "import json, pathlib\n"
            "p = pathlib.Path('.praxia/audits.jsonl')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "with p.open('a') as fh:\n"
            "    fh.write(json.dumps({'domain': 'port', "
            "'tier_verdict': {'status': 'PASS', 'tolerance_policy': 'rtol=1e-4'}}) + chr(10))\n",
            encoding="utf-8",
        )
        proc, results_path, _ = self._run_adapter(
            tmp_path, [sys.executable, str(fake_harness)]
        )
        assert proc.returncode == 0, proc.stderr
        result = json.loads(results_path.read_text())
        assert result["metadata"]["verdict"] == "PASS"
        assert result["metadata"]["tolerance_policy"] == "rtol=1e-4"
        assert result["metadata"]["stdout_hash"].startswith("sha256:")

    def test_fail_case_carries_real_max_discrepancy(self, tmp_path: Path) -> None:
        fake_harness = tmp_path / "fake_fail.py"
        fake_harness.write_text(
            "import json, pathlib, sys\n"
            "p = pathlib.Path('.praxia/audits.jsonl')\n"
            "p.parent.mkdir(parents=True, exist_ok=True)\n"
            "with p.open('a') as fh:\n"
            "    fh.write(json.dumps({'domain': 'port', 'tier_verdict': "
            "{'status': 'FAIL', 'tolerance_policy': 'rtol=1e-4', 'max_discrepancy': 0.75}}) + chr(10))\n"
            "sys.exit(1)\n",
            encoding="utf-8",
        )
        proc, results_path, _ = self._run_adapter(
            tmp_path, [sys.executable, str(fake_harness)]
        )
        # Adapter itself still exits 0: it successfully determined a verdict
        # (FAIL is a real, recorded outcome, not a crash).
        assert proc.returncode == 0, proc.stderr
        result = json.loads(results_path.read_text())
        assert result["metadata"]["verdict"] == "FAIL"
        assert result["metadata"]["max_discrepancy"] == 0.75

    def test_crash_case_yields_error_verdict_and_nonzero_exit(self, tmp_path: Path) -> None:
        # Harness crashes before producing any port-domain record at all.
        proc, results_path, _ = self._run_adapter(
            tmp_path, [sys.executable, "-c", "import sys; sys.exit(2)"]
        )
        assert proc.returncode == 1
        result = json.loads(results_path.read_text())
        assert result["metadata"]["verdict"] == "ERROR"

    def test_missing_separator_is_an_adapter_error_not_a_silent_noop(
        self, tmp_path: Path
    ) -> None:
        xtrax_root = tmp_path / "xtrax"
        xtrax_root.mkdir(exist_ok=True)
        results_path = tmp_path / "results.json"
        env = dict(os.environ)
        env["BTH_RESULTS_PATH"] = str(results_path)
        proc = subprocess.run(
            [
                sys.executable,
                str(_ADAPTER_PATH),
                "--cwd",
                str(xtrax_root),
                "--audits-path",
                str(xtrax_root / ".praxia" / "audits.jsonl"),
                # no "--" separator, no harness command
            ],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
        )
        assert proc.returncode == 1
        result = json.loads(results_path.read_text())
        assert result["metadata"]["verdict"] == "ERROR"
