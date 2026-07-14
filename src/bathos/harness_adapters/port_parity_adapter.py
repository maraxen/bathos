#!/usr/bin/env python3
"""S6 harness adapter (backlog #3493): wraps xtrax's port/ T1-T5 parity
harness as a bathos-run subprocess.

Design locked by gate #3489 (NO-GO on retargeting to empirical_oracle — a
boolean bug-gate; S6 targets port/'s real numeric-tolerance parity oracle
instead). See decisions/260714_spike-harness-supportability.md (maraxiom
repo).

This script is spawned BY bathos.runner.run_script (the existing "add-data
verb runs a subprocess" path — ``bth run``) as the top-level subprocess.
Its job:

1. Record how many lines are already in the target repo's
   ``.praxia/audits.jsonl`` (the port-domain emit sink; see
   ``port/emit/port_emit.py`` / ``port/tests/conftest.py`` in xtrax).
2. Run the REAL harness command (e.g.
   ``uv run pytest port/tests/test_parity_safe_map.py -v --tb=short``) as a
   nested subprocess, with ``cwd`` set to the target repo root so ``uv run``
   resolves that repo's own venv/dependencies (jax, xtrax, etc.) — this
   adapter itself never needs those, only stdlib.
3. Diff the audits.jsonl lines appended during that run, extract the
   port-domain tier_verdict records, and compute an overall verdict
   (PASS|WARN|FAIL|ERROR), the max discrepancy across FAIL'd tiers, and the
   shared tolerance_policy.
4. Hash the harness's combined stdout+stderr.
5. Write all of that as bathos's ``$BTH_RESULTS_PATH`` JSON contract, so
   ``bathos.harness_run.run_harness_as_bathos_run`` can read it back via
   ``run.metadata`` after ``run_script`` returns.

Crash contract: if step 2's subprocess spawn itself fails, or an unhandled
exception occurs anywhere in this script, the outer except clause still
writes a best-effort ``verdict: "ERROR"`` result (never silently swallows a
crash into an empty/missing results file) and exits non-zero.

Deliberately stdlib-only — see harness_adapters/__init__.py docstring.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

VALID_VERDICTS = ("PASS", "WARN", "FAIL", "ERROR")


def count_lines(path: Path) -> int:
    """Number of lines currently in ``path``, or 0 if it doesn't exist yet."""
    if not path.exists():
        return 0
    with path.open("r", encoding="utf-8") as fh:
        return sum(1 for _ in fh)


def _normalize_port_record(record: dict[str, Any]) -> dict[str, Any] | None:
    """Return the port-domain record body (with a top-level ``tier_verdict``),
    handling both known audits.jsonl shapes:

    - flat sketch schema (port_emit._append_sketch_jsonl): domain/tier_verdict
      are top-level fields.
    - enveloped schema (port_emit._try_delegate_append, via
      xtrax.devtools.emit.append_finding): domain/tier_verdict live nested
      under a top-level ``payload`` dict.

    Returns None if ``record`` isn't a port-domain tier-verdict record at all.
    """
    if record.get("domain") == "port" and "tier_verdict" in record:
        return record
    payload = record.get("payload")
    if (
        isinstance(payload, dict)
        and payload.get("domain") == "port"
        and "tier_verdict" in payload
    ):
        return payload
    return None


def read_new_port_records(path: Path, start_line: int) -> list[dict[str, Any]]:
    """Read port-domain tier_verdict records appended to ``path`` after
    ``start_line`` (the line count captured before the harness ran).

    Malformed JSON lines and non-port-domain records are silently skipped —
    this diffs one specific known-shape sink, not a general-purpose log
    parser.
    """
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8") as fh:
        lines = fh.readlines()
    records: list[dict[str, Any]] = []
    for line in lines[start_line:]:
        line = line.strip()
        if not line:
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(raw, dict):
            continue
        normalized = _normalize_port_record(raw)
        if normalized is not None:
            records.append(normalized)
    return records


def compute_verdict(records: list[dict[str, Any]], exit_code: int) -> str:
    """Overall PASS|WARN|FAIL|ERROR verdict for one harness invocation.

    Rules (deterministic given (records, exit_code) — see
    tests/test_port_parity_adapter.py::TestComputeVerdict for the full
    matrix):
      - Any tier record with status=="FAIL" -> FAIL, regardless of exit_code.
      - No records at all:
          - exit_code != 0 -> ERROR (harness crashed before producing
            anything).
          - exit_code == 0 -> WARN (ran "cleanly" but produced no parity
            findings at all — too suspicious to call PASS).
      - Records exist, none FAIL, exit_code != 0 -> WARN (degraded/incomplete
        — something exited non-cleanly despite reported tier passes).
      - Records exist, none FAIL, exit_code == 0 -> PASS.
    """
    statuses = [r.get("tier_verdict", {}).get("status") for r in records]
    if any(status == "FAIL" for status in statuses):
        return "FAIL"
    if not records:
        return "ERROR" if exit_code != 0 else "WARN"
    if exit_code != 0:
        return "WARN"
    return "PASS"


