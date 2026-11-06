"""平台层（ATS）自查：把 `config/rubric.yaml` 的 `ats.platforms` 变成**可执行**清单。

为什么单独一个模块：
    文本层（章节/联系方式/乱码/页数）由 check_output 的第 8.1 节校验，视觉层靠截图。
    剩下的是「同一份简历投到不同平台会被怎么拆」——这一层以前完全没有，
    用户只能靠自己试。

三条铁律（测试会守住，改坏就红）：
  1. **判不出来就 `unknown`**：既不许算 `ok`（假安心），也不许算不合格（假警报）。
  2. **只从正文判**：`dates_/sections_/contact_/keywords_/length_` 能从字里看出来；
     版面（`layout_`）、平台表单（`form_`）、文件名（`filename_`）看不出来，一律 `unknown`。
  3. **不写「会被自动淘汰」**：平台规则会变，也不是我们能验证的事实。
     免责那句（经验总结、以投递页面预览为准）由 `to_markdown()` 统一加。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output  # noqa: E402
import rubric  # noqa: E402

__all__ = ["OK", "MISSING", "UNKNOWN", "STATUS", "evaluate", "summary", "to_markdown"]

OK = "ok"
MISSING = "missing"
UNKNOWN = "unknown"
STATUS = (OK, MISSING, UNKNOWN)

# 「2024.09—2025.06」「2023/07-2024.06」「2022年9月至今」
_DATES_RANGE = re.compile(
    r"(?:19|20)\d{2}\s*[.\-/年·]\s*\d{1,2}"
    r"\s*(?:-|–|—|~|～|至|到)\s*"
    r"(?:(?:19|20)\d{2}\s*[.\-/年·]\s*\d{0,2}|至今|现在|目前|present|now)",
    re.I,
)
# 海外系统要的 MM/YYYY（09/2024）
_DATES_MMYYYY = re.compile(r"(?<![\d/])(?:0?[1-9]|1[0-2])/(?:19|20)\d{2}(?![\d/])")
# 平台按标题分组的常用词根
_STD_SECTIONS = ("教育", "实习", "工作", "项目", "技能")

# 正文里判不出来的三类，理由要写清「为什么判不出」，否则读者以为是系统坏了
_WHY_UNKNOWN = {
    "layout": "版面（分栏、表格、页眉页脚）在正文里看不到，要打开文件或看截图才判得出",
    "form": "这是投递页面上的字段，只能你本人在平台上逐项核对",
    "filename": "文件名不在正文里",
}


def _canon(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _probe_dates_range(text: str) -> tuple[str, str]:
    if _DATES_RANGE.search(text or ""):
        return OK, "正文里有起止年月"
    return MISSING, "正文里找不到「2024.09—2025.06」这类起止年月，只写年份或干脆没写"


def _probe_dates_fmt(text: str) -> tuple[str, str]:
    if _DATES_MMYYYY.search(text or ""):
        return OK, "有 MM/YYYY 格式的日期"
    if _DATES_RANGE.search(text or ""):
        return MISSING, "日期格式不是 MM/YYYY（「2024年9月」这类写法，按固定格式解析的系统可能读不到）"
    return MISSING, "正文里没有 MM/YYYY 格式的日期"


def _probe_sections(text: str) -> tuple[str, str]:
    flat = _canon(text)
    found = [s for s in _STD_SECTIONS if s in flat]
    if len(found) >= 3:
        return OK, f"找到 {len(found)} 类标准章节词根：{'、'.join(found)}"
    missing = [s for s in _STD_SECTIONS if s not in flat]
    return MISSING, (
        f"只找到 {len(found)} 类（{'、'.join(found) or '没有'}），缺 {'、'.join(missing)} 这类标准标题"
    )


def _probe_contact(text: str) -> tuple[str, str]:
    flat = _canon(text)
    if check_output.PHONE_RE.search(flat) or check_output.EMAIL_RE.search(flat):
        return OK, "手机号或邮箱在正文里"
    if "已脱敏" in flat:
        return OK, "联系方式已被脱敏（原文里有，只是发出前被替换了）"
    return MISSING, "正文里没有手机号或邮箱（写在页眉、图片里的，系统一样读不到）"


def _probe_keywords(text: str, job: str) -> tuple[str, str]:
    if not job.strip():
        return UNKNOWN, "没填目标岗位，判不出该覆盖哪些关键词"
    if job.strip() in (text or ""):
        return OK, f"正文里出现了「{job.strip()}」"
    return MISSING, f"正文里没出现「{job.strip()}」，按关键词检索的平台命中率会低"


def _probe_length(text: str) -> tuple[str, str]:
    pages = max(1, round(len(_canon(text)) / check_output.PAGES_CHARS))
    if pages <= 1:
        return OK, f"按 {check_output.PAGES_CHARS} 字/页估算约 1 页"
    return MISSING, f"按 {check_output.PAGES_CHARS} 字/页估算约 {pages} 页"


def _judge(check_id: str, text: str, job: str) -> tuple[str, str]:
    """按 check id 的探针前缀判定。前缀合法性由 rubric.ats_platforms() 保证。"""
    probe = check_id.split("_", 1)[0]
    detail = check_id[len(probe) + 1:]
    if probe == "dates":
        return _probe_dates_fmt(text) if detail == "fmt" else _probe_dates_range(text)
    if probe == "sections":
        return _probe_sections(text)
    if probe == "contact":
        return _probe_contact(text)
    if probe == "keywords":
        return _probe_keywords(text, job)
    if probe == "length":
        return _probe_length(text)
    if probe in _WHY_UNKNOWN:
        return UNKNOWN, _WHY_UNKNOWN[probe]
    raise AssertionError(f"未知探针 {probe!r}（rubric 校验漏了？）")


def evaluate(text: str, job: str = "") -> list[dict]:
    """按配置顺序给出每个平台的逐条状态。

    返回 `[{key, label, note, checks:[{id, text, status, why}]}]`。
    状态只可能是 `ok` / `missing` / `unknown`，没有第四种。
    """
    rows: list[dict] = []
    for key, label, note, checks in rubric.ats_platforms():
        items: list[dict] = []
        for cid, cn in checks.items():
            status, why = _judge(cid, text, job)
            if status not in STATUS:  # 防御：探针写错时必须炸，不许静默当 unknown
                raise AssertionError(f"{cid} 返回了非法状态 {status!r}")
            items.append({"id": cid, "text": cn, "status": status, "why": why})
        rows.append({"key": key, "label": label, "note": note, "checks": items})
    return rows


def summary(rows: list[dict]) -> dict[str, int]:
    """数一下三类各几条（报告首行用）。"""
    out = {OK: 0, MISSING: 0, UNKNOWN: 0}
    for row in rows:
        for c in row.get("checks") or []:
            if c.get("status") in out:
                out[c["status"]] += 1
    return out


_MARK = {OK: "✓", MISSING: "✗", UNKNOWN: "?"}
_WORD = {OK: "已满足", MISSING: "没满足", UNKNOWN: "判不出"}


def to_markdown(rows: list[dict], job: str = "") -> list[str]:
    """渲染成报告里的一节（不含标题，由 review.py 决定放哪）。"""
    if not rows:
        return []
    cnt = summary(rows)
    L: list[str] = []
    L.append(
        f"系统先读一遍，人再读一遍。下面按平台列它在意什么："
        f"✓ {cnt[OK]} 条已满足、✗ {cnt[MISSING]} 条没满足、? {cnt[UNKNOWN]} 条要你自己核。"
    )
    L.append("")
    if not job.strip():
        L.append("> 没填目标岗位，所以「关键词」那几条一律标成「判不出」——不猜。")
        L.append("")
    for row in rows:
        L.append(f"### {row['label']}")
        L.append("")
        L.append(f"> {row['note']}")
        L.append("")
        for c in row["checks"]:
            mark = _MARK[c["status"]]
            word = _WORD[c["status"]]
            if c["status"] == OK:
                L.append(f"- {mark} {word}：{c['text']}")
            else:
                L.append(f"- {mark} {word}：{c['text']}　——　{c['why']}")
        L.append("")
    L.append(
        "> 这些是公开信息与实测经验的总结，**不是任何平台的官方文档**；"
        "平台规则会变，最终以你在投递页面看到的预览为准。"
    )
    return L
