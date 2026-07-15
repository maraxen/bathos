"""Tests for the B2-01 statistical battery (#2181, AC-15/AC-17).

Every synthetic-data case here follows BATHOS.md's mandate: verify the measurement pipeline
against synthetic ground truth before trusting it for any research conclusion. Where an exact
value can be hand-derived (Holm correction, Cohen's d, Win Rate, Breakdown Point, the ICC
perfect-agreement case), the test asserts that exact value. Where the underlying computation is
scipy's own well-tested internals (Wilcoxon, Friedman, Nemenyi, Mann-Whitney), the test asserts
DIRECTIONAL correctness on a clearly-separated synthetic case (significant) and a clearly-null
synthetic case (not significant) — the point being to catch a wired-backwards convention (e.g. an
inverted alternative hypothesis or a swapped statistic/p-value), not to re-derive scipy's math.
"""

import math

import pytest

from bathos.stats_gates import (
    BaselineBudgetResult,
    ScipyUnavailableError,
    StatsGateInputError,
    breakdown_point,
    check_baseline_budget_equivalence,
    cohens_d,
    friedman_test,
    holm_correction,
    intraclass_correlation,
    nemenyi_posthoc,
    probability_of_superiority,
    run_stats_battery,
    wilcoxon_signed_rank,
    win_rate,
)


class TestWilcoxonSignedRank:
    def test_consistent_positive_difference_is_significant(self):
        baseline = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        candidate = [b + (i + 1) for i, b in enumerate(baseline)]  # always +1..+8, always wins
        result = wilcoxon_signed_rank(candidate, baseline)
        assert result.p_value < 0.05

    def test_balanced_alternating_difference_is_not_significant(self):
        baseline = [10.0] * 10
        candidate = [11.0, 9.0, 11.0, 9.0, 11.0, 9.0, 11.0, 9.0, 11.0, 9.0]
        result = wilcoxon_signed_rank(candidate, baseline)
        assert result.p_value == pytest.approx(1.0, abs=1e-6)

    def test_mismatched_length_raises(self):
        with pytest.raises(StatsGateInputError, match="same length"):
            wilcoxon_signed_rank([1.0, 2.0], [1.0])

    def test_too_few_pairs_raises(self):
        with pytest.raises(StatsGateInputError, match="at least 2"):
            wilcoxon_signed_rank([1.0], [2.0])

    def test_non_finite_raises(self):
        with pytest.raises(StatsGateInputError, match="finite"):
            wilcoxon_signed_rank([1.0, math.inf], [1.0, 2.0])


class TestFriedmanTest:
    def test_consistently_ranked_groups_are_significant(self):
        n = 10
        low = [float(i) for i in range(n)]
        mid = [float(i) + 100 for i in range(n)]
        high = [float(i) + 200 for i in range(n)]
        result = friedman_test(low, mid, high)
        assert result.p_value < 0.05

    def test_random_noise_groups_are_not_significant(self):
        # Fixed, hand-picked values with no consistent within-block ranking across blocks.
        a = [5.0, 1.0, 9.0, 2.0, 7.0, 3.0, 8.0, 4.0, 6.0, 0.0]
        b = [1.0, 5.0, 2.0, 9.0, 3.0, 7.0, 4.0, 8.0, 0.0, 6.0]
        c = [9.0, 0.0, 6.0, 5.0, 2.0, 8.0, 1.0, 7.0, 3.0, 4.0]
        result = friedman_test(a, b, c)
        assert result.p_value > 0.05

    def test_fewer_than_3_groups_raises(self):
        with pytest.raises(StatsGateInputError, match=">= 3 groups"):
            friedman_test([1.0, 2.0, 3.0], [4.0, 5.0, 6.0])

    def test_mismatched_lengths_raise(self):
        with pytest.raises(StatsGateInputError, match="same length"):
            friedman_test([1.0, 2.0, 3.0], [4.0, 5.0, 6.0], [7.0, 8.0])


