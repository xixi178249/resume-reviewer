"""无 Key 模式：整条工作台在「没有模型」时也要能改、能评、能比差。

起因（用户实测）：在公网版点「不用 Key」，改稿工作台直接不给开，
只弹一句「必须填自己的 API Key」。可它明明有规则引擎——
同一套 rubric、同一套阈值、同样能算分，凭什么不给用。

这个套件的立场：
- 没 Key 是**降级**，不是**拒绝**：开台、改、重评、报差全都要能用；
- 但不能装作会写句子：规则模式必须明说「不生成补丁」，别塞机器腔；
- 两条路的分必须都出自同一套 rubric（引擎元数据要记清楚，否则历史会混着比）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import server  # noqa: E402

GOOD_KEY = "sk-" + "a" * 32

RESUME = """求职意向：后端开发工程师
教育经历
某大学 计算机科学与技术 本科 2022.09-2026.06
GPA 3.6/4.0，专业前 20%
实习经历
某公司 后端开发实习生 2025.06-2025.09
负责订单模块重构，接口平均响应从 800ms 降到 210ms
项目经历
校园二手交易平台：负责后端接口，日均订单 300 单
技能
Python MySQL Redis Docker
"""

# 改后：补了具体数字与范围（规则引擎认得出来）
EDITED = RESUME.replace(
    "负责订单模块重构，接口平均响应从 800ms 降到 210ms",
    "负责订单模块重构：把 12 个接口合并成 4 个，平均响应从 800ms 降到 210ms，"
    "上线后订单接口错误率从 1.8% 降到 0.2%，支撑日均 3000 单",
)


def _bomb(*_a, **_kw):
    raise AssertionError("无 Key 模式不该调用模型")


class _ChatCase(unittest.TestCase):
    """版本库与报告目录都挪到临时目录，别污染真实历史。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_reports = server.REPORTS
        self._old_history = server.HISTORY
        self._old_env = os.environ.get("CHAT_STORE_DIR")
        server.REPORTS = Path(self.tmp.name)
        server.HISTORY = server.REPORTS / ".history.json"
        os.environ.pop("CHAT_STORE_DIR", None)
        server.CHATS.clear()
        self.sid = "sess-" + uuid.uuid4().hex
        server.SESSIONS[self.sid] = {
            "name": "测试简历.txt",
            "text": RESUME,
            "images": [],
            "images_allowed": False,
            "notes": [],
            "content_sha256": "",
        }

    def tearDown(self):
        server.REPORTS = self._old_reports
        server.HISTORY = self._old_history
        if self._old_env is None:
            os.environ.pop("CHAT_STORE_DIR", None)
        else:
            os.environ["CHAT_STORE_DIR"] = self._old_env
        server.CHATS.clear()
        server.SESSIONS.pop(self.sid, None)
        self.tmp.cleanup()

    def start(self, **payload):
        payload.setdefault("session_id", self.sid)
        payload.setdefault("job", "后端开发")
        return server.api_chat_start(payload)


class StartWithoutKeyTest(_ChatCase):
    def test_start_opens_rules_mode(self):
        r = self.start()
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["mode"], "rules", "没填 Key 就该是规则模式，而不是开不了台")
        self.assertEqual(r["version"], 1)

    def test_v1_is_scored_immediately_so_delta_has_a_base(self):
        # 规则打分是毫秒级的：开台就该有基准分，否则第一次改完只能报「比不了」
        r = self.start()
        row = r["versions"][0]
        self.assertIsInstance(row["score"], int, "规则模式开台必须当场打出 v1 分数")

    def test_start_never_touches_the_model(self):
        with mock.patch.object(server.review_mod, "run_reviews", _bomb), \
             mock.patch.object(server.run_eval, "chat", _bomb):
            r = self.start()
        self.assertTrue(r["ok"], r.get("error"))

    def test_bad_key_is_still_an_error_even_in_rules_mode(self):
        # 填了但格式不对 ≠ 没填：不能悄悄降级成规则模式让他以为在用模型
        r = self.start(api_key="abc")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_BAD_KEY")

    def test_key_mode_still_works(self):
        with mock.patch.object(server, "_score_in_background", lambda *a, **k: None):
            r = self.start(api_key=GOOD_KEY)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["mode"], "model", "填了合法 key 就该走模型模式")