def compute_max_discrepancy(records: list[dict[str, Any]]) -> float | None:
    """Max of all non-null tier_verdict.max_discrepancy values, or None if
    none of the records carry one (e.g. an all-PASS run)."""
    values = [
        r.get("tier_verdict", {}).get("max_discrepancy")
        for r in records
    ]
    numeric = [v for v in values if v is not None]
    return max(numeric) if numeric else None


def compute_tolerance_policy(records: list[dict[str, Any]]) -> str:
    """The first non-empty tolerance_policy across records, or "" if none."""
    for r in records:
        policy = r.get("tier_verdict", {}).get("tolerance_policy")
        if policy:
            return str(policy)
    return ""


def _atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(path)


def _parse_argv(argv: list[str]) -> tuple[Path, Path, Path | None, list[str]]:
    """Parse ``--cwd PATH --audits-path PATH [--result-path PATH] -- <harness argv...>``."""
    if "--" not in argv:
        raise ValueError(
            "port_parity_adapter argv must contain a '--' separator before the "
            "harness command"
        )
    sep = argv.index("--")
    head, harness_argv = argv[:sep], argv[sep + 1 :]
    if not harness_argv:
        raise ValueError("no harness command given after '--'")
    parser = argparse.ArgumentParser(prog="port_parity_adapter")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--audits-path", required=True)
    parser.add_argument(
        "--result-path",
        required=False,
        default=None,
        help=(
            "Optional explicit path to also write the result JSON to, in "
            "addition to $BTH_RESULTS_PATH. Callers that read results back "
            "in-process (bathos.harness_run) use this instead of relying on "
            "bathos's catalog metadata round-trip, which is lossy at the "
            "cool tier (Run.to_arrow uses COOL_SCHEMA, which has no "
            "'metadata' field) until a run has been through bathos.compact."
        ),
    )
    parsed = parser.parse_args(head)
    result_path = Path(parsed.result_path) if parsed.result_path else None
    return Path(parsed.cwd), Path(parsed.audits_path), result_path, harness_argv


def _error_result(message: str) -> dict[str, Any]:
    return {
        "metadata": {
            "verdict": "ERROR",
            "max_discrepancy": None,
            "tolerance_policy": "",
            "stdout_hash": None,
            "harness_exit_code": None,
            "harness_cmd": None,
            "tier_record_count": 0,
            "adapter_error": message,
        }
    }


def _scavenge_result_path(argv: list[str]) -> Path | None:
    """Best-effort recovery of --result-path's value without full argparse —
    used only to still write an ERROR result to it when _parse_argv itself
    raised (e.g. a missing '--' separator), so a malformed invocation doesn't
    silently produce nothing at the one path the caller (bathos.harness_run)
    is actually going to read."""
    if "--result-path" not in argv:
        return None
    idx = argv.index("--result-path")
    if idx + 1 >= len(argv):
        return None
    return Path(argv[idx + 1])


def _write_result_everywhere(
    result: dict[str, Any],
    bth_results_path: Path | None,
    explicit_result_path: Path | None,
) -> None:
    if bth_results_path is not None:
        _atomic_write_json(bth_results_path, result)
    if explicit_result_path is not None:
        _atomic_write_json(explicit_result_path, result)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv

    results_path_str = os.environ.get("BTH_RESULTS_PATH")
    bth_results_path = Path(results_path_str) if results_path_str else None

    try:
        cwd, audits_path, explicit_result_path, harness_argv = _parse_argv(argv)
    except ValueError as exc:
        explicit_result_path = _scavenge_result_path(argv)
        if bth_results_path is None and explicit_result_path is None:
            sys.stderr.write(f"port_parity_adapter: {exc}\n")
            return 1
        _write_result_everywhere(_error_result(str(exc)), bth_results_path, explicit_result_path)
        return 1

    if bth_results_path is None and explicit_result_path is None:
        sys.stderr.write(
            "port_parity_adapter: neither $BTH_RESULTS_PATH nor --result-path is set; "
            "nowhere to write the result\n"
        )
        return 1

    try:
        start_line = count_lines(audits_path)
        proc = subprocess.run(
            harness_argv,
            cwd=cwd,
            capture_output=True,
            text=True,
        )
        stdout_combined = (proc.stdout or "") + (proc.stderr or "")
        stdout_hash = "sha256:" + hashlib.sha256(
            stdout_combined.encode("utf-8", "replace")
        ).hexdigest()

        records = read_new_port_records(audits_path, start_line)
        verdict = compute_verdict(records, proc.returncode)
        max_discrepancy = compute_max_discrepancy(records)
        tolerance_policy = compute_tolerance_policy(records)

        result = {
            "metadata": {
                "verdict": verdict,
                "max_discrepancy": max_discrepancy,
                "tolerance_policy": tolerance_policy,
                "stdout_hash": stdout_hash,
                "harness_exit_code": proc.returncode,
                "harness_cmd": harness_argv,
                "tier_record_count": len(records),
            }
        }
        _write_result_everywhere(result, bth_results_path, explicit_result_path)
        return 0 if verdict != "ERROR" else 1
    except Exception as exc:  # noqa: BLE001 - crash contract: always emit a result
        _write_result_everywhere(
            _error_result(f"{type(exc).__name__}: {exc}"),
            bth_results_path,
            explicit_result_path,
        )
        return 1


if __name__ == "__main__":
    sys.exit(main())
