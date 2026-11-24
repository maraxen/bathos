+++
id = "STAT-007"
title = "Identify the unit of independence before pooling or gating across replicas"
severity = "warning"
applies_when = "has_unit_of_independence_risk"
source_check = "check_unit_of_independence"
tags = ["statistics", "replicas", "pooling", "independence"]
see_also = ["STAT-004", "STAT-005", "STAT-006"]
+++

Simulation replicates or random seeds from the same starting structure are partial samples
of a single ensemble, not independent experiments that must each converge individually.
Conversely, trajectories started from distinct structures or builds are not exchangeable.

Common failure modes when replica or seed units appear in a sidecar:

1. **Per-replica convergence gates** (`per_replica_converged = true`, `converged = true` on
   replica runs) fail valid ensembles because individual trajectories sample different basins.
2. **Replica-split nulls** (`replica_split_null`, within-replica window shuffling) break
   temporal/ensemble structure to fabricate a within-replica null.
3. **Replica-spread error bars** (`replica_spread`, `replica_std`, `seed_std`) or treating
   `n_seeds` as independent sample size `n` conflates between-trajectory variance with
   standard error of the pooled estimator.
4. **Undeclared pooling unit** when `n_replicas`, `n_seeds`, or `pooled_*` columns appear
   with no declared independence unit (`[breakdown].unit`, `[pooling].unit`, or
   `[independence].unit`).

**What to do.** Pool exchangeable replicas as samples of one ensemble and estimate uncertainty
with a block bootstrap at the true unit of independence (such as `[breakdown] unit = "build"`
or `"system"`). If starting structures are genuinely non-exchangeable, state so explicitly via
`[breakdown] waive = "reason"` or `[independence] waive = "reason"`.
