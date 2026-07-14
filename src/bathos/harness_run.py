"""S6 harness-as-bathos-run wrapper (backlog #3493, task 260713_figure-eda-build-dag).

Design locked by gate #3489 (owner NO-GO sign-off on retargeting to
empirical_oracle, a boolean bug-gate — S6 targets xtrax's ``port/`` T1-T5
harness instead, a real numeric-tolerance parity oracle). See
``.praxia/docs/decisions/260714_spike-harness-supportability.md`` (maraxiom
repo).

``run_harness_as_bathos_run`` invokes that harness via bathos's existing
"add-data verb runs a subprocess" pattern (:func:`bathos.runner.run_script` —
the same function backing ``bth run``), reads back the structured result the
harness adapter (:mod:`bathos.harness_adapters.port_parity_adapter`) wrote to
``$BTH_RESULTS_PATH``, and produces a **verdict sidecar**: a small JSON file
carrying the overall verdict (PASS|WARN|FAIL|ERROR), ``max_discrepancy``,
``tolerance_policy``, and ``stdout_hash``.

That sidecar is anchored via the existing S2 anchor-insert seam
(:func:`bathos.anchor.register_anchor`, item 3483, ``kind="harness_run"``) —
this is the seam a future S4 attestation step (#3492; currently a NULL-STUB,
see ``bathos.mcp.query_attestation_tool``) would use to resolve a
``harness_run_ref``: the sidecar's own ``(path, sha256)`` identity, or
equivalently ``find_anchors(catalog_dir, kind="harness_run", label=run_id)``.

Crash contract (backlog #3493 acceptance): if the harness crashes/errs
(verdict == "ERROR"), the sidecar is still written to disk for debuggability,
but it is deliberately NOT anchored — a future attestation step has no
harness_run_ref to find, so it structurally cannot attest, and the product
stays candidate. See TestCrashContract in tests/test_harness_run.py.

Note on result retrieval: this module does NOT read the adapter's structured
result back via bathos's ``run.metadata`` / ``get_run`` catalog round-trip.
``Run.to_arrow`` (bathos.schema) serializes with ``COOL_SCHEMA``, which has
no ``metadata`` field at all — it is silently dropped at the cool tier and
only appears once a run has been through ``bathos.compact`` (which populates
the separate warm DuckDB ``WARM_SCHEMA`` table). Requiring every S6 caller to
compact before reading its own just-completed run back would be an awkward,
easy-to-forget coupling, so instead this module has the adapter write its
result JSON to an explicit, wrapper-controlled path (passed via
``--result-path``, independent of ``$BTH_RESULTS_PATH``) and reads that file
directly. ``run_uuid_var`` is still used for the bathos run id (for
provenance / anchor labeling), just not for the result payload itself.
"""

from __future__ import annotations

import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from bathos.anchor import register_anchor
from bathos.runner import run_script
from bathos.telemetry import run_uuid_var

Verdict = Literal["PASS", "WARN", "FAIL", "ERROR"]

_VALID_VERDICTS = ("PASS", "WARN", "FAIL", "ERROR")

ADAPTER_PATH = Path(__file__).parent / "harness_adapters" / "port_parity_adapter.py"

DEFAULT_AUDITS_RELPATH = Path(".praxia") / "audits.jsonl"

DEFAULT_PORT_TEST_TARGET = "port/tests/test_parity_safe_map.py"


@dataclass(frozen=True)
class HarnessRunResult:
    """Outcome of one :func:`run_harness_as_bathos_run` invocation."""

    run_id: str
    verdict: Verdict
    max_discrepancy: float | None
    tolerance_policy: str
    stdout_hash: str | None
    exit_code: int
    sidecar_path: Path | None
    sidecar_sha256: str | None
    anchored: bool


def default_port_harness_argv(
    test_target: str = DEFAULT_PORT_TEST_TARGET,
) -> list[str]:
    """The real-world harness command for xtrax's port/ T1-T5 suite.

    Uses ``uv run pytest`` so the nested subprocess resolves the *target*
    repo's own venv/dependencies (jax, xtrax, ...) when invoked with
    ``cwd=xtrax_root`` — this wrapper and its adapter never need those
    dependencies themselves.
    """
    return ["uv", "run", "pytest", test_target, "-v", "--tb=short"]


def _atomic_write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(path)


def _extract_metadata_from_json_text(raw: str) -> dict:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(parsed, dict):
        return {}
    metadata = parsed.get("metadata", {})
    return metadata if isinstance(metadata, dict) else {}


