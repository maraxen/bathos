---
title: "Prior Art Research: Result Validity, Supersession, and Knowledge Currency in Experiment Tracking"
description: "Phase 2 Prior Art Research for bathos task 261106_archive-supersession: external invalidation and supersession patterns across 9 domains (MLOps, DataLad, W3C PROV, Bazel/Nix, Retraction Watch, GRADE, 21 CFR Part 11, OSV, ADR), in-depth evaluation of in-house prototypes in tev_design, asr, and praxia, empirical count verifications, and architectural recommendations for the Phase 3 specification."
status: REVIEWED_RESEARCH
task_id: 261106_archive-supersession
phase: 2
date: "2026-11-06"
author: "explorer_prior_art (teamwork_explorer)"
tags:
  - prior-art
  - research
  - invalidation
  - supersession
  - blast-radius
  - provenance
---

# Prior Art Research: Result Validity, Supersession, and Knowledge Currency in Experiment Tracking

## 1. Executive Summary & Core Insights

Experiment tracking systems traditionally optimize for **write-time capture**: recording arguments, hashes, seeds, and metrics as a run executes. However, two chronic failure modes plague long-horizon scientific campaigns:

1. **Contamination (Silent Zombie Results)**: Results produced by a defective, uncalibrated, or since-changed instrument (e.g. the `aminx` MPNN wrapper) persist in repositories and databases. When autonomous agents or researchers query the catalog, these invalid readings are dredged up and cited as authoritative truth because no read, query, citation, or graduation path checks post-hoc invalidation.
2. **Circling (Amnestic Re-litigation)**: Because the current state of belief ("what do we believe about question $Q$ today, and why?") is not machine-recorded, subsequent sessions re-litigate settled questions from scattered, dated markdown notes or duplicate identical experiments, drifting across mutually contradictory conclusions.

The root cause in bathos is an architectural asymmetry: **provenance tracking is rich on write, but validity is unconsulted on read**. bathos records git-drift, lock-drift, and blast-radius flags, but every agent-facing surface (`bth ls`, `bth find`, `bth get`, `bth cite`, `claim register`, `campaign conclude`, `graduate_product`, and SQL queries) returns all rows unconditionally.

This Phase 2 research surveys **9 external domains** handling invalidation, supersession, and knowledge currency, and conducts a forensic evaluation of **3 in-house prototypes** (`tev_design`, `asr`, `praxia`) that attempted ad-hoc solutions.

### Top Actionable Lessons for bathos:
- **Adopt the OSV Interval & Event Model (D1)**: Model instrument defect ranges after the Open Source Vulnerability (OSV) schema using `introduced`, `fixed`, `last_affected`, and `limit` events across both semantic versions and git commit hashes, augmented with DuckDB SQL predicates (e.g. `S > 1`).
- **Enforce 21 CFR Part 11 Non-Obliteration (D3)**: Never mutate or delete historical ledger rows. Hiding invalid, void, or superseded results is strictly a **read-time projection**. Historical runs remain permanently intact, accessible via explicit `--include-superseded` overrides with prominent warning banners.
- **Adopt W3C PROV Invalidation Semantics (`prov:wasInvalidatedBy` / `prov:wasRevisionOf`)**: Expand bathos's W3C PROV-JSON export (`provenance.py`) to formally represent invalidation activities (defect discoveries, postmortems) and revision edges.
- **Implement a GRADE-Inspired Finding Downgrade Ladder (D7)**: Epistemic claims must be tied to supporting run health. When underlying runs are flagged by defect ranges, findings automatically transition to `needs-recheck` at read time, surfacing in a byte-capped session-start view.
- **Subsume In-House Prototypes into Native bathos Primitives**: Retire hand-maintained TOML ledgers (`claims_ledger.toml`), isolated Python checkers (`check_ledger_instrument_staleness.py`), directory-of-verdicts (`.verdicts/`), and markdown indices (`INDEX.md`), replacing them with native bathos findings and defect registries.

---

## 2. External Prior Art Analysis (9 Domains)

We analyze 9 mature disciplines managing artifact invalidation, lifecycle, and knowledge currency. For each domain, we examine concrete mechanisms and extract what to **Adopt**, **Adapt**, and **Avoid**.

```
+----------------------------------------------------------------------------------------------------+
|                                    PRIOR ART DOMAIN TAXONOMY                                       |
+------------------------------------+----------------------------------+----------------------------+
| 1. System & Execution Substrates   | 2. Standards & Epistemic Rigor   | 3. Lifecycle & Decisions   |
| - MLOps (DVC, MLflow, W&B)         | - W3C PROV (PROV-O, PROV-DM)     | - ELN (21 CFR Part 11)     |
| - DataLad & git-annex              | - Retraction Watch & Crossref    | - Vulnerability (OSV)      |
| - Hermetic Builds (Bazel, Nix)     | - Evidence Grading (GRADE)       | - Architecture (ADR)       |
+------------------------------------+----------------------------------+----------------------------+
```

---

### 2.1 MLOps Platforms (DVC, MLflow, Weights & Biases)

#### Primitives & Mechanisms:
- **MLflow**: Employs lifecycle stages for runs (`active`, `deleted`) and registered models (legacy `Staging`, `Production`, `Archived`; modernized to mutable tags and aliases like `@champion`, `@challenger`). In MLflow, runs can be "soft-deleted" (`client.delete_run(run_id)`), which sets `lifecycle_stage = "deleted"`. The default search query `search_runs()` filters on `attributes.status = 'ACTIVE'` and hides deleted runs unless `ViewType.ALL` or `ViewType.DELETED_ONLY` is passed.
- **Weights & Biases (W&B)**: Artifacts form an immutable directed acyclic graph (DAG) where versions are strictly append-only (`v0`, `v1`, `v2`). Mutability is restricted to **aliases** (e.g. `latest`, `best`, `deprecated`) and **tags**. Deprecation is handled by removing positive aliases and applying a `deprecated` tag. Downstream pipelines query artifacts by alias rather than raw digest to avoid pulling deprecated checkpoints.
- **DVC (Data Version Control)**: Manages pipelines as DAGs encoded in `dvc.yaml` and lockfiles (`dvc.lock`). Invalidation is driven by input content hashes: running `dvc repro` detects when an upstream input hash deviates from `dvc.lock` and invalidates all downstream stage caches. Garbage collection (`dvc gc`) prunes untagged or unreachable content from the `.dvc/cache` across branches and commits.

