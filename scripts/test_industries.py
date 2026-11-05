"""行业权重（v2.3 D1）：11 个行业档案必须「能读、算得对、漂移会被抓」。

为什么单独一套：
    权重是**分数的一半真源**。yaml 里写 30%、prompt 里写 25%、check_output 用
    全局 25%，三处任意一处走偏，用户看到的都是「同一份简历换个下拉框，分数
    变了但理由没变」——这种 bug 靠肉眼极难发现，必须钉死。

    这套测试覆盖四层：
      1. 配置层（rubric.yaml）：键齐、每档合计 100、维度名对齐、未知行业报错
      2. 评分层（check_output.weights_of）：空值回落、行业生效、未知键不崩
      3. 输出层（local_engine）：doc["industry"] 只在用户选了才写
      4. 契约层（schema / prompt）：枚举与表格不许落后于配置

用法：
    python scripts/test_industries.py
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output as co  # noqa: E402
import local_engine  # noqa: E402
import rubric  # noqa: E402

DIMS = ("role_fit", "impact", "evidence_quality", "structure", "competitiveness")
TEXT = (
    "张三\n电话 13800000000\n\n教育经历\n某大学 计算机 本科 2022-2026\n\n"
    "项目经历\n项目A\n• 参与接口开发，负责后端模块编写\n"
)


class ConfigLayerTest(unittest.TestCase):
    def test_default_is_first_and_present(self):
        keys = [k for k, _l, _e in rubric.list_industries()]
        self.assertEqual(keys[0], rubric.INDUSTRY_DEFAULT, "default 必须排第一，它是兜底")
        self.assertIn("default", keys)

    def test_industry_count_at_least_ten(self):
        keys = [k for k, _l, _e in rubric.list_industries()]
        self.assertGreaterEqual(len(keys), 11, f"至少 11 个档案（10 行业 + 通用），实际 {keys}")

    def test_every_industry_sums_to_100(self):
        # 逐个行业算，报错时能直接看出是哪个档案写歪了
        for key, _label, _emph in rubric.list_industries():
            total = sum(v for v in rubric.get(f"industries.{key}.weights").values())
            self.assertAlmostEqual(total, 100.0, places=6,
                                   msg=f"{key} 的权重合计 {total}，必须 100")

    def test_every_industry_covers_exactly_five_dims(self):
        base = set(rubric.get("weights"))
        for key, _l, _e in rubric.list_industries():
            got = set(rubric.get(f"industries.{key}.weights"))
            self.assertEqual(got, base, f"{key} 的维度名与顶层 weights 不一致：{got ^ base}")

    def test_every_industry_has_label_and_emphasis(self):
        for key, label, emph in rubric.list_industries():
            self.assertTrue(label.strip(), f"{key} 缺 label（界面上要显示中文名）")
            self.assertTrue(emph.strip(), f"{key} 缺 emphasis（报告里要给一句「这个行业看什么」）")

    def test_labels_are_unique(self):
        labels = [l for _k, l, _e in rubric.list_industries()]
        self.assertEqual(len(labels), len(set(labels)), f"行业中文名重复：{labels}")

    def test_finance_raises_role_fit(self):
        # 金融把岗位对口抬到 30%：证书/资质的权重靠 role_fit 承载
        self.assertAlmostEqual(rubric.weights_for("finance")["role_fit"], 0.30, places=9)
        self.assertLess(rubric.weights_for("finance")["impact"],
                        0.25, "金融不该比通用更看重量化产出")

    def test_design_raises_structure(self):
        self.assertAlmostEqual(rubric.weights_for("design")["structure"], 0.27, places=9)

    def test_weights_without_arg_equals_default(self):
        self.assertEqual(rubric.weights(), rubric.weights_for(None))
        self.assertEqual(rubric.weights_for(""), rubric.weights_for(None))

    def test_unknown_industry_raises_with_hint(self):
        with self.assertRaises(rubric.RubricError) as ctx:
            rubric.weights_for("crypto")
        msg = str(ctx.exception)
        self.assertIn("crypto", msg, "报错必须带上用户传的那个值，否则排不出来")
        self.assertIn("finance", msg, "报错应列出可选的键")

    def test_profile_returns_normalized_floats(self):
        prof = rubric.industry_profile("soe")
        self.assertEqual(prof["key"], "soe")
        self.assertTrue(prof["label"])
        self.assertAlmostEqual(sum(prof["weights"].values()), 1.0, places=9)
        self.assertIsInstance(prof["weights"]["role_fit"], float)


class ScoringLayerTest(unittest.TestCase):
    def test_weights_of_empty_falls_back(self):
        self.assertEqual(co.weights_of({}), co.WEIGHTS)
        self.assertEqual(co.weights_of({"industry": ""}), co.WEIGHTS)

    def test_weights_of_applies_industry(self):
        self.assertAlmostEqual(co.weights_of({"industry": "design"})["structure"], 0.27, places=9)

    def test_weights_of_unknown_is_safe(self):
        # 老报告 / API 传错值时不能崩，必须回落并留下可查的痕迹
        w = co.weights_of({"industry": "不存在的行业"})
        self.assertEqual(w, co.WEIGHTS)
        self.assertFalse(co.industry_known({"industry": "不存在的行业"}))
        self.assertTrue(co.industry_known({"industry": "finance"}))
        self.assertTrue(co.industry_known({}), "没给行业算「已知」，因为会用兜底")

    def test_same_dims_different_totals(self):
        dims = {"role_fit": 4, "impact": 5, "evidence_quality": 4,
                "structure": 3, "competitiveness": 4}

        def total(ind: str) -> int:
            w = co.weights_of({"industry": ind} if ind else {})
            return round(sum(dims[k] * w[k] for k in DIMS) * 20)

        self.assertEqual(total(""), 82, "通用权重下这组维度应是 82")
        self.assertEqual(total("design"), 78, "设计行业压 impact/role_fit、抬 structure，应更低")
        self.assertNotEqual(total(""), total("finance"))
        # 反过来：结构分高、量化分低的简历在「设计」下应该比在「金融」下更高
        alt = {"role_fit": 3, "impact": 2, "evidence_quality": 3,
               "structure": 5, "competitiveness": 3}

        def total_alt(ind: str) -> int:
            w = co.weights_of({"industry": ind} if ind else {})
            return round(sum(alt[k] * w[k] for k in DIMS) * 20)

        self.assertGreater(total_alt("design"), total_alt("finance"),
                           "同一份简历换行业分数不动的权重表是假权重表")


class OutputLayerTest(unittest.TestCase):
    def test_doc_writes_industry_only_when_given(self):
        doc_default, _ = local_engine.review_text(TEXT, "后端实习生", "")
        self.assertNotIn("industry", doc_default,
                         "没选行业就不要写空字段，否则老报告对比会多出一列空值")
        doc_fin, _ = local_engine.review_text(TEXT, "后端实习生", "", industry="finance")
        self.assertEqual(doc_fin["industry"], "finance")

    def test_engine_score_matches_declared_industry(self):
        doc, _gate = local_engine.review_text(TEXT, "后端实习生", "", industry="design")
        w = co.weights_of(doc)
        calc = round(sum(doc["dimension_scores"][k]["score"] * w[k] for k in DIMS) * 20)
        self.assertLessEqual(abs(calc - doc["overall_score"]), 1,
                             "规则引擎的总分必须和它声明的行业权重对得上")


class ContractLayerTest(unittest.TestCase):
    def test_schema_enum_matches_config(self):
        schemas = sorted((ROOT / "prompts").glob("output-schema-*.json"))
        latest = schemas[-1]
        s = json.loads(latest.read_text(encoding="utf-8"))
        enum = (s["properties"].get("industry") or {}).get("enum")
        self.assertIsNotNone(enum, f"{latest.name} 缺 industry 字段，模型就不知道能选哪些行业")
        self.assertEqual(set(enum), {k for k, _l, _e in rubric.list_industries()},
                         "schema 的行业枚举与配置不一致")
        self.assertNotIn("industry", s.get("required", []),
                         "industry 必须可选，否则老报告全部不合格")


if __name__ == "__main__":
    unittest.main(verbosity=2)
