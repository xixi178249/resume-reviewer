"""本地规则评测引擎：完全不调用模型。

为什么需要它
------------
这个站点要让别人用，但不能让别人的评审花我的钱，也不能强迫别人先去申请 key。
所以留一条"什么都能用"的路：纯规则，零成本，2 秒出结果。

它查的都是**纯文本就能机械判定**的东西：
格式可解析性（章节/联系方式/乱码/页数）、弱动词、量化缺失、长句、
结构齐全度、JD 关键词覆盖、自评词、隐私回显、结果被埋。

三条写死的边界（不是没做完，是故意的）
------------------------------------
1. **不生成改写句子。** 句子需要语言能力，规则拼出来的中文像机翻，比不改更糟。
   改写只在"大模型模式"提供。这里只给"该改哪一行、补什么事实"的脚手架，
   每条改写都带占位符并且标 needs_user_confirm=true。
2. **不假装看见排版。** 纯文本输入下 visual_level.known=False，
   格式结论只覆盖文本层，报告里明说"排版需对照原文件"。
3. **不打分猜运气。** 分数是五个维度的规则加权，不是"模型觉得"，
   one_line_summary 必须写明这是规则评测。

输出必须通过 check_output + quality_gate 两套校验——不是为了"看起来正规"，
而是这样它才能和历史记录、改版对比、导出 docx 这些功能共用同一条管线。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output as co  # noqa: E402

SCHEMA_VERSION = "v1.8"
ENGINE = "local-rules"

# 章节根词：和 check_output.SECTION_ROOTS 保持一致，避免"报告说缺、原文有"的官司
ROOTS = co.SECTION_ROOTS

# 数字口径词：出现这些说明至少交代了一半分子分母
DENOM_WORDS = ("相比", "较", "从", "到", "共", "其中", "覆盖", "样本", "用户",
               "接口", "页面", "条", "次", "人", "万", "名", "个")

# 可验证的公开信号（只数类别，不评价学校层次——那属于背景歧视）
COMPETE_SIGNALS = {
    "获奖/竞赛": ("获奖", "奖", "竞赛", "大赛", "冠军", "亚军", "一等奖", "二等奖", "三等奖"),
    "开源/GitHub": ("github", "开源", "gitee"),
    "论文/专利": ("论文", "专利", "发表", "SCI", "EI"),
    "证书": ("证书", "认证", "CET", "雅思", "托福"),
}

# 只用于"建议删除"，绝不用于评价人（check_output 规则 7）
PRIVACY_WORDS = ("年龄", "性别", "婚育", "婚姻", "政治面貌", "民族", "籍贯", "身份证", "身高", "体重")

PLACEHOLDER = "[待补充：处理量、耗时或效果幅度]"

# 这些行是"身份信息/技能清单/转录说明"，不是经历，不该参与"量化率"统计
_SKIP_BULLET = re.compile(
    r"大学|学院|本科|硕士|博士|高中|GPA|专业排名|专业：|求职意向|手机|邮箱|电话|技能|证书"
    r"|主修课程|核心课程|课程：|转录说明|人工转写|字符识别|OCR|脱敏|原件核对|识别误差"
)
_CLAUSE = re.compile(r"[，,。；;、\n]")


def _is_contact(line: str) -> bool:
    return bool(co.PHONE_RE.search(line) or co.EMAIL_RE.search(line)
                or re.search(r"手机|电话|邮箱", line))


# ---------------------------------------------------------------- 文本切分
def _lines(text: str) -> list[str]:
    return [ln.strip() for ln in (text or "").splitlines() if ln.strip()]


def _body(line: str) -> str:
    return co.strip_bullet(line)


def _bullets(text: str) -> list[str]:
    """像经历条目的行：剥掉标签后还有 10 个字以上，且不是身份/技能信息。"""
    return [ln for ln in _lines(text)
            if len(co.canon(_body(ln))) >= 10 and not _SKIP_BULLET.search(ln)]


def _found_roots(text: str) -> list[str]:
    rc = co.canon(text)
    return [r for r in ROOTS if r in rc]


def _missing_roots(text: str) -> list[str]:
    found = _found_roots(text)
    return [r for r in ROOTS if r not in found]


def _pct_numbers(text: str) -> list[tuple[str, str]]:
    """(百分比数字, 所在行) —— 顺序稳定，方便覆盖审计。"""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for ln in _lines(text):
        for n in sorted(co.numbers_in(ln)):
            if n.endswith("%") and n not in seen:
                seen.add(n)
                out.append((n, ln))
    return out


def _clause_of(line: str, num: str) -> str:
    """数字所在的那一小句——分母经常写在别的句子里，不能拿来冒充口径。"""
    for clause in _CLAUSE.split(line):
        if num and num in clause:
            return clause
    return line


def _has_denominator(line: str, num: str = "") -> bool:
    target = _clause_of(line, num) if num else line
    return any(w in target for w in DENOM_WORDS)


def _flat(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def _clip(line: str, n: int = 60) -> str:
    return line if len(line) <= n else line[:n] + "…"


def _longest_line(lines: list[str], min_len: int = 0) -> str:
    """挑一条最长的行当证据锚点；太短的（人名、日期）和联系方式都不算证据。"""
    need = min_len or co.MIN_QUOTE
    best = ""
    for ln in lines:
        if _is_contact(ln):
            continue                      # 引用里回显完整电话/邮箱会被校验器判违规
        if len(co.canon(ln)) > len(co.canon(best)):
            best = ln
    return best if len(co.canon(best)) >= need else ""


# ---------------------------------------------------------------- 五维打分
def _role_fit(text: str, job: str) -> tuple[int, str]:
    rc = co.canon(text)
    terms = [t for t in re.split(r"[\s/、，,·\-|()（）]+", job or "") if len(co.canon(t)) >= 2]
    if not terms:
        return 3, "未提供岗位，按通用标准给中间分（不猜岗位匹配度）"
    hit = sum(1 for t in terms if co.in_text(t, rc))
    rate = hit / len(terms)
    score = 4 if rate >= 0.5 else 3 if rate >= 0.3 else 2 if rate >= 0.15 else 1
    return score, f"岗位关键词命中 {hit}/{len(terms)} 个（{'、'.join(terms[:4])}…）"


def _impact(bullets: list[str]) -> tuple[int, str]:
    if not bullets:
        return 0, "没有可识别的经历条目"
    quant = [b for b in bullets if co.RESULT_SIGNAL.search(b)]
    rate = len(quant) / len(bullets)
    score = 4 if rate >= 0.6 else 3 if rate >= 0.35 else 2 if rate >= 0.15 else 1 if rate > 0 else 0
    return score, f"{len(bullets)} 条经历里 {len(quant)} 条带结果信号（{rate:.0%}）"


def _evidence_quality(text: str, weak: list[str], lines: list[str]) -> tuple[int, str]:
    ratio = len(weak) / max(1, len(lines))
    score = 4 if not weak else 3 if ratio <= 0.2 else 2 if ratio <= 0.4 else 1
    vague_pct = sum(1 for n, ln in _pct_numbers(text) if not _has_denominator(ln, n))
    if vague_pct and score > 1:
        score -= 1
    return max(0, score), f"弱动词行 {len(weak)} 条；{vague_pct} 个百分比没交代口径"


def _structure(text: str, longs: list[str]) -> tuple[int, str]:
    found, missing = _found_roots(text), _missing_roots(text)
    score = 4 if not missing else 3 if len(missing) <= 1 else 2 if len(missing) <= 2 else 1
    # 长句只在"占比够高"时扣分：中文 bullet 60~70 字很常见，
    # 单看条数会把好简历也打成结构差（实测 04 号因此从 69 掉到 56）
    lines = _lines(text)
    if longs and len(longs) >= 6 and len(longs) / max(1, len(lines)) >= 0.4 and score > 0:
        score -= 1
    return max(0, score), f"识别到 {len(found)} 类章节，缺 {len(missing)} 类；{len(longs)} 行长句"


def _competitiveness(text: str) -> tuple[int, str]:
    low = text.lower()
    hits = [name for name, words in COMPETE_SIGNALS.items() if any(w.lower() in low for w in words)]
    score = 4 if len(hits) >= 3 else 3 if len(hits) == 2 else 2 if hits else 1
    return score, "可验证的公开信号：" + ("、".join(hits) if hits else "一条都没看到")


def _score_dims(text: str, job: str) -> tuple[dict, dict]:
    lines = _lines(text)
    bullets = _bullets(text)
    weak = [ln for ln in lines if co.is_weak_verb_line(ln)]
    longs = [ln for ln in lines if len(co.canon(ln)) >= co.LONG_SENTENCE_MIN]

    parts = {
        "role_fit": _role_fit(text, job),
        "impact": _impact(bullets),
        "evidence_quality": _evidence_quality(text, weak, lines),
        "structure": _structure(text, longs),
        "competitiveness": _competitiveness(text),
    }
    dims = {k: {"score": v[0], "comment": v[1]} for k, v in parts.items()}
    overall = round(sum(dims[k]["score"] * w for k, w in co.WEIGHTS.items()) * 20)
    ctx = {"lines": lines, "bullets": bullets, "weak": weak, "longs": longs}
    return dims, {"overall": overall, **ctx}


# ---------------------------------------------------------------- 各部分构造
def _format_review(text: str) -> dict:
    found = _found_roots(text)
    missing = _missing_roots(text)
    flat = _flat(text)
    contact = bool(re.search(r"手机|电话|邮箱|e-?mail", flat, re.I)
                   or re.search(r"1[3-9]\d{9}", flat))
    garbled = len(co.GARBLED_RE.findall(text))
    pages = max(1, round(len(co.canon(text)) / co.PAGES_CHARS))

    findings: list[str] = []
    if missing:
        findings.append(
            "缺 " + "、".join(missing) + " 类章节：学生简历常见，不影响机器解析，"
            "但 HR 会先找这几段，缺了要在别处补上对应证据"
        )
    if garbled >= 50:
        findings.append(f"检测到 {garbled} 个乱码/私用区字符，机器读出来的可能是乱码")
    if not contact:
        findings.append("文本里找不到手机号或邮箱，ATS 与 HR 都无法联系你")
    if not findings:
        findings.append("文本层没发现结构性问题")

    if garbled >= 50 or not contact or len(missing) >= 3:
        verdict, risk = "blocking", "high"
    elif missing:
        verdict, risk = "risky", "medium"
    else:
        verdict, risk = "clean", "low"

    return {
        "verdict": verdict,
        "text_level": {
            "sections_found": found,
            "sections_missing": missing,
            "contact_parseable": contact,
            "garbled_char_count": min(garbled, 500),
            "estimated_pages": pages,
            "risk": risk,
            "findings": findings,
        },
        "visual_level": {
            "known": False,
            "checks": [],
            "density": "unknown",
        },
        "summary": (
            "只查了文本层（章节/联系方式/乱码/页数估算）：" + "；".join(findings)[:150]
            + "。排版需你自己对照原文件，规则引擎看不到版面"
        ),
    }


def _jd_match(text: str, jd: str) -> dict:
    rc = co.canon(text)
    if not (jd or "").strip():
        return {
            "jd_provided": False,
            "keywords": [],
            "coverage_rate": None,
            "stuffed_terms": [],
            "irrelevant_note": "没有提供 JD，只能按岗位名和通用标准看",
            "gap_summary": "贴上目标岗位的 JD 可以算出关键词覆盖率（大模型模式还能判断是否生硬堆砌）",
        }

    terms: list[str] = []
    for raw in re.split(r"[。；;，,、\n\r/|（）()\s:：]+", jd):
        t = raw.strip(" -•·，,。.、")
        if 2 <= len(co.canon(t)) <= 12 and t not in terms:
            terms.append(t)
        if len(terms) >= 10:
            break

    keywords = []
    for t in terms:
        present = co.in_text(t, rc)
        keywords.append({
            "term": t,
            "in_resume": present,
            "natural": present,          # 命中就算自然：规则引擎判不了"堆砌"
            "where": "简历原文命中" if present else "",
        })
    if not keywords:
        return {
            "jd_provided": False,
            "keywords": [],
            "coverage_rate": None,
            "stuffed_terms": [],
            "irrelevant_note": "JD 写得太短，抽不出可比对的关键词",
            "gap_summary": "把 JD 里「岗位职责/任职要求」整段贴进来，才能算覆盖率",
        }
    natural = sum(1 for k in keywords if k["natural"])
    gaps = [k["term"] for k in keywords if not k["natural"]]
    return {
        "jd_provided": True,
        "keywords": keywords,
        "coverage_rate": round(natural / len(keywords), 2),
        "stuffed_terms": [],
        "irrelevant_note": "只做字符串级覆盖判断，不判断表达是否生硬",
        "gap_summary": ("JD 里这些词简历没有对应内容：" + "、".join(gaps[:6])) if gaps else "JD 关键词都覆盖到了",
    }


def _issues(text: str, ctx: dict) -> list[dict]:
    """每条都必须能回指原文，且 fix 带可执行锚点（check_output 会验）。"""
    issues: list[dict] = []
    weak: list[str] = ctx["weak"]
    longs: list[str] = ctx["longs"]

    for line in weak[:2]:
        verb = next((v for v in co.VERB_WEAK if _body(line).startswith(v)), "参与")
        issues.append({
            "id": f"I{len(issues) + 1}",
            "dimension": "impact",
            "severity": "high" if not issues else "medium",
            "issue": f"这一行以弱动词「{verb}」开头，读起来像在旁听而不是在负责",
            "evidence": line,
            "why": "面试官看的是你独立做了什么、带来了什么结果；"
                   "「参与/协助」这类动词把自己写成了旁观者，也天然接不上结果",
            "fix": f"把「{verb}」换成你实际承担的动作，句末补结果："
                   f"「{verb}…」→「负责…，[待补充：覆盖模块数或耗时变化]」",
            "assertion": "fact",
        })

    for line in longs[:2]:
        snippet = _body(line)[:14]
        issues.append({
            "id": f"I{len(issues) + 1}",
            "dimension": "structure",
            "severity": "medium",
            "issue": "这一行太长，一眼扫不到重点",
            "evidence": line,
            "why": "初筛是扫读，一句话塞三件事，读者会跳过去后面那半句",
            "fix": f"在「{snippet}」这一句里断成两句，把结果提到前半句："
                   f"「{snippet}」→ 先说做了什么，再单独一句写结果",
            "assertion": "fact",
        })

    for line in _lines(text):
        for word in PRIVACY_WORDS:
            if word in line:
                issues.append({
                    "id": f"I{len(issues) + 1}",
                    "dimension": "structure",
                    "severity": "low",
                    "issue": f"简历里写了「{word}」这类与岗位无关的个人信息",
                    "evidence": line,
                    "why": "这类信息不参与判断，写上去只是占地方；"
                           "写错一处还会在面试里被追问，不如不写",
                    "fix": f"删掉「{word}」这一项，把腾出的空间留给你做的事",
                    "assertion": "fact",
                })
                break
        if len(issues) >= 6:
            break

    for num, line in _pct_numbers(text)[:1]:
        if _has_denominator(line, num):
            continue
        issues.append({
            "id": f"I{len(issues) + 1}",
            "dimension": "evidence_quality",
            "severity": "high",
            "issue": "这个百分比没有交代分子分母，面试官一定会问",
            "evidence": line,
            "why": "只写结论不写口径，对方只能反复追问；说不清来源的数字反而扣分",
            "fix": "在数字后面补一句口径：把「就算出来的比例」写成"
                   "「[待补充：样本量/时间范围] 中，* 除以 * 得到」",
            "assertion": "fact",
        })

    if len(_missing_roots(text)) >= 2:
        missing = _missing_roots(text)
        issues.append({
            "id": f"I{len(issues) + 1}",
            "dimension": "structure",
            "severity": "medium",
            "issue": "简历缺少 " + "、".join(missing) + " 类章节",
            "evidence": _longest_line(_lines(text)),
            "why": "HR 会按固定顺序找经历，找不到就直接跳到下一份",
            "fix": "在现有章节里补上对应证据，或新增「" + missing[0] + "」小节",
            "assertion": "fact",
        })

    # 缺实习/缺作品链接是实习简历最常见的两条硬伤，且一定能找到真行做证据
    if "实习" not in _found_roots(text):
        anchor = _longest_line(ctx["bullets"])
        if anchor:
            issues.append({
                "id": f"I{len(issues) + 1}",
                "dimension": "role_fit",
                "severity": "high",
                "issue": "简历里没有实习经历段落，而投的是实习岗",
                "evidence": anchor,
                "why": "实习岗筛的就是「有没有在真实项目里交付过」；"
                       "只有课程项目时，别人默认你还需要带教",
                "fix": f"把「{_clip(anchor, 14)}」这类课程/竞赛项目改写成有交付、有协作、有上线痕迹的表述，"
                       "并补[待补充：项目持续周期与验收方式]",
                "assertion": "fact",
            })

    low_text = text.lower()
    if not any(k in low_text for k in ("github", "gitee", "作品集", "个人网站", "http")):
        anchor = _longest_line([ln for ln in _lines(text) if "技能" in ln or "工具" in ln]
                               or ctx["bullets"])
        if anchor:
            issues.append({
                "id": f"I{len(issues) + 1}",
                "dimension": "competitiveness",
                "severity": "medium",
                "issue": "没有可点开的代码仓库或作品链接",
                "evidence": anchor,
                "why": "面试官想先看一眼你写的东西再决定聊不聊；没有链接就只能靠简历措辞说服他",
                "fix": "在页首补一行仓库地址，把[待补充：项目名]里能公开的部分整理成 README",
                "assertion": "fact",
            })

    if not issues:
        anchor = _longest_line(ctx["bullets"] or _lines(text))
        if anchor:
            issues.append({
                "id": "I1",
                "dimension": "impact",
                "severity": "low",
                "issue": "文本层没查到硬问题，但经历里的成果还可以写得更具体",
                "evidence": anchor,
                "why": "规则引擎只能查出机械问题；写得够不够好需要语义判断，那部分要模型来看",
                "fix": f"给「{_clip(anchor, 14)}」这类条目逐条补上[待补充：可核对的数字]",
                "assertion": "fact",
            })

    # 证据短的（人名、日期行）换成能引用的行，避免「引用不在原文」
    fixed: list[dict] = []
    for it in issues:
        if len(co.canon(it["evidence"])) < co.MIN_QUOTE:
            alt = _longest_line(_lines(text))
            if not alt:
                continue
            it = dict(it, evidence=alt)
        fixed.append(it)
    return fixed[:6]


def _rewrites(text: str, ctx: dict) -> list[dict]:
    """只做"补事实"的脚手架，不造句子、不造数字（数字只在占位符里由本人填）。"""
    out: list[dict] = []
    seen: set[str] = set()

    def add(line: str, section: str, why: str) -> None:
        key = co.canon(line)
        if not key or key in seen or len(key) < co.MIN_QUOTE or _is_contact(line):
            return
        seen.add(key)
        body = _body(line).rstrip("。；;，,、 ").strip()
        if not body:
            return
        out.append({
            "section": section,
            "original": line,
            "rewritten": body + "，" + PLACEHOLDER,
            "rationale": why,
            "fact_delta": "wording",
            "needs_user_confirm": True,
        })

    # 优先级：没结果信号的条目 → 最长的条目 → 其它够长的行（意向/技能/教育）
    for line in [b for b in ctx["bullets"] if not co.RESULT_SIGNAL.search(b)]:
        add(line, "经历", "原句只写了做什么，没有可核对的结果；补上结果类型，面试官才有追问的入口")
    for line in sorted(ctx["bullets"], key=lambda b: -len(co.canon(b))):
        add(line, "经历", "这条已经写了结果，但还可以补上规模或时间范围，让人判断这个结果有多难得")
    for line in _lines(text):
        if len(co.canon(line)) < co.MIN_QUOTE:
            continue
        if re.search(r"意向|目标|求职", line):
            add(line, "求职意向", "意向只写了岗位名，补上可到岗时间与期望方向，HR 少问一轮")
        elif re.search(r"技能|工具|语言|框架", line):
            add(line, "技能", "技能写成清单没有证据，补一句用它做过什么，才可信")
        else:
            add(line, "基本信息", "这一行还可以补上可核对的规模或时间范围")
        if len(out) >= 2:
            break
    return out[:2]


def _number_audit(text: str) -> list[dict]:
    audit = []
    for num, line in _pct_numbers(text)[:3]:
        clear = _has_denominator(line, num)
        audit.append({
            "number": num,
            "context": line[:200],
            "source_clarity": "partial" if clear else "vague",
            "denominator": "原文给出了一部分口径" if clear else "原文没写清分子分母",
            "measured_or_estimated": "unknown",
            "questions": [f"这个 {num} 的分子分母分别是什么？", "是用什么方式统计出来的？"],
            "fix": "补一句「[待补充：样本范围] 内的比例」",
        })
    return audit


def _showcase(text: str, ctx: dict, format_review: dict) -> dict:
    found = _found_roots(text)
    if "项目" in found and "实习" not in found:
        positioning = "以项目和课程作业为主的候选人，没有实习经历"
    elif "实习" in found and "项目" in found:
        positioning = "有实习也有项目，但两者之间的主线还不清楚"
    elif "实习" in found:
        positioning = "以实习经历为主的候选人"
    else:
        positioning = "经历线索偏弱，读不出一句话定位"
    has_thread = bool(found) and ("项目" in found or "实习" in found)

    buried = []
    for line in ctx["bullets"]:
        if not co.numbers_in(line):
            continue
        head = line.strip()[:co.HEAD_CHARS]
        if co.RESULT_LABEL.search(head):
            continue
        buried.append({"line": line, "result_position": "结果挤在句子后半段"})
        if len(buried) >= 2:
            break

    self_praise = []
    for word in co.SELF_PRAISE:
        for line in _lines(text):
            if word in line:
                self_praise.append(_clip(line, 40))
                break
    return {
        "positioning": positioning,
        "has_thread": has_thread,
        "weak_verb_lines": ctx["weak"][:8],
        "buried_results": buried,
        "self_praise": self_praise[:8],
        "note": "弱动词/自评词/结果位置都由规则直接扫原文得到，不是模型判断",
    }


def _followup_risks(text: str, ctx: dict) -> list[dict]:
    risks = []
    for num, line in _pct_numbers(text)[:2]:
        if _has_denominator(line, num):
            continue
        risks.append({
            "line": line,
            "questions": [f"{num} 是怎么算出来的？", "基线是多少？", "这个指标为什么重要？"],
            "risk": "high" if not risks else "medium",
        })
    if not risks and ctx["bullets"]:
        risks.append({
            "line": ctx["bullets"][0],
            "questions": ["这段经历里你自己负责的是哪一部分？", "结果是怎么衡量的？"],
            "risk": "medium",
        })
    keep = [r for r in risks if len(co.canon(r["line"])) >= co.MIN_QUOTE][:3]
    if keep:
        return keep
    # 兜底：整份简历没有一条够长可引用的行时，仍然要给一条可追问的风险
    fallback = _longest_line([ln for ln in _lines(text) if not _is_contact(ln)], min_len=1)
    return [{
        "line": fallback or text.strip()[:200],
        "questions": ["这段经历里你自己负责的是哪一部分？", "结果是怎么衡量的？"],
        "risk": "medium",
    }]


def _next_actions(dims: dict, format_review: dict) -> list[dict]:
    """第一条必须是检查器允许的那几种目标之一（硬伤 / 最低分维度 / 格式阻断）。"""
    lowest = min(co.WEIGHTS, key=lambda k: (dims[k]["score"], -co.WEIGHTS[k]))
    acts: list[dict] = []
    if format_review["verdict"] == "blocking":
        acts.append({
            "action": "先修格式：让文字能被机器和你自己一眼读出来",
            "targets": "format_review",
            "est_minutes": 10,
            "expected_gain": "verdict_upgrade",
        })
    first_by_dim = {
        "impact": {"action": "挑 2 条经历，把结果补成可核对的数字（没有数字就写清范围）",
                   "targets": "impact", "est_minutes": 20, "expected_gain": "verdict_upgrade"},
        "role_fit": {"action": "把岗位要求里的关键词逐条对上你的经历，缺的在经历里补证据",
                     "targets": "role_fit", "est_minutes": 15, "expected_gain": "verdict_upgrade"},
        "evidence_quality": {"action": "给每个百分比补一句分子分母，说不清就换成能说清的数字",
                             "targets": "evidence_quality", "est_minutes": 15, "expected_gain": "risk_down"},
        "structure": {"action": "把 60 字以上的长句断成两句，结果提到前半句",
                      "targets": "structure", "est_minutes": 10, "expected_gain": "score_up"},
        "competitiveness": {"action": "把获奖/证书/开源作品的名称和年份补全，别只写「多次获奖」",
                            "targets": "competitiveness", "est_minutes": 10, "expected_gain": "score_up"},
    }
    if not acts or acts[0].get("targets") != "format_review":
        acts.insert(0, first_by_dim[lowest])
    for key, item in first_by_dim.items():
        if key != lowest and len(acts) < 3:
            acts.append(item)
    return acts[:3]


def _before_submit(issues: list[dict]) -> list[str]:
    out = ["核实改写里的「待补充」都已填成真实数字，不要留占位符"]
    if any(it["dimension"] == "evidence_quality" for it in issues):
        out.append("确认每个数字都能说出分子分母和统计时间")
    out.append("确认联系方式与投递渠道一致，邮箱别写成学校临时邮箱")
    return [x[:80] for x in out[:4]]


def _missing_info(text: str, ctx: dict, jd: dict) -> list[str]:
    out = []
    low = text.lower()
    if "github" not in low and "gitee" not in low:
        out.append("没有代码仓库或作品链接，技术岗会被追问「在哪能看」")
    if not any(co.RESULT_SIGNAL.search(b) for b in ctx["bullets"]):
        out.append("经历里几乎没有可核对的结果数字")
    if not jd.get("jd_provided"):
        out.append("没提供目标岗位 JD，无法算关键词覆盖率")
    if len(_missing_roots(text)) >= 2:
        out.append("缺少 " + "、".join(_missing_roots(text)) + " 类章节")
    return out[:6]


# ---------------------------------------------------------------- 组装
def build(text: str, job: str = "", jd: str = "", image_known: bool = False) -> dict:
    """把一份简历原文变成符合 v1.8 契约的评审结果（全部由规则生成）。"""
    if not (text or "").strip():
        raise ValueError("简历原文为空")

    dims, ctx = _score_dims(text, job)
    overall = ctx["overall"]
    format_review = _format_review(text)
    if image_known:                      # 有截图也不假装能判版面，规则引擎不做视觉
        format_review["summary"] = (
            "规则引擎不做视觉判断，只查了文本层；排版请对照原文件由大模型模式确认"
        )
    issues = _issues(text, ctx)
    rewrites = _rewrites(text, ctx)
    jd_match = _jd_match(text, jd)
    showcase = _showcase(text, ctx, format_review)
    followup = _followup_risks(text, ctx)
    audit = _number_audit(text)
    heads = ctx["bullets"] or _lines(text)

    doc = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE,
        "gate_hr": {"pass": False, "reason": ""},
        "gate_interview": {"pass": False, "reason": ""},
        "three_second_test": {
            "job_target_clear": bool(job) and co.in_text(job, co.canon(text)),
            "strongest_point": _clip(heads[0], 78) if heads else "简历里没有可识别的经历条目",
            "readable_in_3s": len(ctx["longs"]) <= 2 and bool(_found_roots(text)),
            "long_sentences": ctx["longs"][:3],
        },
        "format_review": format_review,
        "showcase": showcase,
        "jd_match": jd_match,
        "verdict": co.expected_verdict(overall),
        "overall_score": overall,
        "one_line_summary": "",
        "dimension_scores": dims,
        "hard_blockers": [],
        "issues": issues,
        "rewrites": rewrites,
        "number_audit": audit,
        "followup_risks": followup,
        "missing_info": _missing_info(text, ctx, jd_match),
        "next_actions": [],
        "before_submit": _before_submit(issues),
    }
    doc["gate_hr"] = {
        "pass": co.gate_hr_expected(doc),
        "reason": (
            "格式会让人看不到内容，初筛直接不过" if co.format_is_blocking(doc)
            else f"岗位匹配 {dims['role_fit']['score']}/5、结果量化 {dims['impact']['score']}/5，"
                 f"按初筛公式需要 role_fit≥3 且 impact≥2"
        ),
    }
    doc["gate_interview"] = {
        "pass": co.gate_interview_expected(doc),
        "reason": f"证据质量 {dims['evidence_quality']['score']}/5"
                  f"（需要 ≥3），高风险追问 {sum(1 for f in followup if f['risk'] == 'high')} 条（最多 1）",
    }
    doc["next_actions"] = _next_actions(dims, format_review)
    # 分数是规则算出来的，摘要必须说清楚，别让人误以为这是模型评审
    doc["one_line_summary"] = (
        f"规则引擎评测（未调用大模型）：{overall} 分，{len(issues)} 条可机械确认的问题，"
        f"先按下面第 1 条改；要语义级改写请用大模型模式"
    )[:250]
    return doc


def review_text(text: str, job: str = "", jd: str = "", image_known: bool = False):
    """返回 (doc, 门禁结果)。调用方据此决定能不能展示。"""
    import quality_gate

    doc = build(text, job, jd, image_known=image_known)
    gate = quality_gate.evaluate(doc, text)
    return doc, gate


def main() -> int:
    ap = argparse.ArgumentParser(description="规则引擎评测（不调用模型）")
    ap.add_argument("path", help="简历文本文件（.txt）")
    ap.add_argument("--job", default="", help="目标岗位")
    ap.add_argument("--jd", default="", help="JD 原文")
    ap.add_argument("--json", action="store_true", help="打印完整 JSON")
    args = ap.parse_args()

    text = Path(args.path).read_text(encoding="utf-8")
    doc, gate = review_text(text, args.job, args.jd)
    if args.json:
        print(json.dumps(doc, ensure_ascii=False, indent=2))
    else:
        import review as review_mod

        print(review_mod.render(doc, Path(args.path), args.job, ["规则引擎"], [], text,
                                quality=gate if gate.get("eligible") else None))
    print(f"\n[gate] eligible={gate.get('eligible')} "
          f"blockers={len(gate.get('blockers') or [])} "
          f"warnings={len(gate.get('warnings') or [])}", file=sys.stderr)
    for p in (gate.get("blockers") or [])[:5]:
        print(f"  - [{p.get('code')}] {p.get('message')}", file=sys.stderr)
    return 0 if gate.get("eligible") else 1


if __name__ == "__main__":
    raise SystemExit(main())
