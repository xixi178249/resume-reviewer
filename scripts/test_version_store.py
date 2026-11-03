"""版本快照的契约测试：建档、追加、上限、撤回、路径安全。

为什么值得单独测：撤回一旦算错，用户会以为自己的简历被改坏了。
所以"撤回 = 回到 parent 版本"这条必须钉死。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import version_store as vs  # noqa: E402

V1 = "张三 / 产品经理实习\n教育：重庆城市科技学院\n实习：负责数据看板\n"
V2 = "张三 / 产品经理实习\n教育：重庆城市科技学院\n实习：负责核心数据看板，覆盖 3 个指标\n"


class VersionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = vs.VersionStore(Path(self.tmp.name) / "versions")

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_makes_v1(self):
        rec = self.store.create(V1)
        self.assertEqual(rec["version"], 1)
        self.assertIsNone(rec["parent"])
        self.assertEqual(rec["source"], "upload")
        self.assertEqual(len(rec["doc_id"]), 12)
        self.assertEqual(rec["chars"], len(V1))

    def test_create_same_text_reuses_chain(self):
        first = self.store.create(V1)
        again = self.store.create(V1)
        self.assertEqual(again["version"], 1)
        self.assertEqual(len(self.store.list(first["doc_id"])), 1)

    def test_empty_text_rejected(self):
        with self.assertRaises(vs.VersionError):
            self.store.create("   ")

    def test_append_makes_v2_with_parent(self):
        rec = self.store.create(V1)
        v2 = self.store.append(rec["doc_id"], V2, parent=1, applied_patch_ids=["p1", "p3"])
        self.assertEqual(v2["version"], 2)
        self.assertEqual(v2["parent"], 1)
        self.assertEqual(v2["applied_patch_ids"], ["p1", "p3"])
        self.assertEqual(v2["source"], "patch")

    def test_append_wrong_parent_rejected(self):
        rec = self.store.create(V1)
        with self.assertRaises(vs.VersionError):
            self.store.append(rec["doc_id"], V2, parent=5)

    def test_append_unknown_chain_rejected(self):
        with self.assertRaises(vs.VersionError):
            self.store.append("nope", V2, parent=1)

    def test_limit_enforced(self):
        rec = self.store.create(V1)
        for i in range(2, vs.MAX_VERSIONS + 1):
            self.store.append(rec["doc_id"], V1 + f"# {i}\n", parent=i - 1)
        self.assertEqual(len(self.store.list(rec["doc_id"])), vs.MAX_VERSIONS)
        with self.assertRaises(vs.VersionLimit):
            self.store.append(rec["doc_id"], V1 + "# over\n", parent=vs.MAX_VERSIONS)

    def test_get_returns_text(self):
        rec = self.store.create(V1)
        got = self.store.get(rec["doc_id"], 1)
        self.assertEqual(got["text"], V1)

    def test_latest(self):
        rec = self.store.create(V1)
        self.store.append(rec["doc_id"], V2, parent=1)
        self.assertEqual(self.store.latest(rec["doc_id"])["version"], 2)

    def test_chain_summary_fields(self):
        rec = self.store.create(V1)
        self.store.append(rec["doc_id"], V2, parent=1, applied_patch_ids=["p1"])
        rows = self.store.chain_summary(rec["doc_id"])
        self.assertEqual([r["version"] for r in rows], [1, 2])
        self.assertEqual(rows[1]["applied"], ["p1"])
        self.assertNotIn("text", rows[0])

    def test_update_meta_backfills_score(self):
        rec = self.store.create(V1)
        self.store.update_meta(rec["doc_id"], 1, score=62, verdict="borderline", review_id="r1")
        got = self.store.get(rec["doc_id"], 1)
        self.assertEqual(got["score"], 62)
        self.assertEqual(got["verdict"], "borderline")
        self.assertEqual(got["text"], V1)  # 回填元信息不能弄丢正文

    def test_undo_returns_parent(self):
        rec = self.store.create(V1)
        self.store.append(rec["doc_id"], V2, parent=1)
        back = self.store.undo(rec["doc_id"])
        self.assertEqual(back["version"], 1)
        self.assertEqual(back["text"], V1)

    def test_undo_on_first_version_rejected(self):
        rec = self.store.create(V1)
        with self.assertRaises(vs.VersionError):
            self.store.undo(rec["doc_id"])

    def test_delete_removes_chain(self):
        rec = self.store.create(V1)
        self.assertTrue(self.store.delete(rec["doc_id"]))
        self.assertEqual(self.store.list(rec["doc_id"]), [])
        self.assertFalse(self.store.delete(rec["doc_id"]))

    def test_doc_id_traversal_rejected(self):
        with self.assertRaises(vs.VersionError):
            self.store.list("../etc")

    def test_missing_version_rejected(self):
        rec = self.store.create(V1)
        with self.assertRaises(vs.VersionError):
            self.store.get(rec["doc_id"], 9)

    def test_doc_id_stable_across_whitespace_edges(self):
        a = vs.doc_id_for("abc")
        b = vs.doc_id_for("\n abc \n")
        self.assertEqual(a, b)


if __name__ == "__main__":
    unittest.main(verbosity=2)
