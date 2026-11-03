"""对话改稿引擎：把「用户一句话」变成「可确认的补丁」，再变成「新版本 + 新分数」。

四条产品要求直接钉在这里：
1. 所有模型调用都用调用方传进来的 LLMConfig（访客自己的 key）。这里**没有**任何
   "回退到服务器 key" 的分支——一旦回退，别人用你的额度就变成默认行为了。
2. 评分标准主体不变：重评走同一个 prompt 模板与同一个 quality_gate，不改 rubric。
3. 内容与排版分开：每条补丁的 category 由 patch_protocol 按字符算，不采信模型自报。
4. 上下文只带「当前全文 + 上一轮问题 + rubric 摘要 + 最近 3 轮 + 结构化摘要」。
   全量历史只会让第 5 轮开始又慢又贵，而且更不听话。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import patch_protocol
import quality_gate
import review as review_mod
import rubric
import run_eval
import version_diff

STAGES = ("理解", "提改法", "校验事实", "重评")

HISTORY_TURNS = 3          # 只带最近 3 轮对话
ISSUES_IN_CONTEXT = 6      # 上一轮报告里带前 6 条问题
MAX_FILLS = 10

SYSTEM = """你是简历改稿助手。你只做一件事：把用户这一句指令变成「可确认的补丁」，由用户逐条决定采用与否。

硬性规则（违反任何一条，整轮的补丁都会被丢弃）：
1. 只输出 JSON 对象，形如：
   {"reply": "一句话说清改哪、为什么、你要自己补什么",
    "patches": [{"id": "p1", "quote": "简历里逐字存在的原文片段", "new_text": "替换后的文本",
                 "why": "为什么这么改", "fact_delta": "none", "needs_user_confirm": false}]}
2. `quote` 必须是当前简历里**逐字存在**的一段（10~120 字），不要改写、不要合并多行、不要加省略号。
3. `new_text` 里不许出现简历原文没有的数字。需要数据时写占位符 `«待补：具体指标»`，由用户自己填。
4. 最多 5 条补丁。宁可只改 1 条改对，也不要给 5 条凑数。
5. `fact_delta` 只能取 `none` / `wording` / `number_added` / `ownership_upgrade` / `scope_change`；
   只要不是 `none`，`needs_user_confirm` 必须为 `true`（让用户确认他真做过这件事）。
6. 你自己不打分、不改评分标准、不给总分。分数由系统在应用补丁后重新评。
7. 排版类改动只允许动空格、标点、换行、分隔符；动到任何实词就算内容改动，必须如实标 `fact_delta`。
8. 用户说「只改某个板块」时，所有 `quote` 都必须来自那个板块；在简历里找不到该板块就直说，不要编。
9. 这句话不需要改、或者你没有把握时：`patches` 返回空数组，在 `reply` 里说明原因。不要为了给结果而硬改。
10. `reply` 用中文，不超过 200 字，不要复述规则、不要客套。

评分标准摘要（只用于判断「什么该改」，不要在回复里复述）：
__BRIEF__
"""

REPAIR_NOTE = """你上一次的输出没法用：{reason}

