"""Remediation group 6: an unusable `run.started` declaration (the `{"repr": ...}` fallback of
`sidecar_declaration_for_event`) must fail closed, not rebuild an empty sidecar that passes."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from bathos import breakdown_gate as gate
from bathos.errors import BathosErrorCode
from bathos.sidecar import Sidecar, SidecarError

_FALLBACK = {"repr": "Sidecar(kind=<SidecarKind.EXPERIMENT: 'experiment'>, ...)"}


def test_the_repr_fallback_is_not_a_declaration():
    with pytest.raises(SidecarError):
        Sidecar.from_declaration(_FALLBACK)


def test_a_declaration_with_unknown_keys_only_is_not_a_declaration():
    with pytest.raises(SidecarError):
        Sidecar.from_declaration({"added_by_a_future_version": 1})


def test_the_resolver_answers_unresolvable(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "_run_started_declaration", lambda *_args: _FALLBACK)
    run = SimpleNamespace(id="r1", sidecar_path="", sidecar_sha256="")
    resolved = gate.resolve_run_sidecar(run, tmp_path, db=object(), log_mode=True)
    assert isinstance(resolved, gate.Unresolvable)


def test_the_gate_refuses_with_sidecar_unverifiable(monkeypatch, tmp_path):
    monkeypatch.setattr(gate, "_run_started_declaration", lambda *_args: _FALLBACK)
    run = SimpleNamespace(
        id="r1",
        outcome="pass",
        tentative_outcome=None,
        metadata="{}",
        sidecar_path="",
        sidecar_sha256="",
    )
    findings = gate.check_runs({"r1": run}, tmp_path, db=object(), log_mode=True)
    assert [f.code for f in findings] == [BathosErrorCode.SIDECAR_UNVERIFIABLE]
    with pytest.raises(Exception, match="declaration") as exc:
        gate.refuse_on_findings(findings, "where")
    assert exc.value.code is BathosErrorCode.SIDECAR_UNVERIFIABLE
