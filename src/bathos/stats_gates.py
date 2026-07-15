"""bathos[stats] statistical battery + baseline-budget check (B2-01, #2181, AC-15, AC-17).

Grounding (verified before writing any code, not guessed): the DAG backlog text for B2-01 names
seven mechanical criteria without formulas. The authoritative source is
`xtrax/.praxia/research/260702_nlm_prequery_roadmap.md:133-145` ("Q6: Rigor gates: mechanical vs
judgment, with thresholds"), the same research note `xtrax.loop.multi_metric_ratchet` (T2-17,
already merged) cites for its own per-iteration simplification of this exact battery:

    Significance: Wilcoxon signed-ranks (pairwise), Friedman + post-hoc Nemenyi (multi-model);
      alpha=0.05 with Holm step-down correction.
    Effect size: Cohen's d >= 0.2 minimum.
    Consistency: Win Rate >= 0.6 across tasks, or P(A>B) >= 0.75.
    Stability: Breakdown Point >= 0.2 (>=20% of datasets must be removed to flip ranking).
    Replication: >=3 independent seeds, ICC > 0.990; N=29 trials/splits to detect P(A>B)>0.75 at
      beta=0.05.
    Baseline equivalence: fail any run where baseline gets fewer HPO trials/compute than proposal.

T2-17's own docstring is the precedent for BP's definition ("Breakdown Point" — the minimum
fraction of the comparison set that must be adversarially removed to flip a conclusion, a standard
robust-statistics concept, NOT "Beneficial Proportion" — an earlier, wrong guess corrected before
implementation there). This module's `breakdown_point` reuses that exact k/n search formula
(`xtrax.loop.multi_metric_ratchet._breakdown_point`), applied to per-task win/loss pairs instead of
per-metric deltas — same algorithm, different unit of comparison (T2-17 is per-iteration/
per-metric-dict; this is campaign-level/per-task-or-seed, per the research note's own explicit
"AC-10/F7 is per-iteration... distinct from AC-15's campaign-level battery" framing).

Cohen's d here is the textbook independent-samples formula ((mean1-mean2)/pooled_sd) — distinct
from T2-17's Cohen's dz (paired-difference variant, computed on a single delta vector). The
research note's "Effect size: Cohen's d >= 0.2 minimum" sits alongside genuinely two-sample
significance tests (Wilcoxon, Friedman), so the independent/paired-samples textbook form is the
correct reading here, not T2-17's per-metric-delta dz.

Dependency scoping (why scipy is the only new dependency, matching B2-01's own "scipy behind the
[stats] extra" text): Wilcoxon (`scipy.stats.wilcoxon`), Friedman
(`scipy.stats.friedmanchisquare`), the Nemenyi post-hoc critical value
(`scipy.stats.studentized_range`), and P(A>B) (`scipy.stats.mannwhitneyu`) all come from scipy.stats
directly — no additional package (no scikit-posthocs, no pingouin) is needed. Holm step-down
correction, Cohen's d, Win Rate, Breakdown Point, and ICC are all implementable with pure
`statistics`/`math` and do not require scipy at all — only the four significance-test functions
import scipy, lazily, at call time (matching the existing lazy-import-with-graceful-message pattern
in `bathos.cli`'s `view`/`export --html` commands for the `[viz]` extra, except this module degrades
rather than hard-exits — see `run_stats_battery`'s `scipy_available` handling).

Nonrepudiation note (why this module does NOT touch signing): the DAG's Q6 source text also lists
"Nonrepudiation: K-Veritas-style tamper-evident signature (RSA-PSS-SHA256)..." as a *separate*
mechanical criterion from the six implemented here. That criterion is explicitly out of scope per
the existing ADR `.praxia/docs/decisions/260526_nonrepudiation-v06.md` (self-signed manifest only,
no crypto signing key in this codebase, K-Veritas RSA-PSS deferred to v0.7+ — see B2-07,
`bathos.prereg.verify_run_manifest`). This module implements the six *statistical* criteria only.

Baseline-budget-equivalence field note: `check_baseline_budget_equivalence` consumes
`Run.baseline_hpo_trials` / `Run.baseline_hpo_compute_budget` (B2-02, already merged) — the schema
fields that exist specifically so this check has data to compare against.

conclude_campaign wiring — deliberately NOT done in this PR, and why (a genuine gap found during
implementation, not silently worked around): the natural design would have this module query a
campaign's runs and its `parent_campaign_id`'s runs (the existing single-parent baseline-comparison
field -- B2-03's own DAG text calls extending it to multi-parent "natural bathos schema evolution
over existing PROV lineage", confirming `parent_campaign_id` is already the pre-existing
single-parent mechanism), pulling per-run metric values from `Run.metadata` (a free-form JSON
column that exists specifically for this kind of caller-defined data) keyed by an explicit,
caller-supplied metric name -- mirroring Union Gate's own explicit-opt-in-via-`claim_path` pattern.

That design was implemented and then removed after its own integration test failed: `Run.metadata`
is never actually populated by anything in this codebase's live write path. `COOL_SCHEMA` has no
`metadata` field at all (confirmed against `bathos.harness_run`'s own module docstring, which
already flags this exact gap for its own, unrelated reasons: "`Run.to_arrow` serializes with
COOL_SCHEMA, which has no `metadata` field at all -- it is silently dropped at the cool tier").
`compact()`'s `INSERT INTO runs` does reference `run.metadata`, but by the time a cool fragment is
read back for compaction, `Run.from_arrow_row` has already defaulted it to `"{}"` -- there is no
code path anywhere in `src/bathos/*.py` that ever assigns `Run.metadata` a non-default value. A
DB-querying wrapper built on that column would silently find nothing on every real campaign,
which is worse than not providing the wrapper. This module therefore exposes only the pure,
caller-supplied-arrays contract (`run_stats_battery`) -- fixing `metadata`'s cool-tier persistence
is a separate, unrelated schema-plumbing gap, out of scope for a statistics-battery PR to
silently absorb.
"""

