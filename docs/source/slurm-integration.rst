SLURM Integration
=================

Bathos is designed to work safely with SLURM job arrays on HPC clusters. This guide covers setting up job scripts, running parallel experiments, and syncing results back to your workstation.

Job Script Template
-------------------

Create a SLURM job script that sources the bathos environment helper and runs experiments:

.. code-block:: bash

    #!/bin/bash
    #SBATCH --array=0-99
    #SBATCH --job-name=experiment_batch_1
    #SBATCH --output=outputs/logs/slurm/%j.out
    #SBATCH --error=outputs/logs/slurm/%j.err
    #SBATCH --partition=mit_normal
    #SBATCH --time=01:00:00
    #SBATCH --cpus-per-task=4
    #SBATCH --mem-per-cpu=2GB

    # Source bathos environment setup
    source scripts/slurm/_bth_env.sh

    # Set project slug (overrides .bth.toml if needed)
    export BTH_PROJECT_SLUG="my-research-project"

    # Run experiment with array index
    uv run python scripts/experiments/benchmark_nvt.py \
        --seed $SLURM_ARRAY_TASK_ID \
        --out outputs/runs/$SLURM_ARRAY_TASK_ID

The ``_bth_env.sh`` script sets up the bathos environment:

.. code-block:: bash

    # scripts/slurm/_bth_env.sh
    # Source this in your SLURM job script before calling bth

    # Ensure Python environment is available
    module load python/3.12  # or similar, depends on your cluster

    # Activate uv (if needed)
    which uv > /dev/null || export PATH="$HOME/.local/bin:$PATH"

    # Set catalog location (optional; defaults to ~/.bth/catalog/)
    # export BTH_CATALOG_DIR="/mnt/fast/bathos-catalog"

    # Ensure bathos is installed
    uv tool install bathos

Parallel Job Arrays
-------------------

Use SLURM job arrays to run many experiments in parallel:

.. code-block:: bash

    # Submit array of 100 parallel jobs
    sbatch --array=0-99 scripts/slurm/benchmark.slurm

Each array task (SLURM_ARRAY_TASK_ID) runs independently and:

1. Captures its own run provenance in cool-tier Parquet
2. Writes atomically (no corruption if job is preempted)
3. Does not require locks or central coordination

Bathos guarantees **parallel-safe writes** through atomic write-then-rename semantics.

Monitoring Jobs
---------------

Check job status:

.. code-block:: bash

    squeue -u $USER

View logs for a specific job:

.. code-block:: bash

    tail -f outputs/logs/slurm/<jobid>.out

For run-specific logs, check the bathos run metadata:

.. code-block:: bash

    bth find --pattern "benchmark_nvt" --since 2026-05-15

Syncing Catalog to Cluster
---------------------------

Copy your catalog to the cluster before running batch jobs:

.. code-block:: bash

    bth sync --remote engaging:~/projects/my-research-project

This uploads cool-tier Parquet fragments to the cluster, allowing jobs to reference previous runs.

Downloading Results
-------------------

After batch jobs complete, download results and compact to warm tier:

.. code-block:: bash

    # Download cool-tier fragments from cluster
    bth sync --remote engaging:~/projects/my-research-project --pull

    # Consolidate to warm tier for fast queries
    bth compact

    # Query results
    bth find --status pass --since 2026-05-15

Cluster Workflow Example
------------------------

Here's a complete workflow:

.. code-block:: bash

    # 1. Ensure local project is initialized
    bth init --slug my-research-project

    # 2. Optionally sync previous results from cluster
    bth sync --remote engaging:~/projects/my-research-project --pull
    bth compact

    # 3. Submit batch array to cluster
    sbatch --array=0-99 scripts/slurm/benchmark.slurm

    # 4. Monitor progress (from your laptop)
    squeue

    # 5. After ~30 minutes, download and check results
    bth sync --remote engaging:~/projects/my-research-project --pull
    bth compact

    # 6. Query results
    bth find --pattern benchmark_nvt --status pass

    # 7. Analyze results
    bth sql "SELECT seed, runtime_seconds FROM runs \
             WHERE project_slug='my-research-project' \
             AND status='pass' \
             ORDER BY seed"

