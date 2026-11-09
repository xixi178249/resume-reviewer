#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目测评脚本自己的测试。

一个给别人打分的脚本，如果自己没测试，那它第一句话就该被划掉。
这里只测纯函数与口径一致性，不跑真测评（那要几分钟，属于 selfcheck 的活）。
"""

from __future__ import annotations

import importlib.util
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MOD_PATH = ROOT / "skills" / "project-eval" / "scripts" / "eval_project.py"
REFERENCE = ROOT / "skills" / "project-eval" / "references" / "评分口径.md"


def _load():
    spec = importlib.util.spec_from_file_location("eval_project_under_test", MOD_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EP = _load()


class WeightsTest(unittest.TestCase):
    def test_weights_sum_to_100(self):
        self.assertEqual(sum(EP.WEIGHTS.values()), 100)

    def test_every_weight_has_a_chinese_name(self):
        self.assertEqual(set(EP.WEIGHTS), set(EP.DIM_CN))

    def test_reference_doc_lists_every_weight(self):
        text = REFERENCE.read_text(encoding="utf-8")
        for key, weight in EP.WEIGHTS.items():
            self.assertIn(f"{weight}%", text, f"口径文档没写 {EP.DIM_CN[key]} 的权重")
            self.assertIn(EP.DIM_CN[key], text, f"口径文档没写维度名 {EP.DIM_CN[key]}")

    def test_reference_doc_mentions_unknown_rule(self):
        text = REFERENCE.read_text(encoding="utf-8")
        self.assertIn("判不出", text)
        self.assertIn("归一", text)


class VerifyScoreTest(unittest.TestCase):
    def _t(self, assertions, suites_pass, suites_total):
        return {"status": "ok", "assertions": assertions,
                "suites_pass": suites_pass, "suites_total": suites_total}

    def test_six_hundred_assertions_all_green_is_ten(self):
        self.assertEqual(EP.score_verify(self._t(600, 28, 28))["score"], 10.0)

    def test_four_hundred_is_nine(self):
        self.assertEqual(EP.score_verify(self._t(400, 28, 28))["score"], 9.0)

    def test_red_suite_is_penalised_hard(self):
        green = EP.score_verify(self._t(600, 28, 28))["score"]
        red = EP.score_verify(self._t(600, 27, 28))["score"]
        self.assertLess(red, green * 0.35)

    def test_unknown_stays_unknown_not_zero(self):
        r = EP.score_verify({"status": "unknown", "why": "没有 run_tests.py"})
        self.assertEqual(r["status"], "unknown")
        self.assertNotIn("score", r)


class E2EScoreTest(unittest.TestCase):
    def test_all_pass_is_ten(self):
        r = EP.score_e2e({"status": "ok", "total": 40, "pass": 40, "partial": 0, "fail": 0})
        self.assertEqual(r["score"], 10.0)

    def test_partial_counts_half(self):
        r = EP.score_e2e({"status": "ok", "total": 4, "pass": 3, "partial": 1, "fail": 0})
        self.assertEqual(r["score"], 8.8)

    def test_fail_costs_extra(self):
        r = EP.score_e2e({"status": "ok", "total": 10, "pass": 9, "partial": 0, "fail": 1})
        self.assertEqual(r["score"], 8.5)


class LiveScoreTest(unittest.TestCase):
    def _l(self, version, pages=None):
        return {"status": "ok", "base": "https://x", "health": {"version": version},
                "pages": pages or {"/": {"status": 200}, "/rules": {"status": 200}, "/chat": {"status": 200}}}

    def test_same_version_full_marks(self):
        self.assertEqual(EP.score_live(self._l("2.6.3"), "2.6.3")["score"], 10.0)

    def test_behind_one_release(self):
        self.assertEqual(EP.score_live(self._l("2.6.2"), "2.6.3")["score"], 7.0)

    def test_broken_page(self):
        pages = {"/": {"status": 200}, "/rules": {"status": 500}}
        self.assertEqual(EP.score_live(self._l("2.6.3", pages), "2.6.3")["score"], 4.0)

    def test_no_base_is_unknown(self):
        r = EP.score_live({"status": "unknown", "why": "没给 --base"}, "2.6.3")
        self.assertEqual(r["status"], "unknown")


class DocsScoreTest(unittest.TestCase):
    def _d(self, **kw):
        base = {"status": "ok", "version": "2.6.3", "version_consistent": True,
                "readme_head_has_version": True, "changelog_for_version": True,
                "links_broken_n": 0, "issues": [], "md_files": 10, "links_total": 10,
                "links_broken": []}
        base.update(kw)
        return base

    def test_aligned_is_ten(self):
        self.assertEqual(EP.score_docs(self._d())["score"], 10.0)

    def test_version_drift_costs_three(self):
        self.assertEqual(EP.score_docs(self._d(version_consistent=False))["score"], 7.0)

    def test_broken_links_capped(self):
        d = self._d(links_broken_n=9)
        self.assertEqual(EP.score_docs(d)["score"], 6.0)


class ImportsThreeTierTest(unittest.TestCase):
    SRC = (
        "import json\n"
        "import requests\n"
        "try:\n"
        "    import ujson\n"
        "except ImportError:\n"
        "    ujson = None\n"
        "def f():\n"
        "    import pdfplumber\n"
        "    return pdfplumber\n"
    )

    def test_classifies_three_tiers(self):
        top, lazy, soft = EP._imports_of(self.SRC)
        self.assertIn("requests", top)
        self.assertIn("pdfplumber", lazy)
        self.assertIn("ujson", soft)

    def test_stdlib_is_not_third_party(self):
        top, _lazy, _soft = EP._imports_of(self.SRC)
        self.assertIn("json", top)  # 分类不剔除 stdlib，剔除发生在 measure_code

    def test_broken_file_falls_back(self):
        top, _lazy, _soft = EP._imports_of("def broken(:\n    import sqlite3\n")
        self.assertIn("sqlite3", top)


class CodeScoreTest(unittest.TestCase):
    def _c(self, **kw):
        base = {"status": "ok", "py_files": 10, "py_lines": 1000, "biggest_file": ("a.py", 500),
                "silent_except": [], "silent_except_n": 0, "third_party": [],
                "lazy_deps": [], "optional_deps": []}
        base.update(kw)
        return base

    def test_clean_is_ten(self):
        self.assertEqual(EP.score_code(self._c())["score"], 10.0)

    def test_lazy_dep_does_not_cost(self):
        r = EP.score_code(self._c(lazy_deps=["pdfplumber"]))
        self.assertEqual(r["score"], 10.0)
        self.assertIn("pdfplumber", r["why"])

    def test_silent_except_capped_at_three(self):
        r = EP.score_code(self._c(silent_except_n=40))
        self.assertEqual(r["score"], 7.0)

    def test_top_level_dep_costs_two(self):
        r = EP.score_code(self._c(third_party=["requests"]))
        self.assertEqual(r["score"], 8.0)


class EvidenceScoreTest(unittest.TestCase):
    def _e(self, **kw):
        base = {"status": "ok", "datasets": [], "samples": 0, "heldout": 0,
                "bench": None, "limits_doc": ""}
        base.update(kw)
        return base

    def test_nothing_is_zero(self):
        self.assertEqual(EP.score_evidence(self._e())["score"], 0.0)

    def test_eight_samples_no_holdout_pilot_limits(self):
        r = EP.score_evidence(self._e(samples=8, heldout=0,
                                     bench={"path": "bench/report.json", "status": "PILOT"},
                                     limits_doc="自评-v2.5.md"))
        self.assertEqual(r["score"], 5.0)

    def test_full_marks(self):
        r = EP.score_evidence(self._e(samples=60, heldout=18,
                                     bench={"path": "bench/report.json", "status": "READY"},
                                     limits_doc="自评.md"))
        self.assertEqual(r["score"], 10.0)

    def test_not_ready_bench_earns_nothing(self):
        r = EP.score_evidence(self._e(samples=8, bench={"path": "bench/report.json", "status": "NOT_READY"}))
        self.assertEqual(r["score"], 2.0)


class RenderTest(unittest.TestCase):
    def _result(self):
        dims = {k: {"status": "ok", "score": 8.0, "why": "测试用"} for k in EP.WEIGHTS}
        dims["live"] = {"status": "unknown", "why": "没给 --base"}
        return {
            "root": "/tmp/demo", "version": "9.9.9", "generated_at": "2026-01-01 00:00:00",
            "weights": EP.WEIGHTS, "dims": dims, "total": 8.0, "band": "良好",
            "weight_used": 85, "excluded": ["线上可用"], "defects": [
                {"level": "高", "area": "评测资产", "what": "金标太少"}],
            "raw": {"tests": {"status": "unknown", "why": "跳过"}, "selfcheck": {"status": "unknown", "why": "跳过"},
                    "live": {"status": "unknown", "why": "没给 --base"},
                    "docs": {"md_files": 3, "links_total": 5, "links_broken_n": 0},
                    "code": {"py_files": 2, "py_lines": 100, "biggest_file": ("a.py", 60),
                             "third_party": [], "lazy_deps": [], "optional_deps": []},
                    "deliver": {"changelogs": 1, "screenshots": 1, "packages": [], "has_rules_route": True,
                                "docs_files": 2},
                    "evidence": {"samples": 8, "heldout": 0, "bench": {"path": "bench/report.json", "status": "PILOT"},
                                 "limits_doc": "自评.md", "datasets": []}},
        }

    def test_render_marks_unknown_and_explains_weights(self):
        md = EP.render_md(self._result())
        self.assertIn("判不出", md)
        self.assertIn("权重按剩余 85 归一", md)
        self.assertIn("8.0 / 10（良好）", md)

    def test_render_has_limits_section(self):
        md = EP.render_md(self._result())
        self.assertIn("## 这张卡测不到什么", md)
        self.assertIn("不测「判得准不准」", md)

    def test_render_shows_defects(self):
        md = EP.render_md(self._result())
        self.assertIn("金标太少", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
