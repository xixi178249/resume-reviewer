"""低信息简历（v2.4 D4）：字太少分成「垃圾 / 太短 / 建议模式 / 正常」四档，且不装能评分。

为什么单独一套：
    「太短」最容易被写成两种坏实现——
      1) 一律拒收：只写了学校和一段社团的学生被一句「至少 80 字」打回去，
         他不知道缺的是「结果」而不是「字数」；
      2) 假装能评：直接给 55 分，用户拿着这个分数去投递。
    这套测试钉住的是**第三种**：不评分，但说清缺哪几维的证据、先回答哪几个
    问题、按什么骨架补。所以「不许出现 score/verdict」「必须出现不评分及其理由」
    是硬断言，不是措辞偏好。

用法：
    python scripts/test_thin_resume.py
"""

from __future__ import annotations

import re
import sys
import tempfile
import unittest
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import rubric  # noqa: E402
import server  # noqa: E402
import thin_resume  # noqa: E402

# 一段真实会收到的「草稿」：学校 + 专业 + 一段社团，没有一个数字
DRAFT = (
    "姓名：李雷　电话：13800000000　邮箱：lilei@example.com\n"
    "教育背景：某某大学　市场营销　本科　2026 届\n"
    "校园经历：学生会宣传部干事，负责活动海报与公众号推文\n"
    "技能：Word、PPT\n"
)

# 够正常评分的正文（120 字以上，含数字与经历）
FULL = (
    "求职意向：产品经理实习生\n"
    "教育背景：某某大学 信息管理与信息系统 本科 2026 届 GPA 3.6/4.0 专业前 10%\n"
    "实习经历：某科技公司 产品实习生 2025.07-2025.09\n"
    "负责订单模块需求梳理，输出 12 份需求文档；上线后订单创建成功率从 92% 提升到 99%。\n"
    "项目经历：校园二手交易平台，独立负责后端接口 8 个，日均订单 300 单，接口平均响应 120ms。\n"
    "技能：Axure、SQL、Python；证书：英语六级 560 分。\n"
)


# 凑长度用：像简历的杂字（同一个字重复过半会被判「垃圾」，那是另一档的判断）
_FILLER = "教育背景实习经历项目校园社团负责海报推文排版数据整理会议记录文档撰写"