from __future__ import annotations

import math
import statistics
import warnings
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

Verdict = Literal["pass", "confounded", "underpowered"]


class StatsGateInputError(Exception):
    """Structural validation failure: mismatched sample sizes, too few samples, non-finite values."""


class ScipyUnavailableError(Exception):
    """scipy (the bathos[stats] extra) is not installed; a significance test cannot run."""


def _require_scipy():
    try:
        import scipy.stats as scipy_stats
    except ImportError as e:
        msg = "scipy is not installed. Install with: uv tool install 'bathos[stats]'"
        raise ScipyUnavailableError(msg) from e
    return scipy_stats


def _require_finite(values: Sequence[float], label: str) -> None:
    non_finite = [v for v in values if not math.isfinite(v)]
    if non_finite:
        msg = f"{label} must be finite (no NaN/Inf); found {non_finite}"
        raise StatsGateInputError(msg)


@dataclass(frozen=True, slots=True)
class SignificanceResult:
    """A significance test's raw statistic and p-value."""

    statistic: float
    p_value: float


def wilcoxon_signed_rank(
    candidate: Sequence[float], baseline: Sequence[float]
) -> SignificanceResult:
    """Paired Wilcoxon signed-rank test — the pairwise significance test named in Q6.

    Args:
        candidate: candidate values, one per matched task/seed.
        baseline: baseline values, index-aligned with `candidate` (same task/seed at each index).

    Raises:
        StatsGateInputError: `candidate`/`baseline` differ in length, have fewer than 2 pairs, or
            contain non-finite values.
        ScipyUnavailableError: scipy is not installed.
    """
    if len(candidate) != len(baseline):
        msg = f"candidate ({len(candidate)}) and baseline ({len(baseline)}) must be the same length"
        raise StatsGateInputError(msg)
    if len(candidate) < 2:
        msg = f"need at least 2 paired samples for Wilcoxon signed-rank, got {len(candidate)}"
        raise StatsGateInputError(msg)
    _require_finite(candidate, "candidate")
    _require_finite(baseline, "baseline")

    scipy_stats = _require_scipy()
    # scipy's normal-approximation code path divides by a zero standard error when every paired
    # difference is exactly zero; the RuntimeWarning is benign (scipy still returns the correct
    # statistic=0.0, p_value=1.0 via its exact-method fallback -- verified against this exact
    # all-zero-difference case), but left unsuppressed it would leak into a caller's logs (e.g.
    # `bth campaign conclude`) on every "candidate identical to baseline" comparison.
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message="invalid value encountered in scalar divide")
        result = scipy_stats.wilcoxon(candidate, baseline)
    return SignificanceResult(statistic=float(result.statistic), p_value=float(result.pvalue))


