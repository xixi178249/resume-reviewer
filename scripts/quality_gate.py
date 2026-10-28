"""评审结果的交付门禁。

职责不是"再跑一遍校验器"，而是决定一份模型输出能否作为正式结果交给用户：

  1. 按 v1.8 schema 检查结构；
  2. 由代码重算总分、档位、两道闸门和 JD 覆盖率；
  3. 把可撤下的单条问题限制在条目层，避免一条坏改写毁掉整份报告；
  4. 任何影响核心结论、证据、隐私或结构的问题阻断整份交付；
  5. 不合格候选不参与 repeat 的中位选择。

错误等级：
  blocker  —— 整份结果不可交付；
  filtered —— 只撤下该条内容，其余结果仍可交付；
  warning  —— 可交付，但需记录或展示。

错误码是稳定接口，前端/测试不得依赖中文文案。
"""
from __future__ import annotations

import copy
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output  # noqa: E402
import validate_schema  # noqa: E402

SCHEMA_VERSION = "v1.8"
GATE_VERSION = "v1"
SCHEMA_PATH = ROOT / "prompts" / f"output-schema-{SCHEMA_VERSION}.json"

BLOCKER = "blocker"
FILTERED = "filtered"
WARNING = "warning"
SEVERITIES = {BLOCKER, FILTERED, WARNING}

VALID_FACT_DELTA = {"none", "wording", "ownership_upgrade", "number_added"}


@dataclass(frozen=True)
class Problem:
    code: str
    severity: str
    message: str
    path: str = ""

    def as_dict(self) -> dict:
        return asdict(self)


class QualityGateError(RuntimeError):
    """所有候选都被门禁拒绝。"""

    code = "REVIEW_VALIDATION_FAILED"

    def __init__(self, problems: list[Problem], attempts: int):
        self.problems = problems
        self.attempts = attempts
        super().__init__(
            "这次结果未通过事实与结构校验，没有可作为正式结果交付的版本。"
            "请重试一次；连续失败可减少评审次数，或粘贴文字版简历后再试。"
        )

    def public_details(self, limit: int = 5) -> list[dict]:
        seen: set[tuple[str, str]] = set()
        out: list[dict] = []
        for p in self.problems:
            key = (p.code, p.message)
            if p.severity != BLOCKER or key in seen:
                continue
            seen.add(key)
            out.append({"code": p.code, "message": p.message})
            if len(out) >= limit:
                break
        return out


def _problem(
    problems: list[Problem],
    code: str,
    severity: str,
    message: str,
    path: str = "",
) -> None:
    if severity not in SEVERITIES:
        raise ValueError(f"未知错误等级：{severity}")
    problems.append(Problem(code=code, severity=severity, message=message, path=path))


def _schema_problems(doc: dict, problems: list[Problem]) -> None:
    try:
        schema = json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        _problem(
            problems,
            "SCHEMA_UNAVAILABLE",
            BLOCKER,
            f"无法读取输出契约：{exc}",
            "schema",
        )
        return
    errors: list[str] = []
    validate_schema.check_schema(doc, schema, schema, "", errors)
    for message in errors:
        _problem(
            problems,
            "SCHEMA_INVALID",
            BLOCKER,
            f"输出结构不符合 {SCHEMA_VERSION}：{message}",
            "schema",
        )


