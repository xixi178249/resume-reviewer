"""质量门禁回归测试，不调用真实模型。

运行：
    python scripts/test_quality_gate.py -v
"""
from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import check_output  # noqa: E402
import quality_gate  # noqa: E402
import review  # noqa: E402
import server  # noqa: E402


# 用库里跟踪的样例，不用 samples/txt/ —— 真实简历不入库，
# CI 全新检出时那个文件不存在，会让整条流水线在 import 阶段就红。
RESUME = (ROOT / "samples" / "demo" / "04-Agent算法-强简历.txt").read_text(encoding="utf-8")
BASE = json.loads(
    (ROOT / "prompts" / "example-output-v1.8-good.json").read_text(encoding="utf-8")
)


def base() -> dict:
    return copy.deepcopy(BASE)


def codes(problems: list[dict]) -> set[str]:
    return {p["code"] for p in problems}


class QualityGateTest(unittest.TestCase):
    def test_good_example_is_deliverable(self) -> None:
        q = quality_gate.evaluate(base(), RESUME)
        self.assertTrue(q["eligible"], q["blockers"])
        self.assertEqual(q["doc"]["overall_score"], 78)
        self.assertEqual(q["doc"]["verdict"], "pass")

    def test_score_and_verdict_tampering_are_repaired(self) -> None:
        doc = base()
        doc["overall_score"] = 99
        doc["verdict"] = "reject"
        q = quality_gate.evaluate(doc, RESUME)
        self.assertTrue(q["eligible"], q["blockers"])
        # 超出 ±2 允许范围后由维度分重算：80 -> strong_pass
        self.assertEqual(q["doc"]["overall_score"], 80)
        self.assertEqual(q["doc"]["verdict"], "strong_pass")
        self.assertIn("SCORE_RECALCULATED", codes(q["warnings"]))
        self.assertIn("VERDICT_RECALCULATED", codes(q["warnings"]))

    def test_bad_single_rewrite_is_filtered_not_fatal(self) -> None:
        doc = base()
        doc["rewrites"][0]["rewritten"] += "，效率提升 999%"
        q = quality_gate.evaluate(doc, RESUME)
        self.assertTrue(q["eligible"], q["blockers"])
        self.assertEqual(len(q["doc"]["rewrites"]), len(doc["rewrites"]) - 1)
        self.assertIn("REWRITE_INVENTED_NUMBER", codes(q["filtered"]))
        remaining = json.dumps(q["doc"]["rewrites"], ensure_ascii=False)
        self.assertNotIn("999%", remaining)

    def test_all_rewrites_unsafe_blocks_delivery(self) -> None:
        doc = base()
        for rw in doc["rewrites"]:
            rw["rewritten"] += "，提升 999%"
        q = quality_gate.evaluate(doc, RESUME)
        self.assertFalse(q["eligible"])
        self.assertIn("REWRITES_INSUFFICIENT", codes(q["blockers"]))

    def test_unverifiable_issue_is_removed(self) -> None:
        doc = base()
        original = doc["issues"][0]["evidence"]
        doc["issues"][0]["evidence"] = "这段话完全不在简历原文里，纯属模型编造"
        q = quality_gate.evaluate(doc, RESUME)
        self.assertTrue(q["eligible"], q["blockers"])
        self.assertNotIn(
            "这段话完全不在简历原文里", json.dumps(q["doc"]["issues"], ensure_ascii=False)
        )
        self.assertIn("ISSUE_EVIDENCE_NOT_FOUND", codes(q["filtered"]))
        self.assertIn(original, RESUME)

    def test_unverifiable_hard_blocker_blocks_delivery(self) -> None:
        doc = base()
        doc["hard_blockers"] = [{
            "issue": "核心经历造假",
            "evidence": "这句原文根本不存在，无法回指",
            "fix": "请核对「项目经历」后再投",
            "assertion": "fact",
        }]
        q = quality_gate.evaluate(doc, RESUME)
        self.assertFalse(q["eligible"])
        self.assertIn("MODEL_OUTPUT_INVALID", codes(q["blockers"]))

    def test_missing_required_field_blocks_delivery(self) -> None:
        doc = base()
        doc.pop("rewrites")
        q = quality_gate.evaluate(doc, RESUME)
        self.assertFalse(q["eligible"])
        self.assertIn("SCHEMA_INVALID", codes(q["blockers"]))

    def test_wrong_schema_version_blocks_delivery(self) -> None:
        doc = base()
        doc["schema_version"] = "v1.6"
        q = quality_gate.evaluate(doc, RESUME)
        self.assertFalse(q["eligible"])
        self.assertIn("SCHEMA_INVALID", codes(q["blockers"]))

    def test_false_jd_keyword_is_downgraded_and_recounted(self) -> None:
        doc = base()
        doc["jd_match"]["keywords"][0].update({
            "term": "量子计算芯片流片", "in_resume": True, "natural": True,
        })
        doc["jd_match"]["coverage_rate"] = 1.0
        q = quality_gate.evaluate(doc, RESUME)
        self.assertTrue(q["eligible"], q["blockers"])
        kw = q["doc"]["jd_match"]["keywords"][0]
        self.assertFalse(kw["in_resume"])
        self.assertFalse(kw["natural"])
        kws = q["doc"]["jd_match"]["keywords"]
        expected = round(sum(1 for k in kws if k.get("natural") is True) / len(kws), 2)
        self.assertEqual(q["doc"]["jd_match"]["coverage_rate"], expected)
        self.assertIn("JD_KEYWORD_DOWNGRADED", codes(q["filtered"]))

    def test_copy_block_excludes_confirmation_and_placeholders(self) -> None:
        doc = base()
        doc["rewrites"][0]["needs_user_confirm"] = True
        doc["rewrites"][0]["fact_delta"] = "ownership_upgrade"
        doc["rewrites"][1]["rewritten"] += " [待补充：具体规模]"
        lines = review.paste_lines(doc, RESUME)
        joined = "\n".join(lines)
        self.assertNotIn(doc["rewrites"][0]["rewritten"], joined)
        self.assertNotIn(doc["rewrites"][1]["rewritten"], joined)


