"""benchmark.py 的守卫测试：留出集锁、指标口径、就绪判定、报告措辞。

为什么值得单独一个套件：
    这套代码的价值不在「能算 MAE」，而在**它拒绝在数据不够时给出数字**。
    如果哪天有人为了拿到一个好看的指标，把 heldout 锁拆了、或者把 None 改成 0、
    或者让 NOT_READY 的报告也能被当成准确度引用，那就等于把 v2.0 唯一的卖点
    自己拆了。所以这三件事都必须有测试盯着。

测试全部用临时目录里的合成数据，不依赖 samples/txt（那些是 gitignore 的真实
简历，CI 里不存在）。

运行：
    python scripts/test_benchmark.py
    python scripts/run_tests.py --suite benchmark
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import benchmark  # noqa: E402

RESUME = """张同学
求职意向：后端开发工程师
手机：13800000000  邮箱：zhang@example.com
教育背景
某某大学  计算机科学与技术  本科  2022.09-2026.06
专业技能
熟悉 Python / Go，了解 MySQL、Redis
项目经历
课程项目：实现一个短链服务，独立完成接口设计与压测
实习经历
某某科技  后端开发实习生  2025.07-2025.09
参与订单模块开发，负责两个接口
"""


def _dims(role=4, impact=4, eq=4, st=4, cp=4) -> dict:
    return {"role_fit": role, "impact": impact, "evidence_quality": eq,
            "structure": st, "competitiveness": cp}


def _annotation(dataset_id: str, reviewer_id: str, samples: dict) -> dict:
    return {
        "schema_version": "human-eval-1",
        "dataset_id": dataset_id,
        "reviewer_type": "independent_human",
        "blind_to_model": True,
        "completed_at": "2026-10-29 12:00",
        "reviewer_id": reviewer_id,
        "samples": samples,
    }


def _marked(sid: str, dims: dict, verdict="pass", gate_hr=True, gate_interview=True) -> dict:
    return {
        "blind_id": f"B-{sid}",
        "job_target": "后端开发",
        "dimensions": dims,
        "verdict": verdict,
        "gate_hr": gate_hr,
        "gate_interview": gate_interview,
        "confidence": 4,
        "issues": [{"dimension": "impact", "evidence": "没有量化结果", "severity": "low"}],
    }


class Fixture:
    """一套临时数据集：S01/S02 在 development，S03 在 heldout。"""

    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="bench-"))
        self.text = self.tmp / "resume.txt"
        self.text.write_text(RESUME, encoding="utf-8")
        self.dataset = {
            "schema_version": "human-eval-1",
            "dataset_id": "test-ds",
            "targets": {"total_samples": 3, "heldout_min": 1},
            "samples": [
                {"sample_id": "S01", "job_target": "后端开发", "split": "development",
                 "source_group": "g1", "text_file": str(self.text)},
                {"sample_id": "S02", "job_target": "后端开发", "split": "development",
                 "source_group": "g2", "text_file": str(self.text)},
                {"sample_id": "S03", "job_target": "后端开发", "split": "heldout",
                 "source_group": "g3", "text_file": str(self.text)},
            ],
        }
        self.dataset_path = self.tmp / "dataset.json"
        self.dataset_path.write_text(json.dumps(self.dataset, ensure_ascii=False),
                                     encoding="utf-8")
        self.ann_dir = self.tmp / "annotations"
        self.ann_dir.mkdir()

    def write_annotations(self, reviewers=2, with_heldout=False):
        sid_den = {"S01": _marked("S01", _dims(4, 4, 4, 4, 4)),
                   "S02": _marked("S02", _dims(3, 3, 3, 3, 3), verdict="borderline")}
        if with_heldout:
            sid_den["S03"] = _marked("S03", _dims(4, 3, 3, 4, 3))
        for i in range(reviewers):
            uid = f"R{i + 1}"
            payload = _annotation("test-ds", uid,
                                  json.loads(json.dumps(sid_den, ensure_ascii=False)))
            (self.ann_dir / f"{uid}.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class SelectSamplesTest(unittest.TestCase):
    """heldout 默认锁死——这是整个 v2.0 可信度的地基。"""

    def setUp(self):
        self.fx = Fixture()

    def test_default_only_development(self):
        picked = benchmark.select_samples(self.fx.dataset)
        self.assertEqual([s["sample_id"] for s in picked], ["S01", "S02"])

    def test_heldout_locked_by_default(self):
        with self.assertRaises(benchmark.HeldoutLocked) as ctx:
            benchmark.select_samples(self.fx.dataset, split="heldout")
        msg = str(ctx.exception)
        self.assertIn("S03", msg)
        self.assertIn("--allow-heldout", msg)

    def test_all_also_locked(self):
        with self.assertRaises(benchmark.HeldoutLocked):
            benchmark.select_samples(self.fx.dataset, split="all")

    def test_heldout_readable_when_allowed(self):
        picked = benchmark.select_samples(self.fx.dataset, split="heldout",
                                          allow_heldout=True)
        self.assertEqual([s["sample_id"] for s in picked], ["S03"])

    def test_all_when_allowed(self):
        picked = benchmark.select_samples(self.fx.dataset, split="all", allow_heldout=True)
        self.assertEqual(len(picked), 3)

    def test_invalid_split_rejected(self):
        with self.assertRaises(ValueError):
            benchmark.select_samples(self.fx.dataset, split="train")


class MetricsTest(unittest.TestCase):
    """口径：算不出来就给 None，绝不给 0。"""

    def test_empty_pairs_all_none(self):
        m = benchmark.compute_metrics([])
        self.assertEqual(m["n"], 0)
        for key in ("mae", "rmse", "pearson", "spearman", "kappa_verdict", "qwk",
                    "within5", "within10", "gate_hr_agreement"):
            self.assertIsNone(m[key], f"{key} 应该是 None 而不是 0")

    def test_known_mae_and_hit_rates(self):
        pairs = [
            {"sample_id": "a", "human_score": 80.0, "model_score": 70,
             "human_verdict": "strong_pass", "model_verdict": "pass",
             "human_hr": True, "model_hr": True,
             "human_interview": True, "model_interview": False},
            {"sample_id": "b", "human_score": 50.0, "model_score": 52,
             "human_verdict": "borderline", "model_verdict": "borderline",
             "human_hr": True, "model_hr": True,
             "human_interview": False, "model_interview": False},
        ]
        m = benchmark.compute_metrics(pairs)
        self.assertEqual(m["n"], 2)
        self.assertAlmostEqual(m["mae"], 6.0, places=6)
        self.assertAlmostEqual(m["within10"], 1.0, places=6)
        self.assertAlmostEqual(m["within5"], 0.5, places=6)
        self.assertAlmostEqual(m["gate_hr_agreement"], 1.0, places=6)
        self.assertAlmostEqual(m["gate_interview_agreement"], 0.5, places=6)


class PairsTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.fx.write_annotations()

    def test_consensus_used_as_human_score(self):
        annotations, rejected = benchmark.human_eval.load_annotations(
            self.fx.ann_dir, self.fx.dataset)
        self.assertEqual(rejected, [])
        samples = benchmark.select_samples(self.fx.dataset)
        pairs, notes = benchmark.build_pairs(samples, annotations, "local")
        self.assertEqual(len(pairs), 2, notes)
        # 两人都给 4/4/4/4/4 → 80 分；共识分就是 80
        self.assertEqual(pairs[0]["human_score"], 80.0)
        self.assertIsInstance(pairs[0]["model_score"], int)

    def test_missing_text_becomes_note(self):
        samples = [{"sample_id": "S01", "job_target": "后端开发", "split": "development",
                    "source_group": "g1", "text_file": str(self.fx.tmp / "nope.txt")}]
        annotations = [_annotation("test-ds", "R1", {"S01": _marked("S01", _dims())})]
        pairs, notes = benchmark.build_pairs(samples, annotations, "local")
        self.assertEqual(pairs, [])
        self.assertTrue(any("不存在" in n for n in notes), notes)


class ReadinessTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()

    def test_not_ready_when_dataset_small(self):
        ready = benchmark.readiness(self.fx.dataset, [], [], [], allow_heldout=False)
        self.assertFalse(ready["ready"])
        joined = "；".join(ready["reasons"])
        self.assertIn("数据集", joined)
        self.assertIn("标注者", joined)
        self.assertIn("heldout", joined)

    def test_report_says_not_ready_and_forbids_citing(self):
        ready = benchmark.readiness(self.fx.dataset, [], [], [], allow_heldout=False)
        report = benchmark.render_report(benchmark.compute_metrics([]), ready, [], [],
                                         {"dataset": "d", "annotations": "a",
                                          "predictor": "local", "split": "development"})
        self.assertIn("NOT_READY", report)
        self.assertIn("不得对外引用", report)
        self.assertIn("旧 MAE=8.0 不得继承", report)

    def test_two_reviewers_counted(self):
        self.fx.write_annotations(reviewers=2)
        annotations, _ = benchmark.human_eval.load_annotations(
            self.fx.ann_dir, self.fx.dataset)
        ready = benchmark.readiness(self.fx.dataset, annotations, [], [], allow_heldout=False)
        self.assertEqual(ready["reviewers"], ["R1", "R2"])
        self.assertEqual(ready["annotated_samples"], 2)


class RunTest(unittest.TestCase):
    def test_run_produces_pairs_with_local_predictor(self):
        fx = Fixture()
        fx.write_annotations()
        result = benchmark.run(fx.dataset_path, fx.ann_dir, "local", "development", False)
        self.assertGreaterEqual(len(result["pairs"]), 1)
        self.assertIsNotNone(result["metrics"]["mae"])


class CliTest(unittest.TestCase):
    """退出码要能被 CI 用：没就绪就必须是 2，不是 0。"""

    def setUp(self):
        self.fx = Fixture()
        self.script = Path(__file__).resolve().parent / "benchmark.py"

    def _run(self, *extra):
        # Windows 上子进程默认用 GBK 写 stdout/stderr，父进程按 UTF-8 读会变乱码。
        # 统一强制子进程用 UTF-8，测试才不会因为编码而假失败。
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        return subprocess.run(
            [sys.executable, str(self.script),
             "--dataset", str(self.fx.dataset_path),
             "--annotations", str(self.fx.ann_dir),
             "--out", str(self.fx.tmp / "report.md"),
             "--json", str(self.fx.tmp / "report.json"),
             *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env,
        )

    def test_require_ready_exits_2(self):
        proc = self._run("--require-ready")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("NOT_READY", proc.stdout)

    def test_heldout_lock_exits_2(self):
        proc = self._run("--split", "heldout")
        self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertIn("已锁定", proc.stderr)

    def test_normal_run_exits_0_and_writes_report(self):
        self.fx.write_annotations()
        proc = self._run()
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertTrue((self.fx.tmp / "report.md").exists())
        payload = json.loads((self.fx.tmp / "report.json").read_text(encoding="utf-8"))
        self.assertIn("readiness", payload)
        self.assertIn("metrics", payload)


class FmtTest(unittest.TestCase):
    def test_none_renders_as_dash(self):
        self.assertEqual(benchmark._fmt(None), "—")
        self.assertEqual(benchmark._fmt_ci(None), "—")
        self.assertEqual(benchmark._fmt_ratio(None), "—")

    def test_ratio_and_ci(self):
        self.assertEqual(benchmark._fmt_ratio(0.8333), "83%")
        self.assertEqual(benchmark._fmt_ci((1.234, 5.678)), "1.23 ~ 5.68")


class PilotTest(unittest.TestCase):
    """pilot 模式：只回答「两人是否同一把尺子」，不回答「模型准不准」。

    存在的理由：正式门槛是 60 份 × 2 人，凑齐要几周；但「锚点卡能不能用」5 份就看得出。
    如果 pilot 报告也能被当成准确度引用，那它就变成了绕过正式门槛的后门——所以要测。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pilot-"))
        self.text = self.tmp / "resume.txt"
        self.text.write_text(RESUME, encoding="utf-8")
        samples = [
            {"sample_id": f"S{i:02d}", "job_target": "后端开发", "split": "development",
             "source_group": f"g{i}", "text_file": str(self.text)}
            for i in range(1, 7)
        ]
        self.dataset = {"schema_version": "human-eval-1", "dataset_id": "pilot-ds",
                        "targets": {"total_samples": 6, "heldout_min": 0},
                        "samples": samples}
        self.dataset_path = self.tmp / "dataset.json"
        self.dataset_path.write_text(json.dumps(self.dataset, ensure_ascii=False),
                                     encoding="utf-8")
        self.ann_dir = self.tmp / "annotations"
        self.ann_dir.mkdir()

    def _write(self, reviewers=2, count=6):
        marked = {f"S{i:02d}": _marked(f"S{i:02d}", _dims(4, 4, 4, 4, 4)) for i in range(1, count + 1)}
        for i in range(reviewers):
            uid = f"R{i + 1}"
            (self.ann_dir / f"{uid}.json").write_text(json.dumps(
                _annotation("pilot-ds", uid, json.loads(json.dumps(marked, ensure_ascii=False))),
                ensure_ascii=False), encoding="utf-8")

    def test_no_annotations_not_ready(self):
        ready = benchmark.pilot_readiness(self.dataset, [], [])
        self.assertFalse(ready["ready"])
        self.assertTrue(ready["pilot"])
        self.assertTrue(any("标注者" in r for r in ready["reasons"]))

    def test_one_reviewer_not_enough(self):
        self._write(reviewers=1)
        anns, _ = benchmark.human_eval.load_annotations(self.ann_dir, self.dataset)
        pairs, _ = benchmark.build_pairs(self.dataset["samples"], anns, "local")
        ready = benchmark.pilot_readiness(self.dataset, anns, pairs)
        self.assertFalse(ready["ready"])

    def test_ready_with_two_reviewers_and_five_pairs(self):
        self._write(reviewers=2, count=5)
        anns, _ = benchmark.human_eval.load_annotations(self.ann_dir, self.dataset)
        pairs, _ = benchmark.build_pairs(self.dataset["samples"], anns, "local")
        ready = benchmark.pilot_readiness(self.dataset, anns, pairs)
        self.assertTrue(ready["ready"], ready["reasons"])
        self.assertEqual(ready["evaluated_samples"], 5)

    def test_pilot_report_forbids_quoting_mae(self):
        self._write(reviewers=2, count=5)
        anns, _ = benchmark.human_eval.load_annotations(self.ann_dir, self.dataset)
        pairs, _ = benchmark.build_pairs(self.dataset["samples"], anns, "local")
        metrics = benchmark.compute_metrics(pairs)
        ready = benchmark.pilot_readiness(self.dataset, anns, pairs)
        report = benchmark.render_report(metrics, ready, pairs, [],
                                         {"dataset": "d", "annotations": "a",
                                          "predictor": "local", "split": "development",
                                          "pilot": True})
        self.assertIn("PILOT", report)
        self.assertIn("不回答「模型准不准」", report)
        self.assertIn("不可以看的", report)
        self.assertIn("pilot", report.lower())

    def test_formal_mode_still_not_ready(self):
        """同样的数据，正式模式必须仍然是 NOT_READY——pilot 不是后门。"""
        self._write(reviewers=2, count=5)
        result = benchmark.run(self.dataset_path, self.ann_dir, "local", "development", False)
        self.assertFalse(result["readiness"]["ready"])
        self.assertNotIn("pilot", result["readiness"])
        pilot = benchmark.run(self.dataset_path, self.ann_dir, "local", "development",
                              False, pilot=True)
        self.assertTrue(pilot["readiness"]["ready"])
        self.assertTrue(pilot["readiness"]["pilot"])

    def test_cli_pilot_exit_0_and_marks_report(self):
        self._write(reviewers=2, count=5)
        env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        out_md = self.tmp / "pilot.md"
        proc = subprocess.run(
            [sys.executable, str(Path(__file__).resolve().parent / "benchmark.py"),
             "--dataset", str(self.dataset_path), "--annotations", str(self.ann_dir),
             "--pilot", "--out", str(out_md), "--json", str(self.tmp / "pilot.json")],
            capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("PILOT_READY", proc.stdout)
        self.assertIn("PILOT", out_md.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
