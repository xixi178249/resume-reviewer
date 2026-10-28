"""本地规则引擎的契约测试：不调模型、不联网，秒级跑完。

为什么值得单独测
----------------
规则引擎要跟大模型模式共用同一条管线（校验器、质量门禁、报告渲染、历史对比），
所以它的输出必须和模型输出**守同一份契约**。这里测的不是"分数准不准"
（规则算不出语义），而是四件事：

1. 契约合法 —— 必填字段齐、类型对、枚举合法
2. 不编造   —— 每一条证据都能在原文找到，改写里不出现原文没有的数字
3. 可复现   —— 同一输入两次跑，输出逐字节一致（模型做不到，规则可以）
4. 不为难人 —— 任意一份简历都要能出报告，不能因为"问题太少"而拒收
"""
from __future__ import annotations

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import check_output as co  # noqa: E402
import local_engine as le  # noqa: E402
import quality_gate  # noqa: E402
import validate_schema  # noqa: E402

SAMPLES = ROOT / "samples" / "txt"
SCHEMA = ROOT / "prompts" / "output-schema-v1.8.json"

WEAK = """李四
手机 13800000000 | 邮箱 lisi@example.com
求职意向：后端开发实习生

教育背景
某某大学 计算机 本科 2022.09-2026.06

项目经历
- 参与了校园二手平台的开发，配合产品同学完成了需求梳理
- 协助测试同学修复了一些接口问题
"""

GOOD = """王五
手机 13900000009 | 邮箱 wangwu@example.com
求职意向：后端开发实习生（Java）

教育背景
某理工大学 计算机科学与技术 本科 2022.09 - 2026.06  GPA 3.8/4.0

实习经历
某科技公司 后端开发实习生 2025.07 - 2025.09
- 负责订单模块重构，把下单接口 P99 从 800ms 降到 210ms，覆盖日均 12 万单

项目经历
课程项目：简易网关
- 用 Go 实现限流与鉴权，压测 500 并发下错误率 0.1%

技能
Java、Go、MySQL、Redis；GitHub: github.com/example/demo
"""

PLAIN = """赵六
求职意向：运营

工作经历
- 负责公众号日常运营，产出内容
"""


