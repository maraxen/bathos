"""AC-17 finding (classification b: legacy quirk, NOT fixed here -- see the
task's delivery report for the full write-up).

`campaigns.stopping_threshold` is a `REAL` (32-bit float) warm column
(`compact.py`'s `_CAMPAIGNS_TABLE_SCHEMA` ALTER: `ADD COLUMN IF NOT EXISTS
stopping_threshold REAL`). Once a sequential campaign's threshold is locked
by `link_cool_runs_to_campaigns`, the value is read back ALREADY ROUNDED
(via `get_campaign()`) and round-tripped into the cool JSON snapshot by
`write_campaign_cool` -- so a SECOND `compact(force_rebuild=True)` call on
the SAME, unmodified fixture re-parses the sidecar's exact (float64)
`popper_stopping_threshold` and compares it against the now-rounded stored
value, hits `campaigns.py`'s `sidecar_stopping_threshold != pending_threshold`
mismatch check, and silently NULLs `evalue`/`seq_position` for every member
of that campaign on the second rebuild -- an idempotency bug independent of
anything in `bathos.runlog`: running the identical rebuild twice on
unchanged inputs produces a WORSE result the second time.

This matters for AC-17 (the differential fold test, `tests/runlog/
test_ac17_differential.py`) because the spec's own canonical-state recipe
("Fold rules": `compact(force_rebuild=True)` followed by the reap-ledger
merge of `reconcile_warm_tier`, which force-rebuilds AGAIN internally) is
itself a double rebuild, so this bug fires on the reference computation, not
on anything the new fold does. The AC-17 harness works around it by
rounding every `popper_stopping_threshold` it writes to its nearest exact
float32 value before use (`ac17_harness._f32_safe`), which keeps that test
focused on fold-vs-legacy parity; this file is the narrow, independent repro
of the underlying bug, kept separate so it is never silently rediscovered.

NOT fixed here: changing `stopping_threshold` from `REAL` to `DOUBLE` is a
warm-schema change to `compact.py`, explicitly out of scope for this task
("Do not touch ... legacy compact.py") and a bigger, separate call for the
spec owner (it would need a migration path for every existing catalog).
"""

from __future__ import annotations

from pathlib import Path

import duckdb

from bathos.campaigns import create_campaign
from bathos.catalog import write_run
from bathos.compact import compact
from bathos.schema import Run

SIDECAR = """
[experiment]
hypothesis = "h"

[popper]
null_pass_rate = 0.1
alt_pass_rate = 0.9
stopping_threshold = 0.243

[outcomes.pass]
condition = "true"
decision = "proceed"

[outcomes.fail]
condition = "true"
decision = "stop"

[result_schema]
"""


def test_finding_stopping_threshold_real32_drift_nulls_evalue_on_second_rebuild(
    tmp_catalog: Path,
):
    sidecar_path = tmp_catalog / "r.bth.toml"
    sidecar_path.write_text(SIDECAR)

    compact(tmp_catalog)  # seed the warm schema (empty catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"))
    campaign = create_campaign(db, "c", "proj", "sequential", catalog_dir=tmp_catalog)
    db.close()

    run = Run(
        project_slug="proj",
        command="x",
        argv=["x"],
        git_hash="a",
        git_branch="main",
        git_dirty=False,
        status="completed",
        exit_code=0,
        outcome="fail",
        campaign_id=campaign.id,
        sidecar_path=str(sidecar_path),
    )
    write_run(run, tmp_catalog)

    compact(tmp_catalog)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    threshold_1, evalue_1, seq_1 = db.execute(
        "SELECT c.stopping_threshold, cr.evalue, cr.seq_position "
        "FROM campaigns c JOIN campaign_runs cr ON cr.campaign_id = c.id"
    ).fetchone()
    db.close()

    # First rebuild: computed correctly, but ALREADY rounded through REAL.
    assert evalue_1 == 9.0
    assert seq_1 == 1
    assert threshold_1 != 0.243  # the float32-drift precondition for the bug

    compact(tmp_catalog, force_rebuild=True)
    db = duckdb.connect(str(tmp_catalog / "bathos.db"), read_only=True)
    evalue_2, seq_2 = db.execute("SELECT evalue, seq_position FROM campaign_runs").fetchone()
    db.close()

    # Second rebuild of the SAME, unmodified fixture: silently worse.
    assert evalue_2 is None
    assert seq_2 is None
