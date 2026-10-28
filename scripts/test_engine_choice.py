"""谁能决定用哪个模型：自带 key / 服务器 key / 规则引擎。

这一层最怕的不是算错，是「悄悄用了别人（或服务器）的钱」和
「拿使用者填的地址当跳板访问内网」。所以每个拒绝都要有稳定的错误码，
界面上才能回一句人话。

跑：
    python scripts/test_engine_choice.py
"""
from __future__ import annotations

import os
import sys
import socket
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import run_eval  # noqa: E402
import server  # noqa: E402
import local_engine  # noqa: E402

GOOD_KEY = "sk-" + "a" * 32


class KeyValidationTest(unittest.TestCase):
    """使用者填的 key 必须当场判死，不能等花了 40 秒才报错。"""

    def _err(self, key: str, base_url: str = "", model: str = "") -> str:
        with self.assertRaises(run_eval.LLMError) as ctx:
            run_eval.config_from_request(key, base_url=base_url, model=model)
        return ctx.exception.code

    def test_empty_key_is_no_key(self):
        self.assertEqual(self._err(""), "LLM_NO_KEY")
        self.assertEqual(self._err("   "), "LLM_NO_KEY")

    def test_key_with_whitespace_rejected(self):
        self.assertEqual(self._err("sk-abc def ghi"), "LLM_BAD_KEY")

    def test_short_or_long_key_rejected(self):
        self.assertEqual(self._err("short"), "LLM_BAD_KEY")
        self.assertEqual(self._err("sk-" + "x" * 400), "LLM_BAD_KEY")

    def test_quotes_are_stripped_not_rejected(self):
        # 从 .env / JSON 复制粘贴最常见的形态就是带一层引号，不该因此报错
        cfg = run_eval.config_from_request('"sk-abc123456789"')
        self.assertEqual(cfg.api_key, "sk-abc123456789")
        cfg = run_eval.config_from_request("'sk-abc123456789'")
        self.assertEqual(cfg.api_key, "sk-abc123456789")

    def test_quote_inside_key_rejected(self):
        self.assertEqual(self._err('sk-"abc"123456789'), "LLM_BAD_KEY")

    def test_bad_scheme_rejected(self):
        self.assertEqual(self._err(GOOD_KEY, base_url="ftp://x/y"), "LLM_BAD_BASE_URL")
        self.assertEqual(self._err(GOOD_KEY, base_url="api.deepseek.com"), "LLM_BAD_BASE_URL")

    def test_model_name_validated(self):
        self.assertEqual(self._err(GOOD_KEY, model="deepseek chat"), "LLM_BAD_MODEL")
        self.assertEqual(self._err(GOOD_KEY, model="-leading-dash"), "LLM_BAD_MODEL")


class SsrfGuardTest(unittest.TestCase):
    """填个地址让服务器替我去访问内网，这条路必须堵死。"""

    def _blocked(self, url: str) -> bool:
        try:
            run_eval.config_from_request(GOOD_KEY, base_url=url)
            return False
        except run_eval.LLMError as exc:
            return exc.code == "LLM_BAD_BASE_URL"

    def tearDown(self):
        os.environ.pop("DSH_LLM_ALLOW_LOCAL", None)

    def test_loopback_and_private_blocked(self):
        for url in (
            "http://127.0.0.1:11434/v1",
            "http://localhost:8000/v1",
            "http://[::1]:8000/v1",
            "http://10.1.2.3/v1",
            "http://192.168.1.9:8080/v1",
            "http://172.16.0.5/v1",
            "http://169.254.169.254/latest",   # 云元数据地址
            "http://0.0.0.0:8000/v1",
        ):
            self.assertTrue(self._blocked(url), url)

    def test_public_host_allowed(self):
        self.assertFalse(self._blocked("https://api.deepseek.com/v1"))
        self.assertFalse(self._blocked("https://api.siliconflow.cn/v1"))

    def test_domain_resolving_to_private_is_blocked(self):
        # 字面量能拦住，但 evil.example 解析到 127.0.0.1 这种要解析后才看得见。
        # 用 mock 固定解析结果，避免测试依赖真实 DNS。
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 80))]
        with mock.patch.object(run_eval.socket, "getaddrinfo", return_value=fake):
            self.assertTrue(self._blocked("http://evil.example/v1"))

    def test_domain_resolving_to_public_is_allowed(self):
        fake = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
        with mock.patch.object(run_eval.socket, "getaddrinfo", return_value=fake):
            self.assertFalse(self._blocked("https://api.example.com/v1"))

    def test_dns_failure_is_not_treated_as_attack(self):
        # 离线/临时 DNS 故障不该把正常用户挡在门外，让真正的请求去报网络错误
        with mock.patch.object(run_eval.socket, "getaddrinfo", side_effect=OSError("no dns")):
            self.assertFalse(self._blocked("https://api.deepseek.com/v1"))

    def test_local_allowed_when_explicitly_permitted(self):
        # 自己跑 Ollama 的人要有出路，但必须是显式打开
        os.environ["DSH_LLM_ALLOW_LOCAL"] = "1"
        self.assertFalse(self._blocked("http://127.0.0.1:11434/v1"))