def friedman_test(*groups: Sequence[float]) -> SignificanceResult:
    """Friedman test for >=3 related (multi-model) groups, all the same length (blocked design).

    Raises:
        StatsGateInputError: fewer than 3 groups, groups of differing length, or fewer than 3
            observations per group (Friedman is degenerate below that).
        ScipyUnavailableError: scipy is not installed.
    """
    if len(groups) < 3:
        msg = f"Friedman test needs >= 3 groups (multi-model), got {len(groups)}"
        raise StatsGateInputError(msg)
    lengths = {len(g) for g in groups}
    if len(lengths) != 1:
        msg = f"all groups must be the same length (blocked design), got lengths {sorted(lengths)}"
        raise StatsGateInputError(msg)
    n = lengths.pop()
    if n < 3:
        msg = f"need at least 3 observations per group for a well-defined Friedman test, got {n}"
        raise StatsGateInputError(msg)
    for g in groups:
        _require_finite(g, "group")

    scipy_stats = _require_scipy()
    statistic, p_value = scipy_stats.friedmanchisquare(*groups)
    return SignificanceResult(statistic=float(statistic), p_value=float(p_value))


def nemenyi_posthoc(*groups: Sequence[float]) -> list[list[float]]:
    """Post-hoc Nemenyi pairwise comparison, run after a significant Friedman test.

    Standard formula (Demsar 2006): for k related groups of n blocks each, average-rank each
    group within every block, then for each pair (i, j) compute the studentized statistic
    `q = (R_i - R_j) / sqrt(k(k+1) / (6n))` and read a two-sided p-value off the
    (Tukey-family) studentized range distribution with `k` groups, infinite df.

    Returns:
        A k x k matrix of p-values (symmetric, zero diagonal) in the same group order as the
        input.

    Raises:
        StatsGateInputError: fewer than 3 groups, or groups of differing length.
        ScipyUnavailableError: scipy is not installed.
    """
    if len(groups) < 3:
        msg = f"Nemenyi post-hoc needs >= 3 groups, got {len(groups)}"
        raise StatsGateInputError(msg)
    lengths = {len(g) for g in groups}
    if len(lengths) != 1:
        msg = f"all groups must be the same length (blocked design), got lengths {sorted(lengths)}"
        raise StatsGateInputError(msg)
    n = lengths.pop()
    k = len(groups)

    scipy_stats = _require_scipy()

    # Average rank per block (row), 1 = best (lowest value) by convention; ties get the average
    # rank of the tied positions.
    avg_ranks = [0.0] * k
    for block_idx in range(n):
        block_values = [groups[g][block_idx] for g in range(k)]
        order = sorted(range(k), key=lambda i: block_values[i])
        ranks = [0.0] * k
        i = 0
        while i < k:
            j = i
            while j + 1 < k and block_values[order[j + 1]] == block_values[order[i]]:
                j += 1
            avg_rank_for_tie = (i + 1 + j + 1) / 2.0
            for t in range(i, j + 1):
                ranks[order[t]] = avg_rank_for_tie
            i = j + 1
        for g in range(k):
            avg_ranks[g] += ranks[g] / n

    p_matrix = [[0.0] * k for _ in range(k)]
    se = math.sqrt(k * (k + 1) / (6.0 * n))
    for i in range(k):
        for j in range(k):
            if i == j:
                continue
            q = abs(avg_ranks[i] - avg_ranks[j]) / se
            # Nemenyi's q uses the studentized range statistic directly (not q/sqrt(2)); the
            # studentized_range survival function gives the two-sided p-value.
            p_matrix[i][j] = float(scipy_stats.studentized_range.sf(q, k, math.inf))
    return p_matrix


