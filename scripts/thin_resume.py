"""低信息简历（v2.4 / D4）：字太少不硬拒，但绝不假装能评分。

为什么单独一个模块：
  「太短」有两种，处理方式必须不同——
  1) 垃圾输入（乱码、占位符、"test"）：直接拒收，让他重新粘贴；
  2) 草稿（学校 + 专业 + 一段社团）：**能读**，但五维里至少两维没有证据。
     这时回一句「至少 80 字」是最没用的回答：他不知道自己缺的是「结果」
     而不是「字数」。所以走「建议模式」：不评分，指出缺哪几维的证据、
     先回答哪几个问题、按什么骨架补。

硬规矩（测试钉住）：
  - 建议模式**不产出分数**，也不产出 verdict（不给分就没有「可投/待改」）；
  - 问题数 ≤ rubric.thin()["max_questions"]，且每条都是能直接回答的疑问句；
  - 报告里必须写明「不评分」及其原因，不许把「没评」写成「0 分」。
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rubric  # noqa: E402

# 五维在本模块里的中文名与缺证据时的说明。顺序就是报告里的顺序。
DIM_LABEL = {
    "role_fit": "岗位对口",
    "impact": "结果与影响",
    "evidence_quality": "证据可信度",
    "structure": "结构与排版",
    "competitiveness": "竞争力差异",
}

# 每一维「怎么算有证据」。宁可宽松：这是建议模式，误判成「有」只是少问一句，
# 误判成「没有」会让用户去补他已经写过的东西。
_SIGNALS: dict[str, object] = {
    "job": ("求职意向", "应聘", "目标岗位", "意向岗位", "申请岗位", "求职：", "岗位："),
    "education": ("大学", "学院", "学校", "本科", "硕士", "研究生", "专科", "博士",
                  "GPA", "绩点", "主修", "届"),
    "experience": ("实习", "工作经历", "项目经历", "项目", "负责", "参与", "担任",
                   "社团", "学生会", "志愿", "兼职"),
    "skill": ("技能", "熟悉", "掌握", "熟练", "工具", "语言能力", "证书",
              "Excel", "Python", "SQL", "PPT", "Figma"),
    "award": ("奖", "竞赛", "证书", "排名", "前 10", "前10", "优秀", "荣誉", "认证"),
}

_NUMBER = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|万|千|个|人|条|次|名|天|小时|分钟|ms|倍|页|篇|场|份)")

# 垃圾输入的判据：全是占位符/重复字符/太少的实词
_JUNK = re.compile(r"^(?:[a-zA-Z]{1,3}|test|abc|asdf|简历|resume|1111|null|无|暂无)$", re.I)


def chars(text: str) -> int:
    """有效字数：去掉所有空白后的字符数（中文按字算，英文按字母算）。"""
    return len(re.sub(r"\s+", "", str(text or "")))


def _hits(text: str) -> dict[str, bool]:
    low = str(text or "")
    out: dict[str, bool] = {}
    for key, words in _SIGNALS.items():
        out[key] = any(w in low for w in words)  # type: ignore[union-attr]
    out["number"] = bool(_NUMBER.search(low))
    return out


def looks_like_junk(text: str) -> bool:
    """一眼是乱码/占位符（不是「写得太少」）。"""
    body = re.sub(r"[\s\W_]+", "", str(text or ""))
    if not body:
        return True
    if _JUNK.match(body):
        return True
    # 同一个字符占了一半以上（"1111111111" / "。。。。。")
    if len(body) >= 8 and max(body.count(c) for c in set(body)) / len(body) > 0.5:
        return True
    return False


def classify(text: str) -> str:
    """返回 "junk" / "short" / "advisory" / "full"。

    - junk：拒收，让他重新粘贴
    - short：连结构建议都给不了（< min_chars）
    - advisory：建议模式（min_chars ≤ 字数 ≤ advisory_max）
    - full：正常评分
    """
    cfg = rubric.thin()
    n = chars(text)
    if looks_like_junk(text):
        return "junk"
    if n < cfg["min_chars"]:
        return "short"
    if n <= cfg["advisory_max"]:
        return "advisory"
    return "full"


# 每维缺证据时问用户的那一句。写成陈述句会被当成「平台在解释」，
# 写成疑问句用户才会去补——所以每条都以「？」结尾（测试钉住）。
_QUESTIONS: dict[str, str] = {
    "role_fit": "你这次想投什么岗位？写成一句话，例如「互联网 · 产品经理实习生」。",
    "impact": "你做过的事里哪一件有能写出来的结果？改前多少、改后多少、怎么统计的？",
    "evidence_quality": "这段经历里哪部分是你独立负责的？哪些是团队一起做的？",
    "structure": "你打算按哪几块写？现在缺的是基本信息、教育、经历还是技能？",
    "competitiveness": "同专业的同学大多有什么，你有什么是他们没有的？",
}

# 兜底：五种信号全都有、但字数仍不够（说明写得太散，不是缺块）
_QUESTION_FALLBACK = "挑最近一段经历，按「做什么 → 怎么做 → 结果」各写一行，够 120 字再来评？"

_SKELETON = (
    "基本信息：姓名 ｜ 求职意向（行业 + 岗位）｜ 学校 专业 学历 ｜ 毕业时间 ｜ 电话 邮箱",
    "教育：学校 · 专业 · 起止时间 · GPA/排名（有就写，没有不要编）",
    "经历（倒序，每段 2~3 行）：单位/项目 · 角色 · 时间",
    "　· 做了什么：一句话说清业务对象与你的动作",
    "　· 怎么做：工具、方法、你独立负责的那部分",
    "　· 结果：一个数字（改前 → 改后），并说明怎么统计的",
    "技能：与岗位直接相关的工具/语言，按熟练度排序，最多 6 项",
)


def advise(text: str, job: str = "") -> dict:
    """给出建议模式的结构化结果。**不含 score / verdict 字段**（故意的）。"""
    cfg = rubric.thin()
    n = chars(text)
    found = _hits(text)
    has_job = bool((job or "").strip()) and "未提供" not in str(job)
    if has_job:
        found["job"] = True

    missing: list[dict] = []
    if not found["job"]:
        missing.append({"dimension": "role_fit", "label": DIM_LABEL["role_fit"],
                        "why": "没写想投什么岗位，五维里的「岗位对口」无从判断"})
    if not found["number"]:
        missing.append({"dimension": "impact", "label": DIM_LABEL["impact"],
                        "why": "全篇没有一个数字：做了什么、做成什么样都读不出来"})
    if not found["experience"]:
        missing.append({"dimension": "evidence_quality", "label": DIM_LABEL["evidence_quality"],
                        "why": "没有一段可以追问的经历，只能看到学校信息"})
    if not found["skill"]:
        missing.append({"dimension": "structure", "label": DIM_LABEL["structure"],
                        "why": "没有技能块，简历读起来只有一段话，结构立不住"})
    if not found["award"]:
        missing.append({"dimension": "competitiveness", "label": DIM_LABEL["competitiveness"],
                        "why": "没有和同专业同学区分开的任何信息（竞赛/证书/项目/实习）"})

    questions = [q for d, q in _QUESTIONS.items() if any(m["dimension"] == d for m in missing)]
    if not questions:
        questions = [_QUESTION_FALLBACK]
    questions = questions[: cfg["max_questions"]]

    return {
        "mode": "advisory",
        "chars": n,
        "min_chars": cfg["min_chars"],
        "advisory_max": cfg["advisory_max"],
        "enough_for_score": False,
        "found": found,
        "missing": missing,
        "questions": questions,
        "skeleton": list(_SKELETON),
    }


def to_markdown(adv: dict) -> str:
    """把建议模式渲染成和正式报告同一副骨架的 Markdown（但没有分数）。"""
    n = int(adv.get("chars") or 0)
    limit = int(adv.get("advisory_max") or rubric.thin()["advisory_max"])
    missing = adv.get("missing") or []
    questions = adv.get("questions") or []
    skeleton = adv.get("skeleton") or list(_SKELETON)

    out = [f"# 先补信息，暂不评分（当前 {n} 字，不含空白）", ""]
    out.append(
        f"> 不到 {limit} 字时，五个维度里至少两维没有证据，算出来的分会是猜的——"
        f"猜一个分数给你，比不给更糟。所以这里**不评分**，只说清缺什么、先补哪几条。"
    )
    out.append("")
    out.append("## 现在缺的是")
    if missing:
        for m in missing:
            out.append(f"- **{m['label']}**：{m['why']}")
    else:
        out.append("- 五类信息都有，只是写得散：同一段经历占了太多行，压成「做什么 → 怎么做 → 结果」三行即可。")
    out.append("")
    out.append("## 先回答这几个问题")
    for i, q in enumerate(questions, 1):
        out.append(f"{i}. {q}")
    out.append("")
    out.append("## 按这个骨架补，够 %d 字再评一次" % limit)
    out.append("")
    out.append("```")
    for line in skeleton:
        out.append(line)
    out.append("```")
    out.append("")
    out.append("## 一句提醒")
    out.append(
        "补的时候只写真实发生过的事：没有数据就写清做了什么、卡在哪里、怎么解决的，"
        "**不要为了凑字数编数字**——编出来的数字会在面试第一个追问上暴露。"
    )
    return "\n".join(out).strip() + "\n"


def main(argv: list[str] | None = None) -> int:
    import argparse
    import json

    ap = argparse.ArgumentParser(description="低信息简历：判定模式并给出结构建议")
    ap.add_argument("text", nargs="?", default="", help="简历正文；省略则从 stdin 读")
    ap.add_argument("--job", default="", help="目标岗位（可选）")
    ap.add_argument("--markdown", action="store_true", help="输出报告 Markdown，默认输出 JSON")
    args = ap.parse_args(argv)

    text = args.text or sys.stdin.read()
    mode = classify(text)
    if mode in ("junk", "short"):
        cfg = rubric.thin()
        print(json.dumps({"mode": mode, "chars": chars(text),
                          "min_chars": cfg["min_chars"], "enough_for_score": False},
                         ensure_ascii=False, indent=2))
        return 0
    adv = advise(text, args.job)
    if args.markdown:
        print(to_markdown(adv))
    else:
        print(json.dumps(adv, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