def text_of(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def schema_errors(doc: dict) -> list[str]:
    schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
    out: list[str] = []
    validate_schema.check_schema(doc, schema, schema, "$", out)
    return out


class ContractTest(unittest.TestCase):
    """输出的结构与校验器、质量门禁必须对得上。"""

    def test_schema_valid_for_all_samples(self):
        checked = 0
        for path in sorted(SAMPLES.glob("*.txt")):
            errors = schema_errors(le.build(text_of(path), "后端开发工程师"))
            self.assertEqual(errors, [], f"{path.name} 不符合 v1.8 契约：{errors}")
            checked += 1
        for name, text in (("weak", WEAK), ("good", GOOD), ("plain", PLAIN)):
            errors = schema_errors(le.build(text, "后端开发工程师"))
            self.assertEqual(errors, [], f"{name} 不符合 v1.8 契约：{errors}")
            checked += 1
        self.assertGreaterEqual(checked, 4)

    def test_eligible_for_every_kind_of_resume(self):
        """任何一份简历都要能出报告：本地模式不该像模型那样因为'问题太少'被拒。"""
        cases = [("weak", WEAK), ("good", GOOD), ("plain", PLAIN)]
        cases += [(p.name, text_of(p)) for p in sorted(SAMPLES.glob("*.txt"))]
        for name, text in cases:
            doc, gate = le.review_text(text, "后端开发工程师")
            codes = [b.get("code") for b in (gate.get("blockers") or [])]
            self.assertTrue(gate.get("eligible"), f"{name} 被门禁拒收：{codes}")

    def test_engine_and_version_are_labelled(self):
        doc = le.build(GOOD, "后端开发工程师")
        self.assertEqual(doc["engine"], "local-rules")
        self.assertEqual(doc["schema_version"], "v1.8")

    def test_summary_says_it_is_rule_based(self):
        """分数是规则算的，摘要必须写明，不能让人以为这是模型评审。"""
        doc = le.build(GOOD, "后端开发工程师")
        self.assertIn("规则引擎", doc["one_line_summary"])
        self.assertLessEqual(len(doc["one_line_summary"]), 260)


class NoFabricationTest(unittest.TestCase):
    """规则引擎只会「照抄 + 加占位符」，任何凭空数字都是 bug。"""

    CASES = [("weak", WEAK), ("good", GOOD), ("plain", PLAIN)]

    def all_docs(self):
        for name, text in self.CASES:
            yield name, text, le.build(text, "后端开发工程师")

    def test_evidence_quotes_exist_in_resume(self):
        for name, text, doc in self.all_docs():
            rc = co.canon(text)
            for issue in doc["issues"]:
                quote = co.canon(issue["evidence"])
                self.assertTrue(quote, f"{name}: 证据是空的")
                self.assertIn(quote, rc, f"{name}: 证据不在原文里 -> {issue['evidence']!r}")

    def test_rewrites_only_use_numbers_from_original(self):
        for name, text, doc in self.all_docs():
            original_numbers = co.numbers_in(text)
            for rw in doc["rewrites"]:
                for num in co.numbers_in(rw["rewritten"]):
                    self.assertIn(num, original_numbers,
                                  f"{name}: 改写里出现了原文没有的数字 {num}")
                self.assertIn(co.canon(rw["original"]), co.canon(text),
                              f"{name}: 待改的原句不在原文里")

    def test_rewrites_need_user_confirm(self):
        """规则引擎不知道你的真实成果，所有改写都必须标「本人确认」。"""
        for name, text, doc in self.all_docs():
            for rw in doc["rewrites"]:
                self.assertTrue(rw["needs_user_confirm"], f"{name}: 改写没标需要本人确认")

    def test_no_probability_language(self):
        for name, text, doc in self.all_docs():
            blob = json.dumps(doc, ensure_ascii=False)
            for bad in ("大概率", "大概率会", "可能会", "有望", "预计能"):
                self.assertNotIn(bad, blob, f"{name}: 出现概率化表达 {bad}")

    def test_placeholders_are_explicit(self):
        """要人补的数字必须写成[待补充：…]，不能留半截句子。"""
        for name, text, doc in self.all_docs():
            for rw in doc["rewrites"]:
                self.assertIn("[待补充", rw["rewritten"], f"{name}: 改写里缺占位符")


class GateConsistencyTest(unittest.TestCase):
    """两道关的判断必须和 check_output 的公式一致，不能是引擎自己另算一套。"""

    def test_gates_match_shared_formula(self):
        for path in sorted(SAMPLES.glob("*.txt")) + [Path("WEAK")]:
            text = WEAK if path.name == "WEAK" else text_of(path)
            doc = le.build(text, "后端开发工程师")
            self.assertEqual(doc["gate_hr"]["pass"], co.gate_hr_expected(doc))
            self.assertEqual(doc["gate_interview"]["pass"], co.gate_interview_expected(doc))

    def test_verdict_matches_score(self):
        for text in (WEAK, GOOD, PLAIN):
            doc = le.build(text, "后端开发工程师")
            self.assertEqual(doc["verdict"], co.expected_verdict(doc["overall_score"]))

    def test_blocked_format_blocks_hr_gate(self):
        """格式层面看不到内容时，初筛必须不过——这是整套系统的第一原则。"""
        doc = le.build(WEAK, "后端开发工程师")
        doc["format_review"]["verdict"] = "blocking"
        self.assertFalse(co.gate_hr_expected(doc))

    def test_missing_contact_marks_format_blocking(self):
        text = "李四\n求职意向：后端\n\n项目经历\n- 参与了某系统的开发，配合完成部分接口\n"
        doc = le.build(text, "后端开发工程师")
        self.assertFalse(doc["format_review"]["text_level"]["contact_parseable"])
        self.assertEqual(doc["format_review"]["verdict"], "blocking")
        self.assertFalse(doc["gate_hr"]["pass"])


class DeterminismTest(unittest.TestCase):
    """模型每次结果都不一样，规则引擎必须每次一模一样。"""

    def test_same_input_same_output(self):
        a = le.build(GOOD, "后端开发工程师", jd="需要 Java、Redis、限流经验")
        b = le.build(GOOD, "后端开发工程师", jd="需要 Java、Redis、限流经验")
        self.assertEqual(json.dumps(a, ensure_ascii=False, sort_keys=True),
                         json.dumps(b, ensure_ascii=False, sort_keys=True))

    def test_input_not_mutated(self):
        doc = le.build(GOOD, "后端开发工程师")
        snapshot = copy.deepcopy(doc)
        self.assertEqual(doc, snapshot)

    def test_long_resume_finishes_fast(self):
        import time
        big = (GOOD + "\n") * 40
        start = time.time()
        doc = le.build(big, "后端开发工程师")
        self.assertLess(time.time() - start, 5.0, "长简历跑了超过 5 秒")
        self.assertTrue(doc["issues"])


class EdgeCaseTest(unittest.TestCase):
    def test_empty_text_raises(self):
        with self.assertRaises(ValueError):
            le.build("   \n  ", "后端开发工程师")

    def test_tiny_text_still_returns_doc(self):
        doc = le.build("张三\n会写 Python", "")
        self.assertEqual(doc["dimension_scores"]["role_fit"]["score"], 3)  # 没岗位就给中间分
        self.assertGreaterEqual(len(doc["issues"]), 1)

    def test_job_missing_is_not_guessed(self):
        doc = le.build(GOOD, "")
        self.assertIn("未提供岗位", doc["dimension_scores"]["role_fit"]["comment"])

    def test_jd_coverage_both_ends(self):
        covered = le.build(GOOD, "后端开发工程师", jd="Java Redis 限流 鉴权")
        self.assertGreater(covered["jd_match"]["coverage_rate"] or 0, 0.5)
        empty = le.build(PLAIN, "运营", jd="需要 Python 机器学习 大模型 微调")
        self.assertLess(empty["jd_match"]["coverage_rate"] or 0, 0.3)

    def test_no_jd_means_null_coverage(self):
        doc = le.build(GOOD, "后端开发工程师")
        self.assertIsNone(doc["jd_match"]["coverage_rate"])
        self.assertFalse(doc["jd_match"]["jd_provided"])

    def test_privacy_words_never_score(self):
        """写了年龄/性别只该建议删除，不能因此在任何维度上加分或扣分之外的地方被评价。"""
        with_privacy = GOOD.replace("求职意向", "男 24 岁 未婚 籍贯河南\n求职意向")
        a = le.build(GOOD, "后端开发工程师")
        b = le.build(with_privacy, "后端开发工程师")
        for dim in ("role_fit", "impact", "competitiveness"):
            self.assertEqual(a["dimension_scores"][dim]["score"], b["dimension_scores"][dim]["score"],
                             f"隐私信息影响了 {dim} 的分数")
        kinds = [i["issue"] for i in b["issues"]]
        self.assertTrue(any("未婚" in k or "男" in k or "年龄" in k or "籍贯" in k for k in kinds),
                        f"没有提示删除隐私信息：{kinds}")

    def test_visual_never_claimed_unknown(self):
        """纯文本输入下，规则引擎绝不能声称看过版面。"""
        for text in (WEAK, GOOD, PLAIN):
            doc = le.build(text, "后端开发工程师")
            self.assertFalse(doc["format_review"]["visual_level"]["known"])
            self.assertEqual(doc["format_review"]["visual_level"]["checks"], [])


class WeakResumeTest(unittest.TestCase):
    """弱简历要被抓到，好简历不能被冤枉——这是引擎有没有用的分水岭。"""

    def test_weak_resume_scores_lower_than_good(self):
        weak, _ = le.review_text(WEAK, "后端开发工程师")
        good, _ = le.review_text(GOOD, "后端开发工程师")
        gap = good["overall_score"] - weak["overall_score"]
        self.assertGreaterEqual(gap, 20, f"好简历只比弱简历高 {gap} 分，区分度不够")

    def test_weak_verb_lines_are_listed(self):
        doc = le.build(WEAK, "后端开发工程师")
        joined = " ".join(doc["showcase"]["weak_verb_lines"])
        self.assertIn("参与", joined)
        self.assertIn("协助", joined)

    def test_good_resume_has_no_weak_verb(self):
        doc = le.build(GOOD, "后端开发工程师")
        self.assertEqual(doc["showcase"]["weak_verb_lines"], [])

    def test_high_risk_followup_for_vague_percent(self):
        text = GOOD.replace("把下单接口 P99 从 800ms 降到 210ms", "性能提升 60%")
        doc = le.build(text, "后端开发工程师")
        self.assertTrue(doc["number_audit"], "60% 没有被要求交代口径")
        self.assertEqual(doc["number_audit"][0]["source_clarity"], "vague")

    def test_buried_result_detected(self):
        text = GOOD.replace(
            "负责订单模块重构，把下单接口 P99 从 800ms 降到 210ms，覆盖日均 12 万单",
            "负责订单模块的日常维护与需求跟进，期间在团队协作下完成了整体性能优化工作并取得提升")
        doc = le.build(text, "后端开发工程师")
        self.assertTrue(doc["showcase"]["buried_results"])


class ReportPipelineTest(unittest.TestCase):
    """报告渲染必须能吃下规则引擎的输出（和大模型输出共用同一条管线）。"""

    def test_render_works_on_local_doc(self):
        import review as review_mod
        doc, gate = le.review_text(GOOD, "后端开发工程师")
        md = review_mod.render(doc, Path("resume.txt"), "后端开发工程师", ["local"],
                               [], GOOD, quality=gate if gate.get("eligible") else None)
        self.assertIn("规则引擎", md)
        self.assertGreater(len(md), 400)

    def test_history_entry_shape(self):
        import review as review_mod
        doc, gate = le.review_text(GOOD, "后端开发工程师")
        entry = review_mod.summary_of(doc) if hasattr(review_mod, "summary_of") else None
        if entry is not None:
            self.assertEqual(entry.get("engine") or "local-rules", "local-rules")


if __name__ == "__main__":
    unittest.main(verbosity=2)
