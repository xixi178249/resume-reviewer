"""隐私边界回归：页面图默认不发送、必须知情同意、临时文件可清理、历史可删除。

运行：
    python scripts/test_privacy.py -v
"""
from __future__ import annotations

import base64
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import ingest  # noqa: E402
import quality_gate  # noqa: E402
import server  # noqa: E402

# 用库里跟踪的样例，不用 samples/txt/ —— 真实简历不入库，
# CI 全新检出时那个文件不存在，会让整条流水线在 import 阶段就红。
RESUME = (ROOT / "samples" / "demo" / "04-Agent算法-强简历.txt").read_text(encoding="utf-8")
BASE = json.loads(
    (ROOT / "prompts" / "example-output-v1.7-good.json").read_text(encoding="utf-8")
)


def _fitz():
    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # PyMuPDF 1.x
    return fitz


def make_text_pdf(path: Path) -> None:
    fitz = _fitz()
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text(
        (50, 72),
        "张三 求职意向：后端开发实习生\n教育经历 2022.09-2026.06 某大学 计算机科学与技术\n"
        "项目经历 参与订单系统开发，负责接口与数据库设计，完成课程验收。\n" * 10,
        fontname="china-s",
        fontsize=10,
    )
    doc.save(path)
    doc.close()


def make_blank_pdf(path: Path) -> None:
    fitz = _fitz()
    doc = fitz.open()
    page = doc.new_page()
    page.draw_rect(fitz.Rect(0, 0, 595, 842), color=None, fill=(1, 1, 1))
    doc.save(path)
    doc.close()