#### Actionable Lessons:
- **Adopt**:
  - **Read-Time View Filtering**: Defaulting query APIs to `ACTIVE` / `CURRENT` while providing explicit opt-in flags (`--include-superseded` / `include_superseded=true`) mirrors MLflow's `ViewType` design.
  - **Artifact Aliases as Pointers**: Decoupling immutable run records from mutable currency pointers (findings/claims) avoids mutating historical run rows.
- **Adapt**:
  - In MLOps, run invalidation is typically coarse-grained (human flags a failed training run). bathos needs **retroactive range invalidation**: discovering a bug in an instrument on day 100 must automatically invalidate runs executed on days 1–99 without requiring a human to manually tag thousands of individual runs.
- **Avoid**:
  - **In-Place Status Column Mutation**: Early MLflow model registries mutated model stage columns (`Staging` -> `Production`), creating race conditions in concurrent execution. bathos must compute status via pure folds over immutable event streams.
  - **Hard Deletes as Hygiene**: Wiping runs destroys provenance auditability.

---

### 2.2 Distributed Data & Annex Systems (DataLad, git-annex)

#### Primitives & Mechanisms:
- **git-annex**: Decouples large file content from git trees. The repository tracks symlinks or small pointer files containing content keys (e.g., `SHA256E-s12345--<hash>`), while actual bytes reside in `.git/annex/objects/`.
- **`drop` and `get` Semantics**: Running `git annex drop <file>` verifies that sufficient remote copies exist before safely discarding local bytes. The pointer file remains in the git tree, preserving exact filename, commit history, and cryptographic digest. If an agent tries to read a dropped file, git-annex returns a clear POSIX error or broken symlink rather than silently hallucinating content.
- **DataLad**: Wraps git and git-annex to provide hierarchical dataset nesting and provenance capture (`datalad run`). When code or inputs change, DataLad generates git commits recording the execution invocation, input keys, and output keys. When upstream data is revised, git's Merkle tree naturally invalidates downstream submodule pins.

#### Actionable Lessons:
- **Adopt**:
  - **Decoupled Tombstoning**: When archiving obsolete scripts, sidecars, or large outputs (`bth archive`), retain complete metadata, paths, and content digests in the ledger while stubbing or bundling the bytes. Never delete the record of existence.
  - **Clear Failure Modes on Dropped Artifacts**: If an agent requests an archived artifact, refuse with a structured error (`RESULT_ARCHIVED`) naming the archive bundle location and restore command.
- **Adapt**:
  - Avoid git-annex's complex symlink-farm and git-annex branch sync overhead. bathos uses standard POSIX relative paths, `.partial` atomic writes, and DuckDB catalog indexing.
- **Avoid**:
  - **Unchecked Byte Pruning**: Discarding run outputs without recording their final SHA256 digest in an append-only archive ledger.

---

### 2.3 Formal Provenance Standards (W3C PROV-DM, PROV-O, PROV-JSON)

#### Primitives & Mechanisms:
- **W3C PROV-DM (Data Model)**: Defines core provenance concepts: `Entity` (physical, digital, or conceptual things), `Activity` (actions occurring over time), and `Agent` (entities responsible for activities).
- **`prov:wasInvalidatedBy`**:
  - Formal definition: "Invalidation is the start of the destruction, cessation, or expiry of an existing entity by an activity. The entity is no longer available for use (or no longer holds true) after invalidation."
  - Relation: `wasInvalidatedBy(id; e, a, t, attrs)` connects an entity $e$ to an invalidating activity $a$ at time $t$.
- **`prov:wasRevisionOf`**:
  - Formal definition: A specialized form of derivation (`wasDerivedFrom`) where an entity $e_2$ is a direct revision, replacement, or supersession of entity $e_1$.
  - Crucially separates **supersession** (evolution to a better measurement) from **invalidation** (destruction due to flaw).
- **Current bathos Implementation (`provenance.py`)**:
  - As observed in `src/bathos/provenance.py:22-111`, bathos generates W3C PROV-JSON 1.0 mapping `bth:Run` to `prov:Entity`, execution to `prov:Activity`, and parentage to `prov:wasDerivedFrom`.
  - **Critical Gap Identified**: bathos's PROV-JSON exporter currently emits *zero* invalidation or revision relationships. Neither `prov:wasInvalidatedBy` nor `prov:wasRevisionOf` is implemented.

```
       [Activity: Defect Discovery / Postmortem]
                         |
                prov:wasInvalidatedBy
                         v
[Entity: Run_A (void/affected)] <--- prov:wasRevisionOf --- [Entity: Run_B (current)]
```

#### Actionable Lessons:
- **Adopt**:
  - **First-Class Invalidation Relations**: Implement `prov:wasInvalidatedBy` in `provenance.py`, linking voided runs to the defect declaration activity or postmortem record.
  - **First-Class Revision Relations**: Implement `prov:wasRevisionOf` in `provenance.py` for superseded runs, findings, and archived scripts.
- **Adapt**:
  - In pure PROV-DM, invalidation occurs at time $t_{activity}$. For software defects, invalidation is **retroactive**: the run was epistemically defective at its execution time $t_0$, but known to be defective at $t_{discovery}$. bathos models this by attaching `declared_at` and `evidence` to the invalidating relation.
- **Avoid**:
  - Full RDF/OWL triplestore database architectures. Maintain light, high-performance PROV-JSON serialization over DuckDB views.

---

### 2.4 Hermetic Build Systems & Action Caches (Bazel, Nix, Buck2)

#### Primitives & Mechanisms:
- **Input-Addressed Invalidation**:
  - In Bazel and Nix, build actions are keyed by the exact cryptographic hash of all transitively declared inputs:
    $$\text{ActionKey} = \text{SHA256}(\text{CompilerBinary}, \text{CompilerFlags}, \text{EnvironmentVars}, \text{InputFiles})$$
  - If a compiler has a known bug, fixing the compiler bumps the toolchain hash in the workspace lockfile (`WORKSPACE`, `flake.lock`), which instantaneously changes the action key for all dependent targets.
- **Cache Poisoning & Action Cache Invalidation**:
  - If a defective toolchain already executed and populated the remote action cache, simply bumping the source does not clean the cache. Systems mitigate this via:
    1. **Cache salt / Action key prefixes**: Prepending a monotonically increasing generation number or toolchain UUID to every cache key.
    2. **Tombstoning**: Explicitly blacklisting poisoned action keys in the remote cache metadata.