class TestNemenyiPosthoc:
    def test_identical_groups_have_p_one(self):
        n = 10
        a = [float(i) for i in range(n)]
        b = [float(i) for i in range(n)]  # identical to a in every block
        c = [float(i) + 100 for i in range(n)]  # always ranked highest
        matrix = nemenyi_posthoc(a, b, c)
        assert matrix[0][1] == pytest.approx(1.0, abs=1e-9)
        assert matrix[1][0] == pytest.approx(1.0, abs=1e-9)

    def test_consistently_separated_groups_have_low_p(self):
        n = 10
        a = [float(i) for i in range(n)]
        b = [float(i) for i in range(n)]
        c = [float(i) + 100 for i in range(n)]
        matrix = nemenyi_posthoc(a, b, c)
        assert matrix[0][2] < 0.05
        assert matrix[2][0] < 0.05

    def test_matrix_is_symmetric_zero_diagonal(self):
        a = [1.0, 2.0, 3.0, 4.0, 5.0]
        b = [2.0, 3.0, 4.0, 5.0, 6.0]
        c = [3.0, 1.0, 5.0, 2.0, 4.0]
        matrix = nemenyi_posthoc(a, b, c)
        for i in range(3):
            assert matrix[i][i] == 0.0
        for i in range(3):
            for j in range(3):
                if i != j:
                    assert matrix[i][j] == pytest.approx(matrix[j][i])


class TestHolmCorrection:
    def test_hand_computed_example(self):
        # alpha/5=0.01, alpha/4=0.0125, alpha/3=0.01667, alpha/2=0.025, alpha/1=0.05
        # p_(0)=0.01 <= 0.01 -> reject; p_(1)=0.02 <= 0.0125 -> False -> stop.
        p_values = [0.01, 0.02, 0.03, 0.04, 0.05]
        result = holm_correction(p_values, alpha=0.05)
        assert result == [True, False, False, False, False]

    def test_all_tiny_p_values_all_reject(self):
        p_values = [0.0001] * 5
        result = holm_correction(p_values, alpha=0.05)
        assert result == [True] * 5

    def test_all_large_p_values_none_reject(self):
        p_values = [0.9] * 5
        result = holm_correction(p_values, alpha=0.05)
        assert result == [False] * 5

    def test_preserves_original_order_not_sorted_order(self):
        p_values = [0.04, 0.01, 0.03, 0.02, 0.05]
        result = holm_correction(p_values, alpha=0.05)
        # sorted p: 0.01(idx1),0.02(idx3),0.03(idx2),0.04(idx0),0.05(idx4)
        # rank0: 0.01<=0.01 reject(idx1); rank1: 0.02<=0.0125? No -> stop.
        expected = [False, True, False, False, False]
        assert result == expected

    def test_empty_raises(self):
        with pytest.raises(StatsGateInputError, match="not be empty"):
            holm_correction([])

    def test_out_of_range_raises(self):
        with pytest.raises(StatsGateInputError, match=r"\[0, 1\]"):
            holm_correction([0.5, 1.5])


class TestCohensD:
    def test_hand_computed_example(self):
        candidate = [3.0, 5.0, 7.0, 9.0, 11.0]  # mean=7, var=10
        baseline = [1.0, 3.0, 5.0, 7.0, 9.0]  # mean=5, var=10
        # pooled_var = 10, pooled_sd = sqrt(10) = 3.16227766
        # d = (7-5)/3.16227766 = 0.6324555
        d = cohens_d(candidate, baseline)
        assert d == pytest.approx(0.6324555, abs=1e-5)

    def test_sign_flips_with_direction(self):
        higher = [10.0, 12.0, 14.0, 16.0]
        lower = [1.0, 3.0, 5.0, 7.0]
        assert cohens_d(higher, lower) > 0
        assert cohens_d(lower, higher) < 0

    def test_zero_variance_positive_mean_diff_is_inf(self):
        assert cohens_d([10.0, 10.0], [5.0, 5.0]) == math.inf

    def test_zero_variance_negative_mean_diff_is_neg_inf(self):
        assert cohens_d([5.0, 5.0], [10.0, 10.0]) == -math.inf

    def test_zero_variance_equal_means_is_zero(self):
        assert cohens_d([5.0, 5.0], [5.0, 5.0]) == 0.0

    def test_too_few_points_raises(self):
        with pytest.raises(StatsGateInputError, match="at least 2"):
            cohens_d([1.0], [1.0, 2.0])

    def test_non_finite_raises(self):
        with pytest.raises(StatsGateInputError, match="finite"):
            cohens_d([1.0, math.nan], [1.0, 2.0])


class TestWinRate:
    def test_hand_computed_example(self):
        candidate = [5.0, 3.0, 8.0]
        baseline = [3.0, 5.0, 2.0]
        # wins: 5>3 T, 3>5 F, 8>2 T -> 2/3
        assert win_rate(candidate, baseline) == pytest.approx(2.0 / 3.0)

    def test_all_wins_is_one(self):
        assert win_rate([10.0, 10.0], [5.0, 5.0]) == 1.0

    def test_all_losses_is_zero(self):
        assert win_rate([5.0, 5.0], [10.0, 10.0]) == 0.0

    def test_lower_is_better_direction(self):
        candidate = [1.0, 9.0]  # candidate wins index0 (lower), loses index1
        baseline = [5.0, 5.0]
        assert win_rate(candidate, baseline, higher_is_better=False) == 0.5

    def test_mismatched_length_raises(self):
        with pytest.raises(StatsGateInputError, match="same length"):
            win_rate([1.0], [1.0, 2.0])

    def test_empty_raises(self):
        with pytest.raises(StatsGateInputError, match="not be empty"):
            win_rate([], [])


