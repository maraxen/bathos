User Guide
==========

This guide covers installing bathos, running your first experiment, and using the CLI commands to manage your research catalog.

Installation
------------

Install bathos with ``uv tool``:

.. code-block:: bash

    uv tool install bathos

This installs the ``bth`` command-line tool globally. Verify installation:

.. code-block:: bash

    bth --version
    bth --help

Quick Start
-----------

Initialize a new project:

.. code-block:: bash

    bth init --slug my-project

This creates a `.bth.toml` file in your project root, initializing the experiment catalog.

Run an experiment:

.. code-block:: bash

    bth run scripts/experiments/test_nvt.py --arg1 value1

The CLI captures provenance (timestamp, git state, script path, arguments) and records a run in the local catalog at ``~/.bth/catalog/runs/``.

List all runs:

.. code-block:: bash

    bth ls

Show details of a specific run:

.. code-block:: bash

    bth show <run_id>

Query runs by pattern or status:

.. code-block:: bash

    bth find --pattern "nvt" --status pass

CLI Reference
-------------

**bth init**

Initialize a project catalog.

.. code-block:: bash

    bth init --slug <PROJECT_SLUG> [--root <PATH>]

- ``--slug``: Unique project identifier (required)
- ``--root``: Project root directory (default: current directory)

Creates ``.bth.toml`` with default configuration.

**bth run**

Execute a script and capture provenance.

.. code-block:: bash

    bth run <SCRIPT_PATH> [--arg1 value1] [--out <OUTPUT_DIR>]

- ``SCRIPT_PATH``: Path to Python script to run
- ``--arg*``: Arguments passed to the script
- ``--out``: Output directory for artifacts (default: ``outputs/``)

Returns: run ID, exit code, and metadata.

**bth ls**

List all runs in the catalog.

.. code-block:: bash

    bth ls [--limit 10] [--format json|table]

- ``--limit``: Number of runs to return (default: 10)
- ``--format``: Output format (default: table)

**bth show**

Display full details of a run.

.. code-block:: bash

    bth show <RUN_ID> [--format json|table]

- ``RUN_ID``: Run identifier
- ``--format``: Output format (default: json)

**bth find**

Query runs by pattern and status.

.. code-block:: bash

    bth find [--pattern <PATTERN>] [--status pass|fail|stale] [--since <DATE>]

- ``--pattern``: Filter by script name or project
- ``--status``: Filter by outcome (pass, fail, stale)
- ``--since``: Only runs since date (YYYY-MM-DD)

**bth sql**

Execute raw SQL queries against the catalog.

.. code-block:: bash

    bth sql "SELECT run_id, status FROM runs WHERE status='pass' LIMIT 10"

Direct database access for advanced filtering.

**bth compact**

Compact cool-tier Parquet fragments into warm-tier DuckDB.

.. code-block:: bash

    bth compact [--dry-run]

- ``--dry-run``: Preview compaction without making changes

Moves data from ``~/.bth/catalog/runs/`` (cool) to ``~/.bth/catalog/bathos.db`` (warm).

**bth archive**

Export warm-tier DuckDB to cold-tier partitioned Parquet.

.. code-block:: bash

    bth archive [--project <SLUG>] [--dry-run]

- ``--project``: Limit export to one project (optional)
- ``--dry-run``: Preview export without making changes

Exports to ``~/.bth/catalog/archive/project=<SLUG>/year=<YYYY>/month=<MM>/``.

**bth check**

Validate run freshness and git state.

.. code-block:: bash

    bth check [--output <FILE>]

- ``--output``: Write report to file (default: stdout)

Checks if runs are still valid against current git HEAD. Flags stale runs.

**bth sync**

Synchronize catalog between local machine and cluster.

.. code-block:: bash

    bth sync [--remote <HOST:PATH>] [--pull] [--dry-run]

- ``--remote``: SSH destination for cluster (e.g., ``engaging:~/projects/myproject``)
- ``--pull``: Download from cluster (default: upload)
- ``--dry-run``: Preview without changes

Script Directory Convention
---------------------------

Bathos recognizes script directories with specific naming conventions:

.. list-table::
   :header-rows: 1
   :widths: 20 30 20 20

   * - Directory
     - Purpose
     - Schema enforced
     - Tracked
   * - ``scripts/experiments/``
     - Hypothesis-driven experiments
     - hypothesis + outcomes
     - Yes
   * - ``scripts/benchmarks/``
     - Performance benchmarks
     - baseline + metric
     - Yes
   * - ``scripts/validation/``
     - Property validation
     - property + reference
     - Optional
   * - ``scripts/analysis/``
     - Data analysis
     - none
     - Optional
   * - ``scripts/data/``
     - Data prep and utilities
     - none
     - No
   * - ``scripts/debug/``
     - Debugging and investigation
     - symptom + suspected_cause
     - No

Output Formats
--------------

The ``--format`` flag controls CLI output:

**Table format (default)**

Human-readable ASCII table:

.. code-block:: text

    run_id                              script                    status      created_at
    ----                                ------                    ------      ----------
    run_abc123                          experiments/test_nvt.py   pass        2026-05-18
    run_def456                          experiments/test_nvt.py   fail        2026-05-17

**JSON format**

Structured JSON for scripting:

.. code-block:: json

    [
      {
        "run_id": "run_abc123",
        "script": "experiments/test_nvt.py",
        "status": "pass",
        "created_at": "2026-05-18T12:00:00Z"
      }
    ]

Tips & Troubleshooting
----------------------

**Runs not appearing in catalog**

Verify ``bth init`` was run and ``.bth.toml`` exists:

.. code-block:: bash

    cat .bth.toml

**Slow bth ls / find queries**

Run ``bth compact`` to move cool-tier data to warm-tier DuckDB:

.. code-block:: bash

    bth compact

**Can't run scripts on cluster**

Ensure ``BTH_PROJECT_SLUG`` environment variable is set in your job script:

.. code-block:: bash

    source scripts/slurm/_bth_env.sh
    bth run scripts/experiments/run.py

See :doc:`slurm-integration` for details.

Next Steps
----------

- Read the :doc:`architecture` guide to understand tiered storage
- Configure SLURM integration: :doc:`slurm-integration`
- Explore the :doc:`api` reference
- Contribute to bathos: :doc:`contributing`
