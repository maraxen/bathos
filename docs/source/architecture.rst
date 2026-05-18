Architecture & Design
=====================

Bathos uses a three-tier storage architecture to balance write performance, query speed, and long-term archival.

Tiered Storage
--------------

The three tiers serve different access patterns:

.. code-block:: text

    ┌─────────────────────────────────────────────────────┐
    │ HOT: In-Memory Run Object (during execution)        │
    │ - Captures provenance during script execution        │
    │ - Captured at run end (before cleanup)              │
    └─────────────────────────────────────────────────────┘
                            ↓
    ┌─────────────────────────────────────────────────────┐
    │ COOL: Parquet Fragments (~/.bth/catalog/runs/)     │
    │ - Atomic write-then-rename for SLURM parallelism    │
    │ - Minimal schema (13 fields): run_id, script,       │
    │   status, created_at, git_sha, git_branch, etc.    │
    │ - One fragment per run: run_<uuid>.parquet          │
    │ - Fast writes, safe for parallel jobs               │
    └─────────────────────────────────────────────────────┘
                            ↓
                    (bth compact)
                            ↓
    ┌─────────────────────────────────────────────────────┐
    │ WARM: DuckDB Database (~/.bth/catalog/bathos.db)   │
    │ - Consolidated from cool-tier fragments            │
    │ - Full schema + metadata JSON column                │
    │ - Enables fast queries: bth ls, find, sql           │
    │ - Primary interactive query target                  │
    └─────────────────────────────────────────────────────┘
                            ↓
                    (bth archive)
                            ↓
    ┌─────────────────────────────────────────────────────┐
    │ COLD: Partitioned Parquet Archive                  │
    │ (~/.bth/catalog/archive/project=X/year=Y/...)     │
    │ - Partitioned by project, year, month              │
    │ - Historical bulk export                            │
    │ - Sync-able to cluster cold storage                │
    │ - Read-only after export                            │
    └─────────────────────────────────────────────────────┘

Cool Tier (Parquet Fragments)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The cool tier is optimized for **write performance** and **SLURM safety**:

- **Atomic writes:** Each run writes to a temporary file, then renames atomically. This prevents partial writes if a job is preempted mid-execution.
- **Minimal schema:** Only 13 provenance fields, reducing storage and serialization overhead.
- **Parallel-safe:** Multiple SLURM jobs can write to separate fragments simultaneously without locks.
- **Fine-grained:** One fragment per run, enabling lazy compaction.

Cool schema fields:

.. list-table::
   :header-rows: 1

   * - Field
     - Type
     - Description
   * - ``run_id``
     - string
     - Unique run identifier (UUID)
   * - ``script``
     - string
     - Script path (e.g., ``scripts/experiments/test_nvt.py``)
   * - ``status``
     - string
     - Run status: ``pass``, ``fail``, ``error``, ``unknown``
   * - ``exit_code``
     - int
     - Process exit code (0 = success)
   * - ``created_at``
     - timestamp
     - Run start time (ISO 8601)
   * - ``completed_at``
     - timestamp
     - Run end time (ISO 8601)
   * - ``duration_seconds``
     - float
     - Elapsed time
   * - ``git_sha``
     - string
     - Git HEAD commit SHA
   * - ``git_branch``
     - string
     - Git branch name
   * - ``git_dirty``
     - bool
     - True if working tree had uncommitted changes
   * - ``project_slug``
     - string
     - Project identifier (from .bth.toml or env var)
   * - ``schema_version``
     - int
     - Cool schema version (currently 1)
   * - ``metadata_json``
     - string
     - User-provided metadata as JSON (v0.2+)

Warm Tier (DuckDB)
~~~~~~~~~~~~~~~~~~

The warm tier is optimized for **interactive queries**:

- **Consolidated:** Cool fragments are compacted into a single DuckDB file.
- **Extended schema:** Adds ``outcome`` column (populated by outcome evaluation, v0.2+) and ``metadata`` column (parsed JSON from cool tier).
- **Indexed:** DuckDB supports fast filtering on ``status``, ``project_slug``, ``created_at``.
- **SQL queryable:** Native ``bth sql`` support for custom queries.

Example warm-tier query:

.. code-block:: sql

    SELECT run_id, script, status, created_at
    FROM runs
    WHERE project_slug = 'my-project'
      AND status = 'pass'
      AND created_at > '2026-05-01'
    ORDER BY created_at DESC
    LIMIT 10

