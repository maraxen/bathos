Contributing to Bathos
======================

Thank you for your interest in contributing to bathos! This guide covers development setup, testing, and the contribution process.

Development Setup
-----------------

Clone the repository:

.. code-block:: bash

    git clone https://github.com/marielle-russo/bathos.git
    cd bathos

Install dependencies with ``uv``:

.. code-block:: bash

    uv sync --dev

This installs bathos in editable mode and all development dependencies (pytest, ruff, pyright).

Running Tests
-------------

Run the full test suite:

.. code-block:: bash

    uv run pytest tests/ -v

Run a specific test:

.. code-block:: bash

    uv run pytest tests/test_schema.py::test_run_dataclass -v

Run with coverage:

.. code-block:: bash

    uv run pytest tests/ --cov=bathos --cov-report=html -v

Then open ``htmlcov/index.html`` to view coverage details.

Run tests matching a pattern:

.. code-block:: bash

    uv run pytest -k "test_compact" -v

Linting & Type Checking
-----------------------

Check code style with ruff:

.. code-block:: bash

    ruff check src/ tests/

Fix style issues automatically:

.. code-block:: bash

    ruff check --fix src/ tests/

Check types with pyright:

.. code-block:: bash

    pyright src/

Before submitting a PR, ensure:

.. code-block:: bash

    ruff check src/ tests/         # 0 violations
    pyright src/                   # 0 errors
    uv run pytest tests/ -v        # All tests pass

Code Style
----------

Bathos follows these conventions:

- **Line length:** 100 characters (configured in ``pyproject.toml``)
- **Python version:** 3.12+
- **Imports:** Alphabetical, standard library → third-party → local
- **Docstrings:** Google style (for Sphinx autodoc)
- **Type hints:** Always annotate function parameters and return types

Example:

.. code-block:: python

    """Module for querying the bathos catalog."""

    from __future__ import annotations

    from typing import Any

    import duckdb

    def find_runs(
        status: str | None = None,
        pattern: str | None = None,
    ) -> list[dict[str, Any]]:
        """Find runs matching criteria.

        Args:
            status: Filter by status (pass, fail, error).
            pattern: Filter by script name pattern.

        Returns:
            List of matching run records.

        Raises:
            ValueError: If status is invalid.
        """
        # Implementation
        pass

Commit Message Conventions
--------------------------

Use conventional commits following this format:

.. code-block:: text

    <type>(<scope>): <description>

    <optional body>

Types:

- **feat:** New feature (e.g., "feat(mcp): add init tool")
- **fix:** Bug fix (e.g., "fix(compact): handle empty cool tier")
- **docs:** Documentation changes (e.g., "docs: add SLURM guide")
- **refactor:** Code refactoring (e.g., "refactor(query): simplify filtering logic")
- **test:** Adding or updating tests (e.g., "test: add edge case for schema v2")
- **perf:** Performance improvements (e.g., "perf(compact): parallelize Parquet writing")
- **chore:** Maintenance (e.g., "chore: update dependencies")

Scope (optional) identifies the component:

- ``cli``, ``catalog``, ``query``, ``runner``, ``mcp``, ``schema``, etc.

Examples:

.. code-block:: text

    feat(mcp): implement ls and find tools

    Add FastMCP implementations for query tools, delegating to
    existing query module. Both tools return paginated JSON arrays.

    Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>

    ---

    fix(runner): handle missing output directory

    Create output directory if it does not exist before running script.
    Fixes #47.

    Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>

Pull Request Process
--------------------

1. **Fork & create a feature branch:**

   .. code-block:: bash

       git checkout -b feat/my-feature

2. **Make changes following code style guide above**

3. **Write tests for new code (test-first development)**

4. **Run the full test suite locally:**

   .. code-block:: bash

       uv run pytest tests/ --cov=bathos -v
       ruff check src/ tests/
       pyright src/

5. **Commit with conventional message:**

   .. code-block:: bash

       git commit -m "feat(module): description

       Optional body explaining the change.

       Co-Authored-By: Claude Haiku 4.5 <noreply@anthropic.com>"

6. **Push and open a PR:**

   .. code-block:: bash

       git push origin feat/my-feature

7. **Address review feedback:**

   - Respond to reviewer comments
   - Push additional commits (do not force-push to main)
   - Request re-review when ready

8. **Merge when approved:**

   Maintainer will merge using GitHub UI with squash or rebase as appropriate.

Testing Guidelines
------------------

All new code must include tests. Follow these practices:

**Test-Driven Development (TDD):**