class IngestPrivacyTest(unittest.TestCase):
    def test_text_pdf_does_not_render_pages_without_consent(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "text.pdf"
            make_text_pdf(pdf)
            _, no_pages = ingest._pdf_text_and_pages(pdf, include_pages=False)
            self.assertEqual(no_pages, [])
            got = ingest.load(pdf, ocr=False, include_images=False)
            self.assertEqual(got.kind, "text-pdf")
            self.assertEqual(got.images, [])

    def test_scanned_pdf_without_consent_never_calls_model(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "scan.pdf"
            make_blank_pdf(pdf)
            with mock.patch.object(ingest, "transcribe") as transcribe:
                with self.assertRaises(ingest.ImageConsentRequired):
                    ingest.load(pdf, include_images=False)
                transcribe.assert_not_called()

    def test_image_without_consent_never_calls_model(self) -> None:
        from PIL import Image

        with tempfile.TemporaryDirectory() as td:
            png = Path(td) / "resume.png"
            Image.new("RGB", (12, 12), "white").save(png)
            with mock.patch.object(ingest, "transcribe") as transcribe:
                with self.assertRaises(ingest.ImageConsentRequired):
                    ingest.load(png, include_images=False)
                transcribe.assert_not_called()


class ServerPrivacyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.reports = Path(self.tmp.name) / "reports"
        self.reports.mkdir()
        self.patches = [
            mock.patch.object(server, "REPORTS", self.reports),
            mock.patch.object(server, "HISTORY", self.reports / ".history.json"),
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

    def _extract_bytes(self, name: str, data: bytes, allow: bool) -> dict:
        return server.api_extract({
            "name": name,
            "b64": base64.b64encode(data).decode(),
            "allow_images": allow,
        })

    def test_image_without_consent_returns_structured_error(self) -> None:
        before = set(Path(tempfile.gettempdir()).glob("resume-reviewer-*"))
        res = self._extract_bytes("resume.png", b"\x89PNG\r\n\x1a\nbad", False)
        after = set(Path(tempfile.gettempdir()).glob("resume-reviewer-*"))
        self.assertFalse(res["ok"])
        self.assertEqual(res["code"], "IMAGE_CONSENT_REQUIRED")
        self.assertEqual(after - before, set(), "上传临时目录没有清理")

    def test_invalid_file_is_cleaned_up(self) -> None:
        before = set(Path(tempfile.gettempdir()).glob("resume-reviewer-*"))
        res = self._extract_bytes("resume.pdf", b"not a pdf at all", True)
        after = set(Path(tempfile.gettempdir()).glob("resume-reviewer-*"))
        self.assertFalse(res["ok"])
        self.assertEqual(after - before, set(), "解析异常后上传临时目录没有清理")

    def test_text_pdf_without_consent_has_no_page_images(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "resume.pdf"
            make_text_pdf(pdf)
            res = self._extract_bytes("resume.pdf", pdf.read_bytes(), False)
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["pages"], 0)
        self.assertFalse(res["visual_available"])
        sid = res["session_id"]
        self.sids.append(sid)
        self.assertEqual(server.SESSIONS[sid]["images"], [])

    def test_text_pdf_with_consent_keeps_pages_for_optional_visual(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            pdf = Path(td) / "resume.pdf"
            make_text_pdf(pdf)
            res = self._extract_bytes("resume.pdf", pdf.read_bytes(), True)
        self.assertTrue(res["ok"], res)
        self.assertFalse(res["ocr_used"])
        self.assertGreaterEqual(res["pages"], 1)
        self.assertTrue(res["visual_available"])
        self.sids.append(res["session_id"])

    def test_image_with_consent_still_works(self) -> None:
        from io import BytesIO
        from PIL import Image

        buf = BytesIO()
        Image.new("RGB", (40, 40), "white").save(buf, format="PNG")
        with mock.patch.object(ingest, "transcribe", return_value=RESUME):
            res = self._extract_bytes("resume.png", buf.getvalue(), True)
        self.assertTrue(res["ok"], res)
        self.assertTrue(res["ocr_used"])
        self.assertEqual(res["pages"], 1)
        self.sids.append(res["session_id"])
        self.assertEqual(len(server.SESSIONS[res["session_id"]]["images"]), 1)

    def test_review_requires_both_visual_flag_and_consent(self) -> None:
        sid = "privacy-consent"
        self.sids.append(sid)
        server.SESSIONS[sid] = {
            "text": RESUME,
            "images": [b"fake-png"],
            "images_allowed": True,
            "name": "privacy-test",
            "kind": "text-pdf",
            "notes": [],
            "ts": time.time(),
        }
        quality = quality_gate.evaluate(BASE, RESUME)
        captured: list[list[bytes]] = []

        def fake_run(prompt, images, n=1, dump=None, resume=""):
            captured.append(list(images))
            return quality["doc"], [quality["doc"]["overall_score"]], quality

        with mock.patch.object(server, "MOCK", False), \
                mock.patch.object(server.review_mod, "run_reviews", side_effect=fake_run):
            res1 = server.api_review({"session_id": sid, "visual": False})
            res2 = server.api_review({"session_id": sid, "visual": True})
        self.assertTrue(res1["ok"], res1)
        self.assertEqual(captured[0], [])
        self.assertFalse(res1["visual_used"])
        self.assertTrue(res2["ok"], res2)
        self.assertEqual(captured[1], [b"fake-png"])
        self.assertTrue(res2["visual_used"])

    def test_review_ignores_visual_when_session_not_allowed(self) -> None:
        sid = "privacy-no-consent"
        self.sids.append(sid)
        server.SESSIONS[sid] = {
            "text": RESUME,
            "images": [b"fake-png"],
            "images_allowed": False,
            "name": "privacy-test",
            "kind": "text-pdf",
            "notes": [],
            "ts": time.time(),
        }
        quality = quality_gate.evaluate(BASE, RESUME)
        captured: list[list[bytes]] = []

        def fake_run(prompt, images, n=1, dump=None, resume=""):
            captured.append(list(images))
            return quality["doc"], [quality["doc"]["overall_score"]], quality

        with mock.patch.object(server, "MOCK", False), \
                mock.patch.object(server.review_mod, "run_reviews", side_effect=fake_run):
            res = server.api_review({"session_id": sid, "visual": True})
        self.assertTrue(res["ok"], res)
        self.assertEqual(captured, [[]])
        self.assertFalse(res["visual_used"])

    def test_expired_session_is_cleared(self) -> None:
        sid = "privacy-expired"
        images = [b"fake-png"]
        server.SESSIONS[sid] = {
            "text": "敏感文本",
            "images": images,
            "images_allowed": True,
            "name": "expired",
            "kind": "image",
            "notes": [],
            "ts": time.time() - server.SESSION_TTL - 1,
        }
        server.gc_sessions()
        self.assertNotIn(sid, server.SESSIONS)
        self.assertEqual(images, [])

    def test_delete_report_removes_files_and_history(self) -> None:
        stem = "my.report.v1"
        rid = "a" * 32
        for ext in (".md", ".json", ".txt", ".docx", ".raw.txt"):
            (self.reports / f"{stem}{ext}").write_text("x", encoding="utf-8")
        (self.reports / ".history.json").write_text(json.dumps({
            "version": 2,
            "reviews": [
                {"review_id": rid, "name": stem, "file_stem": stem, "score": 70},
                {"review_id": "b" * 32, "name": "keep", "file_stem": "keep", "score": 80},
            ],
        }), encoding="utf-8")
        res = server.api_delete_report(rid)
        self.assertTrue(res["ok"], res)
        self.assertEqual(list(self.reports.glob(f"{stem}.*")), [])
        data = json.loads((self.reports / ".history.json").read_text(encoding="utf-8"))
        ids = {r["review_id"] for r in data["reviews"]}
        self.assertNotIn(rid, ids)
        self.assertIn("b" * 32, ids)

    def test_delete_rejects_path_traversal(self) -> None:
        for bad in ("../outside", ".history", ".."):
            res = server.api_delete_report(bad)
            self.assertFalse(res["ok"], bad)
            self.assertEqual(res.get("_status"), 400, bad)


if __name__ == "__main__":
    unittest.main(verbosity=2)
