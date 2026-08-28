"""The authoring write path: render, verify, then write -- or write nothing at all.

The ordering here is the whole point, so it is spelled out rather than left implicit:

    1. reject unknown keys        -- a typo names itself instead of vanishing
    2. render                     -- payload to canonical TOML
    3. RE-PARSE the rendered bytes with the same reader the read path uses
    4. validate                   -- with the existing validator, unchanged
    5. GATE: any error and nothing is written
    6. write atomically           -- tmp in the target dir, fsync, os.replace
    7. emit telemetry

Step 3 re-parses the *rendered artifact* rather than trusting the in-memory payload.
That is what makes a renderer bug structurally unable to reach disk: if the renderer
emitted something unparseable -- as the old hand-written claim template did for two and
a half months -- step 3 raises and step 5 refuses. Validating the payload directly would
let a broken renderer straight through.

The gate is also what makes validation unskippable. Today an agent can scaffold, hand-
edit, and simply never call validate; here the validator runs before the bytes exist.

Step 7 is an append-only ledger line (see :mod:`bathos.authoring.ledger`), written
through ``cisternal.provenance.durable.append_manifest`` -- flock'd, append-mode, with no
update or delete API anywhere below it.

The ledger append is ordered AFTER the write and is allowed to undo it. If the append
fails inside a repository, the document is restored to its previous bytes (or removed, if
this call created it) and the whole operation is reported as a refusal. The alternative --
leaving a document on disk with no record of how it got there -- is exactly the state the
ledger exists to make impossible. Outside a repository there is no tracked manifest to
append to, which is legitimate; that case reports ``ledger_recorded=False`` rather than
failing.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel

from bathos.authoring.models import ClaimPayload, unknown_keys
from bathos.authoring.render import render_claim
from bathos.errors import RESOLUTION_HINTS, BathosErrorCode


@dataclass
class AuthorResult:
    """Outcome of an authoring attempt.

    ``ok=False`` always means the target file is untouched -- there is no partial-write
    state to reason about.
    """

    ok: bool
    path: Path | None = None
    sha256: str | None = None
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    infos: list[str] = field(default_factory=list)
    unknown_keys: list[str] = field(default_factory=list)
    error_code: str | None = None
    ledger_recorded: bool = False
    ledger_note: str = ""

    @property
    def resolution_hint(self) -> str:
        if self.error_code is None:
            return ""
        try:
            return RESOLUTION_HINTS[BathosErrorCode(self.error_code)]
        except ValueError:
            return ""

    def as_envelope(self) -> dict:
        """The MCP error-envelope shape: ok/error_code/error/resolution_hint plus data."""
        return {
            "ok": self.ok,
            "error_code": self.error_code,
            "error": self.errors[0] if self.errors else None,
            "resolution_hint": self.resolution_hint or None,
            "path": str(self.path) if self.path else None,
            "sha256": self.sha256,
            "errors": list(self.errors),
            "warnings": list(self.warnings),
            "infos": list(self.infos),
            "unknown_keys": list(self.unknown_keys),
            "ledger_recorded": self.ledger_recorded,
            "ledger_note": self.ledger_note or None,
        }


def _refusal(code: BathosErrorCode, errors: list[str], **extra) -> AuthorResult:
    return AuthorResult(ok=False, error_code=code.value, errors=errors, **extra)


def _atomic_write(path: Path, content: str) -> str:
    """Write *content* to *path* atomically; return its sha256.

    Temp file lives in the target directory so ``os.replace`` stays within one
    filesystem and is therefore atomic.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w", dir=path.parent, suffix=".tmp", delete=False, encoding="utf-8"
    ) as tmp:
        tmp_path = Path(tmp.name)
        tmp.write(content)
        tmp.flush()
        os.fsync(tmp.fileno())

    try:
        os.replace(tmp_path, path)
    except OSError:
        tmp_path.unlink(missing_ok=True)
        raise

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _default_workspace_root(target: Path) -> Path:
    """Where the ledger lives for *target*: its repository, else its own directory."""
    from cisternal.provenance.durable import repo_root

    parent = Path(target).parent
    return repo_root(parent) or parent


def _rollback(target: Path, before_content: bytes | None) -> None:
    """Undo a write: restore the prior bytes, or remove a file this call created."""
    if before_content is None:
        target.unlink(missing_ok=True)
    else:
        target.write_bytes(before_content)


