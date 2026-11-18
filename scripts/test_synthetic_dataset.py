"""合成样本与数据集规模：让「切分护栏」在真实规模上跑起来，但**不许**让分数变好看。

为什么单独一套：
    数据集的机械门槛是「60 份、其中 18 份留出集」。真实简历只有 8 份时，
    `dataset_build.py` 的去重、同组同侧、已标注样本冻结这些护栏一次都没在规模上跑过，
    `benchmark.py` 也永远只看到 8 份——于是「到底哪一步卡住」说不清。

    用合成样本补足规模可以解决「跑不起来」，但会引入一条更隐蔽的错：
    **合成样本被当成真实标注，把 NOT_READY 刷成 READY。** 这套测试钉的就是这条线：

      1. 合成样本必须自带标记（正文首行 + dataset 的 provenance/data_sensitivity）；
      2. 真实简历数单独统计，合成样本不计入；
      3. 给合成样本编标注，READY 仍然是 False；
      4. 机械门槛（60/18）过了之后，剩下的理由只能是人（标注者）和标签（标注数）。

用法：
    python scripts/test_synthetic_dataset.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import benchmark  # noqa: E402
import dataset_build  # noqa: E402
import make_synthetic_samples as gen  # noqa: E402
import rubric  # noqa: E402


class GeneratorTest(unittest.TestCase):
    """生成器本身：确定、可校验、每份都带标记。"""

    def test_same_seed_same_bytes(self):
        a = gen.build(12, 7)
        b = gen.build(12, 7)
        self.assertEqual(a, b)
        self.assertEqual(gen.digest(a), gen.digest(b))

    def test_different_seed_differs(self):
        self.assertNotEqual(gen.digest(gen.build(12, 7)), gen.digest(gen.build(12, 8)))

    def test_count_matches_request(self):
        self.assertEqual(len(gen.build(54)), 54)
        self.assertEqual(len(gen.build(1)), 1)

    def test_every_sample_declares_itself_synthetic(self):
        for name, text in gen.build(54).items():
            self.assertTrue(text.startswith(gen.MARKER), name)
            self.assertIn("合成", name)

    def test_names_are_unique(self):
        names = list(gen.build(54))
        self.assertEqual(len(names), len(set(names)))

    def test_covers_all_families_and_bands(self):
        texts = gen.build(54)
        families = {name.split("-")[1] for name in texts}
        self.assertEqual(families, set(gen.FAMILIES))
        bands = {name.rsplit("-", 1)[-1].replace(".txt", "") for name in texts}
        self.assertEqual(bands, {"强", "中", "弱"})

    def test_weak_samples_have_no_numbers_in_bullets(self):
        """弱档刻意不给数字——它对应「全是负责/参与」的低分档。"""
        texts = gen.build(54)
        weak = [(n, t) for n, t in texts.items() if n.endswith("-弱.txt")]
        self.assertTrue(weak)
        for name, text in weak:
            body = [ln for ln in text.splitlines() if ln.startswith("- ")]
            self.assertTrue(body, name)
            for line in body:
                self.assertIsNone(
                    __import__("re").search(r"\d+(?:\.\d+)?\s*(?:%|％|万|倍|分钟|小时|ms|天)", line),
                    f"{name} 的弱档条目里出现了数字：{line}",
                )

    def test_manifest_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "synthetic"
            written, total = gen.write(out, 10, 3)
            self.assertEqual((written, total), (10, 10))
            manifest = json.loads((out / "MANIFEST.json").read_text(encoding="utf-8"))
            self.assertTrue(manifest["synthetic"])
            self.assertEqual(manifest["count"], 10)
            self.assertEqual(manifest["digest"], gen.digest(gen.build(10, 3)))
            self.assertEqual(gen.check(out, 10, 3), [])

    def test_check_catches_hand_edit(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "synthetic"
            gen.write(out, 5, 1)
            target = sorted(out.glob("*.txt"))[0]
            target.write_text("被手改过\n", encoding="utf-8")
            problems = gen.check(out, 5, 1)
            self.assertTrue(any("不一致" in p for p in problems), problems)

    def test_check_catches_extra_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "synthetic"
            gen.write(out, 3, 1)
            (out / "999-手塞的.txt").write_text("x\n", encoding="utf-8")
            self.assertTrue(any("多出" in p for p in gen.check(out, 3, 1)))


class ProvenanceTest(unittest.TestCase):
    """扫描 → 入库：合成身份必须一路带下去，不能在中途丢掉。"""

    def test_scan_marks_synthetic_by_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            synth = Path(tmp) / "synthetic"
            real = Path(tmp) / "raw"
            synth.mkdir()
            real.mkdir()
            text = gen.build(1, 1)
            (synth / "51-后端开发-合成-强.txt").write_text(list(text.values())[0], encoding="utf-8")
            (real / "01-真实简历.txt").write_text("真实简历正文" * 40, encoding="utf-8")
            entries, skipped = dataset_build.scan(
                [synth / "51-后端开发-合成-强.txt", real / "01-真实简历.txt"])
            self.assertEqual(skipped, [])
            got = {e.path.name: e.provenance for e in entries}
            self.assertEqual(got["51-后端开发-合成-强.txt"], "synthetic")
            self.assertEqual(got["01-真实简历.txt"], "real")

    def test_plan_records_provenance_and_sensitivity(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic" / "51-后端开发-合成-强.txt"
            path.parent.mkdir(parents=True)
            path.write_text(list(gen.build(1, 1).values())[0], encoding="utf-8")
            entries, _ = dataset_build.scan([path])
            plan = dataset_build.plan(entries, {"samples": []}, set(), heldout_every=3)
            sample = plan.samples[0]
            self.assertEqual(sample["provenance"], "synthetic")
            self.assertEqual(sample["data_sensitivity"], "synthetic_fixture")
            self.assertIn(sample["split"], {"development", "heldout"})

    def test_merge_counts_synthetic_and_real_separately(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "synthetic" / "51-后端开发-合成-强.txt"
            path.parent.mkdir(parents=True)
            path.write_text(list(gen.build(1, 1).values())[0], encoding="utf-8")
            entries, _ = dataset_build.scan([path])
            plan = dataset_build.plan(entries, {"samples": []}, set(), heldout_every=3)
            merged = dataset_build.merge_into({"samples": []}, plan, {})
            self.assertEqual(merged["counts"]["synthetic"], 1)
            self.assertEqual(merged["counts"]["real"], 0)


class ReadinessTest(unittest.TestCase):
    """门槛：合成样本能过「规模」，不能过「可信」。"""

    def _dataset(self, n_real: int = 8, n_synth: int = 54, heldout: int = 18) -> dict:
        samples = []
        for i in range(1, n_real + 1):
            samples.append({"sample_id": f"S{i:03d}", "split": "development",
                            "provenance": "real"})
        for i in range(1, n_synth + 1):
            samples.append({"sample_id": f"S{n_real + i:03d}",
                            "split": "heldout" if i <= heldout else "development",
                            "provenance": "synthetic"})
        return {"dataset_id": "t", "samples": samples, "targets": {}}

    def test_size_gates_pass_with_synthetic(self):
        ds = self._dataset()
        rep = benchmark.readiness(ds, [], [], [], allow_heldout=False)
        joined = " ".join(rep["reasons"])
        self.assertNotIn("数据集 62 份 < 目标", joined)
        self.assertNotIn("heldout 18 份 < 目标", joined)
        self.assertEqual(rep["dataset_size"], 62)
        self.assertEqual(rep["heldout_size"], 18)

    def test_real_samples_still_short(self):
        ds = self._dataset()
        rep = benchmark.readiness(ds, [], [], [], allow_heldout=False)
        self.assertEqual(rep["real_size"], 8)
        self.assertEqual(rep["synthetic_size"], 54)
        self.assertTrue(any("真实简历 8 份 < 目标 60 份" in r for r in rep["reasons"]), rep["reasons"])
        self.assertTrue(any("合成样本" in r for r in rep["reasons"]), rep["reasons"])

    def test_synthetic_annotations_never_count(self):
        """给 54 份合成样本都编上标注，READY 也必须还是 False。"""
        ds = self._dataset()
        fake = {"reviewer_id": "ghost", "samples": {
            f"S{i:03d}": {"dimensions": {"role_fit": 4}} for i in range(9, 63)}}
        rep = benchmark.readiness(ds, [fake, {**fake, "reviewer_id": "ghost2"}],
                                 [], [], allow_heldout=True)
        self.assertFalse(rep["ready"], rep)
        self.assertEqual(rep["real_annotated"], 0)
        self.assertTrue(any("真实简历里已标注 0 份" in r for r in rep["reasons"]), rep["reasons"])

    def test_only_human_and_labels_remain(self):
        """机械门槛过了以后，剩下的理由只许是「人」和「标签」和「留出集未验收」。"""
        ds = self._dataset()
        rep = benchmark.readiness(ds, [], [], [], allow_heldout=False)
        allowed = ("真实简历 8 份", "独立标注者", "真实简历里已标注", "heldout 尚未做最终验收")
        for r in rep["reasons"]:
            self.assertTrue(r.startswith(allowed), f"出现了不该有的阻断理由：{r}")


class ShippedDatasetTest(unittest.TestCase):
    """仓库里那份 dataset-v2 的硬数字，防止以后悄悄改动。"""

    def setUp(self):
        self.path = ROOT / "samples" / "eval" / "dataset-v2.json"
        if not self.path.exists():  # pragma: no cover
            self.skipTest("dataset-v2.json 不存在")
        self.dataset = json.loads(self.path.read_text(encoding="utf-8"))

    def test_scale_and_split(self):
        samples = self.dataset["samples"]
        self.assertGreaterEqual(len(samples), 60)
        heldout = [s for s in samples if s.get("split") == "heldout"]
        self.assertGreaterEqual(len(heldout), int(rubric.num("eval.heldout_target")))

    def test_every_synthetic_is_marked(self):
        for s in self.dataset["samples"]:
            if s.get("provenance") == "synthetic":
                self.assertEqual(s.get("data_sensitivity"), "synthetic_fixture")
                text = (ROOT / s["text_file"]).read_text(encoding="utf-8")
                self.assertTrue(text.startswith(gen.MARKER), s["sample_id"])

    def test_verify_passes(self):
        warns: list[str] = []
        problems = dataset_build.verify(self.dataset, set(), warns)
        self.assertEqual(problems, [])

    def test_benchmark_says_not_ready_with_only_human_reasons(self):
        rep = benchmark.readiness(self.dataset, [], [], [], allow_heldout=False)
        self.assertFalse(rep["ready"])
        self.assertEqual(rep["real_size"], 8)
        self.assertTrue(all("合成" in r or "真实" in r or "标注" in r or "heldout" in r
                            for r in rep["reasons"]), rep["reasons"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
