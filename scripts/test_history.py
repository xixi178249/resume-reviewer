"""阶段 C 历史归属回归：review_id、唯一文件、A/B 预览、旧记录、并发写。

运行：
    python scripts/test_history.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import server  # noqa: E402

RESUME_A = (ROOT / "samples" / "demo" / "02-后端-部分量化.txt").read_text(encoding="utf-8")
RESUME_B = (ROOT / "samples" / "demo" / "03-后端-较完整.txt").read_text(encoding="utf-8")
BASE = json.loads(
    (ROOT / "prompts" / "example-output-v1.8-good.json").read_text(encoding="utf-8")
)


class HistoryBindingTest(unittest.TestCase):
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
        self.sids: list[str] = []

    def tearDown(self) -> None:
        for sid in self.sids:
            server.SESSIONS.pop(sid, None)
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def _extract(self, text: str, name: str = "同名简历.txt") -> dict:
        res = server.api_extract({"text": text, "name": name, "allow_images": False})
        self.assertTrue(res["ok"], res)
        self.sids.append(res["session_id"])
        return res

    def _review(self, session_id: str, job: str = "后端开发实习生", jd: str = "") -> dict:
        res = server.api_review({
            "session_id": session_id,
            "job": job,
            "jd": jd,
            "visual": False,
            "repeat": 1,
        })
        self.assertTrue(res["ok"], res)
        return res

    def test_same_name_different_content_get_unique_ids_and_files(self) -> None:
        a = self._review(self._extract(RESUME_A)["session_id"])
        b = self._review(self._extract(RESUME_B)["session_id"])
        self.assertNotEqual(a["review_id"], b["review_id"])
        self.assertEqual(len(a["review_id"]), 32)
        self.assertTrue((self.reports / f"同名简历-{a['review_id'][:12]}.md").exists())
        self.assertTrue((self.reports / f"同名简历-{b['review_id'][:12]}.md").exists())
        self.assertEqual(len(list(self.reports.glob("同名简历-*.md"))), 2)

    def test_report_preview_is_bound_to_its_own_id(self) -> None:
        a = self._review(self._extract(RESUME_A)["session_id"])
        b = self._review(self._extract(RESUME_B)["session_id"])
        ra = server.api_report(a["review_id"])
        rb = server.api_report(b["review_id"])
        self.assertTrue(ra["ok"], ra)
        self.assertTrue(rb["ok"], rb)
        self.assertEqual(ra["review_id"], a["review_id"])
        self.assertIn(RESUME_A[:40], ra["resume_text"])
        self.assertIn(RESUME_B[:40], rb["resume_text"])
        self.assertNotEqual(ra["resume_text"], rb["resume_text"])

    def test_history_without_resume_text_is_an_explicit_empty_state(self) -> None:
        a = self._review(self._extract(RESUME_A)["session_id"])
        txt = next(self.reports.glob(f"*{a['review_id'][:12]}.txt"))
        txt.unlink()
        rep = server.api_report(a["review_id"])
        self.assertTrue(rep["ok"], rep)
        self.assertFalse(rep["resume_available"])
        self.assertEqual(rep["resume_text"], "")

    def test_history_lists_each_review_separately(self) -> None:
        a = self._review(self._extract(RESUME_A)["session_id"])
        b = self._review(self._extract(RESUME_B)["session_id"])
        hist = server.api_history()
        ids = {x["review_id"] for x in hist["items"]}
        self.assertIn(a["review_id"], ids)
        self.assertIn(b["review_id"], ids)
        self.assertTrue(all(x["has_report"] for x in hist["items"]))

    def test_delta_only_when_content_and_config_match(self) -> None:
        first = self._review(self._extract(RESUME_A)["session_id"])
        same = self._review(self._extract(RESUME_A)["session_id"])
        changed = self._review(
            self._extract(RESUME_A)["session_id"],
            job="产品经理实习生",
        )
        self.assertEqual(same["delta"], 0)
        self.assertEqual(same["prev"]["review_id"], first["review_id"])
        self.assertIsNone(changed["delta"])
        self.assertIn("不直接比较", changed["comparison_note"])

    def test_delete_only_removes_selected_review(self) -> None:
        a = self._review(self._extract(RESUME_A)["session_id"])
        b = self._review(self._extract(RESUME_B)["session_id"])
        res = server.api_delete_report(a["review_id"])
        self.assertTrue(res["ok"], res)
        self.assertFalse((self.reports / f"同名简历-{a['review_id'][:12]}.md").exists())
        self.assertTrue((self.reports / f"同名简历-{b['review_id'][:12]}.md").exists())
        self.assertFalse(server.api_report(a["review_id"])["ok"])
        self.assertTrue(server.api_report(b["review_id"])["ok"])

    def test_invalid_ids_are_rejected_before_any_path_lookup(self) -> None:
        for bad in ("../outside", ".history", "not-an-id", ""):
            self.assertFalse(server.api_report(bad)["ok"])
            self.assertEqual(server.api_delete_report(bad).get("_status"), 400)

    def test_legacy_history_is_readable_and_deletable(self) -> None:
        stem = "旧简历.v1"
        (self.reports / f"{stem}.md").write_text("# old report", encoding="utf-8")
        (self.reports / f"{stem}.json").write_text(
            json.dumps(BASE, ensure_ascii=False), encoding="utf-8"
        )
        (self.reports / f"{stem}.txt").write_text(RESUME_A, encoding="utf-8")
        (self.reports / ".history.json").write_text(json.dumps({
            stem: [
                {"ts": "2026-01-01 10:00", "score": 61, "verdict": "borderline", "job": "旧岗位"},
                {"ts": "2026-01-02 11:00", "score": 70, "verdict": "pass", "job": "后端"},
            ]
        }, ensure_ascii=False), encoding="utf-8")

        hist = server.api_history()
        legacy = next(x for x in hist["items"] if x["legacy"])
        self.assertEqual(legacy["name"], stem)
        self.assertEqual(legacy["legacy_runs"], 2)
        rid = legacy["review_id"]
        rep = server.api_report(rid)
        self.assertTrue(rep["ok"], rep)
        self.assertIn(RESUME_A[:40], rep["resume_text"])
        self.assertTrue(server.api_delete_report(rid)["ok"])
        self.assertFalse((self.reports / f"{stem}.md").exists())
        self.assertFalse(any(x["review_id"] == rid for x in server.api_history()["items"]))

    def test_concurrent_history_appends_do_not_lose_entries(self) -> None:
        def add(i: int) -> None:
            server._history_append({
                "review_id": f"{i:032x}",
                "name": f"并发-{i}",
                "file_stem": f"并发-{i}-{i:012x}",
                "created_at": f"2026-10-24 15:{i:02d}:00",
                "score": 60 + i,
                "verdict": "pass",
            })

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(add, range(12)))
        data = server._history_load()
        self.assertEqual(len(data["reviews"]), 12)
        self.assertEqual(len({r["review_id"] for r in data["reviews"]}), 12)


if __name__ == "__main__":
    unittest.main(verbosity=2)