- **Content-Addressed Early Cutoff**:
  - If target $T_1$ is re-executed due to an input change, but its output bytes are identical to the previous build, downstream targets $T_2$ are not rebuilt.

#### Actionable Lessons:
- **Adopt**:
  - **Toolchain / Instrument Hashing**: A run's epistemic validity is inextricably bound to the instrument binary/wheel that executed it. bathos must treat instrument version + lockfile SHA as a strict input address.
  - **Cache Salting via Defect Ranges**: Rather than purging historical records, defect ranges act as an epistemic "poison filter" evaluated at query time.
- **Adapt**:
  - Build systems assume actions are cheap, deterministic, and hermetic. Experiments in molecular biology, machine learning, and molecular dynamics are expensive, stochastic, and run on shared SLURM nodes. We cannot simply "re-run all invalidated targets." Invalidation must be flagged and reported, not trigger automatic re-computation.
- **Avoid**:
  - Assuming builds/runs are hermetic without checking lockfiles. (The spike in Section 4 demonstrates that untracked environments allow dirty tools to poison results silently).

---

### 2.5 Scholarly Retraction, Errata, and Contribution (Crossref Crossmark, Retraction Watch, CRediT)

#### Primitives & Mechanisms:
- **Crossref Crossmark**:
  - Industry standard metadata schema for tracking the currency of scholarly articles. A Crossmark record embeds machine-readable assertions about updates:
    - Update types: `retraction`, `correction` (erratum/corrigendum), `expression_of_concern`, `withdrawal`.
    - Relations: `isUpdateTo` (cites the original DOI), `updateDate`.
- **Retraction Watch Taxonomy**:
  - Decomposes invalidation into precise root causes:
    1. *Unreliable Results*: Software error, calculation bug, contaminated reagent, batch effect, irreproducible protocol.
    2. *Methodological Flaws*: Uncalibrated instrument, invalid baseline control.
    3. *Misconduct*: Fabrication, falsification, manipulation.
- **Expressions of Concern vs Retraction vs Corrigendum**:
  - **Expression of Concern**: Issued when substantial suspicion exists (e.g., ongoing investigation, unresolved anomaly) but final proof is not yet established. The paper remains citable but carries an alert banner.
  - **Full Retraction**: The findings are fundamentally unsound; the paper is not deleted, but watermarked "RETRACTED" across every page, and bibliographic APIs refuse clean citations.
  - **Corrigendum**: A localized typo or minor table error is amended without voiding the main conclusion.
- **Estimand Shift vs Invalidation**:
  - In clinical trials and epidemiology, if the protocol definition changes (e.g. Intention-to-Treat vs As-Treated), earlier results are not "retracted"; they measure a different **estimand**. They cannot be naively pooled in meta-analyses without an explicit comparability adjustment.

#### Actionable Lessons:
- **Adopt**:
  - **Granular Validity States (D3)**:
    - `void`: Direct equivalent of Retraction (fatal instrument defect; results false).
    - `suspect`: Direct equivalent of Expression of Concern (defect on inferred/dirty commit; banner shown, citation requires acknowledgment).
    - `estimand_change`: Direct equivalent of Estimand Shift (measurement quantity changed; valid within its regime, non-comparable across the boundary).
  - **Tombstone Watermarking**: An agent viewing a void run must receive a prominent banner with defect reason and replacement run ID.
- **Adapt**:
  - Academic retractions take years; bathos needs instant, machine-actionable defect ranges that take effect across local catalogs in milliseconds.
- **Avoid**:
  - **Silent Removal**: Unpublishing or deleting a paper without a persistent tombstone is condemned in academic publishing; deleting runs from `bathos.db` is an anti-goal.

---

### 2.6 Living Systematic Reviews & Evidence Grading (GRADE Framework)

#### Primitives & Mechanisms:
- **GRADE (Grading of Recommendations Assessment, Development and Evaluation)**:
  - Framework used by Cochrane, WHO, and CDC to rate the certainty of evidence for scientific questions.
  - **Certainty Tiers**: `High`, `Moderate`, `Low`, `Very Low`.
  - **5 Downgrading Factors**:
    1. *Risk of Bias*: Flawed design, lack of negative/positive controls.
    2. *Inconsistency*: Heterogeneity between replicate runs or campaigns.
    3. *Indirectness*: Surrogate measurement does not represent the real phenomenon.
    4. *Imprecision*: Small sample size, wide variance.
    5. *Publication / Reporting Bias*: Selective reporting of positive runs.
  - **3 Upgrading Factors**:
    1. Large effect size.
    2. Dose-response gradient.
    3. Plausible residual confounding working against the observed effect.
- **Living Systematic Reviews (LSR)**:
  - Continually monitored evidence syntheses where newly published trials trigger dynamic recalculation of pooled effect estimates and evidence grades.

#### Actionable Lessons:
- **Adopt**:
  - **Evidence Tiering (D4)**: Structure bathos's epistemic classes into a hierarchy: `confirmatory` > `calibrating` > `exploratory` > `spike`.
  - **Automated Downgrading Ladder (D7)**: A finding cannot achieve `settled` status on `exploratory` or `spike` evidence alone. If an instrument defect hits the supporting confirmatory runs, the finding automatically downgrades to `needs-recheck`.
- **Adapt**:
  - GRADE involves manual panel reviews. bathos codifies the grading rules into automated gates (`prereg.py`, `claim.py`, `breakdown_gate.py`).
- **Avoid**:
  - Conflating sample size or green exit codes with certainty. A script exiting `0` with 10,000 cells generated by a flawed instrument is still `Very Low` / `void` certainty.

---

### 2.7 Electronic Lab Notebooks & Regulatory Compliance (21 CFR Part 11)

#### Primitives & Mechanisms:
- **FDA 21 CFR Part 11 Regulations**:
  - Applies to electronic records and electronic signatures in regulated life sciences.
  - **§11.10(e) Audit Trails**: Requires computer-generated, time-stamped audit trails that independently record the date and time of operator entries and actions that create, modify, or delete electronic records.
  - **Strike-Through without Obliteration**: "Record changes shall not obscure previously recorded information." The original data must remain visible and readable alongside the modification, author, timestamp, and reason.
  - **Witnessing & Non-Repudiation**: Attestations must be attributable to a unique individual with dual authorization on critical state transitions.