class CandidateSelectionTest(unittest.TestCase):
    def _doc_with_score(self, score: int) -> dict:
        doc = base()
        doc["overall_score"] = score
        doc["verdict"] = "strong_pass" if score >= 80 else "pass"
        return doc

    def test_only_eligible_candidates_participate_in_median(self) -> None:
        bad = self._doc_with_score(95)
        bad["hard_blockers"] = [{
            "issue": "核心经历无法回指",
            "evidence": "根本不存在的原文",
            "fix": "请核对「项目经历」",
            "assertion": "fact",
        }]
        good_low = self._doc_with_score(78)
        good_high = self._doc_with_score(80)
        with mock.patch.object(
            review.run_eval, "call_llm", side_effect=[bad, good_low, good_high]
        ):
            doc, scores, quality = review.run_reviews(
                "prompt", [], 3, resume=RESUME
            )
        self.assertEqual(scores, [78, 80])
        self.assertEqual(doc["overall_score"], 80)
        self.assertTrue(quality["eligible"])

    def test_no_eligible_candidate_raises_gate_error(self) -> None:
        bad1 = self._doc_with_score(90)
        bad2 = self._doc_with_score(95)
        for i, doc in enumerate((bad1, bad2)):
            doc["hard_blockers"] = [{
                "issue": "核心经历无法回指",
                "evidence": f"根本不存在的原文 {i}",
                "fix": "请核对「项目经历」",
                "assertion": "fact",
            }]
        with mock.patch.object(
            review.run_eval, "call_llm", side_effect=[bad1, bad2]
        ):
            with self.assertRaises(quality_gate.QualityGateError) as ctx:
                review.run_reviews("prompt", [], 2, resume=RESUME)
        self.assertTrue(ctx.exception.public_details())
        self.assertEqual(ctx.exception.code, "REVIEW_VALIDATION_FAILED")

    def _bad_doc(self, score: int, tag: str) -> dict:
        doc = self._doc_with_score(score)
        doc["hard_blockers"] = [{
            "issue": "核心经历无法回指",
            "evidence": f"根本不存在的原文 {tag}",
            "fix": "请核对「项目经历」",
            "assertion": "fact",
        }]
        return doc

    def test_single_sample_retries_once_before_failing(self) -> None:
        """repeat=1 时一次抖动不该让用户直接看到报错：自动补一次。"""
        bad = self._bad_doc(90, "retry")
        good = self._doc_with_score(72)
        with mock.patch.object(
            review.run_eval, "call_llm", side_effect=[bad, good]
        ) as spy:
            doc, scores, quality = review.run_reviews("prompt", [], 1, resume=RESUME)
        self.assertEqual(spy.call_count, 2)          # 真的补了一次
        # 门禁会把 overall_score 按维度重算（这里维度合计 80），所以只校验
        # 「拿到的确实是第二次那份、且是通过校验的」，不比对具体分数。
        self.assertEqual(scores, [doc["overall_score"]])
        self.assertTrue(quality["eligible"])

    def test_single_sample_retry_gives_up_after_second_failure(self) -> None:
        """只补一次：两次都不过就报错，不能无限重试烧钱。"""
        first = self._bad_doc(90, "a")
        second = self._bad_doc(91, "b")
        with mock.patch.object(
            review.run_eval, "call_llm", side_effect=[first, second, self._doc_with_score(99)]
        ) as spy:
            with self.assertRaises(quality_gate.QualityGateError) as ctx:
                review.run_reviews("prompt", [], 1, resume=RESUME)
        self.assertEqual(spy.call_count, 2)          # 没有跑第三次
        self.assertEqual(ctx.exception.attempts, 2)  # 如实报告尝试次数

    def test_repeat_three_does_not_get_extra_attempts(self) -> None:
        """repeat>1 的候选本来就互为备份，不该再额外加调用。"""
        docs = [self._bad_doc(90, f"x{i}") for i in range(3)]
        with mock.patch.object(
            review.run_eval, "call_llm", side_effect=docs + [self._doc_with_score(80)]
        ) as spy:
            with self.assertRaises(quality_gate.QualityGateError):
                review.run_reviews("prompt", [], 3, resume=RESUME)
        self.assertEqual(spy.call_count, 3)


class ReviewApiGateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name) / "reports"
        self.reports.mkdir()
        self.patches = [
            mock.patch.object(server, "REPORTS", self.reports),
            mock.patch.object(server, "HISTORY", self.reports / ".history.json"),
            mock.patch.object(server, "MOCK", True),
        ]
        for p in self.patches:
            p.start()
        self.sid = "gate-test-session"
        server.SESSIONS[self.sid] = {
            "text": RESUME,
            "images": [],
            "name": "GATE-TEST",
            "kind": "pasted-text",
            "notes": [],
            "ts": 0,
        }

    def tearDown(self) -> None:
        server.SESSIONS.pop(self.sid, None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_api_returns_structured_gate_error_and_writes_nothing(self) -> None:
        bad = base()
        bad["hard_blockers"] = [{
            "issue": "核心经历无法回指",
            "evidence": "根本不存在的原文",
            "fix": "请核对「项目经历」",
            "assertion": "fact",
        }]
        with mock.patch.object(server.run_eval, "mock_review", return_value=bad):
            res = server.api_review({"session_id": self.sid})
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "REVIEW_VALIDATION_FAILED")
        self.assertTrue(res["retryable"])
        self.assertTrue(res["details"])
        self.assertEqual(list(self.reports.glob("GATE-TEST.*")), [])
        self.assertFalse((self.reports / ".history.json").exists())

    def test_api_success_only_after_gate(self) -> None:
        good = base()
        with mock.patch.object(server.run_eval, "mock_review", return_value=good):
            res = server.api_review({"session_id": self.sid})
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["quality"]["eligible"])
        self.assertEqual(len(res["review_id"]), 32)
        self.assertTrue((self.reports / f"GATE-TEST-{res['review_id'][:12]}.md").exists())
        self.assertTrue((self.reports / f"GATE-TEST-{res['review_id'][:12]}.json").exists())
        self.assertEqual(len(server.api_history()["items"]), 1)

    def test_gate_failure_does_not_overwrite_existing_report(self) -> None:
        old_md = self.reports / "GATE-TEST.md"
        old_json = self.reports / "GATE-TEST.json"
        old_md.write_text("old report", encoding="utf-8")
        old_json.write_text('{"old": true}', encoding="utf-8")
        history = self.reports / ".history.json"
        history.write_text(json.dumps({
            "GATE-TEST": [{"ts": "2026-01-01 00:00", "score": 77}]
        }), encoding="utf-8")
        bad = base()
        bad["hard_blockers"] = [{
            "issue": "核心经历无法回指",
            "evidence": "根本不存在的原文",
            "fix": "请核对「项目经历」",
            "assertion": "fact",
        }]
        with mock.patch.object(server.run_eval, "mock_review", return_value=bad):
            res = server.api_review({"session_id": self.sid})
        self.assertFalse(res["ok"])
        self.assertEqual(old_md.read_text(encoding="utf-8"), "old report")
        self.assertEqual(old_json.read_text(encoding="utf-8"), '{"old": true}')
        saved = json.loads(history.read_text(encoding="utf-8"))
        self.assertEqual(len(saved["GATE-TEST"]), 1)
        self.assertEqual(saved["GATE-TEST"][0]["score"], 77)


