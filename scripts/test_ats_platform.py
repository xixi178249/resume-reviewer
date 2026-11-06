"""平台层（ATS，v2.5 D5）：同一份简历在不同平台会被怎么拆。

为什么单独一套：
    这一层最容易写成两种骗人的东西——
      1) **假安心**：正文里根本没有版面信息，却给每条都打「已通过」；
      2) **假警报**：判不出来就说「你的简历会被淘汰」。
    平台解析规则会变，也不是我们能验证的事实。这套测试钉住的是第三种做法：
    能从正文判的才判（ok/missing），判不出来的标 unknown，报告里写明「以投递页面预览为准」。

另外钉住一件事：视觉 10 项清单与致命 5 项从 v2.5 起只有一个真源
（config/rubric.yaml 的 ats 段），prompt 的表格必须与它逐字一致——
「脚本判 10 项、prompt 让模型判 8 项」这种漂移以前出现过。

用法：
    python scripts/test_ats_platform.py
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ats_platform as A  # noqa: E402
import check_output as co  # noqa: E402
import rubric  # noqa: E402

PROMPT = ROOT / "prompts" / "system-v1.8.md"

GOOD = """李雷  13800000000  lilei@example.com

教育经历
2021.09—2025.06  某某大学  软件工程  本科
主修课程：数据结构、操作系统、数据库

实习经历
2024.06—2024.09  某某科技有限公司  后端开发实习生
负责订单模块重构，把下单接口 P99 从 800ms 降到 210ms，覆盖日均 12 万单

项目经历
校园二手交易平台  后端负责人
用 Python + MySQL 做商品搜索，QPS 从 80 提到 300

