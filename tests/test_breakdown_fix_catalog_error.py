# ruff: noqa: F811
"""Remediation group 4: the claim-binding read in `_add_run_to_campaign_impl` tolerates only a
catalog that predates claim registration (missing column); any other DuckDB error propagates."""

from __future__ import annotations

import duckdb
import pytest

from bathos.campaigns import add_run_to_campaign
from tests.test_breakdown_gate import (  # noqa: F401
    _OFFENDING,
    _bound_campaign,
    _db,
    _record_run,
    _write_sidecar,
    cat,
)


class _Flaky:
    """A connection whose claim_path read fails with `error`."""

    def __init__(self, db, error: Exception) -> None:
        self._db = db
        self._error = error

    def execute(self, sql, *args, **kwargs):
        if "SELECT claim_path FROM campaigns" in sql:
            raise self._error
        return self._db.execute(sql, *args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._db, name)


def _setup(cat, tmp_path):
    sidecar = _write_sidecar(tmp_path, _OFFENDING)
    campaign = _bound_campaign(cat, tmp_path)
    run = _record_run(cat, campaign, sidecar, sidecar_text=_OFFENDING)
    return campaign, run


def test_a_non_catalog_duckdb_error_propagates(cat, tmp_path):
    campaign, run = _setup(cat, tmp_path)
    db = _db(cat)
    with pytest.raises(duckdb.IOException):
        add_run_to_campaign(_Flaky(db, duckdb.IOException("disk gone")), campaign.id, run.id, cat)


def test_a_missing_claim_path_column_means_nothing_is_claim_bound(cat, tmp_path):
    campaign, run = _setup(cat, tmp_path)
    db = _db(cat)
    err = duckdb.BinderException('Referenced column "claim_path" not found in FROM clause!')
    add_run_to_campaign(_Flaky(db, err), campaign.id, run.id, cat)  # not gated: no claim column
    assert db.execute("SELECT COUNT(*) FROM campaign_runs").fetchone()[0] == 1
