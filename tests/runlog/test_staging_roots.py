"""debt #1994: the staging-tree traversal lives once, in `ingest.staging_roots_for_attempt`.

`migrate._staging_roots_from` (the `--dry-run` seam that points the fold at a scratch tree)
used to carry its own copy; it now delegates, so the two cannot drift.
"""

from pathlib import Path

from bathos.runlog.ingest import staging_roots_for_attempt
from bathos.runlog.migrate import _staging_roots_from


def _tree(root: Path) -> None:
    (root / "unaffiliated").mkdir(parents=True)
    (root / "project" / "pid-a").mkdir(parents=True)
    (root / "project" / "pid-b").mkdir(parents=True)
    (root / "project" / "stray-file").write_text("not a directory")
    (root / "loose-file").write_text("not a directory")


def test_staging_roots_override_walks_the_given_tree(tmp_path: Path):
    staging = tmp_path / "attempt-1"
    _tree(staging)

    roots = staging_roots_for_attempt("attempt-1", staging_root=staging)

    assert roots == [
        ("staging", "attempt-1/project/pid-a", staging / "project" / "pid-a"),
        ("staging", "attempt-1/project/pid-b", staging / "project" / "pid-b"),
        ("staging", "attempt-1/unaffiliated", staging / "unaffiliated"),
    ]


def test_migrate_helper_delegates_to_ingest(tmp_path: Path):
    staging = tmp_path / "attempt-2"
    _tree(staging)

    assert _staging_roots_from(staging, "attempt-2") == staging_roots_for_attempt(
        "attempt-2", staging_root=staging
    )


def test_missing_staging_root_is_empty(tmp_path: Path):
    assert staging_roots_for_attempt("nope", staging_root=tmp_path / "absent") == []
    assert _staging_roots_from(tmp_path / "absent", "nope") == []