- **ELN Invalidation Workflows (e.g. Benchling, LabArchives)**:
  - When a lab experiment is invalidated (e.g. contaminated reagent, incubator power failure), the notebook page is marked "Invalidated" or "Witness Rejected".
  - A diagonal watermark or strike-through is applied across the UI.
  - Export tools flag the dataset so it cannot be imported into downstream submission packages.

#### Actionable Lessons:
- **Adopt**:
  - **Strict Non-Obliteration Invariant (A2)**: Hiding is read-time only; no ledger row is mutated or deleted. Every invalidation record appends an event naming `reason`, `declared_by`, `declared_at`, and `evidence`.
  - **Audit Trail Readback**: Provide full readback capability (`--include-superseded`, `bth find --all`) that displays the complete history of how an entity transitioned from clean to void.
- **Adapt**:
  - Replace heavy PKI hardware tokens with git commits and cryptographically hashed run records.
- **Avoid**:
  - Creating separate "dirty" and "clean" physical databases that desynchronize.

---

### 2.8 Software Vulnerability & Advisory Models (OSV - Open Source Vulnerability Schema)

#### Primitives & Mechanisms:
- **OSV Schema (Open Source Vulnerability)**:
  - Published by OpenSSF, Google, PyPA, RustSec, and GitHub Advisory Database. Designed specifically for automated, machine-actionable dependency matching.
  - **Core Schema Fields**:
    ```json
    {
      "schema_version": "1.6.0",
      "id": "BTH-INST-2026-001",
      "package": {"name": "aminx", "ecosystem": "PyPI"},
      "ranges": [
        {
          "type": "ECOSYSTEM",
          "events": [
            {"introduced": "0.1.0a0"},
            {"fixed": "0.2.1"}
          ]
        },
        {
          "type": "GIT",
          "repo": "https://github.com/.../aminx",
          "events": [
            {"introduced": "3a12bf0"},
            {"fixed": "d930bba"}
          ]
        }
      ],
      "database_specific": {
        "severity": "void",
        "kind": "defect",
        "predicate": "S > 1"
      }
    }
    ```
  - **Key Structural Properties**:
    - **Interval Semantics**: Represented as ordered pairs of `introduced` and `fixed` (or `last_affected` / `limit`). This avoids brittle comparison operators (`<`, `<=`).
    - **Dual Typing**: Distinct range types for semantic/ecosystem releases (`0.1.0a24`) and source git commits (`d930bbad`). This matches reality: scientific dependencies often land critical fixes in unreleased git commits long before wheel release.

#### Actionable Lessons:
- **Adopt**:
  - **Direct Schema Foundation for bathos D1**: Model the bathos Instrument Defect Declaration schema directly on OSV's `ranges` and `events` structure.
  - **Clear Interval Boundaries**: Use `introduced` and `fixed` keys. A run's resolved package version is compared against these intervals.
- **Adapt**:
  - Add scientific-specific attributes to the advisory: `kind = "defect"` vs `kind = "estimand_change"`, and SQL `predicate` expressions (e.g. `S > 1`) evaluating over run parameters.
- **Avoid**:
  - Arbitrary regex pattern matching on package version strings. Use standardized PEP 440 parsing for Python packages and git ancestor checks for commit hashes.

---

### 2.9 Architecture Decision Records (ADR Lifecycle & Supersession)

#### Primitives & Mechanisms:
- **ADR Formal Lifecycle (Nygard / MADR)**:
  $$\text{Draft} \longrightarrow \text{Proposed} \longrightarrow \text{Accepted} \longrightarrow \text{Superseded} \mid \text{Amended}$$
- **Immutability of Decisions**:
  - An ADR is never rewritten when a decision is reversed. A new ADR (e.g. ADR-0015) is created stating "Supersedes ADR-0003".
  - The superseded ADR is updated *only* to append a frontmatter status header pointing to the successor:
    ```markdown
    Status: Superseded by [ADR-0015](../0015-new-decision.md)
    Date Superseded: 2026-11-06
    ```
- **Amended vs Superseded**:
  - `Superseded`: The prior choice is abandoned entirely.
  - `Amended`: The core decision stands, but a specific sub-clause or configuration is modified.
- **Bidirectional Graph**:
  - Downstream ADR has `Supersedes: ADR-0003`.
  - Upstream ADR has `Superseded by: ADR-0015`.

#### Actionable Lessons:
- **Adopt**:
  - **Typed Supersession Edges (D5)**: Supersession must be a typed edge (`run -> run`, `finding -> finding`, `doc -> doc`) with an explicit successor ID, reason, and actor, rather than free-form prose.
  - **Bidirectional Indexing**: Store supersession edges append-only with automatic inverse indexing ("what replaced X" vs "what X replaced").
- **Adapt**:
  - In ADRs, markdown links are human-navigated. In bathos, supersession edges must be queryable via DuckDB SQL and FastMCP tool calls.
- **Avoid**:
  - Unstructured free-text notes like `superseded_by = "the new script"` in sidecars (E3), which break programmatic traversal.

---

## 3. Rigorous Evaluation of In-House Prototypes

We evaluate the three existing in-house prototypes that bathos must replace rather than duplicate:
1. `tev_design` (`claims_ledger.toml`, `check_ledger_instrument_staleness.py`, `wheels/AMINX_PIN.md`)
2. `asr` (`.verdicts/`, `.praxia/docs/experiments/INDEX.md`)
3. `praxia` (`crates/praxia-tools/src/docs/default_registry.toml`)

```
+----------------------------------------------------------------------------------------------------+
|                               IN-HOUSE PROTOTYPE EVALUATION MATRIX                                 |
+---------------------+-------------------------------+----------------------------------------------+
| Prototype           | Core Mechanism                | Primary Failure Mode & Maintenance Burden    |
+---------------------+-------------------------------+----------------------------------------------+
| claims_ledger.toml  | Hand-maintained TOML table    | Split-brain pin drift; ignored by bth run/cite|
| staleness.py        | 181-line standalone Python    | Out-of-band audit; no blast radius; repo silo |
| AMINX_PIN.md        | 400+ line prose markdown log  | Institutional memory trapped in passive prose|
| asr .verdicts/      | 41 isolated TOML/MD files     | File sprawl; no automated defect propagation |
| asr INDEX.md        | Hand-edited markdown table    | Chronic sync drift; no validity awareness    |
| praxia doc registry | Rust frontmatter convention   | Separate tool silo; disjoint from run catalog|
+---------------------+-------------------------------+----------------------------------------------+
```

