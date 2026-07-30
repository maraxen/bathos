# bathos — Mission

bathos tracks research experiments so you never lose track of what ran, what it produced, or whether results are still valid.

**Design principles:**
- **Local-first:** all state lives at `~/.bth/catalog/` (DuckDB + Parquet). No server. No account.
- **Zero friction:** `bth run scripts/foo.py -- --args` wraps any script without modification.
- **Cross-repo:** one catalog covers all your projects, namespaced by project slug.
- **SLURM-safe:** parallel job arrays write without lock contention.
- **Git-native validity:** every run records the git hash; `bth check` tells you if results are stale.

**The three failure modes bathos eliminates:**
1. "I don't know what command produced this result"
2. "I can't find the output from that run last week"
3. "I don't know if this result is still valid after my code changes"

**What bathos is not:**
- Not a hyperparameter sweep framework
- Not a model registry
- Not a pipeline orchestrator
- Not a team tool — single-researcher design center

**Stack:** Python 3.12+, Typer CLI, DuckDB + Parquet catalog, `uv tool install`.
