# bth remote — Remote Management Subcommand Group

> **For agentic workers:** Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `bth remote` subcommand group (add / list / remove / test) and make the `remote` argument to `bth sync` optional with auto-selection when exactly one remote is configured.

**Architecture:** New pure-logic module `src/bathos/remote.py` handles all TOML read/write using `tomlkit` (preserves existing formatting and comments). CLI wiring lives in `src/bathos/cli.py` as a Typer sub-app registered with `app.add_typer`. The `.bth.toml` `[remotes.*]` schema is already consumed by `load_project_config`; this spec only adds the write path.

**Tech Stack:** Python 3.12, Typer, tomlkit, uv

---

## File Map

| Action | Path |
|--------|------|
| Modify | `pyproject.toml` |
| Create | `src/bathos/remote.py` |
| Modify | `src/bathos/cli.py` |
| Create | `tests/test_remote.py` |

---

## Data Contract

After `bth remote add engaging engaging:~/projects/bathos`, `.bth.toml` gains:

```toml
[remotes.engaging]
host = "engaging"
remote_root = "~/projects/bathos"
```

This is the format already present in the project's `.bth.toml` and already consumed by `load_project_config`: `data.get("remotes", {})` returns `{"engaging": {"host": "engaging", "remote_root": "~/projects/bathos"}}`, stored verbatim on `ProjectConfig.remotes`. `sync_catalog` reads `remote_config["host"]` and `remote_config["remote_root"]`. No changes to `config.py` or `sync.py` are required.

**URL parsing rule:** split on the **first** `:` only. `engaging:~/projects/bathos` → `host="engaging"`, `path="~/projects/bathos"`. `user@host.example.com:~/path` → `host="user@host.example.com"`, `path="~/path"`. Implementation: `host, path = url.split(":", 1)`.

---

## Dependency Change

Add `"tomlkit>=0.12"` to `[project] dependencies` in `pyproject.toml`. `tomlkit` preserves TOML comments, blank lines, and key ordering on round-trip, which is required so that `bth remote add` does not destroy existing `.bth.toml` formatting.

---

## Error Taxonomy

| Exception | Raised by | When |
|-----------|-----------|------|
| `FileNotFoundError` | `add_remote`, `remove_remote` | `config_path` does not exist |
| `ValueError(f"Remote '{name}' already exists")` | `add_remote` | `name` is already in `[remotes]` |
| `ValueError(f"Remote '{name}' not found")` | `remove_remote`, `test_remote` | `name` not in config |
| (no exception) | `test_remote` | SSH failure is captured into `TestResult.error`, never raised |

---

## Module: `src/bathos/remote.py`

### Dataclasses

```python
from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import tomlkit

from bathos.config import ProjectConfig


@dataclass
class TestResult:
    success: bool
    latency_ms: float | None  # None when success is False
    error: str                 # empty string when success is True
```

### `add_remote`

```python
def add_remote(config_path: Path, name: str, host: str, path: str) -> None:
```

**Reads:** Opens `config_path` with `tomlkit.load()`.

**Behavior:**
- Raises `FileNotFoundError` if `config_path` does not exist.
- Raises `ValueError(f"Remote '{name}' already exists")` if `doc["remotes"][name]` is already present. Does **not** silently overwrite; callers wanting to update must `remove_remote` then `add_remote`.
- Creates `doc["remotes"]` table if the key is absent.
- Writes a `[remotes.<name>]` table with exactly two keys in order: `host`, `remote_root`.
- Writes back atomically: write to `config_path.with_suffix(".tmp")`, then `Path.replace(config_path)`.

**Returns:** `None`.

### `list_remotes`

```python
def list_remotes(config: ProjectConfig) -> list[tuple[str, str, str]]:
```

**Reads:** `config.remotes` (already-parsed dict). **No I/O.**

**Returns:** `list[tuple[name, host, remote_root]]` sorted alphabetically by `name`. Empty list when `config.remotes` is empty.

### `remove_remote`

```python
def remove_remote(config_path: Path, name: str) -> None:
```

**Reads:** Opens `config_path` with `tomlkit.load()`.

**Behavior:**
- Raises `FileNotFoundError` if `config_path` does not exist.
- Raises `ValueError(f"Remote '{name}' not found")` if `name` is not in `doc["remotes"]`.
- Deletes `doc["remotes"][name]`.
- If `doc["remotes"]` is empty after deletion, removes the `[remotes]` key entirely.
- Atomic write (same tmp-then-replace pattern as `add_remote`).

**Returns:** `None`.

### `test_remote`

```python
def test_remote(config: ProjectConfig, name: str) -> TestResult:
```

**Reads:** `config.remotes[name]["host"]`.

