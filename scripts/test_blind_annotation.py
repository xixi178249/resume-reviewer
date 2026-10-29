"""盲评发放包与仲裁清单的测试（v2.0 A 路线第③项的软件侧）。

守的是一条线：**标注者能看到的东西，决定了他给的分数有没有意义**。
- 发放包里出现模型分数 → 不是盲评，κ 没有意义；
- 两人分差 20 分被悄悄平均掉 → 分歧被藏起来，MAE 会好看但没用。
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
import rubric  # noqa: E402

DIM_KEYS = ["role_fit", "impact", "evidence_quality", "structure", "competitiveness"]


def _write_resume(tmp: Path, name: str, body: str) -> Path:
    p = tmp / name
    p.write_text(body, encoding="utf-8")
    return p


def _make_dataset(tmp: Path, n: int = 6, missing: int = 0) -> Path:
    samples = []
    for i in range(1, n + 1):
        rel = _write_resume(
            tmp, f"{i:02d}.txt",
            f"候选{i}：负责后端接口开发，把响应时间从 {100 + i}ms 降到 {50 + i}ms，"
            f"覆盖 {i * 3} 个接口，线上零回滚。",
        )
        samples.append({
            "sample_id": f"S{i:02d}",
            "blind_id": f"ANON-T{i:03d}",
            "source_group": f"group-{i}",
            "text_file": str(rel) if i > missing else str(tmp / "not-there.txt"),
            "model_output": f"outputs/{i:02d}.json",
            "job_target": "后端开发实习生",
            "jd": "",
            "stratum": {"role_family": "backend", "quality_band_legacy": "low"},
            "split": "development",
        })
    dataset = {
        "schema_version": "eval-1",
        "dataset_id": "test-dataset",
        "split_policy": {},
        "samples": samples,
    }
    path = tmp / "dataset.json"
    path.write_text(json.dumps(dataset, ensure_ascii=False), encoding="utf-8")
    return path


def _annotation(reviewer: str, sid: str, value: int) -> dict:
    return {
        "schema_version": "human-eval-1",
        "reviewer_id": reviewer,
        "blind_to_model": True,
        "samples": {
            sid: {
                "blind_id": "ANON-T000",
                "dimensions": {k: value for k in DIM_KEYS},
                "verdict": "pass",
                "gate_hr": True,
                "gate_interview": True,
            }
        },
    }


class BlindPackageTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dataset_path = _make_dataset(self.tmp)
        self.out = self.tmp / "handouts"

    def tearDown(self):
        self._tmp.cleanup()

    def _run(self, reviewer="R01", **kw):
        return he._blind_package(self.dataset_path, reviewer, self.out, **kw)

    def test_handout_shows_blind_ids_only(self):
        result = self._run()
        md = Path(result["markdown"]).read_text(encoding="utf-8")
        for i in range(1, 7):
            self.assertIn(f"ANON-T{i:03d}", md)
        # 内部编号不进手读稿：看到 S03 就知道它是第几份，等于按顺序猜分
        for i in range(1, 7):
            self.assertNotIn(f"## S{i:02d}", md)

    def test_handout_never_leaks_model_output(self):
        result = self._run()
        md = Path(result["markdown"]).read_text(encoding="utf-8")
        self.assertNotIn("outputs/", md)
        for word in ("overall_score", "model_output", "quality_band"):
            self.assertNotIn(word, md)

    def test_blank_json_follows_handout_order(self):
        result = self._run()
        md = Path(result["markdown"]).read_text(encoding="utf-8")
        payload = json.loads(Path(result["blank"]).read_text(encoding="utf-8"))
        dataset = json.loads(self.dataset_path.read_text(encoding="utf-8"))
        blind_to_sid = {s["blind_id"]: s["sample_id"] for s in dataset["samples"]}
        md_order = [blind_to_sid[b] for b in
                    __import__("re").findall(r"^## (ANON-T\d{3})$", md, __import__("re").M)]
        self.assertEqual(md_order, list(payload["samples"]))
        self.assertTrue(payload["blind_to_model"])

    def test_order_is_stable_per_reviewer(self):
        a = he._blind_order(he.load_dataset(self.dataset_path), "R01")
        b = he._blind_order(he.load_dataset(self.dataset_path), "R01")
        self.assertEqual([s["sample_id"] for s in a], [s["sample_id"] for s in b])

    def test_two_reviewers_get_different_orders(self):
        ds = he.load_dataset(self.dataset_path)
        r1 = [s["sample_id"] for s in he._blind_order(ds, "R01")]
        r2 = [s["sample_id"] for s in he._blind_order(ds, "R02")]
        self.assertNotEqual(r1, r2)

    def test_handout_carries_anchor_table(self):
        # 只给维度名、不给锚点，两个人的「3 分」能差 20 分；锚点必须随稿一起发
        md = Path(self._run()["markdown"]).read_text(encoding="utf-8")
        self.assertIn("打分锚点（摘要", md)
        for dim in ("role_fit", "impact", "evidence_quality", "structure", "competitiveness"):
            self.assertIn(dim, md)
        self.assertIn("60 字", md)
        self.assertIn("不写改进建议", md)

    def test_anchor_card_exists_where_handout_points(self):
        # 稿子里写了「完整口径见 docs/盲评锚点卡.md」，那份文没了就等于骗人
        card = ROOT / "docs" / "盲评锚点卡.md"
        self.assertTrue(card.exists(), "docs/盲评锚点卡.md 不存在")
        body = card.read_text(encoding="utf-8")
        self.assertIn("不要因为排版好看给高分", body)
        for dim in ("role_fit", "impact", "evidence_quality", "structure", "competitiveness"):
            self.assertIn(dim, body)

    def test_missing_text_is_skipped_loudly(self):
        dataset_path = _make_dataset(self.tmp, n=6, missing=2)
        result = he._blind_package(dataset_path, "R01", self.out)
        # 顺序按盲评随机序，不是样本号序；这里只关心「都报出来了」
        self.assertEqual(sorted(result["skipped"]), ["S01", "S02"])
        self.assertEqual(result["samples"], 4)
        payload = json.loads(Path(result["blank"]).read_text(encoding="utf-8"))
        self.assertNotIn("S01", payload["samples"])
        self.assertIn("S01", Path(result["markdown"]).read_text(encoding="utf-8"))

    def test_limit_caps_samples(self):
        result = self._run(limit=3)
        self.assertEqual(result["samples"], 3)


class ArbitrationTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.dataset = json.loads(_make_dataset(self.tmp).read_text(encoding="utf-8"))

    def tearDown(self):
        self._tmp.cleanup()

    def test_large_spread_needs_arbitration(self):
        anns = [_annotation("R01", "S01", 1), _annotation("R02", "S01", 5)]
        result = he._arbitration(anns, self.dataset)
        self.assertEqual(len(result["needs_arbitration"]), 1)
        row = result["needs_arbitration"][0]
        self.assertGreater(row["spread"], result["threshold"])
        self.assertEqual(sorted(row["scores"]), ["R01", "R02"])
        # 「差在哪」必须能指到具体维度，否则只能知道有分差、不知道该对齐哪条
        self.assertTrue(row["dim_spread"])
        self.assertEqual(sorted(row["dim_spread"]["role_fit"]), ["R01", "R02"])

    def test_small_spread_is_agreed(self):
        # 单维差 1 档的代价：role_fit(25%)=5 分、impact(25%)=5 分 → 两维各差 1 档 = 10 分
        anns = [
            _annotation("R01", "S02", 3),
            _annotation("R02", "S02", 3),
        ]
        anns[1]["samples"]["S02"]["dimensions"]["role_fit"] = 4
        anns[1]["samples"]["S02"]["dimensions"]["impact"] = 4
        result = he._arbitration(anns, self.dataset)
        self.assertEqual(result["needs_arbitration"], [])
        self.assertEqual([r["sample_id"] for r in result["agreed"]], ["S02"])
        self.assertEqual(result["agreed"][0]["spread"], 10)

    def test_one_notch_on_all_dims_is_a_conflict(self):
        # 五个维度各差 1 档 = 20 分 > 15：这已经是系统性理解差异，不该被平均掉
        anns = [_annotation("R01", "S05", 3), _annotation("R02", "S05", 4)]
        result = he._arbitration(anns, self.dataset)
        self.assertEqual(result["needs_arbitration"][0]["spread"], 20)

    def test_single_reviewer_listed_separately(self):
        result = he._arbitration([_annotation("R01", "S03", 3)], self.dataset)
        self.assertEqual([r["sample_id"] for r in result["single_reviewer"]], ["S03"])

    def test_never_scored_lists_expected_samples(self):
        result = he._arbitration([_annotation("R01", "S01", 3)], self.dataset)
        self.assertEqual(result["never_scored"], ["S02", "S03", "S04", "S05", "S06"])

    def test_threshold_comes_from_rubric(self):
        result = he._arbitration([], self.dataset)
        self.assertEqual(result["threshold"], int(rubric.num("eval.arbitration_threshold")))

    def test_markdown_renders_conflict_row(self):
        anns = [_annotation("R01", "S04", 1), _annotation("R02", "S04", 5)]
        md = he.render_arbitration_markdown(he._arbitration(anns, self.dataset))
        self.assertIn("需要第三人仲裁", md)
        self.assertIn("S04", md)
        self.assertIn("R01", md)


if __name__ == "__main__":
    unittest.main(verbosity=2)
