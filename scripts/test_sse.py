"""M7 SSE 四阶段 + /api/metrics：进度必须是真的，终态必须必达。

要防的错：
- 编进度条（「假进度比没进度更伤信任」）——阶段事件只能来自真实执行点
- `done`/`error` 丢事件，前端永远转圈
- 重连时重放历史 stage → 前端把「已用 12 秒」累加成 24 秒
- 指标把 0 当默认值：`fail_rate=0` 会被读成「从来没失败过」

运行：
    python scripts/test_sse.py -v
"""
from __future__ import annotations

import http.client
import json
import sys
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import server  # noqa: E402

RESUME = (ROOT / "samples" / "demo" / "02-后端-部分量化.txt").read_text(encoding="utf-8")
STAGES = ["desensitize", "format", "check", "render"]


class JobRegistryTest(unittest.TestCase):
    """job 的生命周期：建 → 推事件 → 收终态 → 过期清理。"""

    def setUp(self) -> None:
        server.JOBS.clear()

    def tearDown(self) -> None:
        server.JOBS.clear()

    def test_new_push_finish(self):
        job_id = server.api_review_start.__globals__["_job_new"]()
        server._job_push(job_id, "check", repeat=1)
        job = server._job_get(job_id)
        self.assertEqual(len(job["events"]), 1)
        self.assertEqual(job["events"][0]["label"], "正在逐项检验")
        self.assertIsInstance(job["events"][0]["elapsed_ms"], int)
        server._job_finish(job_id, {"ok": True, "score": 80})
        self.assertEqual(server._job_get(job_id)["final"]["score"], 80)

    def test_expired_jobs_are_collected(self):
        old = server._job_new()
        with server.JOBS_LOCK:
            server.JOBS[old]["created"] = time.time() - server.JOB_TTL - 5
        server._job_new()                      # 新建时会顺手清过期的
        self.assertIsNone(server._job_get(old))

    def test_push_to_unknown_job_is_harmless(self):
        server._job_push("nope", "check")      # 不许抛异常把评审线程带走
        self.assertIsNone(server._job_get("nope"))


