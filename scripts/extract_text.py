"""从 docx / pdf / txt 提取纯文本，可选脱敏。

用法：
    python scripts/extract_text.py <文件或目录> [-o out.txt] [--desensitize]
    python scripts/extract_text.py samples/            # 目录下所有简历 -> samples/txt/*.txt
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SUPPORTED = {".docx", ".pdf", ".txt", ".md"}


# ---------------------------------------------------------------- 提取
def extract_docx(path: Path) -> str:
    """用 python-docx 提取段落 + 表格；dirty=False，避免生成 read-only 的 zip 成员句柄。"""
    from docx import Document  # pip install python-docx

    doc = Document(str(path))
    blocks: list[str] = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            # 合并单元格会重复出现同一段文字，去重保序
            seen: list[str] = []
            for c in cells:
                if c and (not seen or seen[-1] != c):
                    seen.append(c)
            if seen:
                blocks.append(" | ".join(seen))
    return "\n".join(blocks)


def extract_pdf(path: Path) -> str:
    """优先 pypdf，失败回退 pdfplumber。"""
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [(page.extract_text() or "") for page in reader.pages]
        text = "\n".join(pages).strip()
        if text:
            return text
        print(f"[warn] {path.name}: pypdf 提取为空，可能是扫描件，尝试 pdfplumber", file=sys.stderr)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] {path.name}: pypdf 失败 ({exc})，回退 pdfplumber", file=sys.stderr)

    try:
        import pdfplumber

        with pdfplumber.open(str(path)) as pdf:
            return "\n".join((p.extract_text() or "") for p in pdf.pages).strip()
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"{path.name} 提取失败。若是扫描件/图片型 PDF，请先用 OCR 或手动复制文本。原始错误：{exc}"
        ) from exc


def normalize(text: str) -> str:
    """去掉项目符号、多余空行、不间断空格等噪音，保留换行以维持段落边界。"""
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = []
    for line in text.splitlines():
        s = line.strip()
        # 纯项目符号行没有信息量
        if s in {"•", "-", "*", "·", "●", "○", "▪", "◆"}:
            continue
        lines.append(s)
    return "\n".join(lines).strip()


def extract(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".docx":
        return normalize(extract_docx(path))
    if suffix == ".pdf":
        return normalize(extract_pdf(path))
    if suffix in {".txt", ".md"}:
        return normalize(path.read_text(encoding="utf-8", errors="ignore"))
    raise ValueError(f"不支持的格式：{suffix}")


# ---------------------------------------------------------------- 脱敏
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
# 只认"出生/生日"字段，或带 年/月 单位的完整日期。
# 关键：绝不能吞掉 2022.09 - 2026.06 这类教育/工作起止时间——评审要用。
DOB_LINE = re.compile(r"(出生|生日|出生年月|birth)", re.I)
DOB_FULL = re.compile(
    r"\d{4}\s*年\s*\d{1,2}\s*月(\s*\d{1,2}\s*日)?|\d{4}\s*年\s*生"
)
GITHUB = re.compile(r"(https?://)?(www\.)?(github\.com|gitee\.com)/[\w.-]+", re.I)
LINKEDIN = re.compile(r"(https?://)?(www\.)?linkedin\.com/in/[\w-]+", re.I)


def desensitize(text: str) -> str:
    text = ID_CARD.sub("[身份证已脱敏]", text)
    text = PHONE.sub("[手机号已脱敏]", text)
    text = EMAIL.sub("[邮箱已脱敏]", text)
    text = GITHUB.sub("[代码主页已脱敏]", text)
    text = LINKEDIN.sub("[领英已脱敏]", text)

    # 逐行处理出生日期：只有该行明确是出生信息时才替换，其余日期（学历起止）保留
    out = []
    for line in text.splitlines():
        if DOB_LINE.search(line):
            line = DOB_FULL.sub("[出生年月已脱敏]", line)
            line = re.sub(r"\d{4}[./-]\d{1,2}([./-]\d{1,2})?(?!\d)", "[出生年月已脱敏]", line)
            line = re.sub(r"(?<!\d)(19|20)\d{2}(?![\d./年])", "[出生年已脱敏]", line)
        out.append(line)
    return "\n".join(out)


# ---------------------------------------------------------------- 入口
def main() -> int:
    ap = argparse.ArgumentParser(description="提取简历文本（可选脱敏）")
    ap.add_argument("target", help="文件或目录")
    ap.add_argument("-o", "--output", help="输出文件；目录模式默认写到 <dir>/txt/")
    ap.add_argument("--desensitize", action="store_true", help="对手机号/邮箱/身份证/链接脱敏")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出 {文件名: 文本}")
    args = ap.parse_args()

    target = Path(args.target)
    if not target.exists():
        print(f"[error] 路径不存在：{target}", file=sys.stderr)
        return 2

    paths = (
        sorted(p for p in target.iterdir() if p.suffix.lower() in SUPPORTED)
        if target.is_dir()
        else [target]
    )
    if not paths:
        print(f"[error] {target} 下没有可提取的文件（支持 {sorted(SUPPORTED)}）", file=sys.stderr)
        return 2

    results: dict[str, str] = {}
    for p in paths:
        try:
            text = extract(p)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {p.name}: {exc}", file=sys.stderr)
            continue
        if args.desensitize:
            text = desensitize(text)
        results[p.name] = text
        print(f"[ok] {p.name}: {len(text)} 字符", file=sys.stderr)

    if not results:
        return 1

    if args.json:
        payload = json.dumps(results, ensure_ascii=False, indent=2)
        if args.output:
            Path(args.output).write_text(payload, encoding="utf-8")
        else:
            print(payload)
        return 0

    if target.is_dir():
        out_dir = Path(args.output) if args.output else target / "txt"
        out_dir.mkdir(parents=True, exist_ok=True)
        for name, text in results.items():
            (out_dir / (Path(name).stem + ".txt")).write_text(text, encoding="utf-8")
        print(f"[done] {len(results)} 份 -> {out_dir}", file=sys.stderr)
    elif args.output:
        Path(args.output).write_text(next(iter(results.values())), encoding="utf-8")
        print(f"[done] -> {args.output}", file=sys.stderr)
    else:
        print(next(iter(results.values())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