def holm_correction(p_values: Sequence[float], alpha: float = 0.05) -> list[bool]:
    """Holm-Bonferroni step-down correction. Pure Python, no scipy needed.

    Sorts p-values ascending; hypothesis at sorted rank r (0-indexed) rejects iff
    `p_(r) <= alpha / (n - r)` AND every smaller p-value also rejected (Holm's step-down: the
    first non-rejection stops the whole chain, even if a later, larger p-value would individually
    clear its own threshold).

    Returns:
        A list of booleans (reject null / significant), in the SAME order as the input
        `p_values` — not sorted order.

    Raises:
        StatsGateInputError: `p_values` is empty, or contains a value outside [0, 1].
    """
    n = len(p_values)
    if n == 0:
        msg = "p_values must not be empty"
        raise StatsGateInputError(msg)
    out_of_range = [p for p in p_values if not (0.0 <= p <= 1.0)]
    if out_of_range:
        msg = f"p_values must be in [0, 1], found {out_of_range}"
        raise StatsGateInputError(msg)

    order = sorted(range(n), key=lambda i: p_values[i])
    reject = [False] * n
    for rank, idx in enumerate(order):
        threshold = alpha / (n - rank)
        if p_values[idx] <= threshold:
            reject[idx] = True
        else:
            break
    return reject


def cohens_d(candidate: Sequence[float], baseline: Sequence[float]) -> float:
    """Textbook independent-samples Cohen's d: `(mean(candidate) - mean(baseline)) / pooled_sd`.

    Distinct from `xtrax.loop.multi_metric_ratchet`'s Cohen's dz (a paired-difference variant
    computed on a single delta vector) -- this is the two-sample form, matching Q6's context
    (sitting alongside Wilcoxon/Friedman, genuinely two-sample tests).

    Returns:
        `math.inf` if both samples have zero variance and `mean(candidate) > mean(baseline)`;
        `0.0` if both have zero variance and equal means; `-math.inf` if zero variance and
        `mean(candidate) < mean(baseline)` -- a degenerate-variance edge case, not a NaN.

    Raises:
        StatsGateInputError: either sample has fewer than 2 points (variance undefined), or
            contains non-finite values.
    """
    n1, n2 = len(candidate), len(baseline)
    if n1 < 2 or n2 < 2:
        msg = f"need at least 2 points in each sample for Cohen's d, got {n1} and {n2}"
        raise StatsGateInputError(msg)
    _require_finite(candidate, "candidate")
    _require_finite(baseline, "baseline")

    mean1, mean2 = statistics.mean(candidate), statistics.mean(baseline)
    var1, var2 = statistics.variance(candidate), statistics.variance(baseline)
    pooled_var = ((n1 - 1) * var1 + (n2 - 1) * var2) / (n1 + n2 - 2)
    if pooled_var == 0:
        if mean1 == mean2:
            return 0.0
        return math.inf if mean1 > mean2 else -math.inf
    pooled_sd = math.sqrt(pooled_var)
    return (mean1 - mean2) / pooled_sd


def win_rate(
    candidate: Sequence[float], baseline: Sequence[float], *, higher_is_better: bool = True
) -> float:
    """Fraction of matched (candidate_i, baseline_i) pairs where the candidate wins.

    Raises:
        StatsGateInputError: `candidate`/`baseline` differ in length, or are empty.
    """
    if len(candidate) != len(baseline):
        msg = f"candidate ({len(candidate)}) and baseline ({len(baseline)}) must be the same length"
        raise StatsGateInputError(msg)
    n = len(candidate)
    if n == 0:
        msg = "candidate/baseline must not be empty"
        raise StatsGateInputError(msg)
    wins = sum(1 for c, b in zip(candidate, baseline) if (c > b if higher_is_better else c < b))
    return wins / n