**Behavior:**
- Raises `ValueError(f"Remote '{name}' not found")` if `name` not in `config.remotes`.
- Runs: `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "echo", "ok"]` via `subprocess.run(cmd, capture_output=True, text=True, timeout=10)`.
- Records wall-clock elapsed time around the call.
- If `returncode == 0` and `stdout.strip() == "ok"`: returns `TestResult(success=True, latency_ms=elapsed*1000, error="")`.
- Otherwise: returns `TestResult(success=False, latency_ms=None, error=stderr.strip() or stdout.strip())`.
- `subprocess.TimeoutExpired` is caught: returns `TestResult(success=False, latency_ms=None, error="Connection timed out after 10s")`.

**Does not raise** on SSH failure.

---

## CLI: `src/bathos/cli.py`

### Sub-app registration

Add near the top of `cli.py` (after `app = typer.Typer(...)` is defined):

```python
remote_app = typer.Typer(help="Manage remote hosts for sync.")
app.add_typer(remote_app, name="remote")
```

All four remote commands are `@remote_app.command()` decorated. Imports from `bathos.remote` are lazy (inside each function body), consistent with the existing pattern in `cli.py`.

### `bth remote add <name> <url>`

```python
@remote_app.command("add")
def remote_add(
    name: str = typer.Argument(..., help="Remote name (e.g. 'engaging')"),
    url: str = typer.Argument(..., help="host:path (e.g. 'engaging:~/projects/myproject')"),
) -> None:
    """Add a remote host for sync."""
```

Logic:
1. `find_project_config()` → `"No .bth.toml found. Run 'bth init' first."` + exit 1 if None.
2. If `":"` not in `url` → `"Invalid URL '{url}': expected 'host:path' format"` + exit 1.
3. `host, path = url.split(":", 1)`.
4. `add_remote(cfg_path, name, host, path)`.
5. On `ValueError` → print `str(e)` + exit 1.

Success output: `Remote '{name}' added ({host}:{path})`

### `bth remote list`

```python
@remote_app.command("list")
def remote_list() -> None:
    """List configured remotes."""
```

Logic:
1. `find_project_config()` → error + exit 1 if None.
2. `load_project_config(cfg_path)` → `list_remotes(config)`.
3. If empty: `"No remotes configured. Use 'bth remote add' to add one."` → exit 0.
4. Otherwise print table:

```
NAME        HOST:PATH
----------  --------------------------
engaging    engaging:~/projects/bathos
```

Column widths are right-padded to the maximum value in each column (minimum 10 for NAME, minimum 9 for HOST:PATH). Separator line uses `-` characters matching each column's width.

Exit code: 0 always.

### `bth remote remove <name>`

```python
@remote_app.command("remove")
def remote_remove(
    name: str = typer.Argument(..., help="Remote name to remove"),
) -> None:
    """Remove a configured remote."""
```

Logic:
1. `find_project_config()` → error + exit 1 if None.
2. `remove_remote(cfg_path, name)`.
3. On `ValueError` → print `str(e)` + exit 1.

Success output: `Remote '{name}' removed.`

Exit codes: 0 on success, 1 on not found.

### `bth remote test <name>`

```python
@remote_app.command("test")
def remote_test(
    name: str = typer.Argument(..., help="Remote name to test"),
) -> None:
    """Test SSH connectivity to a remote."""
```

Logic:
1. `find_project_config()` → error + exit 1 if None.
2. `load_project_config(cfg_path)`.
3. `test_remote(config, name)` — on `ValueError` → print `str(e)` + exit 1.
4. If `result.success`: `"{name}: ok ({result.latency_ms:.0f}ms)"` → exit 0.
5. If not: `"{name}: unreachable — {result.error}"` → exit 1.

---

## CLI: Updated `bth sync`

Change `remote` from required positional to optional positional:

```python
@app.command()
def sync(
    remote: str | None = typer.Argument(None, help="Remote name from .bth.toml (auto-selected if only one configured)"),
    pull: bool = typer.Option(False, "--pull", help="Pull from remote (default: push)"),
) -> None:
    """Sync cool-tier catalog to/from remote."""
```

Insert the following auto-selection block **after** `config = load_project_config(cfg_path)` and **before** `sync_catalog(...)`:

```python
if remote is None:
    remotes = list(config.remotes.keys())
    if len(remotes) == 0:
        typer.echo("No remotes configured. Use 'bth remote add' to add one.", err=True)
        raise typer.Exit(1)
    elif len(remotes) == 1:
        remote = remotes[0]
    else:
        names = ", ".join(f"'{r}'" for r in sorted(remotes))
        typer.echo(f"Multiple remotes configured ({names}). Specify one explicitly.", err=True)
        raise typer.Exit(1)
```

---

## Exit Code Table

