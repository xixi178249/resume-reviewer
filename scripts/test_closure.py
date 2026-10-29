"""改稿闭环落实率的测试。

这个脚本最容易犯的错不是算错，而是**在数据不够时算出一个数**。
3 次观察出 67% 会被当成结论，所以一半的测试都在钉「什么时候必须返回 None」。
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import closure  # noqa: E402


def _meta(applied=0, untouched=0, changed=0, unknown=0, *, compare=True, **extra) -> dict:
    data = {
        "review_id": "r1",
        "name": "简历A",
        "file_stem": "a",
        "created_at": extra.pop("created_at", "2026-10-29 10:00:00"),
        "engine": extra.pop("engine", "llm"),
        "model": "deepseek-flash",
        "schema_version": "v1.8",
        "overall_score": 70,
        "verdict": "pass",
        "compare": (
            {
                "prev_stem": "a_prev",
                "prev_at": "2026-10-28 10:00:00",
                "applied_counts": {
                    "applied": applied,
                    "untouched": untouched,
                    "changed": changed,
                    "unknown": unknown,
                },
            }
            if compare
            else None
        ),
    }
    data.update(extra)
    return data


class _TmpReports:
    def __init__(self):
        self.dir = Path(tempfile.mkdtemp(prefix="closure-"))

    def write(self, name: str, payload) -> Path:
        path = self.dir / name
        if isinstance(payload, (dict, list)):
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        else:
            path.write_text(str(payload), encoding="utf-8")
        return path

    def cleanup(self):
        for path in sorted(self.dir.rglob("*"), reverse=True):
            if path.is_file():
                path.unlink()
            else:
                path.rmdir()
        if self.dir.exists():
            self.dir.rmdir()


class CollectTest(unittest.TestCase):
    def setUp(self):
        self.tmp = _TmpReports()

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_dir_returns_empty(self):
        self.assertEqual(closure.collect(self.tmp.dir / "nope"), [])

    def test_only_meta_with_compare_counts(self):
        self.tmp.write("a.meta.json", _meta(applied=2, untouched=1))
        self.tmp.write("b.meta.json", _meta(compare=False))
        records = closure.collect(self.tmp.dir)
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["file_stem"], "a")
        self.assertEqual(records[0]["applied"], 2)

    def test_broken_json_is_skipped(self):
        self.tmp.write("bad.meta.json", "{not json")
        self.tmp.write("a.meta.json", _meta(applied=1))
        self.assertEqual(len(closure.collect(self.tmp.dir)), 1)

    def test_negative_counts_clamped(self):
        meta = _meta(applied=3)
        meta["compare"]["applied_counts"]["applied"] = -5
        self.tmp.write("a.meta.json", meta)
        self.assertEqual(closure.collect(self.tmp.dir)[0]["applied"], 0)

    def test_sorted_newest_first(self):
        self.tmp.write("old.meta.json", _meta(applied=1, created_at="2026-10-01 09:00:00"))
        self.tmp.write("new.meta.json", _meta(applied=1, created_at="2026-10-29 09:00:00"))
        self.assertEqual(
            [r["file_stem"] for r in closure.collect(self.tmp.dir)],
            ["a", "a"],   # 两份都是 file_stem=a，只看顺序：created_at 倒序
        )
        self.assertEqual(
            closure.collect(self.tmp.dir)[0]["created_at"], "2026-10-29 09:00:00"
        )


class SummarizeTest(unittest.TestCase):
    def test_empty_says_why_not_zero(self):
        summary = closure.summarize([], min_pairs=10)
        self.assertEqual(summary["status"], "insufficient_data")
        self.assertIsNone(summary["apply_rate"])
        self.assertIn("还没有", summary["reason"])
        self.assertEqual(summary["decided"], 0)

    def test_below_threshold_no_rate(self):
        records = [{"applied": 6, "untouched": 2, "changed": 0, "unknown": 0, "engine": "llm"}]
        summary = closure.summarize(records, min_pairs=10)
        self.assertEqual(summary["decided"], 8)
        self.assertIsNone(summary["apply_rate"])
        self.assertIn("少于 10 条", summary["reason"])

    def test_above_threshold_gives_rate(self):
        records = [{"applied": 8, "untouched": 4, "changed": 3, "unknown": 5, "engine": "llm"}]
        summary = closure.summarize(records, min_pairs=10)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["decided"], 12)
        self.assertAlmostEqual(summary["apply_rate"], 8 / 12, places=4)

    def test_changed_and_unknown_not_in_denominator(self):
        records = [{"applied": 5, "untouched": 5, "changed": 20, "unknown": 30, "engine": "llm"}]
        summary = closure.summarize(records, min_pairs=10)
        self.assertEqual(summary["decided"], 10)
        self.assertAlmostEqual(summary["apply_rate"], 0.5, places=4)

    def test_local_engine_excluded_from_rate(self):
        """规则引擎的改写是占位模板，逐字比对必然 untouched——不能让它把落实率压成 0。"""
        records = [
            {"applied": 0, "untouched": 9, "changed": 0, "unknown": 0, "engine": "local-rules"},
            {"applied": 8, "untouched": 2, "changed": 0, "unknown": 0, "engine": "llm"},
        ]
        summary = closure.summarize(records, min_pairs=10)
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(summary["decided"], 10)      # 只有模型引擎的 10 条
        self.assertAlmostEqual(summary["apply_rate"], 0.8, places=4)
        self.assertIn("local-rules", summary["by_engine"])
        self.assertEqual(summary["by_engine"]["local-rules"]["decided"], 9)

    def test_only_local_engine_says_why(self):
        records = [{"applied": 0, "untouched": 12, "changed": 0, "unknown": 0, "engine": "local-rules"}]
        summary = closure.summarize(records, min_pairs=10)
        self.assertEqual(summary["status"], "insufficient_data")
        self.assertIn("占位模板", summary["reason"])

    def test_only_unjudgeable_says_so(self):
        records = [{"applied": 0, "untouched": 0, "changed": 4, "unknown": 2, "engine": "llm"}]
        summary = closure.summarize(records, min_pairs=1)
        self.assertEqual(summary["status"], "insufficient_data")
        self.assertIn("没有可判定", summary["reason"])

    def test_totals_across_records(self):
        records = [
            {"applied": 3, "untouched": 1, "changed": 0, "unknown": 0, "engine": "llm"},
            {"applied": 4, "untouched": 4, "changed": 1, "unknown": 1, "engine": "llm"},
        ]
        summary = closure.summarize(records, min_pairs=10)
        self.assertEqual(summary["applied"], 7)
        self.assertEqual(summary["untouched"], 5)
        self.assertEqual(summary["rewrites_total"], 14)
        self.assertEqual(summary["reports_with_meta"], 2)

    def test_default_threshold_comes_from_rubric(self):
        self.assertEqual(closure.summarize([])["min_pairs"], closure.default_min_pairs())

    def test_markdown_states_insufficient(self):
        text = closure.to_markdown(closure.summarize([], min_pairs=10))
        self.assertIn("数据不够", text)
        self.assertIn("不进分母", text)


class CliTest(unittest.TestCase):
    def test_json_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "a.meta.json").write_text(
                json.dumps(_meta(applied=9, untouched=1), ensure_ascii=False), encoding="utf-8"
            )
            out = subprocess.run(
                [sys.executable, str(SCRIPTS / "closure.py"), "--reports", tmp, "--json"],
                capture_output=True, text=True, encoding="utf-8", timeout=60,
                env={
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONUTF8": "1",
                    "SYSTEMROOT": __import__("os").environ.get("SYSTEMROOT", ""),
                    "PATH": __import__("os").environ.get("PATH", ""),
                },
            )
            self.assertEqual(out.returncode, 0, out.stderr)
            payload = json.loads(out.stdout)
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["summary"]["status"], "ok")
            self.assertAlmostEqual(payload["summary"]["apply_rate"], 0.9, places=4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
