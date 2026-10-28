"""对评审输出做确定性校验，并计算与人工标准的指标。

用法：
    python scripts/check_output.py outputs/sample-stack-nosignal.mock.json
    python scripts/check_output.py outputs/ --json

三类检查：
  1. 结构校验   —— 必填字段、量表范围、verdict 与分数是否一致
  2. 事实校验   —— evidence 是否逐字出自简历原文；改写里是否凭空出现数字
  3. 规则校验   —— 视觉信息是否越界猜测、数字溯源是否覆盖、动作优先级是否正确

v1.1 的检查全部在这里实现，prompt 里不再重复要求模型自检。
WEIGHTS 是权重的唯一真源：改权重只能改这里，prompt 里已注明。

退出码：0 全部通过；1 存在 ERROR；2 参数/文件问题。
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

WEIGHTS = {
    "role_fit": 0.25,
    "impact": 0.25,
    "evidence_quality": 0.20,
    "structure": 0.15,
    "competitiveness": 0.15,
}
VERDICT_BANDS = [(80, "strong_pass"), (65, "pass"), (50, "borderline"), (0, "reject")]
FILLER = [
    "建议加强", "可以进一步", "建议进一步", "更加突出", "进一步丰富",
    "可以适当", "有待提升", "建议优化", "注意细节",
]
TARGETS = set(WEIGHTS) | {"hard_blockers", "ats_check", "format_review"}
GAINS = {"verdict_upgrade", "score_up", "risk_down", "polish"}
PARSE_RISKS = {"none", "low", "medium", "high", "unknown"}
# 与 schema 保持一致；partial = 说了一半
CLARITY = {"clear", "partial", "vague", "unclear"}

# 无截图时，这些词一出现就说明模型在"看见"它看不见的东西。
# 注意：不要收录「加粗」「字体」这类词——它们常出现在"初筛只看加粗标题"这种
# 解释招聘方行为的元叙述里，会误伤。视觉类判断由 layout_check.known 兜底。
VISUAL_CLAIM = re.compile(
    r"照片|头像|图片|配图|插画|分栏|双栏|两栏|水印|"
    r"右上角|左上角|右下角|左下角|页眉|页脚|装饰图|配色方案"
)

# v1.2 展示力：弱动词、结果信号、自评词——三者都是纯文本可判
WEAK_VERBS = ("参与", "负责", "协助", "配合", "跟进", "帮助", "完成", "使用", "学习")
RESULT_SIGNAL = re.compile(r"\d|提升|降低|下降|提高|缩短|减少|优化到|从.{1,12}到")
SELF_PRAISE = (
    "精通", "熟练掌握", "学习能力强", "责任心强", "抗压",
    "沟通能力强", "吃苦耐劳", "具备良好的",
)
BULLET = re.compile(r"^[\s•·●▪◆\-*]*")

# v1.4：权属强度（改写把「参与」升级成「主导」属于改事实，不属于改措辞）
VERB_WEAK = ("参与", "协助", "配合", "跟进", "帮助", "学习", "了解", "接触")
VERB_MID = ("负责", "完成", "使用", "实现", "编写", "做了")
VERB_STRONG = ("主导", "设计", "牵头", "独立", "搭建", "重构", "优化", "推动", "落地", "架构", "规划")

# 禁止输出结果预测
# 只禁「对求职结果下注」；"大概率会被追问"这类过程判断不算
PROBABILITY = re.compile(
    r"通过率|录取概率|offer ?概率|通过概率|拿到 ?offer 的概率|概率(?:约|为)\s*\d"
    r"|(?:大概率|很可能)(?:能|会|可以)(?:进|通过|拿到|录取|上岸)"
)

# 隐私：完整联系方式不得回显
PHONE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+")
GARBLED_RE = re.compile(r"[\ufffd\uE000-\uF8FF]")
PAGES_CHARS = 900

# v1.3：两关判定是推导出来的，不是自由填的
GATE_ROLE_MIN = 3
GATE_IMPACT_MIN = 2
GATE_EQ_MIN = 3
GATE_HIGH_RISK_MAX = 1

# 结果信号只看句首，不看数字落点（v1.3 修正 6.2 的判定逻辑）
# "持续优化内容框架"是动作不是结果，所以"优化"要带落点才算信号
RESULT_LABEL = re.compile(
    r"提升|降低|下降|提高|优化(?:到|至|后)|从.{1,12}到|达到|%|成果|节省|缩短"
)
HEAD_CHARS = 12
# 长句阈值：与 prompt「检验 2」保持一致，改一处必须改两处
LONG_SENTENCE_MIN = 60

# fix 必须有可执行锚点：原文片段 / 「」替换文本 / 占位符
ANCHOR_PAT = re.compile(r"「[^」]{4,}」|\[待补充[^\]]{0,40}\]")

# v1.5 客观性：三级断言
ASSERTIONS = {"fact", "inference", "judgment"}
# 这些结论默认只能是「推断」，除非原文逐字写明
SENSITIVE_CLAIM = re.compile(
    r"专升本|自考|成人教育|非全日制|第一学历|统招|延期毕业|空档期|跨专业|转行|在职"
)
# 推断级措辞
HEDGE = re.compile(r"疑似|需确认|请核对|看起来|可能|大概|待确认")

NUM = re.compile(r"\d+(?:\.\d+)?%?")
# 省略号：用来把引用的两端接起来，需要分段校验，所以单独一个常量
ELLIPSIS = re.compile(r"…+|\.{3,}")
PUNCT = re.compile(r"[\s，。、；：！？,.;:!?·…—\-—\u3000\u00a0\"'“”‘’()（）\[\]【】]")
CJK = re.compile(r"[\u4e00-\u9fff]")

MIN_QUOTE = 6
MAX_QUOTE = 200


def in_text(term: str, rc: str) -> bool:
    """判断一个（可能是中文复合词的）术语在简历里有没有对应内容。

    整词优先；不行就切开；再不行看二元片段覆盖率 ——
    「大模型微调」在只写了「模型」「微调」的简历里算有证据；
    「向量检索」在完全没提「向量」的简历里不算。
    """
    t = canon(term)
    if not t:
        return True
    if t in rc:
        return True
    parts = [p for p in re.split(r"[\s/、，,·\-+|]+", term) if len(canon(p)) >= 2]
    if parts and any(canon(p) in rc for p in parts):
        return True
    if 3 <= len(t) <= 10:
        grams = [t[i : i + 2] for i in range(len(t) - 1)]
        hit = sum(1 for g in grams if g in rc)
        if grams and hit / len(grams) >= 0.5:
            return True
    return False


def has_anchor(fix: str, evidence: str) -> bool:
    """fix 里有没有可执行锚点：原文片段 / 「」替换 / 占位符。"""
    fix = fix or ""
    if ANCHOR_PAT.search(fix):
        return True
    ev, f = canon(evidence), canon(fix)
    if len(ev) < MIN_QUOTE:
        return False
    for i in range(0, len(ev) - MIN_QUOTE + 1):
        if ev[i : i + MIN_QUOTE] in f:
            return True
    return False


def format_is_blocking(review: dict) -> bool:
    """格式是否真会让人看不到内容。

    不采信模型自报的 verdict —— 只要致命项判了 ok=false，或 verdict 自己说 blocking，就算。
    （v1.7 教训：只信 verdict 的话，模型把它写成 clean 就能绕过 gate_hr。）
    """
    fr = review.get("format_review") or {}
    if fr.get("verdict") == "blocking":
        return True
    checks = (fr.get("visual_level") or {}).get("checks") or []
    return any(
        c.get("item") in BLOCKING_ITEMS and c.get("ok") is False for c in checks
    )


def gate_hr_expected(review: dict) -> bool:
    """v1.7 起：格式 blocking 直接判初筛不过——看不见就等于没投。"""
    dims = review.get("dimension_scores") or {}
    role = (dims.get("role_fit") or {}).get("score")
    impact = (dims.get("impact") or {}).get("score")
    if format_is_blocking(review):
        return False
    return (
        not (review.get("hard_blockers") or [])
        and isinstance(role, int)
        and role >= GATE_ROLE_MIN
        and isinstance(impact, int)
        and impact >= GATE_IMPACT_MIN
    )


def gate_interview_expected(review: dict) -> bool:
    dims = review.get("dimension_scores") or {}
    eq = (dims.get("evidence_quality") or {}).get("score")
    high = sum(1 for f in (review.get("followup_risks") or []) if f.get("risk") == "high")
    return isinstance(eq, int) and eq >= GATE_EQ_MIN and high <= GATE_HIGH_RISK_MAX


# 模板站常见写法：【标签】正文 / 流程初识：正文 —— 判弱动词前必须先剥掉标签，
# 否则整行以「【」「流程」开头，永远匹配不到行首动词。
LABEL = re.compile(r"^(?:【[^】]{1,14}】|\[[^\]]{1,14}\]|(?![\d\s])[^，。；：、\s]{1,8}[：:])")


def strip_bullet(line: str) -> str:
    body = BULLET.sub("", line or "").strip()
    # 可能连着两层：• 【标签】正文
    for _ in range(2):
        new_body = LABEL.sub("", body).strip()
        if new_body == body:
            break
        body = new_body
    return body


# 「项目概述/背景/简介」这类行本来就是在交代背景，不该按成就描述去要求结果
CONTEXT_LABEL = re.compile(r"^(?:项目概述|概述|背景|简介|项目简介|项目描述|项目背景)")


# PDF 的分栏/表格被拍平后，一行经历会被拆成碎片：「完成」「2024.06-2024.09」
# 「产品经理」各占一行。碎片不是"经历行"，按弱动词漏报去拦只会挡住整份结果。
FRAGMENT_NOISE = re.compile(r"[\s\d./\-—_~～年月日:：、，,()（）%]")
WEAK_LINE_MIN_REST = 4  # 动词后面至少还有这么多实字，才算一行真正的经历


def is_weak_verb_line(line: str) -> bool:
    """以弱动词开头，且整行没有任何结果信号。

    背景/概述类行（项目概述：参与开发…)是交代来龙去脉的，不算弱动词行，
    否则每份简历的"项目概述"都会被报成问题，变成噪音。
    分栏/表格抽出的碎片（单独一行「完成」）也不算。
    """
    raw = BULLET.sub("", line or "").strip()
    label = LABEL.match(raw)
    if label and CONTEXT_LABEL.match(label.group(0).strip("【】：: ")):
        return False
    body = strip_bullet(line)
    if not body.startswith(WEAK_VERBS):
        return False
    verb = next(v for v in WEAK_VERBS if body.startswith(v))
    rest = FRAGMENT_NOISE.sub("", body[len(verb):])
    if len(rest) < WEAK_LINE_MIN_REST:
        return False
    return not RESULT_SIGNAL.search(body)


# ---------------------------------------------------------------- 工具
def canon(text: str) -> str:
    """把空白和中文标点全部抹掉，只比较"字面内容"，避免提取噪音造成误判。"""
    return PUNCT.sub("", text or "")


def _quote_hit(q_canon: str, resume_canon: str) -> bool:
    """已经去掉空白和标点的引用，是否出现在同样处理过的简历里。"""
    if not q_canon:
        return False
    if q_canon in resume_canon:
        return True
    # 长引用允许丢掉首尾各 5%~15% 后仍命中（模型常见行为：多打了一个词）
    for trim in (0.05, 0.1, 0.15):
        n = int(len(q_canon) * trim)
        if len(q_canon) - 2 * n >= 12 and q_canon[n : len(q_canon) - n] in resume_canon:
            return True
    return False


def _quote_coverage(q_canon: str, resume_canon: str) -> tuple[float, int, int]:
    """把引用切成若干段「原文里逐字能查到」的连续片段，返回 (覆盖率, 段数, 最短段长)。

    模型常见的两种拼接：把两条 bullet 用逗号直接连起来（中间没有省略号），
    或者带个结尾省略号。逐字查整句必然查不到，但每一段都是真的。
    覆盖率低于 0.85 或最短段不足 6 字，仍判编造——防止拿碎片拼出原文没有的句子。
    """
    total, i, pieces = 0, 0, []
    while i < len(q_canon):
        for L in range(len(q_canon) - i, 5, -1):
            if q_canon[i : i + L] in resume_canon:
                pieces.append(L)
                total += L
                i += L
                break
        else:
            i += 1
    if not pieces:
        return 0.0, 0, 0
    return total / len(q_canon), len(pieces), min(pieces)


def find_quote(quote: str, resume_canon: str) -> bool:
    """逐字命中，或允许模型截取中间片段/跨换行。

    允许把简历里**不相邻的两段**接成一句引用（"独立完成200+项目需求沟通，累计输出200+汇报材料"），
    前提是每一段都能逐字命中。理由：这是表达方式问题不是编造，整份结果因此被拒，
    用户什么也拿不到——比放过它更糟。
    但覆盖率必须 ≥85% 且每段 ≥6 字，防止拿碎片拼出原文里没有的句子。
    """
    if _quote_hit(canon(quote), resume_canon):
        return True
    parts = [canon(p) for p in ELLIPSIS.split(quote or "") if p and p.strip()]
    if len(parts) >= 2 and all(len(p) >= 6 and _quote_hit(p, resume_canon) for p in parts):
        return True
    coverage, pieces, shortest = _quote_coverage(canon(quote), resume_canon)
    return coverage >= 0.85 and pieces >= 2 and shortest >= 6


def numbers_in(text: str) -> set[str]:
    return set(NUM.findall(text or ""))


def expected_verdict(score: int) -> str:
    for floor, name in VERDICT_BANDS:
        if score >= floor:
            return name
    return "reject"


# ---------------------------------------------------------------- 校验
def check(review: dict, resume_text: str) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    sv = review.get("schema_version")
    stats = {
        "version": (
            sv if sv in {"v1.3", "v1.4", "v1.5", "v1.6", "v1.7", "v1.8"} else
            "v1.2" if "showcase" in review
            else "v1.1" if "layout_check" in review or "number_audit" in review
            else "v1.0"
        ),
        "evidence_total": 0,
        "evidence_hit": 0,
        "rewrites": 0,
        "invented_numbers": [],
        "numbers_unaudited": [],
        "unclear_numbers": 0,
        "visual_claims_without_image": 0,
        "weak_verb_missed": 0,
        "self_praise_missed": 0,
        # 可自动修好的偏差：由 quality_gate 依次修，不拦用户看结果
        "repairs": [],
    }
    repairs = stats["repairs"]

    resume_canon = canon(resume_text)
    if not resume_canon:
        errors.append("简历原文为空，无法做事实校验")

    is_v12 = stats["version"] in {"v1.2", "v1.3", "v1.4", "v1.5", "v1.6", "v1.7", "v1.8"}
    is_v11 = stats["version"] in {"v1.1", "v1.2", "v1.3", "v1.4", "v1.5", "v1.6", "v1.7", "v1.8"}
    is_v13 = stats["version"] in {"v1.3", "v1.4", "v1.5", "v1.6", "v1.7", "v1.8"}
    is_v14 = stats["version"] in {"v1.4", "v1.5", "v1.6", "v1.7", "v1.8"}
    is_v15 = stats["version"] in {"v1.5", "v1.6", "v1.7", "v1.8"}
    is_v16 = stats["version"] in {"v1.6", "v1.7", "v1.8"}

    # --- 1. 结构
    if is_v11:
        required = [
            "gate_hr", "gate_interview", "three_second_test",
            "format_review" if is_v16 else ("ats_check" if is_v14 else "layout_check"),
            "verdict", "overall_score", "one_line_summary", "dimension_scores",
            "hard_blockers", "issues", "rewrites", "number_audit",
            "followup_risks", "missing_info", "next_actions",
        ]
        if is_v12:
            required.insert(4, "showcase")
        if is_v14:
            required.insert(5, "jd_match")
            required.append("before_submit")
        if is_v13:
            required.insert(0, "schema_version")
    elif "gate_hr" in review or "followup_risks" in review:
        required = [
            "gate_hr", "gate_interview", "three_second_test", "verdict",
            "overall_score", "one_line_summary", "dimension_scores",
            "hard_blockers", "issues", "rewrites", "followup_risks",
            "missing_info", "next_actions",
        ]
    else:
        required = [
            "verdict", "overall_score", "one_line_summary", "dimension_scores",
            "hard_blockers", "issues", "rewrites", "missing_info",
            "interview_questions", "next_actions",
        ]
    for key in required:
        if key not in review:
            errors.append(f"缺少字段：{key}")
    if errors:
        return {"file_ok": False, "errors": errors, "warnings": warnings, "stats": stats}

    dims = review.get("dimension_scores") or {}
    for key in WEIGHTS:
        item = dims.get(key)
        if not isinstance(item, dict):
            errors.append(f"dimension_scores.{key} 缺失或格式错误")
            continue
        score = item.get("score")
        if not isinstance(score, int) or not 0 <= score <= 5:
            errors.append(f"dimension_scores.{key}.score 必须是 0~5 的整数，实际 {score!r}")
    total = review.get("overall_score")
    if not isinstance(total, int) or not 0 <= total <= 100:
        errors.append(f"overall_score 必须是 0~100 的整数，实际 {total!r}")

    if not errors and total is not None:
        calc = round(sum(dims[k]["score"] * w for k, w in WEIGHTS.items()) * 20)
        if abs(calc - total) > 2:
            errors.append(f"分数不自洽：按维度加权应为 {calc}，模型给出 {total}")
        want = expected_verdict(total)
        if review.get("verdict") != want:
            errors.append(f"verdict 与分数不一致：{total} 分应为 {want}，模型给出 {review.get('verdict')}")

    # --- 2. 事实：所有引用逐字出自原文
    quotes: list[tuple[str, str]] = []
    for b in review.get("hard_blockers") or []:
        if b.get("evidence"):
            quotes.append((f"hard_blockers[{b.get('issue', '')[:12]}]", b["evidence"]))
    for i, it in enumerate(review.get("issues") or []):
        if it.get("evidence"):
            quotes.append((f"issues[{i}]", it["evidence"]))
    for i, rw in enumerate(review.get("rewrites") or []):
        if rw.get("original"):
            quotes.append((f"rewrites[{i}].original", rw["original"]))
    for i, fq in enumerate(review.get("followup_risks") or []):
        if fq.get("line"):
            quotes.append((f"followup_risks[{i}].line", fq["line"]))
    for i, na in enumerate(review.get("number_audit") or []):
        if na.get("context"):
            quotes.append((f"number_audit[{i}].context", na["context"]))
    for i, ls in enumerate((review.get("three_second_test") or {}).get("long_sentences") or []):
        quotes.append((f"three_second_test.long_sentences[{i}]", ls))
    if is_v12:
        sc = review.get("showcase") or {}
        for i, wl in enumerate(sc.get("weak_verb_lines") or []):
            quotes.append((f"showcase.weak_verb_lines[{i}]", wl))
        for i, br in enumerate(sc.get("buried_results") or []):
            if isinstance(br, dict) and br.get("line"):
                quotes.append((f"showcase.buried_results[{i}].line", br["line"]))
        for i, sp in enumerate(sc.get("self_praise") or []):
            quotes.append((f"showcase.self_praise[{i}]", sp))

    stats["evidence_total"] = len(quotes)
    for where, q in quotes:
        if find_quote(q, resume_canon):
            stats["evidence_hit"] += 1
        elif resume_canon:
            errors.append(f"引用不在原文中（疑似编造）：{where} → {q[:40]}…")
        # 引用长度：太长会让模型为了凑字数截取无关内容
        if not where.startswith("showcase.self_praise"):
            n = len(canon(q))
            if n and n < MIN_QUOTE:
                warnings.append(f"{where} 只有 {n} 字（去标点），低于 {MIN_QUOTE} 字下限")
            elif n > MAX_QUOTE + 20:
                warnings.append(f"{where} 长 {n} 字，超过 {MAX_QUOTE} 字上限，考虑截取关键部分")

    # --- 2b. 两关判定必须与维度自洽（v1.3：两关是推导出来的）
    if is_v13 and not errors:
        gh = (review.get("gate_hr") or {}).get("pass")
        gi = (review.get("gate_interview") or {}).get("pass")
        exp_hr, exp_gi = gate_hr_expected(review), gate_interview_expected(review)
        if gh is not exp_hr:
            why = "格式会让人看不到内容（format blocking）→ 初筛必不过；" if format_is_blocking(review) else ""
            errors.append(
                f"gate_hr 与维度不自洽：{why}按公式 "
                f"(格式不 blocking 且 hard_blockers 空 且 role_fit≥{GATE_ROLE_MIN} 且 impact≥{GATE_IMPACT_MIN}) "
                f"应为 {exp_hr}，实际 {gh}"
            )
        if gi is not exp_gi:
            errors.append(
                f"gate_interview 与维度不自洽：按公式 "
                f"(evidence_quality≥{GATE_EQ_MIN} 且 high 风险≤{GATE_HIGH_RISK_MAX}) "
                f"应为 {exp_gi}，实际 {gi}"
            )

    # --- 3. 事实：改写不得凭空造数字
    resume_nums = numbers_in(resume_text)
    for i, rw in enumerate(review.get("rewrites") or []):
        stats["rewrites"] += 1
        new = numbers_in(rw.get("rewritten", "")) - resume_nums
        if new:
            stats["invented_numbers"].append({"index": i, "numbers": sorted(new)})
            errors.append(f"改写 [{i}] 出现了简历里没有的数字 {sorted(new)}（编造数据，必须改为占位符）")
        if canon(rw.get("rewritten", "")) == canon(rw.get("original", "")):
            warnings.append(f"rewrite[{i}] 与原句相同，没有实际改写")
    if stats["rewrites"] < 2:
        errors.append(f"rewrites 至少 2 条，实际 {stats['rewrites']}")

    # --- 4. 可执行性：不看禁用词，看有没有可执行锚点
    for i, it in enumerate(review.get("issues") or []):
        fix = it.get("fix", "")
        if not has_anchor(fix, it.get("evidence", "")):
            errors.append(
                f"issues[{i}].fix 没有任何可执行锚点（空话）：{fix[:24]}… "
                "必须含原文片段 / 「」替换文本 / [待补充：xxx] 之一"
            )
    for i, b in enumerate(review.get("hard_blockers") or []):
        if not has_anchor(b.get("fix", ""), b.get("evidence", "")):
            errors.append(
                f"hard_blockers[{i}].fix 没有任何可执行锚点（空话）：{(b.get('fix') or '')[:24]}…"
            )
    if not review.get("issues"):
        errors.append("issues 不能为空")

    # --- 5. 语言
    if CJK.search(review.get("one_line_summary", "")) is None:
        warnings.append("摘要不是中文，检查是否语言漂移")

    # --- 5b. 规则7：歧视性信息不能拿来评价（只警告，避免误伤"与岗位无关"这类正当说明）
    for i, it in enumerate(review.get("issues") or []):
        text = f"{it.get('issue','')} {it.get('why','')}"
        m = DISCRIMINATORY.search(text)
        if m:
            warnings.append(
                f"issues[{i}] 出现了「{m.group(0)}」——这类信息不得用于评价候选人；"
                "如确需提及，只能说明「与岗位无关，建议删除」"
            )

    if is_v11:
        _check_v11(review, resume_text, resume_nums, errors, warnings, stats)
    if is_v12:
        _check_showcase(review, resume_text, errors, warnings, stats)
    if is_v14:
        _check_v14(review, resume_text, errors, warnings, stats)
    if is_v15:
        _check_v15(review, errors, warnings, stats)
    if is_v16:
        _check_v16(review, errors, warnings, stats)

    # --- 6. 高分不该硬凑缺陷（v1.0 也适用）
    if review.get("verdict") == "strong_pass" and (review.get("hard_blockers") or []):
        errors.append(
            "verdict=strong_pass 却写了 hard_blockers，说明并未真正认为可强推："
            "要么删掉缺陷，要么把分数降到 pass（这是'给好简历硬找茬'的信号）"
        )

    stats["evidence_hit_rate"] = (
        round(stats["evidence_hit"] / stats["evidence_total"], 3) if stats["evidence_total"] else None
    )
    return {"file_ok": not errors, "errors": errors, "warnings": warnings, "stats": stats}


def _check_v11(review, resume_text, resume_nums, errors, warnings, stats) -> None:
    """v1.1 专有规则：视觉越界、数字溯源、动作优先级、长句主张。"""

    # 5.1 视觉层一致性（v1.4 起视觉信息在 ats_check.visual 里）
    lc, where_prefix = visual_block(review)
    known = lc.get("known")
    parse_risk = lc.get("parse_risk")
    if not isinstance(known, bool):
        errors.append(f"{where_prefix}.known 必须是布尔值")
    if parse_risk not in PARSE_RISKS:
        errors.append(f"{where_prefix}.parse_risk 取值非法：{parse_risk!r}")
    if known is False:
        if parse_risk != "unknown":
            errors.append(
                f"{where_prefix}.known=false 时 parse_risk 必须是 unknown，实际 {parse_risk!r}"
                "（没有截图就不该判断视觉解析风险）"
            )
        # 没有图却"看见"了排版 → 幻觉
        visual_fields = [
            ("one_line_summary", review.get("one_line_summary", "")),
            (f"{where_prefix}.findings", " ".join(lc.get("findings") or [])),
        ]
        for b in review.get("hard_blockers") or []:
            visual_fields.append(("hard_blockers.issue", b.get("issue", "")))
        for i, it in enumerate(review.get("issues") or []):
            visual_fields.append((f"issues[{i}].issue", it.get("issue", "")))
            visual_fields.append((f"issues[{i}].why", it.get("why", "")))
        for k, v in (review.get("dimension_scores") or {}).items():
            if isinstance(v, dict):
                visual_fields.append((f"dimension_scores.{k}.comment", v.get("comment", "")))
        for where, text in visual_fields:
            m = VISUAL_CLAIM.search(text or "")
            if m:
                stats["visual_claims_without_image"] += 1
                errors.append(
                    f"无截图却声称看到视觉元素（{m.group(0)}）：{where} → {text[:40]}…"
                    " 纯文本输入下这属于幻觉，必须删除或改为已知信息"
                )

    # 5.2 长句主张是否真的长
    for i, ls in enumerate((review.get("three_second_test") or {}).get("long_sentences") or []):
        if len(canon(ls)) < LONG_SENTENCE_MIN:
            warnings.append(
                f"three_second_test.long_sentences[{i}] 只有 {len(canon(ls))} 字（去标点），"
                f"达不到 {LONG_SENTENCE_MIN} 字的长句标准，可能是误判"
            )

    # 5.3 数字溯源
    audit = review.get("number_audit") or []
    audited_nums: set[str] = set()
    for i, na in enumerate(audit):
        num = (na.get("number") or "").strip()
        if num:
            audited_nums |= numbers_in(num)
            for part in numbers_in(num):
                if part not in resume_nums:
                    errors.append(f"number_audit[{i}] 审计的数字 {part!r} 在简历中不存在")
        clarity = na.get("source_clarity")
        if clarity not in CLARITY:
            errors.append(f"number_audit[{i}].source_clarity 取值非法：{clarity!r}")
        if clarity in {"vague", "unclear", "partial"}:
            stats["unclear_numbers"] += 1
            if not na.get("questions"):
                errors.append(f"number_audit[{i}] 判定来源不清，却没给出面试官会问的问题")
        moe = na.get("measured_or_estimated")
        if moe is not None and moe not in {"measured", "estimated", "unknown"}:
            errors.append(f"number_audit[{i}].measured_or_estimated 取值非法：{moe!r}")
        if not na.get("questions"):
            errors.append(f"number_audit[{i}] 缺少 questions")

    # 覆盖要求：简历里有百分比，就必须审到其中至少 3 个（有 3 个才要求 3 个）。
    # 简历里没有百分比时，number_audit 为空是正确答案——不再逼模型拿年份凑数。
    pct_nums = {n for n in resume_nums if n.endswith("%")}
    need = min(3, len(pct_nums))
    covered = len({n for n in pct_nums if n in audited_nums})
    if covered < need:
        missing = sorted(pct_nums - audited_nums)
        stats["numbers_unaudited"] = missing
        errors.append(
            f"数字溯源覆盖不足：简历里有 {len(pct_nums)} 个百分比指标"
            f"（{'、'.join(sorted(pct_nums))}），至少需审计 {need} 个，"
            f"实际只审计了 {covered} 个，漏了：{'、'.join(missing)}"
        )
    if not pct_nums and not audit:
        stats["numbers_unaudited"] = []
    elif not pct_nums and audit:
        warnings.append("简历里没有百分比指标，number_audit 可以留空，当前填了内容，确认不是凑数")

    # 5.4 动作优先级：初筛过不了就别先改排版
    actions = review.get("next_actions") or []
    if not actions:
        errors.append("next_actions 不能为空")
        return
    for i, a in enumerate(actions):
        if not isinstance(a, dict):
            errors.append(f"next_actions[{i}] 必须是对象（含 action/targets/est_minutes/expected_gain）")
            continue
        if a.get("targets") not in TARGETS:
            errors.append(f"next_actions[{i}].targets 取值非法：{a.get('targets')!r}")
        if a.get("expected_gain") not in GAINS:
            errors.append(f"next_actions[{i}].expected_gain 取值非法：{a.get('expected_gain')!r}")
        mins = a.get("est_minutes")
        if not isinstance(mins, int) or not 1 <= mins <= 30:
            errors.append(f"next_actions[{i}].est_minutes 必须是 1~30 的整数，实际 {mins!r}")

    # 只有动作结构本身不可用时才跳过优先级判断；est_minutes 等错误不该阻断这项检查
    if any(not isinstance(a, dict) for a in actions):
        return
    if any(a.get("targets") not in TARGETS for a in actions if isinstance(a, dict)):
        return
    lowest = min(WEIGHTS, key=lambda k: (dims_score(review, k), -WEIGHTS[k]))
    gate_hr_fail = (review.get("gate_hr") or {}).get("pass") is False
    has_blocker = bool(review.get("hard_blockers") or [])
    first = actions[0].get("targets")
    # 格式阻断时，可接受的动作多一种（修格式）。否则 v1.6 与 v1.1 两条规则会互斥，
    # 模型无论怎么填都会违反其中一条。
    allowed = {"hard_blockers", lowest}
    blocking = format_is_blocking(review)
    if blocking:
        allowed.add("format_review")
    if gate_hr_fail or has_blocker:
        if first not in allowed:
            errors.append(
                f"优先级错误：初筛未通过（或存在 hard_blockers）时，next_actions[0] 应修 "
                f"{'format_review / ' if blocking else ''}hard_blockers 或最低分维度 {lowest}，"
                f"实际修的是 {first}"
            )
    elif blocking:
        if first != "format_review":
            errors.append(
                f"格式阻断时 next_actions[0] 必须先修格式（format_review），实际修的是 {first}"
            )
    elif first != lowest:
        warnings.append(
            f"next_actions[0] 修的是 {first}，但最低分维度是 {lowest}；"
            "确认这是不是最能让 verdict 提档的一条"
        )


def dims_score(review: dict, key: str) -> int:
    item = (review.get("dimension_scores") or {}).get(key) or {}
    score = item.get("score")
    return score if isinstance(score, int) else 99


def _check_showcase(review, resume_text, errors, warnings, stats) -> None:
    """v1.2 展示力规则：弱动词漏报、自评词漏报、结果位置误报。

    这三项都是纯文本可机械判定的，所以不交给模型自觉，全部在这里核。
    """
    sc = review.get("showcase") or {}
    if not isinstance(sc, dict):
        errors.append("showcase 必须是对象")
        return

    for key in ("positioning", "has_thread", "weak_verb_lines", "buried_results", "self_praise"):
        if key not in sc:
            errors.append(f"showcase.{key} 缺失")

    # --- 6.1 弱动词行：模型报的必须真是弱动词行；真的弱动词行不能被漏报
    truth = [ln.strip() for ln in resume_text.splitlines() if is_weak_verb_line(ln)]
    reported = sc.get("weak_verb_lines") or []
    for i, line in enumerate(reported):
        if not is_weak_verb_line(line):
            warnings.append(
                f"showcase.weak_verb_lines[{i}] 其实不算「弱动词开头且无结果」"
                f"（可能该行已含结果信号）：{line[:30]}…"
            )
    reported_canon = {canon(x) for x in reported}
    # issues / hard_blockers 的 evidence 里提过同一行，也算已经交付给用户了，
    # 不必强制在两个字段里重复登记
    for it in (review.get("issues") or []):
        if it.get("evidence"):
            reported_canon.add(canon(it["evidence"]))
    for b in (review.get("hard_blockers") or []):
        if b.get("evidence"):
            reported_canon.add(canon(b["evidence"]))
    missed = [ln for ln in truth if not any(canon(ln) in r or r in canon(ln) for r in reported_canon if r)]
    stats["weak_verb_missed"] = len(missed)
    if missed:
        # 漏报是"少说了一条"，不是"说错了"。挡住整份结果让用户白等一分钟，
        # 代价远大于收益，所以降级为 warning，只统计质量。
        warnings.append(
            f"漏报弱动词行 {len(missed)} 条（简历里以"
            f"{'/'.join(WEAK_VERBS[:4])}…开头且无任何结果的行）："
            + "；".join(ln[:24] for ln in missed[:3])
        )

    # --- 6.3 自评词：出现在简历里就必须被指出，一条都不许漏
    issue_ev = " ".join((it.get("evidence") or "") for it in review.get("issues") or [])
    reported_sp = " ".join(sc.get("self_praise") or [])
    hay = reported_sp + " " + issue_ev
    sp_missed = [w for w in SELF_PRAISE if w in resume_text and w not in hay]
    stats["self_praise_missed"] = len(sp_missed)
    if sp_missed:
        errors.append(
            f"漏报自评词：{'、'.join(sp_missed)}。这类词零信息量，必须写进 showcase.self_praise"
        )

    # --- 6.2 结果位置（v1.3 判据改成了「句首有没有结果信号」，不再看数字落点）
    for i, br in enumerate(sc.get("buried_results") or []):
        if not isinstance(br, dict):
            errors.append(f"showcase.buried_results[{i}] 必须是对象（含 line / result_position）")
            continue
        line = br.get("line") or ""
        # 标签本身就是结果信号（「项目成果：」），所以原始行和剥离行分别看
        hit_head = next(
            (h for h in (line.strip()[:HEAD_CHARS], strip_bullet(line)[:HEAD_CHARS])
             if RESULT_LABEL.search(h)),
            "",
        )
        if hit_head:
            warnings.append(
                f"showcase.buried_results[{i}] 误报：句首「{hit_head}」已含结果信号，"
                "按 v1.3 规则不算被埋（那属于长句问题，归检验 2）"
            )
        elif not numbers_in(line):
            warnings.append(
                f"showcase.buried_results[{i}] 标为「结果被埋」，但该行没有任何数字：{line[:30]}…"
            )
        if not br.get("result_position"):
            errors.append(f"showcase.buried_results[{i}] 缺少 result_position")

    # --- 6.4 主线
    positioning = (sc.get("positioning") or "").strip()
    if len(positioning) < 4:
        errors.append("showcase.positioning 太短，必须给出「一句话记住这个人是什么」")
    if sc.get("has_thread") is True and review.get("verdict") == "reject":
        warnings.append("has_thread=true 但 verdict=reject，确认主线判断是否与整体结论矛盾")


# 格式块：v1.6 用 format_review，v1.4/1.5 用 ats_check，更早用 layout_check
VISUAL_ITEMS = (
    "图片化文字", "分栏或表格", "字号与行距", "留白密度", "是否超出1页",
    "照片占位", "页眉页脚占用", "对齐与间距一致", "章节命名标准", "联系方式可提取",
)
# 这五项一旦不 ok，就是「看不见」，直接判 blocking
BLOCKING_ITEMS = {"图片化文字", "分栏或表格", "是否超出1页", "章节命名标准", "联系方式可提取"}
SECTION_ROOTS = ("教育", "实习", "项目", "技能", "获奖")
# 规则 7：这些只能用于「无关信息」提示，不能拿来评价候选人
DISCRIMINATORY = re.compile(
    r"性别|年龄|籍贯|婚育|婚姻状况|生育|身高|体重|政治面貌|民族|双非|二本|三本"
)


def text_level_block(review: dict) -> tuple[dict, dict]:
    fr = review.get("format_review") or {}
    if fr:
        return fr, (fr.get("text_level") or {})
    ats = review.get("ats_check") or {}
    return ats, (ats.get("text_level") or {})


def visual_block(review: dict) -> tuple[dict, str]:
    fr = review.get("format_review") or {}
    if fr:
        v = fr.get("visual_level") or {}
        known = v.get("known")
        synth = {
            "known": known,
            "parse_risk": "unknown" if known is False else "low",
            "findings": [c.get("note", "") for c in (v.get("checks") or []) if not c.get("ok")],
        }
        return synth, "format_review.visual_level"
    ats = review.get("ats_check") or {}
    if ats:
        return (ats.get("visual") or {}), "ats_check.visual"
    return (review.get("layout_check") or {}), "layout_check"


def _check_v16(review, errors, warnings, stats) -> None:
    """v1.6/v1.7 格式优先：10 项判满、白名单、结论映射、动作顺序。"""
    fr = review.get("format_review") or {}
    v = fr.get("visual_level") or {}
    tl = fr.get("text_level") or {}
    known = v.get("known")
    checks = v.get("checks") or []
    verdict = fr.get("verdict")
    density = v.get("density")

    # ---- 章节 vs 缺失：不能同一段既报存在又报缺失
    found = tl.get("sections_found") or []
    missing = tl.get("sections_missing") or []
    found_roots = {r for r in SECTION_ROOTS if any(r in str(x) for x in found)}
    for m in missing:
        hit = [r for r in SECTION_ROOTS if r in str(m) and r in found_roots]
        if hit:
            errors.append(
                f"format_review.text_level：{hit} 相关章节既出现在 sections_found（{found}），"
                f"又出现在 sections_missing（{m}）——同一段不能两处都报"
            )

    if verdict not in {"blocking", "risky", "clean"}:
        errors.append(f"format_review.verdict 取值非法：{verdict!r}")

    if known is False:
        if checks:
            errors.append("format_review.visual_level.known=false，但 checks 非空（没有截图就不该有视觉结论）")
        if density not in {None, "unknown"}:
            errors.append(
                f"format_review.visual_level.known=false 时 density 必须是 unknown，实际 {density!r}"
            )
        # 没看到版面却判「看不见」，必须有文本层证据。
        # （真实事故：一份 .txt 简历被判 blocking，导致 gate_hr 直接不过，
        #   而它的 10 项视觉检查是空的——结论凭空而生。）
        if verdict == "blocking":
            evidence: list[str] = []
            if tl.get("contact_parseable") is False:
                evidence.append("联系方式提取不到")
            if int(tl.get("garbled_char_count") or 0) >= 50:
                evidence.append("乱码 ≥50 字")
            missing_roots = {
                r for r in SECTION_ROOTS
                if any(r in str(m) for m in (tl.get("sections_missing") or []))
            }
            if len(missing_roots) >= 3:
                evidence.append(f"关键章节缺 {len(missing_roots)} 类")
            if not evidence:
                errors.append(
                    "format_review.verdict=blocking，但这份简历没有截图"
                    "（visual_level.known=false）——判「看不见内容」必须给出文本层证据："
                    "联系方式提取不到 / 乱码 ≥50 字 / 关键章节缺 3 类以上，"
                    "当前三项都不成立。请改判 risky，或补上证据"
                )
    elif known is True:
        # ① 必须判满 10 项
        if len(checks) != len(VISUAL_ITEMS):
            errors.append(
                f"有截图时必须判满 {len(VISUAL_ITEMS)} 项，实际 {len(checks)} 项"
                "（固定 10 项一项都不能少）"
            )
        items = []
        for i, c in enumerate(checks):
            item = c.get("item")
            if item not in VISUAL_ITEMS:
                errors.append(
                    f"format_review.visual_level.checks[{i}].item={item!r} 不在固定 10 项里（禁止自造检查项）"
                )
            items.append(item)
            note = (c.get("note") or "").strip()
            # ② 视觉判断允许不确定
            if c.get("certainty") == "uncertain" and "确认" not in note:
                warnings.append(
                    f"checks[{i}]「{item}」标了 uncertain，note 里应写明「请对照原文件确认」"
                )
            if c.get("ok") is False and len(note) < 4:
                warnings.append(f"checks[{i}]「{item}」判了有问题，但 note 太短，没说清看到什么")
        dup = {x for x in items if items.count(x) > 1}
        if dup:
            errors.append(f"format_review.visual_level.checks 有重复项：{sorted(dup)}")

        # ③ verdict 只卡两端，中间地带允许自由裁量
        #    （之前用"非致命 ≥2 才算 risky"，那个 2 是拍脑袋的，真实数据上误判过）
        fatal_bad = [c for c in checks if c.get("item") in BLOCKING_ITEMS and c.get("ok") is False]
        minor_bad = [c for c in checks if c.get("item") not in BLOCKING_ITEMS and c.get("ok") is False]
        if fatal_bad and verdict != "blocking":
            errors.append(
                f"有 {len(fatal_bad)} 个致命项不 ok（{'、'.join(sorted({c['item'] for c in fatal_bad}))}），"
                f"format_review.verdict 必须是 blocking，实际 {verdict!r}"
            )
        elif not fatal_bad and not minor_bad and density != "high" and verdict != "clean":
            errors.append(
                f"10 项全部通过、density={density}，format_review.verdict 应为 clean，实际 {verdict!r}"
            )
        # 有非致命问题但没到 blocking：clean 和 risky 都算合理，不判错

        # ③b blocking 必须有致命项撑着。没有就说"看不见"，会直接让初筛不过，
        #     这是最贵的一句结论，不能只凭感觉。
        if verdict == "blocking" and not fatal_bad:
            errors.append(
                "format_review.verdict=blocking，但 10 项里没有任何致命项判为不 ok"
                f"（致命项只有这 5 项：{'、'.join(sorted(BLOCKING_ITEMS))}）——"
                "只有非致命问题时应判 risky"
            )

        # ④ density 与「留白密度」必须一致
        pad = next((c for c in checks if c.get("item") == "留白密度"), None)
        if pad and density in {"high", "low"} and pad.get("ok") is True:
            errors.append("density 判为 high/low，但「留白密度」这一项 ok=true，二者矛盾")
        if pad and density == "ok" and pad.get("ok") is False:
            errors.append("density 判为 ok，但「留白密度」这一项判了有问题，二者矛盾")

    # ⑤ 格式阻断时，结论不许写成"可以投"
    if format_is_blocking(review):
        if review.get("verdict") == "strong_pass":
            errors.append(
                "格式阻断时 verdict 不能是 strong_pass——"
                "一份解析不出来、看不到内容的简历，内容分再高也不能强推"
            )
        summary = review.get("one_line_summary", "")
        if not any(k in summary for k in ("格式", "排版", "解析", "看不见", "看不到")):
            # 能靠一句前缀救回来，就不该让用户重跑一分钟。
            stats["repairs"].append("format_in_summary")
            warnings.append(
                "格式阻断时 one_line_summary 没点明格式问题，已在开头自动补一句"
                "（否则用户只看分数就去投）"
            )

    # ⑥ 动作顺序：格式真要命时必须先修（不看模型自报的 verdict）
    actions = review.get("next_actions") or []
    fatal = {c.get("item") for c in checks if c.get("ok") is False} & BLOCKING_ITEMS
    effective_blocking = format_is_blocking(review)
    if effective_blocking and actions and isinstance(actions[0], dict):
        if actions[0].get("targets") != "format_review":
            reason = "存在致命项 " + "、".join(sorted(fatal)) if fatal else "verdict=blocking"
            stats["repairs"].append("format_first_action")
            warnings.append(
                f"格式层会让人看不到内容（{reason}），"
                f"第一条动作本该是 format_review（实际 {actions[0].get('targets')!r}），"
                "已自动把「先修格式」提到第一位——先改内容再排版等于白改"
            )


def verb_level(text: str) -> tuple[int, str]:
    """取句首动词的强度等级。数字越大越强。"""
    body = strip_bullet(text or "")
    for lvl, group in ((3, VERB_STRONG), (2, VERB_MID), (1, VERB_WEAK)):
        for v in group:
            if body.startswith(v):
                return lvl, v
    return 0, ""


def ownership_upgrade(original: str, rewritten: str) -> str:
    """改写是否把事实强度提升了。返回 '参与→主导' 这样的描述，没有则空串。

    ⚠️ 只看句首动词会误伤：原文「参与项目开发，负责后端接口编写」里本来就有「负责」，
    模型只是删掉了"参与"两个字，并没有升级事实。所以强动词必须**是原文没有的**才算升级。
    """
    lo, vo = verb_level(original)
    ln, vn = verb_level(rewritten)
    if not (vo and vn and ln > lo):
        return ""
    if vn and vn in strip_bullet(original):
        return ""  # 原文已经这么说了
    return f"{vo}→{vn}"


def _check_v14(review, resume_text, errors, warnings, stats) -> None:
    """v1.4 规则：ATS 文本层、JD 覆盖、权属升级、隐私回显、禁止概率。"""
    rc = canon(resume_text)

    # ---- 8.1 ATS 文本层：章节必须真的存在
    _fr, tl = text_level_block(review)
    for i, s in enumerate(tl.get("sections_found") or []):
        if s and canon(s) not in rc:
            errors.append(f"ats_check.text_level.sections_found[{i}] 「{s}」在简历原文中找不到")

    # 注意：简历里常见「电 话」「邮 箱」这种字间空格，先压掉空白再匹配
    flat = re.sub(r"\s+", "", resume_text)
    has_contact = bool(
        PHONE_RE.search(flat)
        or EMAIL_RE.search(flat)
        or re.search(r"手机|电话|邮箱|e-?mail", flat, re.I)
    )
    if isinstance(tl.get("contact_parseable"), bool) and tl["contact_parseable"] != has_contact:
        warnings.append(
            f"ats_check.text_level.contact_parseable={tl['contact_parseable']}，"
            f"但文本里{'能' if has_contact else '不能'}提取到手机号或邮箱（脱敏后可能不一致，请人工确认）"
        )

    real_garbled = len(GARBLED_RE.findall(resume_text))
    reported = tl.get("garbled_char_count")
    if isinstance(reported, int) and abs(reported - real_garbled) > 2:
        warnings.append(
            f"ats_check.text_level.garbled_char_count={reported}，原文实际约 {real_garbled} 个乱码字符"
        )

    expect_pages = max(1, round(len(rc) / PAGES_CHARS))
    got_pages = tl.get("estimated_pages")
    if isinstance(got_pages, int) and abs(got_pages - expect_pages) > 1:
        warnings.append(
            f"ats_check.text_level.estimated_pages={got_pages}，按 {PAGES_CHARS} 字/页估算应约 {expect_pages} 页"
        )

    if (tl.get("sections_missing") or []) and not (tl.get("findings") or []):
        warnings.append(
            "报了缺失章节，却没在 findings 里说明影响"
            "（例如「无实习经历，学生简历常见，不影响解析」）"
        )

    # ---- 7. JD 覆盖：不能被声称"有"，但原文找不到
    jd = review.get("jd_match") or {}
    kws = jd.get("keywords") or []
    if jd.get("jd_provided") is False and kws:
        errors.append("jd_match.jd_provided=false，但 keywords 非空（无 JD 时关键词应为空）")
    for i, k in enumerate(kws):
        term = (k.get("term") or "").strip()
        if not term:
            errors.append(f"jd_match.keywords[{i}].term 为空")
            continue
        # in_resume 的语义是「简历里有对应内容」，不是「这个字符串逐字出现」。
        # 三层判断：整词 → 分隔符切块 → 二元片段覆盖率 ≥50%
        present = in_text(term, rc)
        if k.get("in_resume") and not present:
            errors.append(f"jd_match.keywords[{i}] 声称简历里有「{term}」，但原文连关键词块都找不到")
        if k.get("natural") and k.get("in_resume") is not True:
            errors.append(f"jd_match.keywords[{i}]「{term}」被判为有证据（natural=true），就必须 in_resume=true")
        if present and k.get("natural") is False and term not in (jd.get("stuffed_terms") or []):
            warnings.append(
                f"jd_match.keywords[{i}]「{term}」在简历里存在但被判为无证据，"
                "应同时写进 stuffed_terms（否则用户不知道怎么补）"
            )
    if kws and jd.get("coverage_rate") is not None:
        calc = round(sum(1 for k in kws if k.get("natural") is True) / len(kws), 2)
        rep = jd.get("coverage_rate")
        if isinstance(rep, (int, float)) and abs(rep - calc) > 0.2:
            errors.append(f"jd_match.coverage_rate 不自洽：按 keywords 应为 {calc}，给出 {rep}")
    terms = {(k.get("term") or "") for k in kws}
    for t in jd.get("stuffed_terms") or []:
        if t not in terms:
            warnings.append(f"jd_match.stuffed_terms 里的「{t}」不在 keywords 中，无法追溯")

    # ---- 权属升级：改动词就是改事实
    for i, rw in enumerate(review.get("rewrites") or []):
        fd = rw.get("fact_delta")
        if fd not in {"none", "wording", "ownership_upgrade", "number_added"}:
            errors.append(f"rewrites[{i}].fact_delta 取值非法：{fd!r}")
        up = ownership_upgrade(rw.get("original", ""), rw.get("rewritten", ""))
        if up and fd != "ownership_upgrade":
            errors.append(
                f"rewrites[{i}] 把事实强度从「{up}」升级了，fact_delta 必须是 ownership_upgrade，实际 {fd!r}"
            )
        if up and rw.get("needs_user_confirm") is not True:
            errors.append(
                f"rewrites[{i}] 存在权属升级「{up}」，needs_user_confirm 必须为 true"
                "（让本人确认'我真的是主导吗'）"
            )
        if fd == "ownership_upgrade" and rw.get("needs_user_confirm") is not True:
            errors.append(f"rewrites[{i}] fact_delta=ownership_upgrade 但 needs_user_confirm 不是 true")

    # ---- 隐私：不得回显完整联系方式
    pii_fields: list[tuple[str, str]] = []
    for i, rw in enumerate(review.get("rewrites") or []):
        pii_fields.append((f"rewrites[{i}].original", rw.get("original", "")))
        pii_fields.append((f"rewrites[{i}].rewritten", rw.get("rewritten", "")))
    for i, it in enumerate(review.get("issues") or []):
        pii_fields.append((f"issues[{i}].evidence", it.get("evidence", "")))
        pii_fields.append((f"issues[{i}].why", it.get("why", "")))
    for i, fq in enumerate(review.get("followup_risks") or []):
        pii_fields.append((f"followup_risks[{i}].line", fq.get("line", "")))
    for i, na in enumerate(review.get("number_audit") or []):
        pii_fields.append((f"number_audit[{i}].context", na.get("context", "")))
    pii_fields.append(("one_line_summary", review.get("one_line_summary", "")))
    for where, text in pii_fields:
        if PHONE_RE.search(text or "") or EMAIL_RE.search(text or ""):
            errors.append(f"{where} 回显了完整联系方式，必须替换为 [已脱敏] 后再引用")

    # ---- 禁止结果预测
    hay = " ".join([
        review.get("one_line_summary", ""),
        " ".join(it.get("issue", "") + it.get("why", "") for it in review.get("issues") or []),
    ])
    m = PROBABILITY.search(hay)
    if m:
        errors.append(f"出现结果预测表述「{m.group(0)}」：不得给出通过率/概率，只能描述当前状态")

    # ---- 投递前复核
    bs = review.get("before_submit") or []
    if not bs:
        errors.append("before_submit 不能为空")
    needs = [rw for rw in review.get("rewrites") or [] if rw.get("needs_user_confirm") is True]
    if needs and not any(("确认" in x or "核实" in x) for x in bs):
        warnings.append(f"有 {len(needs)} 条改写需要本人确认，但 before_submit 里没有提醒核实")


def _check_v15(review, errors, warnings, stats) -> None:
    """v1.5 客观性：把「事实 / 推断 / 判断」分清楚，推断不许写成事实。"""
    for i, it in enumerate(review.get("issues") or []):
        a = it.get("assertion")
        if a not in ASSERTIONS:
            errors.append(f"issues[{i}].assertion 取值非法：{a!r}（应为 fact/inference/judgment）")
            continue
        text = f"{it.get('issue','')} {it.get('why','')}"

        # 敏感结论（学历性质/时间线/身份）默认只能是推断
        m = SENSITIVE_CLAIM.search(text)
        if m and a != "inference":
            errors.append(
                f"issues[{i}] 出现了「{m.group(0)}」这类结论，但 assertion={a!r}。"
                "原文没写明就必须按 inference 处理，并给出 verify_question"
            )

        if a == "inference":
            if not (it.get("verify_question") or "").strip():
                errors.append(f"issues[{i}] 是推断级，必须给 verify_question（一句让本人回答的话）")
            if not HEDGE.search(it.get("issue", "")):
                warnings.append(
                    f"issues[{i}] 标为推断，但 issue 措辞里没有「疑似/需确认/请核对」之类的保留词"
                )
        elif a == "fact":
            if not (it.get("evidence") or "").strip():
                errors.append(f"issues[{i}] 标为事实级，但没有 evidence 原文支撑")
            if HEDGE.search(it.get("issue", "")):
                warnings.append(f"issues[{i}] 标为事实，措辞却是推断口气（{HEDGE.search(it['issue']).group(0)}），二者矛盾")
        stats.setdefault("inference_count", 0)
        if a == "inference":
            stats["inference_count"] += 1

    # 一票否决只能是事实
    for i, b in enumerate(review.get("hard_blockers") or []):
        if b.get("assertion") not in {"fact", None}:
            errors.append(
                f"hard_blockers[{i}].assertion={b.get('assertion')!r}："
                "一票否决的结论必须能在原文里逐字找到，只允许 fact"
            )


# ---------------------------------------------------------------- 批量指标
def evaluate_dir(out_dir: Path, gold: Path | None, only_files: list[Path] | None = None) -> dict:
    txt_dir = Path(__file__).resolve().parent.parent / "samples" / "txt"
    if not txt_dir.exists():
        txt_dir = Path("samples/txt")
    files = sorted(out_dir.glob("*.json"))
    if only_files is not None:
        keep = {f.name for f in only_files}
        files = [f for f in files if f.name in keep]
    if not files:
        return {"error": f"{out_dir} 下没有 JSON"}

    d = diffs = 0
    hit = total = 0
    invented = 0
    unclear = 0
    hallucinated_layout = 0
    sp_missed = 0
    wv_missed = 0
    failed = []
    for f in files:
        review = json.loads(f.read_text(encoding="utf-8"))
        case = f.name.split(".")[0]
        txt = txt_dir / f"{case}.txt"
        resume_text = txt.read_text(encoding="utf-8") if txt.exists() else ""
        res = check(review, resume_text)
        if not res["file_ok"]:
            failed.append({"case": case, "errors": res["errors"][:3]})
        s = res["stats"]
        hit += s.get("evidence_hit") or 0
        total += s.get("evidence_total") or 0
        invented += len(s.get("invented_numbers") or [])
        unclear += s.get("unclear_numbers") or 0
        hallucinated_layout += s.get("visual_claims_without_image") or 0
        sp_missed += s.get("self_praise_missed") or 0
        wv_missed += s.get("weak_verb_missed") or 0
        if gold:
            gold_map = json.loads(gold.read_text(encoding="utf-8"))
            human = (gold_map.get(case) or {}).get("human_score")
            if isinstance(human, int):
                d += abs(human - review.get("overall_score", 0))
                diffs += 1

    return {
        "cases": len(files),
        "failed_cases": failed,
        "evidence_hit_rate": round(hit / total, 3) if total else None,
        "invented_numbers_count": invented,
        "unclear_numbers_count": unclear,
        "layout_hallucination_count": hallucinated_layout,
        "self_praise_missed_count": sp_missed,
        "weak_verb_missed_count": wv_missed,
        "mae_vs_human": round(d / diffs, 1) if diffs else None,
        "health": {
            "evidence_hit_rate": "PASS" if total and hit / total >= 0.95 else "CHECK",
            "invented_numbers": "PASS" if invented == 0 else "FAIL",
            "layout_hallucination": "PASS" if hallucinated_layout == 0 else "FAIL",
            "showcase_coverage": "PASS" if sp_missed == 0 and wv_missed == 0 else "FAIL",
            "mae_vs_human": "PASS" if diffs and d / diffs <= 10 else ("CHECK" if diffs else "NO_GOLD"),
        },
        "weights_note": "权重为经验值，未做回归校准；WEIGHTS 是唯一真源",
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="校验 AI 简历评审输出")
    ap.add_argument("path", help="单个 json 或 outputs 目录")
    ap.add_argument("--resume", help="简历原文 txt（单文件模式）")
    ap.add_argument("--gold", default="samples/gold.json", help="人工标注文件，用于算 MAE")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出")
    args = ap.parse_args()

    path = Path(args.path)
    if not path.exists():
        print(f"[error] 路径不存在：{path}", file=sys.stderr)
        return 2

    if path.is_dir():
        gold = Path(args.gold) if Path(args.gold).exists() else None
        result = evaluate_dir(path, gold)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else _pretty_batch(result))
        return 0 if not result.get("failed_cases") else 1

    review = json.loads(path.read_text(encoding="utf-8"))
    resume_text = ""
    if args.resume:
        resume_text = Path(args.resume).read_text(encoding="utf-8")
    else:
        guess = Path("samples/txt") / (path.name.split(".")[0] + ".txt")
        if guess.exists():
            resume_text = guess.read_text(encoding="utf-8")
    result = check(review, resume_text)
    print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else _pretty_one(path.name, result))
    return 0 if result["file_ok"] else 1


def _pretty_one(name: str, res: dict) -> str:
    out = [f"== {name} ==", f"结果：{'通过' if res['file_ok'] else '不通过'}｜版本 {res['stats'].get('version')}"]
    s = res["stats"]
    if s.get("evidence_total"):
        out.append(f"引用命中：{s['evidence_hit']}/{s['evidence_total']} ({s['evidence_hit_rate']:.0%})")
    line = f"改写条数：{s.get('rewrites', 0)}｜编造数字：{len(s.get('invented_numbers') or [])}"
    if s.get("version") in {"v1.1", "v1.2", "v1.3", "v1.4"}:
        line += (
            f"｜来源不清的数字：{s.get('unclear_numbers', 0)}"
            f"｜无图却谈排版：{s.get('visual_claims_without_image', 0)}"
        )
    if s.get("version") in {"v1.2", "v1.3", "v1.4"}:
        line += (
            f"｜漏报弱动词：{s.get('weak_verb_missed', 0)}"
            f"｜漏报自评词：{s.get('self_praise_missed', 0)}"
        )
    out.append(line)
    if res["errors"]:
        out.append("\n[ERROR]")
        out += [f"  - {e}" for e in res["errors"]]
    if res["warnings"]:
        out.append("\n[WARN]")
        out += [f"  - {w}" for w in res["warnings"]]
    return "\n".join(out)


def _pretty_batch(res: dict) -> str:
    if "error" in res:
        return res["error"]
    out = [
        f"样本数：{res['cases']}",
        f"引用命中率：{res['evidence_hit_rate']}（目标 ≥ 0.95）",
        f"编造数字：{res['invented_numbers_count']}（目标 0）",
        f"无图却谈排版：{res.get('layout_hallucination_count', 0)}（目标 0）",
        f"展示力覆盖：漏报弱动词 {res.get('weak_verb_missed_count', 0)} 条"
        f"、漏报自评词 {res.get('self_praise_missed_count', 0)} 个（目标 0）",
        f"来源不清的数字：{res.get('unclear_numbers_count', 0)}（提示项，需人工看）",
        f"与人工分 MAE：{res['mae_vs_human']}（目标 ≤ 10；权重未校准，仅供参考）",
        "健康度：" + json.dumps(res["health"], ensure_ascii=False),
    ]
    for f in res["failed_cases"]:
        out.append(f"\n[FAIL] {f['case']}")
        out += [f"  - {e}" for e in f["errors"]]
    return "\n".join(out)


if __name__ == "__main__":
    raise SystemExit(main())
