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

import quality_gate  # noqa: E402
import review  # noqa: E402
import server  # noqa: E402


RESUME = (ROOT / "samples" / "txt" / "04-Agent算法-强简历.txt").read_text(encoding="utf-8")
BASE = json.loads(
    (ROOT / "prompts" / "example-output-v1.7-good.json").read_text(encoding="utf-8")
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


if __name__ == "__main__":
    unittest.main(verbosity=2)