Checking Run Validity
---------------------

After syncing runs from the cluster, validate that they are still reproducible:

.. code-block:: bash

    bth check --output validity-report.txt

This reports:

- **VALID:** Run was completed with the current git HEAD (reproducible)
- **STALE:** Run was completed with an older git commit (may not reproduce)
- **UNKNOWN:** Run has no git information (pre-v0.1)

Example output:

.. code-block:: text

    Run ID                          Status    Git SHA             Validity
    ------                          ------    -------             --------
    run_abc123                      pass      a7b8c9d...          VALID
    run_def456                      pass      f1e2d3c...          STALE
    run_ghi789                      fail      a7b8c9d...          VALID

STALE runs can be re-run to ensure reproducibility:

.. code-block:: bash

    git checkout a7b8c9d  # Check out old commit
    bth run scripts/experiments/test_nvt.py

Cluster Configuration
---------------------

Partition Limits
~~~~~~~~~~~~~~~~

Different SLURM partitions have different walltime limits:

.. list-table::
   :header-rows: 1

   * - Partition
     - Max Walltime
     - Usage
   * - ``pi_so3``
     - 48 hours
     - GPU-heavy, selective access
   * - ``mit_normal``
     - 12 hours
     - CPU workloads, most reliable
   * - ``mit_normal_gpu``
     - 6 hours
     - GPU validation, short runs
   * - ``mit_quicktest``
     - 15 minutes
     - Script testing, verification

Request walltime **at least 10% under** the partition maximum to avoid rejection:

.. code-block:: bash

    #SBATCH --partition=mit_normal
    #SBATCH --time=10:00:00   # 10 hours (under 12h limit)

Environment Variables
~~~~~~~~~~~~~~~~~~~~~~

Configure bathos behavior on the cluster:

.. list-table::
   :header-rows: 1

   * - Variable
     - Default
     - Purpose
   * - ``BTH_PROJECT_SLUG``
     - From ``.bth.toml``
     - Override project identifier
   * - ``BTH_CATALOG_DIR``
     - ``~/.bth/catalog/``
     - Override catalog location
   * - ``BTH_SYNC_REMOTE``
     - None
     - Default remote for ``bth sync``

Example:

.. code-block:: bash

    export BTH_PROJECT_SLUG="my-research"
    export BTH_CATALOG_DIR="/mnt/fast/bathos-catalog"
    bth run scripts/experiments/test.py

Troubleshooting
---------------

**Job script fails to find bth command**

Ensure ``uv tool`` is installed and in ``PATH``:

.. code-block:: bash

    which uv
    uv tool install bathos  # Install or update

Or explicitly call with full path:

.. code-block:: bash

    /home/username/.local/bin/bth run scripts/experiments/test.py

**Catalog not found on cluster**

Set ``BTH_CATALOG_DIR`` if the default location is not writable:

.. code-block:: bash

    export BTH_CATALOG_DIR="/work/username/bathos-catalog"
    mkdir -p $BTH_CATALOG_DIR
    bth run ...

**Job preempted, run incomplete**

Bathos writes atomically. If a job is preempted mid-run:

1. A temporary file is written (not yet renamed)
2. On next run, the temporary file is cleaned up (stale)
3. The incomplete run does NOT pollute the catalog

No data loss or corruption.

**Slow queries on cluster**

Run ``bth compact`` on your local machine after syncing:

.. code-block:: bash

    bth sync --remote engaging:~/projects/my-project --pull
    bth compact  # Consolidate cool → warm

Then queries are fast:

.. code-block:: bash

    bth ls  # Fast (warm tier)

Next Steps
----------

- Read :doc:`architecture` to understand tiered storage
- Explore the :doc:`api` for programmatic access
- See :doc:`user-guide` for CLI command details
- Check :doc:`contributing` to help improve bathos
