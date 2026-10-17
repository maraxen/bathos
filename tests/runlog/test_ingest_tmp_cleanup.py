"""A failed ingest must not leave its full-size generation copy of index.db behind.

Found by the independent review of PR #111 (debt #2815): `_ingest_locked` copies index.db to
`index.db.<hex>.tmp`, folds into the copy, then swaps it in. With no cleanup, any exception from
the fold left the copy on disk, and since `ingest_after_write` swallows the exception, a
persistent failure leaked one full index copy per command.
"""

from pathlib import Path

import pytest

from bathos.runlog import ingest
from bathos.runlog.ingest import _ingest_locked, index_db_path


class _FoldBoom(RuntimeError):
    pass


def test_failed_fold_leaves_no_generation_copy_and_live_index_untouched(
    tmp_path: Path, monkeypatch
):
    cd = tmp_path / "catalog"
    cd.mkdir()
    live = index_db_path(cd)
    live.write_bytes(b"live-index-bytes")

    def boom(db_path, roots):
        db_path.write_bytes(b"half-written generation")
        Path(f"{db_path}.wal").write_bytes(b"wal")
        raise _FoldBoom("fold failed")

    monkeypatch.setattr(ingest, "discover_roots", lambda _cd: [])
    monkeypatch.setattr(ingest, "fold_roots_into", boom)

    with pytest.raises(_FoldBoom):
        _ingest_locked(cd)

    assert live.read_bytes() == b"live-index-bytes"
    assert sorted(p.name for p in cd.iterdir()) == [live.name]
