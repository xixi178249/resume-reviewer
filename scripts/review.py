"""一键评审：PDF / 图片 / docx / txt → 一页可读报告。

用法：
    python scripts/review.py 简历.pdf
    python scripts/review.py 简历.png --job "后端开发实习生" --visual
    python scripts/review.py 简历.docx --visual    # 明确允许页面图用于排版检查

输出：
    屏幕上一页报告 + reports/<文件名>.md + reports/<文件名>.json
"""
from __future__ import annotations

import argparse
import base64
import json
import re
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output  # noqa: E402
import ingest  # noqa: E402
import quality_gate  # noqa: E402
import run_eval  # noqa: E402
import version_diff  # noqa: E402

REPORTS = ROOT / "reports"

VERDICT_CN = {
    "strong_pass": "强推",
    "pass": "可投",
    "borderline": "勉强",
    "reject": "不建议投",
}


# ---------------------------------------------------------------- 报告零件
def select_rewrites(doc: dict, resume: str = "") -> tuple[list[dict], int]:
    """挑出可以放进"改写建议"区的条目。

    含编造数字的（改写里出现原文没有的数字）一律撤下——这是本产品最重要的一条红线。
    返回 (可展示的前 3 条, 因编造数字被撤下的条数)。
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


PLACEHOLDER_RE = re.compile(r"\[待补充[^\]]*\]")


def directly_copyable(rewrite: dict) -> bool:
    """只有事实无需确认、且不含待补占位符的改写，才允许一键复制。"""
    if rewrite.get("needs_user_confirm") is True:
        return False
    if PLACEHOLDER_RE.search(rewrite.get("rewritten") or ""):
        return False
    return True


def copyable_rewrites(rewrites: list[dict]) -> list[dict]:
    return [r for r in rewrites if directly_copyable(r)]


def paste_lines(doc: dict, resume: str = "", rewrites: list[dict] | None = None) -> list[str]:
    """把可安全直接复制的改写拼成"按原简历顺序"的段落。"""
    if rewrites is None:
        rewrites, _ = select_rewrites(doc, resume)
    rewrites = copyable_rewrites(rewrites)
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


def with_repair_note(messages: list[dict], reasons: list[str]) -> list[dict]:
    """把上一次被拒的具体原因告诉模型，让它照着改，而不是原样再赌一次。

    实测：光重试不加信息，同一份简历会连续撞同一堵墙（数字溯源漏一个、字段类型写 null）。
    把错误原文回灌后，第二轮基本能逐条修掉。
    """
    if not reasons:
        return messages
    lines = "\n".join(f"- {r}" for r in reasons[:6])
    note = (
        "你上一次的输出被质量门禁拒绝了，原因如下：\n"
        f"{lines}\n\n"
        "请逐条修正后重新输出**完整的** JSON（不要只输出改动部分、不要解释）。"
        "除上述问题外，原有的角色设定、评分标准、字段要求一律保持不变。"
    )
    return list(messages) + [{"role": "user", "content": note}]


def run_reviews(
    prompt: str,
    images: list[bytes],
    n: int = 1,
    dump: Path | None = None,
    resume: str = "",
    config: run_eval.LLMConfig | None = None,
):
    """跑 N 次，只从通过质量门禁的候选里取「分数中位」的那一份。

    同一份简历单次打分会有 ±5 的抖动，所以多跑几次、取中间那次更稳。
    但"分数居中"不能成为坏结果被选中的理由：任何被门禁拒绝的候选都不参与排序。
    返回 (选中的完整结果, 合格候选的分数列表, 选中候选的质量报告)。
    """
    if not resume:
        raise ValueError("run_reviews 需要简历原文用于事实校验")

    candidates: list[tuple[dict, dict]] = []
    rejected: list[quality_gate.Problem] = []
    failures: list[str] = []

    # 用例：repeat=1（默认）时只采一次样，一次模型抖动就让用户直接看到报错，
    # 24 秒白等。所以「单次采样」失败后自动补一次；repeat>1 本身已有多个候选，
    # 不再额外增加调用（那几个候选本来就互为备份）。
    attempts_planned = max(1, n)
    max_attempts = attempts_planned + 1 if attempts_planned == 1 else attempts_planned

    used = 0
    repair: list[str] = []
    for _ in range(max_attempts):
        used += 1
        try:
            messages = with_repair_note(messages_with_images(prompt, images), repair)
            raw = run_eval.call_llm(messages, dump_to=dump, config=config)
        except run_eval.LLMError:
            # key/余额/地址/网络这类错误，重试只会继续烧用户的钱。
            # 原样往上抛，让调用方把 code 和原因告诉用户（自己填的 key 自己改）。
            raise
        except SystemExit as exc:
            # 鉴权、余额、网络配置类错误重试只会继续烧时间。
            failures.append(str(exc))
            break
        except (json.JSONDecodeError, RuntimeError) as exc:
            failures.append(str(exc))
            continue
        except Exception as exc:  # noqa: BLE001
            failures.append(str(exc))
            continue

        quality = quality_gate.evaluate(raw, resume)
        if quality["eligible"]:
            candidates.append((quality["doc"], quality))
        else:
            problems = [quality_gate.Problem(**p) for p in quality["blockers"]]
            rejected.extend(problems)
            repair = [p.message for p in problems if p.severity == quality_gate.BLOCKER]

    if not candidates:
        if rejected:
            raise quality_gate.QualityGateError(rejected, attempts=used)
        detail = failures[0][:160] if failures else "没有拿到模型输出"
        raise RuntimeError(f"多次调用都失败了，检查网络或模型额度：{detail}")

    candidates.sort(key=lambda pair: pair[0].get("overall_score") or 0)
    doc, quality = candidates[len(candidates) // 2]
    scores = [pair[0].get("overall_score") for pair in candidates]
    return doc, scores, quality


def render(
    doc: dict, src: Path, job: str, notes: list[str], problem: list[str],
    resume: str = "", scores: list | None = None, delta: int | None = None,
    quality: dict | None = None, compare: dict | None = None,
) -> str:
    """渲染成一页能读懂的报告（不是 JSON 搬家）。

    传入 quality 时只允许渲染通过门禁的交付版本，拒绝"失败却看起来成功"的报告。
    compare 是上一版本对比（见 version_diff）；没有就不渲染这一节。
    """
    if quality is not None and not quality.get("eligible", False):
        raise ValueError("质量门禁未通过，不能渲染正式报告")
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

    # 改版前后对比放在最前面：用户最想知道的就是"我改的有没有用"
    L.extend(version_diff.to_markdown(compare or {}))

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

    # 只展示安全改写。需本人确认事实或含 [待补充] 的仍可展示，但不能一键复制。
    rewrites, dropped = select_rewrites(doc, resume)
    quality_filtered = (quality or {}).get("filtered") or []
    rewrite_filters = [
        p for p in quality_filtered if str(p.get("code", "")).startswith("REWRITE_")
    ]
    dropped = max(dropped, len(rewrite_filters))
    needs_confirm = any(
        r.get("needs_user_confirm") is True or PLACEHOLDER_RE.search(r.get("rewritten") or "")
        for r in rewrites
    )

    if rewrites:
        L.append("## 三处建议改写（确认事实后再替换）" if needs_confirm
                 else "## 三处可以直接替换的写法")
        L.append("")
        for i, r in enumerate(rewrites, 1):
            L.append(f"**{i}. {r.get('section', '经历')}**")
            L.append("")
            L.append(f"- 原来：`{r.get('original', '').strip()}`")
            L.append(f"- 改成：`{r.get('rewritten', '').strip()}`")
            why = (r.get("rationale") or "").strip()
            if why:
                L.append(f"- 为什么：{why}")
            delta = str(r.get("fact_delta") or "")
            if r.get("needs_user_confirm") and delta not in ("", "none", "wording"):
                L.append("- ⚠️ 这条改了事实强度，**投之前先确认自己真的做到了，不能一键直接复制**")
            if PLACEHOLDER_RE.search(r.get("rewritten") or ""):
                L.append("- ⚠️ 这条还有待补充事实，补全后才能使用")
            L.append("")

    if dropped:
        L.append(f"> 另有 {dropped} 条改写因新增数字、无法回指原文或事实未经确认，已从正式建议中撤下。")
        L.append("")

    block = paste_lines(doc, resume, rewrites)
    if block:
        L.append("## 可直接复制的改写片段（按简历顺序）")
        L.append("")
        L.append("```")
        L.extend(block)
        L.append("```")
        L.append("")
        L.append("> 这里只是可安全直接复制的改写片段，不是完整简历。需确认事实或含待补占位符的条目没有放进来。")
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
    issue_filters = [
        p for p in quality_filtered if str(p.get("code", "")).startswith("ISSUE_")
    ]
    if issue_filters:
        L.append(f"> 另有 {len(issue_filters)} 条问题因无法回指原文或建议不可执行，已从报告中撤下。")
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
    ap.add_argument("--visual", action="store_true",
                    help="明确允许把页面图发给模型做 OCR/排版检查（默认不发送）")
    ap.add_argument("--no-visual", action="store_true", help=argparse.SUPPRESS)  # 兼容旧命令
    ap.add_argument("--no-ocr", action="store_true", help="禁止 OCR（扫描件/图片会直接报错）")
    ap.add_argument("--out", default=None, help="报告输出目录，默认 reports/")
    ap.add_argument(
        "--repeat", type=int, default=1,
        help="评审几次取中位（默认 1，最多 3）。同一份简历单次会抖 ±5 分，3 次更稳，成本也变 3 倍",
    )
    ap.add_argument(
        "--from-json",
        action="store_true",
        help="不调模型，用 reports/<文件名>.json 重新渲染报告（改排版/过滤时用，不花钱）",
    )
    args = ap.parse_args()
    args.repeat = max(1, min(3, args.repeat))  # 与 Web 一致，防误触烧额度
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
        if not resume:
            print("[error] 找不到已存的脱敏文本，无法做事实校验；请重新提供原文件或 .txt", file=sys.stderr)
            return 2
        quality = quality_gate.evaluate(doc, resume)
        if not quality["eligible"]:
            print("[error] 已有 JSON 未通过质量门禁，未生成正式报告：", file=sys.stderr)
            for p in quality["blockers"][:6]:
                print(f"  - [{p['code']}] {p['message']}", file=sys.stderr)
            return 1
        doc = quality["doc"]
        warn = [p["message"] for p in quality["warnings"]]
        body = render(doc, src, args.job, [], warn[:3], resume, quality=quality)
        (out_dir / f"{src.stem}.md").write_text(body, encoding="utf-8", newline="\n")
        try:
            write_docx(body, out_dir / f"{src.stem}.docx", title=f"简历评审 · {src.stem}")
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] docx 生成失败：{exc}", file=sys.stderr)
        print(body)
        return 0

    print(f"→ 读取 {src.name} …", file=sys.stderr)
    allow_images = args.visual and not args.no_visual
    try:
        ing = ingest.load(src, ocr=not args.no_ocr, include_images=allow_images)
    except ingest.ImageConsentRequired as exc:
        print(f"[error] {exc}\n  需要图像识别或排版检查时，请显式加 --visual", file=sys.stderr)
        return 2
    except Exception as exc:  # noqa: BLE001
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    if len(ing.text) < 80:
        print(f"[error] 提取到的文字太短（{len(ing.text)} 字），无法评审。"
              "请确认文件不是空白页，或手动粘贴文本。", file=sys.stderr)
        return 2

    print(f"→ 提取完成：{len(ing.text)} 字（{ing.kind}）", file=sys.stderr)

    case = {"job_target": args.job, "jd": jd, "background": ""}
    images = ing.images
    prompt = run_eval.build_prompt(
        ing.text, case, run_eval.DEFAULT_PROMPT_VERSION, has_image=bool(images)
    )

    print(f"→ 调用模型评审{'(含 ' + str(min(len(images), 2)) + ' 页截图)' if images else '(纯文本)'} …", file=sys.stderr)
    out_dir.mkdir(parents=True, exist_ok=True)
    dump = out_dir / f"{src.stem}.raw.txt"
    try:
        doc, scores, quality = run_reviews(prompt, images, args.repeat, dump, resume=ing.text)
    except quality_gate.QualityGateError as exc:
        print(f"[error] {exc}", file=sys.stderr)
        for p in exc.public_details():
            print(f"  - [{p['code']}] {p['message']}", file=sys.stderr)
        return 1
    except run_eval.LLMError as exc:
        print(f"[error] {exc.code}｜{exc.message}", file=sys.stderr)
        return 2
    except SystemExit as exc:
        print(str(exc), file=sys.stderr)
        return 2
    except (RuntimeError, json.JSONDecodeError) as exc:
        print(f"[error] {exc}", file=sys.stderr)
        return 2

    problem = [p["message"] for p in quality["warnings"]][:3]
    body = render(
        doc, src, args.job, ing.notes, problem, ing.text, scores, quality=quality
    )

    md = out_dir / f"{src.stem}.md"
    js = out_dir / f"{src.stem}.json"
    tx = out_dir / f"{src.stem}.txt"
    md.write_text(body, encoding="utf-8", newline="\n")
    js.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
    tx.write_text(ing.text, encoding="utf-8", newline="\n")   # 已脱敏，供 --from-json 复用
    try:
        write_docx(body, out_dir / f"{src.stem}.docx", title=f"简历评审 · {src.stem}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] docx 生成失败：{exc}", file=sys.stderr)

    print()
    print(body)
    print()
    print(f"报告已保存：{md}", file=sys.stderr)
    if quality["filtered"]:
        print(f"ℹ️ 已撤下 {len(quality['filtered'])} 条不可安全交付的局部内容", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
