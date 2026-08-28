"""The append-only ledger of authored-document mutations.

Three properties are load-bearing and each has a test that would fail without it:

1. **Every write that lands is recorded** -- and if it cannot be recorded inside a
   repository, the write is undone rather than left unrecorded.
2. **Superseded content stays recoverable** -- the prior bytes go into the git object
   store before being overwritten, keyed by the sha in the ledger line.
3. **Tampering is detectable** -- both a hand-edit after the last recorded mutation and
   a removed or reordered ledger line.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from test_authoring_write import valid_payload  # noqa: E402

from bathos.authoring.ledger import (  # noqa: E402
    LedgerAppendError,
    append_authoring_entry,
    build_entry,
    read_authoring_entries,
    verify_authoring_ledger,
)
from bathos.authoring.write import author_claim  # noqa: E402


@pytest.fixture
def repo(tmp_path):
    """A real git repository -- the ledger only exists inside one."""
    subprocess.run(["git", "init", "-q", "."], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
    return tmp_path


@pytest.fixture
def target(repo):
    return repo / ".bth" / "claims" / "demo.claim.toml"


# ---------------------------------------------------------------------------
# 1. Every landed write is recorded
# ---------------------------------------------------------------------------


def test_create_writes_one_ledger_entry(repo, target):
    result = author_claim(valid_payload(), target, workspace_root=repo)

    assert result.ok
    assert result.ledger_recorded is True

    entries = read_authoring_entries(repo)
    assert len(entries) == 1
    entry = entries[0]
    assert entry["op"] == "create"
    assert entry["doc_kind"] == "claim"
    assert entry["before_sha256"] is None
    assert entry["after_sha256"] == result.sha256
    assert entry["path"] == ".bth/claims/demo.claim.toml"


def test_amend_chains_onto_the_previous_entry(repo, target):
    first = author_claim(valid_payload(), target, workspace_root=repo)
    second = author_claim(
        valid_payload(headline="Revised headline for the sparse claim"),
        target,
        force=True,
        workspace_root=repo,
        reason="revised after review",
    )

    assert second.ok
    create, amend = read_authoring_entries(repo)

    assert amend["op"] == "amend"
    assert amend["before_sha256"] == create["after_sha256"] == first.sha256
    assert amend["after_sha256"] == second.sha256
    assert amend["reason"] == "revised after review"


def test_the_ledger_file_lands_where_provenance_paths_expects_it(repo, target):
    """`.bth/refs` is in git_pin.PROVENANCE_PATHS, so the ledger is git-tracked."""
    from bathos.git_pin import AUTHORING_RELPATH, PROVENANCE_PATHS

    author_claim(valid_payload(), target, workspace_root=repo)

    assert (repo / AUTHORING_RELPATH).exists()
    assert any(AUTHORING_RELPATH.as_posix().startswith(p) for p in PROVENANCE_PATHS)


def test_ledger_is_append_only_across_many_writes(repo, target):
    author_claim(valid_payload(), target, workspace_root=repo)
    for i in range(4):
        author_claim(
            valid_payload(headline=f"Revision number {i} of the sparse-attention claim"),
            target,
            force=True,
            workspace_root=repo,
        )

    entries = read_authoring_entries(repo)
    assert len(entries) == 5, "each landed write must add exactly one line, never rewrite"
    assert [e["op"] for e in entries] == ["create", "amend", "amend", "amend", "amend"]


def test_entry_ids_are_unique(repo, target):
    author_claim(valid_payload(), target, workspace_root=repo)
    author_claim(
        valid_payload(headline="A second distinct headline"),
        target,
        force=True,
        workspace_root=repo,
    )

    ids = [e["entry_id"] for e in read_authoring_entries(repo)]
    assert len(set(ids)) == len(ids)


# ---------------------------------------------------------------------------
# 2. Superseded content stays recoverable
# ---------------------------------------------------------------------------


def test_superseded_content_is_recoverable_from_the_object_store(repo, target):
    author_claim(valid_payload(), target, workspace_root=repo)
    original = target.read_text()

    author_claim(
        valid_payload(headline="Revised headline for the sparse claim"),
        target,
        force=True,
        workspace_root=repo,
    )

    amend = read_authoring_entries(repo)[1]
    assert amend["before_blob_sha"], "an amend must stash the bytes it replaced"

    recovered = subprocess.run(
        ["git", "cat-file", "-p", amend["before_blob_sha"]],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert recovered == original


def test_create_has_no_prior_blob(repo, target):
    author_claim(valid_payload(), target, workspace_root=repo)
    assert read_authoring_entries(repo)[0]["before_blob_sha"] is None


# ---------------------------------------------------------------------------
# 3. Tampering is detectable
# ---------------------------------------------------------------------------


def test_verify_passes_on_an_untouched_ledger(repo, target):
    author_claim(valid_payload(), target, workspace_root=repo)
    author_claim(
        valid_payload(headline="A second distinct headline"),
        target,
        force=True,
        workspace_root=repo,
    )

    result = verify_authoring_ledger(repo)
    assert result.ok, result.errors
    assert result.entries_checked == 2


def test_verify_detects_an_out_of_band_edit(repo, target):
    """The failure mode the ledger exists to catch: a hand-edit after the last write."""
    author_claim(valid_payload(), target, workspace_root=repo)
    target.write_text(target.read_text() + "\n# snuck in by hand\n")

    result = verify_authoring_ledger(repo)

    assert not result.ok
    assert any("does not match the newest ledger entry" in e for e in result.errors)


def test_verify_detects_a_removed_ledger_line(repo, target):
    """Chain continuity: dropping a middle entry breaks the before/after linkage."""
    from bathos.git_pin import AUTHORING_RELPATH

    author_claim(valid_payload(), target, workspace_root=repo)
    author_claim(
        valid_payload(headline="Second distinct headline here"),
        target,
        force=True,
        workspace_root=repo,
    )
    author_claim(
        valid_payload(headline="Third distinct headline here"),
        target,
        force=True,
        workspace_root=repo,
    )

    ledger_path = repo / AUTHORING_RELPATH
    lines = ledger_path.read_text().splitlines()
    assert len(lines) == 3
    ledger_path.write_text("\n".join([lines[0], lines[2]]) + "\n")

    result = verify_authoring_ledger(repo)

    assert not result.ok
    assert any("broken chain" in e for e in result.errors)


def test_verify_warns_rather_than_errors_outside_a_repository(tmp_path):
    result = verify_authoring_ledger(tmp_path)
    assert result.ok
    assert any("not a git repository" in w for w in result.warnings)


def test_verify_warns_when_a_recorded_document_was_deleted(repo, target):
    """A deliberate deletion is legitimate and outside this ledger's remit."""
    author_claim(valid_payload(), target, workspace_root=repo)
    target.unlink()

    result = verify_authoring_ledger(repo)

    assert result.ok
    assert any("no longer on disk" in w for w in result.warnings)


