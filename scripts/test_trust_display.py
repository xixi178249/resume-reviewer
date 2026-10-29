"""「这个分数准不准」的展示测试。

为什么值得有测试：
    这类文案最容易变成永远正确的空话——写「结果仅供参考」，谁都不会检查它是否
    与真实状态一致。这里锁三件事：
      1. 未验证时**必须**出现「未验证/验证过」的明确说法，不能含糊；
      2. 就绪时必须写出 n 与误差，不许只说「已验证」；
      3. 读取失败时按未验证处理，不能静默省略（省略会被读成没问题）。

运行：
    python scripts/test_trust_display.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import benchmark  # noqa: E402
import review  # noqa: E402
import server  # noqa: E402


class TrustLineTest(unittest.TestCase):
    """报告里那行「分数可信度」。"""

    def test_not_ready_says_unverified_and_counts(self):
        line = review._trust_line({
            "ok": True, "ready": False,
            "counts": {"dataset_size": 8, "dataset_target": 60, "reviewers": 0},
            "metrics": {"n": 0, "mae": None, "kappa_verdict": None},
        })
        self.assertIn("还没有被人工作答验证过", line)
        self.assertIn("8/60", line)
        self.assertIn("0/2", line)
        self.assertIn("不要当成准确率", line)

    def test_ready_reports_n_and_error(self):
        line = review._trust_line({
            "ok": True, "ready": True,
            "counts": {"dataset_size": 60, "dataset_target": 60, "reviewers": 2},
            "metrics": {"n": 60, "mae": 6.25, "kappa_verdict": 0.71},
        })
        self.assertIn("已用人工标注验证", line)
        self.assertIn("60 份", line)
        self.assertIn("6.2", line)     # 一位小数，不许写成 6.25 冒充精度
        self.assertIn("0.71", line)

    def test_unreadable_is_treated_as_unverified(self):
        for trust in (None, {"ok": False, "error": "缺少 scripts/benchmark.py"}):
            line = review._trust_line(trust)
            self.assertIn("未验证", line)
            self.assertNotIn("已用人工标注验证", line)


class MetricsStateTest(unittest.TestCase):
    """benchmark.state()：给页面用的状态，永远不抛异常、永远不读 heldout。"""

    def test_returns_not_ready_with_reasons_on_real_data(self):
        st = benchmark.state()
        self.assertFalse(st["ready"])
        self.assertTrue(st["reasons"], "未就绪必须给原因，否则页面只能写「未就绪」")
        self.assertEqual(st["counts"]["dataset_target"], 60)
        self.assertEqual(st["counts"]["dataset_size"], 8)
        # 指标全 None：给 0 会被前端画成「误差 0，完美」
        self.assertIsNone(st["metrics"]["mae"])
        self.assertEqual(st["metrics"]["n"], 0)

    def test_missing_dataset_does_not_raise(self):
        st = benchmark.state(dataset_path="samples/eval/根本不存在.json")
        self.assertFalse(st["ready"])
        self.assertTrue(any("读不出来" in r for r in st["reasons"]))

    def test_state_never_exposes_heldout(self):
        # state() 内部固定走 development 切分；heldout 目录就算存在也不该被算进去
        st = benchmark.state()
        self.assertEqual(st["counts"]["heldout_size"], 0)

    def test_server_returns_compact_payload(self):
        server._METRICS_CACHE["data"] = None
        data = server.api_metrics()
        self.assertTrue(data["ok"])
        self.assertIs(data["ready"], False)
        for key in ("counts", "reasons", "thresholds", "metrics", "checked_at"):
            self.assertIn(key, data)
        self.assertEqual(
            set(data["metrics"]),
            {"n", "mae", "mae_ci95", "pearson", "spearman",
             "kappa_verdict", "qwk", "within5", "within10"},
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
