# bathos Rules

**bathos (`bth`)** — Experiment tracking and sidecar discipline. Flags below were checked against `bth run --help` on bathos 0.13.0a6 (2026-10-16). Shipped as a reference of the `using-bathos` skill via `bth plugin update` (moved from the praxia-rendered `agent_assets/snippets/rules.md`, 261016).

## `bth run` Invocation Pattern

Always invoke scripts via `uv run python`, never plain `python`. This ensures the project's `.venv` is activated and dependency isolation is preserved. Add `--no-sync` (a bare `uv run` silently re-syncs and reverts hand-installed wheels).

```bash
# ✓ Correct
bth run --project-slug myproject -- uv run --no-sync python3 scripts/experiments/my_experiment.py --seed 42

# ✗ Wrong (will fail with FileNotFoundError on cluster)
bth run -- python scripts/experiments/my_experiment.py --seed 42
```

Run it as `bth`, never `uv run bth`: with a project-pinned bathos that predates enforcement, `uv run bth` silently skips sidecar enforcement and exits 0. After the run, check the record, not the exit code: `bth sql "SELECT id, status, outcome, exit_code FROM runs WHERE id LIKE '<prefix>%'"` (run `bth compact` first if the run is missing). `outcome = 'unknown'` means no sidecar was bound or no result was read, so the run is not a pre-registered finding.

**Critical for SLURM:** Compute nodes lack global Python; only `uv run` activates the local `.venv`.

## One `--` Before the Wrapped Command

`bth run` options (`--project-slug`, `--output-paths`, `--tags`, `--campaign-id`, `--no-sidecar`) come first. A single `--` then ends them and starts the command to run; that command's own flags (`--no-sync`, `--seed 42`) are forwarded to it untouched. A second `--` is not a separator for the script: the script receives it as an argument.

```bash
# ✓ Correct — bth options, one `--`, then the command with its own flags
bth run --project-slug myproject --output-paths outputs/result.json -- uv run --no-sync python3 scripts/experiments/train.py --seed 42

# ✗ Wrong — the second `--` is passed to the script as an argument
bth run -- -- uv run python scripts/experiments/train.py --seed 42
```

Smoke tests do not go through `bth run` at all (see Output Path Convention below).

## Sidecar Outcome Conditions: Valid DuckDB SQL

Outcome `condition` fields are evaluated as DuckDB SQL (`SELECT (<condition>) FROM _dummy LIMIT 0`). DuckDB does NOT support Python-style chained comparisons.

```toml
# ✓ Correct — uses AND
[outcomes.marginal]
condition = "temp_std >= 5 AND temp_std < 10"

# ✗ Wrong — Python-style chaining fails
[outcomes.marginal]
condition = "5 <= temp_std < 10"
```

## `bth sync` Ownership

`bth sync` delegates to myxcel for remote transfer. Do NOT call `rsync` directly. Use `bth sync` subcommands (`--push`, `--pull`, `--list`) and respect the remote root configured in `.bth.toml`.

## Campaign and Run Recording

Tag runs with `--tags` and group with `--campaign-id` to organize related experiments:

```bash
bth run --tags "v1.2" --campaign-id "npt-validation" -- uv run python script.py
```

Campaigns are queryable: `bth campaign ls`, `bth campaign review`.

## When to Use `--no-sidecar`

Treat `--no-sidecar` as an exceptional case. The sidecar enforces pre-registration discipline. Use it only when:
- Developing exploratory code (not a tracked experiment)
- Reproducing an existing result (no new hypothesis)
- Debugging a sidecar validation error

Prefer fixing the sidecar over bypassing it. If the sidecar is hard to write, that's a signal the experiment design needs clarification.

## Output Path Convention

Never pass a temp-directory path to `bth run --output-paths`. Bathos catalogs `--output-paths` paths as durable references.

```bash
# ✓ Correct — persistent, project-relative
bth run --output-paths outputs/result.json -- uv run python scripts/experiments/train.py

# ✗ Wrong — /tmp is ephemeral; catalog entry becomes stale after reboot
bth run --output-paths /tmp/result.json -- uv run python scripts/experiments/train.py
```

Smoke-test validation runs should be executed directly (not via `bth run`) so they are never cataloged:

```bash
# ✓ Correct — smoke test is direct, not tracked
uv run python scripts/experiments/train.py --smoke --out /tmp/test.json
```

`bth lint` will warn if the warm catalog contains runs with ephemeral output paths.
`bth run` will warn at execution time if `--output-paths` points to a temp directory.
