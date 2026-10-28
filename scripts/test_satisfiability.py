"""v1.8 质量自测：可满足性 + 结论一致性。

正测（应当通过）：格式阻断时，按规则填出来的输出必须能过校验。
负测（应当拒绝）：矛盾结论必须被抓出来。
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "scripts")
import check_output as co  # noqa: E402

# 用库里跟踪的样例，不用 samples/txt/ —— 真实简历永远不入库，
# 拿它当测试输入会让 CI 一进来就 FileNotFoundError（整条流水线因此红过一次）。
RESUME = Path("samples/demo/04-Agent算法-强简历.txt").read_text(encoding="utf-8")
ITEMS = list(co.VISUAL_ITEMS)


def base():
    return json.loads(Path("prompts/example-output-v1.8-good.json").read_text(encoding="utf-8"))


def checks(bad_items=()):
    return [
        {"item": i, "ok": i not in bad_items,
         "note": "有问题，需整改" if i in bad_items else "正常",
         "certainty": "certain"}
        for i in ITEMS
    ]


def blocking_review():
    r = base()
    r["format_review"]["verdict"] = "blocking"
    r["format_review"]["visual_level"] = {"known": True, "checks": checks(("图片化文字",)), "density": "ok"}
    r["gate_hr"]["pass"] = False
    r["next_actions"][0]["targets"] = "format_review"
    r["one_line_summary"] = "排版导致解析失败，需先修格式再看内容"
    return r


cases = []

# 正测 1：格式阻断 + gate false + 先修格式  → 应该过
cases.append(("正测：格式阻断的合规输出", blocking_review(), True))

# 正测 2：全部正常 + 无截图
ok = base()
ok["format_review"]["verdict"] = "clean"
ok["format_review"]["text_level"]["sections_missing"] = []
ok["format_review"]["visual_level"] = {"known": False, "checks": [], "density": "unknown"}
cases.append(("正测：无截图的合规输出", ok, True))

# 下面两条曾经是"硬拒绝"，实际用下来代价太大：模型只是把动作顺序写错、
# 或结论少提一句格式，整份结果就被丢掉，用户白等一分钟。现在改成自动修正，
# 但测试仍然盯着它——修没修好必须能验证。
bad_order = blocking_review()
bad_order["next_actions"][0]["targets"] = "impact"

bad_summary = blocking_review()
bad_summary["one_line_summary"] = "整体内容尚可，建议补强量化"

# 负测 2：格式阻断却强推
bad2 = blocking_review()
bad2["verdict"] = "pass"          # 分数 78 → pass 合法
bad2["overall_score"] = 85        # 但 85 应 strong_pass
bad2["verdict"] = "strong_pass"
cases.append(("负测：格式阻断却 strong_pass", bad2, False))


# 负测 4：判满 10 项但全是 certain，某项 ok=false 却 note 太短
bad4 = blocking_review()
bad4["format_review"]["visual_level"]["checks"] = checks(("留白密度",))
bad4["format_review"]["verdict"] = "risky"
for c in bad4["format_review"]["visual_level"]["checks"]:
    if c["item"] == "留白密度":
        c["note"] = "差"
cases.append(("负测：问题项 note 太短", bad4, False))

print("=== v1.8 质量自测 ===\n")
fails = 0
for name, review, should_pass in cases:
    res = co.check(review, RESUME)
    okk = res["file_ok"] == should_pass
    if not okk:
        fails += 1
    tag = "OK  " if okk else "FAIL"
    print(f"[{tag}] {name}  期望{'通过' if should_pass else '拒绝'} 实际{'通过' if res['file_ok'] else '拒绝'}")
    if not okk or not should_pass:
        for e in res["errors"][:3]:
            print(f"        → {e}")

print()
print(f"{len(cases) - fails}/{len(cases)} 符合预期")

# ---------------------------------------------------------------- 自动修正
import quality_gate  # noqa: E402


def expect_repair(name: str, review: dict, tag: str, confirm) -> None:
    global fails
    res = co.check(review, RESUME)
    tags = (res.get("stats") or {}).get("repairs") or []
    gate = quality_gate.evaluate(review, RESUME)
    doc = gate["doc"]
    okk = tag in tags and gate["eligible"] and confirm(doc)
    if not okk:
        fails += 1
    print(f"[{'OK  ' if okk else 'FAIL'}] {name}  期望自动修正并交付 实际"
          f"{'已修正' if okk else '未修正'}")
    if not okk:
        print(f"        → repairs={tags} eligible={gate['eligible']} "
              f"blockers={[b['message'][:40] for b in gate['blockers']][:2]}")


print()
print("=== 自动修正（不拦用户，但必须真修好） ===")
expect_repair(
    "自动修正：格式阻断却先改内容",
    bad_order,
    "format_first_action",
    lambda d: d["next_actions"][0]["targets"] == "format_review",
)
expect_repair(
    "自动修正：格式阻断但摘要不提格式",
    bad_summary,
    "format_in_summary",
    lambda d: any(k in d["one_line_summary"] for k in ("格式", "排版", "解析")),
)

print()
print(f"总计失败 {fails} 项")
sys.exit(0 if fails == 0 else 1)