# ---------------------------------------------------------------------------
# The "no repo" vs "append failed" distinction cisternal conflates
# ---------------------------------------------------------------------------


def test_outside_a_repository_the_write_stands_unrecorded(tmp_path):
    """append_manifest returns None here because there is no repo, not because it failed."""
    target = tmp_path / "demo.claim.toml"
    result = author_claim(valid_payload(), target, workspace_root=tmp_path)

    assert result.ok, result.errors
    assert target.exists()
    assert result.ledger_recorded is False
    assert "not a git repository" in result.ledger_note


def test_append_raises_when_it_fails_inside_a_repository(repo, monkeypatch):
    """The case that must NOT be silently tolerated."""
    import bathos.authoring.ledger as ledger_mod

    monkeypatch.setattr(ledger_mod, "_repo_root", lambda _cwd: repo)
    monkeypatch.setattr("bathos.git_pin.append_authoring_manifest", lambda _e, _c: None)

    entry = build_entry(
        doc_kind="claim",
        path=repo / "x.claim.toml",
        workspace_root=repo,
        before_sha256=None,
        after_sha256="a" * 64,
        op="create",
        actor="test",
    )

    with pytest.raises(LedgerAppendError):
        append_authoring_entry(entry, repo)


def test_a_failed_ledger_append_rolls_the_write_back(repo, target, monkeypatch):
    """A document with no record of its provenance is worse than no document."""
    monkeypatch.setattr("bathos.git_pin.append_authoring_manifest", lambda _e, _c: None)

    result = author_claim(valid_payload(), target, workspace_root=repo)

    assert not result.ok
    assert not target.exists(), "a document whose ledger line failed must not survive"


def test_a_failed_append_on_an_amend_restores_the_prior_bytes(repo, target, monkeypatch):
    first = author_claim(valid_payload(), target, workspace_root=repo)
    assert first.ok
    original = target.read_bytes()

    monkeypatch.setattr("bathos.git_pin.append_authoring_manifest", lambda _e, _c: None)
    result = author_claim(
        valid_payload(headline="This revision must not survive"),
        target,
        force=True,
        workspace_root=repo,
    )

    assert not result.ok
    assert target.read_bytes() == original, "rollback must restore the exact prior bytes"