class TestBreakdownPoint:
    def test_hand_computed_example(self):
        # 10 pairs, candidate wins 8 -> win_rate=0.8, threshold=0.6.
        # k=5: (8-5)/(10-5)=0.6, not <0.6 -> continue. k=6: (8-6)/(10-6)=0.5<0.6 -> BP=6/10=0.6
        candidate = [10.0] * 8 + [1.0] * 2
        baseline = [5.0] * 10
        bp = breakdown_point(candidate, baseline, win_rate_threshold=0.6)
        assert bp == pytest.approx(0.6)

    def test_win_rate_already_below_threshold_is_zero(self):
        candidate = [10.0] * 3 + [1.0] * 7  # win_rate=0.3 < 0.6
        baseline = [5.0] * 10
        assert breakdown_point(candidate, baseline, win_rate_threshold=0.6) == 0.0

    def test_all_wins_is_maximally_robust(self):
        candidate = [10.0] * 10
        baseline = [5.0] * 10
        bp = breakdown_point(candidate, baseline, win_rate_threshold=0.6)
        assert bp == pytest.approx(9.0 / 10.0)  # max_removable = min(10, 9) = 9

    def test_mismatched_length_raises(self):
        with pytest.raises(StatsGateInputError, match="same length"):
            breakdown_point([1.0], [1.0, 2.0])


class TestProbabilityOfSuperiority:
    def test_fully_separated_is_near_one(self):
        candidate = [100.0, 101.0, 102.0, 103.0]
        baseline = [1.0, 2.0, 3.0, 4.0]
        p_sup = probability_of_superiority(candidate, baseline)
        assert p_sup == pytest.approx(1.0)

    def test_fully_reversed_is_near_zero(self):
        candidate = [1.0, 2.0, 3.0, 4.0]
        baseline = [100.0, 101.0, 102.0, 103.0]
        p_sup = probability_of_superiority(candidate, baseline)
        assert p_sup == pytest.approx(0.0)

    def test_identical_distributions_is_near_half(self):
        candidate = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        baseline = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
        p_sup = probability_of_superiority(candidate, baseline)
        assert p_sup == pytest.approx(0.5, abs=0.05)

    def test_empty_raises(self):
        with pytest.raises(StatsGateInputError, match="non-empty"):
            probability_of_superiority([], [1.0])


class TestIntraclassCorrelation:
    def test_perfect_agreement_is_exactly_one(self):
        # Zero within-subject variance -> ICC = (MSB - 0) / (MSB + 0) = 1.0 exactly,
        # regardless of the specific subject means.
        subjects = [[1.0, 1.0, 1.0], [5.0, 5.0, 5.0], [10.0, 10.0, 10.0]]
        assert intraclass_correlation(subjects) == pytest.approx(1.0)

    def test_no_between_subject_signal_is_near_zero(self):
        # Every subject has the identical set of replicate values in different orders --
        # zero true between-subject difference, all "signal" is within-subject noise.
        subjects = [[1.0, 5.0, 9.0], [5.0, 9.0, 1.0], [9.0, 1.0, 5.0]]
        icc = intraclass_correlation(subjects)
        assert icc < 0.3

    def test_too_few_subjects_raises(self):
        with pytest.raises(StatsGateInputError, match="at least 2 subjects"):
            intraclass_correlation([[1.0, 2.0]])

    def test_unbalanced_design_raises(self):
        with pytest.raises(StatsGateInputError, match="same number of replicates"):
            intraclass_correlation([[1.0, 2.0], [1.0, 2.0, 3.0]])

    def test_too_few_replicates_raises(self):
        with pytest.raises(StatsGateInputError, match="at least 2 replicates"):
            intraclass_correlation([[1.0], [2.0]])


