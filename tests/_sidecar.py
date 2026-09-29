"""Test helper: write a myxcel provenance sidecar the way a myxcel push would.

cisternal>=0.1.1a8 only surfaces a sidecar/env sha when the sidecar embeds a tree
manifest (schema v2) that verifies against the files on disk, so tests that expect a
channel sha must stage real pushed content plus its manifest, not just JSON.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from cisternal.provenance import build_tree_manifest

SIDECAR_NAME = ".myxcel_provenance.json"
PUSHED_FILE = "src/app.py"


def sidecar_record(root: Path, sha: str, **overrides: Any) -> dict[str, Any]:
    """A schema-v1 record (no manifest) -- what pre-0.1.1a8 myxcel pushes wrote."""
    record: dict[str, Any] = {
        "schema_version": 1,
        "provenance_status": "git",
        "git_sha": sha,
        "git_branch": "main",
        "git_dirty": False,
        "dirty_content_id": None,
        "capture_stage": "push",
        "sync_state": "verified",
        "computed_at": "2026-08-20T14:00:00Z",
        "provenance_root": str(root),
        "remote": "test",
        "project": "testproj",
        "worktree": None,
        "myxcel_version": "0.1.0",
    }
    record.update(overrides)
    return record


def write_v1_sidecar(root: Path, sha: str, **overrides: Any) -> Path:
    path = root / SIDECAR_NAME
    path.write_text(json.dumps(sidecar_record(root, sha, **overrides)))
    return path


def write_v2_sidecar(root: Path, sha: str, *, matches_commit: bool = True, **overrides: Any) -> Path:
    """Stage one pushed file under `root` and a v2 sidecar whose manifest covers it.

    `matches_commit` stands in for the writer's own attestation: with a fabricated sha
    there is no commit to compare against, so the manifest built here always says False;
    a clean push of a real commit says True, which the reader trusts as written.
    """
    pushed = root / PUSHED_FILE
    pushed.parent.mkdir(parents=True, exist_ok=True)
    pushed.write_text("print('pushed')\n")
    manifest = build_tree_manifest(root, commit=sha, paths=[PUSHED_FILE], extra_excludes=[])
    assert manifest is not None
    manifest_dict = manifest.to_dict()
    manifest_dict["matches_commit"] = matches_commit
    path = root / SIDECAR_NAME
    path.write_text(
        json.dumps(
            sidecar_record(root, sha, schema_version=2, tree_manifest=manifest_dict, **overrides)
        )
    )
    return path