class RequestConfigTest(unittest.TestCase):
    """请求带来的 key 只属于这一次请求：不落盘、不继承服务器的 thinking。"""

    def tearDown(self):
        os.environ.pop("DSH_LLM_THINKING", None)

    def test_origin_and_defaults(self):
        cfg = run_eval.config_from_request(GOOD_KEY)
        self.assertEqual(cfg.origin, "request")
        self.assertEqual(cfg.api_key, GOOD_KEY)
        self.assertTrue(cfg.base_url.startswith("http"))
        self.assertFalse(cfg.base_url.endswith("/"))
        self.assertEqual(cfg.thinking, "off")     # 别人等不起思维链
        self.assertGreaterEqual(cfg.timeout, 60)

    def test_trailing_slash_normalized(self):
        cfg = run_eval.config_from_request(GOOD_KEY, base_url="https://api.deepseek.com/v1/")
        self.assertEqual(cfg.base_url, "https://api.deepseek.com/v1")

    def test_describe_never_leaks_key(self):
        cfg = run_eval.config_from_request(GOOD_KEY)
        self.assertNotIn(GOOD_KEY, cfg.describe())
        self.assertNotIn(GOOD_KEY[-8:], cfg.describe())


class ConfigHashTest(unittest.TestCase):
    """分差只在「同一把尺子」下才有意义：换引擎、换模型都不能算变化。"""

    def test_same_config_same_hash(self):
        a = server._review_config_hash("后端", "jd 文本", False, 1, engine="llm",
                                       model="deepseek-chat", base_url="https://x/v1")
        b = server._review_config_hash("后端", "jd 文本", False, 1, engine="llm",
                                       model="deepseek-chat", base_url="https://x/v1")
        self.assertEqual(a, b)

    def test_engine_change_breaks_comparability(self):
        llm = server._review_config_hash("后端", "", False, 1, engine="llm")
        loc = server._review_config_hash("后端", "", False, 1, engine="local-rules")
        self.assertNotEqual(llm, loc)

    def test_model_change_breaks_comparability(self):
        a = server._review_config_hash("后端", "", False, 1, engine="llm", model="deepseek-chat")
        b = server._review_config_hash("后端", "", False, 1, engine="llm", model="deepseek-flash")
        self.assertNotEqual(a, b)

    def test_repeat_change_breaks_comparability(self):
        a = server._review_config_hash("后端", "", False, 1)
        b = server._review_config_hash("后端", "", False, 3)
        self.assertNotEqual(a, b)


class QuotaOverrideTest(unittest.TestCase):
    """自带 key 的人花自己的钱，不该被全站免费额度卡住；但仍要有防刷上限。"""

    def setUp(self):
        with server.RL_LOCK:
            server.RL["ips"].clear()
            server.RL["count"] = 0

    def test_byok_limit_is_generous(self):
        ip = "203.0.113.77"
        for _ in range(30):
            ok, why = server.allow_request(ip, 1, limit=120, daily=0)
            self.assertTrue(ok, why)

    def test_byok_still_capped(self):
        ip = "203.0.113.78"
        for _ in range(3):
            self.assertTrue(server.allow_request(ip, 1, limit=3, daily=0)[0])
        ok, why = server.allow_request(ip, 1, limit=3, daily=0)
        self.assertFalse(ok)
        self.assertIn("每小时最多 3 次", why)

    def test_daily_zero_disables_site_quota(self):
        ip = "203.0.113.79"
        for _ in range(5):
            self.assertTrue(server.allow_request(ip, 1, limit=0, daily=0)[0])