---

### 3.1 `tev_design` Prototypes

#### Prototype A: `scripts/audit/claims_ledger.toml`
- **What it tried to do**:
  Act as a single source of truth for all scientific claims in `tev_design`, mapping each claim to its evidential status, supporting runs, and the aminx wheel version used.
- **How it implemented it**:
  A single TOML file containing 43 `[[claims]]` entries. Each entry defined:
  - `id`, `title`, `statement`
  - `evidential_status`: categorized into `tracked_reproducible` (8), `untracked_reproducible` (17), `unverifiable` (6), and `refuted_or_superseded` (13).
  - `instrument_wheel`: string naming the wheel (e.g. `wheels/aminx-0.1.0a24-cp311-cp311-linux_x86_64.whl`).
  - A top-level table `[instrument]` with `aminx_pinned_here = "0.1.0a24"`.
- **Where it broke down & created maintenance burden**:
  1. *Amnestic Circling*: The file header contains a candid admission: an item was **"twice proposed as new work because nothing here pointed at it."** Agents proposing experiments did not read or parse this file before generating proposals.
  2. *Split-Brain Pin Drift*: The header pinned `aminx_pinned_here = "0.1.0a24"`. In reality, the project wheel pin in `pyproject.toml` / `uv.lock` advanced through `0.1.0a25`, `0.2.0`, `0.2.1` to `0.3.0`. The ledger froze at a24, creating an active lie about what instrument was in use.
  3. *Zero Read/Execution Enforcement*: Neither `bth run` nor `bth cite` consulted `claims_ledger.toml`. An agent could cite an `unverifiable` or `refuted_or_superseded` claim with zero resistance.
- **Clean Subsumption in bathos**:
  - Subsume claims into **bathos Findings** (D7) and **Claim Tier** records.
  - Automatically resolve instrument versions from `dependency_provenance` and `uv.lock` (D2), eliminating manual `aminx_pinned_here` strings.
  - Read-time hiding (D3) ensures that `refuted_or_superseded` or defect-hit claims are hidden by default from citation and search.

#### Prototype B: `scripts/audit/check_ledger_instrument_staleness.py`
- **What it tried to do**:
  Detect when the aminx wheel pinned in `claims_ledger.toml` drifted from the wheel actually installed or available in the repository.
- **How it implemented it**:
  A 181-line standalone Python script parsing `claims_ledger.toml`, extracting the wheel filename, checking file existence under `wheels/`, inspecting `pyproject.toml`, and throwing errors on mismatch.
- **Where it broke down & created maintenance burden**:
  1. *Out-of-Band Execution*: The script was an isolated audit utility. It was not run in pre-commit, pre-push, or `bth run`. It was only executed when a human remembered to run it.
  2. *Coarse Binary Output*: It could only say "the pin is stale." It could not trace blast radius: which specific runs, metrics, or design variants were invalidated by the version delta?
  3. *Repository Siloing*: It lived only in `tev_design`. `asr` had identical aminx staleness issues but had to invent its own checks or suffer undetected drift.
- **Clean Subsumption in bathos**:
  - Subsume into bathos's **Instrument Defect Registry** (D1) and **Distance from Present** (D6).
  - Every run read automatically displays how many versions behind current the instrument was at run time.
  - Defect detection runs automatically during `bth check` and session start.

#### Prototype C: `wheels/AMINX_PIN.md`
- **What it tried to do**:
  Record the narrative bug history and breaking changes across aminx versions (e.g. line 400: *"S > 1 AR-sampled results at <= a22 are void"*).
- **How it implemented it**:
  A 400+ line prose markdown file detailing bug postmortems:
  - $\le \text{a22}$: $S > 1$ autoregressive sampling bug produced corrupt logits; all $S > 1$ results void.
  - $\text{a24} \rightarrow \text{a25}$: Diagonal self-leak fix, which also constituted an **estimand change** (altered *which mathematical quantity* was computed from logits).
  - $0.2.1$: X-token sampling fix.
- **Where it broke down & created maintenance burden**:
  1. *Institutional Knowledge Trapped in Passive Prose*: An agent executing `bth run` or `bth cite` cannot parse a 400-line markdown file. The critical condition $S > 1$ on $\le \text{a22}$ was ignored in practice.
  2. *Conflation of Defect vs Estimand Change*: The document mixed fatal calculation errors (which void results) with estimand shifts (which alter comparability across boundaries).
- **Clean Subsumption in bathos**:
  - Transform prose rules into machine-readable TOML declarations under `.bth/instruments/aminx.toml` (D1).
  - Express $S > 1$ as an executable DuckDB predicate:
    ```toml
    [[defect]]
    id = "AMINX-001"
    package = "aminx"
    range = "<=0.1.0a22"
    predicate = "sample_s > 1"
    kind = "defect"
    severity = "void"
    evidence = "wheels/AMINX_PIN.md:400"
    ```
  - Formally distinguish `kind = "defect"` (severity `void`) from `kind = "estimand_change"`.

---

### 3.2 `asr` Prototypes

#### Prototype A: `.verdicts/` Directory
- **What it tried to do**:
  Store modular, per-question scientific verdicts (e.g. binding affinity, sequence recovery, structural stability) to prevent re-opening settled design questions.
- **How it implemented it**:
  A directory containing 41 separate files (TOML/markdown) with structured fields: question, answer, date, author, supporting run IDs.
- **Where it broke down & created maintenance burden**:
  1. *File Sprawl & Query Blindness*: 41 disconnected files in `.verdicts/`. Searching them required custom grep scripts. They were not integrated into DuckDB or `bth sql`.
  2. *Vulnerability to Instrument Contamination*: When the aminx MPNN defect and the 4-month LG-matrix defect (2026-06-08 to 2026-10-23) were identified, there was no automated mechanism to determine which of the 41 verdicts were backed by contaminated runs.
  3. *No Supersession Edges*: Updating a verdict meant either editing the old file in place (destroying history) or writing a new file with no machine-enforced link to the old one.