def _breakdown_point(win_count: int, n: int, win_rate_threshold: float) -> float:
    """Same k/n search formula as `xtrax.loop.multi_metric_ratchet._breakdown_point`: the
    smallest `k / n` (removed count over the total comparison count, removing winning
    comparisons specifically) that drops the win rate below `win_rate_threshold`. 0.0 if the win
    rate is already below threshold (nothing to flip)."""
    if n == 0:
        return 0.0
    wr = win_count / n
    if wr < win_rate_threshold:
        return 0.0

    max_removable = min(win_count, n - 1)
    for k in range(1, max_removable + 1):
        if (win_count - k) / (n - k) < win_rate_threshold:
            return k / n
    return max_removable / n


def breakdown_point(
    candidate: Sequence[float],
    baseline: Sequence[float],
    *,
    higher_is_better: bool = True,
    win_rate_threshold: float = 0.6,
) -> float:
    """The composed Q6 stability check: how robust the win-rate verdict is to removing a small
    number of comparisons. See `_breakdown_point` for the exact search formula.

    Raises:
        StatsGateInputError: `candidate`/`baseline` differ in length, or are empty.
    """
    if len(candidate) != len(baseline):
        msg = f"candidate ({len(candidate)}) and baseline ({len(baseline)}) must be the same length"
        raise StatsGateInputError(msg)
    n = len(candidate)
    if n == 0:
        msg = "candidate/baseline must not be empty"
        raise StatsGateInputError(msg)
    win_count = sum(
        1 for c, b in zip(candidate, baseline) if (c > b if higher_is_better else c < b)
    )
    return _breakdown_point(win_count, n, win_rate_threshold)


def probability_of_superiority(candidate: Sequence[float], baseline: Sequence[float]) -> float:
    """P(A>B): the probability a randomly drawn candidate value exceeds a randomly drawn
    baseline value, via the Mann-Whitney U statistic (`U / (n1 * n2)`), independent samples
    (unlike Wilcoxon, does NOT require paired/index-aligned samples).

    Raises:
        StatsGateInputError: either sample has fewer than 1 point, or is empty.
        ScipyUnavailableError: scipy is not installed.
    """
    n1, n2 = len(candidate), len(baseline)
    if n1 == 0 or n2 == 0:
        msg = "candidate and baseline must both be non-empty"
        raise StatsGateInputError(msg)
    _require_finite(candidate, "candidate")
    _require_finite(baseline, "baseline")

    scipy_stats = _require_scipy()
    result = scipy_stats.mannwhitneyu(candidate, baseline, alternative="two-sided")
    return float(result.statistic) / (n1 * n2)