class BlockingEvidenceTest(unittest.TestCase):
    """「看不见内容」是最贵的一句结论：它会直接判初筛不过，所以必须有证据。

    真实事故：一份 .txt 简历被判 format_review.verdict=blocking，
    但它的 10 项视觉检查是空的（没有截图），gate_hr 因此直接不过。
    """

    def _text_only(self) -> dict:
        doc = base()
        doc["format_review"]["visual_level"] = {
            "known": False,
            "parse_risk": "unknown",
            "findings": [],
        }
        doc["format_review"]["verdict"] = "blocking"
        doc["format_review"]["text_level"]["contact_parseable"] = True
        doc["format_review"]["text_level"]["garbled_char_count"] = 0
        doc["format_review"]["text_level"]["sections_missing"] = ["获奖"]
        # 让其它规则保持自洽：格式阻断 → 初筛不过、结论不许 strong_pass、
        # 一句话结论要点明格式、第一个动作必须就是修格式
        doc["gate_hr"] = {"pass": False, "reason": "排版导致解析失败，先修格式再看内容"}
        doc["verdict"] = "borderline"
        doc["one_line_summary"] = "排版导致解析失败，需先修格式；内容本身中等偏上"
        doc["next_actions"][0]["targets"] = "format_review"
        return doc

    def test_text_only_blocking_without_evidence_is_rejected(self) -> None:
        q = quality_gate.evaluate(self._text_only(), RESUME)
        self.assertFalse(q["eligible"])
        messages = [p["message"] for p in q["blockers"]]
        self.assertTrue(
            any("没有截图" in m and "文本层证据" in m for m in messages),
            messages,
        )

    def test_text_only_blocking_with_contact_evidence_is_allowed(self) -> None:
        doc = self._text_only()
        doc["format_review"]["text_level"]["contact_parseable"] = False
        res = quality_gate.evaluate(doc, RESUME)
        messages = [p["message"] for p in res["blockers"]]
        self.assertFalse(
            any("没有截图" in m for m in messages),
            messages,
        )

    def test_screenshot_blocking_needs_a_fatal_item(self) -> None:
        doc = base()
        checks = [
            {"item": item, "ok": True, "certainty": "certain", "note": "看起来正常"}
            for item in check_output.VISUAL_ITEMS
        ]
        doc["format_review"]["visual_level"] = {
            "known": True,
            "parse_risk": "low",
            "density": "ok",
            "checks": checks,
        }
        doc["format_review"]["verdict"] = "blocking"
        doc["gate_hr"] = {"pass": False, "reason": "排版导致解析失败，先修格式"}
        doc["verdict"] = "borderline"
        doc["one_line_summary"] = "排版有问题，需先修格式"
        doc["next_actions"][0]["targets"] = "format_review"
        q = quality_gate.evaluate(copy.deepcopy(doc), RESUME)
        messages = [p["message"] for p in q["blockers"]]
        self.assertTrue(any("没有任何致命项" in m for m in messages), messages)

        # 补上一个致命项后，同一句 blocking 就站得住了
        ok = copy.deepcopy(doc)
        ok["format_review"]["visual_level"]["checks"][0] = {
            "item": "图片化文字",
            "ok": False,
            "certainty": "certain",
            "note": "整页是图片，文字提取不到",
        }
        messages2 = [p["message"] for p in quality_gate.evaluate(ok, RESUME)["blockers"]]
        self.assertFalse(any("没有任何致命项" in m for m in messages2), messages2)


