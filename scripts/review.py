"""一键评审：PDF / 图片 / docx / txt → 一页可读报告。

用法：
    python scripts/review.py 简历.pdf
    python scripts/review.py 简历.png --job "后端开发实习生"
    python scripts/review.py 简历.docx --no-visual     # 不把页面图发给模型，省 token

输出：
    屏幕上一页报告 + reports/<文件名>.md + reports/<文件名>.json
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output  # noqa: E402
import ingest  # noqa: E402
import run_eval  # noqa: E402

REPORTS = ROOT / "reports"

VERDICT_CN = {
    "strong_pass": "强推",
    "pass": "可投",
    "borderline": "勉强",
    "reject": "不建议投",
}


# ---------------------------------------------------------------- 报告零件
def select_rewrites(doc: dict, resume: str = "") -> tuple[list[dict], int]:
    """挑出可以放进"直接替换"区的改写。

    含编造数字的（改写里出现原文没有的数字）一律撤下——这是本产品最重要的一条红线。
    返回 (可用的前 3 条, 因编造数字被撤下的条数)。
    注意：超出 3 条的"多余改写"不算被撤下，不能写进报告，否则等于诬告模型。
    """
    all_rw = doc.get("rewrites") or []
    pool = all_rw
    if resume:
        pool = [
            r for r in pool
            if not (check_output.numbers_in(r.get("rewritten", ""))
                    - check_output.numbers_in(resume))
        ]
    return pool[:3], len(all_rw) - len(pool)


def paste_lines(doc: dict, resume: str = "", rewrites: list[dict] | None = None) -> list[str]:
    """把 3 条改写拼成"按原简历顺序"的段落，供用户一次粘回 Word。"""
    if rewrites is None:
        rewrites, _ = select_rewrites(doc, resume)
    if not rewrites:
        return []

    def pos(r: dict) -> int:
        orig = (r.get("original") or "").strip()
        idx = resume.find(orig) if (resume and orig) else -1
        return idx if idx >= 0 else 10 ** 9

    out: list[str] = []
    for r in sorted(rewrites, key=pos):
        line = (r.get("rewritten") or "").strip()
        if line:
            out.append(line)
    return out


def paste_md(doc: dict, resume: str = "") -> str:
    return "\n".join(paste_lines(doc, resume))


def _jd_section(doc: dict) -> list[str]:
    """岗位匹配：JD 给了才有。没有 JD 时整段不出现，不占版面。"""
    jd = doc.get("jd_match") or {}
    if not jd.get("jd_provided"):
        return []
    kws = [k for k in (jd.get("keywords") or []) if (k.get("term") or "").strip()]
    L: list[str] = ["## 岗位匹配（对着你贴的 JD）", ""]
    if kws:
        hit = sum(1 for k in kws if k.get("natural") is True)
        rate = jd.get("coverage_rate")
        pct = f"{int(round(rate * 100))}%" if isinstance(rate, (int, float)) else f"{hit}/{len(kws)}"
        L.append(f"**关键词覆盖 {pct}**：{hit} 个在经历里有证据，{len(kws) - hit} 个没有。")
        L.append("")
    for k in kws[:8]:
        term = (k.get("term") or "").strip()
        if k.get("natural") is True:
            where = (k.get("where") or "").strip()
            L.append(f"- ✅ **{term}**：{where or '经历里有对应证据'}")
        elif k.get("in_resume"):
            L.append(f"- ⚠️ **{term}**：只出现在技能栏，经历里没有证据——面试官会追着问")
        else:
            L.append(f"- ❌ **{term}**：简历里完全没有")
    if kws:
        L.append("")
    gap = (jd.get("gap_summary") or "").strip()
    if gap:
        L.append(f"> {gap}")
        L.append("")
    return L


def write_docx(report: str, out_path: Path, title: str | None = None) -> Path:
    """把 Markdown 报告写成一个能直接打开的 .docx（不依赖第三方 md 库）。"""
    from docx import Document
    from docx.shared import Pt, RGBColor
    from docx.oxml.ns import qn

    doc = Document()
    normal = doc.styles["Normal"]
    normal.font.name = "Microsoft YaHei"
    normal.font.size = Pt(10.5)
    normal.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")

    def inline(p, text: str) -> None:
        """处理 **粗体** 与 `等宽`，其余原样。"""
        import re as _re

        for part in _re.split(r"(\*\*[^*]+\*\*|`[^`]+`)", text):
            if not part:
                continue
            if part.startswith("**") and part.endswith("**"):
                run = p.add_run(part[2:-2])
                run.bold = True
            elif part.startswith("`") and part.endswith("`"):
                run = p.add_run(part[1:-1])
                run.font.name = "Consolas"
                run.font.size = Pt(9.5)
                run.font.color.rgb = RGBColor(0x0F, 0x17, 0x2A)
            else:
                p.add_run(part)

    in_code = False
    code_buf: list[str] = []

    def flush_code() -> None:
        for line in code_buf:
            p = doc.add_paragraph()
            run = p.add_run(line)
            run.font.name = "Consolas"
            run.font.size = Pt(9.5)
        code_buf.clear()

    for raw in report.splitlines():
        line = raw.rstrip()
        if line.startswith("```"):
            if in_code:
                flush_code()
            in_code = not in_code
            continue
        if in_code:
            code_buf.append(line)
            continue
        if not line.strip():
            continue
        if line.startswith("# "):
            h = doc.add_heading(line[2:].strip(), level=1)
            for r in h.runs:
                r.font.name = "Microsoft YaHei"
                r.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
            continue
        if line.startswith("## "):
            h = doc.add_heading(line[3:].strip(), level=2)
            for r in h.runs:
                r.font.name = "Microsoft YaHei"
                r.element.rPr.rFonts.set(qn("w:eastAsia"), "Microsoft YaHei")
            continue
        if line.startswith("> "):
            p = doc.add_paragraph()
            p.paragraph_format.left_indent = Pt(14)
            run = p.add_run(line[2:].strip())
            run.italic = True
            run.font.color.rgb = RGBColor(0x4B, 0x55, 0x63)
            continue
        if line.startswith("- "):
            p = doc.add_paragraph(style="List Bullet")
            inline(p, line[2:].strip())
            continue
        if line.startswith("---"):
            continue
        p = doc.add_paragraph()
        inline(p, line.strip())

    if code_buf:
        flush_code()

    meta = doc.core_properties
    meta.title = title or out_path.stem
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    return out_path


def messages_with_images(prompt: str, images: list[bytes], max_pages: int = 2) -> list[dict]:
    if not images:
        return [{"role": "user", "content": prompt}]
    parts: list[dict] = [{"type": "text", "text": prompt}]
    for png in images[:max_pages]:
        b64 = base64.b64encode(png).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    return [{"role": "user", "content": parts}]


def run_reviews(prompt: str, images: list[bytes], n: int = 1, dump: Path | None = None):
    """跑 N 次，取「分数中位」的那一份。

    同一份简历单次打分会有 ±5 的抖动（模型非确定性 + OCR 文本每次略有差异），
    所以多跑几次、取中间那次，比只跑一次可靠。
    返回 (选中的完整结果, 各次分数列表)。
    """
    docs = []
    for _ in range(max(1, n)):
        try:
            docs.append(run_eval.call_llm(messages_with_images(prompt, images), dump_to=dump))
        except json.JSONDecodeError:
            continue  # 单次失败不致命，只要还有成功的
    if not docs:
        raise RuntimeError("多次调用都失败了，检查网络或模型额度")

    docs.sort(key=lambda d: d.get("overall_score") or 0)
    mid = docs[len(docs) // 2]
    scores = [d.get("overall_score") for d in docs]
    return mid, scores


def render(
    doc: dict, src: Path, job: str, notes: list[str], problem: list[str],
    resume: str = "", scores: list | None = None, delta: int | None = None,
) -> str:
    """渲染成一页能读懂的报告（不是 JSON 搬家）。"""
    score = doc.get("overall_score", "?")
    verdict = VERDICT_CN.get(doc.get("verdict", ""), doc.get("verdict", "?"))
    hr = "✅ 过" if (doc.get("gate_hr") or {}).get("pass") else "❌ 不过"
    iv = "✅ 过" if (doc.get("gate_interview") or {}).get("pass") else "❌ 会挂"
    fr = doc.get("format_review") or {}
    fmt = {"blocking": "⛔ 格式会让人看不到内容", "risky": "⚠️ 格式有风险", "clean": "✅ 格式没问题"}.get(
        fr.get("verdict", ""), ""
    )

    L: list[str] = []
    L.append(f"# 简历评审 · {src.stem}")
    L.append("")
    L.append(f"岗位：{job}　｜　{datetime.now():%Y-%m-%d %H:%M}")
    L.append("")
    spread = ""
    if scores and len(scores) > 1:
        uniq = sorted(x for x in scores if isinstance(x, int))
        if uniq and uniq[0] != uniq[-1]:
            spread = f"　（{len(uniq)} 次：{'/'.join(map(str, uniq))}，取中位）"
    trend = ""
    if isinstance(delta, int) and delta != 0:
        trend = f"　（比上次 {'+' if delta > 0 else ''}{delta}）"
    L.append(f"## 结论：{score} / 100　{verdict}{spread}{trend}")
    L.append("")
    L.append(f"初筛 {hr}　面试 {iv}　{fmt}")
    L.append("")
    summary = doc.get("one_line_summary", "").strip()
    if summary:
        L.append(f"> {summary}")
        L.append("")

    L.extend(_jd_section(doc))

    # 格式层有具体问题时必须列出来，否则"格式有风险"是句空话
    bad_visual = [
        c for c in ((fr.get("visual_level") or {}).get("checks") or []) if c.get("ok") is False
    ]
    if bad_visual:
        L.append("## 排版上要改的地方")
        L.append("")
        for c in bad_visual[:4]:
            L.append(f"- **{c.get('item')}**：{c.get('note', '')}")
        L.append("")

    # 只展示"没有编造数字"的改写。含未经证实数据的句子不能进"直接替换"区。
    rewrites, dropped = select_rewrites(doc, resume)

    if rewrites:
        L.append("## 三处可以直接替换的写法")
        L.append("")
        for i, r in enumerate(rewrites, 1):
            L.append(f"**{i}. {r.get('section', '经历')}**")
            L.append("")
            L.append(f"- 原来：`{r.get('original', '').strip()}`")
            L.append(f"- 改成：`{r.get('rewritten', '').strip()}`")
            why = (r.get("rationale") or "").strip()
            if why:
                L.append(f"- 为什么：{why}")
            if r.get("needs_user_confirm"):
                L.append("- ⚠️ 这条改了事实强度，**投之前先确认自己真的做到了**")
            L.append("")

    if dropped:
        L.append(f"> 另有 {dropped} 条改写含未经核实的数据，已从上面撤下，见 .json 里的 rewrites。")
        L.append("")

    block = paste_lines(doc, resume, rewrites)
    if block:
        L.append("## 改完的版本（按简历顺序，可直接粘贴）")
        L.append("")
        L.append("```")
        L.extend(block)
        L.append("```")
        L.append("")
        L.append("> 这一段是上面几条改写的合体，按原简历的出现顺序排好。带 `[待补充：…]` 的地方必须换成你自己的真实数据再粘。")
        L.append("")

    actions = doc.get("next_actions") or []
    if actions:
        a0 = actions[0]
        L.append("## 今天先做这一件事")
        L.append("")
        L.append(f"**{a0.get('action', '')}**")
        mins = a0.get("est_minutes")
        if mins:
            L.append("")
            L.append(f"约 {mins} 分钟。做完这一步，再考虑其它 {len(actions) - 1} 条。")
        L.append("")

    bs = doc.get("before_submit") or []
    if bs:
        L.append("## 投递前自己核对")
        L.append("")
        for x in bs[:4]:
            L.append(f"- {x}")
        L.append("")

    HARD = ("编造", "不在原文", "回显了完整联系方式", "fact_delta", "needs_user_confirm",
            "没有任何可执行锚点", "数字溯源覆盖不足")
    hard = [e for e in problem if any(k in e for k in HARD)]
    soft = [e for e in problem if e not in hard]

    if hard:
        L.append("## ⚠️ 这份结果不可直接使用")
        L.append("")
        for x in hard[:3]:
            L.append(f"- {x}")
        L.append("")
        L.append("以上是模型在事实层面的问题，请以 .json 为准、人工复核后再用。")
        L.append("")
    if soft:
        L.append("> 自检提示（不影响阅读）：" + "；".join(x[:40] for x in soft[:2]))
        L.append("")

    rest = len(doc.get("issues") or []) - 0
    L.append("---")
    L.append("")
    tail = f"完整评审（{rest} 条问题、{len(doc.get('number_audit') or [])} 个待核数字）见同名 .json 文件。"
    L.append(tail)
    if notes:
        L.append("")
        L.append("处理说明：" + "；".join(notes))
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="一键评审简历，输出一页报告")
    ap.add_argument("path", help="简历文件（pdf / png / jpg / docx / txt）")
    ap.add_argument("--job", default="未提供（按简历里的求职意向推断）", help="目标岗位")
    ap.add_argument("--jd", default="", help="岗位 JD 原文（贴一段文本），给了才算关键词覆盖")
    ap.add_argument("--jd-file", default=None, help="JD 文本文件路径（比 --jd 好用）")
    ap.add_argument("--no-visual", action="store_true", help="不把页面图发给模型（省 token，但格式检查会退化）")
    ap.add_argument("--no-ocr", action="store_true", help="禁止 OCR（只处理文字型文件）")
    ap.add_argument("--out", default=None, help="报告输出目录，默认 reports/")
    ap.add_argument(
        "--repeat", type=int, default=1,
        help="评审几次取中位（默认 1）。同一份简历单次会抖 ±5 分，3 次更稳，成本也变 3 倍",
    )
    ap.add_argument(
        "--from-json",
        action="store_true",
        help="不调模型，用 reports/<文件名>.json 重新渲染报告（改排版/过滤时用，不花钱）",
    )
    args = ap.parse_args()
    out_dir = Path(args.out) if args.out else REPORTS

    jd = (args.jd or "").strip()
    if args.jd_file:
        jd = Path(args.jd_file).read_text(encoding="utf-8", errors="ignore").strip()

    src = Path(args.path)
    if args.from_json:
        js = out_dir / f"{src.stem}.json"
        if not js.exists():
            print(f"[error] 找不到 {js}，先正常跑一次", file=sys.stderr)
            return 2
        doc = json.loads(js.read_text(encoding="utf-8"))
        tx = out_dir / f"{src.stem}.txt"
        resume = tx.read_text(encoding="utf-8") if tx.exists() else ""
        if not resume:
            try:
                resume = ingest.load(src, ocr=False).text
            except Exception:  # noqa: BLE001
                print("[warn] 找不到已存的文本，跳过编造数字过滤", file=sys.stderr)
        res = check_output.check(doc, resume) if resume else {"errors": []}
        body = render(doc, src, args.job, [], res["errors"][:3], resume)
        (out_dir / f"{src.stem}.md").write_text(body, encoding="utf-8")
        try:
            write_docx(body, out_dir / f"{src.stem}.docx", title=f"简历评审 · {src.stem}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] docx 生成失败：{exc}", file=sys.stderr)
        print(body)
        return 0

    print(f"→ 读取 {src.name} …", file=sys.stderr)
    try:
        ing = ingest.load(src, ocr=not args.no_ocr)
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if len(ing.text) < 80:
        print(f"[error] 提取到的文字太短（{len(ing.text)} 字），无法评审。"
              "请确认文件不是空白页，或手动粘贴文本。", file=sys.stderr)
        return 2

    print(f"→ 提取完成：{len(ing.text)} 字（{ing.kind}）", file=sys.stderr)

    case = {"job_target": args.job, "jd": jd, "background": ""}
    images = [] if args.no_visual else ing.images
    prompt = run_eval.build_prompt(
        ing.text, case, run_eval.DEFAULT_PROMPT_VERSION, has_image=bool(images)
    )

    print(f"→ 调用模型评审{'(含 ' + str(min(len(images), 2)) + ' 页截图)' if images else '(纯文本)'} …", file=sys.stderr)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = out_dir / f"{src.stem}.raw.txt"
    try:
        doc, scores = run_reviews(prompt, images, args.repeat, dump)
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    res = check_output.check(doc, ing.text)
    problem = res["errors"][:3]

    body = render(doc, src, args.job, ing.notes, problem, ing.text, scores)

    md = out_dir / f"{src.stem}.md"
    js = out_dir / f"{src.stem}.json"
    tx = out_dir / f"{src.stem}.txt"
    md.write_text(body, encoding="utf-8")
    js.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tx.write_text(ing.text, encoding="utf-8")   # 已脱敏，供 --from-json 复用
    try:
        write_docx(body, out_dir / f"{src.stem}.docx", title=f"简历评审 · {src.stem}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] docx 生成失败：{exc}", file=sys.stderr)

    print()
    print(body)
    print()
    print(f"报告已保存：{md}", file=sys.stderr)
    if problem:
        print(f"⚠️ 有 {len(res['errors'])} 处质量问题未通过校验", file=sys.stderr)
    return 0 if not problem else 1


if __name__ == "__main__":
    raise SystemExit(main())