1. Write a failing test first
2. Implement code to make it pass
3. Refactor while keeping tests green

**Test Organization:**

- One test file per module: ``test_<module>.py``
- Test functions named ``test_<feature>_<scenario>``
- Fixtures in ``tests/conftest.py``

**Example test:**

.. code-block:: python

    def test_compact_consolidates_cool_fragments(tmp_catalog):
        """Test that compact merges cool-tier Parquet into warm-tier DuckDB."""
        # Arrange: Create some mock cool-tier runs
        run1 = Run(run_id="run_1", script="test.py", status="pass")
        run2 = Run(run_id="run_2", script="test.py", status="pass")
        cool_path = tmp_catalog / "runs"
        cool_path.mkdir(parents=True, exist_ok=True)
        write_parquet(cool_path / "run_1.parquet", [run1])
        write_parquet(cool_path / "run_2.parquet", [run2])

        # Act
        count = compact(catalog_dir=str(tmp_catalog))

        # Assert
        assert count == 2
        warm_path = tmp_catalog / "bathos.db"
        assert warm_path.exists()
        rows = duckdb.query(f"SELECT COUNT(*) FROM '{warm_path}'").fetchall()
        assert rows[0][0] == 2

Docstring Requirements
----------------------

All public functions, classes, and modules must have docstrings in Google style:

.. code-block:: python

    def list_runs(
        limit: int = 10,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        """List runs from the warm-tier catalog.

        Args:
            limit: Maximum number of runs to return.
            status: Filter by status (pass, fail, error, unknown).

        Returns:
            List of run dictionaries, most recent first.

        Raises:
            ValueError: If status is not a valid option.
            FileNotFoundError: If catalog directory does not exist.

        Example:
            >>> runs = list_runs(limit=5, status='pass')
            >>> for run in runs:
            ...     print(f"{run['run_id']}: {run['status']}")
        """
        # Implementation
        pass

Sphinx autodoc uses these docstrings to generate API reference. Ensure all public symbols are documented.

Project Structure
-----------------

.. code-block:: text

    bathos/
    ├── src/bathos/          # Main package
    │   ├── __init__.py
    │   ├── cli.py           # Typer CLI app
    │   ├── mcp.py           # FastMCP server (v0.2+)
    │   ├── schema.py        # Run dataclass and PyArrow schema
    │   ├── catalog.py       # Parquet/DuckDB I/O
    │   ├── query.py         # Query interface
    │   ├── runner.py        # Script execution
    │   ├── compact.py       # Cool → warm
    │   ├── archive.py       # Warm → cold
    │   ├── checker.py       # Validation
    │   ├── sync.py          # Rsync wrapper
    │   ├── config.py        # Config parsing
    │   ├── git.py           # Git utilities
    │   └── init.py          # Project init
    ├── tests/               # Test suite
    │   ├── conftest.py      # Shared fixtures
    │   ├── test_*.py        # Test modules
    │   └── fixtures/        # Test data
    ├── docs/                # Sphinx documentation
    │   ├── source/
    │   │   ├── conf.py      # Sphinx config
    │   │   ├── index.rst    # TOC
    │   │   ├── user-guide.rst
    │   │   └── api.rst
    │   └── Makefile
    ├── pyproject.toml       # Project metadata
    ├── README.md
    ├── CHANGELOG.md
    └── LICENSE

Documentation
--------------

Maintain documentation in:

- **README.md:** Quick start and project overview
- **CHANGELOG.md:** Versioned release notes (Keep a Changelog format)
- **docs/source/*.rst:** Sphinx documentation (API reference, user guide, etc.)
- **Code docstrings:** Google style for autodoc

When adding features, update corresponding documentation.

Reporting Issues
----------------

If you find a bug or have a feature request, open an issue on GitHub:

- **Title:** Brief description of the issue
- **Description:** Detailed explanation, steps to reproduce, expected vs. actual behavior
- **Environment:** Python version, OS, bathos version (``bth --version``)
- **Example:** Code or commands that trigger the issue

Code Review Expectations
------------------------

All PRs are reviewed before merging. Reviewers will check:

- Code follows style guide (ruff, pyright pass)
- Tests cover new code (>80% overall coverage)
- Docstrings are present and accurate
- Commit messages follow conventions
- No breaking changes to public API (unless v0.x)

Be respectful and constructive in discussions. If you disagree with feedback, explain your reasoning with evidence.

Questions?
----------

- Open a discussion on GitHub
- Email the maintainer at mariellexr@gmail.com
- Check existing issues and documentation first

Thank you for contributing to bathos!
