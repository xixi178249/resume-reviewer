#!/usr/bin/env python3
"""自评：把已经做完的功能，一条条在真服务上跑一遍，出评分卡。

和 run_tests.py 的分工：
  run_tests.py  逐模块的单元/契约测试（562 条，防回归）
  selfcheck.py  从用户视角走完整链路（起真服务、发真请求、读真报告），
                回答「这些功能现在到底能不能用、有没有互相打脸」

用法：
    python scripts/selfcheck.py                 # 自己起服务（mock 模型），跑完关掉
    python scripts/selfcheck.py --base URL      # 对已在跑的服务跑（不会再起一个）
    python scripts/selfcheck.py --json out.json # 结果同时写文件

退出码：有 FAIL 就 1，全过或只有 PARTIAL 就 0。
PARTIAL 的含义是「能用，但有明确短板」，不代表坏了——每条的 reason 会写清楚。
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

GOOD = """姓名：李雷
电话：13800001111　邮箱：lilei@example.com
求职意向：产品经理实习

教育背景
2022.09—2026.06　某某大学　信息管理与信息系统　本科　GPA 3.7/4.0

实习经历
2024.09—2025.06　某某科技有限公司　产品实习生
- 负责订单模块重构的需求梳理，输出 12 份需求文档，上线后客服工单下降 30%
- 主导 A/B 实验 3 轮，把下单转化率从 2.1% 提到 2.8%

项目经历
2025.03—2025.05　校园二手交易平台　产品负责人
- 组织 5 人团队 3 周完成 MVP，注册用户 1200 人，次日留存 41%

