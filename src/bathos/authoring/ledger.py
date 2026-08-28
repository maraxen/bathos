"""Append-only ledger of authored-document mutations.

Every create or amend that reaches disk gets one JSONL line recording what changed and
what it changed from. The append itself is
:func:`cisternal.provenance.durable.append_manifest` -- flock'd, append-mode, one
``json.dumps(..., sort_keys=True)`` per line, with no update or delete API anywhere in
cisternal or bathos. bathos supplies the path, exactly as ``bathos.git_pin`` supplies
``MANIFEST_RELPATH`` for the run manifest.

The ledger lands at ``.bth/refs/authoring.jsonl``. ``.bth/refs`` is already listed in
``bathos.git_pin.PROVENANCE_PATHS``, so it is a tracked, git-visible file and a run's
worktree snapshot captures it for free -- no new provenance wiring.

**Two failure modes that `append_manifest` deliberately conflates.** It returns ``None``
both when the append raised ``OSError`` *and* when ``repo_root(cwd)`` is ``None``. Those
mean opposite things:

* not a git repository -- there is no tracked manifest to append to, and never could be.
  Legitimate (a scratch directory, a test tmp_path). Record telemetry, report
  ``recorded=False``, and let the write stand.
* inside a repository but the append failed -- a read-only ``.bth/``, a full disk. This
  is the case that would otherwise leave a document on disk with no record of how it got
  there, so the caller must roll the write back.

Distinguishing them is this module's job; callers get :class:`LedgerAppendError` for the
second and a plain ``recorded=False`` for the first.

**What immutability means here, precisely.** The file is append-only by construction and
git-tracked, so history is as tamper-evident as the repository is. It is not
cryptographically signed. What it does give you is detection:
:func:`verify_authoring_ledger` checks chain continuity (entry N's ``before_sha256``
equals entry N-1's ``after_sha256`` for the same path) and live agreement (the newest
entry's ``after_sha256`` equals the file's current hash). An out-of-band hand-edit breaks
the second; a removed or reordered line breaks the first.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

SCHEMA_VERSION = 1


class LedgerAppendError(RuntimeError):
    """The ledger append failed inside a repository where it should have succeeded."""


@dataclass
class LedgerResult:
    """Outcome of an append.

    ``recorded=False`` with no error means there was no repository to record into --
    not that recording was skipped for convenience.
    """

    recorded: bool
    entry: dict
    manifest_path: Path | None = None
    reason: str = ""


@dataclass
class VerifyResult:
    ok: bool
    entries_checked: int = 0
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _authoring_relpath() -> Path:
    from bathos.git_pin import AUTHORING_RELPATH

    return AUTHORING_RELPATH


def _repo_root(cwd: Path) -> Path | None:
    from cisternal.provenance.durable import repo_root

    return repo_root(cwd)


def stash_blob(content: bytes, cwd: Path) -> str | None:
    """Write *content* into the git object store and return its blob sha.

    This is what makes a superseded document recoverable: the bytes live in the object
    store keyed by the returned sha, independent of the file that used to hold them.

    Deliberately NOT paired with ``update_ref``. ``update_ref`` verifies with
    ``rev-parse --verify <sha>^{commit}``, so pointing a ref at a blob fails the
    read-back check and silently returns False. The sha in the ledger line is the
    reference.
    """
    if _repo_root(cwd) is None:
        return None
    try:
        proc = subprocess.run(
            ["git", "hash-object", "-w", "--stdin"],
            cwd=str(cwd),
            input=content,
            capture_output=True,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    sha = proc.stdout.decode("utf-8", errors="replace").strip()
    return sha or None


def build_entry(
    *,
    doc_kind: str,
    path: Path,
    workspace_root: Path,
    before_sha256: str | None,
    after_sha256: str,
    op: str,
    actor: str,
    reason: str = "",
    campaign_id: str | None = None,
    before_blob_sha: str | None = None,
    mcp_request_id: str | None = None,
) -> dict:
    """Assemble one ledger line. Paths are stored workspace-relative and POSIX-shaped."""
    try:
        rel = Path(path).resolve().relative_to(Path(workspace_root).resolve()).as_posix()
    except ValueError:
        rel = Path(path).as_posix()

    return {
        "schema": SCHEMA_VERSION,
        "entry_id": uuid.uuid4().hex,
        "ts": datetime.now(UTC).isoformat(),
        "op": op,
        "doc_kind": doc_kind,
        "path": rel,
        "before_sha256": before_sha256,
        "after_sha256": after_sha256,
        "before_blob_sha": before_blob_sha,
        "actor": actor,
        "reason": reason,
        "campaign_id": campaign_id,
        "mcp_request_id": mcp_request_id,
    }


def append_authoring_entry(entry: dict, cwd: Path) -> LedgerResult:
    """Append *entry*, distinguishing "no repo" from "append failed".

    Raises:
        LedgerAppendError: inside a repository, when the append did not land. The caller
            must roll its write back rather than leave an unrecorded document on disk.
    """
    from bathos.git_pin import append_authoring_manifest

    root = _repo_root(cwd)
    if root is None:
        return LedgerResult(
            recorded=False,
            entry=entry,
            reason="not a git repository -- no tracked manifest to append to",
        )

    manifest_path = append_authoring_manifest(entry, cwd)
    if manifest_path is None:
        raise LedgerAppendError(
            f"failed to append the authoring ledger under {root / _authoring_relpath()}; "
            "the document write has been rolled back"
        )

    return LedgerResult(recorded=True, entry=entry, manifest_path=manifest_path)


def read_authoring_entries(cwd: Path) -> list[dict]:
    """Every ledger entry, in file order. Malformed lines are skipped, not fatal."""
    root = _repo_root(cwd)
    if root is None:
        return []

    path = root / _authoring_relpath()
    if not path.exists():
        return []

    entries: list[dict] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


def verify_authoring_ledger(cwd: Path) -> VerifyResult:
    """Check the ledger is internally consistent and agrees with what is on disk.

    Two independent checks, per document path:

    1. **Chain continuity** -- entry N's ``before_sha256`` must equal entry N-1's
       ``after_sha256``. A break means a line was removed or reordered, or the document
       was edited outside the authoring path between two recorded mutations.
    2. **Live agreement** -- the newest entry's ``after_sha256`` must equal the file's
       current sha256. A mismatch means the document was hand-edited after its last
       recorded mutation.

    A document recorded in the ledger but now missing is a warning, not an error: a
    deliberate deletion is legitimate and outside this ledger's remit.
    """
    import hashlib

    root = _repo_root(cwd)
    if root is None:
        return VerifyResult(ok=True, warnings=["not a git repository -- no ledger to verify"])

    entries = read_authoring_entries(cwd)
    if not entries:
        return VerifyResult(ok=True, entries_checked=0)

    by_path: dict[str, list[dict]] = {}
    for entry in entries:
        by_path.setdefault(entry.get("path", ""), []).append(entry)

    errors: list[str] = []
    warnings: list[str] = []

    for rel, chain in by_path.items():
        previous: dict | None = None
        for entry in chain:
            before = entry.get("before_sha256")
            if previous is None:
                if before is not None:
                    warnings.append(
                        f"{rel}: first recorded entry amends sha {before[:12]}, which this "
                        "ledger has no record of creating (pre-existing document)"
                    )
            elif before != previous.get("after_sha256"):
                errors.append(
                    f"{rel}: broken chain -- entry {entry.get('entry_id', '?')[:8]} follows "
                    f"{previous.get('after_sha256', 'None')} but records "
                    f"before_sha256={before}"
                )
            previous = entry

        live = root / rel
        if not live.exists():
            warnings.append(f"{rel}: recorded in the ledger but no longer on disk")
            continue

        actual = hashlib.sha256(live.read_bytes()).hexdigest()
        expected = chain[-1].get("after_sha256")
        if actual != expected:
            errors.append(
                f"{rel}: on-disk sha256 {actual[:12]} does not match the newest ledger "
                f"entry {str(expected)[:12]} -- edited outside the authoring path"
            )

    return VerifyResult(
        ok=not errors, entries_checked=len(entries), errors=errors, warnings=warnings
    )