- **Clean Subsumption in bathos**:
  - Migrate all 41 verdicts directly into the **bathos Findings Register** (D7, D9 step 2).
  - When supporting runs are flagged by defect ranges, the finding status flips to `needs-recheck` at read time.
  - Expose findings through unified CLI (`bth findings list`) and MCP tools.

#### Prototype B: `.praxia/docs/experiments/INDEX.md`
- **What it tried to do**:
  Provide a human-readable index table of all experimental investigations, their run IDs, hypotheses, and status.
- **How it implemented it**:
  A manually maintained markdown table in `.praxia/docs/experiments/INDEX.md`.
- **Where it broke down & created maintenance burden**:
  1. *Chronic Human Sync Drift*: Updating the markdown table after every SLURM batch was tedious; the table quickly fell weeks behind the actual catalog.
  2. *Static & Unverified*: The index displayed runs as valid forever, completely oblivious to subsequent git-drift, output drift, or instrument defect flags.
- **Clean Subsumption in bathos**:
  - Replace the manual index table with dynamic, byte-capped project summaries (`bth session-start` / hook) generated on the fly from the DuckDB catalog.

---

### 3.3 `praxia` Prototypes

#### Prototype: Docs Registry (`default_registry.toml`)
- **What it tried to do**:
  Manage technical document lifecycles, enforce consistent frontmatter metadata, and track document deprecation across the workspace.
- **How it implemented it**:
  A Rust configuration in praxia tools (`/home/marielle/projects/praxia/crates/praxia-tools/src/docs/default_registry.toml`) defining:
  - Document statuses (`status_enum`, line 18): `["draft", "active", "complete", "superseded", "archived"]`.
  - Relations (`optional` frontmatter, line 17): `supersedes`.
  - Archival storage convention (`[categories.archive]`, lines 83–88): Storing archived docs in `archive/` as `.tar.zst` archives with paired `.md` manifest sidecars (`manifest_pair = true`).
- **Where it broke down & created maintenance burden**:
  1. *Tool Silo Separation*: Praxia tracked docs; bathos tracked runs and scripts. If a script was archived in bathos, praxia docs citing it remained marked `active` or `complete`.
  2. *Duplicate Lifecycle Taxonomies*: Disjoint statuses between tools created cognitive overhead.
- **Clean Subsumption in bathos**:
  - Implement D8's shared doc store configuration: bathos can be configured to point its doc store at `.praxia/docs`, reusing praxia's vocabulary (`superseded`, `archived`, `supersedes`) and `.tar.zst` + manifest convention.
  - Enable cross-tool sweeps: bathos archive sweeps propose archiving docs whose cited runs and findings are void or archived.

---

## 4. Count Re-derivation & Empirical Baseline Verification

### 4.1 Methodological Verification & Live Repository Probes

Per project constraints, every count cited from `asr`, `tev_design`, or `praxia` must show the exact verifying bash command executed against the read-only repositories (`/home/marielle/projects/asr`, `/home/marielle/projects/tev_design`, and `/home/marielle/projects/praxia`), and spike estimates from the initial brainstorming session are distinguished from the live re-derived counts (see also `.praxia/docs/research/261106_validity-surfaces-recon.md` §2–3 for full command traces).

---

### 4.2 Verified Counts & Bash Command Reference

| Entity / Metric | Verified Count (Initial Spike -> Live Probe) | Verifying Bash Command (Executed on Host) | Status & Verification Notes |
|---|---|---|---|
| `tev_design` Claims Count | **43 claims** | `rg -c "^\[\[claims\]\]" /home/marielle/projects/tev_design/scripts/audit/claims_ledger.toml` | Verified (E14). 43 distinct `[[claims]]` blocks declared. |
| `tev_design` Evidential Status Breakdown | `tracked_reproducible`: 8<br>`untracked_reproducible`: 17<br>`unverifiable`: 6<br>`refuted_or_superseded`: 13 | `grep -E "^evidential_status = " /home/marielle/projects/tev_design/scripts/audit/claims_ledger.toml \| sort \| uniq -c` | Verified (E14). Sums to 44 entries across 43 blocks (one compound block). |
| `tev_design` Staleness Script Lines | **181 lines** | `wc -l /home/marielle/projects/tev_design/scripts/audit/check_ledger_instrument_staleness.py` | Verified (E14). Standalone custom parser. |
| `tev_design` AMINX_PIN Void Rule | Line 400: $S > 1$ AR-sampled $\le \text{a22}$ void | `sed -n '400p' /home/marielle/projects/tev_design/wheels/AMINX_PIN.md` | Verified (E12). Institutional memory in prose. |
| `asr` Verdicts Count | **41 files** | `ls -1 /home/marielle/projects/asr/.praxia/verdicts \| wc -l` | Verified (E15). Located at `asr/.praxia/verdicts/`. |
| `asr` Total Run Events | **14,057 run events** (grown from 11,673 at initial spike) | `cat /home/marielle/projects/asr/.bth/log/*.jsonl \| jq -r 'select(.kind == "run.imported" or .kind == "run.started") \| .eid' \| wc -l` | Verified (E10). Counts run entity events (`run.imported` + `run.started`). |
| `tev_design` Total Run Events | **3,167 -> 3,170 run events** | `cat /home/marielle/projects/tev_design/.bth/log/*.jsonl \| jq -r 'select(.kind == "run.imported" or .kind == "run.started") \| .eid' \| wc -l` | Verified (E10). 3,167 at recon probe; 3,170 at Cycle 3 verification (1,574 `nolock` from `2026-05-19..2026-08-13`; 1,596 `lock` from `2026-08-13..2026-11-07`). |
| `asr` Lock Hash Present | **4,362 runs** (grown from 3,516 at initial spike; ~31.0%) | `cat /home/marielle/projects/asr/.bth/log/*.jsonl \| jq -r 'select(.kind == "run.imported" or .kind == "run.started") \| select(.data.dependency_lock_sha256 != null and .data.dependency_lock_sha256 != "") \| .eid' \| wc -l` | Verified (E10). Recoverable via D2 Tier 2 (`verified-from-lock`). |
| `tev_design` Lock Hash Present | **1,593 -> 1,596 runs** (~50.3%) | `cat /home/marielle/projects/tev_design/.bth/log/*.jsonl \| jq -r 'select(.kind == "run.imported" or .kind == "run.started") \| select(.data.dependency_lock_sha256 != null and .data.dependency_lock_sha256 != "") \| .eid' \| wc -l` | Verified (E10). Note: all lock-hashed runs in `tev_design` start `2026-08-13` (`>= 0.1.0a24`); `<= 0.1.0a22` runs (`2026-07-15..2026-07-28`) lack lock hashes (`OD-1`). |
| `dependency_provenance` Empty Rate | **0 runs (100% null)** in both repos | `cat /home/marielle/projects/tev_design/.bth/log/*.jsonl \| jq -r 'select(.kind == "run.imported" or .kind == "run.started") \| select(.data.dependency_provenance != null and .data.dependency_provenance != "") \| .eid' \| wc -l` | Verified (E10/E19). Cause: capture is opt-in, neither repo set `track_packages`. However, 117 events (36 distinct runs) in `tev_design` record `.data.metadata.aminx_version`. |
| Version Recovery Accuracy Sample | **24/25 pairs** (96%) | `cat /home/marielle/projects/tev_design/.bth/log/*.jsonl \| jq -r 'select(.kind == "run.imported" or .kind == "run.started") \| select(.data.git_hash != null and .data.dependency_lock_sha256 != null) \| "\(.data.git_hash) \(.data.dependency_lock_sha256)"' \| sort -u \| head -n 25` (checked against `git show <hash>:uv.lock \| sha256sum`) | Verified sample (E11). 24/25 match; 1 mismatch (`0b4e3ae1`, dirty `uv.lock`). |
| `asr` Declared Stage Names | **46 / 357 full-repo sidecars** (**46 / 209** in `asr/scripts`) | `fd -e bth.toml . /home/marielle/projects/asr \| wc -l` (357) and `rg -l "stage_name\s*=" /home/marielle/projects/asr -g "*.bth.toml" \| wc -l` (46) | Verified (E9). Drift: 7 `"confirmatory"`, 1 `"power"`, 32 `"validation"`, 5 `"exploration"`, 1 `"calibration"`. |
| `tev_design` Declared Stage Names | **15 / 130 sidecars** (11.5%; total `357 + 130 = 487` sidecars across both repos) | `fd -e bth.toml . /home/marielle/projects/tev_design \| wc -l` (130) and `rg -l "stage_name\s*=" /home/marielle/projects/tev_design -g "*.bth.toml" \| wc -l` (15) | Verified (E9/E13). All 15 declared sidecars in `tev_design` use `"validation"`. |