def _pad(n: int) -> str:
    """返回长度恰好为 n 的假正文（字符多样，不会被 junk 规则命中）。"""
    return (_FILLER * (n // len(_FILLER) + 1))[:n]


class ThresholdTest(unittest.TestCase):
    """配置层：三个阈值必须能读、且相对关系成立。"""

    def test_reads_three_thresholds(self):
        cfg = rubric.thin()
        self.assertEqual(set(cfg), {"min_chars", "advisory_max", "max_questions"})
        for key, value in cfg.items():
            self.assertIsInstance(value, int, f"{key} 必须是整数")
            self.assertGreater(value, 0, f"{key} 必须大于 0")

    def test_min_below_advisory(self):
        # 反过来会导致「40 字走正常评分、120 字反而被拒」这种反直觉行为
        cfg = rubric.thin()
        self.assertLess(cfg["min_chars"], cfg["advisory_max"])

    def test_question_budget_is_sane(self):
        # 少于 3 个定位不准，超过 5 个用户不会答
        self.assertGreaterEqual(rubric.thin()["max_questions"], 3)
        self.assertLessEqual(rubric.thin()["max_questions"], 5)


class ClassifyTest(unittest.TestCase):
    """四档判定：边界必须与配置逐字一致。"""

    def test_empty_is_junk(self):
        self.assertEqual(thin_resume.classify(""), "junk")
        self.assertEqual(thin_resume.classify("   \n\t "), "junk")

    def test_placeholder_is_junk(self):
        for bad in ("test", "简历", "暂无", "1111111111", "aaaaaaaaaa", "。。。。。。"):
            self.assertEqual(thin_resume.classify(bad), "junk", bad)

    def test_counts_without_whitespace(self):
        # 用像简历的杂字凑长度，再靠换行撑大——字数不该被空白灌水
        body = _pad(60)
        text = "\n".join([body[i:i + 4] for i in range(0, len(body), 4)])
        self.assertGreater(len(text), len(body))
        self.assertEqual(thin_resume.chars(text), len(body))
        self.assertEqual(thin_resume.classify(text), "advisory")

    def test_boundaries(self):
        cfg = rubric.thin()
        short = _pad(cfg["min_chars"] - 1)
        edge_low = _pad(cfg["min_chars"])
        edge_high = _pad(cfg["advisory_max"])
        over = _pad(cfg["advisory_max"] + 1)
        self.assertEqual(thin_resume.classify(short), "short")
        self.assertEqual(thin_resume.classify(edge_low), "advisory")
        self.assertEqual(thin_resume.classify(edge_high), "advisory")
        self.assertEqual(thin_resume.classify(over), "full")

    def test_real_draft_and_full(self):
        self.assertEqual(thin_resume.classify(DRAFT), "advisory")
        self.assertEqual(thin_resume.classify(FULL), "full")


class AdviseTest(unittest.TestCase):
    """建议模式的结构：不给分、缺口具体、问题有限且可回答。"""

    def setUp(self):
        self.adv = thin_resume.advise(DRAFT, "未提供（按简历里的求职意向推断）")

    def test_never_scores(self):
        for key in ("score", "overall_score", "verdict", "gates"):
            self.assertNotIn(key, self.adv, f"建议模式不许出现 {key}")
        self.assertFalse(self.adv["enough_for_score"])
        self.assertEqual(self.adv["mode"], "advisory")

    def test_missing_dimensions_are_real(self):
        dims = {m["dimension"] for m in self.adv["missing"]}
        # 全篇没有一个数字 → 结果与影响必须有缺口
        self.assertIn("impact", dims)
        # 没写求职意向 → 岗位对口要有缺口
        self.assertIn("role_fit", dims)
        for m in self.adv["missing"]:
            self.assertIn(m["dimension"], thin_resume.DIM_LABEL)
            self.assertEqual(m["label"], thin_resume.DIM_LABEL[m["dimension"]])
            self.assertGreater(len(m["why"]), 8, "只说「缺失」等于没说")

    def test_job_argument_fills_role_fit(self):
        adv = thin_resume.advise(DRAFT, "互联网 · 产品经理实习生")
        dims = {m["dimension"] for m in adv["missing"]}
        self.assertNotIn("role_fit", dims)
        self.assertTrue(adv["found"]["job"])

    def test_questions_bounded_and_answerable(self):
        qs = self.adv["questions"]
        self.assertTrue(qs)
        self.assertLessEqual(len(qs), rubric.thin()["max_questions"])
        self.assertEqual(len(qs), len(set(qs)), "问题不许重复")
        for q in qs:
            # 允许「问句 + 举例」，但必须真的在问（纯陈述句用户不会去补）
            self.assertIn("？", q, f"必须写成问句：{q}")
            self.assertGreaterEqual(len(q), 12, f"问题太短，答不出来：{q}")

    def test_fallback_question_when_nothing_missing(self):
        # 五类信号齐全但正文仍短：给一句兜底的「压成三行」，而不是空列表
        text = (
            "求职意向：产品经理；教育背景：某某大学 本科 2026 届 GPA 3.6；"
            "实习：某公司 负责需求 12 份 上线后 92% 提升到 99%；"
            "技能：Excel Python SQL；奖项：校级竞赛二等奖"
        )
        adv = thin_resume.advise(text)
        self.assertTrue(adv["questions"])


class MarkdownTest(unittest.TestCase):
    """报告层：必须写着「不评分」并给出理由，不许出现任何分数。"""

    def setUp(self):
        self.adv = thin_resume.advise(DRAFT)
        self.md = thin_resume.to_markdown(self.adv)

    def test_says_no_score_and_why(self):
        self.assertIn("暂不评分", self.md)
        self.assertIn("不评分", self.md)
        self.assertIn("至少两维没有证据", self.md)
        self.assertIn("比不给更糟", self.md)

    def test_has_questions_and_skeleton(self):
        self.assertIn("## 先回答这几个问题", self.md)
        self.assertIn("## 按这个骨架补", self.md)
        self.assertIn("基本信息", self.md)
        self.assertIn("结果：一个数字", self.md)

    def test_no_score_leaks(self):
        self.assertNotRegex(self.md, r"\d{2}\s*分\s*·")
        self.assertNotIn("综合评分", self.md)
        self.assertNotIn("overall_score", self.md)
        self.assertNotIn("/ 100", self.md)

    def test_warns_against_fabricating(self):
        self.assertIn("不要为了凑字数编数字", self.md)

    def test_chars_in_title(self):
        self.assertIn(f"当前 {self.adv['chars']} 字", self.md)


class ServerTest(unittest.TestCase):
    """整条路径：短草稿不进引擎、不为注定打不准的分要 key、不写报告目录。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old = (server.REPORTS, server.HISTORY, server.MOCK)
        server.REPORTS = Path(self.tmp.name)
        server.HISTORY = server.REPORTS / ".history.json"
        server.MOCK = False
        self.sid = "sess-" + uuid.uuid4().hex

    def tearDown(self):
        server.REPORTS, server.HISTORY, server.MOCK = self._old
        server.SESSIONS.pop(self.sid, None)
        self.tmp.cleanup()

    def _put(self, text):
        server.SESSIONS[self.sid] = {
            "name": "草稿.txt", "text": text, "images": [],
            "images_allowed": False, "notes": [], "content_sha256": "",
        }

    def review(self, **payload):
        payload.setdefault("session_id", self.sid)
        payload.setdefault("job", "")
        return server.api_review(payload)

    def test_draft_returns_advisory_not_score(self):
        self._put(DRAFT)
        r = self.review(mode="local")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["mode"], "advisory")
        self.assertIsNone(r["score"], "建议模式不许给分")
        self.assertIsNone(r["verdict"], "没有分就没有「可投/待改」")
        self.assertEqual(r["review_id"], "", "没有分数就没有可下载的报告 id")
        self.assertEqual(r["engine"], "none", "不该声称用了某个引擎")
        self.assertIn("不评分", r["report"])
        self.assertEqual(r["advisory"]["mode"], "advisory")

    def test_draft_needs_no_key_even_in_llm_mode(self):
        # 用户点了「AI 深度评审」但正文只有草稿：先别要 key，因为这一次
        # 注定打不准，填了也是白花他的钱
        self._put(DRAFT)
        r = self.review(mode="llm")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["mode"], "advisory")

    def test_nothing_written_to_reports(self):
        self._put(DRAFT)
        self.review(mode="local")
        written = [p.name for p in server.REPORTS.iterdir() if p.is_file()]
        self.assertEqual(written, [], f"建议模式不该落盘：{written}")

    def test_junk_rejected_with_retryable_code(self):
        self._put("test")
        r = self.review(mode="local")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "RESUME_TOO_THIN")
        self.assertTrue(r["retryable"])
        self.assertIn(str(rubric.thin()["min_chars"]), r["error"], "错误信息要说清低于多少字")

    def test_full_resume_still_scores(self):
        self._put(FULL)
        r = self.review(mode="local")
        self.assertTrue(r["ok"], r.get("error"))
        self.assertNotEqual(r.get("mode"), "advisory")
        self.assertIsInstance(r["score"], int)


if __name__ == "__main__":
    unittest.main(verbosity=2)
