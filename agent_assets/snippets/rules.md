# bathos Rules

**bathos v0.5+** — Experiment tracking and sidecar discipline.

## `bth run` Invocation Pattern

Always invoke scripts via `uv run python`, never plain `python`. This ensures the project's `.venv` is activated and dependency isolation is preserved.

```bash
# ✓ Correct
bth run -- uv run python scripts/experiments/my_experiment.py --seed 42

# ✗ Wrong (will fail with FileNotFoundError on cluster)
bth run -- python scripts/experiments/my_experiment.py --seed 42
```

**Critical for SLURM:** Compute nodes lack global Python; only `uv run` activates the local `.venv`.

## No `--` Separator Unless Script Expects Positional Args

`bth run` arguments end before the script command. Arguments for the script are forwarded as-is. Do NOT use `--` to separate bath args from script args unless the script itself expects positional arguments after named flags.

```bash
# ✓ Correct — smoke test run directly (not via bth run; /tmp is ok for smoke-only validation)
uv run python script.py --smoke --out /tmp/result.json

# ✗ Wrong — script sees an extra "--" argument
bth run -- -- uv run python script.py --smoke --out /tmp/result.json
```

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

Tag runs with `--tag` and group with `--campaign` to organize related experiments:

```bash
bth run --tag "v1.2" --campaign "npt-validation" -- uv run python script.py
```

Campaigns are queryable: `bth campaign ls`, `bth campaign review`.

## When to Use `--no-sidecar`

Treat `--no-sidecar` as an exceptional case. The sidecar enforces pre-registration discipline. Use it only when:
- Developing exploratory code (not a tracked experiment)
- Reproducing an existing result (no new hypothesis)
- Debugging a sidecar validation error

Prefer fixing the sidecar over bypassing it. If the sidecar is hard to write, that's a signal the experiment design needs clarification.

## Output Path Convention

Never pass a temp-directory path to `bth run --out`. Bathos catalogs `--out` paths as durable references.

```bash
# ✓ Correct — persistent, project-relative
bth run --out outputs/result.json -- uv run python scripts/experiments/train.py

# ✗ Wrong — /tmp is ephemeral; catalog entry becomes stale after reboot
bth run --out /tmp/result.json -- uv run python scripts/experiments/train.py
```

Smoke-test validation runs should be executed directly (not via `bth run`) so they are never cataloged:

```bash
# ✓ Correct — smoke test is direct, not tracked
uv run python scripts/experiments/train.py --smoke --out /tmp/test.json
```

`bth lint` will warn if the warm catalog contains runs with ephemeral output paths.
`bth run` will warn at execution time if `--out` points to a temp directory.