---

## 5. Synthesis & Design Recommendations for Phase 3 Specification

Based on external prior art and prototype autopsies, we present concrete recommendations to govern Phase 3 specification writing.

### 5.1 Adopt, Adapt, Avoid Decision Matrix

| Dimension | Adopt | Adapt | Avoid |
|---|---|---|---|
| **Defect Model (D1)** | OSV interval syntax (`introduced`, `fixed`, `last_affected`, `limit`) with dual SemVer and Git commit range typing. | Augment OSV schema with DuckDB SQL `predicate` expressions (e.g. `sample_s > 1`) and `kind` (`defect` vs `estimand_change`). | Brittle string prefix matching; full CVSS calculation matrices. |
| **Version Recovery (D2)** | Build system input-addressing: resolve toolchain from cryptographic lockfile hashes (`uv.lock`). | Four-tier resolution hierarchy (`recorded` > `verified-from-lock` > `inferred-from-commit` > `unknown`). Dirty trees cap at `suspect`. | Mutating historical run records to backfill versions (violates A2). |
| **Read-Time Hiding (D3)** | 21 CFR Part 11 non-obliteration: hiding is strictly a read-time projection. History is immutable. MLflow `ViewType` filtering. | Central choke point function `resolve_validity(run_ids)` shared identically across cyclopts CLI and FastMCP. | Deleting catalog rows; silent dropped runs; mutable database stage columns. |
| **Evidence Tiers (D4)** | GRADE certainty grading: `confirmatory` > `calibrating` > `exploratory` > `spike`. | Map existing stage names to 4 canonical epistemic tiers. Display undeclared stages as `undeclared` rather than defaulting to `exploration`. | Treating an uncalibrated exploratory probe as citable proof. |
| **Supersession (D5)** | ADR formal status lifecycle; W3C PROV `prov:wasRevisionOf` semantics. | Append-only supersession edge ledger with automatic inverse indexing (`replaced_by` / `replaces`). | Unstructured free-text `superseded_by` notes in sidecars. |
| **Currency & Distance (D6)** | Living Systematic Review currency tracking: show distance from present (age, versions behind, commits). | Present factual distance attributes on every run read; never apply automatic time decay. | Arbitrary score-based decay algorithms that silently expire sound physics. |
| **Findings Register (D7)** | Modular living evidence synthesis: `{question, answer, status, supporting_runs, supersedes}`. | Automated downgrade ladder: finding status flips to `needs-recheck` when supporting runs become hidden. Reopen guard. | Disconnected `.verdicts/` files; manual index markdown tables. |
| **Archive Sweeps (D8)** | DataLad `drop` semantics: retain full cryptographic digests in ledger while bundling bytes. | Configurable doc store path supporting `.praxia/docs` integration with `.tar.zst` bundles. | Deleting untracked outputs without registering bundle checksums. |

---

### 5.2 Data Model & Invalidation Geometry (OSV + SQL Predicates)

To resolve **D1**, we recommend structuring `.bth/instruments/<pkg>.toml` using OSV-compatible syntax:

```toml
# .bth/instruments/aminx.toml
schema_version = "1.0.0"
package = "aminx"
ecosystem = "PyPI"

[[entries]]
id = "AMINX-2026-001"
kind = "defect"
severity = "void"
summary = "Autoregressive multi-sample corruption when S > 1 on <= a22"
evidence = "wheels/AMINX_PIN.md:400"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[entries.ranges]
type = "ECOSYSTEM"
events = [
    { introduced = "0.1.0a0" },
    { fixed = "0.1.0a23" }
]

[entries.predicate]
# Evaluated against run metadata / result schema in DuckDB
condition = "sample_s > 1"
reasoning = "Single-sample (S=1) argmax sampling is mathematically unaffected"

[[entries]]
id = "AMINX-2026-002"
kind = "estimand_change"
severity = "current"  # Does not void; marks comparability boundary
summary = "Diagonal self-leak elimination between a24 and a25 changed computed quantity"
evidence = "tev_design/scripts/audit/claims_ledger.toml"
declared_at = "2026-11-06T12:00:00Z"
declared_by = "owner"

[entries.ranges]
type = "ECOSYSTEM"
events = [
    { introduced = "0.1.0a25" }
]
```