class StageOrderTest(unittest.TestCase):
    """阶段只能按 1→2→3→4 发，且每个都带真实耗时。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        reports = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(server, "REPORTS", reports),
            mock.patch.object(server, "HISTORY", reports / ".history.json"),
            mock.patch.object(server, "MOCK", True),
        ]
        for p in self.patches:
            p.start()
        self.sids: list[str] = []

    def tearDown(self) -> None:
        for sid in self.sids:
            server.SESSIONS.pop(sid, None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _extract(self) -> str:
        out = server.api_extract({"name": "测试简历.txt", "text": RESUME})
        self.assertTrue(out.get("ok"), out)
        self.sids.append(out["session_id"])
        return out["session_id"]

    def test_four_stages_in_order(self):
        sid = self._extract()
        seen: list[str] = []
        res = server.api_review(
            {"session_id": sid, "job": "后端开发", "mode": "local"},
            progress=lambda stage, **kw: seen.append(stage),
        )
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(seen, STAGES)

    def test_stage_payload_has_label_and_elapsed(self):
        sid = self._extract()
        events: list[dict] = []
        server.api_review(
            {"session_id": sid, "job": "后端开发", "mode": "local"},
            progress=lambda stage, **kw: events.append({"stage": stage, **kw}),
        )
        by_stage = {e["stage"]: e for e in events}
        self.assertEqual(by_stage["format"]["chars"], len(server.SESSIONS[sid]["text"]))
        self.assertEqual(by_stage["check"]["repeat"], 1)   # 规则引擎强制 1 次


class MetricsTest(unittest.TestCase):
    """指标：算不出来给 None；分母是「本次进程」。"""

    def setUp(self) -> None:
        with server.METRICS_LOCK:
            server.METRICS.update({
                "requests_total": 0, "review_ok": 0, "review_failed": 0,
                "refusal": 0, "latencies": [], "engines": {}, "stage_ms": {},
            })
        import run_eval
        run_eval.USAGE_TOTALS.update(
            {"prompt_tokens": 0, "completion_tokens": 0, "calls": 0})

    def test_empty_gives_none_not_zero(self):
        m = server._request_metrics()
        self.assertEqual(m["requests_total"], 0)
        self.assertIsNone(m["fail_rate"])
        self.assertIsNone(m["refusal_rate"])
        self.assertIsNone(m["latency_ms"]["p50"])
        self.assertIsNone(m["latency_ms"]["max"])

    def test_counts_and_rates(self):
        server._note_review({"ok": True, "engine": "local-rules"}, 900, "local-rules")
        server._note_review({"ok": True, "engine": "llm"}, 2000, "llm")
        server._note_review({"ok": False, "code": "REVIEW_VALIDATION_FAILED"}, 1500, "llm")
        m = server._request_metrics()
        self.assertEqual(m["requests_total"], 3)
        self.assertEqual(m["review_ok"], 2)
        self.assertEqual(m["review_failed"], 1)
        self.assertEqual(m["fail_rate"], round(1 / 3, 3))
        self.assertEqual(m["refusal_rate"], round(1 / 3, 3))
        self.assertEqual(m["engines"], {"local-rules": 1, "llm": 2})
        # 线性插值：numpy.percentile([900, 2000], 50) == 1450
        self.assertEqual(m["latency_ms"]["p50"], 1450)   # 只统计成功请求
        self.assertEqual(m["latency_ms"]["max"], 2000)

    def test_percentiles_on_small_samples(self):
        self.assertIsNone(server._pct([], 50))
        self.assertEqual(server._pct([7], 95), 7)
        self.assertEqual(server._pct([1, 2, 3, 4, 5], 50), 3)

    def test_stage_ms_has_all_four_keys_even_without_samples(self):
        """没样本的阶段要给 None + n=0，并保持页面文案的顺序。"""
        m = server._request_metrics()
        self.assertEqual(list(m["stage_ms"]), STAGES)
        for stage in STAGES:
            self.assertIsNone(m["stage_ms"][stage]["p50"])
            self.assertEqual(m["stage_ms"][stage]["n"], 0)

    def test_stage_ms_records_each_stage_separately(self):
        """分开记的意义：能看出慢在模型那段还是 OCR 那段，而不是只有一个总耗时。"""
        server._note_stage("desensitize", 120)
        server._note_stage("check", 31000)
        server._note_stage("check", 33000)
        server._note_stage("desensitize", 80)
        m = server._request_metrics()["stage_ms"]
        self.assertEqual(m["desensitize"]["n"], 2)
        self.assertEqual(m["desensitize"]["p50"], 100)
        self.assertEqual(m["check"]["n"], 2)
        self.assertEqual(m["check"]["p50"], 32000)
        self.assertIsNone(m["format"]["p50"])          # 这一段没有样本

    def test_unknown_stage_is_ignored(self):
        """阶段名写错不许静默混进指标——否则页面会多出一个没人认识的分段。"""
        server._note_stage("编造出来的阶段", 100)
        self.assertEqual(server._request_metrics()["stage_ms"]["check"]["n"], 0)
        self.assertNotIn("编造出来的阶段", server._request_metrics()["stage_ms"])

    def test_stage_ms_sample_window_is_bounded(self):
        for _ in range(260):
            server._note_stage("check", 10)
        self.assertLessEqual(server._request_metrics()["stage_ms"]["check"]["n"], 200)

    def test_tokens_none_before_any_call(self):
        """一次模型都没调（规则引擎模式）：给 None，不给 0。"""
        self.assertIsNone(server._request_metrics()["tokens"])

    def test_tokens_accumulate_across_calls(self):
        import run_eval
        run_eval._note_usage({"prompt_tokens": 8000, "completion_tokens": 1200})
        run_eval._note_usage({"prompt_tokens": 7000, "completion_tokens": 900})
        tokens = server._request_metrics()["tokens"]
        self.assertEqual(tokens["prompt_tokens"], 15000)
        self.assertEqual(tokens["completion_tokens"], 2100)
        self.assertEqual(tokens["calls"], 2)

    def test_usage_without_numbers_is_ignored(self):
        """有些服务商不回 usage；不能因此把 calls 加一（会把均值算小）。"""
        import run_eval
        run_eval._note_usage({})
        run_eval._note_usage({"prompt_tokens": "n/a"})
        self.assertEqual(run_eval.usage_snapshot()["calls"], 0)

    def test_metrics_fields_match_spec(self):
        """§3.7.4 的字段名是契约，少一个图会空、多一个没人读。"""
        m = server._request_metrics()
        for key in ("requests_total", "review_ok", "review_failed", "fail_rate",
                    "refusal_rate", "latency_ms", "stage_ms", "tokens",
                    "engines", "uptime_s"):
            self.assertIn(key, m)
        for key in ("p50", "p90", "p95", "max"):
            self.assertIn(key, m["latency_ms"])


class StreamEndpointTest(unittest.TestCase):
    """真起一个 HTTP 服务，走一遍 start → stream，确认事件与终态。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.tmp = tempfile.TemporaryDirectory()
        reports = Path(cls.tmp.name)
        cls.patches = [
            mock.patch.object(server, "REPORTS", reports),
            mock.patch.object(server, "HISTORY", reports / ".history.json"),
            mock.patch.object(server, "MOCK", True),
        ]
        for p in cls.patches:
            p.start()
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        with server.METRICS_LOCK:
            server.METRICS.update({
                "requests_total": 0, "review_ok": 0, "review_failed": 0,
                "refusal": 0, "latencies": [], "engines": {},
            })

    @classmethod
    def tearDownClass(cls) -> None:
        cls.httpd.shutdown()
        cls.httpd.server_close()
        for p in reversed(cls.patches):
            p.stop()
        cls.tmp.cleanup()

    def _post(self, path: str, body: dict) -> tuple[int, dict]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        data = json.dumps(body).encode("utf-8")
        conn.request("POST", path, body=data, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        payload = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, payload

    def _stream(self, job_id: str) -> list[tuple[str, dict]]:
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=60)
        conn.request("GET", f"/api/review/stream?job_id={job_id}")
        resp = conn.getresponse()
        self.assertEqual(resp.status, 200)
        self.assertIn("text/event-stream", resp.getheader("Content-Type") or "")
        self.assertEqual(resp.getheader("X-Accel-Buffering"), "no")
        events: list[tuple[str, dict]] = []
        event = None
        deadline = time.time() + 40
        while time.time() < deadline:
            line = resp.fp.readline()
            if not line:
                break
            text = line.decode("utf-8").strip()
            if text.startswith("event: "):
                event = text[len("event: "):]
            elif text.startswith("data: ") and event:
                events.append((event, json.loads(text[len("data: "):])))
                if event in ("done", "error"):
                    break
                event = None
        conn.close()
        return events

    def test_start_then_stream(self):
        _, ex = self._post("/api/extract", {"name": "测试简历.txt", "text": RESUME})
        self.assertTrue(ex.get("ok"), ex)
        sid = ex["session_id"]
        try:
            status, started = self._post("/api/review/start",
                                         {"session_id": sid, "job": "后端开发", "mode": "local"})
            self.assertEqual(status, 202)
            self.assertTrue(started.get("ok"), started)
            self.assertEqual(started["expires_in"], server.JOB_TTL)

            events = self._stream(started["job_id"])
            kinds = [k for k, _ in events]
            stages = [d["stage"] for k, d in events if k == "stage"]
            for name in STAGES:                       # 四段一个都不能少
                self.assertIn(name, stages, stages)
            # 顺序必须单调：1→2→3→4，不许回退
            self.assertEqual(stages, [s for s in STAGES if s in stages])
            self.assertEqual(kinds[-1], "done", kinds)
            self.assertTrue(events[-1][1].get("ok"), events[-1][1])
        finally:
            server.SESSIONS.pop(sid, None)

    def test_unknown_job_gives_explicit_error(self):
        events = self._stream("0" * 32)
        self.assertEqual(len(events), 1)
        kind, data = events[0]
        self.assertEqual(kind, "error")
        self.assertEqual(data["code"], "SSE_JOB_NOT_FOUND")

    def test_metrics_endpoint_has_requests_block(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        conn.request("GET", "/api/metrics")
        body = json.loads(conn.getresponse().read().decode("utf-8"))
        conn.close()
        self.assertIn("requests", body)
        self.assertIn("uptime_s", body["requests"])
        self.assertIn("本次进程", body["requests"]["window"])


class FallbackTest(unittest.TestCase):
    """服务端关了流式时必须明确说「请用同步接口」，而不是让前端干等。"""

    def test_sse_disabled_says_so(self):
        with mock.patch.object(server, "SSE_ENABLED", False):
            out = server.api_review_start({"session_id": "x"})
            self.assertFalse(out["ok"])
            self.assertEqual(out["code"], "SSE_DISABLED")
            self.assertIn("同步", out["error"])


class FrontendWiringTest(unittest.TestCase):
    """前端只许「照着事件点亮」，不许自己算进度。

    这里查的是名字对不对：前端写的 stage 名跟服务端 STAGE_LABEL 的键不一致时，
    接口全绿、进度灯却永远不亮——这类错跑测试才发现不了，只能在源码里对上。
    """

    HTML = (ROOT / "web" / "index.html").read_text(encoding="utf-8")

    def test_stage_names_match_server(self):
        import re
        m = re.search(r"const STAGE_LIST = \[(.*?)\];", self.HTML, re.S)
        self.assertIsNotNone(m, "index.html 里找不到 STAGE_LIST")
        keys = re.findall(r"\['([a-z_]+)'", m.group(1))
        self.assertEqual(keys, list(server.STAGE_LABEL.keys()))

    def test_uses_stream_then_falls_back(self):
        self.assertIn("new EventSource(started.stream)", self.HTML)
        self.assertIn("/api/review/start", self.HTML)
        self.assertIn("SSE_DISABLED", self.HTML)     # 服务端关了流式要能退回同步
        self.assertIn("/api/review',", self.HTML)    # 同步接口仍在（老服务/兜底）

    def test_no_fake_progress(self):
        # 进度只能由 stage 事件驱动：不允许出现「按时间推进百分比」这类写法
        lowered = self.HTML.lower()
        for bad in ("progressbar", "progress-bar", "estimated="):
            self.assertNotIn(bad, lowered)


if __name__ == "__main__":
    unittest.main(verbosity=2)
