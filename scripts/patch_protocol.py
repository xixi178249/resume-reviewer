"""对话改稿的补丁协议：模型只能交「原文 → 新文」的结构化补丁。

为什么不让模型直接重写整篇：
1. 整篇重写没法逐条确认——用户看不出自己同意了哪几处；
2. 没法判断"这句话到底是被改的还是被模型顺手换掉的"，回滚也失去意义；
3. 模型重写时最容易顺手编数字，而逐条补丁可以把守卫卡在写入之前。

协议只支持 op="replace"：new_text 为空串表示删掉这段。这已经够覆盖
"改一句 / 加量化 / 合并两行 / 删掉自评词"这些真实需求，且实现简单到可以逐行审。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

import check_output

# 一轮最多 5 条：再多用户就没法逐条读完，模型也会开始"顺手改别处"
MAX_PATCHES = 5
# 单条新文上限：一条 bullet 排到 400 字已经不是改稿而是重写了
MAX_NEW_TEXT = 400
MAX_QUOTE = 300
MAX_WHY = 120
MAX_REPLY = 800

FACT_DELTAS = ("none", "number_added", "ownership_upgrade", "scope_change", "wording")

# 只改这些字符就算「排版类」补丁——它们不改变任何事实陈述
LAYOUT_ONLY = set(" \t\u3000\xa0·•-—|｜/／,，.。、；;：:\n\r")

# 占位符三种写法都接受：项目里历史出现过 «待补：…»、[待补充…]、｛待补…｝
PLACEHOLDER_RE = re.compile(r"«待补[：:][^»]*»|\[待补充[^\]]*\]|｛待补[^｝]*｝|\{\{待补[^}]*\}\}")


class PatchFormatError(ValueError):
    """模型返回的东西不是可用的补丁结构。上层要把它变成"没听懂，换个说法"。"""


@dataclass
class Patch:
    id: str
    quote: str
    new_text: str
    why: str = ""
    fact_delta: str = "none"
    needs_user_confirm: bool = False
    op: str = "replace"
    # 由代码算出来，不采信模型自报：内容 / 排版 必须分开评价
    category: str = "content"
    problems: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "op": self.op,
            "quote": self.quote,
            "new_text": self.new_text,
            "why": self.why,
            "fact_delta": self.fact_delta,
            "needs_user_confirm": self.needs_user_confirm,
            "category": self.category,
            "problems": list(self.problems),
        }


# ---------------------------------------------------------------- 文本定位
def _norm_with_map(text: str) -> tuple[str, list[int]]:
    """压掉空白得到归一化串，同时记住每个字在原串里的下标。

    为什么不直接用 str.find：简历里同一句话常因空格/全角空格不一致而匹配不上
    （导出 PDF 时尤其常见），而归一化之后又必须能把命中位置映射回原串才能替换。
    """
    out: list[str] = []
    idx: list[int] = []
    for i, ch in enumerate(text):
        if ch in " \t\u3000\xa0\u200b":
            continue
        out.append(ch)
        idx.append(i)
    return "".join(out), idx


def locate(text: str, quote: str, occurrence: int = 1) -> tuple[int, int] | None:
    """在 text 里找到第 occurrence 次出现的 quote，返回原串下标区间。找不到返回 None。"""
    if not quote or not text:
        return None
    pos = text.find(quote)
    if pos >= 0 and occurrence <= 1:
        return pos, pos + len(quote)

    nq = re.sub(r"[ \t\u3000\xa0\u200b]+", "", quote)
    if not nq:
        return None
    norm, idx = _norm_with_map(text)
    hits = []
    start = 0
    while True:
        p = norm.find(nq, start)
        if p < 0:
            break
        hits.append(p)
        start = p + 1
        if len(hits) >= occurrence:
            break
    if len(hits) < occurrence:
        return None
    p = hits[occurrence - 1]
    return idx[p], idx[p + len(nq) - 1] + 1


def category_of(quote: str, new_text: str) -> str:
    """排版 vs 内容：把空白与标点全部抹掉后还一样，就算排版改动。

    口径必须由代码定：模型自报「排版类」然后把"参与"改成"主导"是真实会发生的。
    用"抹掉排版字符再比较"而不是逐字对齐，是为了不让「加了一个空格」被算成内容改动
    （对齐算法在长度变化时必然要额外加一条规则，而那条规则迟早会有反例）。
    """
    def core(s: str) -> str:
        return "".join(ch for ch in (s or "") if ch not in LAYOUT_ONLY)

    return "layout" if core(quote) == core(new_text) else "content"


# ---------------------------------------------------------------- 解析
def _extract_json(raw: str) -> dict:
    s = (raw or "").strip()
    if not s:
        raise PatchFormatError("模型返回了空内容")
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s)
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        i, j = s.find("{"), s.rfind("}")
        if i < 0 or j <= i:
            raise PatchFormatError("模型没有返回 JSON 对象")
        try:
            data = json.loads(s[i:j + 1])
        except json.JSONDecodeError as exc:
            raise PatchFormatError(f"JSON 解析失败：{exc.msg}（第 {exc.pos} 字符附近）")
    if not isinstance(data, dict):
        raise PatchFormatError("模型返回的是 JSON 数组或标量，不是对象")
    return data


def parse(raw: str) -> tuple[str, list[Patch], list[str]]:
    """把模型原始输出解析成 (reply, patches, problems)。结构不对就抛 PatchFormatError。"""
    data = _extract_json(raw)
    problems: list[str] = []

    reply = str(data.get("reply") or "").strip()
    if len(reply) > MAX_REPLY:
        reply = reply[:MAX_REPLY] + "…"
        problems.append(f"reply 超过 {MAX_REPLY} 字，已截断")

    raw_items = data.get("patches")
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise PatchFormatError("patches 必须是数组")

    patches: list[Patch] = []
    used_ids: set[str] = set()
    for i, item in enumerate(raw_items[:MAX_PATCHES], 1):
        if not isinstance(item, dict):
            problems.append(f"第 {i} 条补丁不是对象，已丢弃")
            continue
        pid = str(item.get("id") or f"p{i}").strip()
        if not re.match(r"^[A-Za-z0-9_-]{1,16}$", pid) or pid in used_ids:
            pid = f"p{i}"
            while pid in used_ids:
                pid += "x"
        used_ids.add(pid)

        op = str(item.get("op") or "replace").strip().lower()
        quote = str(item.get("quote") or "")
        new_text = item.get("new_text")
        new_text = "" if new_text is None else str(new_text)

        p = Patch(id=pid, op=op, quote=quote, new_text=new_text,
                  why=str(item.get("why") or "").strip()[:MAX_WHY])

        if op != "replace":
            p.problems.append(f"op={op} 暂不支持，已丢弃")
            continue
        if not quote.strip():
            p.problems.append("quote 为空，无法定位，已丢弃")
            continue
        if len(quote) > MAX_QUOTE:
            p.problems.append(f"quote 超过 {MAX_QUOTE} 字，已丢弃")
            continue
        if len(new_text) > MAX_NEW_TEXT:
            p.problems.append(f"new_text 超过 {MAX_NEW_TEXT} 字，已丢弃")
            continue

        fd = str(item.get("fact_delta") or "none").strip()
        if fd not in FACT_DELTAS:
            p.problems.append(f"fact_delta={fd} 不认识，按 wording 处理并要求确认")
            fd = "wording"
        p.fact_delta = fd

        confirm = bool(item.get("needs_user_confirm"))
        if fd != "none" and not confirm:
            # 事实有变动却标"不用确认"，是模型最常见的偷懒；这里强制扭回来
            confirm = True
            p.problems.append("fact_delta 非 none 却标了无需确认，已强制为需确认")
        p.needs_user_confirm = confirm

        p.category = category_of(quote, new_text)
        patches.append(p)

    if len(raw_items) > MAX_PATCHES:
        problems.append(f"模型给了 {len(raw_items)} 条补丁，只保留前 {MAX_PATCHES} 条")

    if not patches and not reply:
        raise PatchFormatError("既没有可用的补丁也没有回复内容")
    return reply, patches, problems


# ---------------------------------------------------------------- 事实守卫
def _added_numbers(new_text: str, resume_text: str, extra: set[str] | None = None) -> list[str]:
    bare = PLACEHOLDER_RE.sub("", new_text or "")
    return sorted(check_output.numbers_in(bare) - check_output.numbers_in(resume_text) - set(extra or ()))


def fact_issues(patch: Patch, resume_text: str, extra_numbers: set[str] | None = None) -> list[dict]:
    """写入前的守卫。返回 [{kind, severity, message}]，severity=hard 表示不许写入。

    hard：新文里出现了简历原文没有的数字（含把占位符换成真数字但原文并无依据）
    soft：权属升级、整段删除——用户确认即可，但代码必须先提醒，不能直接落盘

    extra_numbers 是「用户自己手填的数字」。这类数字原文确实没有，但来源是用户本人，
    所以放行——代价是必须在结果里标成"未经原文佐证"，让用户自己记住这件事。
    """
    issues: list[dict] = []

    added = _added_numbers(patch.new_text, resume_text, extra_numbers)
    if added:
        issues.append({
            "kind": "fabricated_number",
            "severity": "hard",
            "message": f"新文里有原文找不到的数字：{'、'.join(added)}。要么改回占位符，要么由你手工填。",
        })

    upgrade = check_output.ownership_upgrade(patch.quote, patch.new_text)
    if upgrade:
        issues.append({
            "kind": "ownership_upgrade",
            "severity": "soft",
            "message": f"把事实强度从「{upgrade}」提升了，需要你确认这事你真做过。",
        })

    if not patch.new_text.strip() and patch.quote.strip():
        issues.append({
            "kind": "delete_line",
            "severity": "soft",
            "message": "这条补丁会删掉整段内容，确认要删。",
        })

    return issues


# ---------------------------------------------------------------- 应用
def apply_patches(text: str, patches: list[Patch], extra_numbers: set[str] | None = None) -> dict:
    """按顺序应用补丁。守卫在每次替换之前跑，硬问题直接拦，不写进结果。"""
    new_text = text
    applied: list[dict] = []
    skipped: list[dict] = []
    blocked: list[dict] = []

    for p in patches:
        issues = fact_issues(p, text, extra_numbers)
        hard = [i for i in issues if i["severity"] == "hard"]
        if hard:
            blocked.append({**p.to_dict(), "reasons": hard})
            continue
        soft = [i for i in issues if i["severity"] == "soft"]
        if soft and not p.needs_user_confirm:
            blocked.append({**p.to_dict(), "reasons": soft})
            continue

        loc = locate(new_text, p.quote, 1)
        if loc is None:
            skipped.append({**p.to_dict(), "reason": "在简历里没找到这句原文，可能你已经改过它"})
            continue

        new_text = new_text[:loc[0]] + p.new_text + new_text[loc[1]:]
        applied.append({**p.to_dict(), "warnings": soft})

    return {
        "text": new_text,
        "changed": new_text != text,
        "applied": applied,
        "skipped": skipped,
        "blocked": blocked,
    }


def summarize(applied: list[dict], skipped: list[dict], blocked: list[dict]) -> str:
    """一句话交代结果。UI 与 CLI 都用它，免得两处各写一套口径。"""
    parts = []
    if applied:
        content = sum(1 for a in applied if a["category"] == "content")
        layout = len(applied) - content
        detail = f"内容 {content} 条" if content else ""
        if layout:
            detail += ("、" if detail else "") + f"排版 {layout} 条"
        parts.append(f"已改 {len(applied)} 条（{detail}）")
    if skipped:
        parts.append(f"没找到原文 {len(skipped)} 条")
    if blocked:
        parts.append(f"被守卫拦下 {len(blocked)} 条")
    return "，".join(parts) if parts else "没有可执行的改动"