def intraclass_correlation(subject_replicates: Sequence[Sequence[float]]) -> float:
    """ICC(1): one-way random-effects intraclass correlation (Shrout & Fleiss 1979 /
    McGraw & Wong 1996 ICC(1,1)) -- how consistently repeated seeds (the "raters") agree with
    each other across subjects (tasks), the Q6 "Replication: >=3 independent seeds, ICC > 0.990"
    check.

    `ICC(1) = (MSB - MSW) / (MSB + (k - 1) * MSW)`, where MSB is the between-subject mean square
    and MSW is the within-subject mean square from a one-way ANOVA over `subject_replicates`.

    Args:
        subject_replicates: one sequence per subject/task; each subject's sequence holds its
            seed-replicate values (>= 3 seeds per Q6, though this function itself only requires
            >= 2 to be well-defined — the >= 3 floor is a caller-level policy, enforced by
            `run_stats_battery`, not here).

    Raises:
        StatsGateInputError: fewer than 2 subjects, any subject has fewer than 2 replicates, or
            replicate counts differ across subjects (a balanced design is required by this
            one-way formula).
    """
    if len(subject_replicates) < 2:
        msg = f"need at least 2 subjects for ICC, got {len(subject_replicates)}"
        raise StatsGateInputError(msg)
    replicate_counts = {len(s) for s in subject_replicates}
    if len(replicate_counts) != 1:
        msg = f"all subjects must have the same number of replicates (balanced design), got {sorted(replicate_counts)}"
        raise StatsGateInputError(msg)
    k = replicate_counts.pop()
    if k < 2:
        msg = f"need at least 2 replicates per subject for ICC, got {k}"
        raise StatsGateInputError(msg)
    n = len(subject_replicates)

    for s in subject_replicates:
        _require_finite(s, "subject replicate")

    all_values = [v for s in subject_replicates for v in s]
    grand_mean = statistics.mean(all_values)

    subject_means = [statistics.mean(s) for s in subject_replicates]
    ss_between = k * sum((m - grand_mean) ** 2 for m in subject_means)
    ms_between = ss_between / (n - 1)

    ss_within = sum(
        (v - subject_means[i]) ** 2 for i, s in enumerate(subject_replicates) for v in s
    )
    df_within = n * (k - 1)
    if df_within == 0:
        msg = "degrees of freedom within is zero; need k >= 2 replicates per subject"
        raise StatsGateInputError(msg)
    ms_within = ss_within / df_within

    denom = ms_between + (k - 1) * ms_within
    if denom == 0:
        return 1.0 if ms_within == 0 else 0.0
    return (ms_between - ms_within) / denom


@dataclass(frozen=True, slots=True)
class BaselineBudgetResult:
    """Q6's "Baseline equivalence" check: whether the baseline arm got at least as much HPO
    budget (trials and/or compute) as the candidate/proposal arm."""

    equivalent: bool
    reason: str


def check_baseline_budget_equivalence(
    *,
    baseline_hpo_trials: int | None,
    candidate_hpo_trials: int | None,
    baseline_hpo_compute_budget: float | None,
    candidate_hpo_compute_budget: float | None,
) -> BaselineBudgetResult:
    """Q6: "fail any run where baseline gets fewer HPO trials/compute than proposal."

    A `None` on either side of a given dimension (trials or compute) skips that dimension's
    check — no comparison is possible without both values recorded (matching `Run.seed`/
    `baseline_hpo_trials`/`baseline_hpo_compute_budget`'s own all-optional, caller-populated
    design, B2-02). `equivalent=True` (vacuously) if neither dimension has both values present —
    nothing to compare, not a failure.
    """
    reasons: list[str] = []
    if (
        baseline_hpo_trials is not None
        and candidate_hpo_trials is not None
        and baseline_hpo_trials < candidate_hpo_trials
    ):
        reasons.append(
            f"baseline_hpo_trials ({baseline_hpo_trials}) < candidate_hpo_trials "
            f"({candidate_hpo_trials})"
        )
    if (
        baseline_hpo_compute_budget is not None
        and candidate_hpo_compute_budget is not None
        and baseline_hpo_compute_budget < candidate_hpo_compute_budget
    ):
        reasons.append(
            f"baseline_hpo_compute_budget ({baseline_hpo_compute_budget}) < "
            f"candidate_hpo_compute_budget ({candidate_hpo_compute_budget})"
        )
    if reasons:
        return BaselineBudgetResult(equivalent=False, reason="; ".join(reasons))
    return BaselineBudgetResult(equivalent=True, reason="")


@dataclass(frozen=True, slots=True)
class StatsBatteryVerdict:
    """The composed Q6 battery verdict.

    `verdict`:
      - "pass": every enforced criterion cleared its threshold.
      - "confounded": scipy was available but one or more criteria failed.
      - "underpowered": scipy was not installed, so the significance tests (Wilcoxon/P(A>B))
        could not run — a graceful advisory downgrade, not a hard failure (`reasons` names it).

    `icc` and `baseline_budget_equivalent` are `None` when the caller did not supply
    `seed_replicates` / HPO-budget fields respectively — those two Q6 criteria are opt-in
    (nothing to compute without the caller-supplied data), matching `Run.seed`'s own
    caller-populated design (B2-02).
    """

    verdict: Verdict
    scipy_available: bool
    reasons: tuple[str, ...]
    cohens_d: float
    win_rate: float
    breakdown_point: float
    p_superiority: float | None
    wilcoxon_p_value: float | None
    icc: float | None
    baseline_budget_equivalent: bool | None