def _sanitize_rewrites(doc: dict, resume: str, problems: list[Problem]) -> None:
    """撤下不能安全交付的单条改写；少于 2 条则整份不合格。"""
    raw = doc.get("rewrites")
    if not isinstance(raw, list):
        return  # 结构问题交给 schema/checker

    resume_canon = check_output.canon(resume)
    resume_nums = check_output.numbers_in(resume)
    kept: list[dict] = []

    for i, rw in enumerate(raw):
        path = f"rewrites[{i}]"
        if not isinstance(rw, dict):
            _problem(problems, "REWRITE_INVALID", FILTERED, "改写条目不是对象，已撤下", path)
            continue

        original = (rw.get("original") or "").strip()
        rewritten = (rw.get("rewritten") or "").strip()
        if not original or not rewritten:
            _problem(problems, "REWRITE_EMPTY", FILTERED, "改写缺少原句或改后句，已撤下", path)
            continue

        if not check_output.find_quote(original, resume_canon):
            _problem(
                problems,
                "REWRITE_ORIGINAL_NOT_FOUND",
                FILTERED,
                "改写原句无法在简历中回指，已撤下",
                path,
            )
            continue

        new_numbers = check_output.numbers_in(rewritten) - resume_nums
        if new_numbers:
            _problem(
                problems,
                "REWRITE_INVENTED_NUMBER",
                FILTERED,
                f"改写新增了原文没有的数字 {sorted(new_numbers)}，已撤下",
                path,
            )
            continue

        fd = rw.get("fact_delta")
        confirm = rw.get("needs_user_confirm")
        if fd not in VALID_FACT_DELTA or not isinstance(confirm, bool):
            _problem(
                problems,
                "REWRITE_CONTRACT_INVALID",
                FILTERED,
                "改写的 fact_delta 或 needs_user_confirm 不合法，已撤下",
                path,
            )
            continue
        if fd == "number_added":
            _problem(
                problems,
                "REWRITE_NUMBER_ADDED",
                FILTERED,
                "改写被标注为新增数字，已撤下",
                path,
            )
            continue

        upgrade = check_output.ownership_upgrade(original, rewritten)
        if upgrade and (fd != "ownership_upgrade" or confirm is not True):
            _problem(
                problems,
                "REWRITE_OWNERSHIP_UNCONFIRMED",
                FILTERED,
                f"改写把事实强度从「{upgrade}」升级却未要求确认，已撤下",
                path,
            )
            continue
        if fd == "ownership_upgrade" and confirm is not True:
            _problem(
                problems,
                "REWRITE_CONFIRM_MISSING",
                FILTERED,
                "改写声明升级了事实但未要求本人确认，已撤下",
                path,
            )
            continue

        kept.append(rw)

    doc["rewrites"] = kept
    if len(kept) < 2:
        _problem(
            problems,
            "REWRITES_INSUFFICIENT",
            BLOCKER,
            f"安全可交付的改写只有 {len(kept)} 条，少于最低 2 条",
            "rewrites",
        )


def _sanitize_issues(doc: dict, resume: str, problems: list[Problem]) -> None:
    """撤下引用不到原文或无法执行的单条问题。"""
    raw = doc.get("issues")
    if not isinstance(raw, list):
        return

    resume_canon = check_output.canon(resume)
    kept: list[dict] = []
    for i, issue in enumerate(raw):
        path = f"issues[{i}]"
        if not isinstance(issue, dict):
            _problem(problems, "ISSUE_INVALID", FILTERED, "问题条目不是对象，已撤下", path)
            continue
        evidence = (issue.get("evidence") or "").strip()
        fix = (issue.get("fix") or "").strip()
        if not evidence or not check_output.find_quote(evidence, resume_canon):
            _problem(
                problems,
                "ISSUE_EVIDENCE_NOT_FOUND",
                FILTERED,
                "问题引用的原文无法回指，已撤下",
                path,
            )
            continue
        if not fix or not check_output.has_anchor(fix, evidence):
            _problem(
                problems,
                "ISSUE_FIX_NOT_ACTIONABLE",
                FILTERED,
                "问题建议没有可执行锚点，已撤下",
                path,
            )
            continue
        kept.append(issue)

    doc["issues"] = kept
    if not kept:
        _problem(
            problems,
            "ISSUES_INSUFFICIENT",
            BLOCKER,
            "撤下不可回指的问题后，没有任何可交付的问题项",
            "issues",
        )


# 模型经常漏写"先修格式"这条动作；比起让它重跑一分钟，直接补一条更划算
DEFAULT_FORMAT_ACTION = {
    "targets": "format_review",
    "action": (
        "先把排版改成单栏纯文本：去掉表格和分栏、正文不要放进图片、"
        "章节名统一用「教育经历/实习经历/项目经历/专业技能」"
    ),
    "est_minutes": 20,
    "expected_gain": "risk_down",
}

REPAIR_LABELS = {
    "format_first_action": "已把「先修格式」提到第一条动作",
    "format_in_summary": "已在结论开头点明格式问题",
}