class RubricIdentityTest(unittest.TestCase):
    """换 key / 换模型 / 换接口地址，评价标准一个字都不能变。

    这是产品承诺，不是实现细节：使用者拿自己的 key 来评，评出来的
    65 分必须和别人的 65 分是同一把尺子量出来的。所以这里钉死三件事：
    ① 送进模型的 prompt 与 key/model/url 无关 ② 权重与通过线是全局常量
    ③ 接口上没有任何可以改标准的口子（没有 version / weights 参数）。
    """

    TEXT = (ROOT / "samples" / "demo" / "04-Agent算法-强简历.txt").read_text(encoding="utf-8")
    CASE = {"job_target": "算法实习生", "jd": "", "background": "（未提供）"}

    def _prompt(self) -> str:
        return run_eval.build_prompt(self.TEXT, self.CASE, run_eval.DEFAULT_PROMPT_VERSION)

    def test_prompt_identical_across_keys_and_models(self):
        envs = [
            {"DSH_LLM_API_KEY": "sk-" + "1" * 32, "DSH_LLM_MODEL": "deepseek-chat",
             "DSH_LLM_BASE_URL": "https://api.deepseek.com/v1"},
            {"DSH_LLM_API_KEY": "sk-" + "2" * 32, "DSH_LLM_MODEL": "deepseek-flash",
             "DSH_LLM_BASE_URL": "https://api.deepseek.com/v1"},
            {"DSH_LLM_API_KEY": "sk-" + "3" * 32, "DSH_LLM_MODEL": "gpt-4o-mini",
             "DSH_LLM_BASE_URL": "https://openrouter.example.com/v1"},
        ]
        prompts = []
        for env in envs:
            with mock.patch.dict(os.environ, env, clear=False):
                prompts.append(self._prompt())
        self.assertEqual(len(set(prompts)), 1, "不同 key/模型产出的 prompt 不一致，标准被改了")

    def test_key_never_enters_prompt(self):
        key = "sk-" + "z" * 32
        with mock.patch.dict(os.environ, {"DSH_LLM_API_KEY": key}, clear=False):
            p = self._prompt()
        self.assertNotIn(key, p)
        for slot in ("{{api_key}}", "{{key}}", "{{model}}", "{{base_url}}"):
            self.assertNotIn(slot, p, f"prompt 里不该有 {slot} 这种可被替换的槽位")

    def test_rubric_constants_pinned(self):
        # 钉死值：改动这里必须是一次有意识的「标准升级」，而不是顺手改
        import check_output as co
        self.assertEqual(co.WEIGHTS, {
            "role_fit": 0.25, "impact": 0.25, "evidence_quality": 0.2,
            "structure": 0.15, "competitiveness": 0.15,
        })
        self.assertEqual(
            (co.GATE_ROLE_MIN, co.GATE_IMPACT_MIN, co.GATE_EQ_MIN, co.GATE_HIGH_RISK_MAX),
            (3, 2, 3, 1),
        )
        self.assertLess(abs(sum(co.WEIGHTS.values()) - 1.0), 1e-9, "权重之和必须是 1")

    def test_no_per_request_rubric_knob(self):
        import inspect
        import review
        for fn, name in ((review.run_reviews, "run_reviews"),
                         (server.api_review, "api_review")):
            params = set(inspect.signature(fn).parameters)
            for bad in ("version", "weights", "rubric", "prompt_version", "scale"):
                self.assertNotIn(bad, params, f"{name} 不该有 {bad} 参数，否则调用方能改标准")
        params = set(inspect.signature(local_engine.review_text).parameters)
        self.assertEqual(params, {"text", "job", "jd", "image_known"})

    def test_both_engines_share_weights_and_dimensions(self):
        import check_output as co
        doc, _gate = local_engine.review_text(self.TEXT, "算法实习生", "")
        self.assertEqual(set(doc["dimension_scores"]), set(co.WEIGHTS))
        recomputed = round(sum(doc["dimension_scores"][k]["score"] * w
                               for k, w in co.WEIGHTS.items()) * 20)
        self.assertLessEqual(abs(recomputed - doc["overall_score"]), 1,
                             "规则引擎的加权总分必须能和全局权重对上")
        # 大模型那一侧：prompt 里必须出现全部 5 个维度名，否则两把尺子会走偏
        p = self._prompt()
        for k in co.WEIGHTS:
            self.assertIn(k, p, f"prompt 缺少维度 {k}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
