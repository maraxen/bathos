# bathos Strategic Design: Agentic Science and the Audit Substrate

*Field synthesis for implementation planning. Solo researcher context; Claude increasingly involved in orchestrating SLURM experiments. Synthesized from NotebookLM research (2026-05-20, ~144 sources, notebook 3f0490aa-7c70-4cd5-bfe7-94c3bd4a041f).*

---

## Section 1 — Field Landscape

The trajectory of scientific workflow tooling has two parallel tracks that have not yet converged.

The first track is **computational provenance systems**: VisTrails, Kepler, Nextflow, Snakemake, CWL Prov, RO-Crate. These systems answer "what ran, with what parameters, in what order, on what inputs." They are execution graphs. They capture how a result was produced but say nothing about whether the result was the one being sought, whether the experiment stayed on-hypothesis, or whether reported conclusions match the logged outcomes. RO-Crate and CWL Prov are the most rigorous, but they require significant infrastructure investment — a solo researcher will not maintain a provenance server.

The second track is **pre-registration and scientific integrity research**. This body of literature finds that basic pre-registration (OSF-style open timestamping with no verification mechanism) has near-zero empirical effect on p-hacking. What does work: complete Pre-Analysis Plans with forced articulation before data collection, reviewer-verified stage-locking (Registered Reports), and cryptographic binding of analysis decisions to execution records. The gap between these two tracks is **experiment nonrepudiation** — no existing system binds a reported number to the declared intent under which it was collected, in a way that is tamper-evident and machine-queryable.

The third development is **agentic science**: AI Scientist (Lu et al. 2024) and self-driving labs (Aspuru-Guzik group, Emerald Cloud Lab). These systems close the hypothesis → experiment → result → refinement loop automatically. They reveal a new failure mode that pure computational provenance systems cannot detect: **implementation drift**, where an agent silently abandons the declared hypothesis under execution pressure and reports success anyway. The literature calls this "overexcitement." When Claude orchestrates a SLURM experiment by calling `bth run`, that failure mode is already live.

What is missing across all three tracks: a lightweight, serverless layer that simultaneously captures computational provenance, links execution to a declared hypothesis, and provides a machine-queryable audit trail that can detect when an agent (human or AI) drifted from its stated intent.

---

## Section 2 — Where bathos Fits

bathos is not trying to be Nextflow (execution orchestration), OSF (public registry with peer review), or MLflow (model registry with experiment dashboards). Its actual position is more specific and more defensible: it is the **audit substrate for agentic experiment orchestration** targeting a solo researcher at SLURM scale.

Three existing properties, combined, define this position:

**Tiered serverless storage** (cool Parquet → warm DuckDB → cold archive) makes bathos SLURM-safe without a daemon. Every parallel job array can write atomically to the cool tier. This solves the fundamental infrastructure problem that prevents RO-Crate and Sacred from working in HPC contexts.

**Sidecar pre-registration** (`.bth.toml` with `[outcomes]` DuckDB SQL conditions) means hypothesis and confirmatory tests exist as a machine-readable artifact adjacent to the script — before execution, content-addressed. This is the articulation+lock component without requiring an external registry.

**The MCP server** (`bth-mcp`) means Claude Code already has a typed interface to call `bth run`, `bth ls`, and `bth find`. The audit trail for agentic runs is already the same catalog as human runs. bathos does not need to add an "AI mode" — it just needs to make agentic provenance first-class.

No other tool in the space has all three. Nextflow has execution graphs but no hypothesis binding. OSF has pre-registration but no computational provenance or MCP interface. The AI Scientist has no persistent audit substrate at all — its failure mode is precisely the absence of what bathos can provide.

---

## Section 3 — Design Directions

### High leverage: provenance graph / lineage

The current schema stores individual run records. The missing primitive is a `parent_run_id` (or `derived_from: str[]`) field on the run record, enabling a directed acyclic graph of runs. This unlocks:

- "Which downstream analyses depend on run X?" (impact of invalidation)
- "What was the full lineage of this result?" (one-shot reproducibility audit)
- "Did any run in this chain have a dirty git state?"

DuckDB already supports recursive CTEs for graph traversal. Implementation cost is low: add one nullable UUID column to the cool schema, one `bth run --derived-from <run-id>` flag, and a `bth lineage <run-id>` query command. This is the highest-ROI schema extension.

### High leverage: agentic audit trail

When Claude calls `bth run` via MCP, the current record captures what ran but not what the agent declared it was investigating. The fix requires a hypothesis to be bound at MCP call time, not just at sidecar-read time.

Two design options:
- **(a)** The MCP `run` tool accepts an optional `hypothesis` string parameter, which gets content-hashed and stored alongside the run record — the agent provides this at dispatch
- **(b)** The sidecar is generated by the agent before calling `bth run`, and its hash is verified against what `bth run` loads

Option (a) is lower friction for agentic callers but allows post-hoc rationalization. Option (b) is more tamper-evident. This is the key architectural decision for agentic integrity (see Section 5).

Either way, the audit query is: `bth find --agent --hypothesis-mismatch` — runs where the recorded outcome label does not match the pre-declared outcome condition. That query, run after a sprint, tells a researcher whether Claude drifted.

### Medium leverage: campaigns / experiment series

Groups of related runs that together constitute an investigation. A campaign record needs: name, hypothesis (shared across runs), start date, member run IDs, status (open / concluded), and a conclusion field. Implementation: a `campaigns` table in the warm DuckDB, with a junction table to runs. CLI: `bth campaign create "nvt-stability-investigation"`, `bth campaign add <run-id>`, `bth campaign conclude --outcome pass --note "..."`.

This is the correct granularity for multi-run aggregate analysis — not per-run metrics, but per-investigation conclusions. It maps directly to the ML two-phase pre-registration protocol: Phase A before training is a campaign-level declaration; Phase B is the campaign conclusion.

### Medium leverage: comparison / regression tracking

`bth compare <run-a> <run-b> --metric temp_std` requires that result metrics are stored in the warm schema, not just in output files. The `metadata JSON` column (already planned for warm tier) is the right place. A thin `bth annotate <run-id> --results '{"temp_std": 4.2}'` command lets any script or agent push metrics back into the catalog post-run, and then `bth compare` does a DuckDB join. No separate metric store needed.

### What NOT to build

- **Execution orchestration**: bathos wraps, it does not orchestrate. Nextflow and Snakemake handle DAG execution; bathos tracks what they ran.
- **Hyperparameter sweep management**: Optuna/Ray Tune own this. bathos records the run; it does not generate the parameter grid.
- **Cross-researcher collaboration or public sharing**: OSF and Zenodo handle publication. bathos is single-researcher.
- **A UI or dashboard**: DuckDB + `bth sql` + Claude via MCP is the query interface. Adding a web dashboard is a maintenance sink.
- **Metric streaming or real-time monitoring**: cool-tier writes are atomic post-run. Online metrics belong to W&B or TensorBoard.

---

## Section 4 — Pre-registration Hardening

The empirical literature is clear: the mechanism that reduces p-hacking is **forced articulation + temporal lock + immutable link**. Basic high-level registration does nothing. The sidecar `.bth.toml` with `[outcomes]` DuckDB SQL conditions is already at the correct granularity.

**Three-component stack:**

1. **Articulation** — the sidecar must specify at least one confirmatory test as a DuckDB SQL fragment over result columns, before execution. `bth run` validates that the SQL parses at start.
2. **Lock** — content-hash the sidecar at `bth run` invocation; store the SHA256 in the run record. If the sidecar changes between run start and result evaluation, the hash mismatch is detectable.
3. **Binding** — store the evaluated outcome label (`pass` / `marginal` / `fail`) in the warm schema at run completion, derived from the locked sidecar conditions, not re-evaluated later.