def _apply_repairs(doc: dict, tags: list[str], problems: list[Problem]) -> None:
    """把"顺序写错、结论没提格式"这类偏差直接修好，而不是拦下来让用户重跑。"""
    done: list[str] = []
    for tag in tags:
        if tag == "format_first_action":
            actions = doc.get("next_actions")
            if not isinstance(actions, list) or not actions:
                continue
            idx = next(
                (
                    i
                    for i, a in enumerate(actions)
                    if isinstance(a, dict) and a.get("targets") == "format_review"
                ),
                None,
            )
            if idx is not None and idx != 0:
                actions.insert(0, actions.pop(idx))
                done.append(tag)
            elif idx is None:
                actions.insert(0, dict(DEFAULT_FORMAT_ACTION))
                del actions[6:]  # schema 的 maxItems 是 6
                done.append(tag)
        elif tag == "format_in_summary":
            summary = doc.get("one_line_summary")
            if not isinstance(summary, str):
                continue
            if any(k in summary for k in ("格式", "排版", "解析", "看不见", "看不到")):
                continue
            doc["one_line_summary"] = ("【先修格式】排版导致内容读不全，先修格式再看内容。" + summary)[:260]
            done.append(tag)

    if done:
        _problem(
            problems,
            "AUTO_REPAIRED",
            WARNING,
            "已自动修正：" + "；".join(REPAIR_LABELS.get(t, t) for t in done),
            "model_output",
        )


def _repair_jd_match(doc: dict, resume: str, problems: list[Problem]) -> None:
    """JD 关键词只影响岗位匹配段，按原文事实降级，不让整份报告陪葬。"""
    jd = doc.get("jd_match")
    if not isinstance(jd, dict):
        return
    if jd.get("jd_provided") is False:
        return

    resume_canon = check_output.canon(resume)
    kept: list[dict] = []
    for i, kw in enumerate(jd.get("keywords") or []):
        path = f"jd_match.keywords[{i}]"
        if not isinstance(kw, dict) or not (kw.get("term") or "").strip():
            _problem(problems, "JD_KEYWORD_INVALID", FILTERED, "JD 关键词条目为空，已撤下", path)
            continue
        term = kw["term"].strip()
        present = check_output.in_text(term, resume_canon)
        if not present:
            if kw.get("in_resume") is True or kw.get("natural") is True:
                _problem(
                    problems,
                    "JD_KEYWORD_DOWNGRADED",
                    FILTERED,
                    f"JD 关键词「{term}」在简历中找不到，已改为“不匹配”",
                    path,
                )
            kw["in_resume"] = False
            kw["natural"] = False
        elif kw.get("natural") is True and kw.get("in_resume") is not True:
            kw["natural"] = False
        kept.append(kw)
    jd["keywords"] = kept


