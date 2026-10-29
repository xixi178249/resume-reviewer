"""输入归一化：PDF / 图片 / docx / txt → 纯文本（+ 页面图片）。

设计要点：
  · 文字型 PDF / docx / txt  → 直接抽文本，零成本、默认不渲染页面图
  · 扫描版 PDF / 图片        → 仅在用户明确允许后，交给视觉模型转写
  · 文本先脱敏再往下走；页面图不做像素级脱敏，调用方必须明确告知用户
"""
from __future__ import annotations

import base64
import io
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rubric  # noqa: E402

# 与 extract_text.py 保持一致的脱敏口径
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
ID_CARD = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
DOB_LINE = re.compile(r"(出生|生日|出生年月|birth)", re.I)
DOB_FULL = re.compile(r"\d{4}\s*年\s*\d{1,2}\s*月(\s*\d{1,2}\s*日)?|\d{4}\s*年\s*生")

SUPPORTED = {".pdf", ".png", ".jpg", ".jpeg", ".webp", ".docx", ".txt", ".md"}

# 少于这个字数，就认为 PDF 是扫描件（纯文字 PDF 抽出来的通常远多于 100 字）
# rubric.ingest.min_text_chars：改阈值改配置文件，别改这里
MIN_TEXT_CHARS = int(rubric.num("ingest.min_text_chars"))


class ImageConsentRequired(RuntimeError):
    """当前输入必须发送页面图像才能识别，但用户没有明确允许。"""


@dataclass
class Ingest:
    text: str                      # 脱敏后的纯文本
    source: Path
    kind: str                      # text-pdf / scanned-pdf / image / docx / txt
    images: list[bytes] = field(default_factory=list)   # 每页 PNG，仅在明确允许时保留
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
def _pdf_text_and_pages(
    path: Path, dpi: int = 150, include_pages: bool = True
) -> tuple[str, list[bytes]]:
    """用 PyMuPDF 抽取文本；只有明确需要页面图时才渲染，避免无谓外发与开销。"""
    try:  # PyMuPDF 2.x 起 fitz 别名弃用，优先用新模块名
        import pymupdf as fitz
    except ImportError:
        import fitz  # PyMuPDF 1.x

    texts: list[str] = []
    pages: list[bytes] = []
    with fitz.open(str(path)) as doc:
        for page in doc:
            texts.append(page.get_text("text"))
            if include_pages:
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
def load(path: str | Path, ocr: bool = True, include_images: bool = True) -> Ingest:
    """解析文件。

    include_images=False 时不渲染/保留页面图；图片或扫描件仍需要 OCR 时会
    抛出 ImageConsentRequired，由调用方提示用户明确同意，绝不静默发送。
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"文件不存在：{p}")
    suffix = p.suffix.lower()
    if suffix not in SUPPORTED:
        raise ValueError(f"不支持的格式 {suffix}，支持：{', '.join(sorted(SUPPORTED))}")

    notes: list[str] = []

    if suffix == ".pdf":
        raw, pages = _pdf_text_and_pages(p, include_pages=include_images)
        if len(normalize(raw)) >= MIN_TEXT_CHARS:
            kind = "text-pdf"
            notes.append("文字型 PDF，直接读取文本层" +
                         ("；页面图仅用于用户主动选择的排版检查" if pages else "；未发送页面图"))
        else:
            kind = "scanned-pdf"
            if not include_images or not ocr:
                raise ImageConsentRequired(
                    "这是扫描版 PDF，必须把页面图像发送给模型才能识别。"
                    "请先明确允许发送页面图像。"
                )
            notes.append(f"PDF 里几乎没有文字（{len(normalize(raw))} 字），按扫描件处理，已转成 {len(pages)} 张图识别")
            raw = transcribe(pages)
        return Ingest(desensitize(normalize(raw)), p, kind, pages, notes)

    if suffix in {".png", ".jpg", ".jpeg", ".webp"}:
        if not include_images or not ocr:
            raise ImageConsentRequired(
                "图片输入必须把原始图像发送给模型才能识别。"
                "请先明确允许发送页面图像。"
            )
        png = _image_to_png(p)
        notes.append("图片输入，已用视觉模型转写")
        raw = transcribe([png])
        return Ingest(desensitize(normalize(raw)), p, "image", [png], notes)

    if suffix == ".docx":
        return Ingest(desensitize(normalize(_docx_text(p))), p, "docx", [], notes)

    raw = _read_text_any(p)
    return Ingest(desensitize(normalize(raw)), p, "txt", [], notes)
