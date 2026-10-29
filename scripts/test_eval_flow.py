#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""整条人工评测链路的端到端测试（不联网、不花钱、不碰真实标注）。

为什么要有这个文件：`test_benchmark.py` 测的是「函数输入什么输出什么」，
`test_blind_annotation.py` 测的是「盲评包长什么样」。但真实的失败发生在**命令之间**：

    packet 出的待填表       →  人填完的 json 能不能被 validate 接受
    人填完的 json           →  status 会不会说「pilot 就绪」
    status 就绪             →  benchmark --pilot 会不会给 PILOT_READY
    同一批数据              →  正式模式必须仍然 NOT_READY

这四个交接点此前一个都没有测过。v2.0.1 里那个「标注明明填了却显示 0 人」的 bug
正是这种交接处的问题（`load_annotations()` 只吃目录，被当成按文件调用），
单测各自全绿，链路整体是断的。

做法：在 `samples/eval/_flow_tmp/` 下造 6 份合成简历 + 数据集，用和真人完全一样的
命令走一遍；两人对其中 5 份打分，断言 pilot 就绪而正式门槛未就绪。跑完即删。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = ROOT / "scripts"
TMP = ROOT / "samples" / "eval" / "_flow_tmp"

RESUME = (
    "{who}　求职意向：后端开发工程师\n"
    "教育背景：某大学 计算机科学与技术 本科 2022.09-2026.06\n"
    "实习经历：2025.06-2025.09 某科技有限公司 后端开发实习生\n"
    "· 负责订单查询接口开发，把平均响应时间从 480ms 降到 120ms，覆盖 12 个调用方。\n"
    "· 参与日志采集脚本重构，用 Python 重写后单次处理时间从 6 分钟降到 40 秒。\n"
    "· 用 Redis 给热点商品接口加缓存，命中率 91%，数据库 QPS 下降 63%。\n"
    "项目经历：校园二手交易平台（个人项目）\n"
    "· 独立开发后端 14 个接口，日活 200 人，累计注册 1800 人。\n"
    "· 写了一份接口文档，把联调时间从 3 天缩到 6 小时。\n"
    "技能：Python、Java、MySQL、Redis、Git\n"
)

# 两份不同的评分（分差 6 分，低于仲裁阈值 15）
DIMS_R01 = {"role_fit": 3, "impact": 3, "evidence_quality": 3, "structure": 3, "competitiveness": 3}
DIMS_R02 = {"role_fit": 3, "impact": 3, "evidence_quality": 3, "structure": 4, "competitiveness": 4}
VERDICT = {"role_fit": "", "overall": "borderline"}


def _env() -> dict:
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"  # Windows 默认 GBK，父进程按 utf-8 读会乱码
    env["PYTHONUTF8"] = "1"
    return env