def _normalize_derived(doc: dict, problems: list[Problem]) -> None:
    """派生字段由代码重算；模型只负责维度分和内容。"""
    dims = doc.get("dimension_scores")
    if not isinstance(dims, dict):
        return

    scores: dict[str, int] = {}
    for key in check_output.WEIGHTS:
        item = dims.get(key)
        score = item.get("score") if isinstance(item, dict) else None
        if not isinstance(score, int) or isinstance(score, bool) or not 0 <= score <= 5:
            return
        scores[key] = score

    calc_score = round(sum(scores[k] * w for k, w in check_output.WEIGHTS.items()) * 20)
    raw_score = doc.get("overall_score")
    # 现行规则允许 ±2 的取整差。只在超出允许范围时重算，避免 78 被推到
    # 80 后跨档变成 strong_pass——这种机械改档比保留 2 分误差更危险。
    if not isinstance(raw_score, int) or isinstance(raw_score, bool) \
            or not 0 <= raw_score <= 100 or abs(raw_score - calc_score) > 2:
        _problem(
            problems,
            "SCORE_RECALCULATED",
            WARNING,
            f"总分由 {raw_score!r} 按维度重算为 {calc_score}",
            "overall_score",
        )
        doc["overall_score"] = calc_score

    canonical_score = doc.get("overall_score")
    calc_verdict = check_output.expected_verdict(canonical_score)
    raw_verdict = doc.get("verdict")
    if raw_verdict != calc_verdict:
        _problem(
            problems,
            "VERDICT_RECALCULATED",
            WARNING,
            f"档位由 {raw_verdict!r} 按分数重算为 {calc_verdict}",
            "verdict",
        )
        doc["verdict"] = calc_verdict

    exp_hr = check_output.gate_hr_expected(doc)
    exp_gi = check_output.gate_interview_expected(doc)
    for key, expected, code in (
        ("gate_hr", exp_hr, "GATE_HR_RECALCULATED"),
        ("gate_interview", exp_gi, "GATE_INTERVIEW_RECALCULATED"),
    ):
        gate = doc.get(key)
        if not isinstance(gate, dict):
            gate = {}
            doc[key] = gate
        raw = gate.get("pass")
        if raw is not expected:
            _problem(
                problems,
                code,
                WARNING,
                f"{key}.pass 由 {raw!r} 按维度与规则重算为 {expected}",
                key,
            )
            gate["pass"] = expected

    jd = doc.get("jd_match")
    if isinstance(jd, dict):
        kws = jd.get("keywords") or []
        if jd.get("jd_provided") is True and isinstance(kws, list) and kws:
            calc_cov = round(
                sum(1 for k in kws if isinstance(k, dict) and k.get("natural") is True)
                / len(kws),
                2,
            )
            if jd.get("coverage_rate") != calc_cov:
                _problem(
                    problems,
                    "COVERAGE_RECALCULATED",
                    WARNING,
                    f"JD 覆盖率由 {jd.get('coverage_rate')!r} 重算为 {calc_cov}",
                    "jd_match.coverage_rate",
                )
                jd["coverage_rate"] = calc_cov


def evaluate(doc: dict, resume_text: str) -> dict:
    """评估一份候选。

    返回的 ``doc`` 是清理后的交付版本，原对象不被修改。
    只有 ``eligible=True`` 时才能展示、复制、下载或写入历史。
    """
    if not isinstance(doc, dict):
        problems = [Problem("OUTPUT_NOT_OBJECT", BLOCKER, "模型输出不是 JSON 对象", "$")]
        return {
            "eligible": False,
            "doc": {},
            "problems": [p.as_dict() for p in problems],
            "blockers": [p.as_dict() for p in problems],
            "filtered": [],
            "warnings": [],
            "stats": {},
        }
    if not isinstance(resume_text, str) or not resume_text.strip():
        problems = [Problem("RESUME_EMPTY", BLOCKER, "简历原文为空，无法做事实校验", "resume")]
        return {
            "eligible": False,
            "doc": copy.deepcopy(doc),
            "problems": [p.as_dict() for p in problems],
            "blockers": [p.as_dict() for p in problems],
            "filtered": [],
            "warnings": [],
            "stats": {},
        }

    delivery = copy.deepcopy(doc)
    problems: list[Problem] = []

    # 先做局部的、可证明的清理，再计算派生字段。
    _sanitize_rewrites(delivery, resume_text, problems)
    _sanitize_issues(delivery, resume_text, problems)
    _repair_jd_match(delivery, resume_text, problems)
    _normalize_derived(delivery, problems)

    res = check_output.check(delivery, resume_text)
    repairs = (res.get("stats") or {}).get("repairs") or []
    if repairs:
        _apply_repairs(delivery, repairs, problems)
        res = check_output.check(delivery, resume_text)

    _schema_problems(delivery, problems)
    for message in res.get("errors") or []:
        _problem(
            problems,
            "MODEL_OUTPUT_INVALID",
            BLOCKER,
            message,
            "model_output",
        )
    for message in res.get("warnings") or []:
        _problem(problems, "MODEL_OUTPUT_WARNING", WARNING, message, "model_output")

    blockers = [p for p in problems if p.severity == BLOCKER]
    filtered = [p for p in problems if p.severity == FILTERED]
    warnings = [p for p in problems if p.severity == WARNING]
    return {
        "eligible": not blockers,
        "doc": delivery,
        "problems": [p.as_dict() for p in problems],
        "blockers": [p.as_dict() for p in blockers],
        "filtered": [p.as_dict() for p in filtered],
        "warnings": [p.as_dict() for p in warnings],
        "stats": res.get("stats") or {},
    }
