"""stats_metrics 的单元测试：用构造数据把每个口径钉死。

运行：
    python scripts/test_stats_metrics.py -v

为什么不用随机数据：统计函数最容易错在边界（空、单点、全同值、并列、容差边界、
上限溢出），随机数据恰好绕开这些点，只在均值上"看起来对"。所以下面所有
期望值都是手算出来的，或者是有明确数学恒等式的。

这套测试防的是「报告里的数字悄悄变了」——任何口径改动都必须先改这里。
"""

from __future__ import annotations

import math
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import stats_metrics as sm  # noqa: E402


# 四档 verdict 的真实取值，直接用字符串，验证「任意可哈希标签」这条承诺
SP, P, B, R = "strong_pass", "pass", "borderline", "reject"


class StatsMetricsTest(unittest.TestCase):
    # ------------------------------------------------------------ 误差
    def test_mae_known_value(self) -> None:
        """手算：|80-70| + |60-60| = 10，除以 2 → 5.0。"""
        self.assertEqual(sm.mae([(80, 70), (60, 60)]), 5.0)

    def test_mae_rejects_empty_and_bad_shape(self) -> None:
        with self.assertRaises(ValueError):
            sm.mae([])
        with self.assertRaises(ValueError):
            sm.mae([(10, 20, 30)])

    def test_rmse_known_value(self) -> None:
        """手算：sqrt((100 + 0) / 2) = sqrt(50)。"""
        self.assertAlmostEqual(sm.rmse([(80, 70), (60, 60)]), math.sqrt(50.0), places=12)

    def test_rmse_penalizes_outliers_more_than_mae(self) -> None:
        """三个样本差 0 / 40 / 0：mae = 40/3 ≈ 13.33，rmse = √(1600/3) ≈ 23.09。

        大偏差被平方放大，所以同一份数据上 rmse 必然不低于 mae —— 报告里
        两个数都列出来，差得越多说明"错得集中的一两份"越突出。
        """
        pairs = [(0, 0), (0, 40), (0, 0)]
        self.assertAlmostEqual(sm.mae(pairs), 40.0 / 3.0, places=12)
        self.assertAlmostEqual(sm.rmse(pairs), math.sqrt(1600.0 / 3.0), places=12)
        self.assertGreater(sm.rmse(pairs), sm.mae(pairs))

    # ------------------------------------------------------------ 相关
    def test_pearson_perfect_positive(self) -> None:
        self.assertEqual(sm.pearson([10, 20, 30, 40, 50], [20, 40, 60, 80, 100]), 1.0)

    def test_pearson_perfect_negative(self) -> None:
        self.assertEqual(sm.pearson([10, 20, 30, 40, 50], [100, 80, 60, 40, 20]), -1.0)

    def test_pearson_short_or_constant_is_none(self) -> None:
        """两点必然在一条直线上，r 恒为 ±1，是假信号；全同值分母为 0。"""
        self.assertIsNone(sm.pearson([1, 2], [3, 4]))
        self.assertIsNone(sm.pearson([], []))
        self.assertIsNone(sm.pearson([5, 5, 5, 5], [1, 2, 3, 4]))
        self.assertIsNone(sm.pearson([1, 2, 3, 4], [7, 7, 7, 7]))

    def test_pearson_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            sm.pearson([1, 2, 3], [1, 2])

    def test_spearman_catches_monotonic_but_nonlinear(self) -> None:
        """立方关系：秩次完全一致 → 1.0；Pearson 只有 0.94 左右。"""
        xs = [1, 2, 3, 4, 5]
        ys = [1, 8, 27, 64, 125]
        self.assertEqual(sm.spearman(xs, ys), 1.0)
        self.assertLess(sm.pearson(xs, ys), 0.99)

    def test_spearman_averages_tied_ranks(self) -> None:
        """并列必须取平均秩。

        xs=[1,2,2,3] 的秩是 [1, 2.5, 2.5, 4]，正确结果是 3/√10 ≈ 0.9487；
        若按出现顺序硬排成 [1,2,3,4]，结果会变成 0.8 —— 这个差别就是本用例的存在意义。
        """
        got = sm.spearman([1, 2, 2, 3], [1, 3, 2, 4])
        self.assertAlmostEqual(got, 3.0 / math.sqrt(10.0), places=12)

    def test_spearman_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            sm.spearman([1, 2, 3], [1, 2])

    # ------------------------------------------------------------ Cohen κ
    def test_cohen_kappa_perfect_agreement(self) -> None:
        labels = [SP, SP, P, B, R]
        self.assertEqual(sm.cohen_kappa(labels, list(labels)), 1.0)

    def test_cohen_kappa_random_like_agreement_is_zero(self) -> None:
        """固定构造：两人边际分布完全相同（每档 3 个），恰好 3/12 一致。

        Po = 0.25，Pe = 4 × (3/12)² = 0.25 → κ = 0。这正是"和随机猜差不多"。
        """
        a = [SP] * 3 + [P] * 3 + [B] * 3 + [R] * 3
        b = [SP, P, B, P, B, R, R, SP, SP, R, P, B]
        self.assertAlmostEqual(sm.cohen_kappa(a, b), 0.0, places=12)

    def test_cohen_kappa_four_verdicts_is_moderate(self) -> None:
        """真实构造：10 份简历，8 份同档。

        Po = 0.8；Pe = (3×2 + 3×3 + 2×3 + 2×2) / 100 = 0.25；
        κ = (0.8 - 0.25) / 0.75 = 0.7333…
        落在 (0.4, 0.9) 之间才说明"有明显一致但不是复制粘贴"。
        """
        a = [SP, SP, SP, P, P, P, B, B, R, R]
        b = [SP, SP, P, P, P, B, B, B, R, R]
        kappa = sm.cohen_kappa(a, b)
        self.assertIsNotNone(kappa)
        self.assertGreater(kappa, 0.4)
        self.assertLess(kappa, 0.9)
        self.assertAlmostEqual(kappa, 0.7333333333333334, places=9)

    def test_cohen_kappa_all_same_label_is_none(self) -> None:
        """两人都把全部样本放进同一档：Pe=1，无定义。不能返回 0，也不能除零。"""
        self.assertIsNone(sm.cohen_kappa([P, P, P, P], [P, P, P, P]))

    def test_cohen_kappa_empty_or_single_is_none(self) -> None:
        self.assertIsNone(sm.cohen_kappa([], []))
        self.assertIsNone(sm.cohen_kappa([P], [P]))
        self.assertIsNone(sm.cohen_kappa([P], [R]))

    def test_cohen_kappa_length_mismatch_raises(self) -> None:
        with self.assertRaises(ValueError):
            sm.cohen_kappa([SP, P, B], [SP, P])

    def test_cohen_kappa_rejects_unhashable_labels(self) -> None:
        with self.assertRaises(ValueError):
            sm.cohen_kappa([["x"], ["y"]], [["x"], ["y"]])

    # ------------------------------------------------------------ 二次加权 κ
    def test_qwk_perfect_agreement(self) -> None:
        scores = [10, 30, 50, 70, 90]
        self.assertEqual(sm.quadratic_weighted_kappa(scores, list(scores)), 1.0)

    def test_qwk_full_gap_is_far_worse_than_perfect(self) -> None:
        """差 100 分：观测不一致 = 2，期望不一致 = 1 → κ = -1。"""
        full_gap = sm.quadratic_weighted_kappa([0, 100], [100, 0])
        self.assertAlmostEqual(full_gap, -1.0, places=12)
        self.assertLess(full_gap, sm.quadratic_weighted_kappa([0, 100], [0, 100]))

    def test_qwk_small_gap_ranks_between_perfect_and_full_gap(self) -> None:
        """差 10 分 ≈ 0.9756，应当严格小于完全一致、严格大于差 100 分。"""
        perfect = sm.quadratic_weighted_kappa([0, 100], [0, 100])
        small = sm.quadratic_weighted_kappa([0, 100], [10, 90])
        full = sm.quadratic_weighted_kappa([0, 100], [100, 0])
        self.assertAlmostEqual(small, 1.0 - 0.02 / 0.82, places=12)
        self.assertEqual(perfect, 1.0)
        self.assertGreater(small, full)
        self.assertLess(small, 1.0)

    def test_qwk_single_sample_is_none(self) -> None:
        self.assertIsNone(sm.quadratic_weighted_kappa([80], [80]))
        self.assertIsNone(sm.quadratic_weighted_kappa([], []))

    def test_qwk_all_same_score_is_none(self) -> None:
        """全部落在同一档：期望不一致量为 0，κ 无定义（返回 None 而不是 1.0）。"""
        self.assertIsNone(sm.quadratic_weighted_kappa([50, 50, 50], [50, 50, 50]))

    def test_qwk_rounds_fractional_scores(self) -> None:
        """0.4 分会落回 10 档、0.6 分会进位到 91 档，与整数分完全一致 → 1.0。"""
        self.assertEqual(sm.quadratic_weighted_kappa([10.4, 90.6], [10, 91]), 1.0)

    def test_qwk_supports_custom_range(self) -> None:
        """1~5 分的维度分也要能用：lo=1, hi=5。"""
        self.assertEqual(sm.quadratic_weighted_kappa([1, 3, 5], [1, 3, 5], lo=1, hi=5), 1.0)
        self.assertAlmostEqual(sm.quadratic_weighted_kappa([1, 5], [5, 1], lo=1, hi=5), -1.0, places=12)

    def test_qwk_rejects_bad_range_and_mismatch(self) -> None:
        with self.assertRaises(ValueError):
            sm.quadratic_weighted_kappa([10, 20], [10, 20], lo=100, hi=0)
        with self.assertRaises(ValueError):
            sm.quadratic_weighted_kappa([10, 20, 30], [10, 20])

    # ------------------------------------------------------------ 容差一致率
    def test_agreement_within_counts_boundary_as_inside(self) -> None:
        """差值恰好等于 tol 算「在内」：(80,70) 差 10，tol=10 → 一致。"""
        self.assertEqual(sm.agreement_within([(80, 70), (60, 60)], 10), 1.0)
        self.assertEqual(sm.agreement_within([(80, 70), (60, 60)], 9.999), 0.5)

    def test_agreement_within_rejects_empty_and_negative_tol(self) -> None:
        with self.assertRaises(ValueError):
            sm.agreement_within([], 5)
        with self.assertRaises(ValueError):
            sm.agreement_within([(80, 70)], -1)

    def test_agreement_within_zero_tolerance_requires_exact(self) -> None:
        self.assertEqual(sm.agreement_within([(70, 70), (70, 71)], 0), 0.5)

    # ------------------------------------------------------------ 分布
    def test_diff_distribution_quantiles(self) -> None:
        """0~100 步长 10 的 11 个点，分位数可以手算，且必须满足 p25 ≤ median ≤ p75。"""
        values = [float(v) for v in range(0, 101, 10)]
        dist = sm.diff_distribution(values)
        self.assertEqual(dist["n"], 11)
        self.assertEqual(dist["min"], 0.0)
        self.assertEqual(dist["max"], 100.0)
        self.assertAlmostEqual(dist["p10"], 10.0, places=9)
        self.assertAlmostEqual(dist["p25"], 25.0, places=9)
        self.assertAlmostEqual(dist["median"], 50.0, places=9)
        self.assertAlmostEqual(dist["p75"], 75.0, places=9)
        self.assertAlmostEqual(dist["p90"], 90.0, places=9)
        self.assertAlmostEqual(dist["mean"], 50.0, places=9)
        self.assertLessEqual(dist["p25"], dist["median"])
        self.assertLessEqual(dist["median"], dist["p75"])

    def test_diff_distribution_stays_inside_range(self) -> None:
        """不规则的差值分布：所有分位数都必须落在 min..max 里，不能越界。"""
        dist = sm.diff_distribution([-12, 3, 3, 7, 7, 7, 20, 41])
        self.assertEqual(dist["min"], -12.0)
        self.assertEqual(dist["max"], 41.0)
        for key in ("p10", "p25", "median", "p75", "p90"):
            self.assertGreaterEqual(dist[key], dist["min"])
            self.assertLessEqual(dist[key], dist["max"])

    def test_diff_distribution_empty_and_single(self) -> None:
        empty = sm.diff_distribution([])
        self.assertEqual(empty["n"], 0)
        for key in ("min", "p10", "p25", "median", "p75", "p90", "max", "mean"):
            self.assertIsNone(empty[key])
        one = sm.diff_distribution([42])
        self.assertEqual(one["n"], 1)
        for key in ("min", "p10", "p25", "median", "p75", "p90", "max", "mean"):
            self.assertEqual(one[key], 42.0)

    def test_diff_distribution_constant_values(self) -> None:
        dist = sm.diff_distribution([7, 7, 7, 7])
        self.assertEqual(dist["min"], dist["max"])
        self.assertEqual(dist["p10"], 7.0)
        self.assertEqual(dist["median"], 7.0)

    # ------------------------------------------------------------ bootstrap
    def test_bootstrap_ci_is_reproducible(self) -> None:
        """同样的 seed 必须给出逐位相同的区间，否则报告里的置信区间每次刷新都变。"""
        pairs = [(80, 70), (60, 60), (90, 85), (40, 55), (70, 70), (55, 60)]
        first = sm.bootstrap_ci(pairs, sm.mae, n=400, seed=3)
        second = sm.bootstrap_ci(pairs, sm.mae, n=400, seed=3)
        self.assertIsInstance(first, tuple)
        self.assertEqual(len(first), 2)
        self.assertEqual(first, second)
        self.assertLessEqual(first[0], first[1])

    def test_bootstrap_ci_brackets_point_estimate(self) -> None:
        """点估计取 stat_fn(pairs)，必须落在区间内（区间会被撑到包住它）。"""
        pairs = [(80, 70), (60, 60), (90, 85), (40, 55), (70, 70), (55, 60)]
        ci = sm.bootstrap_ci(pairs, sm.mae, n=500, seed=0)
        point = sm.mae(pairs)
        self.assertLessEqual(ci[0], point)
        self.assertGreaterEqual(ci[1], point)

    def test_bootstrap_ci_default_parameters_run(self) -> None:
        """默认 n=2000 也要能跑（用 mae，成本可忽略），且区间含点估计。"""
        pairs = [(80, 70), (60, 60), (90, 85), (40, 55)]
        ci = sm.bootstrap_ci(pairs, sm.mae)
        self.assertLessEqual(ci[0], sm.mae(pairs))
        self.assertGreaterEqual(ci[1], sm.mae(pairs))

    def test_bootstrap_ci_handles_degenerate_inputs(self) -> None:
        self.assertIsNone(sm.bootstrap_ci([], sm.mae))
        self.assertIsNone(sm.bootstrap_ci([(80, 70)], sm.mae))
        # stat_fn 自己返回 None（例如全同值时算不出相关）→ 整条链返回 None
        self.assertIsNone(sm.bootstrap_ci([(80, 70), (60, 60)], lambda _pairs: None))

    def test_bootstrap_ci_rejects_bad_parameters(self) -> None:
        pairs = [(80, 70), (60, 60)]
        with self.assertRaises(ValueError):
            sm.bootstrap_ci(pairs, sm.mae, n=0)
        with self.assertRaises(ValueError):
            sm.bootstrap_ci(pairs, sm.mae, ci=1.5)

    def test_bootstrap_ci_constant_stat_is_a_point(self) -> None:
        ci = sm.bootstrap_ci([(80, 70), (60, 60)], lambda _pairs: 1.0, n=50, seed=1)
        self.assertEqual(ci, (1.0, 1.0))

    # ------------------------------------------------------------ 概况
    def test_summarize_basic(self) -> None:
        """sd 用样本标准差：mean=4，(4+0+4)/(3-1)=4，开方 = 2。"""
        got = sm.summarize([2, 4, 6])
        self.assertEqual(got["n"], 3)
        self.assertEqual(got["mean"], 4.0)
        self.assertAlmostEqual(got["sd"], 2.0, places=12)
        self.assertEqual(got["min"], 2.0)
        self.assertEqual(got["max"], 6.0)

    def test_summarize_empty_single_and_constant(self) -> None:
        empty = sm.summarize([])
        self.assertEqual(empty["n"], 0)
        for key in ("mean", "sd", "min", "max"):
            self.assertIsNone(empty[key])

        one = sm.summarize([88])
        self.assertEqual(one["n"], 1)
        self.assertEqual(one["mean"], 88.0)
        self.assertIsNone(one["sd"])
        self.assertEqual(one["min"], 88.0)
        self.assertEqual(one["max"], 88.0)

        flat = sm.summarize([70, 70, 70])
        self.assertEqual(flat["sd"], 0.0)
        self.assertEqual(flat["min"], flat["max"])

    def test_summarize_rejects_non_numeric(self) -> None:
        with self.assertRaises(ValueError):
            sm.summarize([70, "缺考"])

    # ------------------------------------------------------------ 全同值总检查
    def test_all_same_values_never_lie(self) -> None:
        """把「全同值」一次过一遍：该给 None 的给 None，该给 0 的给 0。

        这里专门防"除零 → 返回 0"这种伪装：0 会被读成"不相关/无一致性"，
        但那是一个结论，不是算不出来的托词。
        """
        flat = [5, 5, 5, 5]
        self.assertIsNone(sm.pearson(flat, flat))
        self.assertIsNone(sm.spearman(flat, flat))
        self.assertIsNone(sm.cohen_kappa([P] * 4, [P] * 4))
        self.assertIsNone(sm.quadratic_weighted_kappa([5] * 4, [5] * 4))
        self.assertEqual(sm.summarize(flat)["sd"], 0.0)
        self.assertEqual(sm.mae(list(zip(flat, flat))), 0.0)
        self.assertEqual(sm.agreement_within(list(zip(flat, flat)), 0), 1.0)


if __name__ == "__main__":
    unittest.main()