| Command | Condition | Exit |
|---------|-----------|------|
| `bth remote add` | Success | 0 |
| `bth remote add` | No `.bth.toml` | 1 |
| `bth remote add` | Invalid URL format | 1 |
| `bth remote add` | Name already exists | 1 |
| `bth remote list` | Success or empty | 0 |
| `bth remote list` | No `.bth.toml` | 1 |
| `bth remote remove` | Success | 0 |
| `bth remote remove` | No `.bth.toml` | 1 |
| `bth remote remove` | Name not found | 1 |
| `bth remote test` | Reachable | 0 |
| `bth remote test` | Unreachable or timeout | 1 |
| `bth remote test` | Name not found | 1 |
| `bth sync` (no arg) | Exactly one remote → auto-selected | 0 |
| `bth sync` (no arg) | Zero remotes | 1 |
| `bth sync` (no arg) | Multiple remotes | 1 |
| `bth sync <name>` | Name not found | 1 |
| `bth sync <name>` | rsync failure | 1 |

---

## CLI Output Examples (exact strings)

```
$ bth remote add engaging engaging:~/projects/bathos
Remote 'engaging' added (engaging:~/projects/bathos)

$ bth remote add engaging engaging:~/projects/bathos   # duplicate
Remote 'engaging' already exists

$ bth remote add badurl nocolon
Invalid URL 'nocolon': expected 'host:path' format

$ bth remote list
NAME        HOST:PATH
----------  --------------------------
engaging    engaging:~/projects/bathos

$ bth remote list   # no remotes
No remotes configured. Use 'bth remote add' to add one.

$ bth remote remove engaging
Remote 'engaging' removed.

$ bth remote remove nosuchhost
Remote 'nosuchhost' not found

$ bth remote test engaging   # success
engaging: ok (42ms)

$ bth remote test engaging   # unreachable
engaging: unreachable — ssh: Could not resolve hostname engaging: Name or service not known

$ bth remote test engaging   # timeout
engaging: unreachable — Connection timed out after 10s

$ bth sync   # one remote configured
Pushed 3 runs to/from 'engaging' in 0.8s

$ bth sync   # two remotes: engaging, psc
Multiple remotes configured ('engaging', 'psc'). Specify one explicitly.

$ bth sync   # no remotes
No remotes configured. Use 'bth remote add' to add one.
```

---

## Fixer Task Decomposition

### Task 1: Add `tomlkit` dependency
- [ ] Add `"tomlkit>=0.12"` to `[project] dependencies` in `pyproject.toml`.
- [ ] Run `uv sync` to update the lockfile.
- [ ] Verify: `uv run python -c "import tomlkit; print(tomlkit.__version__)"` exits 0.

### Task 2: Create `src/bathos/remote.py` — `TestResult` + `list_remotes`
- [ ] Create `src/bathos/remote.py` with `TestResult` dataclass and `list_remotes` only.
- [ ] Create `tests/test_remote.py`. Test: empty config → `[]`; two remotes → sorted tuples.
- [ ] Verify: `uv run pytest tests/test_remote.py` passes; full suite passes.

### Task 3: Implement `add_remote`
- [ ] Implement `add_remote(config_path, name, host, path) -> None`.
- [ ] Atomic write via tmp file + `Path.replace`.
- [ ] Tests: adds remote, round-trips comments, raises on duplicate, raises on missing file.
- [ ] Verify: full suite passes.

### Task 4: Implement `remove_remote`
- [ ] Implement `remove_remote(config_path, name) -> None`.
- [ ] Remove empty `[remotes]` table after deletion.
- [ ] Tests: removes remote, empty-table cleanup, raises on missing name, raises on missing file.
- [ ] Verify: full suite passes.

### Task 5: Implement `test_remote`
- [ ] Implement `test_remote(config, name) -> TestResult`.
- [ ] SSH: `["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5", host, "echo", "ok"]`, `timeout=10`.
- [ ] Tests using `unittest.mock.patch("subprocess.run")`: success, failure, timeout, name-not-found.
- [ ] Verify: full suite passes.

### Task 6: Wire `bth remote` sub-app into `cli.py`
- [ ] Add `remote_app = typer.Typer(...)` and `app.add_typer(remote_app, name="remote")`.
- [ ] Implement `remote_add`, `remote_list`, `remote_remove`, `remote_test` commands.
- [ ] Verify: `uv run bth remote --help` lists four subcommands.
- [ ] Verify: full suite passes.

### Task 7: Update `bth sync` — optional remote with auto-selection
- [ ] Change `sync` signature: `remote: str | None = typer.Argument(None, ...)`.
- [ ] Insert auto-selection block.
- [ ] Verify: full suite passes.

### Task 8: CLI integration tests
- [ ] Add CLI tests using `typer.testing.CliRunner` with tmp `.bth.toml`.
- [ ] Cover: `remote add` success/duplicate; `remote list` empty/populated; `remote remove` success/missing; `bth sync` with zero/one/two remotes.
- [ ] Verify: full suite passes.
