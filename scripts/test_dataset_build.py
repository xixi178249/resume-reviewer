"""dataset_build.py 的守卫测试：去重、粘连切分、冻结已标注样本。

为什么值得单独一个套件（而不是随手试一下）：
    这个脚本会**改写数据集文件**。数据集是 v2.0 唯一能产出可信数字的东西，
    写坏一次（同一个人同时出现在开发集和留出集、或者已标注样本被挪去留出集）
    就意味着之前所有人工标注作废、所有指标不可引用。所以以下三件事必须有测试：

      1. 同样正文不许进两次（按内容哈希判断，不看文件名）
      2. 同一来源组只在同一侧（否则就是留出集泄露）
      3. 已有人工标注的样本，切分不许被改动

测试全部用临时目录 + 合成文本，不碰 samples/txt（那些是 gitignore 的真实简历）。

运行：
    python scripts/test_dataset_build.py
    python scripts/run_tests.py --suite dataset-build
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

import dataset_build  # noqa: E402

RESUME_A = """李同学
求职意向：后端开发工程师
教育背景
某某大学  软件工程  本科  2022.09-2026.06
专业技能
熟悉 Python、MySQL，了解 Redis
项目经历
独立实现短链服务，完成接口设计与压测，QPS 提升到 1200
实习经历
某某科技  后端开发实习生  2025.07-2025.09
负责订单模块两个接口的开发与联调，错误率降到 0.3%
"""

RESUME_B = """王同学
求职意向：产品经理
手机：13900000000  邮箱：wang@example.com
教育背景
某某大学  工商管理  本科  2022.09-2026.06  GPA 3.6/4.0
实习经历
某某公司  产品实习生  2025.06-2025.09
参与需求评审与竞品分析，输出 3 份需求文档，其中 2 份进入开发排期
牵头搭建用户反馈周报机制，每周汇总 40 条反馈，推动 3 个高频问题进入迭代
校园经历
组织校级创业比赛，招募 12 支队伍，完赛率 100%
"""


class Fixture:
    def __init__(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="dsbuild-"))
        self.raw = self.tmp / "raw"
        self.raw.mkdir()
        self.dataset_path = self.tmp / "dataset.json"
        self.ann_dir = self.tmp / "annotations"
        self.ann_dir.mkdir()
        self.dataset = {"schema_version": "human-eval-1",
                        "dataset_id": "test-ds", "samples": []}

    def write(self, name: str, text: str) -> Path:
        path = self.raw / name
        path.write_text(text, encoding="utf-8")
        return path

    def save_dataset(self, samples: list[dict]):
        self.dataset["samples"] = samples
        self.dataset_path.write_text(json.dumps(self.dataset, ensure_ascii=False),
                                     encoding="utf-8")

    def write_annotation(self, sid: str, reviewer: str = "R1"):
        payload = {
            "schema_version": "human-eval-1",
            "dataset_id": "test-ds",
            "reviewer_type": "independent_human",
            "blind_to_model": True,
            "completed_at": "2026-10-29 12:00",
            "reviewer_id": reviewer,
            "samples": {sid: {"blind_id": "B1", "job_target": "后端开发",
                              "dimensions": {"role_fit": 4, "impact": 3,
                                             "evidence_quality": 3, "structure": 4,
                                             "competitiveness": 3},
                              "verdict": "pass", "gate_hr": True,
                              "gate_interview": True, "confidence": 4, "issues": []}},
        }
        (self.ann_dir / f"{reviewer}.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")


class ScanTest(unittest.TestCase):
    def test_skips_too_short_file(self):
        fx = Fixture()
        fx.write("短.txt", "只有几行\n")
        entries, skipped = dataset_build.scan([fx.raw / "短.txt"])
        self.assertEqual(entries, [])
        self.assertTrue(any("正文只有" in s for s in skipped), skipped)

    def test_reads_normal_file(self):
        fx = Fixture()
        path = fx.write("a.txt", RESUME_A)
        entries, skipped = dataset_build.scan([path])
        self.assertEqual(len(entries), 1, skipped)
        self.assertEqual(entries[0].group, "a")


class PlanTest(unittest.TestCase):
    def _entries(self, fx: Fixture, texts: dict[str, str]):
        for name, text in texts.items():
            fx.write(name, text)
        entries, _ = dataset_build.scan([fx.raw / n for n in texts])
        return entries

    def test_dedupe_by_content_not_filename(self):
        fx = Fixture()
        entries = self._entries(fx, {"a.txt": RESUME_A, "a-副本.txt": RESUME_A})
        result = dataset_build.plan(entries, fx.dataset, set(), heldout_every=4)
        self.assertEqual(len(result.added), 1)
        self.assertEqual(len(result.duplicates), 1)
        self.assertIn("正文相同", result.duplicates[0])

    def test_ids_and_blind_tokens(self):
        fx = Fixture()
        entries = self._entries(fx, {"a.txt": RESUME_A, "b.txt": RESUME_B})
        result = dataset_build.plan(entries, fx.dataset, set(), heldout_every=4)
        ids = [s["sample_id"] for s in result.samples]
        self.assertEqual(ids, ["S001", "S002"])
        blinds = [s["blind_id"] for s in result.samples]
        self.assertEqual(len(set(blinds)), 2)
        self.assertTrue(all(b.startswith("K") for b in blinds))

    def test_heldout_every_distributes(self):
        fx = Fixture()
        texts = {f"r{i}.txt": RESUME_A.replace("李同学", f"李{i}").replace(
            "订单模块", f"订单模块{i}") for i in range(8)}
        entries = self._entries(fx, texts)
        result = dataset_build.plan(entries, fx.dataset, set(), heldout_every=4)
        splits = [s["split"] for s in result.samples]
        self.assertEqual(splits.count("heldout"), 2, splits)

    def test_group_follows_existing_split(self):
        fx = Fixture()
        fx.save_dataset([{"sample_id": "S001", "blind_id": "KAAAAAA", "job_target": "后端开发",
                          "split": "heldout", "source_group": "a", "text_file": "x.txt"}])
        entries = self._entries(fx, {"a.txt": RESUME_A})
        result = dataset_build.plan(entries, fx.dataset, set(), heldout_every=4)
        self.assertEqual(result.samples[-1]["split"], "heldout")

    def test_annotated_group_forced_to_development(self):
        fx = Fixture()
        # 已有 S001 与新人同在组 a，且 S001 已被标注
        fx.save_dataset([{"sample_id": "S001", "blind_id": "KAAAAAA", "job_target": "后端开发",
                          "split": "development", "source_group": "a", "text_file": "x.txt"}])
        entries = self._entries(fx, {"a.txt": RESUME_A})
        entries[0].group = "a"
        result = dataset_build.plan(entries, fx.dataset, {"S001"}, heldout_every=2)
        self.assertEqual(result.samples[-1]["split"], "development")

    def test_guess_job_target_from_filename(self):
        self.assertEqual(dataset_build.guess_job_target("05-产品经理-本科"), "产品经理")
        self.assertEqual(dataset_build.guess_job_target("后端-张三"), "后端")
        self.assertEqual(dataset_build.guess_job_target("随便一个名字"), "")


class VerifyTest(unittest.TestCase):
    def test_flags_group_across_splits(self):
        dataset = {"samples": [
            {"sample_id": "S001", "blind_id": "K1", "job_target": "后端", "split": "development",
             "source_group": "同一人"},
            {"sample_id": "S002", "blind_id": "K2", "job_target": "后端", "split": "heldout",
             "source_group": "同一人"},
        ]}
        problems = dataset_build.verify(dataset, set())
        self.assertTrue(any("构成泄露" in p for p in problems), problems)

    def test_flags_annotated_sample_moved_to_heldout(self):
        dataset = {"samples": [
            {"sample_id": "S001", "blind_id": "K1", "job_target": "后端",
             "split": "heldout", "source_group": "g1"},
        ]}
        problems = dataset_build.verify(dataset, {"S001"})
        self.assertTrue(any("切分被改动过" in p for p in problems), problems)

    def test_flags_duplicate_blind_and_missing_target(self):
        dataset = {"samples": [
            {"sample_id": "S001", "blind_id": "K1", "job_target": "", "split": "development",
             "source_group": "g1"},
            {"sample_id": "S002", "blind_id": "K1", "job_target": "后端", "split": "development",
             "source_group": "g2"},
        ]}
        pending: list[str] = []
        problems = dataset_build.verify(dataset, set(), pending)
        self.assertTrue(any("盲号重复" in p for p in problems), problems)
        # job_target 空只算「待补」，不算违规：先加样本后补岗位是正常流程
        self.assertTrue(any("job_target" in w for w in pending), pending)

    def test_clean_dataset_passes(self):
        dataset = {"samples": [
            {"sample_id": "S001", "blind_id": "K1", "job_target": "后端",
             "split": "development", "source_group": "g1"},
            {"sample_id": "S002", "blind_id": "K2", "job_target": "后端",
             "split": "heldout", "source_group": "g2"},
        ]}
        self.assertEqual(dataset_build.verify(dataset, {"S001"}), [])


class CliTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.script = Path(__file__).resolve().parent / "dataset_build.py"
        self.env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
        self.fx.save_dataset([])

    def _run(self, *extra):
        return subprocess.run(
            [sys.executable, str(self.script),
             "--dataset", str(self.fx.dataset_path),
             "--annotations", str(self.fx.ann_dir),
             *extra],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=self.env,
        )

    def test_dry_run_writes_nothing(self):
        self.fx.write("a.txt", RESUME_A)
        proc = self._run("--source", str(self.fx.raw), "--dry-run")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(self.fx.dataset_path.read_text(encoding="utf-8"))
        self.assertEqual(data["samples"], [])

    def test_adds_samples_and_writes(self):
        self.fx.write("a.txt", RESUME_A)
        self.fx.write("b.txt", RESUME_B)
        proc = self._run("--source", str(self.fx.raw))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(self.fx.dataset_path.read_text(encoding="utf-8"))
        self.assertEqual(len(data["samples"]), 2)
        self.assertEqual(data["counts"]["total"], 2)
        # 文件名猜不出岗位时留空，但必须提示「待补」，不能让人以为已经填好
        self.assertEqual(data["samples"][0]["job_target"], "")
        self.assertIn("待补", proc.stdout)

    def test_job_target_guessed_from_filename(self):
        self.fx.write("05-产品经理-本科.txt", RESUME_B)
        proc = self._run("--source", str(self.fx.raw))
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        data = json.loads(self.fx.dataset_path.read_text(encoding="utf-8"))
        self.assertEqual(data["samples"][0]["job_target"], "产品经理")

    def test_verify_mode_passes_on_clean(self):
        proc = self._run("--verify")
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("通过", proc.stdout)


if __name__ == "__main__":
    unittest.main(verbosity=2)