class TestCheckBaselineBudgetEquivalence:
    def test_equivalent_when_baseline_has_more_trials(self):
        result = check_baseline_budget_equivalence(
            baseline_hpo_trials=50,
            candidate_hpo_trials=30,
            baseline_hpo_compute_budget=None,
            candidate_hpo_compute_budget=None,
        )
        assert result == BaselineBudgetResult(equivalent=True, reason="")

    def test_fails_when_baseline_has_fewer_trials(self):
        result = check_baseline_budget_equivalence(
            baseline_hpo_trials=10,
            candidate_hpo_trials=30,
            baseline_hpo_compute_budget=None,
            candidate_hpo_compute_budget=None,
        )
        assert result.equivalent is False
        assert "baseline_hpo_trials" in result.reason

    def test_fails_when_baseline_has_less_compute(self):
        result = check_baseline_budget_equivalence(
            baseline_hpo_trials=None,
            candidate_hpo_trials=None,
            baseline_hpo_compute_budget=100.0,
            candidate_hpo_compute_budget=200.0,
        )
        assert result.equivalent is False
        assert "compute_budget" in result.reason

    def test_vacuously_equivalent_when_nothing_supplied(self):
        result = check_baseline_budget_equivalence(
            baseline_hpo_trials=None,
            candidate_hpo_trials=None,
            baseline_hpo_compute_budget=None,
            candidate_hpo_compute_budget=None,
        )
        assert result.equivalent is True

    def test_equal_budgets_are_equivalent(self):
        result = check_baseline_budget_equivalence(
            baseline_hpo_trials=30,
            candidate_hpo_trials=30,
            baseline_hpo_compute_budget=100.0,
            candidate_hpo_compute_budget=100.0,
        )
        assert result.equivalent is True


class TestRunStatsBattery:
    def _strong_win(self):
        baseline = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        candidate = [b * 1.5 for b in baseline]  # consistent, large, positive win
        return candidate, baseline

    def test_strong_candidate_passes(self):
        candidate, baseline = self._strong_win()
        verdict = run_stats_battery(candidate, baseline)
        assert verdict.verdict == "pass"
        assert verdict.scipy_available is True
        assert verdict.reasons == ()

    def test_equal_candidate_is_confounded(self):
        baseline = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0]
        candidate = list(baseline)
        verdict = run_stats_battery(candidate, baseline)
        assert verdict.verdict == "confounded"
        assert len(verdict.reasons) > 0

    def test_insufficient_n_fails_significance_despite_perfect_wins(self):
        # n=2: even a perfect, maximal win on both pairs cannot reach p < 0.05 on a
        # Wilcoxon signed-rank test (only 4 possible sign permutations -> min p = 0.5).
        # Isolates significance_pass from consistency/effect-size/stability, which all pass.
        candidate = [20.0, 40.0]
        baseline = [10.0, 20.0]
        verdict = run_stats_battery(candidate, baseline)
        assert verdict.verdict == "confounded"
        assert verdict.win_rate == 1.0
        assert verdict.cohens_d > 0.2
        assert verdict.wilcoxon_p_value == pytest.approx(0.5)
        assert any("significance failed" in r for r in verdict.reasons)

    def test_missing_scipy_downgrades_to_underpowered(self, monkeypatch):
        import bathos.stats_gates as stats_gates_module

        def _raise(*_args, **_kwargs):
            raise ScipyUnavailableError("simulated missing scipy")

        monkeypatch.setattr(stats_gates_module, "probability_of_superiority", _raise)
        candidate, baseline = self._strong_win()
        verdict = run_stats_battery(candidate, baseline)
        assert verdict.verdict == "underpowered"
        assert verdict.scipy_available is False
        assert any("scipy" in r for r in verdict.reasons)
        # Non-scipy metrics are still computed even when scipy is unavailable.
        assert verdict.cohens_d > 0
        assert verdict.win_rate == 1.0

    def test_seed_replicates_below_3_raises(self):
        candidate, baseline = self._strong_win()
        with pytest.raises(StatsGateInputError, match=">= 3 seeds"):
            run_stats_battery(candidate, baseline, seed_replicates=[[1.0, 2.0], [3.0, 4.0]])

    def test_baseline_budget_failure_downgrades_a_strong_candidate(self):
        candidate, baseline = self._strong_win()
        verdict = run_stats_battery(
            candidate,
            baseline,
            baseline_hpo_trials=5,
            candidate_hpo_trials=50,
        )
        assert verdict.verdict == "confounded"
        assert verdict.baseline_budget_equivalent is False
        assert any("baseline budget" in r for r in verdict.reasons)

    def test_replication_pass_with_high_icc_seed_replicates(self):
        candidate, baseline = self._strong_win()
        # 10 subjects (matching len(candidate)), 3 near-identical seed replicates each.
        seed_replicates = [[v, v, v] for v in candidate]
        verdict = run_stats_battery(candidate, baseline, seed_replicates=seed_replicates)
        assert verdict.icc == pytest.approx(1.0)
        assert verdict.verdict == "pass"