def run_stats_battery(
    candidate_values: Sequence[float],
    baseline_values: Sequence[float],
    *,
    seed_replicates: Sequence[Sequence[float]] | None = None,
    higher_is_better: bool = True,
    baseline_hpo_trials: int | None = None,
    candidate_hpo_trials: int | None = None,
    baseline_hpo_compute_budget: float | None = None,
    candidate_hpo_compute_budget: float | None = None,
    alpha: float = 0.05,
    win_rate_threshold: float = 0.6,
    p_superiority_threshold: float = 0.75,
    breakdown_point_threshold: float = 0.2,
    cohens_d_threshold: float = 0.2,
    icc_threshold: float = 0.990,
) -> StatsBatteryVerdict:
    """The composed Q6 / AC-15 / AC-17 battery: significance + effect size + consistency +
    stability + (optional) replication + (optional) baseline-budget equivalence.

    Args:
        candidate_values / baseline_values: matched per-task/per-seed values, index-aligned
            (same task/seed at each index) — required for Wilcoxon and Win Rate; also used for
            Cohen's d, Breakdown Point, and P(A>B) (which don't themselves require pairing, but
            reuse the same aligned arrays for a single consistent contract).
        seed_replicates: optional; when supplied, enables the ICC replication check (Q6 requires
            >= 3 seeds per subject for this to be meaningful — enforced here, not in
            `intraclass_correlation` itself, which only requires >= 2).
        baseline_hpo_trials/candidate_hpo_trials/baseline_hpo_compute_budget/
            candidate_hpo_compute_budget: optional; when at least one same-dimension pair is
            supplied, enables the baseline-budget-equivalence check.
        alpha: the Wilcoxon significance threshold (Q6's own "Significance:... alpha=0.05");
            `significance_pass = wilcoxon_p_value < alpha` is one of the composed pass/fail
            criteria, checked alongside (not instead of) effect size / consistency / stability.
            Holm step-down correction (`holm_correction`) is a separate, standalone utility for a
            caller comparing p-values ACROSS multiple batteries (e.g. several candidate variants)
            — this single-comparison composed function has only one p-value to correct, so it does
            not invoke `holm_correction` itself.

    Returns:
        A `StatsBatteryVerdict`. Never raises for a normal "candidate doesn't clear the bar"
        outcome (mirrors `xtrax.loop.multi_metric_ratchet`'s pure-decision-function stance) —
        only structural input errors (`StatsGateInputError`) propagate. A missing scipy install
        does NOT raise; it downgrades to `verdict="underpowered"`.

    Raises:
        StatsGateInputError: candidate_values/baseline_values are malformed (see
            `wilcoxon_signed_rank`, `cohens_d`, `win_rate`, `breakdown_point`), or
            `seed_replicates` is supplied with fewer than 3 seeds per subject.
    """
    if seed_replicates is not None:
        replicate_counts = {len(s) for s in seed_replicates}
        min_replicates = min(replicate_counts) if replicate_counts else 0
        if min_replicates < 3:
            msg = f"Q6 requires >= 3 seeds per subject for the ICC replication check, got {min_replicates}"
            raise StatsGateInputError(msg)

    reasons: list[str] = []

    d = cohens_d(candidate_values, baseline_values)
    wr = win_rate(candidate_values, baseline_values, higher_is_better=higher_is_better)
    bp = breakdown_point(
        candidate_values,
        baseline_values,
        higher_is_better=higher_is_better,
        win_rate_threshold=win_rate_threshold,
    )

    scipy_available = True
    p_superiority: float | None = None
    wilcoxon_p_value: float | None = None
    try:
        p_superiority = probability_of_superiority(candidate_values, baseline_values)
        wilcoxon_result = wilcoxon_signed_rank(candidate_values, baseline_values)
        wilcoxon_p_value = wilcoxon_result.p_value
    except ScipyUnavailableError:
        scipy_available = False
        reasons.append(
            "scipy not installed (bathos[stats] extra missing); significance tests skipped"
        )

    icc_val: float | None = None
    if seed_replicates is not None:
        icc_val = intraclass_correlation(seed_replicates)

    budget_result: BaselineBudgetResult | None = None
    if (baseline_hpo_trials is not None and candidate_hpo_trials is not None) or (
        baseline_hpo_compute_budget is not None and candidate_hpo_compute_budget is not None
    ):
        budget_result = check_baseline_budget_equivalence(
            baseline_hpo_trials=baseline_hpo_trials,
            candidate_hpo_trials=candidate_hpo_trials,
            baseline_hpo_compute_budget=baseline_hpo_compute_budget,
            candidate_hpo_compute_budget=candidate_hpo_compute_budget,
        )

    if not scipy_available:
        return StatsBatteryVerdict(
            verdict="underpowered",
            scipy_available=False,
            reasons=tuple(reasons),
            cohens_d=d,
            win_rate=wr,
            breakdown_point=bp,
            p_superiority=None,
            wilcoxon_p_value=None,
            icc=icc_val,
            baseline_budget_equivalent=budget_result.equivalent if budget_result else None,
        )

    significance_pass = wilcoxon_p_value is not None and wilcoxon_p_value < alpha
    consistency_pass = wr >= win_rate_threshold or (
        p_superiority is not None and p_superiority >= p_superiority_threshold
    )
    effect_size_pass = d >= cohens_d_threshold
    stability_pass = bp >= breakdown_point_threshold
    replication_pass = icc_val is None or icc_val > icc_threshold
    budget_pass = budget_result is None or budget_result.equivalent

    if not significance_pass:
        reasons.append(f"significance failed: Wilcoxon p={wilcoxon_p_value:.4f} >= alpha={alpha}")
    if not consistency_pass:
        reasons.append(
            f"consistency failed: win_rate={wr:.4f} < {win_rate_threshold} and "
            f"P(A>B)={p_superiority:.4f} < {p_superiority_threshold}"
        )
    if not effect_size_pass:
        reasons.append(f"effect size failed: Cohen's d={d:.4f} < {cohens_d_threshold}")
    if not stability_pass:
        reasons.append(f"stability failed: breakdown_point={bp:.4f} < {breakdown_point_threshold}")
    if not replication_pass:
        reasons.append(f"replication failed: ICC={icc_val:.4f} <= {icc_threshold}")
    if not budget_pass:
        reasons.append(f"baseline budget equivalence failed: {budget_result.reason}")

    verdict: Verdict = (
        "pass"
        if (
            significance_pass
            and consistency_pass
            and effect_size_pass
            and stability_pass
            and replication_pass
            and budget_pass
        )
        else "confounded"
    )

    return StatsBatteryVerdict(
        verdict=verdict,
        scipy_available=True,
        reasons=tuple(reasons),
        cohens_d=d,
        win_rate=wr,
        breakdown_point=bp,
        p_superiority=p_superiority,
        wilcoxon_p_value=wilcoxon_p_value,
        icc=icc_val,
        baseline_budget_equivalent=budget_result.equivalent if budget_result else None,
    )


__all__ = [
    "BaselineBudgetResult",
    "ScipyUnavailableError",
    "SignificanceResult",
    "StatsBatteryVerdict",
    "StatsGateInputError",
    "breakdown_point",
    "check_baseline_budget_equivalence",
    "cohens_d",
    "friedman_test",
    "holm_correction",
    "intraclass_correlation",
    "nemenyi_posthoc",
    "probability_of_superiority",
    "run_stats_battery",
    "wilcoxon_signed_rank",
    "win_rate",
]