#### Why SQL Predicates are Essential:
The $S > 1$ rule in `AMINX_PIN.md:400` proves that version ranges alone are too coarse. If bathos voided *all* runs on $\le \text{a22}$, hundreds of valid single-sample ($S = 1$) calibration runs would be destroyed. Combining OSV version intervals with DuckDB SQL fragments allows surgical, sound invalidation.

---

### 5.3 Read-Time Hiding vs Ledger Non-Obliteration (21 CFR Part 11)

In compliance with 21 CFR Part 11 and Anti-Goal A2, bathos must never rewrite history:

```
[Write-Time Storage] (Append-Only)
   ├── cool/run_<uuid>.parquet
   ├── cool/blast_radius_<uuid>.parquet
   └── .bth/instruments/*.toml
            │
            ▼
[Read-Time Evaluation] (Pure Projection)
   resolve_validity(run_ids, catalog_state)
            │
            ├──────────────┬──────────────┬──────────────┐
            ▼              ▼              ▼              ▼
         [void]       [affected]    [superseded]     [current]
            │              │              │              │
            ▼              ▼              ▼              ▼
         HIDDEN         HIDDEN         HIDDEN          SHOWN
   (refuse cite)  (refuse cite)  (refuse cite)   (allow cite)
```

#### Settling the `run_sql` Designer Call:
In the design document (§4 D10), the designer suggested shadowing raw `runs` with a current-only view.
- **Prior Art Assessment**: In SQL databases, silently altering the semantics of a base table breaks existing analytics, saved views, and scripts.
- **Recommendation**:
  1. Create a dedicated view `runs_current` filtering out hidden runs (`validity = 'current'`).
  2. In documentation, agent instructions, and error hints, establish `runs_current` as the standard query target.
  3. Keep `runs` as the full historical table, but if a query selects from raw `runs` without an explicit `--include-superseded` flag, return a structured warning banner stating:
     `"Notice: Query touched raw 'runs' table. N void/superseded results included. Query 'runs_current' for currency."`

---

### 5.4 Findings Register, Downgrading Ladders, and Session-Start Currency

To eliminate the chronic circling identified in `tev_design` and `asr`:

1. **Findings Data Model (D7)**:
   - Each finding addresses a specific design/scientific question:
     - `id`: Unique slug (`finding_tev_binding_site_stability`)
     - `question`: Concise scientific question
     - `answer`: Current best answer
     - `status`: `open`, `tentative`, `settled`, `superseded`, `refuted`, `needs-recheck`
     - `supporting_run_ids`: List of supporting bathos run UUIDs
     - `tier`: Highest supporting evidence tier
     - `supersedes`: Optional predecessor finding slug
     - `last_reviewed`: Timestamp and actor
2. **Automated Downgrading Ladder**:
   - `settled`: Requires $\ge 1$ `current` confirmatory run.
   - `tentative`: Backed by exploratory/spike runs only.
   - `needs-recheck`: Triggered dynamically whenever **all** supporting runs for a finding become hidden (`void`, `affected`, or `superseded`).
3. **Session-Start View (Byte-Capped $\le 2\text{ KB}$)**:
   - Surfaced by the new `SessionStart` hook.
   - Lists:
     1. Active defect alerts (e.g. `aminx <= 0.1.0a22 active void range`).
     2. Findings requiring attention (`needs-recheck` count and slugs).
     3. Settled findings (question + answer + age in days).
   - Never lists superseded or void items, preventing context window pollution.
4. **Reopen Guard**:
   - Creating a new run or finding matching a `settled` finding slug requires an explicit `--reopen-reason` parameter, directly preventing the "twice proposed as new work" anti-pattern.

---

### 5.5 Archive Sweep & Cross-Project Sharing (Praxia Integration)

To resolve **D8**:
- Build on `artifact_archive.py` and `archived_items.py`.
- **Sweep Proposals**: Propose archive candidates based on:
  1. Scripts with `[status] stale = true`.
  2. Scripts whose outputs have all been superseded or voided.
  3. Scripts unreferenced for $> 90$ days and cited by zero current findings.
- **Praxia Docs Sharing**:
  - Pointing bathos at `.praxia/docs` adopts praxia's `superseded`, `archived` frontmatter and `.tar.zst` + `.md` manifest archive structure.
  - Archive sweep detects documents citing exclusively voided runs and proposes moving them to `.praxia/docs/archive/`.

---

### 5.6 Verification of Key Decisions & Constraints for Phase 3

| Design Decision | Prior Art Basis | Recommendation for Phase 3 Spec |
|---|---|---|
| **Defect vs Estimand Change** | Retraction Watch vs Epidemiological Estimand Shift | Maintain strict separation. `defect` voids; `estimand_change` banners cross-boundary comparisons but never voids. |
| **Inferred Version Capping** | 21 CFR Part 11 Audit Integrity / Anti-Goal A1 | When version is inferred from git commit without lock SHA, defect match caps at `suspect`. Never void on inferred data. |
| **Central Choke Point** | Software Architecture / DRY | Specify `resolve_validity(run_ids)` in `src/bathos/validity.py`, called identically by cyclopts and FastMCP. |
| **PROV Export** | W3C PROV-DM 1.0 | Extend `provenance.py` with `prov:wasInvalidatedBy` and `prov:wasRevisionOf`. |
| **Session-Start Payload** | Cognitive Load Theory & Anti-Goal A3 | Hard cap at 2,048 bytes. Deterministic ordering: Alerts $\rightarrow$ Needs-Recheck $\rightarrow$ Open Findings $\rightarrow$ Settled Findings. |

---

## 6. Conclusion

Phase 2 research establishes that result invalidation, supersession, and knowledge currency are well-understood in external disciplines (OSV intervals, 21 CFR Part 11 audit trails, W3C PROV-DM, GRADE evidence grading, and ADR lifecycles). The repeated failures in `tev_design` and `asr` arose not from complex scientific unknowns, but from relying on **passive, unintegrated, hand-maintained text files** that execution and citation tools never consulted.

By translating proven external specifications into native, read-time bathos projections—anchored by an OSV-style defect registry, an automated findings downgrade ladder, and strict non-obliteration invariants—the Phase 3 specification can permanently eliminate both result contamination and circular re-litigation across all research repositories.
