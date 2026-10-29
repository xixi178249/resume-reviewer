"""评测方式按键（llm / local）与「先验证 key 再评审」接口。

这两件事的共同点是**不许猜**：
- 使用者点了「AI 深度评审」却没填 key，就必须报错，不能悄悄换成规则引擎，
  然后再拿一份规则报告跟模型的报告放在一起比分数；
- 使用者填了 key，就必须真的用他的 key，而不是回落到服务器自己的 key。

跑：
    python scripts/test_mode_and_keycheck.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import run_eval  # noqa: E402
import server  # noqa: E402

GOOD_KEY = "sk-" + "a" * 32
PRIVATE_KEY = "sk-" + "b" * 32

RESUME = """求职意向：后端开发工程师
教育经历
某大学 计算机科学与技术 本科 2022.09-2026.06
项目经历
参与校园二手交易平台开发，负责后端接口编写。
负责订单模块设计与实现，日均订单300单。
实习经历
某公司 后端开发实习生 2025.06-2025.09
技能
Python MySQL Redis
"""


def _bomb(*_a, **_kw):
    raise AssertionError("这条路径不该调用模型")


class _ServerCase(unittest.TestCase):
    """把报告目录挪到临时目录，避免测试污染真实历史。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._old_reports = server.REPORTS
        self._old_history = server.HISTORY
        self._old_mock = server.MOCK
        server.REPORTS = Path(self.tmp.name)
        server.HISTORY = server.REPORTS / ".history.json"
        server.MOCK = False

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
        server.MOCK = self._old_mock
        server.SESSIONS.pop(self.sid, None)
        self.tmp.cleanup()

    def review(self, **payload):
        payload.setdefault("session_id", self.sid)
        payload.setdefault("job", "后端开发")
        return server.api_review(payload)

    def history(self):
        return server._history_load()["reviews"]


class ModeChoiceTest(_ServerCase):
    """mode 是页面上点出来的选择，后端必须照办，不许自行改写。"""

    def test_llm_mode_without_key_is_rejected(self):
        with mock.patch.object(server.review_mod, "run_reviews", _bomb):
            r = self.review(mode="llm")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_NO_KEY")
        self.assertFalse(r["retryable"], "没填 key 重试一百次也一样，不该提示重试")
        self.assertEqual(r["engine"], "llm")

    def test_bad_mode_is_rejected(self):
        r = self.review(mode="auto")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "BAD_MODE")

    def test_local_mode_never_calls_model_even_with_key(self):
        # 输入框里可能还留着上次的 key：点了「基础评测」就不许再花他的钱
        with mock.patch.object(run_eval, "chat", _bomb):
            r = self.review(mode="local", api_key=GOOD_KEY)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["engine"], "local-rules")

    def test_local_mode_forces_single_run(self):
        r = self.review(mode="local", repeat=3)
        self.assertTrue(r["ok"], r.get("error"))
        entries = self.history()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["repeat"], 1, "规则引擎是确定性的，跑三次只是白等")
        self.assertEqual(entries[0]["engine"], "local-rules")

    def test_mode_omitted_with_key_takes_llm_path(self):
        # 旧客户端/直接调 API 的兼容路径：有 key 就走模型
        err = run_eval.LLMError("LLM_HTTP_401", "key 无效", retryable=False)
        with mock.patch.object(server.review_mod, "run_reviews", side_effect=err):
            r = self.review(api_key=GOOD_KEY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_HTTP_401")
        self.assertEqual(r["engine"], "llm", "走了模型分支才可能拿到模型的错误码")

    def test_llm_mode_with_bad_key_format_reports_llm_code(self):
        r = self.review(mode="llm", api_key="abc")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_BAD_KEY")

    def test_mode_omitted_without_key_falls_back_to_rules(self):
        r = self.review()
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["engine"], "local-rules")

    def test_llm_mode_is_not_weakened_by_mock_flag(self):
        # MOCK 只影响“怎么拿到模型结果”，不影响“没 key 就不能选 AI 评审”这条规则
        server.MOCK = True
        r = self.review(mode="llm")
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_NO_KEY")

    def test_local_report_marks_engine_so_scores_are_not_compared_across_engines(self):
        r1 = self.review(mode="local")
        self.assertTrue(r1["ok"], r1.get("error"))
        entries = self.history()
        self.assertEqual(entries[0]["engine"], "local-rules")
        # 规则引擎与模型的分数不是一把尺子：config_hash 必须把 engine 算进去
        self.assertTrue(entries[0].get("config_hash"))
        self.assertNotEqual(
            server._review_config_hash("后端开发", "", False, 1, engine="local-rules"),
            server._review_config_hash("后端开发", "", False, 1, engine="llm"),
            "两种引擎的 config_hash 必须不同，否则历史会拿规则分和模型分直接做差",
        )


class KeyCheckTest(_ServerCase):
    """/api/key/check：一秒内告诉使用者 key 能不能用，且永不回显 key。"""

    def check(self, **payload):
        return server.api_key_check(payload)

    def test_missing_key(self):
        self.assertEqual(self.check(api_key="")["code"], "LLM_NO_KEY")
        self.assertEqual(self.check(api_key="   ")["code"], "LLM_NO_KEY")

    def test_malformed_key(self):
        self.assertEqual(self.check(api_key="abc")["code"], "LLM_BAD_KEY")

    def test_private_base_url_rejected(self):
        # 别让“填个地址”变成访问内网的跳板
        r = self.check(api_key=GOOD_KEY, base_url="http://127.0.0.1:8080/v1")
        self.assertEqual(r["code"], "LLM_BAD_BASE_URL")

    def test_ok_reports_model_and_latency_and_never_echoes_key(self):
        with mock.patch.object(run_eval, "chat", return_value="可用"):
            r = self.check(api_key=PRIVATE_KEY)
        self.assertTrue(r["ok"], r.get("error"))
        # 不填 model 时用默认模型：返回的是模型名，不是 key
        cfg = run_eval.config_from_request(PRIVATE_KEY)
        self.assertEqual(r["model"], cfg.describe()["model"])
        self.assertIsInstance(r["latency_ms"], int)
        self.assertGreaterEqual(r["latency_ms"], 0)
        self.assertNotIn(PRIVATE_KEY, json.dumps(r, ensure_ascii=False),
                         "验证结果里不能出现 key 本身")

    def test_model_error_is_passed_through(self):
        err = run_eval.LLMError("LLM_HTTP_401", "API Key 无效", retryable=False)
        with mock.patch.object(run_eval, "chat", side_effect=err):
            r = self.check(api_key=GOOD_KEY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_HTTP_401")
        self.assertFalse(r["retryable"])

    def test_unexpected_error_is_retryable(self):
        with mock.patch.object(run_eval, "chat", side_effect=RuntimeError("网络断了")):
            r = self.check(api_key=GOOD_KEY)
        self.assertFalse(r["ok"])
        self.assertEqual(r["code"], "LLM_ERROR")
        self.assertTrue(r["retryable"])

    def test_mock_mode_does_not_pretend_to_have_called_model(self):
        server.MOCK = True
        with mock.patch.object(run_eval, "chat", _bomb):
            r = self.check(api_key=GOOD_KEY)
        self.assertTrue(r["ok"])
        self.assertTrue(r.get("mock"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
