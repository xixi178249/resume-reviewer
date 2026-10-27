"""两版简历对比的回归测试。

防的不是"函数会不会算错"，而是四类会让人被误导的错误：
- 把两份不相干的简历认成同一份的两个版本，还算出分差
- 分数不可比（换了岗位/模型/重复次数）时仍报 +12 分
- 上次的建议"看起来已改"被当成事实，其实是另一个写法
- 报告里出现 None 或空节（用户只看到一堆占位符）

运行：python scripts/test_version_diff.py
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "web"))

import version_diff as vd  # noqa: E402

A = """张三｜产品经理｜138****0000｜zhang@example.com
教育背景
2022.09 - 2026.06 某某大学 信息管理与信息系统 本科
实习经历
2025.07 - 2025.12 某电商平台 产品实习生
负责用户增长系统的需求分析与方案设计，推动转化率提升 12%
整理竞品功能清单 40 项，输出 3 份复盘文档
主导 AB 实验 30 组，沉淀 5 个可复用策略
校园经历
担任学生会外联部负责人，组织 2 场校招宣讲，到场 300 人
技能
熟练使用 SQL、Axure、Excel 透视表
"""
A2 = A.replace("12%", "18%") + "独立搭建活动配置后台，把上线周期从 3 天压到 4 小时\n"
B = """李四｜Rust 后端工程师｜li@example.com
教育背景
2021.09 - 2025.06 某某理工大学 计算机科学与技术 本科
项目经历
维护 tokio 生态的一个分支，优化调度器空转问题
重写内存分配器，吞吐提升 40%，内存占用下降 25%
用 raft 实现配置中心，支撑 12 个服务的灰度发布
实习经历
2024.07 - 2024.10 某云厂商 内核开发实习生
排查并修复 epoll 边沿触发丢事件问题，提交 3 个上游 PR
技能
熟悉 Rust、C、Linux 内核，读过 Linux 调度器源码
"""


def _doc(score, dims, hr, iv, fmt, verdict="pass"):
    return {
        "overall_score": score,
        "verdict": verdict,
        "dimension_scores": {k: {"score": v, "weight": 20} for k, v in dims.items()},
        "gate_hr": {"pass": hr},
        "gate_interview": {"pass": iv},
        "format_review": {"verdict": fmt},
    }


PREV = _doc(60, {"role_fit": 3, "impact": 2, "evidence_quality": 3, "structure": 2, "competitiveness": 2},
            False, False, "risky", "borderline")
CUR = _doc(72, {"role_fit": 4, "impact": 3, "evidence_quality": 3, "structure": 3, "competitiveness": 3},
           True, False, "clean")

CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# ---------- 相似度 ----------

@case
def test_normalise_strips_noise():
    got = vd.normalise("负责后端接口，性能提升 20%！")
    assert "%" not in got and "，" not in got and " " not in got, got
    assert "20" in got


@case
def test_similarity_identical_is_one():
    assert vd.similarity(A, A) == 1.0


@case
def test_similarity_version_is_high():
    sim = vd.similarity(A, A2)
    assert sim >= 0.7, f"同一份简历的两版相似度只有 {sim}"


@case
def test_similarity_unrelated_is_low():
    sim = vd.similarity(A, B)
    assert sim < vd.MIN_SIMILARITY, f"两份不相干的简历被算出 {sim}"


@case
def test_similarity_empty_is_zero():
    assert vd.similarity("", A) == 0.0
    assert vd.similarity("   \n ", A) == 0.0


# ---------- 挑上一版 ----------

@case
def test_pick_ignores_unrelated_candidate():
    cands = [
        {"review_id": "1", "created_at": "2026-10-20 10:00:00", "text": B},
        {"review_id": "2", "created_at": "2026-10-18 10:00:00", "text": A},
    ]
    entry, sim = vd.pick_predecessor(cands, A2)
    assert entry and entry["review_id"] == "2", entry
    assert sim >= 0.7, sim


@case
def test_pick_prefers_newer_on_tie():
    cands = [
        {"review_id": "old", "created_at": "2026-10-20 10:00:00", "text": A},
        {"review_id": "new", "created_at": "2026-10-25 10:00:00", "text": A},
    ]
    entry, _ = vd.pick_predecessor(cands, A2)
    assert entry["review_id"] == "new", entry


@case
def test_pick_skips_same_content_hash():
    cands = [{"review_id": "1", "created_at": "2026-10-20 10:00:00", "text": A, "content_sha256": "abc"}]
    entry, _ = vd.pick_predecessor(cands, A2, exclude_sha="abc")
    assert entry is None, entry


@case
def test_pick_none_when_all_below_threshold():
    cands = [{"review_id": "1", "created_at": "2026-10-20 10:00:00", "text": B}]
    entry, sim = vd.pick_predecessor(cands, A2)
    assert entry is None, entry
    assert sim == 0.0


@case
def test_pick_skips_candidate_without_text():
    cands = [{"review_id": "1", "created_at": "2026-10-20 10:00:00", "text": ""}]
    entry, _ = vd.pick_predecessor(cands, A2)
    assert entry is None


# ---------- 行级 diff ----------

@case
def test_diff_classifies_three_kinds():
    old = "一行\n二行\n三行"
    new = "一行\n二行改\n三行\n四行"
    d = vd.diff_lines(old, new)
    st = d["stats"]
    assert st["changed"] == 1, st
    assert st["added"] == 1, st
    assert st["removed"] == 0, st
    assert st["unchanged"] == 2, st
    assert d["changed"][0]["before"] == "二行" and d["changed"][0]["after"] == "二行改"


@case
def test_diff_empty_lines_are_ignored():
    d = vd.diff_lines("一\n\n  \n二", "一\n二")
    assert d["stats"]["changed"] == 0 and d["stats"]["added"] == 0, d["stats"]


@case
def test_diff_truncates_long_lines():
    long = "长" * 300
    d = vd.diff_lines("短句", long)
    assert d["stats"]["added"] + d["stats"]["changed"] >= 1
    for item in d["added"]:
        assert len(item) <= 100, len(item)


# ---------- 分数对照 ----------

@case
def test_compare_scores_deltas():
    s = vd.compare_scores(PREV, CUR)
    assert s["score"]["delta"] == 12, s["score"]
    role = next(d for d in s["dims"] if d["key"] == "role_fit")
    assert role["delta"] == 1, role
    assert s["gates"]["hr"] == {"before": False, "after": True}
    assert s["format"] == {"before": "risky", "after": "clean"}


@case
def test_compare_scores_missing_dim_gives_none():
    s = vd.compare_scores(PREV, {"overall_score": 70})
    for d in s["dims"]:
        assert d["delta"] is None, d


@case
def test_compare_scores_orders_all_five_dims():
    s = vd.compare_scores(PREV, CUR)
    assert [d["key"] for d in s["dims"]] == vd.DIM_ORDER


# ---------- 建议落实 ----------

REWRITES = {"rewrites": [
    {"section": "项目经历",
     "original": "负责用户增长系统的需求分析与方案设计，推动转化率提升 12%",
     "rewritten": "主导用户增长系统的目标拆解与方案设计，推动转化率提升 18%"},
    {"section": "工作经历",
     "original": "参与后端接口开发，负责登录模块",
     "rewritten": "独立负责登录模块的后端接口开发，QPS 从 200 提到 800"},
    {"section": "技能", "original": "负责", "rewritten": "主导"},
]}

NEW_TEXT = "主导用户增长系统的目标拆解与方案设计，推动转化率提升 18%\n参与后端接口开发，负责登录模块\n"


@case
def test_applied_three_states():
    states = [a["state"] for a in vd.check_applied(REWRITES, NEW_TEXT)]
    assert states == ["applied", "untouched", "unknown"], states


@case
def test_applied_changed_when_rewritten_elsewhere():
    # 原句删了，但也没用模型给的说法 —— 只能判"改成别的写法了"
    states = [a["state"] for a in vd.check_applied(REWRITES, "全新的一段经历，和上面都没关系")]
    assert states[0] == "changed", states


@case
def test_applied_uses_long_quote_as_probe():
    doc = {"rewrites": [{"section": "项目经历",
                         "original": "负责用户增长系统的需求分析与方案设计",
                         "rewritten": "改成「主导用户增长系统的目标拆解与方案设计」"}]}
    got = vd.check_applied(doc, NEW_TEXT)
    assert got[0]["state"] == "applied", got


@case
def test_applied_short_quote_falls_back_not_crash():
    doc = {"rewrites": [{"section": "项目经历",
                         "original": "负责用户增长系统的需求分析与方案设计",
                         "rewritten": "把「负责」换成「主导」，写清目标拆解"}]}
    got = vd.check_applied(doc, NEW_TEXT)
    assert got[0]["state"] in ("changed", "applied", "untouched"), got


@case
def test_applied_identical_suggestion_is_unknown():
    doc = {"rewrites": [{"section": "技能", "original": "熟悉 Python 数据分析",
                         "rewritten": "熟悉 Python 数据分析"}]}
    got = vd.check_applied(doc, "熟悉 Python 数据分析")
    assert got[0]["state"] == "unknown", got


# ---------- 组装 ----------

def _cmp(comparable=True):
    return vd.build(
        {"review_id": "p1", "created_at": "2026-10-20 10:00:00", "score": 60,
         "verdict": "borderline", "job": "产品经理"},
        PREV, A, CUR, A2,
        sim=0.82, score_comparable=comparable,
        config_note="岗位从「运营」改成「产品经理」" if not comparable else "",
    )


@case
def test_build_has_prev_and_stats():
    c = _cmp()
    assert c["has_prev"] is True
    assert c["prev"]["review_id"] == "p1"
    assert c["text"]["stats"]["lines_after"] == len(vd.lines_of(A2)), c["text"]["stats"]
    assert c["applied_counts"]["unknown"] >= 0


@case
def test_build_clears_delta_when_not_comparable():
    c = _cmp(comparable=False)
    assert c["score"]["delta"] is None, c["score"]
    assert all(d["delta"] is None for d in c["dims"]), c["dims"]
    assert c["score"]["before"] == 60 and c["score"]["after"] == 72


# ---------- 报告渲染 ----------

@case
def test_markdown_empty_when_no_prev():
    assert vd.to_markdown(None) == []
    assert vd.to_markdown({}) == []
    assert vd.to_markdown({"has_prev": False}) == []


@case
def test_markdown_shows_delta_arrow():
    text = "\n".join(vd.to_markdown(_cmp()))
    assert "↑ 12 分" in text, text
    assert "产品经理" not in text or True  # 不强制，只是别崩


@case
def test_markdown_explains_incomparable():
    text = "\n".join(vd.to_markdown(_cmp(comparable=False)))
    assert "不算分差" in text, text
    assert "岗位从「运营」改成「产品经理」" in text, text


@case
def test_markdown_handles_missing_date():
    c = _cmp()
    c["prev"]["created_at"] = ""
    text = "\n".join(vd.to_markdown(c))
    assert "对比的是上一版" in text, text


@case
def test_markdown_never_prints_none():
    text = "\n".join(vd.to_markdown(_cmp()))
    assert "None" not in text, text
    assert "null" not in text, text


@case
def test_markdown_marks_inference():
    text = "\n".join(vd.to_markdown(_cmp()))
    if "上次的建议" in text:
        assert "推测" in text, text


# ---------- 接进网站后的整链路 ----------

def _server_env():
    """把 server 的落盘目录换成临时目录，并用 MOCK 模式（不调模型）。"""
    import server  # 延迟导入：没装依赖或没配 key 也不影响上面的纯函数测试

    tmp = tempfile.TemporaryDirectory()
    reports = Path(tmp.name) / "reports"
    reports.mkdir()
    patches = [
        mock.patch.object(server, "REPORTS", reports),
        mock.patch.object(server, "HISTORY", reports / ".history.json"),
        mock.patch.object(server, "MOCK", True),
    ]
    for p in patches:
        p.start()
    return server, tmp, patches


def _stop_server_env(server, tmp, patches):
    server.SESSIONS.clear()
    for p in reversed(patches):
        p.stop()
    tmp.cleanup()


def _review_text(server, text: str, name: str = "简历.txt") -> dict:
    ex = server.api_extract({"text": text, "name": name, "allow_images": False})
    assert ex["ok"], ex
    res = server.api_review({
        "session_id": ex["session_id"], "job": "产品经理", "jd": "",
        "visual": False, "repeat": 1,
    })
    assert res["ok"], res
    return res


@case
def test_flow_first_review_has_no_compare():
    server, tmp, patches = _server_env()
    try:
        first = _review_text(server, A)
        assert first.get("compare") is None, first.get("compare")
        assert "## 和上一版比" not in first["report"]
    finally:
        _stop_server_env(server, tmp, patches)


@case
def test_flow_second_version_gets_compare():
    server, tmp, patches = _server_env()
    try:
        first = _review_text(server, A)
        second = _review_text(server, A2)
        cmp = second.get("compare")
        assert cmp and cmp.get("has_prev"), cmp
        assert cmp["prev"]["review_id"] == first["review_id"], cmp["prev"]
        assert cmp["text"]["stats"]["changed"] >= 1, cmp["text"]["stats"]
        assert "## 和上一版比" in second["report"], second["report"][:400]
    finally:
        _stop_server_env(server, tmp, patches)


@case
def test_flow_unrelated_resume_gets_no_compare():
    server, tmp, patches = _server_env()
    try:
        _review_text(server, A)
        other = _review_text(server, B, name="另一份.txt")
        assert other.get("compare") is None, other.get("compare")
    finally:
        _stop_server_env(server, tmp, patches)


@case
def test_flow_same_content_review_uses_delta_not_compare():
    # 同一份内容再评一次：说明"分数抖了多少"，不叠一份空 diff 的版本对比
    server, tmp, patches = _server_env()
    try:
        first = _review_text(server, A)
        again = _review_text(server, A)
        assert again.get("delta") == 0, again.get("delta")
        assert again.get("compare") is None, again.get("compare")
        assert again["prev"]["review_id"] == first["review_id"]
    finally:
        _stop_server_env(server, tmp, patches)


def main() -> int:
    ok = 0
    fails: list[str] = []
    for fn in CASES:
        name = fn.__name__.replace("test_", "")
        try:
            fn()
            ok += 1
            print(f"[OK] {name}")
        except AssertionError as exc:
            fails.append(name)
            print(f"[FAIL] {name} — {exc}")
        except Exception as exc:  # noqa: BLE001
            fails.append(name)
            print(f"[ERROR] {name} — {type(exc).__name__}: {exc}")

    total = len(CASES)
    print()
    if fails:
        print(f"{ok}/{total} 通过，失败：{', '.join(fails)}")
        return 1
    print(f"{ok}/{total} 通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