def run_harness_as_bathos_run(
    *,
    harness_argv: list[str],
    xtrax_root: Path,
    catalog_dir: Path,
    project_slug: str = "xtrax-port-harness",
    audits_relpath: Path = DEFAULT_AUDITS_RELPATH,
    campaign_id: str | None = None,
    tags: list[str] | None = None,
) -> HarnessRunResult:
    """Invoke the port/ T1-T5 parity harness as a bathos-tracked subprocess
    run and produce a stable verdict sidecar (spec section 2 Flow P step P2 /
    section 7.2).

    Args:
        harness_argv: the real harness invocation, e.g.
            ``default_port_harness_argv()`` (``["uv","run","pytest",
            "port/tests/test_parity_safe_map.py","-v","--tb=short"]``). Tests
            substitute a small fake-harness script here.
        xtrax_root: path to the xtrax checkout (or a fixture standing in for
            one in tests) — passed as ``cwd`` to the harness subprocess so
            ``uv run`` resolves that repo's own project/venv.
        catalog_dir: bathos catalog directory for this run's provenance,
            sidecar, and anchor.
        project_slug: stored on the bathos Run row.
        audits_relpath: where the port-domain JSONL sink lives, relative to
            ``xtrax_root`` (default ``.praxia/audits.jsonl``, matching
            xtrax's ``port/tests/conftest.py``).
        campaign_id: optional campaign to associate the run + anchor with.
        tags: extra bathos run tags (``["harness_run", "port_parity"]`` are
            always included).

    Returns:
        HarnessRunResult with the computed verdict, sidecar location, and
        whether it was anchored (False exactly when verdict == "ERROR" — see
        module docstring's crash contract).
    """
    audits_path = xtrax_root / audits_relpath
    catalog_dir.mkdir(parents=True, exist_ok=True)

    # Explicit, wrapper-controlled result path — see module docstring's "Note
    # on result retrieval". Independent of both run.id (unknown until
    # run_script generates it) and bathos's catalog metadata round-trip.
    result_token = uuid.uuid4().hex
    tmp_result_path = catalog_dir / "harness_runs" / f".pending-{result_token}.json"

    adapter_argv = [
        sys.executable,
        str(ADAPTER_PATH),
        "--cwd",
        str(xtrax_root),
        "--audits-path",
        str(audits_path),
        "--result-path",
        str(tmp_result_path),
        "--",
        *harness_argv,
    ]

    all_tags = ["harness_run", "port_parity", *(tags or [])]

    exit_code = run_script(
        argv=adapter_argv,
        project_slug=project_slug,
        catalog_dir=catalog_dir,
        output_paths=[],
        tags=all_tags,
        cwd=xtrax_root,
        no_sidecar=True,
        campaign_id=campaign_id,
    )

    run_id = run_uuid_var.get(None) or ""

    metadata = _extract_metadata_from_json_text(
        tmp_result_path.read_text(encoding="utf-8") if tmp_result_path.exists() else ""
    )
    try:
        if tmp_result_path.exists():
            tmp_result_path.unlink()
    except OSError:
        pass

    if not run_id:
        # run_script always sets this before spawning the subprocess; this
        # branch should be unreachable in practice, but if it ever happens
        # there's nothing to anchor and nothing to key a sidecar on — treat
        # it as the crash case.
        return HarnessRunResult(
            run_id="",
            verdict="ERROR",
            max_discrepancy=None,
            tolerance_policy="",
            stdout_hash=None,
            exit_code=exit_code,
            sidecar_path=None,
            sidecar_sha256=None,
            anchored=False,
        )

    verdict = metadata.get("verdict")
    if verdict not in _VALID_VERDICTS:
        verdict = "ERROR"

    max_discrepancy = metadata.get("max_discrepancy")
    tolerance_policy = metadata.get("tolerance_policy") or ""
    stdout_hash = metadata.get("stdout_hash")

    sidecar_payload = {
        "harness_run_id": run_id,
        "harness_cmd": harness_argv,
        "verdict": verdict,
        "max_discrepancy": max_discrepancy,
        "tolerance_policy": tolerance_policy,
        "stdout_hash": stdout_hash,
        "exit_code": exit_code,
        "tier_record_count": metadata.get("tier_record_count"),
    }

    sidecar_path = catalog_dir / "harness_runs" / f"{run_id}.verdict.json"
    _atomic_write_json(sidecar_path, sidecar_payload)
    sidecar_sha256 = hashlib.sha256(sidecar_path.read_bytes()).hexdigest()

    anchored = False
    if verdict != "ERROR":
        register_anchor(
            catalog_dir,
            path=str(sidecar_path),
            sha256=sidecar_sha256,
            kind="harness_run",
            label=run_id,
            content_hash=stdout_hash,
            campaign_id=campaign_id,
        )
        anchored = True

    return HarnessRunResult(
        run_id=run_id,
        verdict=verdict,  # type: ignore[arg-type]
        max_discrepancy=max_discrepancy,
        tolerance_policy=tolerance_policy,
        stdout_hash=stdout_hash,
        exit_code=exit_code,
        sidecar_path=sidecar_path,
        sidecar_sha256=sidecar_sha256,
        anchored=anchored,
    )