def _record_mutation(
    *,
    doc_kind: str,
    path: Path,
    workspace_root: Path,
    before_sha256: str | None,
    after_sha256: str,
    before_blob_sha: str | None,
    op: str,
    actor: str,
    reason: str,
    campaign_id: str | None = None,
):
    """Append the ledger line and emit telemetry.

    Raises:
        LedgerAppendError: inside a repository, when the append did not land. The caller
            rolls the document write back rather than leaving it unrecorded.
    """
    from bathos.authoring.ledger import append_authoring_entry, build_entry
    from bathos.telemetry import event

    entry = build_entry(
        doc_kind=doc_kind,
        path=path,
        workspace_root=workspace_root,
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        before_blob_sha=before_blob_sha,
        op=op,
        actor=actor,
        reason=reason,
        campaign_id=campaign_id,
    )

    ledger = append_authoring_entry(entry, workspace_root)

    event(
        f"authoring.{op}",
        doc_kind=doc_kind,
        path=str(path),
        before_sha256=before_sha256,
        after_sha256=after_sha256,
        actor=actor,
        reason=reason,
        ledger_recorded=ledger.recorded,
    )
    return ledger


def author_claim(
    payload: ClaimPayload | dict,
    target: Path,
    *,
    force: bool = False,
    actor: str = "cli",
    reason: str = "",
    catalog_db=None,
    workspace_root: Path | None = None,
    campaign_id: str | None = None,
) -> AuthorResult:
    """Author a claim document from a structured payload.

    Args:
        payload:    a :class:`ClaimPayload`, or a dict to coerce into one.
        target:     where to write. Refused if it exists and *force* is False.
        force:      overwrite an existing document.
        actor:      ``"cli"`` or ``"mcp"``, recorded with the mutation.
        reason:     free text recorded with the mutation.
        catalog_db: optional DuckDB connection. When given, ``validate_claim`` can run
                    its catalog-aware checks (baseline parity against real runs, gate
                    state); without it those checks are skipped exactly as they are on
                    the existing validate path.

    Returns:
        An :class:`AuthorResult`. On refusal, *target* is untouched.
    """
    from bathos.claim import parse_claim, validate_claim

    # 1. Coerce, and refuse typos by name rather than dropping them.
    if not isinstance(payload, BaseModel):
        try:
            payload = ClaimPayload.model_validate(payload)
        except Exception as e:
            return _refusal(BathosErrorCode.DOCUMENT_INVALID, [f"payload does not validate: {e}"])

    stray = unknown_keys(payload)
    if stray:
        return _refusal(
            BathosErrorCode.DOCUMENT_INVALID,
            [f"unrecognised field(s): {', '.join(stray)}"],
            unknown_keys=stray,
        )

    # 2. Conflict check before doing any work.
    target = Path(target)
    before_sha256: str | None = None
    before_content: bytes | None = None
    if target.exists():
        if not force:
            return _refusal(
                BathosErrorCode.DOCUMENT_CONFLICT,
                [f"a document already exists at {target}"],
            )
        before_content = target.read_bytes()
        before_sha256 = hashlib.sha256(before_content).hexdigest()

    # 3. Render.
    rendered = render_claim(payload, guidance=False)

    # 4. Re-parse the RENDERED BYTES with the real reader, in a scratch location so a
    #    failure cannot leave anything behind at the target.
    with tempfile.TemporaryDirectory() as scratch:
        probe = Path(scratch) / target.name
        probe.write_text(rendered)
        try:
            parsed = parse_claim(probe)
        except (ValueError, FileNotFoundError) as e:
            return _refusal(
                BathosErrorCode.DOCUMENT_INVALID,
                [f"rendered document did not parse: {e}"],
            )

        # 5. Validate, and gate on it.
        result = validate_claim(parsed, db=catalog_db)

    if not result.ok:
        return AuthorResult(
            ok=False,
            error_code=BathosErrorCode.DOCUMENT_INVALID.value,
            errors=[e.message for e in result.errors],
            warnings=list(result.warnings),
            infos=list(result.infos),
        )

    # 6. Write atomically.
    root = Path(workspace_root) if workspace_root else _default_workspace_root(target)

    # Stash the superseded bytes in the git object store BEFORE overwriting them, so the
    # ledger's before_blob_sha refers to content that is actually recoverable.
    before_blob_sha = None
    if before_content is not None:
        from bathos.authoring.ledger import stash_blob

        before_blob_sha = stash_blob(before_content, root)

    sha256 = _atomic_write(target, rendered)

    # 7. Record. A ledger failure inside a repository undoes the write -- a document with
    #    no record of its provenance is worse than no document.
    from bathos.authoring.ledger import LedgerAppendError

    try:
        ledger = _record_mutation(
            doc_kind="claim",
            path=target,
            workspace_root=root,
            before_sha256=before_sha256,
            after_sha256=sha256,
            before_blob_sha=before_blob_sha,
            op="amend" if before_sha256 else "create",
            actor=actor,
            reason=reason,
            campaign_id=campaign_id,
        )
    except LedgerAppendError as e:
        _rollback(target, before_content)
        return _refusal(BathosErrorCode.DOCUMENT_INVALID, [str(e)])

    return AuthorResult(
        ok=True,
        path=target,
        sha256=sha256,
        warnings=list(result.warnings),
        infos=list(result.infos),
        ledger_recorded=ledger.recorded,
        ledger_note=ledger.reason,
    )
