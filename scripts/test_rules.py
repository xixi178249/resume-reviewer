"""分数段锚点（D2）与规则透明化（D6）：规则必须「讲得清、算得对、改不漂」。

为什么单独一套：
    分数段的错是**最隐蔽的那类错**——分数看着合理，结论标签却是隔壁那一段。
    比如把 pass_min 从 65 改成 70，忘了改 bands，那么 65~69 分的简历会被
    `verdict` 标成 pass、被 `band_for` 标成 borderline，同一份报告自相矛盾。
    所以「段不许跨 verdict 阈值」必须是断言，不能靠人记得。

    另一半是 D6：`docs/评分规则.md` 是从 rubric 生成的，生成结果与提交结果
    逐字节比对——手改了文档、或改了 yaml 忘了重新生成，两种漂移都要红；
    `GET /rules` 页只是这份文档的渲染，渲染器同样要钉住（尤其转义）。

用法：
    python scripts/test_rules.py
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "web"))

import gen_rules_doc  # noqa: E402
import md_render  # noqa: E402
import rubric  # noqa: E402
import rules_page  # noqa: E402

DIMS = ("role_fit", "impact", "evidence_quality", "structure", "competitiveness")

NL = "\n"


def _bands() -> list[tuple]:
    return rubric.bands()


class DimensionsTest(unittest.TestCase):
    """五维说明与权重同源（v2.3 起由 rubric.yaml 提供）。"""

    def test_keys_match_weights(self):
        self.assertEqual(set(rubric.dimensions()), set(DIMS),
                         "dimensions 的键集必须与五维权重一致")

    def test_each_dimension_has_one_line_explanation(self):
        for key, text in rubric.dimensions().items():
            self.assertTrue(text.strip(), f"{key} 的说明不能为空")
            self.assertNotIn(NL, text, f"{key} 的说明必须是单行，别写多行字符串")

    def test_explanation_draws_the_boundary(self):
        """说明里必须带「而不是…」——只写正面定义，读的人还是不知道边界在哪。"""
        for key, text in rubric.dimensions().items():
            self.assertIn("而不是", text,
                          f"{key} 的说明要写清边界（而不是什么），现在只有正面定义")


class BandAnchorTest(unittest.TestCase):
    """分数段配置：连续、不跨阈值、建议分合法。"""

    def test_bands_start_at_zero_and_end_at_hundred(self):
        rows = _bands()
        self.assertEqual(rows[0][2], 0, "最低一段必须从 0 起")
        self.assertEqual(rows[-1][3], 100, "最高一段必须到 100")

    def test_bands_are_contiguous(self):
        rows = _bands()
        for i in range(1, len(rows)):
            self.assertEqual(rows[i - 1][3] + 1, rows[i][2],
                             f"{rows[i - 1][0]}({rows[i - 1][3]}) 与 "
                             f"{rows[i][0]}({rows[i][2]}) 之间断档或重叠")

    def test_bands_do_not_straddle_verdict_thresholds(self):
        """每一段必须完整落在同一个 verdict 区间里。

        这是本套测试最重要的一条：段与 verdict 分家，报告就会自相矛盾。
        """
        cuts = rubric._verdict_cut_points()
        for key, label, lo, hi, *_rest in _bands():
            inside = [c for c in cuts[1:] if lo < c <= hi]
            self.assertEqual(inside, [],
                             f"分数段 {key}（{label} {lo}~{hi}）跨过了 verdict 阈值 {inside}；"
                             "要么改段，要么改 verdict，两者必须同步")

    def test_suggest_score_is_inside_its_band(self):
        for key, _label, lo, hi, suggest, *_rest in _bands():
            self.assertTrue(lo <= suggest <= hi,
                            f"{key} 的建议分 {suggest} 不在 {lo}~{hi} 里")

    def test_band_labels_unique(self):
        labels = [r[1] for r in _bands()]
        self.assertEqual(len(labels), len(set(labels)), "分数段标签不能重复")

    def test_band_for_hits_every_boundary(self):
        expect = {
            0: "reject", 49: "reject",
            50: "borderline", 64: "borderline",
            65: "pass", 79: "pass",
            80: "strong", 89: "strong",
            90: "top", 100: "top",
        }
        for score, key in expect.items():
            self.assertEqual(rubric.band_for(score)[0], key,
                             f"{score} 分应该落在 {key}")

    def test_band_for_rounds_and_clamps(self):
        # 报告分数本来就是整数；小数先四舍五入定段（64.6 -> 65 -> pass）
        self.assertEqual(rubric.band_for(64.4)[0], "borderline")
        self.assertEqual(rubric.band_for(64.6)[0], "pass")
        self.assertEqual(rubric.band_for(-5)[0], "reject",
                         "越界不该抛错，夹到边界即可（报告里偶尔有脏数据）")
        self.assertEqual(rubric.band_for(105)[0], "top")
        self.assertEqual(rubric.band_for(None)[0], "reject", "拿不到分数时按最低段，不崩")


class RulesDocTest(unittest.TestCase):
    """docs/评分规则.md 必须与 rubric 一致（D6 的防漂移）。"""

    def test_doc_matches_generator_byte_for_byte(self):
        text = gen_rules_doc.render()
        path = gen_rules_doc.TARGET
        self.assertTrue(path.exists(), "docs/评分规则.md 不存在，先跑 scripts/gen_rules_doc.py")
        self.assertEqual(path.read_text(encoding="utf-8"), text,
                         "docs/评分规则.md 与 config/rubric.yaml 不一致："
                         "改了 yaml 要重新生成，手改文档也要撤回")

    def test_check_mode_exits_zero(self):
        self.assertEqual(gen_rules_doc.main(["--check"]), 0)

    def test_doc_lists_every_industry(self):
        text = gen_rules_doc.render()
        for key, label, _e in rubric.list_industries():
            self.assertIn(f"| `{key}` {label} |", text,
                          f"行业表里少了这一行：{key} / {label}（标签必须与配置逐字一致）")

    def test_doc_lists_every_band(self):
        text = gen_rules_doc.render()
        for key, label, *_rest in _bands():
            short = label.split("·")[-1].strip()
            self.assertIn(f"| `{key}` {short} |", text,
                          f"分数段表里少了这一行：{key} / {short}")

    def test_doc_shows_weights_as_percent_not_fraction(self):
        """0.25 必须印成 25%（v2.1 曾打印成 0.25%）。"""
        text = gen_rules_doc.render()
        self.assertNotIn("0.25%", text)
        self.assertIn("25%", text)

    def test_doc_has_no_timestamp(self):
        """生成物里不许有时间戳，否则每次生成都是 diff。"""
        text = gen_rules_doc.render()
        for year in ("2024-", "2025-", "2026-"):
            self.assertNotIn(year, text, "规则文档里出现了日期，去掉时间戳")

    def test_doc_states_formula_and_industry_neutrality(self):
        text = gen_rules_doc.render()
        self.assertIn("Σ(维度分 × 该行业权重) × 20", text)
        self.assertIn("权重只改总分，不改维度分", text,
                      "必须写明「换行业不改维度分」，否则模型会顺手改维度分")


class ThinSectionTest(unittest.TestCase):
    """低信息简历那一节：数字必须来自配置，不能手写。"""

    def test_thin_numbers_come_from_config(self):
        thin = rubric.thin()
        doc = (ROOT / "docs" / "评分规则.md").read_text(encoding="utf-8")
        self.assertIn("简历太短", doc, "文档里得有这一节")
        self.assertIn(f"少于 {thin['min_chars']} 字", doc)
        self.assertIn(f"{thin['min_chars']}~{thin['advisory_max']} 字", doc)
        self.assertIn(f"多于 {thin['advisory_max']} 字", doc)
        self.assertIn(f"最多问 {thin['max_questions']} 个问题", doc)

    def test_says_no_score_is_not_zero(self):
        doc = (ROOT / "docs" / "评分规则.md").read_text(encoding="utf-8")
        self.assertIn("不评分", doc)
        self.assertIn("不许把「没评」写成 0 分", doc)


class RulesPageTest(unittest.TestCase):
    """D6：/rules 页 = 规则文档的可读版（渲染不炸、内容在、不给人 500）。"""

    def test_renderer_escapes_html(self):
        html = md_render.render("段落里的 <script>alert(1)</script> 必须只是个字" + NL)
        self.assertNotIn("<script>", html, "渲染器必须转义标签，不能把 md 当 HTML 执行")
        self.assertIn("&lt;script&gt;", html)

    def test_renderer_keeps_tables_and_inline_code(self):
        html = md_render.render("| a | b |" + NL + "|---|---|" + NL + "| `x` | **y** |" + NL)
        self.assertIn("<table>", html)
        self.assertIn("<code>x</code>", html)
        self.assertIn("<b>y</b>", html)

    def test_renderer_does_not_mistake_separator_for_data_row(self):
        html = md_render.render("| h |" + NL + "|---|" + NL + "| 1 |" + NL)
        self.assertEqual(html.count("<tr>"), 2, "分隔行不能变成一行数据")

    def test_renderer_keeps_code_fence_body_escaped(self):
        html = md_render.render("```" + NL + "a < b" + NL + "```" + NL)
        self.assertIn("<pre>", html)
        self.assertIn("&lt;", html, "代码块里也要转义，否则 <b> 之类会被当标签")

    def test_rules_page_contains_real_content(self):
        rules_page.reset_cache()
        html = rules_page.render()
        self.assertIn("评分规则", html)
        self.assertGreaterEqual(html.count("<table>"), 6, "文档里的表格都要渲染出来")
        flat = html.replace("<code>", "`").replace("</code>", "`")
        for key, _label, _e in rubric.list_industries():
            self.assertIn(f"`{key}`", flat, f"页面上少了行业 {key}")

    def test_rules_page_links_back(self):
        rules_page.reset_cache()
        html = rules_page.render()
        self.assertIn('href="/"', html)
        self.assertIn('href="/chat"', html)

    def test_rules_page_survives_missing_doc(self):
        rules_page.reset_cache()
        original = rules_page.SOURCE
        try:
            rules_page.SOURCE = original.with_name("不存在的文件.md")
            html = rules_page.render()
        finally:
            rules_page.SOURCE = original
            rules_page.reset_cache()
        self.assertIn("不可用", html, "文档丢了也要给人话，不能给 500")


if __name__ == "__main__":
    unittest.main(verbosity=2)