请重新输出**完整的** JSON 对象（不要解释、不要 Markdown 代码块之外的文字、不要遗漏字段）。
特别注意：`quote` 必须是简历里逐字存在的原文片段；`new_text` 里不许出现原文没有的数字。
除上述要求外，其余规则保持不变。"""


def _num(key: str, default):
    """读 rubric 里的数字，缺键就退回默认值。

    rubric.num() 在缺键时会抛错（这是它的设计：宁可炸也不要静默用错阈值）。
    但提示词摘要属于"少一个格不影响主流程"的场景，所以这里显式兜底并留痕。
    """
    try:
        return rubric.num(key)
    except Exception:  # noqa: BLE001 - 缺键/类型不对都退回默认值
        return default


def rubric_brief() -> str:
    """把 rubric.yaml 里的关键数字拼成一段摘要。

    从配置读而不是写死：这套阈值在 v2.0 已经被抽成单一真源，
    这里再抄一份就等于把"两套标准"的老毛病请回来。
    """
    w = rubric.weights()
    total = sum(v for v in w.values() if isinstance(v, (int, float)))
    scale = 100 if total and total <= 1.5 else 1   # 配置里可能写成 0.25，也可能写成 25
    pct = {k: (v * scale if isinstance(v, (int, float)) else v) for k, v in w.items()}
    pct = {k: (int(v) if isinstance(v, float) and v.is_integer() else v) for k, v in pct.items()}
    long_chars = _num("readability.long_sentence_chars", 60)
    min_q = _num("evidence.min_quote", 6)
    max_q = _num("evidence.max_quote", 200)
    role_min = _num("gates.hr.role_fit_min", 3)
    impact_min = _num("gates.hr.impact_min", 2)
    eq_min = _num("gates.interview.evidence_quality_min", 3)
    strong = _num("verdict.strong_pass_min", 80)
    pass_min = _num("verdict.pass_min", 65)
    mid = _num("verdict.borderline_min", 50)
    pages_chars = _num("format.pages_chars", 900)
    return (
        f"- 五个维度与权重：岗位匹配 {pct.get('role_fit')}% / 成果量化 {pct.get('impact')}% / "
        f"证据质量 {pct.get('evidence_quality')}% / 结构可读性 {pct.get('structure')}% / 竞争力 {pct.get('competitiveness')}%\n"
        f"- 单句超过 {long_chars} 字算长句，要拆\n"
        f"- 事实引用长度必须在 {min_q}~{max_q} 字之间，且能回指原文\n"
        f"- 初筛通过需要：岗位匹配 ≥{role_min} 且 成果量化 ≥{impact_min}\n"
        f"- 面试通过需要：证据质量 ≥{eq_min}\n"
        f"- 结论分档：≥{strong} 强通过 / ≥{pass_min} 通过 / ≥{mid} 边缘 / 其余淘汰\n"
        f"- 一页约 {pages_chars} 字，超了就是排版问题"
    )


def _issues_brief(doc: dict | None) -> str:
    """上一轮评审的问题清单（最多 6 条），给模型当"当前该先改什么"。"""
    if not doc:
        return "（这份简历还没有评审报告，先按通用标准改）"
    rows = []
    for it in (doc.get("issues") or [])[:ISSUES_IN_CONTEXT]:
        if not isinstance(it, dict):
            continue
        where = (it.get("where") or "").strip()
        problem = (it.get("problem") or it.get("issue") or "").strip()
        fix = (it.get("fix") or "").strip()
        assertion = it.get("assertion") or "judgment"
        if problem:
            rows.append(f"- [{assertion}] {where}：{problem}" + (f" → 建议：{fix}" if fix else ""))
    if not rows:
        return "（报告里没有列出的问题）"
    return "\n".join(rows)


def _summary_brief(summary: dict) -> str:
    """结构化摘要：已采纳 / 已拒绝 / 待确认。避免模型反复推荐你否掉的改法。"""
    parts = []
    for key, label in (("accepted", "已采纳"), ("rejected", "已拒绝"), ("pending", "待确认")):
        items = summary.get(key) or []
        if items:
            parts.append(f"{label}：" + "；".join(items[-5:]))
    return "\n".join(parts) if parts else "（这是第一轮，还没有已采纳或已拒绝的改法）"


def build_messages(
    *,
    text: str,
    job: str,
    jd: str,
    doc: dict | None,
    history: list[dict],
    summary: dict,
    message: str,
) -> list[dict]:
    # 用 replace 而不是 str.format：SYSTEM 里含 JSON 示例（{"reply": ...}），
    # format 会把花括号当占位符，直接 KeyError: '"reply"'
    system = SYSTEM.replace("__BRIEF__", rubric_brief())
    ctx = [
        "## 当前简历全文",
        text,
        "",
        "## 上一轮评审发现的问题",
        _issues_brief(doc),
        "",
        "## 已经发生过的事",
        _summary_brief(summary),
    ]
    if jd.strip():
        ctx += ["", "## 目标岗位 JD", jd.strip()[:1200]]
    messages: list[dict] = [{"role": "system", "content": system}]
    for turn in history[-HISTORY_TURNS:]:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": "\n".join(ctx) + "\n\n## 用户这一句\n" + message})
    return messages


def _fills_to_numbers(fills: dict[str, str]) -> set[str]:
    import check_output
    out: set[str] = set()
    for v in (fills or {}).values():
        out |= check_output.numbers_in(str(v))
    return out


class ChatSession:
    """一次「边聊边改」的会话：绑定一条版本链，所有动作都能追溯到版本号。"""

    def __init__(
        self,
        store,
        doc_id: str,
        *,
        job: str = "",
        jd: str = "",
        config=None,
        prompt_version: str | None = None,
        repeat: int = 1,
    ):
        self.store = store
        self.doc_id = doc_id
        self.job = job
        self.jd = jd
        self.config = config
        self.prompt_version = prompt_version or run_eval.DEFAULT_PROMPT_VERSION
        self.repeat = max(1, min(3, int(repeat or 1)))
        self.history: list[dict] = []
        self.summary = {"accepted": [], "rejected": [], "pending": []}
        self.patches: list[patch_protocol.Patch] = []

    # ------------------------------------------------------------ 内部
    def current(self) -> dict:
        return self.store.latest(self.doc_id)

    def _previous_doc(self, version: int) -> dict | None:
        return self.store.load_review(self.doc_id, version)

    # ------------------------------------------------------------ 对话
    def send(self, message: str) -> dict:
        """一轮对话。返回 {ok, reply, patches, problems}；格式不对会重试一次后明确报错。"""
        text = self.current()["text"]
        doc = self.store.load_review(self.doc_id, self.current()["version"])
        messages = build_messages(
            text=text, job=self.job, jd=self.jd, doc=doc,
            history=self.history, summary=self.summary, message=message,
        )

        reason = ""
        for attempt in (1, 2):
            try:
                raw = run_eval.chat(messages, json_mode=True, config=self.config)
            except run_eval.LLMError as exc:
                # key/余额/地址问题：重试只会继续烧用户的钱，直接往上抛
                raise
            try:
                reply, patches, problems = patch_protocol.parse(raw)
            except patch_protocol.PatchFormatError as exc:
                reason = str(exc)
                if attempt == 1:
                    messages = messages + [
                        {"role": "assistant", "content": (raw or "")[:400]},
                        {"role": "user", "content": REPAIR_NOTE.format(reason=reason)},
                    ]
                    continue
                # 第二次还不行就明说。绝不悄悄降级成纯文本——用户会以为补丁生效了
                return {
                    "ok": False,
                    "code": "CHAT_FORMAT",
                    "message": "没听懂，换个说法（模型这次没按格式返回，什么都没改）。",
                    "detail": reason,
                }

            self.patches = patches
            self.history.append({"role": "user", "content": message})
            self.history.append({"role": "assistant", "content": reply or "（无文字回复）"})
            self.history = self.history[-HISTORY_TURNS * 2:]
            return {
                "ok": True,
                "reply": reply,
                "patches": [
                    {**p.to_dict(), "issues": patch_protocol.fact_issues(p, text)}
                    for p in patches
                ],
                "problems": problems,
                "version": self.current()["version"],
            }
        return {"ok": False, "code": "CHAT_FORMAT", "message": "没听懂，换个说法。"}

    # ------------------------------------------------------------ 落地
    def apply(self, patch_ids: list[str], fills: dict[str, str] | None = None) -> dict:
        """把用户勾选的补丁写进新版本，然后重评、报差、给建议。"""
        if not self.patches:
            return {"ok": False, "code": "NO_PATCHES", "message": "还没有可用的补丁，先聊一句。"}

        want = [str(i) for i in (patch_ids or [])]
        chosen = [p for p in self.patches if p.id in want] or self.patches
        fills = {str(k): str(v) for k, v in (fills or {}).items()} or {}
        if len(fills) > MAX_FILLS:
            return {"ok": False, "code": "TOO_MANY_FILLS", "message": f"一次最多填 {MAX_FILLS} 个占位符。"}

        for p in chosen:
            for key, val in fills.items():
                p.new_text = p.new_text.replace(key, val)
            if fills and patch_protocol.PLACEHOLDER_RE.search(p.quote) is None:
                # 用户手填的数字原文没有，来源是用户本人：放行但要打上标记
                p.needs_user_confirm = True

        prev = self.current()
        out = patch_protocol.apply_patches(
            prev["text"], chosen, extra_numbers=_fills_to_numbers(fills)
        )
        if not out["changed"]:
            return {
                "ok": False,
                "code": "NOTHING_APPLIED",
                "message": "这些补丁一条都没能落下：" + patch_protocol.summarize(
                    out["applied"], out["skipped"], out["blocked"]),
                "detail": out,
            }

        record = self.store.append(
            self.doc_id,
            out["text"],
            parent=prev["version"],
            source="patch",
            applied_patch_ids=[a["id"] for a in out["applied"]],
            meta={"fills": fills, "job": self.job, "jd": self.jd},
        )
        self.summary["accepted"].extend(a["why"] or a["id"] for a in out["applied"])
        self.patches = []

        result = {
            "ok": True,
            "version": record["version"],
            "parent": prev["version"],
            "applied": out["applied"],
            "skipped": out["skipped"],
            "blocked": out["blocked"],
            "summary": patch_protocol.summarize(out["applied"], out["skipped"], out["blocked"]),
            "text": out["text"],
        }
        result.update(self._rereview(record, prev, out["text"]))
        return result

    def _rereview(self, record: dict, prev: dict, text: str) -> dict:
        """重评新版本并与上一版比。失败也要把原因带回去，不能只说"已更新"。"""
        case = {"job_target": self.job or "实习岗位", "jd": self.jd}
        try:
            prompt = run_eval.build_prompt(text, case, version=self.prompt_version)
            doc, scores, quality = review_mod.run_reviews(
                prompt, [], n=self.repeat, resume=text, config=self.config
            )
        except quality_gate.QualityGateError as exc:
            reasons = [str(p) for p in getattr(exc, "problems", [])]
            self.store.update_meta(self.doc_id, record["version"], review_status="rejected")
            return {
                "review": {"ok": False, "code": "REVIEW_REJECTED",
                           "message": "新版本没能通过质量门禁，分数不可信。", "reasons": reasons},
                "compare": None,
            }

        eligible = bool(quality.get("eligible"))
        self.store.save_review(self.doc_id, record["version"], doc)
        self.store.update_meta(
            self.doc_id, record["version"],
            score=doc.get("overall_score"), verdict=doc.get("verdict"),
            review_status="ok" if eligible else "rejected", engine="llm",
        )

        prev_doc = self._previous_doc(prev["version"])
        compare = version_diff.compare_scores(prev_doc, doc) if prev_doc else {
            "score": {"before": prev.get("score"), "after": doc.get("overall_score"),
                      "delta": (doc.get("overall_score") - prev["score"])
                      if isinstance(prev.get("score"), int) and isinstance(doc.get("overall_score"), int)
                      else None},
            "verdict": {"before": prev.get("verdict"), "after": doc.get("verdict")},
            "dims": [], "gates": {}, "format": {},
        }
        applied_status = version_diff.check_applied(prev_doc, text) if prev_doc else []

        delta = (compare.get("score") or {}).get("delta")
        advice = ""
        if isinstance(delta, int) and delta < 0:
            advice = f"这版比上一版低 {abs(delta)} 分，可以撤回（撤销会回到 v{prev['version']}）。"
        elif isinstance(delta, int) and delta > 0:
            advice = f"比上一版高 {delta} 分。"

        return {
            "review": {"ok": eligible, "score": doc.get("overall_score"),
                       "verdict": doc.get("verdict"), "scores": scores,
                       "doc": doc},
            "compare": compare,
            "applied_status": applied_status,
            "advice": advice,
        }

    def undo(self) -> dict:
        back = self.store.undo(self.doc_id)
        return {"ok": True, "version": back["version"], "text": back["text"],
                "score": back.get("score"), "message": f"已撤回 v{back['version']}。"}
