"""AC-17 finding (real divergence, FIXED as part of this delivery -- see the
task's delivery report for the full write-up).

Extending AC-17 to cover `register_claim`/`attest_parity` (delivery item 3)
surfaced a genuine legacy bug, distinguishable from the legacy quirks
elsewhere in this package (which are deliberately left alone and excepted as
BC-1..BC-12): unlike `register_claim`'s own `_claim_bound_legacy_write`
closure (`bathos.claim.register_claim`), `attest_parity`'s legacy write path
(`bathos.claim._attest_parity_impl`'s `_claim_bound_legacy_write`) updated
ONLY the live, already-open `bathos.db` connection's `campaigns.claim_sha256`
column and never called `write_campaign_cool` to persist that change to the
cool-tier campaign JSON.

This was invisible to an ordinary incremental `compact()` (which never
touches an existing warm row), but `compact(catalog_dir, force_rebuild=True)`
-- the AC-17 spec's own "canonical legacy state" recipe (`ac17_harness.
canonical_legacy_state`), and a real recovery path production already
exposes -- deletes `bathos.db` outright and rebuilds `campaigns` purely from
cool JSON (`ingest_cool_campaigns`/`read_cool_campaigns`). Because
`attest_parity`'s claim_sha256 update never reached that JSON, a
force-rebuild silently reverted `claim_sha256` back to whatever
`register_claim` (or nothing) had last written there -- discarding a
completed attestation with no error, exactly the kind of silent-loss bug
this whole epic exists to make impossible.

Fixed here (`bathos.claim`): `attest_parity`/`_attest_parity_impl` now thread
`catalog_dir` through to `_claim_bound_legacy_write`, which persists the
refreshed campaign row via `write_campaign_cool` exactly like
`register_claim`'s own closure already does. This is a legacy (flag-off)
behavior fix, not a runlog/fold change -- the new fold was never affected,
since `campaign.claim_bound` events are append-only and always durable.

This test is the narrow, independent repro (kept separate from the
randomized `test_ac17_differential.py`, whose generator now also exercises
`attest_parity` and would otherwise rediscover this by chance on some seeds
rather than deterministically on every run).
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import pytest

from bathos import claim as claim_ops
from bathos.campaigns import create_campaign
from bathos.catalog import write_run
from bathos.compact import compact
from bathos.schema import Run

from .ac17_harness import _refresh_and_get_db, _safe_close, make_backend


def test_attest_parity_claim_sha256_survives_force_rebuild(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    backend = make_backend(tmp_path, "legacy", is_new=False)
    monkeypatch.setenv("BTH_WORKSPACE_ROOT", str(backend.workspace))

    db = _refresh_and_get_db(backend)
    campaign = create_campaign(
        db, "camp", "proj", "exploration", catalog_dir=backend.catalog_dir, cwd=backend.workspace
    )
    _safe_close(db)

    # A finished, parity-eligible run (attest_parity's AC-12 validation
    # requires outcome in {pass, partial} and parity_run_type=='literature_parity').
    run = Run(
        id="run-parity",
        project_slug="proj",
        command="python x.py",
        argv=["python", "x.py"],
        git_hash="deadbeef",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        outcome="pass",
        parity_run_type="literature_parity",
    )
    write_run(run, backend.catalog_dir)

    db = _refresh_and_get_db(backend)
    claim_path = claim_ops.scaffold_claim(campaign.id, db, backend.workspace)
    claim_ops.register_claim(
        claim_path, campaign.id, db, backend.workspace, catalog_dir=backend.catalog_dir
    )
    _safe_close(db)

    db = _refresh_and_get_db(backend)
    sha_after_register = db.execute(
        "SELECT claim_sha256 FROM campaigns WHERE id=?", [campaign.id]
    ).fetchone()[0]
    _safe_close(db)

    db = _refresh_and_get_db(backend)
    claim_ops.attest_parity(
        campaign.id, "run-parity", db, backend.workspace, catalog_dir=backend.catalog_dir
    )
    sha_after_attest = db.execute(
        "SELECT claim_sha256 FROM campaigns WHERE id=?", [campaign.id]
    ).fetchone()[0]
    _safe_close(db)

    # Precondition: attest_parity really did change the SHA (it binds
    # parity_run_id into the claim's [confounds.reference_parity] block).
    assert sha_after_attest != sha_after_register

    # The regression: a force-rebuild must not lose that change.
    compact(backend.catalog_dir, force_rebuild=True)
    con = duckdb.connect(str(backend.catalog_dir / "bathos.db"), read_only=True)
    try:
        sha_after_rebuild = con.execute(
            "SELECT claim_sha256 FROM campaigns WHERE id=?", [campaign.id]
        ).fetchone()[0]
    finally:
        con.close()

    assert sha_after_rebuild == sha_after_attest, (
        "attest_parity's claim_sha256 update did not survive "
        "compact(force_rebuild=True) -- the cool-tier campaign JSON was never "
        "updated (see module docstring)"
    )
