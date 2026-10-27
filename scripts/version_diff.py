"""两版简历的对比：文本差异、分数与维度变化、上次建议落实情况。

为什么需要它：原来的"分差"只在**正文哈希完全相同**时才出现，也就是同一份
文件重评两次。但用户真正想知道的是「我按你说的改了，分数涨了吗」——那是
一份**内容变了**的新版本，哈希必然不同，旧逻辑一句话都不会说。

三条硬规则（和前几个模块保持一致）：

1. 只用有数据支持的结论。读不到上一版报告就说「无法比较」，不猜。
2. 分数只有在**配置可比**时才谈增量：岗位、JD、prompt、模型、重复次数、
   是否发图任一不同，都只报文本变化，不报分差。
3. 「建议是否落实」是**推断**，不是事实。措辞用「看起来已经改过」，并明确
   写出这是按文本比对推出来的；证据不足时给 untouched / unknown，不硬猜。

只依赖标准库（difflib），不引入相似度库。
"""

from __future__ import annotations

import difflib
import re
from typing import Any

# 低于这个相似度就不认为是"同一份简历的另一个版本"，宁可不比，也不拿两份
# 不相干的简历算分差。
MIN_SIMILARITY = 0.62

# 拿去比对时最多看多少个历史版本（每次评审都要读这些 .txt，别无限翻）
MAX_CANDIDATES = 40

# 整份重写时差异行会非常多，返回给前端的列表要截断；统计数字仍保留真实值
MAX_DIFF_ITEMS = 200

# 判断"这段文字还在不在"时，太短的片段（如"负责"）没有区分度，不能当证据
MIN_PROBE = 8

DIM_LABELS = {
    "role_fit": "岗位匹配",
    "impact": "结果与量化",
    "evidence_quality": "证据质量",
    "structure": "结构可读性",
    "competitiveness": "竞争力",
}
DIM_ORDER = ["role_fit", "impact", "evidence_quality", "structure", "competitiveness"]

_STRIP_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalise(text: str) -> str:
    """只留文字与数字：标点、空白、Markdown 符号全部去掉，便于跨版本比对。

    简历改版时通常只改措辞和标点，保留标点会让相似度虚低。
    """
    return _STRIP_RE.sub("", (text or "").lower())


def lines_of(text: str) -> list[str]:
    """切成"内容行"：去掉空行与行尾空白，压缩行内连续空格。"""
    out: list[str] = []
    for raw in (text or "").splitlines():
        line = re.sub(r"[ \t\u3000]+", " ", raw).strip()
        if line:
            out.append(line)
    return out