技能
Axure、SQL、Python、Excel 数据透视表
"""

# 40~120 字之间：够看出是简历，但五维里至少两维没有证据 → 建议模式
SHORT = """姓名：王芳
电话：13900002222
教育背景：2022.09—2026.06 某某大学 市场营销 本科
实习经历：2024.07—2024.09 某公司 市场实习生，负责公众号运营
技能：Excel、PPT、剪映
"""

TINY = "简历\n姓名：张三\n电话：13700003333\n"


class Card:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def add(self, fid: str, feature: str, check: str, status: str, detail: str = "") -> None:
        self.rows.append({"id": fid, "feature": feature, "check": check,
                          "status": status, "detail": detail})

    def count(self, status: str) -> int:
        return sum(1 for r in self.rows if r["status"] == status)


def post(base: str, path: str, body: dict, timeout: int = 300) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(base + path, data=data, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400]}


def get(base: str, path: str, timeout: int = 60) -> tuple[int, bytes, dict]:
    req = urllib.request.Request(base + path)
    code = 200
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        code = exc.code
    try:
        return code, raw, json.loads(raw.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return code, raw, {}


def delete(base: str, path: str) -> int:
    req = urllib.request.Request(base + path, method="DELETE")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def wait_ready(base: str, timeout: float = 40.0) -> bool:
    end = time.time() + timeout
    while time.time() < end:
        try:
            code, _raw, data = get(base, "/api/health", timeout=3)
            if code == 200 and data.get("ok"):
                return True
        except OSError:
            pass
        time.sleep(0.3)
    return False


# ---------------------------------------------------------------- 探针

def probe_health(card: Card, base: str, ctx: dict) -> None:
    _code, _raw, d = get(base, "/api/health")
    card.add("F1.1", "服务可用", "健康检查返回版本/行业/留存",
             "PASS" if d.get("version") and d.get("industries") else "FAIL",
             f"version={d.get('version')} industries={len(d.get('industries') or [])} "
             f"retention={d.get('retention')}")
    card.add("F1.2", "服务可用", "行业清单不写死在前端（后端给）",
             "PASS" if len(d.get("industries") or []) >= 10 else "FAIL",
             f"取到 {len(d.get('industries') or [])} 个行业档案，首项={(d.get('industries') or [{}])[0].get('label')}")


def probe_main_chain(card: Card, base: str, ctx: dict) -> None:
    code, d = post(base, "/api/extract", {"text": GOOD})
    sid = d.get("session_id") or ""
    ctx["sid"] = sid
    ctx["chars"] = d.get("chars")
    card.add("F2.1", "评审主链", "粘贴正文 → 建会话",
             "PASS" if code == 200 and sid else "FAIL",
             f"chars={d.get('chars')} sid={'有' if sid else '无'}")

    code, r = post(base, "/api/review", {"session_id": sid, "job": "产品经理实习", "mode": "local"})
    ctx["review"] = r
    ok = code == 200 and isinstance(r.get("score"), int) and r.get("report")
    card.add("F2.2", "评审主链", "本地规则引擎出分 + 出报告",
             "PASS" if ok else "FAIL",
             f"score={r.get('score')} verdict={r.get('verdict')} report={len(r.get('report') or '')} 字 "
             f"engine={r.get('engine')}")
    rep = r.get("report") or ""
    h2 = [ln for ln in rep.splitlines() if ln.startswith("## ")]
    card.add("F2.3", "评审主链", "报告结构完整（结论/改写/追问/今天做/核对）",
             "PASS" if len(h2) >= 5 else "PARTIAL",
             f"{len(h2)} 个小节：{'、'.join(x[3:].strip() for x in h2)[:90]}")


def report_json(review_id: str) -> dict:
    """按 review_id 找回落盘的报告 JSON（维度分只在文件里，接口只给总分）。"""
    reports = ROOT / "reports"
    if not reports.exists():
        return {}
    for meta in reports.glob("*.meta.json"):
        try:
            m = json.loads(meta.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if m.get("review_id") == review_id:
            body = meta.with_name(meta.name[: -len(".meta.json")] + ".json")
            if body.exists():
                try:
                    return json.loads(body.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    return {}
    return {}


def probe_bands(card: Card, base: str, ctx: dict) -> None:
    import rubric
    r = ctx.get("review") or {}
    score = r.get("score")
    verdict = r.get("verdict") or ""
    doc = report_json(r.get("review_id") or "")
    dims = doc.get("dimension_scores") or doc.get("dimensions") or {}
    w = rubric.weights_for(r.get("industry") or None)
    expect = round(sum(dims[k]["score"] * w[k] for k in w) * 20) if dims else None
    dims = doc.get("dimension_scores") or {}
    card.add("F3.1", "评分算得对", "总分 = Σ(维度分 × 权重) × 20",
             "PASS" if expect is None or expect == score else "FAIL",
             f"接口={score} 复算={expect} 维度分={ {k: v.get('score') for k, v in dims.items()} }")

    band = rubric.band_for(score if isinstance(score, int) else 0)
    gates = (r.get("gates") or {})
    card.add("F3.2", "评分算得对", "档位与门槛一致（不出现 68 分却「可投」的混乱）",
             "PASS" if band and verdict else "PARTIAL",
             f"score={score} → 段={band[1]}[{band[2]}-{band[3]}] 结论={verdict} "
             f"HR门槛={gates.get('hr')} 面试门槛={gates.get('interview')}")


def probe_industry(card: Card, base: str, ctx: dict) -> None:
    sid = ctx.get("sid") or ""
    _c, a = post(base, "/api/review", {"session_id": sid, "job": "产品经理实习",
                                      "mode": "local", "industry": ""})
    _c, b = post(base, "/api/review", {"session_id": sid, "job": "产品经理实习",
                                      "mode": "local", "industry": "design"})
    da, db = {}, {}
    for r, box in ((a, da), (b, db)):
        rid = r.get("review_id") or ""
        if rid:
            box.update(report_json(rid).get("dimension_scores") or {})
    same_dims = all(da.get(k, {}).get("score") == db.get(k, {}).get("score") for k in da) if da and db else None
    import rubric
    totals = set()
    if db:
        for key, _label, _emph in rubric.list_industries():
            w = rubric.weights_for(key)
            totals.add(round(sum(db[k]["score"] * w[k] for k in w) * 20))
    card.add("F4.1", "行业权重", "换行业：维度分不变、总分按该行业权重重算",
             "PASS" if same_dims and len(totals) >= 2 else "FAIL",
             f"通用={a.get('score')} 设计={b.get('score')} 维度分一致={same_dims} "
             f"11 套权重在这份简历上能算出 {len(totals)} 个不同总分：{sorted(totals)}")
    basis = next((ln for ln in (b.get("report") or "").splitlines() if ln.startswith("> 计分口径：")), "")
    card.add("F4.2", "行业权重", "报告正文写明按哪套权重算（不只写在页面上）",
             "PASS" if basis else "PARTIAL",
             basis[:90] or "报告未说明计分口径")

    _c, bad = post(base, "/api/review", {"session_id": sid, "job": "产品经理实习",
                                        "mode": "local", "industry": "不存在的行业"})
    note = " ".join(bad.get("warnings") or []) + (bad.get("report") or "")
    card.add("F4.3", "行业权重", "传了不认识的行业：不崩、明说按通用算",
             "PASS" if bad.get("ok") and ("通用" in note or "default" in note) else "PARTIAL",
             f"score={bad.get('score')} 提示={'有' if ('通用' in note or 'default' in note) else '无'}")


def probe_followup(card: Card, base: str, ctx: dict) -> None:
    import local_engine
    doc, _gate = local_engine.review_text(GOOD, job="产品经理实习")
    risks = doc.get("followup_risks") or []
    four = [x for x in risks if x.get("ask") and x.get("prepare") and x.get("pitfall")]
    card.add("F5.1", "面试追问", "每条追问给齐「他会问/怎么准备/答错的样子」",
             "PASS" if risks and len(four) == len(risks) else "FAIL",
             f"{len(four)}/{len(risks)} 条齐全")
    pit = [x.get("pitfall", "") for x in risks]
    concrete = sum(1 for p in pit if len(p) >= 12)
    card.add("F5.2", "面试追问", "「答错的样子」是具体的话，不是「多练习」",
             "PASS" if pit and concrete == len(pit) else "PARTIAL",
             f"{concrete}/{len(pit)} 条 ≥12 字；示例：{(pit[0] if pit else '')[:40]}")
    rep = (ctx.get("review") or {}).get("report") or ""
    card.add("F5.3", "面试追问", "报告里排成一节、标签齐全",
             "PASS" if "## 面试官会怎么追问" in rep and "答错的样子：" in rep else "FAIL",
             "报告含该节与三段标签" if "答错的样子：" in rep else "报告缺该节")


def probe_thin(card: Card, base: str, ctx: dict) -> None:
    before = len(list((ROOT / "reports").glob("*.json"))) if (ROOT / "reports").exists() else 0
    _c, d = post(base, "/api/extract", {"text": SHORT})
    code, r = post(base, "/api/review", {"session_id": d.get("session_id") or "",
                                        "job": "市场实习", "mode": "llm"})
    after = len(list((ROOT / "reports").glob("*.json"))) if (ROOT / "reports").exists() else 0
    card.add("F6.1", "低信息简历", "40~120 字：给建议、不给分、不要 key",
             "PASS" if r.get("mode") == "advisory" and r.get("score") is None else "FAIL",
             f"mode={r.get('mode')} score={r.get('score')} engine={r.get('engine')} "
             f"（选的是 llm 且没填 key，仍未报错）")
    card.add("F6.2", "低信息简历", "建议模式不写盘（不留 0 分污染历史）",
             "PASS" if before == after else "FAIL",
             f"reports 目录 {before} → {after}")
    _c, d2 = post(base, "/api/extract", {"text": TINY})
    code2, r2 = post(base, "/api/review", {"session_id": d2.get("session_id") or "", "mode": "local"})
    rejected = d2.get("code") == "TEXT_TOO_SHORT" or r2.get("code") == "RESUME_TOO_THIN"
    msg = (d2.get("error") or r2.get("error") or "")
    card.add("F6.3", "低信息简历", "连结构建议都给不了时，说清门槛",
             "PASS" if rejected and "40" in msg else "FAIL",
             f"拦截码={d2.get('code') or r2.get('code')} 提示={msg[:60]}")


def probe_ats(card: Card, base: str, ctx: dict) -> None:
    rep = (ctx.get("review") or {}).get("report") or ""
    has = "## 招聘系统（ATS）能不能读出来" in rep
    platforms = rep.count("\n### ") if has else 0
    card.add("F7.1", "ATS 平台层", "报告给出各平台清单",
             "PASS" if has and platforms >= 6 else "FAIL",
             f"节={'有' if has else '无'} 平台小节={platforms}")
    three = all(ch in rep for ch in ("✓ 已满足", "✗ 没满足", "? 判不出"))
    card.add("F7.2", "ATS 平台层", "三态齐全，判不出就是判不出",
             "PASS" if three else "FAIL",
             "✓/✗/? 都在" if three else "三态不全")
    unknown_n = rep.count("? 判不出：")
    satisfied_n = rep.count("✓ 已满足：")
    card.add("F7.3", "ATS 平台层", "「判不出」占比合理（信息量够用）",
             "PASS" if 0 < unknown_n <= satisfied_n else "PARTIAL",
             f"判不出 {unknown_n} 条 / 已满足 {satisfied_n} 条（判不出多说明这节信息量偏低）")
    banned = [w for w in ("自动淘汰", "百分之百", "保证通过") if w in rep]
    card.add("F7.4", "ATS 平台层", "不说「会被系统淘汰」这类无法验证的话",
             "PASS" if not banned else "FAIL",
             "未出现" if not banned else f"出现禁用词：{banned}")


def probe_rules(card: Card, base: str, ctx: dict) -> None:
    import rubric
    code, raw, _d = get(base, "/rules")
    text = raw.decode("utf-8", "replace")
    thin = rubric.thin()
    card.add("F8.1", "规则公开", "/rules 可访问且是本项目生成的",
             "PASS" if code == 200 and "评分规则" in text else "FAIL",
             f"{code}，{len(raw)} 字节，表格 {text.count('<table>')} 张")
    nums = str(thin["min_chars"]) in text and str(thin["advisory_max"]) in text
    card.add("F8.2", "规则公开", "页面里的门槛数字与配置一致（不是手抄）",
             "PASS" if nums else "FAIL",
             f"配置 {thin['min_chars']}/{thin['advisory_max']} 字，页面{'有' if nums else '缺'}")
    card.add("F8.3", "规则公开", "平台层与分数段也公开",
             "PASS" if "9.2 平台层" in text and "分数段" in text else "FAIL",
             "9.2 平台层 + 分数段表都在")


def probe_trust(card: Card, base: str, ctx: dict) -> None:
    _c, _raw, m = get(base, "/api/metrics")
    keys = sorted(m.keys())[:8]
    card.add("F9.1", "可信度与闭环", "/api/metrics 可读，且没标注时不编数字",
             "PASS" if (m.get("ok") is False or "n" in json.dumps(m)[:400] or keys) else "PARTIAL",
             f"字段：{keys}；无标注时状态={m.get('status') or m.get('level') or '未标注'}")
    rep = (ctx.get("review") or {}).get("report") or ""
    card.add("F9.2", "可信度与闭环", "报告里写了「这份结果有多可信」",
             "PASS" if ("可信" in rep or "标注" in rep) else "PARTIAL",
             "报告含可信度说明" if ("可信" in rep or "标注" in rep) else "报告未提可信度")


def probe_privacy(card: Card, base: str, ctx: dict) -> None:
    rep = (ctx.get("review") or {}).get("report") or ""
    card.add("F10.1", "隐私", "手机号/邮箱不原样出现在报告里",
             "PASS" if "13800001111" not in rep and "lilei@example.com" not in rep else "FAIL",
             "报告未出现原始手机号与邮箱" if "13800001111" not in rep else "原文泄漏")
    card.add("F10.2", "隐私", "报告里写明这份文件会存多久",
             "PASS" if "保存方式：" in rep else "PARTIAL",
             next((ln for ln in rep.splitlines() if ln.startswith("> 保存方式：")), "报告未写明保存方式")[:80])


def probe_versions(card: Card, base: str, ctx: dict) -> None:
    import shutil
    import tempfile
    import version_store
    tmp = Path(tempfile.mkdtemp(prefix="selfcheck-ver-"))
    try:
        store = version_store.VersionStore(tmp)
        v1 = store.create(GOOD)
        vid = v1["doc_id"]
        store.append(vid, GOOD + "\n补充一行", parent=v1["version"], source="patch")
        items = store.list(vid)
        card.add("F11.1", "版本链", "同一份简历（换文件名也算）归同一条链",
                 "PASS" if len(items) == 2 else "FAIL",
                 f"{len(items)} 个版本，doc_id={vid[:8]}…")
        meta = store.undo(vid)
        card.add("F11.2", "版本链", "撤回不删新版（能再回来）",
                 "PASS" if meta.get("version") == v1["version"] and len(store.list(vid)) == 2 else "FAIL",
                 f"撤回后指向 v{meta.get('version')}，磁盘仍有 2 个版本")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def probe_patch_guard(card: Card, base: str, ctx: dict) -> None:
    import patch_protocol as pp
    base_text = GOOD
    bad = [pp.Patch(id="p1", quote="客服工单下降 30%", new_text="客服工单下降 85%")]
    out = pp.apply_patches(base_text, bad)
    card.add("F12.1", "改稿守卫", "编造数字被拦在写入前",
             "PASS" if out["blocked"] and not out["applied"] and "85%" not in out["text"] else "FAIL",
             f"拦下 {len(out['blocked'])} 条，原因="
             f"{(out['blocked'][0]['reasons'][0]['message'] if out['blocked'] else '无')[:40]}")
    good = [pp.Patch(id="p2", quote="负责订单模块重构的需求梳理",
                     new_text="负责订单模块重构的需求梳理（对接 3 个业务方）")]
    out2 = pp.apply_patches(base_text, good)
    card.add("F12.2", "改稿守卫", "合法改写通过，且只动被引用的那一处",
             "PASS" if out2["applied"] and "对接 3 个业务方" in out2["text"] else "FAIL",
             f"应用 {len(out2['applied'])} 条，新长度 {len(out2['text'])}（原文 {len(base_text)}）")
    spans = pp.locate(base_text, "负责订单模块重构的需求梳理")
    card.add("F12.3", "改稿守卫", "引用能定位到原文（空白/换行不同也能对上）",
             "PASS" if spans else "FAIL", f"定位到 {len(spans)} 处")


def probe_chat(card: Card, base: str, ctx: dict) -> None:
    # v2.6 之前这里是「没填 key 就 CHAT_NO_KEY」——用户反馈那等于「不用 key 用不了」。
    # 现在的契约是：没 key 开规则模式，能改、能重评、能报差，只是不逐句生成改写。
    code, d = post(base, "/api/chat/start", {"text": GOOD})
    card.add("F13.1", "改稿工作台", "没填 key：开规则模式，而不是拒绝开工",
             "PASS" if d.get("ok") and d.get("mode") == "rules" else "FAIL",
             f"ok={d.get('ok')} mode={d.get('mode')} code={d.get('code')}")
    chat_id = d.get("chat_id") or ""
    v1_score = ((d.get("versions") or [{}])[0] or {}).get("score")
    card.add("F13.2", "改稿工作台", "规则模式开台当场打出 v1（否则第一次改完没法报差）",
             "PASS" if isinstance(v1_score, int) else "FAIL", f"v1 score={v1_score}")
    code, r = post(base, "/api/chat/send", {"chat_id": chat_id, "message": "实习那条怎么改"})
    card.add("F13.3", "改稿工作台", "规则模式不假装会写句子（manual=True 且零补丁）",
             "PASS" if r.get("ok") and r.get("manual") and (r.get("patches") or []) == [] else "FAIL",
             f"ok={r.get('ok')} manual={r.get('manual')} patches={len(r.get('patches') or [])}")
    edited = GOOD + "\n补充：上线后接口错误率从 1.8% 降到 0.2%，支撑日均 3000 单"
    code, e = post(base, "/api/chat/edit", {"chat_id": chat_id, "text": edited})
    ok = bool(e.get("ok")) and e.get("version") == 2 and isinstance(e.get("delta"), int)
    card.add("F13.4", "改稿工作台", "「自己改这一版」：存成 v2 并当场报分差",
             "PASS" if ok else ("PARTIAL" if e.get("ok") else "FAIL"),
             f"v{e.get('version')} score={e.get('score')} before={e.get('before')} delta={e.get('delta')}")
    code, d2 = post(base, "/api/chat/apply", {"chat_id": "不存在"})
    card.add("F13.5", "改稿工作台", "不存在的会话：报错而不是 500",
             "PASS" if code in (200, 400, 404) and d2.get("ok") is False else "PARTIAL",
             f"{code} ok={d2.get('ok')} error={(d2.get('error') or '')[:40]}")


def probe_errors(card: Card, base: str, ctx: dict) -> None:
    code, _raw, _d = get(base, "/api/nope")
    card.add("F14.1", "边界", "未知接口 404（不乱返回 HTML）", "PASS" if code == 404 else "FAIL", f"{code}")
    code, d = post(base, "/api/review", {"session_id": "过期的会话"})
    card.add("F14.2", "边界", "过期会话给可读提示",
             "PASS" if d.get("ok") is False and "过期" in (d.get("error") or "") else "FAIL",
             f"error={(d.get('error') or '')[:40]}")
    code, _raw, d3 = get(base, "/api/download?id=../../../etc/passwd")
    card.add("F14.3", "边界", "下载接口不接受路径（只能按记录 ID）",
             "PASS" if (d3.get("ok") is False or code in (400, 404)) else "PARTIAL",
             f"{code} ok={d3.get('ok')}")


PROBES = [
    probe_health, probe_main_chain, probe_bands, probe_industry, probe_followup,
    probe_thin, probe_ats, probe_rules, probe_trust, probe_privacy,
    probe_versions, probe_patch_guard, probe_chat, probe_errors,
]


def main(argv: list[str]) -> int:
    try:  # Windows 控制台默认 GBK，报告里有 ✓/✗ 会直接抛 UnicodeEncodeError
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
    base = ""
    json_out = ""
    i = 0
    while i < len(argv):
        if argv[i] == "--base" and i + 1 < len(argv):
            base = argv[i + 1].rstrip("/")
            i += 2
        elif argv[i] == "--json" and i + 1 < len(argv):
            json_out = argv[i + 1]
            i += 2
        else:
            i += 1

    proc = None
    if not base:
        port = free_port()
        base = f"http://127.0.0.1:{port}"
        # 版本链必须落在临时目录：doc_id 是正文的 sha256，而探针用的是固定 GOOD 文本，
        # 上一轮留在 reports/versions 里的 v2 会让这一轮「自己改这一版」直接吃 CHAT_UNCHANGED
        # （真踩过：F13.4 从 PASS 变成 FAIL，查了半天是自评自己的残留）。
        import atexit
        import shutil
        import tempfile

        chat_tmp = tempfile.mkdtemp(prefix="selfcheck-chat-")
        atexit.register(shutil.rmtree, chat_tmp, True)
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   CHAT_STORE_DIR=str(Path(chat_tmp) / "versions"))
        proc = subprocess.Popen(
            [sys.executable, str(ROOT / "web" / "server.py"), "--port", str(port), "--mock"],
            cwd=str(ROOT), env=env,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    try:
        if not wait_ready(base):
            print(f"服务没起来：{base}")
            return 1
        card = Card()
        ctx: dict = {}
        for fn in PROBES:
            try:
                fn(card, base, ctx)
            except Exception as exc:  # noqa: BLE001
                card.add(getattr(fn, "__name__", "?"), "探针异常", "执行过程不崩", "FAIL", str(exc)[:120])
        # 收尾：把自评造出来的报告删干净
        deleted = 0
        for r in card.rows:
            pass
        rid = (ctx.get("review") or {}).get("review_id") or ""
        if rid and delete(base, f"/api/history?id={rid}") in (200, 204):
            deleted += 1

        width_feature = max(len(r["feature"]) for r in card.rows)
        print()
        print(f"{'编号':<7}{'功能':<{width_feature + 2}}{'状态':<9}说明")
        print("-" * 104)
        for r in card.rows:
            print(f"{r['id']:<7}{r['feature']:<{width_feature + 2}}{r['status']:<9}{r['detail'][:78]}")
        print("-" * 104)
        p, pa, f = card.count("PASS"), card.count("PARTIAL"), card.count("FAIL")
        total = len(card.rows)
        print(f"共 {total} 条：PASS {p} / PARTIAL {pa} / FAIL {f}"
              f"　（通过率 {p / total * 100:.0f}%，含部分通过 {((p + pa) / total) * 100:.0f}%）")
        if json_out:
            Path(json_out).write_text(json.dumps(card.rows, ensure_ascii=False, indent=2), "utf-8")
            print(f"明细已写入 {json_out}")
        return 1 if f else 0
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