class SendWithoutKeyTest(_ChatCase):
    def test_send_returns_rules_answer_without_patches(self):
        start = self.start()
        r = server.api_chat_send({"chat_id": start["chat_id"], "message": "帮我把实习那条改得更有结果"})
        self.assertTrue(r["ok"], r.get("error"))
        self.assertTrue(r.get("manual"), "规则模式要标明「这轮没动笔」")
        self.assertEqual(r["patches"], [], "规则拼句子只会塞机器腔，不许假装生成改写")
        self.assertIn("无 Key 模式", r["reply"])
        self.assertIsInstance(r.get("score"), int)

    def test_send_says_how_to_edit_instead_of_pretending(self):
        start = self.start()
        r = server.api_chat_send({"chat_id": start["chat_id"], "message": "怎么改"})
        self.assertIn("自己改这一版", r["reply"])

    def test_send_never_calls_the_model(self):
        start = self.start()
        with mock.patch.object(server.run_eval, "chat", _bomb):
            r = server.api_chat_send({"chat_id": start["chat_id"], "message": "改一下"})
        self.assertTrue(r["ok"], r.get("error"))


class EditWithoutKeyTest(_ChatCase):
    def test_edit_appends_version_and_reports_delta(self):
        start = self.start()
        r = server.api_chat_edit({"chat_id": start["chat_id"], "text": EDITED})
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["version"], 2)
        self.assertEqual(r["mode"], "rules")
        self.assertIsInstance(r["score"], int)
        self.assertIsInstance(r["before"], int, "上一版有分，就必须报基准分")
        self.assertEqual(r["delta"], r["score"] - r["before"])
        self.assertEqual(len(r["versions"]), 2)

    def test_edit_records_engine_so_history_never_mixes_rulers(self):
        start = self.start()
        server.api_chat_edit({"chat_id": start["chat_id"], "text": EDITED})
        item = server.CHATS[start["chat_id"]]
        meta = item["store"].get(item["chat"].doc_id, 2)
        self.assertEqual(meta.get("engine"), "local-rules")

    def test_edit_rejects_empty_text(self):
        start = self.start()
        r = server.api_chat_edit({"chat_id": start["chat_id"], "text": "   "})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "CHAT_EMPTY_TEXT")

    def test_edit_rejects_text_below_floor(self):
        start = self.start()
        r = server.api_chat_edit({"chat_id": start["chat_id"], "text": "太短了"})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "CHAT_NO_TEXT")

    def test_edit_rejects_unchanged_text(self):
        start = self.start()
        r = server.api_chat_edit({"chat_id": start["chat_id"], "text": RESUME})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "CHAT_UNCHANGED", "没改动就不该多存一个版本")

    def test_edit_rejects_crlf_only_change(self):
        """Windows 的 textarea 交上来是 CRLF：只差换行不能算改过。

        线上真事：用户在浏览器里点「保存并重评」，正文一个字没改，
        却因为 CRLF 与行尾空格被判成「改过」，于是凭空多出一个一模一样的版本。
        """
        start = self.start()
        r = server.api_chat_edit({"chat_id": start["chat_id"], "text": RESUME.replace("\n", "\r\n")})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "CHAT_UNCHANGED")
        r2 = server.api_chat_edit({"chat_id": start["chat_id"],
                                   "text": "\r\n".join(line + "  " for line in RESUME.split("\n"))})
        self.assertEqual(r2["code"], "CHAT_UNCHANGED", "行尾空格也不该算改过")
        versions = server.api_chat_versions({"chat_id": start["chat_id"]})["versions"]
        self.assertEqual(len(versions), 1, "被拒的保存不能留下版本")

    def test_edit_expired_session(self):
        r = server.api_chat_edit({"chat_id": "nope", "text": EDITED})
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "CHAT_EXPIRED")

    def test_edit_keeps_a_linear_history(self):
        start = self.start()
        a = server.api_chat_edit({"chat_id": start["chat_id"], "text": EDITED})
        b = server.api_chat_edit({"chat_id": start["chat_id"],
                                  "text": EDITED + "\n技能\nKubernetes"})
        self.assertTrue(a["ok"], a.get("error"))
        self.assertTrue(b["ok"], b.get("error"))
        self.assertEqual([v["version"] for v in b["versions"]], [1, 2, 3])


class BothPathsSameRubricTest(_ChatCase):
    def test_rules_score_matches_the_review_page(self):
        # 同一份正文，评审页（local 模式）与工作台的规则模式必须同分，
        # 否则用户会觉得「改稿工作台的分数是编的」。
        review = server.api_review({"session_id": self.sid, "job": "后端开发", "mode": "local"})
        self.assertTrue(review["ok"], review.get("error"))
        start = self.start()
        self.assertEqual(start["versions"][0]["score"], review["score"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
