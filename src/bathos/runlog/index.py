"""Re-export shim: the real implementation moved to `bathos.index` (delivery
step 3, wave d), the module name the spec uses for AC-18's exempt read API
(`bathos.index.connect_read` / `bathos.index.connect_legacy`).

Kept so pre-existing imports (`bathos.runlog.ingest`, a couple of runlog
tests) do not need to change; new code should `import bathos.index` directly.
"""

from __future__ import annotations

from bathos.index import (
    ARCHIVED_ITEMS_COLUMNS,
    ARCHIVED_ITEMS_DDL,
    BLAST_RADIUS_LEDGER_COLUMNS,
    BLAST_RADIUS_LEDGER_DDL,
    CAMPAIGN_EDGES_COLUMNS,
    CAMPAIGN_EDGES_DDL,
    CAMPAIGN_RUNS_COLUMNS,
    CAMPAIGN_RUNS_DDL,
    CAMPAIGNS_COLUMNS,
    CAMPAIGNS_DDL,
    EVENTS_DDL,
    QUARANTINE_DDL,
    RUN_EDGES_COLUMNS,
    RUN_EDGES_DDL,
    RUNS_COLUMNS,
    RUNS_DDL,
    SIDECAR_ANCHORS_COLUMNS,
    SIDECAR_ANCHORS_DDL,
    SUBMITS_COLUMNS,
    SUBMITS_DDL,
    TRUST_LEDGER_COLUMNS,
    TRUST_LEDGER_DDL,
    WATERMARKS_DDL,
    catalog_readable,
    connect_legacy,
    connect_read,
    index_db_path,
    init_index_schema,
)

__all__ = [
    "ARCHIVED_ITEMS_COLUMNS",
    "ARCHIVED_ITEMS_DDL",
    "BLAST_RADIUS_LEDGER_COLUMNS",
    "BLAST_RADIUS_LEDGER_DDL",
    "CAMPAIGNS_COLUMNS",
    "CAMPAIGNS_DDL",
    "CAMPAIGN_EDGES_COLUMNS",
    "CAMPAIGN_EDGES_DDL",
    "CAMPAIGN_RUNS_COLUMNS",
    "CAMPAIGN_RUNS_DDL",
    "EVENTS_DDL",
    "QUARANTINE_DDL",
    "RUNS_COLUMNS",
    "RUNS_DDL",
    "RUN_EDGES_COLUMNS",
    "RUN_EDGES_DDL",
    "SIDECAR_ANCHORS_COLUMNS",
    "SIDECAR_ANCHORS_DDL",
    "SUBMITS_COLUMNS",
    "SUBMITS_DDL",
    "TRUST_LEDGER_COLUMNS",
    "TRUST_LEDGER_DDL",
    "WATERMARKS_DDL",
    "catalog_readable",
    "connect_legacy",
    "connect_read",
    "index_db_path",
    "init_index_schema",
]