技能
Python  SQL  Redis  Git
"""


class ConfigLayerTest(unittest.TestCase):
    """配置层：清单从 yaml 来，条目必须可判。"""

    def test_platforms_are_enough_to_be_useful(self):
        rows = rubric.ats_platforms()
        self.assertGreaterEqual(len(rows), 6, "平台少于 6 个，覆盖不到校招主流投递口")
        labels = [label for _key, label, _note, _checks in rows]
        self.assertEqual(len(labels), len(set(labels)), f"平台名重复：{labels}")

    def test_every_platform_has_at_least_three_checks(self):
        for key, _label, _note, checks in rubric.ats_platforms():
            self.assertGreaterEqual(len(checks), 3, f"{key} 的检查项不足 3 条")

    def test_check_ids_start_with_known_probe(self):
        for key, _label, _note, checks in rubric.ats_platforms():
            for cid in checks:
                probe = cid.split("_", 1)[0]
                self.assertIn(probe, rubric.ATS_PROBES, f"{key}.{cid} 的探针不认识")

    def test_visual_items_are_ten_and_blocking_is_subset(self):
        items = rubric.ats_visual_items()
        blocking = rubric.ats_blocking_items()
        self.assertEqual(len(items), 10, "有截图时必须判满 10 项")
        self.assertEqual(len(set(items)), 10, "视觉项有重复")
        self.assertTrue(blocking <= set(items), f"致命项不在视觉项里：{blocking - set(items)}")
        self.assertEqual(len(blocking), 5, "致命项一直是 5 项，改变会改变 verdict 语义")

    def test_check_output_reads_the_same_source(self):
        self.assertEqual(tuple(co.VISUAL_ITEMS), rubric.ats_visual_items())
        self.assertEqual(set(co.BLOCKING_ITEMS), rubric.ats_blocking_items())


class ProbeTest(unittest.TestCase):
    """探针层：从正文判得出的那五类，判对；判不出的，老实说不知道。"""

    def test_dates_range(self):
        self.assertEqual(A._judge("dates_range", GOOD, "")[0], A.OK)
        self.assertEqual(A._judge("dates_range", "2024 年在一家公司实习", "")[0], A.MISSING)

    def test_dates_fmt(self):
        self.assertEqual(A._judge("dates_fmt", "Worked 06/2024 - 09/2024", "")[0], A.OK)
        self.assertEqual(A._judge("dates_fmt", GOOD, "")[0], A.MISSING, "中文日期不是 MM/YYYY")

    def test_sections(self):
        self.assertEqual(A._judge("sections_std", GOOD, "")[0], A.OK)
        self.assertEqual(A._judge("sections_std", "我的经历\n关于我\n随便写点", "")[0], A.MISSING)

    def test_contact(self):
        self.assertEqual(A._judge("contact_top", GOOD, "")[0], A.OK)
        self.assertEqual(A._judge("contact_top", "李雷　已脱敏　已脱敏", "")[0], A.OK,
                         "脱敏是我们自己替换的，不能因此判不合格")
        self.assertEqual(A._judge("contact_top", "李雷 某大学", "")[0], A.MISSING)

    def test_keywords_without_job_is_unknown(self):
        status, why = A._judge("keywords_role", GOOD, "")
        self.assertEqual(status, A.UNKNOWN)
        self.assertIn("没填目标岗位", why)

    def test_keywords_with_job(self):
        self.assertEqual(A._judge("keywords_role", GOOD, "后端开发")[0], A.OK)
        self.assertEqual(A._judge("keywords_role", GOOD, "产品经理")[0], A.MISSING)

    def test_length(self):
        self.assertEqual(A._judge("length_one_page", GOOD, "")[0], A.OK)
        long_text = "经历" * 2000
        self.assertEqual(A._judge("length_one_page", long_text, "")[0], A.MISSING)

    def test_layout_form_filename_are_always_unknown(self):
        for cid in ("layout_single", "form_fill", "filename_en"):
            status, why = A._judge(cid, GOOD, "后端开发")
            self.assertEqual(status, A.UNKNOWN, f"{cid} 从正文判不出来，不许装作判得出")
            self.assertTrue(why, f"{cid} 标了 unknown 就必须说清为什么")


class EvaluateTest(unittest.TestCase):
    """渲染层：状态只有三种，不说「会被淘汰」，必须带免责。"""

    def test_rows_cover_every_platform_and_status_is_three_kinds(self):
        rows = A.evaluate(GOOD, "后端开发")
        self.assertEqual(len(rows), len(rubric.ats_platforms()))
        for row in rows:
            for c in row["checks"]:
                self.assertIn(c["status"], A.STATUS)
                self.assertTrue(c["text"], "检查项文案不能为空")

    def test_no_doom_claims(self):
        """平台层不许出现「会被淘汰」「一定」这类断言——我们验证不了。"""
        text = "\n".join(A.to_markdown(A.evaluate(GOOD, "后端开发"), "后端开发"))
        for banned in ("自动淘汰", "一律淘汰", "必然被刷", "百分之百", "保证通过"):
            self.assertNotIn(banned, text, f"出现了无法验证的断言：{banned}")

    def test_markdown_has_counts_and_disclaimer(self):
        rows = A.evaluate(GOOD, "后端开发")
        text = "\n".join(A.to_markdown(rows, "后端开发"))
        cnt = A.summary(rows)
        self.assertIn(f"✓ {cnt['ok']} 条已满足", text)
        self.assertIn(f"✗ {cnt['missing']} 条没满足", text)
        self.assertIn("不是任何平台的官方文档", text)
        self.assertIn("以你在投递页面看到的预览为准", text)

    def test_markdown_without_job_says_so(self):
        text = "\n".join(A.to_markdown(A.evaluate(GOOD, ""), ""))
        self.assertIn("没填目标岗位", text)
        self.assertIn("不猜", text)

    def test_summary_counts_add_up(self):
        rows = A.evaluate(GOOD, "后端开发")
        cnt = A.summary(rows)
        total = sum(len(row["checks"]) for row in rows)
        self.assertEqual(sum(cnt.values()), total)


class PromptWiringTest(unittest.TestCase):
    """prompt 的 1.2 表格必须与 yaml 的 visual_items 逐字一致。"""

    def test_prompt_visual_table_matches_config(self):
        text = PROMPT.read_text(encoding="utf-8")
        start = text.find("### 1.2 视觉层")
        self.assertGreater(start, 0, "prompt 里找不到 1.2 视觉层小节")
        end = text.find("### 1.3", start)
        self.assertGreater(end, start, "prompt 里找不到 1.3 小节（1.2 没有结尾？）")
        block = text[start:end]
        rows = re.findall(r"^\|\s*`([^`]+)`\s*\|", block, re.M)
        self.assertEqual(
            tuple(rows), rubric.ats_visual_items(),
            "prompt 的视觉项表格与 rubric.yaml 的 ats.visual_items 不一致",
        )

    def test_report_renders_platform_section(self):
        import local_engine as le
        import review as rv

        doc = le.build(GOOD, "后端开发工程师")
        md = rv.render(doc, Path("r.txt"), "后端开发工程师", ["local"], [], resume=GOOD)
        self.assertIn("## 招聘系统（ATS）能不能读出来", md)
        self.assertIn("北森", md)
        self.assertIn("判不出", md)
        self.assertIn("不是任何平台的官方文档", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