class RepairNoteTest(unittest.TestCase):
    """重试时把被拒原因回灌，模型才可能改对；不回灌就是原样再赌一次。"""

    def test_no_reasons_keeps_messages_untouched(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        self.assertIs(review.with_repair_note(msgs, []), msgs)

    def test_reasons_are_appended_with_original_text(self) -> None:
        msgs = [{"role": "user", "content": "hi"}]
        out = review.with_repair_note(
            msgs,
            ["数字溯源覆盖不足：漏了 3%", "one_line_summary: 长度 206 > maxLength 200"],
        )
        self.assertEqual(len(out), 2)
        self.assertEqual(out[0], msgs[0])
        note = out[1]["content"]
        self.assertIn("漏了 3%", note)
        self.assertIn("206 > maxLength 200", note)
        self.assertIn("完整的", note)

    def test_reasons_are_capped(self) -> None:
        out = review.with_repair_note([{"role": "user", "content": "hi"}], [f"问题{i}" for i in range(20)])
        note = out[-1]["content"]
        self.assertNotIn("问题9", note)


class NumberAuditMessageTest(unittest.TestCase):
    def test_missing_numbers_are_named_in_the_error(self) -> None:
        # 只有 2 个百分比 → 要求全覆盖；模型只审了 1 个，报错里要写清漏了哪个，
        # 否则重试时模型不知道自己漏了什么。
        resume = "• 把接口延迟降低 30%，页面转化率提升 12%。"
        doc = base()
        doc["number_audit"] = [{
            "number": "30%",
            "where": "项目经历",
            "source_clarity": "clear",
            "measured_or_estimated": "measured",
            "questions": ["30% 是与哪一版对比测的？"],
        }]
        errors = check_output.check(doc, resume)["errors"]
        hit = [e for e in errors if "数字溯源覆盖不足" in e]
        self.assertTrue(hit, errors)
        self.assertIn("漏了", hit[0])
        self.assertIn("12%", hit[0])


class EllipsisQuoteTest(unittest.TestCase):
    """模型把两条不相邻的 bullet 用「…」接成一句引用——内容是真的，不该判成编造。

    直接拒收的代价是用户什么都拿不到，而这条引用本身逐字可查。
    但每一段都必须真的在原文里，任何一段查不到仍然算编造。
    """

    RESUME = (
        "商业演示设计与内容策划 兼职 2024/06~至今\n"
        "• 【需求理解】独立完成200+项目需求沟通，快速理解不同行业客户的业务逻辑与核心诉求；\n"
        "• 【方案输出】将客户需求转化为结构化、可视化PPT方案，累计输出200+汇报材料；\n"
        "• 【迭代优化】基于客户反馈持续优化内容框架，客户复购率95%。\n"
    )

    def test_stitched_quote_with_real_parts_is_accepted(self) -> None:
        canon = check_output.canon(self.RESUME)
        quote = "独立完成200+项目需求沟通，累计输出200+汇报材料…"
        self.assertTrue(check_output.find_quote(quote, canon))

    def test_stitched_quote_with_fabricated_part_is_still_rejected(self) -> None:
        canon = check_output.canon(self.RESUME)
        quote = "独立完成200+项目需求沟通，服务过50家世界500强企业…"
        self.assertFalse(check_output.find_quote(quote, canon))

    def test_single_fabricated_quote_is_rejected(self) -> None:
        canon = check_output.canon(self.RESUME)
        self.assertFalse(check_output.find_quote("主导公司全部产品线重构", canon))


class ExtractPayloadTest(unittest.TestCase):
    """外部脚本/前端可能把字段包成对象，服务端要给一句人话，不能 500。"""

    def test_wrapped_text_value_is_unwrapped(self) -> None:
        # PowerShell 的 ConvertTo-Json 会把字符串序列化成 {"value": "..."}
        res = server.api_extract({"name": "x.txt", "text": {"value": "简历正文" * 40}})
        self.assertTrue(res["ok"], res)
        self.assertGreaterEqual(res["chars"], 80)

    def test_non_string_text_is_rejected_with_plain_words(self) -> None:
        res = server.api_extract({"name": "x.txt", "text": {"weird": "x"}})
        self.assertFalse(res["ok"])
        self.assertIn("格式", res["error"])

    def test_non_string_b64_is_rejected_with_plain_words(self) -> None:
        res = server.api_extract({"name": "x.pdf", "b64": {"a": 1}})
        self.assertFalse(res["ok"])
        self.assertIn("格式", res["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