def _run(args: list[str], expect: int = 0) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        [sys.executable, "-X", "utf8", *args],
        cwd=str(ROOT), env=_env(), capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != expect:
        raise AssertionError(
            f"命令退出码 {proc.returncode}，期望 {expect}：{' '.join(args)}\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )
    return proc


def _dimensions(score_target: str) -> dict:
    return dict(DIMS_R01 if score_target == "r01" else DIMS_R02)


class EvalFlowTest(unittest.TestCase):
    """packet → 填表 → validate → status → benchmark --pilot"""

    @classmethod
    def setUpClass(cls):
        if TMP.exists():
            shutil.rmtree(TMP)
        (TMP / "resumes").mkdir(parents=True)
        cls.dataset = TMP / "dataset.json"
        samples = []
        for i in range(1, 7):
            sid = f"F{i:02d}"
            rel = f"samples/eval/_flow_tmp/resumes/{sid}.txt"
            (ROOT / rel).write_text(
                RESUME.format(who=f"测试候选人{i}号"), encoding="utf-8")
            samples.append({
                "sample_id": sid,
                "blind_id": f"X{i:02d}",
                "text_file": rel,
                "job_target": "后端开发工程师",
                "jd": "",
                "source_group": f"grp-{sid}",
                "stratum": {"role_family": "后端", "seniority": "实习"},
                "split": "development",
            })
        cls.dataset.write_text(json.dumps({
            "schema_version": "human-eval-1",
            "dataset_id": "flow-e2e",
            "created_at": "2026-10-29",
            "samples": samples,
        }, ensure_ascii=False, indent=2), encoding="utf-8")

        # 1) 打包：和真人一样用 packet
        cls.handouts = TMP / "handouts"
        _run([str(SCRIPTS / "human_eval.py"), "packet",
              "--dataset", str(cls.dataset), "--reviewers", "R01", "R02",
              "--out", str(cls.handouts)])

        # 2) 模拟两人填表：把待填 json 填上维度分后放进 annotations/
        cls.annotations = TMP / "annotations"
        cls.annotations.mkdir()
        for reviewer, which in (("R01", "r01"), ("R02", "r02")):
            blank_path = cls.handouts / reviewer / f"{reviewer}-待填.json"
            payload = json.loads(blank_path.read_text(encoding="utf-8"))
            payload["completed_at"] = "2026-10-29"
            for item in payload["samples"].values():
                item["dimensions"] = _dimensions(which)
                item["verdict"] = "borderline"
                # 待填表里这三项目标值都是 None，是留给标注者填的必填项；
                # 只填维度分不填它们，validate 会整份拒收（这正是链路测试该抓的）
                item["gate_hr"] = True
                item["gate_interview"] = True
                item["confidence"] = 4
                item["notes"] = ""
            (cls.annotations / f"{reviewer}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(TMP, ignore_errors=True)

    # ---- 交接点 1：packet 出的待填表能被原样填完后通过 validate ----

    def test_packet_handout_shape_matches_annotation_schema(self):
        for reviewer in ("R01", "R02"):
            payload = json.loads(
                (self.handouts / reviewer / f"{reviewer}-待填.json").read_text(encoding="utf-8"))
            self.assertEqual(payload["schema_version"], "human-eval-1")
            self.assertEqual(payload["dataset_id"], "flow-e2e")
            self.assertEqual(payload["reviewer_type"], "independent_human")
            self.assertIs(payload["blind_to_model"], True)
            self.assertEqual(len(payload["samples"]), 6)
            first = next(iter(payload["samples"].values()))
            self.assertEqual(
                sorted(first["dimensions"]),
                ["competitiveness", "evidence_quality", "impact", "role_fit", "structure"])

    def test_validate_accepts_filled_annotations(self):
        proc = _run([str(SCRIPTS / "human_eval.py"), "validate",
                     "--dataset", str(self.dataset), "--annotations", str(self.annotations)])
        self.assertIn("被拒绝：0", proc.stdout + proc.stderr)

    # ---- 交接点 2：status 认出「人填了」 ----

    def test_status_reports_pilot_ready(self):
        # 未到正式门槛时 status 退出码 1 是设计（发布脚本要能拦住），这里只看输出
        proc = _run([str(SCRIPTS / "human_eval.py"), "status",
                     "--dataset", str(self.dataset), "--annotations", str(self.annotations)], expect=1)
        self.assertIn("pilot：就绪", proc.stdout, proc.stdout)
        self.assertNotIn("0 人 / 0 份", proc.stdout)
        self.assertIn("2 人 / 6 份", proc.stdout)

    # ---- 交接点 3：pilot 出报告，且措辞不越界 ----

    def test_benchmark_pilot_ready(self):
        out = TMP / "bench"
        proc = _run([str(SCRIPTS / "benchmark.py"), "--pilot",
                     "--dataset", str(self.dataset), "--annotations", str(self.annotations),
                     "--out", str(out / "report.md"), "--json", str(out / "report.json")])
        self.assertIn("PILOT_READY", proc.stdout, proc.stdout)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("不回答「模型准不准」", report)
        self.assertIn("不得对外引用", report)
        self.assertIn("κ", report)
        data = json.loads((out / "report.json").read_text(encoding="utf-8"))
        self.assertTrue(data["readiness"]["pilot"])
        self.assertEqual(data["readiness"]["evaluated_samples"], 6)
        # 这份合成数据里两人四档结论全是 borderline（单类），κ 按口径应为 None 而不是 0；
        # 真正要钉的是「pilot 为真 + 有配对 + 指标字段齐全」
        for key in ("mae", "kappa_verdict", "qwk", "within10"):
            self.assertIn(key, data["metrics"])

    # ---- 交接点 4：同一批数据，正式模式不许变绿 ----

    def test_formal_mode_still_not_ready_on_same_data(self):
        out = TMP / "bench-formal"
        proc = _run([str(SCRIPTS / "benchmark.py"),
                     "--dataset", str(self.dataset), "--annotations", str(self.annotations),
                     "--out", str(out / "report.md"), "--json", str(out / "report.json")])
        self.assertIn("NOT_READY", proc.stdout, proc.stdout)
        report = (out / "report.md").read_text(encoding="utf-8")
        self.assertIn("旧 MAE=8.0 不得继承", report)

    def test_require_ready_exits_2_even_with_pilot_ready(self):
        _run([str(SCRIPTS / "benchmark.py"), "--require-ready",
              "--dataset", str(self.dataset), "--annotations", str(self.annotations),
              "--out", str(TMP / "bench-strict" / "report.md"), "--json", str(TMP / "bench-strict" / "report.json")], expect=2)

    # ---- 交接点 5：分歧超阈值时仲裁清单能列出来（且不影响 pilot）----

    def test_arbitration_lists_nothing_when_they_agree(self):
        proc = _run([str(SCRIPTS / "human_eval.py"), "arbitration",
                     "--dataset", str(self.dataset), "--annotations", str(self.annotations)])
        self.assertIn("阈值", proc.stdout + proc.stderr)

    def test_arbitration_flags_big_spread(self):
        loud = json.loads((self.annotations / "R02.json").read_text(encoding="utf-8"))
        for item in loud["samples"].values():
            item["dimensions"] = {k: 1 for k in DIMS_R02}
        tmp_ann = TMP / "annotations_split"
        tmp_ann.mkdir(exist_ok=True)
        (tmp_ann / "R01.json").write_text(
            (self.annotations / "R01.json").read_text(encoding="utf-8"), encoding="utf-8")
        (tmp_ann / "R02.json").write_text(
            json.dumps(loud, ensure_ascii=False, indent=2), encoding="utf-8")
        out = TMP / "arb.md"
        proc = _run([str(SCRIPTS / "human_eval.py"), "arbitration",
                     "--dataset", str(self.dataset), "--annotations", str(tmp_ann),
                     "--out", str(out)])
        text = out.read_text(encoding="utf-8") if out.exists() else proc.stdout
        self.assertIn("F01", text.replace("X01", "F01"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