def similarity(a: str, b: str) -> float:
    """两段简历正文的相似度 0~1。

    先按字符集合做快速上界（quick_ratio，O(n)），够高才算精确值；
    单份简历也就几千字，精确比对是毫秒级，但历史里有几十份时不值得全算。
    """
    na, nb = normalise(a), normalise(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    sm = difflib.SequenceMatcher(None, na, nb, autojunk=False)
    if sm.quick_ratio() < MIN_SIMILARITY:
        return round(sm.quick_ratio(), 4)
    return round(sm.ratio(), 4)


def _short(text: str, limit: int = 60) -> str:
    t = re.sub(r"\s+", " ", (text or "").strip())
    return t if len(t) <= limit else t[: limit - 1] + "…"


def pick_predecessor(
    candidates: list[dict],
    new_text: str,
    *,
    exclude_sha: str = "",
) -> tuple[dict | None, float]:
    """从历史里挑出"这份简历的上一版"。

    candidates 每项至少含 review_id / created_at / text / job。
    同名优先，但**不以文件名为主**——用户很可能把 简历_v2.pdf 改成 简历_终版.pdf。
    内容相似度才是主判据；同分时取更近的那份。
    """
    scored: list[tuple[float, str, dict]] = []
    for c in candidates:
        if not c.get("text"):
            continue
        if exclude_sha and c.get("content_sha256") == exclude_sha:
            continue          # 正文一模一样：那是"重评"，交给原来的分差逻辑
        sim = similarity(c["text"], new_text)
        if sim >= MIN_SIMILARITY:
            scored.append((sim, str(c.get("created_at") or ""), c))
    if not scored:
        return None, 0.0
    scored.sort(key=lambda x: (x[0], x[1]), reverse=True)
    best_sim, _ts, best = scored[0]
    return best, best_sim


def diff_lines(old_text: str, new_text: str) -> dict:
    """按行 diff。返回新增/删除/改写三类，以及统计数字。

    改写是按 replace 段一一配对的；两边行数不等时，多出来的算纯增删，
    不硬凑成"改写"，否则会给出很离谱的对照。
    """
    old, new = lines_of(old_text), lines_of(new_text)
    sm = difflib.SequenceMatcher(None, old, new, autojunk=False)
    added: list[str] = []
    removed: list[str] = []
    changed: list[dict] = []
    unchanged = 0

    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            unchanged += i2 - i1
        elif tag == "delete":
            removed.extend(old[i1:i2])
        elif tag == "insert":
            added.extend(new[j1:j2])
        else:  # replace
            a, b = old[i1:i2], new[j1:j2]
            pair = min(len(a), len(b))
            for k in range(pair):
                changed.append({"before": a[k], "after": b[k]})
            removed.extend(a[pair:])
            added.extend(b[pair:])

    return {
        "added": [_short(x, 100) for x in added],
        "removed": [_short(x, 100) for x in removed],
        "changed": [{"before": _short(c["before"], 100), "after": _short(c["after"], 100)} for c in changed],
        "stats": {
            "added": len(added),
            "removed": len(removed),
            "changed": len(changed),
            "unchanged": unchanged,
            "lines_before": len(old),
            "lines_after": len(new),
        },
    }


def compare_scores(prev_doc: dict, doc: dict) -> dict:
    """分数、五个维度、两道关、格式结论的前后对照。只做减法，不解释。"""
    def num(v: Any) -> int | None:
        return v if isinstance(v, int) else None

    def dim(d: dict, key: str) -> int | None:
        block = (d.get("dimension_scores") or {}).get(key) or {}
        return num(block.get("score"))

    before, after = num(prev_doc.get("overall_score")), num(doc.get("overall_score"))
    dims = []
    for key in DIM_ORDER:
        b, a = dim(prev_doc, key), dim(doc, key)
        dims.append({
            "key": key,
            "label": DIM_LABELS[key],
            "before": b,
            "after": a,
            "delta": (a - b) if isinstance(a, int) and isinstance(b, int) else None,
        })

    def gate(d: dict, key: str) -> bool | None:
        block = d.get(key) or {}
        if "pass" not in block:
            return None
        return bool(block.get("pass"))

    return {
        "score": {
            "before": before,
            "after": after,
            "delta": (after - before) if isinstance(after, int) and isinstance(before, int) else None,
        },
        "verdict": {
            "before": prev_doc.get("verdict"),
            "after": doc.get("verdict"),
        },
        "dims": dims,
        "gates": {
            "hr": {"before": gate(prev_doc, "gate_hr"), "after": gate(doc, "gate_hr")},
            "interview": {"before": gate(prev_doc, "gate_interview"), "after": gate(doc, "gate_interview")},
        },
        "format": {
            "before": (prev_doc.get("format_review") or {}).get("verdict"),
            "after": (doc.get("format_review") or {}).get("verdict"),
        },
    }


def _rewrite_core(rewrite: dict) -> tuple[str, str]:
    """取出一条改写的 (原文核心, 改写核心)，都做归一化。

    改写文本里常用「」标出真正被替换的片段，那段最有区分度；但片段可能只有
    两个字（如「负责」→「主导」），拿它当探针会大量误判，所以只有够长才用，
    否则退回整句改写。
    """
    raw_after = re.sub(r"[`*_>#]+", "", (rewrite.get("rewritten") or "").strip())
    quoted = [q for q in re.findall(r"[「『\"](.+?)[」』\"]", raw_after)]
    usable = [q for q in quoted if len(normalise(q)) >= MIN_PROBE]
    core_after = max(usable, key=len) if usable else raw_after
    return normalise(rewrite.get("original") or ""), normalise(core_after)


def check_applied(prev_doc: dict, new_text: str) -> list[dict]:
    """上一次的每条改写，这次看起来落实了没有。

    三态：applied（新文本里出现了改写后的说法）/ untouched（原句还在）/
    changed（原句不在了，但也没找到改写后的说法）/ unknown（片段太短，无法判断）。
    这是**推断**：一个人完全可能自己改成了别的写法，所以措辞必须留余地。
    """
    new = normalise(new_text)
    out: list[dict] = []
    for r in prev_doc.get("rewrites") or []:
        orig, after = _rewrite_core(r)
        section = (r.get("section") or "经历").strip()
        if after and after == orig:
            state = "unknown"      # 模型给的"改写"和原句一样，没法判断落没落实
        elif len(after) < MIN_PROBE and len(orig) < MIN_PROBE:
            state = "unknown"
        elif len(after) >= MIN_PROBE and after in new:
            state = "applied"
        elif len(orig) >= MIN_PROBE and orig in new:
            state = "untouched"
        elif len(orig) >= MIN_PROBE:
            state = "changed"
        else:
            state = "unknown"
        out.append({
            "section": section,
            "state": state,
            "original": _short(r.get("original"), 60),
            "rewritten": _short(r.get("rewritten"), 60),
        })
    return out


def build(
    prev_entry: dict,
    prev_doc: dict,
    prev_text: str,
    doc: dict,
    new_text: str,
    *,
    sim: float,
    score_comparable: bool,
    config_note: str = "",
) -> dict:
    """组装成给前端和报告用的对比结构。"""
    scores = compare_scores(prev_doc, doc)
    diff = diff_lines(prev_text, new_text)
    for key in ("added", "removed", "changed"):
        if len(diff[key]) > MAX_DIFF_ITEMS:
            diff[key] = diff[key][:MAX_DIFF_ITEMS]
    applied = check_applied(prev_doc, new_text)
    counts = {k: sum(1 for a in applied if a["state"] == k)
              for k in ("applied", "untouched", "changed", "unknown")}

    # 分差不可比时把分数和维度增量清空，避免前端显示一个误导性的 +N
    if not score_comparable:
        scores["score"]["delta"] = None
        for d in scores["dims"]:
            d["delta"] = None

    return {
        "has_prev": True,
        "prev": {
            "review_id": prev_entry.get("review_id"),
            "created_at": prev_entry.get("created_at") or "",
            "score": prev_doc.get("overall_score", prev_entry.get("score")),
            "verdict": prev_doc.get("verdict", prev_entry.get("verdict")),
            "job": prev_entry.get("job") or "",
        },
        "similarity": sim,
        "score_comparable": score_comparable,
        "config_note": config_note,
        **scores,
        "text": diff,
        "applied": applied,
        "applied_counts": counts,
    }


def to_markdown(cmp: dict) -> list[str]:
    """把对比渲染成报告里的一节。没上一版时返回空列表。"""
    if not cmp or not cmp.get("has_prev"):
        return []

    L: list[str] = ["## 和上一版比", ""]
    prev = cmp.get("prev") or {}
    when = (prev.get("created_at") or "").split(" ")[0]
    sim = cmp.get("similarity")
    head = f"对比的是 {when} 那份（相似度 {sim:.0%}）" if when and isinstance(sim, float) else "对比的是上一版"

    sc = cmp.get("score") or {}
    delta = sc.get("delta")
    before, after = sc.get("before"), sc.get("after")
    if isinstance(delta, int) and isinstance(after, int):
        arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "→")
        word = "没变" if delta == 0 else f"{arrow} {abs(delta)} 分"
        L.append(f"{head}：**{before} → {after}，{word}**")
    elif not cmp.get("score_comparable"):
        if isinstance(before, int) and isinstance(after, int):
            L.append(f"{head}：**{before} → {after}**")
        elif isinstance(after, int):
            L.append(f"{head}：这次 **{after} 分**（上次那份没留下可比分数）")
        else:
            L.append(head)
        L.append("")
        note = cmp.get("config_note") or "岗位、JD、模型或重复次数与上次不同"
        L.append(f"> {note}，所以不算分差，只看文本改了什么。")
    else:
        L.append(head)
    L.append("")

    # 维度只有真的变了才列，全绿或者全平就别占版面
    dims = [d for d in (cmp.get("dims") or []) if isinstance(d.get("delta"), int) and d["delta"] != 0]
    if dims:
        L.append("**维度变化**：" + "；".join(
            f"{d['label']} {d['before']}→{d['after']}（{'+' if d['delta'] > 0 else ''}{d['delta']}）"
            for d in dims
        ))
        L.append("")

    gates = cmp.get("gates") or {}
    flips = []
    for key, label in (("hr", "初筛"), ("interview", "面试")):
        g = gates.get(key) or {}
        if g.get("before") is not None and g.get("after") is not None and g["before"] != g["after"]:
            flips.append(f"{label}：{'过' if g['before'] else '不过'} → {'过' if g['after'] else '不过'}")
    fmt = cmp.get("format") or {}
    if fmt.get("before") and fmt.get("after") and fmt["before"] != fmt["after"]:
        cn = {"blocking": "会让人看不到内容", "risky": "有风险", "clean": "没问题"}
        flips.append(f"格式：{cn.get(fmt['before'], fmt['before'])} → {cn.get(fmt['after'], fmt['after'])}")
    if flips:
        L.append("**门槛变化**：" + "；".join(flips))
        L.append("")

    stats = (cmp.get("text") or {}).get("stats") or {}
    if stats:
        L.append(
            f"**正文变化**：新增 {stats.get('added', 0)} 行，删掉 {stats.get('removed', 0)} 行，"
            f"改写 {stats.get('changed', 0)} 处（原 {stats.get('lines_before', 0)} 行 → 现 {stats.get('lines_after', 0)} 行）"
        )
        changed = (cmp.get("text") or {}).get("changed") or []
        if changed:
            L.append("")
            for c in changed[:3]:
                L.append(f"- 原来：`{c.get('before', '')}`")
                L.append(f"  现在：`{c.get('after', '')}`")
        L.append("")

    counts = cmp.get("applied_counts") or {}
    if any(counts.values()):
        bits = []
        if counts.get("applied"):
            bits.append(f"{counts['applied']} 条看起来已经改过")
        if counts.get("untouched"):
            bits.append(f"{counts['untouched']} 条原句还在")
        if counts.get("changed"):
            bits.append(f"{counts['changed']} 条改成了别的写法")
        if counts.get("unknown"):
            bits.append(f"{counts['unknown']} 条无法判断")
        L.append("**上次的建议**：" + "，".join(bits) + "。")
        L.append("")
        # 先报好的：用户更关心"哪几条已经生效"，而不是被念一遍没改的
        applied = [a for a in (cmp.get("applied") or []) if a.get("state") in ("applied", "untouched")]
        applied.sort(key=lambda a: 0 if a["state"] == "applied" else 1)
        for a in applied[:3]:
            mark = "✅ 看起来已改" if a["state"] == "applied" else "◻️ 原句还在"
            L.append(f"- {mark}：{a.get('section')}｜`{_short(a.get('original'), 40)}`")
        if applied:
            L.append("")
        L.append("> 上面这一行是按文本比对推测的，不是模型判断。你如果换了个说法自己改，它看不出来。")
        L.append("")

    while L and L[-1] == "":
        L.pop()
    L.append("")
    return L
