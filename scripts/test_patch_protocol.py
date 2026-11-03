"""补丁协议的契约测试：解析、定位、事实守卫、应用。

这些断言存在的理由：补丁协议是整个对话工作台的地基。
地基塌了，界面做得再顺也只是把错误数据写进用户的简历。
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import patch_protocol as pp  # noqa: E402

RESUME = """张三 / 产品经理实习
教育：重庆城市科技学院 计算机科学与技术 2022.09-2026.06
实习：参与用户增长项目，负责数据看板，覆盖 3 个指标
技能：Python、SQL
"""


def make(**kw) -> pp.Patch:
    base = dict(id="p1", quote="参与用户增长项目", new_text="负责用户增长项目")
    base.update(kw)
    return pp.Patch(**base)


class ParseTest(unittest.TestCase):
    def test_minimal_object(self):
        raw = json.dumps({"reply": "改两处", "patches": [
            {"id": "p1", "quote": "参与用户增长项目", "new_text": "主导用户增长项目"},
            {"id": "p2", "quote": "覆盖 3 个指标", "new_text": "覆盖 3 个核心指标"},
        ]}, ensure_ascii=False)
        reply, patches, problems = pp.parse(raw)
        self.assertEqual(reply, "改两处")
        self.assertEqual([p.id for p in patches], ["p1", "p2"])
        self.assertEqual(problems, [])

    def test_fenced_json(self):
        raw = "```json\n" + json.dumps({"reply": "ok", "patches": [
            {"quote": "技能：Python、SQL", "new_text": "技能：Python、SQL、Tableau"}]}, ensure_ascii=False) + "\n```"
        _, patches, _ = pp.parse(raw)
        self.assertEqual(len(patches), 1)

    def test_prose_around_json(self):
        raw = "好的，我看了：\n" + json.dumps({"reply": "改一处", "patches": [
            {"quote": "负责数据看板", "new_text": "负责数据看板，日均 «待补：UV» 人使用"}]}, ensure_ascii=False) + "\n以上。"
        _, patches, _ = pp.parse(raw)
        self.assertEqual(len(patches), 1)

    def test_non_json_raises(self):
        with self.assertRaises(pp.PatchFormatError):
            pp.parse("我觉得这句可以再量化一点。")

    def test_array_raises(self):
        with self.assertRaises(pp.PatchFormatError):
            pp.parse('[{"quote": "x", "new_text": "y"}]')

    def test_empty_raises(self):
        with self.assertRaises(pp.PatchFormatError):
            pp.parse("   ")

    def test_too_many_patches_truncated(self):
        items = [{"quote": f"第{i}行", "new_text": f"第{i}行改"} for i in range(7)]
        _, patches, problems = pp.parse(json.dumps({"reply": "多", "patches": items}, ensure_ascii=False))
        self.assertEqual(len(patches), pp.MAX_PATCHES)
        self.assertTrue(any("只保留前" in p for p in problems))

    def test_duplicate_ids_renamed(self):
        items = [{"id": "p1", "quote": "技能：Python、SQL", "new_text": "技能：Python、SQL、R"},
                 {"id": "p1", "quote": "负责数据看板", "new_text": "负责核心数据看板"}]
        _, patches, _ = pp.parse(json.dumps({"patches": items}, ensure_ascii=False))
        self.assertEqual(len({p.id for p in patches}), 2)

    def test_bad_id_chars_replaced(self):
        _, patches, _ = pp.parse(json.dumps(
            {"patches": [{"id": "补丁 一！", "quote": "负责数据看板", "new_text": "负责核心数据看板"}]},
            ensure_ascii=False))
        self.assertRegex(patches[0].id, r"^[A-Za-z0-9_-]{1,16}$")

    def test_unsupported_op_dropped(self):
        _, patches, _ = pp.parse(json.dumps(
            {"reply": "插入一段", "patches": [{"op": "insert", "quote": "技能：Python、SQL", "new_text": "新行"}]},
            ensure_ascii=False))
        self.assertEqual(patches, [])

    def test_empty_quote_dropped(self):
        _, patches, _ = pp.parse(json.dumps({"reply": "x", "patches": [{"quote": "  ", "new_text": "y"}]}))
        self.assertEqual(patches, [])

    def test_overlong_quote_dropped(self):
        _, patches, _ = pp.parse(json.dumps(
            {"reply": "x", "patches": [{"quote": "字" * (pp.MAX_QUOTE + 1), "new_text": "y"}]}, ensure_ascii=False))
        self.assertEqual(patches, [])

    def test_overlong_new_text_dropped(self):
        _, patches, _ = pp.parse(json.dumps(
            {"reply": "x", "patches": [{"quote": "负责数据看板", "new_text": "改" * (pp.MAX_NEW_TEXT + 1)}]},
            ensure_ascii=False))
        self.assertEqual(patches, [])


class FactDeltaTest(unittest.TestCase):
    def test_unknown_delta_becomes_wording_and_needs_confirm(self):
        _, patches, problems = pp.parse(json.dumps(
            {"patches": [{"quote": "负责数据看板", "new_text": "负责核心数据看板", "fact_delta": "胡说"}]},
            ensure_ascii=False))
        self.assertEqual(patches[0].fact_delta, "wording")
        self.assertTrue(patches[0].needs_user_confirm)
        self.assertTrue(any("不认识" in p for p in problems + patches[0].problems))

    def test_forced_confirm_when_model_says_no(self):
        _, patches, _ = pp.parse(json.dumps(
            {"patches": [{"quote": "参与用户增长项目", "new_text": "负责用户增长项目",
                          "fact_delta": "ownership_upgrade", "needs_user_confirm": False}]},
            ensure_ascii=False))
        self.assertTrue(patches[0].needs_user_confirm)
        self.assertTrue(any("强制" in p for p in patches[0].problems))

    def test_none_delta_keeps_no_confirm(self):
        _, patches, _ = pp.parse(json.dumps(
            {"patches": [{"quote": "负责数据看板", "new_text": "负责核心数据看板"}]}, ensure_ascii=False))
        self.assertFalse(patches[0].needs_user_confirm)


class CategoryTest(unittest.TestCase):
    def test_whitespace_only_is_layout(self):
        self.assertEqual(pp.category_of("负责数据看板", "负责 数据看板"), "layout")

    def test_punctuation_swap_is_layout(self):
        self.assertEqual(pp.category_of("Python、SQL", "Python，SQL"), "layout")

    def test_word_change_is_content(self):
        self.assertEqual(pp.category_of("参与项目", "主导项目"), "content")

    def test_delete_is_content(self):
        self.assertEqual(pp.category_of("负责数据看板", ""), "content")

    def test_parse_sets_category(self):
        _, patches, _ = pp.parse(json.dumps({"patches": [
            {"quote": "技能：Python、SQL", "new_text": "技能：Python；SQL"}], }, ensure_ascii=False))
        self.assertEqual(patches[0].category, "layout")


class LocateTest(unittest.TestCase):
    def test_exact(self):
        loc = pp.locate(RESUME, "负责数据看板")
        self.assertEqual(RESUME[loc[0]:loc[1]], "负责数据看板")

    def test_space_mismatch(self):
        text = "教育：重庆城市科技学院   计算机科学与技术"
        loc = pp.locate(text, "重庆城市科技学院 计算机科学与技术")
        self.assertIsNotNone(loc)
        self.assertEqual(text[loc[0]:loc[1]], "重庆城市科技学院   计算机科学与技术")

    def test_second_occurrence(self):
        text = "负责看板。负责看板。"
        first = pp.locate(text, "负责看板", 1)
        second = pp.locate(text, "负责看板", 2)
        self.assertEqual(text[second[0]:second[1]], "负责看板")
        self.assertGreater(second[0], first[0])

    def test_missing_returns_none(self):
        self.assertIsNone(pp.locate(RESUME, "这句话不存在"))

    def test_empty_inputs(self):
        self.assertIsNone(pp.locate("", "x"))
        self.assertIsNone(pp.locate(RESUME, ""))


class GuardTest(unittest.TestCase):
    def test_fabricated_number_is_hard(self):
        p = make(new_text="负责数据看板，日均 12000 人使用")
        issues = pp.fact_issues(p, RESUME)
        self.assertEqual(issues[0]["kind"], "fabricated_number")
        self.assertEqual(issues[0]["severity"], "hard")

    def test_existing_number_allowed(self):
        p = make(quote="覆盖 3 个指标", new_text="覆盖 3 个核心指标，周均 3 次复盘")
        self.assertEqual(pp.fact_issues(p, RESUME), [])

    def test_placeholder_allowed(self):
        p = make(quote="覆盖 3 个指标", new_text="覆盖 3 个核心指标，日均 «待补：UV 5000» 人使用")
        self.assertEqual([i for i in pp.fact_issues(p, RESUME) if i["severity"] == "hard"], [])

    def test_ownership_upgrade_is_soft(self):
        p = make()
        issues = pp.fact_issues(p, RESUME)
        self.assertTrue(any(i["kind"] == "ownership_upgrade" and i["severity"] == "soft" for i in issues))

    def test_delete_is_soft(self):
        p = make(quote="技能：Python、SQL", new_text="")
        self.assertTrue(any(i["kind"] == "delete_line" for i in pp.fact_issues(p, RESUME)))


class ApplyTest(unittest.TestCase):
    def test_apply_changes_text(self):
        p = make(quote="覆盖 3 个指标", new_text="覆盖 3 个核心指标", needs_user_confirm=False)
        out = pp.apply_patches(RESUME, [p])
        self.assertTrue(out["changed"])
        self.assertIn("覆盖 3 个核心指标", out["text"])
        self.assertEqual(len(out["applied"]), 1)

    def test_fabricated_number_blocks_write(self):
        p = make(quote="覆盖 3 个指标", new_text="覆盖 3 个指标，日均 12000 人使用", needs_user_confirm=True)
        out = pp.apply_patches(RESUME, [p])
        self.assertFalse(out["changed"])
        self.assertEqual(out["text"], RESUME)
        self.assertEqual(out["blocked"][0]["reasons"][0]["kind"], "fabricated_number")

    def test_missing_quote_skipped(self):
        p = make(quote="简历里没有这句", new_text="x")
        out = pp.apply_patches(RESUME, [p])
        self.assertEqual(len(out["skipped"]), 1)
        self.assertFalse(out["changed"])

    def test_soft_issue_without_confirm_blocked(self):
        p = make(needs_user_confirm=False)  # 参与→负责，属权属升级
        out = pp.apply_patches(RESUME, [p])
        self.assertEqual(len(out["blocked"]), 1)
        self.assertFalse(out["changed"])

    def test_soft_issue_with_confirm_applied(self):
        p = make(needs_user_confirm=True)
        out = pp.apply_patches(RESUME, [p])
        self.assertEqual(len(out["applied"]), 1)
        self.assertTrue(out["changed"])

    def test_second_patch_locates_after_first_change(self):
        p1 = make(id="p1", quote="参与用户增长项目", new_text="负责用户增长项目", needs_user_confirm=True)
        p2 = make(id="p2", quote="负责数据看板", new_text="负责核心数据看板")
        out = pp.apply_patches(RESUME, [p1, p2])
        self.assertEqual(len(out["applied"]), 2)
        self.assertIn("负责用户增长项目，负责核心数据看板", out["text"])

    def test_summary_text(self):
        out = pp.apply_patches(RESUME, [make(quote="覆盖 3 个指标", new_text="覆盖 3 个核心指标")])
        self.assertIn("已改 1 条", pp.summarize(out["applied"], out["skipped"], out["blocked"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
