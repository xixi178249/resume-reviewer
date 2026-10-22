"""输入归一化：PDF / 图片 / docx / txt → 纯文本（+ 页面图片）。

设计要点：
  · 文字型 PDF / docx / txt  → 直接抽文本，零成本
  · 扫描版 PDF / 图片        → 渲染成图，交给视觉模型转写（需要 API Key）
  · 无论哪条路径，输出都先脱敏，再往下走
"""
from __future__ import annotations

import base64
import io
import re
from dataclasses import dataclass, field
from pathlib import Path

# 与 extract_text.py 保持一致的脱敏口径
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
DOB_LINE = re.compile(r"(出生|生日|出生年月|birth)", re.I)
DOB_FULL = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月(\s*\d{1,2}\s*日)?|\d{4}\s*年\s*生")

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".txt", ".md"}

# 少于这个字数，就认为 PDF 是扫描件（纯文字 PDF 抽出来的通常远多于 100 字）
MIN_TEXT_CHARS = 120


@dataclass
class Ingest:
    text: str                      # 脱敏后的纯文本
    source: Path
    kind: str                      # text-pdf / scanned-pdf / image / docx / txt
    images: list[bytes] = field(default_factory=list)   # 每页 PNG
    notes: list[str] = field(default_factory=list)


def desensitize(text: str) -> str:
    """只留评审必须的信息：删掉手机号、邮箱、身份证，出生日期保留年。"""
    text = ID_CARD.sub("[身份证已脱敏]", text)
    text = PHONE.sub("[手机号已脱敏]", text)
    text = EMAIL.sub("[邮箱已脱敏]", text)
    out = []
    for line in text.splitlines():
        if DOB_LINE.search(line):
            line = DOB_FULL.sub("[出生年月已脱敏]", line)
            line = re.sub(r"\d{4}[./-]\d{1,2}([./-]\d{1,2})?(?!\d)", "[出生年月已脱敏]", line)
            line = re.sub(r"(?<!\d)(19|20)\d{2}(?![\d./年])", "[出生年已脱敏]", line)
        out.append(line)
    return "\n".join(out)


def normalize(text: str) -> str:
    text = text.replace("\u00a0", " ").replace("\u3000", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if s in {"•", "-", "*", "·", "●", "○", "▪", "◆"}:
            continue
        lines.append(s)
    return "\n".join(lines).strip()


# ---------------------------------------------------------------- 各格式抽取
def _pdf_text_and_pages(path: Path, dpi: int = 150) -> tuple[str, list[bytes]]:
    """用 PyMuPDF 同时拿文本和页面图；页面图只在需要 OCR 时才用得上。"""
    import fitz  # PyMuPDF

    texts: list[str] = []
    pages: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            texts.append(page.get_text("text"))
            pix = page.get_pixmap(dpi=dpi)
            pages.append(pix.tobytes("png"))
    return "\n".join(texts), pages


def _docx_text(path: Path) -> str:
    from docx import Document

    doc = Document(str(path))
    blocks = [p.text.strip() for p in doc.paragraphs if p.text.strip()]
    for table in doc.tables:
        for row in table.rows:
            cells = [c.text.strip().replace("\n", " ") for c in row.cells]
            seen: list[str] = []
            for c in cells:
                if c and (not seen or seen[-1] != c):
                    seen.append(c)
            if seen:
                blocks.append(" | ".join(seen))
    return "\n".join(blocks)


def _image_to_png(path: Path, max_width: int = 1400) -> bytes:
    """统一转 PNG 并压到合理宽度，控制 token 消耗。"""
    from PIL import Image

    with Image.open(path) as im:
        im = im.convert("RGB")
        if im.width > max_width:
            h = int(im.height * max_width / im.width)
            im = im.resize((max_width, h), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        return buf.getvalue()


# ---------------------------------------------------------------- 视觉转写
TRANSCRIBE_PROMPT = """把这张简历图片里的文字**逐行转录**成纯文本。

要求：
1. 只转录，不要总结、不要改写、不要补充任何内容。
2. 保留原有的小标题和项目符号（•）。
3. 看不清的字写 [无法辨认]，不要猜。
4. 姓名、手机号、邮箱按原样转录，后续会被程序脱敏。
5. 直接输出正文，不要加 ``` 代码块，不要任何解释。"""


def transcribe(images: list[bytes], model_note: str = "") -> str:
    """把页面图交给视觉模型转成文字。没有 Key 会抛 SystemExit。"""
    import run_eval

    parts = [{"type": "text", "text": TRANSCRIBE_PROMPT}]
    for png in images:
        b64 = base64.b64encode(png).decode("ascii")
        parts.append({"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}})
    messages = [{"role": "user", "content": parts}]
    return run_eval.chat(messages)


def _read_text_any(path: Path) -> str:
    """读纯文本：中文 Windows 记事本默认常是 GBK，直接 utf-8 会读出乱码。

    顺序很重要：先 utf-8 严格解码（成功就说明是 UTF-8），失败再退 gb18030，
    最后才用忽略错误兜底。gb18030 几乎不会抛错，放前面会把 UTF-8 也"解成功"成乱码。
    """
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "gb18030", "big5"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "ignore")


# ---------------------------------------------------------------- 主入口
def load(path: str | Path, ocr: bool = True) -> Ingest:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"不支持的格式 {suffix}，支持：{', '.join(sorted(SUPPORTED))}")

    notes: list[str] = []

    if suffix == ".pdf":
        raw, pages = _pdf_text_and_pages(p)
        if len(normalize(raw)) >= MIN_TEXT_CHARS:
            kind = "text-pdf"
        else:
            kind = "scanned-pdf"
            if not ocr:
                raise RuntimeError("这是扫描版 PDF，需要 OCR。去掉 --no-ocr 再试。")
            notes.append(f"PDF 里几乎没有文字（{len(normalize(raw))} 字），按扫描件处理，已转成 {len(pages)} 张图识别")
            raw = transcribe(pages)
        return Ingest(desensitize(normalize(raw)), p, kind, pages, notes)

    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        png = _image_to_png(p)
        if not ocr:
            raise RuntimeError("图片输入需要 OCR。去掉 --no-ocr 再试。")
        notes.append("图片输入，已用视觉模型转写")
        raw = transcribe([png])
        return Ingest(desensitize(normalize(raw)), p, "image", [png], notes)

    if suffix == ".docx":
        return Ingest(desensitize(normalize(_docx_text(p))), p, "docx", [], notes)

    raw = _read_text_any(p)
    return Ingest(desensitize(normalize(raw)), p, "txt", [], notes)
