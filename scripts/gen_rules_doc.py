#!/usr/bin/env python3
"""从 config/rubric.yaml 生成 docs/评分规则.md（人读版）。

为什么要有这个脚本：
    规则只写在代码和 prompt 里，等于对外说「信我就好」。用户问「为什么 62 分不是 70」
    时，我们没法指着一处让人自己算。这份文档把规则摊开：五维权重、行业权重、
    verdict 阈值、分数段、两道门、各种阈值——全部从 rubric 生成，手改一个字
    测试就会红（scripts/test_rules.py 比对生成结果与提交结果逐字节一致）。

用法：
    python scripts/gen_rules_doc.py           # 写 docs/评分规则.md
    python scripts/gen_rules_doc.py --check   # 只比对，不一致退出码 1（CI/测试用）

零第三方依赖；不写时间戳（否则每次生成都有 diff）。
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import rubric  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "docs" / "评分规则.md"

DIM_ORDER = ["role_fit", "impact", "evidence_quality", "structure", "competitiveness"]


def _pct(value: float) -> str:
    """0.25 -> '25%'（不要打印成 0.25%，v2.1 踩过）。"""
    return f"{round(value * 100)}%"


def _verdict_thresholds() -> list[tuple[str, str]]:
    """verdict 的分数边界，按从高到低。"""
    strong = int(rubric.num("verdict.strong_pass_min"))
    pass_min = int(rubric.num("verdict.pass_min"))
    borderline = int(rubric.num("verdict.borderline_min"))
    return [
        (f"{strong}~100", "strong_pass（强推）"),
        (f"{pass_min}~{strong - 1}", "pass（可投）"),
        (f"{borderline}~{pass_min - 1}", "borderline（勉强）"),
        (f"0~{borderline - 1}", "reject（不建议投）"),
    ]


def render() -> str:
    lines: list[str] = []
    add = lines.append

    add("# 评分规则")
    add("")
    add("> 本文件由 `scripts/gen_rules_doc.py` 从 `config/rubric.yaml` 自动生成，**不要手改**。")
    add("> 改了 yaml 请跑 `python scripts/gen_rules_doc.py`；忘了跑，`scripts/test_rules.py` 会报红。")
    add(">")
    add("> 给这份文档的理由：分数不该是黑盒。同一份简历，任何人对着下面的表，")
    add("> 都应该能算出同一个大致区间。算不出来，说明规则本身没写清。")
    add("")

    # 1 总分
    add("## 1 总分怎么算")
    add("")
    add("五个维度各打 1~5 分（整数），按所选行业的权重加权，再乘 20 换算成 100 分制：")
    add("")
    add("```")
    add("overall_score = round(Σ(维度分 × 该行业权重) × 20)")
    add("```")
    add("")
    add(f"不选行业时用 `{rubric.INDUSTRY_DEFAULT}`（通用权重）。")
    add("**权重只改总分，不改维度分**——同一份简历换个行业，五个维度分不该变。")
    add("")

    # 2 五维
    add("## 2 五个维度量什么（通用权重）")
    add("")
    add("| 维度 | 量什么 | 权重 |")
    add("|---|---|---|")
    dims = rubric.dimensions()
    base = rubric.weights()
    for key in DIM_ORDER:
        add(f"| `{key}` | {dims[key]} | {_pct(base[key])} |")
    add("")

    # 3 行业
    add("## 3 行业权重（选定行业后覆盖上表）")
    add("")
    add("| 行业 | 权重顺序 | 这份简历重点看什么 |")
    add("|---|---|---|")
    for key, label, emphasis in rubric.list_industries():
        w = rubric.weights_for(key)
        order = " / ".join(_pct(w[k]) for k in DIM_ORDER)
        add(f"| `{key}` {label} | {order} | {emphasis} |")
    add("")
    add("权重顺序对应：" + " / ".join(f"`{k}`" for k in DIM_ORDER) + "。")
    add("")

    # 4 阈值
    add("## 4 结论阈值")
    add("")
    add("| 分数 | verdict |")
    add("|---|---|")
    for span, name in _verdict_thresholds():
        key, _sep, zh = name.partition("（")
        add(f"| {span} | `{key}`（{zh} |")
    add("")
    add("`verdict` 必须与分数严格一致，代码会校验；不一致的输出直接被判为不合格。")
    add("")

    # 5 分数段
    add("## 5 分数段（先定段，再在段内定分）")
    add("")
    add("| 分数段 | 区间 | 典型画像 | 建议分 |")
    add("|---|---|---|---|")
    for key, label, lo, hi, suggest, _feature, typical, _misjudge in rubric.bands()[::-1]:
        short = label.split("·")[-1].strip()
        add(f"| `{key}` {short} | {int(lo)}~{int(hi)} | {typical} | {suggest} |")
    add("")
    add("每段「长什么样」「最常见误判」写在 prompt 的分数段表里；这里只放区间与典型画像。")
    add("")

    # 6 两道门
    add("## 6 两道门（结论是算出来的，不许模型自由填）")
    add("")
    add("| 门 | 条件 | 不过会怎样 |")
    add("|---|---|---|")
    add(
        "| HR 初筛 | "
        f"`role_fit` ≥ {rubric.num('gates.hr.role_fit_min'):g} 且 "
        f"`impact` ≥ {rubric.num('gates.hr.impact_min'):g} |"
        " 报告标「HR 初筛不过」，不再往下推荐 |"
    )
    add(
        "| 面试官视角 | "
        f"`evidence_quality` ≥ {rubric.num('gates.interview.evidence_quality_min'):g} 且 "
        f"`high_risk` ≤ {rubric.num('gates.interview.high_risk_max'):g} |"
        " 报告标「面试官会追问到卡壳」，并列出高危项 |"
    )
    add("")

    # 7 内容层
    add("## 7 内容层的硬性数字")
    add("")
    add("| 项 | 阈值 | 为什么是这个数 |")
    add("|---|---|---|")
    add(
        f"| 一页字数 | {rubric.num('format.pages_chars'):g} 字 |"
        " 中文简历一页（含项目/实习）实测 800~1100 字 |"
    )
    add(
        f"| 长句 | 单句 > {rubric.num('readability.long_sentence_chars'):g} 字 |"
        " 合格 bullet 多在 40~60 字，超过 60 就要拆 |"
    )
    add(
        f"| 弱动词行 | 动词后实字 ≥ {rubric.num('readability.weak_line_min_rest'):g} |"
        " 防「完成」两个字被当成一行经历（v1.10 误报源） |"
    )
    add(
        f"| 引用长度 | {rubric.num('evidence.min_quote'):g}~{rubric.num('evidence.max_quote'):g} 字 |"
        " 太短无法回指原文；太长像复述、会绕过校验 |"
    )
    add("")

    # 8 输出契约
    add("## 8 输出契约")
    add("")
    add(
        f"改写建议 {rubric.num('output.rewrites_min'):g}~{rubric.num('output.rewrites_max'):g} 条："
        "少于 2 条像是「只有唯一解」，多于 6 条一页读不完。"
    )
    add("")

    # 9 改版 / 识别 / 评测
    add("## 9 改版对比、输入识别、评测门槛")
    add("")
    add("| 项 | 值 | 说明 |")
    add("|---|---|---|")
    add(
        f"| 同版本判定 | 相似度 ≥ {rubric.num('version_diff.min_similarity'):g} |"
        " 同版不同稿实测 0.72~0.95，不相干简历 0.1~0.45，取中间 |"
    )
    add(
        f"| 历史版本上限 | {rubric.num('version_diff.max_candidates'):g} 份 | 每次都要读原文，不能无限翻 |"
    )
    add(
        f"| 差异行上限 | {rubric.num('version_diff.max_diff_items'):g} 行 | 前端渲染上限，统计数字仍保留真实值 |"
    )
    add(
        f"| 扫描件判定 | 抽出文本 < {rubric.num('ingest.min_text_chars'):g} 字 | 低于此值走视觉转写，不硬猜 |"
    )
    add(
        f"| 人工对齐门槛 | MAE ≤ {rubric.num('eval.mae_max'):g} | 用独立标注数据测，不继承旧代理值 |"
    )
    add(
        f"| 抖动控制 | repeat=3 极差 ≤ {rubric.num('eval.repeat_spread_max'):g} | 同一份重复评三次 |"
    )
    add(
        f"| 双人一致性 | Cohen's κ ≥ {rubric.num('eval.kappa_min'):g} | 四档 verdict |"
    )
    add(
        f"| 仲裁触发 | 两名标注者差 > {rubric.num('eval.arbitration_threshold'):g} 分 | 差太多就进仲裁队列 |"
    )
    add(
        f"| 闭环落实率门槛 | 可判定改写 ≥ {rubric.num('eval.closure_min_pairs'):g} 条 |"
        " 少于 10 条时 ±1 条≈10 个点，不给百分比 |"
    )
    add(
        f"| pilot 门槛 | {rubric.num('eval.pilot_min_pairs'):g} 份 × "
        f"{rubric.num('eval.pilot_min_reviewers'):g} 人 |"
        " pilot 只验「两人是否用同一把尺」，不回答模型准不准 |"
    )
    add("")

    # 9.1 简历太短（v2.4 D4）
    thin = rubric.thin()
    add("### 9.1 简历太短：不评分，先补信息")
    add("")
    add(
        "字太少时不打分，是因为五个维度里至少两维没有证据——"
        "这时给出的分数是猜的，比不给更糟。"
    )
    add("")
    add("| 字数（去空白） | 处理 | 说明 |")
    add("| --- | --- | --- |")
    add(
        f"| 少于 {thin['min_chars']} 字 | 拒收，提示重新粘贴 | "
        "占位符、乱码、只有一行标题：看不出是简历 |"
    )
    add(
        f"| {thin['min_chars']}~{thin['advisory_max']} 字 | 建议模式：**不评分** | "
        "指出缺哪几维的证据、先回答哪几个问题、按什么骨架补 |"
    )
    add(
        f"| 多于 {thin['advisory_max']} 字 | 正常评分 | 五档分数段见第 5 节 |"
    )
    add("")
    add(
        f"建议模式最多问 {thin['max_questions']} 个问题：超过这个数用户不会答，等于没问。"
        "报告里必须写明「不评分」及其原因，**不许把「没评」写成 0 分**。"
    )
    add("")

    # 9.2 平台层
    platforms = rubric.ats_platforms()
    add("## 9.2 平台层（ATS）：同一份简历，不同平台读法不一样")
    add("")
    add("| 平台 | 它怎么看 | 检查项 |")
    add("|---|---|---|")
    for key, label, note, checks in platforms:
        add(f"| `{key}` {label} | {note} | {len(checks)} 条 |")
    add("")
    for _key, label, _note, checks in platforms:
        add(f"- **{label}**：" + "；".join(checks.values()))
    add("")
    add(
        "平台层只回答「去检查什么」，**不回答「会不会被淘汰」**：这些是公开信息与实测经验的总结，"
        "不是任何平台的官方文档，规则也会变。正文里判不出来的（版面、平台表单、文件名）一律标 `unknown`，"
        "既不算通过，也不算不合格。"
    )
    add("")

    # 10 不算分
    add("## 10 什么不算分")
    add("")
    add("- 长相、性别、年龄、婚育、籍贯、政治面貌、民族、照片、健康状况：不评、不提、不进报告。")
    add("- 学校名气、公司名气、家庭背景：不作为竞争力加分（只作为事实陈述）。")
    add("- 与目标岗位无关的兴趣、证书堆砌：最多算「结构与可读性」的一处噪音，不加分。")
    add("")

    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str]) -> int:
    text = render()
    if "--check" in argv:
        current = TARGET.read_text(encoding="utf-8") if TARGET.exists() else ""
        if current != text:
            print("docs/评分规则.md 与 config/rubric.yaml 不一致。")
            print("跑 `python scripts/gen_rules_doc.py` 重新生成后再提交。")
            return 1
        print("docs/评分规则.md 与 config/rubric.yaml 一致。")
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    TARGET.write_text(text, encoding="utf-8")
    print(f"已写入 {TARGET.relative_to(ROOT)}（{len(text.splitlines())} 行）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