**Implementation order:**
1. Store `sidecar_sha256` + `sidecar_path` in the cool schema — one field, zero behavior change
2. Evaluate outcome conditions at run end and write `outcome` to the warm schema during compaction
3. Surface `--hypothesis-mismatch` in `bth find` as a query filter
4. Amendment log (`bth amend <run-id> --reason "..."`) for version-controlled sidecar changes — P3

---

## Section 5 — Recommended Feature Backlog Additions

| Priority | Item | Note |
|---|---|---|
| P1 | `sidecar_sha256` + `sidecar_path` fields in cool schema | Zero behavior change; enables all integrity features |
| P1 | `parent_run_id` field in cool schema + `--derived-from` flag | Enables lineage graph; cheap to add early, expensive to retrofit |
| P2 | `validate_sidecar()` in `sidecar.py` + gate enforcement in `bth run` | Scoped to `scripts/experiments/` and `scripts/benchmarks/` only |
| P2 | `bth annotate <run-id> --results <json>` + outcome evaluation at compaction | Result-emission protocol decision required first (see Q3) |
| P2 | `bth lineage <run-id>` — recursive CTE query over parent_run_id graph | Depends on parent_run_id field |
| P2 | `campaigns` table + `bth campaign` subcommand | Multi-run investigation grouping |
| P2 | `bth compare <run-a> <run-b> --metric <field>` | Depends on annotate / metric storage |
| P3 | Amendment log (`bth amend`) | After lock is solid |
| P3 | `--hypothesis-mismatch` filter in `bth find` | After outcome evaluation is built |

---

## Section 6 — Open Questions

**Agentic hypothesis capture mechanism.** Does the MCP `run` tool accept a `hypothesis` string (agent-provided, low friction, weaker integrity), or does it require a pre-existing sidecar whose hash is verified (stronger integrity, more friction for Claude)? The answer determines whether agentic audit is auditable or decorative.

**Campaign granularity.** Are campaigns per-project or global across the catalog? Per-project is simpler; global enables cross-project pattern detection. Decision affects the campaigns table schema.

**Metric storage policy.** Does `bth run` expect the script to emit a structured result (e.g., JSON to stdout, or a known output file), or is `bth annotate` always a separate step? The former enables automatic outcome evaluation; the latter keeps bathos script-agnostic. The current design leans script-agnostic — is that still the right call at scale?

Three concrete options for result emission:
- **(a)** Structured stdout line: `#bth-result: {"temp_mean": 1.2}`
- **(b)** Results JSON file at conventional path: `<script-stem>.bth-results.json`
- **(c)** `@bth.experiment` decorator (backlog #129) captures return values

Option (b) is the most language-agnostic and consistent with the sidecar-as-companion-file pattern.

**Parent-run linkage.** Is `--derived-from` user-specified (the researcher knows the lineage), or does bathos attempt to infer it (e.g., if a run reads a registered output file from a prior run, auto-link)? Auto-inference is powerful but fragile; manual is reliable but requires discipline.

**Sidecar enforcement timeline.** Currently `bth run` warns but does not block on missing sidecar for scripts in `scripts/experiments/`. When does the block become hard? Early enforcement breaks existing scripts; late enforcement means pre-registration never gets adopted.

**Outcome evaluation on failure.** If a run exits non-zero, should bathos evaluate the outcome conditions anyway (result files may still exist), or mark outcome as `error` regardless? Agentic runs frequently fail partway and still produce partial results that get used downstream — forcing `error` may hide useful signal.

---

*Research basis: 4 NotebookLM queries (~144 sources) covering: pre-registration enforcement mechanisms and empirical evidence; scientific workflow systems survey (VisTrails, Kepler, RO-Crate, Nextflow, CWL Prov); AI Scientist / self-driving labs (Lu et al. 2024, Aspuru-Guzik group); pre-registration granularity and ML two-phase protocol.*
