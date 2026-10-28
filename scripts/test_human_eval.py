"""阶段 D 评测工具测试，使用构造数据，不代表真实模型效果。

运行：
    python scripts/test_human_eval.py -v
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import human_eval as he  # noqa: E402


DIMS_60 = {"role_fit": 3, "impact": 3, "evidence_quality": 3, "structure": 3, "competitiveness": 3}
DIMS_50 = {"role_fit": 3, "impact": 3, "evidence_quality": 2, "structure": 2, "competitiveness": 2}


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def annotation(reviewer: str, overrides: dict | None = None) -> dict:
    samples = {
        "S01": {"blind_id": "B-S01", "job_target": "后端开发实习生",
                "dimensions": DIMS_60, "verdict": "borderline", "gate_hr": True,
                "gate_interview": False, "confidence": 4, "issues": []},
        "S02": {"blind_id": "B-S02", "job_target": "后端开发实习生",
                "dimensions": DIMS_50, "verdict": "reject", "gate_hr": False,
                "gate_interview": False, "confidence": 5, "issues": []},
        "S03": {"blind_id": "B-S03", "job_target": "产品经理实习生",
                "dimensions": {k: 4 for k in he.DIMS}, "verdict": "pass", "gate_hr": True,
                "gate_interview": True, "confidence": 4, "issues": []},
    }
    for sid, val in (overrides or {}).items():
        samples[sid].update(val)
    return {
        "schema_version": "human-eval-1",
        "dataset_id": "fixture-ds",
        "reviewer_id": reviewer,
        "reviewer_type": "independent_human",
        "blind_to_model": True,
        "completed_at": "2026-10-24T12:00:00",
        "samples": samples,
    }


class HumanEvalTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "texts").mkdir()
        self.dataset = {
            "schema_version": "human-eval-1",
            "dataset_id": "fixture-ds",
            "targets": {"total_samples": 3, "heldout_min": 1, "reviewers_per_sample": 2},
            "samples": [],
        }
        scores = [60, 70, 80]
        for i in range(3):
            sid = f"S0{i+1}"
            txt = self.root / "texts" / f"{sid}.txt"
            txt.write_text(f"{sid} 简历文本", encoding="utf-8")
            out = self.root / "outputs" / f"{sid}.json"
            verdict = "borderline" if i == 0 else ("pass" if i == 1 else "strong_pass")
            write_json(out, {
                "overall_score": scores[i],
                "verdict": verdict,
                "gate_hr": {"pass": i != 1},
                "gate_interview": {"pass": i == 2},
                "issues": [
                    {"dimension": "impact", "evidence": f"{sid} 原文证据", "severity": "high"}
                ],
            })
            self.dataset["samples"].append({
                "sample_id": sid,
                "blind_id": f"B{sid}",
                "source_group": f"g{i}",
                "text_file": str(txt),
                "model_output": str(out),
                "job_target": "后端开发实习生",
                "jd": "",
                "stratum": {"role_family": "backend" if i < 2 else "product", "input_type": "text"},
                "split": "heldout" if i == 2 else "development",
            })
        self.annotations = self.root / "annotations"
        self.annotations.mkdir()
        write_json(self.annotations / "R01.json", annotation("R01"))
        write_json(self.annotations / "R02.json", annotation("R02"))

        self.audits = self.root / "audits"
        write_json(self.audits / "A01.json", {
            "schema_version": "human-eval-audit-1",
            "dataset_id": "fixture-ds",
            "reviewer_id": "A01",
            "outputs": {
                "S01": {
                    "issues": [{"index": 0, "supported_by_source": True, "job_relevant": True,
                                "severity_appropriate": False, "actionable": True}],
                    "rewrites": [{"index": 0, "fact_preserved": True,
                                  "better_than_original": True, "usable_as_is": False}],
                }
            },
        })
        self.runs = self.root / "runs"
        for n, score in enumerate([60, 64, 56], 1):
            write_json(self.runs / f"S01.run{n}.json", {
                "overall_score": score,
                "verdict": "borderline" if score != 64 else "pass",
                "gate_hr": {"pass": True},
                "gate_interview": {"pass": False},
                "issues": [{"dimension": "impact", "evidence": "S01 原文证据", "severity": "high"}],
            })
        self.ocr = self.root / "ocr.json"
        write_json(self.ocr, {"pairs": [{
            "sample_id": "O1",
            "reference": "P95 从 420ms 降到 90ms",
            "recognized": "P95 从 420ms 降到 80m",
        }]})

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_dataset_valid(self) -> None:
        self.assertEqual(he.validate_dataset(self.dataset), [])

    def test_dataset_detects_cross_split_and_missing_file(self) -> None:
        ds = json.loads(json.dumps(self.dataset))
        ds["samples"][0]["split"] = "heldout"
        ds["samples"][0]["source_group"] = ds["samples"][1]["source_group"]
        ds["samples"][1]["text_file"] = str(self.root / "missing.txt")
        problems = he.validate_dataset(ds)
        self.assertTrue(any("跨开发/留出集" in x for x in problems))
        self.assertTrue(any("不存在" in x for x in problems))

    def test_invalid_annotations_are_rejected(self) -> None:
        bad = annotation("BAD")
        bad["reviewer_type"] = "model"
        write_json(self.annotations / "BAD.json", bad)
        valid, rejected = he.load_annotations(self.annotations, self.dataset)
        self.assertEqual(len(valid), 2)
        self.assertEqual(len(rejected), 1)

    def test_quadratic_weighted_kappa(self) -> None:
        self.assertEqual(
            he.quadratic_weighted_kappa([(1, 1), (2, 2), (3, 3), (4, 4), (5, 5)]), 1.0
        )
        self.assertLess(
            he.quadratic_weighted_kappa([(1, 5), (2, 4), (3, 3), (4, 2), (5, 1)]), 0
        )

    def test_agreement_and_scores(self) -> None:
        valid, _ = he.load_annotations(self.annotations, self.dataset)
        agree = he.agreement_metrics(valid)
        self.assertEqual(agree["reviewer_pairs"], 1)
        self.assertEqual(agree["samples_compared"], 3)
        self.assertEqual(agree["overall_mae"], 0.0)
        self.assertEqual(agree["verdict_exact_rate"], 1.0)
        metrics = he.score_metrics(self.dataset, valid)
        self.assertEqual(metrics["n"], 3)
        self.assertEqual(metrics["mae"], 6.7)
        self.assertEqual(metrics["rmse"], 11.5)
        self.assertEqual(metrics["spearman"], 0.5)
        self.assertEqual(metrics["mae_ci95"], [0.0, 20.0])

    def test_audit_stability_and_ocr(self) -> None:
        audit = he.audit_metrics(self.audits, self.dataset)
        self.assertEqual(audit["n_issues"], 1)
        self.assertEqual(audit["n_rewrites"], 1)
        self.assertEqual(audit["issue_rates"]["supported_by_source"]["rate"], 1.0)
        self.assertEqual(audit["issue_rates"]["severity_appropriate"]["rate"], 0.0)
        stab = he.stability_metrics(self.runs, self.dataset)
        self.assertEqual(stab["samples"], 1)
        self.assertEqual(stab["runs"], 3)
        self.assertGreater(stab["score_std_mean"], 0)
        self.assertEqual(stab["verdict_flip_rate"], 1.0)
        self.assertEqual(stab["gate_interview_flip_rate"], 0.0)
        ocr = he.ocr_metrics(self.ocr)
        self.assertEqual(ocr["pairs"], 1)
        self.assertGreater(ocr["cer"], 0)
        self.assertGreater(ocr["digit_error_rate"], 0)

    def test_readiness_and_report(self) -> None:
        valid, rejected = he.load_annotations(self.annotations, self.dataset)
        metrics = he.score_metrics(self.dataset, valid)
        ready = he.readiness(self.dataset, valid, rejected, metrics, [])
        self.assertEqual(ready["status"], "ready", ready)
        result, md = he.build_report(
            self.dataset_path(), self.annotations, self.audits, self.runs, self.ocr,
            legacy_gold=None,
        )
        self.assertEqual(result["readiness"]["status"], "ready")
        self.assertIn("MAE", md)
        self.assertIn("二次加权 Kappa", md)
        self.assertIn("档位翻转率", md)

    def dataset_path(self) -> Path:
        p = self.root / "dataset.json"
        write_json(p, self.dataset)
        return p

    def test_empty_annotation_directory_is_not_ready(self) -> None:
        empty = self.root / "empty"
        empty.mkdir()
        ds = he.load_dataset(self.dataset_path())
        valid, rejected = he.load_annotations(empty, ds)
        metrics = he.score_metrics(ds, valid)
        ready = he.readiness(ds, valid, rejected, metrics, [])
        self.assertEqual(ready["status"], "not_ready")
        self.assertEqual(metrics["n"], 0)

    def test_current_dataset_has_no_fake_heldout(self) -> None:
        ds = he.load_dataset(ROOT / "samples" / "eval" / "dataset-v1.json")
        self.assertTrue(all(s["split"] == "development" for s in ds["samples"]))

    # 真实简历永远不入库，CI 全新检出里没有 samples/txt/。
    # 所以"素材是否齐全"只在本地（真跑过评测的机器上）才检查，
    # 在 CI 里跳过 —— 否则整条流水线会因为一个隐私边界而永久变红。
    @unittest.skipUnless(
        (ROOT / "samples" / "txt").is_dir(),
        "没有 samples/txt（真实简历不入库），跳过素材存在性检查",
    )
    def test_current_dataset_resume_texts_exist(self) -> None:
        ds = he.load_dataset(ROOT / "samples" / "eval" / "dataset-v1.json")
        self.assertEqual(he.validate_dataset(ds), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
