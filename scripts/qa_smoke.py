"""按 PRD 验收项跑一遍冒烟。用法：

    # 便宜：mock 模式，不调模型，验接口契约 / 错误文案 / 导出 / 路径安全
    python scripts/qa_smoke.py --base http://127.0.0.1:8790 --mock

    # 真实：会调模型（约 20~60 秒、几次调用）
    python scripts/qa_smoke.py --base http://127.0.0.1:8790 --real --ocr

退出码：0 = 全过；1 = 有失败。
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS: list[tuple[bool, str, str]] = []


def ok(name: str, detail: str = "") -> None:
    RESULTS.append((True, name, detail))
    print(f"  PASS  {name}" + (f"  {detail}" if detail else ""))


def bad(name: str, detail: str = "") -> None:
    RESULTS.append((False, name, detail))
    print(f"  FAIL  {name}" + (f"  {detail}" if detail else ""))


def check(name: str, cond: bool, detail: str = "") -> bool:
    ok(name, detail) if cond else bad(name, detail)
    return cond


def post(base: str, path: str, body: dict, timeout: int = 600) -> tuple[int, dict]:
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


def get(base: str, path: str, timeout: int = 30) -> tuple[int, bytes, dict]:
    try:
        with urllib.request.urlopen(base + path, timeout=timeout) as r:
            return r.status, r.read(), dict(r.headers)
    except urllib.error.HTTPError as e:
        return e.code, e.read(), dict(e.headers)


def delete(base: str, path: str, timeout: int = 30) -> tuple[int, dict]:
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as e:
        raw = e.read().decode("utf-8", "ignore")
        try:
            return e.code, json.loads(raw)
        except json.JSONDecodeError:
            return e.code, {"raw": raw[:200]}


# ---------------------------------------------------------------- 本地解析（不用起服务）
def test_ingest_local() -> None:
    import tempfile

    sys.path.insert(0, str(ROOT / "scripts"))
    import ingest

    print("\n[0] 本地解析：编码 / 脱敏 / PDF 分流")
    tmp = Path(tempfile.mkdtemp())

    gbk = tmp / "gbk.txt"
    text = "张三 求职意向：后端开发实习生\n教育经历 2022.09-2026.06 某大学 计算机科学与技术"
    gbk.write_bytes(text.encode("gbk"))
    got = ingest.load(gbk, ocr=False).text
    check("GBK 文本不乱码", "后端开发实习生" in got and "计算机科学与技术" in got, got[:24].replace("\n", " / "))

    utf8 = tmp / "utf8.txt"
    utf8.write_bytes((text * 6).encode("utf-8-sig"))
    got2 = ingest.load(utf8, ocr=False).text
    check("UTF-8(带 BOM) 正常", "后端开发实习生" in got2)

    masked = ingest.desensitize(
        "手机 13812345678 邮箱 a@b.com 身份证 110101199001011234\n"
        "出生年月：1999年3月\n教育经历 2022.09-2026.06 某大学"
    )
    check("手机号已脱敏", "13812345678" not in masked and "手机号已脱敏" in masked)
    check("邮箱已脱敏", "a@b.com" not in masked)
    check("身份证已脱敏", "110101199001011234" not in masked)
    check("保留教育时间线", "2022.09-2026.06" in masked, "AC-2.5")

    try:
        try:  # PyMuPDF 2.x 起 fitz 别名弃用，优先用新模块名
            import pymupdf as fitz
        except ImportError:
            import fitz  # PyMuPDF 1.x
    except Exception:  # noqa: BLE001
        bad("PyMuPDF 不可用，跳过 PDF 用例")
        return

    pdf = tmp / "text.pdf"
    d = fitz.open()
    page = d.new_page()
    page.insert_text((50, 72), "张三 后端开发实习生\n教育经历 2022.09-2026.06 某大学\n" * 10,
                     fontname="china-s", fontsize=10)
    d.save(pdf)
    d.close()
    check("文字型 PDF 不触发 OCR", ingest.load(pdf, ocr=False).kind == "text-pdf", "AC-2.1")
    check("文字型 PDF 默认不渲染页面图", ingest.load(pdf, ocr=False, include_images=False).images == [], "隐私最小化")

    scan = tmp / "scan.pdf"
    d = fitz.open()
    p2 = d.new_page()
    p2.draw_rect(fitz.Rect(0, 0, 595, 842), color=None, fill=(1, 1, 1))
    d.save(scan)
    d.close()
    raw, pages = ingest._pdf_text_and_pages(scan)
    check("空白 PDF 判定为扫描件", len(raw.strip()) < 120 and len(pages) == 1, "AC-2.2")
    try:
        ingest.load(scan, include_images=False)
        check("扫描件未经同意直接拒绝", False, "没有抛 ImageConsentRequired")
    except ingest.ImageConsentRequired:
        ok("扫描件未经同意直接拒绝")


# ---------------------------------------------------------------- 契约（mock 可跑）
def test_contract(base: str, mock: bool) -> str | None:
    print("\n[1] 健康检查与输入校验")
    st, raw, _ = get(base, "/api/health")
    check("GET /api/health 200", st == 200, f"HTTP {st}")
    try:
        health = json.loads(raw)
        check("health 带 mock 标记", health.get("mock") is mock, str(health))
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        pkg = json.loads((ROOT / "package.json").read_text(encoding="utf-8"))
        lock = json.loads((ROOT / "package-lock.json").read_text(encoding="utf-8"))
        check("health 版本与 VERSION 一致", health.get("version") == version,
              f"{health.get('version')} vs {version}")
        check("package.json / lock 与 VERSION 一致",
              pkg.get("version") == version and (lock.get("packages") or {}).get("", {}).get("version") == version,
              f"pkg={pkg.get('version')} lock={(lock.get('packages') or {}).get('', {}).get('version')}")
    except Exception:  # noqa: BLE001
        bad("health 返回 JSON")

    st, r = post(base, "/api/extract", {"text": "太短了", "name": "qa-short"})
    check("过短文本被拒", st == 200 and not r.get("ok") and "空白页" in (r.get("error") or ""), r.get("error", "")[:40])

    st, r = post(base, "/api/extract", {})
    check("空 payload 被拒", not r.get("ok"), (r.get("error") or "")[:40])

    st, r = post(base, "/api/extract", {"name": "x.exe", "b64": base64.b64encode(b"MZ").decode()})
    check("不支持的格式被拒", not r.get("ok") and "不支持" in (r.get("error") or ""), (r.get("error") or "")[:40])

    st, r = post(base, "/api/extract", {
        "name": "consent.png",
        "b64": base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"not-a-real-image").decode(),
    })
    check("图片未经同意不发送", r.get("code") == "IMAGE_CONSENT_REQUIRED",
          f"{r.get('code')} {(r.get('error') or '')[:30]}")

    try:
        import pymupdf as fitz
    except ImportError:
        import fitz  # PyMuPDF 1.x
    d = fitz.open()
    page = d.new_page()
    page.insert_text(
        (50, 72),
        "张三 求职意向：后端开发实习生\n教育经历 2022.09-2026.06 某大学 计算机科学与技术\n"
        "项目经历 参与订单系统开发，负责接口与数据库设计，完成课程验收。\n" * 8,
        fontname="china-s",
        fontsize=10,
    )
    pdf_bytes = d.tobytes()
    d.close()
    st, r = post(base, "/api/extract", {
        "name": "privacy-text.pdf",
        "b64": base64.b64encode(pdf_bytes).decode(),
        "allow_images": False,
    })
    check("文字 PDF 未同意时不保留页面图",
          r.get("ok") is True and r.get("pages") == 0 and r.get("visual_available") is False,
          f"pages={r.get('pages')} visual={r.get('visual_available')}")

    print("\n[2] 体积上限（30MB 文件 → base64 约 40MB）")
    big = base64.b64encode(b"a" * (31 * 1024 * 1024)).decode()
    st, r = post(base, "/api/extract", {"name": "big.png", "b64": big})
    check("31MB 被 413 拦下", st == 413 and "30MB" in (r.get("error") or ""), f"HTTP {st} {r.get('error','')[:30]}")
    del big
    mid = base64.b64encode(b"a" * (24 * 1024 * 1024)).decode()
    st, r = post(base, "/api/extract", {"name": "mid.exe", "b64": mid})
    check("24MB 能进到业务层（不再被体积挡）", st == 200 and "不支持" in (r.get("error") or ""), f"HTTP {st}")
    del mid

    print("\n[3] 会话与评审")
    st, r = post(base, "/api/review", {"session_id": "nope"})
    check("无效会话被拒", not r.get("ok") and "过期" in (r.get("error") or ""), r.get("error", ""))

    txt = (ROOT / "samples" / "demo" / "02-后端-部分量化.txt").read_text(encoding="utf-8")
    name = f"QA-{uuid.uuid4().hex[:6]}"
    st, ex = post(base, "/api/extract", {"text": txt, "name": name})
    if not check("文本识别成功", st == 200 and ex.get("ok"), str(ex)[:80]):
        return None
    sid = ex["session_id"]

    t0 = time.time()
    st, rv = post(base, "/api/review", {
        "session_id": sid, "job": "后端开发实习生（Java）",
        "jd": "要求 Java / Redis / Kafka / 单元测试", "repeat": 1, "visual": True,
    })
    dt = time.time() - t0
    if not check("评审返回 ok", rv.get("ok") is True, f"{dt:.1f}s {rv.get('error','')[:60]}"):
        return None
    rep = rv.get("report") or ""
    check("报告有分数", isinstance(rv.get("score"), int), f"score={rv.get('score')}")
    check("报告含三处改写", "## 三处" in rep)
    check("报告含今天要做的事", "今天先做这一件事" in rep)
    check("返回 gates 结构", isinstance(rv.get("gates"), dict) and "hr" in rv["gates"], str(rv.get("gates")))
    paste = rv.get("paste") or ""
    # 复制块允许为空：需要确认事实或仍含占位符的改写不得进入一键复制。
    check("复制块不含待补占位符", "[待补充" not in paste, paste[:60])

    print("\n[4] 导出与路径安全")
    rid = rv.get("review_id") or ""
    check("返回 review_id", bool(rid) and len(rid) == 32, rid)
    for kind, ctype, magic in (
        ("md", "text/markdown", b"#"),
        ("docx", "wordprocessingml", b"PK"),
        ("json", "json", b"{"),
    ):
        st, body, hdrs = get(base, f"/api/download?id={urllib.parse.quote(rid)}&kind={kind}")
        name_ok = check(f"下载 .{kind} 200", st == 200, f"HTTP {st} {len(body)}B")
        if not name_ok:
            continue
        check(f".{kind} 类型正确", ctype in (hdrs.get("Content-Type") or "").lower(), hdrs.get("Content-Type", ""))
        check(f".{kind} 内容合法", body.startswith(magic), body[:8].hex())

    st, body, _ = get(base, "/api/download?id=..%2F..%2Fweb%2Fserver&kind=md")
    check("下载路径穿越被挡", st in (400, 404), f"HTTP {st}")
    st, body, _ = get(base, "/api/download?id=" + "f" * 32 + "&kind=md")
    check("不存在的报告 404", st == 404, f"HTTP {st}")
    st, body, _ = get(base, f"/api/report?id={urllib.parse.quote(rid)}")
    try:
        rp = json.loads(body)
    except Exception:  # noqa: BLE001
        rp = {}
    check("按 ID 重看历史报告", st == 200 and rp.get("review_id") == rid and bool(rp.get("resume_text")),
          f"HTTP {st}")

    print("\n[5] 落盘与历史")
    rep_dir = Path(os.environ.get("REPORTS_DIR") or (ROOT / "reports"))
    artifacts = {}
    for ext in (".md", ".json", ".txt", ".docx", ".meta.json"):
        found = list(rep_dir.glob(f"*{rid[:12]}{ext}"))
        artifacts[ext] = found[0] if found else None
        check(f"reports/*{rid[:12]}{ext} 存在", bool(found), f"{found[0].stat().st_size}B" if found else "")
    txt_file = artifacts.get(".txt")
    if txt_file:
        saved = txt_file.read_text(encoding="utf-8")
        import re
        check("落盘文本已脱敏", not re.search(r"(?<!\d)1[3-9]\d{9}(?!\d)", saved), "无完整手机号")

    st, raw, _ = get(base, "/api/history")
    try:
        h = json.loads(raw)
    except Exception:  # noqa: BLE001
        h = {}
    hist_item = next((x for x in (h.get("items") or []) if x.get("review_id") == rid), None)
    check("历史列表按 ID 出现", bool(hist_item), str(hist_item)[:100])

    st, ex2 = post(base, "/api/extract", {"text": txt, "name": name})
    check("同内容再上传能读到历史", (ex2.get("runs") or 0) >= 1 and
          isinstance(ex2.get("prev"), dict) and ex2["prev"].get("review_id"),
          f"runs={ex2.get('runs')} prev={ex2.get('prev')}")

    print("\n[6] 删除历史与文件")
    st, dr = delete(base, f"/api/report?id={urllib.parse.quote(rid)}")
    check("删除报告成功", st == 200 and dr.get("ok") is True, f"HTTP {st} {dr}")
    st, _, _ = get(base, f"/api/download?id={urllib.parse.quote(rid)}&kind=md")
    check("删除后下载 404", st == 404, f"HTTP {st}")
    # 元数据文件也必须一起删：留着它等于留着「这份简历评过几次、改没改」的痕迹
    left = list(rep_dir.glob(f"*{rid[:12]}.meta.json"))
    check("删除后不留 .meta.json", not left, str(left[:1]))
    st, dr = delete(base, f"/api/report?id={urllib.parse.quote(rid)}")
    check("重复删除返回 404", st == 404, f"HTTP {st}")
    st, dr = delete(base, "/api/report?id=..%2F..%2Fweb%2Fserver")
    check("删除接口挡非法 ID", st == 400, f"HTTP {st}")

    return rid


# ---------------------------------------------------------------- 真实模型
def test_real(base: str, stem: str | None, ocr: bool) -> None:
    print("\n[6] 真实模型：带 JD 的评审")
    txt = (ROOT / "samples" / "demo" / "03-后端-较完整.txt").read_text(encoding="utf-8")
    name = f"QA-real-{uuid.uuid4().hex[:4]}"
    st, ex = post(base, "/api/extract", {"text": txt, "name": name})
    if not check("真实识别成功", ex.get("ok"), str(ex)[:60]):
        return
    t0 = time.time()
    st, rv = post(base, "/api/review", {
        "session_id": ex["session_id"], "job": "后端开发实习生",
        "jd": "要求 Java / Spring Boot / MySQL / Redis / Kafka", "repeat": 1, "visual": True,
    })
    dt = time.time() - t0
    rep = rv.get("report") or ""
    check("真实评审 ok", rv.get("ok") is True,
          f"{dt:.1f}s {rv.get('code','')} {(rv.get('error') or '')[:80]}")
    check("报告出现岗位匹配段（贴了 JD）", "## 岗位匹配" in rep)
    check("耗时在 10~120 秒内", 10 <= dt <= 120, f"{dt:.1f}s")

    rep_dir = Path(os.environ.get("REPORTS_DIR") or (ROOT / "reports"))
    rid = rv.get("review_id") or ""
    found = list(rep_dir.glob(f"*{rid[:12]}.json")) if rid else []
    js = found[0] if found else None
    if js and js.exists():
        sys.path.insert(0, str(ROOT / "scripts"))
        import quality_gate
        doc = json.loads(js.read_text(encoding="utf-8"))
        gate = quality_gate.evaluate(doc, txt)
        # 正式交付的门禁：任何 blocker 都不能被当成"真实模型可接受波动"。
        check("交付门禁通过（0 blocker）", gate["eligible"],
              f"blockers={[p['code'] for p in gate['blockers']][:3]}")

    if ocr:
        print("\n[7] 真实模型：图片走视觉转写")
        png = ROOT / "_test" / "简历-AI应用开发.png"
        if not png.exists():
            bad("OCR 用例缺样例图", str(png))
            return
        st, ex = post(base, "/api/extract", {
            "name": f"QA-ocr-{uuid.uuid4().hex[:4]}.png",
            "b64": base64.b64encode(png.read_bytes()).decode(),
            "allow_images": True,
        }, timeout=300)
        check("图片识别成功（已明确同意）", ex.get("ok") is True, f"kind={ex.get('kind')} {ex.get('chars')}字")
        check("识别字数合理", (ex.get("chars") or 0) > 500, str(ex.get("chars")))


def cleanup() -> None:
    """删掉本次 QA 生成的报告与历史，不碰用户真实数据。"""
    rep = Path(os.environ.get("REPORTS_DIR") or (ROOT / "reports"))
    for f in list(rep.glob("QA-*")):
        f.unlink(missing_ok=True)
    hist = rep / ".history.json"
    if hist.exists():
        try:
            data = json.loads(hist.read_text(encoding="utf-8"))
            for k in [k for k in data if k.startswith("QA-")]:
                data.pop(k, None)
            hist.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    ap = argparse.ArgumentParser(description="PRD 验收冒烟")
    ap.add_argument("--base", default="http://127.0.0.1:8765")
    ap.add_argument("--mock", action="store_true", help="服务是 mock 模式")
    ap.add_argument("--real", action="store_true", help="额外跑真实模型用例")
    ap.add_argument("--ocr", action="store_true", help="真实模式下再跑图片 OCR")
    ap.add_argument("--keep", action="store_true", help="保留 QA 生成的报告文件")
    args = ap.parse_args()

    print(f"目标：{args.base}　模式：{'mock' if args.mock else 'real'}")
    test_ingest_local()
    stem = test_contract(args.base, args.mock)
    if args.real:
        test_real(args.base, stem, args.ocr)
    if not args.keep:
        cleanup()

    total = len(RESULTS)
    passed = sum(1 for r in RESULTS if r[0])
    print(f"\n结果：{passed}/{total} 通过")
    for f, n, d in RESULTS:
        if not f:
            print(f"  失败：{n}　{d}")
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
