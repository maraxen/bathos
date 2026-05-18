API Reference
=============

This reference documents all public modules, functions, and classes in the bathos package.

Core Schema
-----------

.. automodule:: bathos.schema
   :members:
   :undoc-members:
   :show-inheritance:

Run provenance dataclass and PyArrow schema definitions.

Catalog (Cool & Warm Tiers)
---------------------------

.. automodule:: bathos.catalog
   :members:
   :undoc-members:
   :show-inheritance:

Parquet and DuckDB catalog I/O operations.

Queries
-------

.. automodule:: bathos.query
   :members:
   :undoc-members:
   :show-inheritance:

Run listing, filtering, and SQL query interface.

CLI
---

.. automodule:: bathos.cli
   :members:
   :undoc-members:
   :show-inheritance:

Typer command-line application and subcommands.

Runner
------

.. automodule:: bathos.runner
   :members:
   :undoc-members:
   :show-inheritance:

Script execution and provenance capture.

Compact (Cool → Warm)
---------------------

.. automodule:: bathos.compact
   :members:
   :undoc-members:
   :show-inheritance:

Cool-tier Parquet fragment compaction into warm-tier DuckDB.

Archive (Warm → Cold)
---------------------

.. automodule:: bathos.archive
   :members:
   :undoc-members:
   :show-inheritance:

Warm-tier DuckDB export to cold-tier partitioned Parquet archive.

Checker
-------

.. automodule:: bathos.checker
   :members:
   :undoc-members:
   :show-inheritance:

Run freshness and git state validation.

Sync
----

.. automodule:: bathos.sync
   :members:
   :undoc-members:
   :show-inheritance:

Catalog synchronization between local and remote (cluster) systems.

Config
------

.. automodule:: bathos.config
   :members:
   :undoc-members:
   :show-inheritance:

Configuration file parsing and defaults.

Git
---

.. automodule:: bathos.git
   :members:
   :undoc-members:
   :show-inheritance:

Git state capture and history introspection.

Init
----

.. automodule:: bathos.init
   :members:
   :undoc-members:
   :show-inheritance:

Project initialization and catalog setup.