Cold Tier (Partitioned Parquet)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~

The cold tier is optimized for **historical archival and bulk export**:

- **Partitioned:** Organized as ``project=X/year=YYYY/month=MM/``.
- **Immutable:** Once exported, data is read-only.
- **Sync-able:** Entire cold-tier directory can be rsynced to cluster cold storage.
- **Bulk processing:** Tools like DuckDB, pandas, or Apache Spark can read cold-tier files directly.

Schema Versioning & Migrations
-------------------------------

Bathos tracks schema version in each run to support backward-compatible schema evolution:

Current versions:

- **Cool schema v1:** Defined in ``schema_version=1`` field. 13 fields as above.
- **Warm schema v1:** Same fields as cool v1 + ``outcome`` and ``metadata`` columns added during compaction.

Migration path (v0.1 → v0.2 → ...):

When a new schema version is released, old runs are still readable because each fragment records its ``schema_version``. The compaction tool (``bth compact``) handles schema inference and mapping.

Example: If cool schema v2 adds a new field ``hypothesis_id``, v0.2 compaction will:

1. Read cool v1 fragments → populate new v2 fields with NULL or defaults
2. Write consolidated warm v2 database
3. v1 and v2 coexist during transition period

Git State Capture
-----------------

Every run captures git state for reproducibility:

- **SHA:** Git HEAD commit at run time (``git_sha``)
- **Branch:** Current branch (``git_branch``)
- **Dirty:** Whether working tree had uncommitted changes (``git_dirty``)

This enables:

- Reproducing old experiments: checkout the run's ``git_sha`` and re-run
- Detecting stale runs: ``bth check`` flags runs where the current HEAD is different
- Debugging regressions: query runs by git commit range

SLURM Integration
-----------------

Bathos is designed to work safely with SLURM parallel job arrays:

- **Cool-tier atomic writes:** Ensures each job's run is fully written before marking complete
- **no locks:** Multiple jobs can write simultaneously without contention
- **Lazy compaction:** ``bth compact`` consolidates all cool fragments to warm tier (run separately after jobs finish)
- **Catalog sync:** ``bth sync`` transfers catalog between laptop and cluster

Example SLURM workflow:

.. code-block:: bash

    # Job array runs on cluster
    sbatch --array=0-100 scripts/experiments/run_nvt.slurm

    # Each array task:
    #   source scripts/slurm/_bth_env.sh    (sets BTH_PROJECT_SLUG)
    #   bth run scripts/experiments/nvt.py  (writes cool-tier fragment)

    # After jobs complete, on laptop:
    bth sync --remote engaging:~/projects/myproject  (download cool fragments)
    bth compact                                      (consolidate to warm tier)
    bth find --status pass --since 2026-05-15       (query results)

Catalog Location
----------------

By default, bathos stores the catalog in the user's home directory:

.. code-block:: text

    ~/.bth/catalog/
    ├── runs/              # cool tier (Parquet fragments)
    │   ├── run_abc123.parquet
    │   ├── run_def456.parquet
    │   └── ...
    ├── bathos.db          # warm tier (DuckDB)
    ├── archive/           # cold tier (partitioned Parquet)
    │   └── project=my-project/
    │       └── year=2026/
    │           └── month=05/
    │               └── runs.parquet
    └── config.toml        # catalog configuration

Override with ``BTH_CATALOG_DIR`` environment variable:

.. code-block:: bash

    export BTH_CATALOG_DIR=/mnt/fast/bathos-catalog
    bth run scripts/experiments/test.py

Design Decisions
----------------

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Cool → Warm mechanism | Explicit ``bth compact`` | Decouples write performance from query setup; safe for SLURM parallelism |
| Cool storage format | Parquet (PyArrow) | Language-agnostic; supports append, partition-aware; fast I/O |
| Warm storage format | DuckDB | SQL-queryable; no external server; scales to GBs on single machine; plays nice with Parquet |
| Schema versioning | Field in each run | Supports zero-downtime schema evolution; old runs remain readable |
| SLURM safety | Atomic write-then-rename | Prevents partial fragments if job preempted; no locks needed |
| Catalog location | ``~/.bth/catalog/`` | Per-user, not per-project; enables central archive across all projects |

Next Steps
----------

- See :doc:`slurm-integration` for cluster workflows
- Explore the :doc:`api` reference
- Read :doc:`user-guide` for CLI usage
