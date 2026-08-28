---
name: bathos-cluster
description: SLURM cluster submission, catalog sync, and remote profiles for bathos experiment tracking
triggers: [bth submit, slurm, sbatch, myxcel, cluster, bth sync, bth remote, remote profile]
---

# bathos-cluster

Cluster submission, catalog sync, and remote profile management for bathos (`bth`). Assumes a project already initialized with `bth init` — see **using-bathos** for that and for the core run-tracking / sidecar workflow this builds on.

## Cluster Submission

### Submit Job to SLURM

```bash
bth submit \
  --preset gpu-h200 \
  --array 0-19%4 \
  --then-sync \
  -- bth run uv run python scripts/train.py --epochs 100
```

Submits to SLURM via myxcel, waits for completion, syncs results back. Records `slurm_job_id` and `slurm_array_task_id` in run record.

**Options:**
- `--preset NAME` — SLURM preset (gpu, gpu-h200, cpu, quicktest, etc.)
- `--remote NAME` — Override myxcel remote (default: from `.bth.toml`)
- `--array SPEC` — SLURM array spec (e.g., `0-9%4`)
- `--dependency SPEC` — SLURM dependency (e.g., `afterok:12345`)
- `--name NAME` — Job name
- `--push-first / --no-push-first` — Push project before submit (default: push)
- `--wait / --no-wait` — Block until completion (default: no-wait)
- `--then-pull` — Pull results after completion (implies `--wait`)
- `--then-sync` — Run `bth sync` after pull (implies `--then-pull --wait`)

Exit codes: 0 = success or no-wait, 1 = job failure, 2 = timeout.

`bth submit` also enforces the reproduction-prerequisite gate declared in a sidecar's `[reproduction].requires_pass_stem` field — see **using-bathos**'s Controls Discipline section for the gate's exact hard/advisory/skip behavior by `stage_name`.

### Override Preset in Sidecar

```toml
[cluster]
preset = "gpu-h200"
remote = "engaging"
project = "myproject"
```

Sidecar `[cluster]` section overrides `.bth.toml` defaults. CLI flags override sidecar.

## Sync Catalog

### Push to Remote

```bash
bth sync engaging
```

Runs `rsync` over SSH directly (not via myxcel) to push the cool-tier catalog — both
`{catalog}/runs/<project>/*.parquet` and `{catalog}/campaigns/*.json` cool-JSON campaign
records — to the remote's `{remote_root}/.bth/catalog`. Only syncs current project (v0.4+).
`bathos.db` (the warm DuckDB) is deliberately never rsynced.

### Pull from Remote

```bash
bth sync engaging --pull
```

Fetches run Parquet fragments and campaign cool-JSON from the cluster and merges them into
the local catalog. `bth campaign show`/`list` union cool JSON with `bathos.db` at read time,
so newly-pulled campaigns show up immediately; `bth find`/SQL-level queries against the warm
tier need `bth compact` first to fold new cool-tier data (runs and campaigns) into `bathos.db`.

### Full Workflow

```bash
bth sync engaging --pull      # Get latest cluster results (runs + campaign JSON)
bth find --slurm-job 12345    # Query locally
```

## Remote Profiles

### Add Remote

```bash
bth remote add engaging engaging.csail.mit.edu:~/projects/myproject
```

`bth remote add <name> <host>:<path>` — two positional arguments in `host:path` form, not
`--host`/`--path` flags. Registers the cluster host for sync and submission, and
(re)writes `scripts/slurm/_bth_env.sh` so cluster jobs export `BTH_CATALOG_DIR` pointing at
`{remote_root}/.bth/catalog`. `bth submit` checks this file matches the configured remote
and hard-fails with a `CatalogIdentityError` if it doesn't — re-run `bth remote add` after
hand-editing a remote's path in `.bth.toml`.

### List Remotes

```bash
bth remote list
```

Shows configured remotes. (There's no `ls` alias — `bth remote ls` errors with
"No such command 'ls'. Did you mean 'list'?".)


### Test Connectivity

```bash
bth remote test engaging
```

Verifies SSH access to remote.

## Related

- **using-bathos** — run tracking, sidecar pre-registration, controls discipline, catalog queries
- **bathos-campaigns** — campaigns and claim-tier rigor for runs submitted here
- **Cluster rules**: `~/.claude/rules/CLUSTER.md` — SLURM partition limits, job submission, local validation gates
