"""端到端快照测试：断言「真正送给模型的东西」是对的。

v1.2 的三处严重问题（`--image` 骗模型、few-shot 没注入、必填字段 prompt 里没写）
`--mock` 和 `--selftest` 全都测不出来——因为它们只验证"校验器"，不验证"输入"。
这个文件专门补那个缺口。

用法：
    python scripts/test_prompt_wiring.py
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROMPT_DIR = ROOT / "prompts"
sys.path.insert(0, str(Path(__file__).resolve().parent))

import run_eval  # noqa: E402

VERSION = run_eval.DEFAULT_PROMPT_VERSION
CASE = {"job_target": "后端开发实习生（Java）", "jd": "负责服务端接口开发", "background": "大三"}
RESUME = "张三\n项目经历\n• 参与项目开发，负责后端接口编写\n"


def schema_required_fields(version: str) -> list[str]:
    """收集 schema 里所有必填字段名（顶层 + 关键嵌套）。"""
    p = PROMPT_DIR / f"output-schema-{version}.json"
    s = json.loads(p.read_text(encoding="utf-8"))
    names: list[str] = list(s.get("required", []))

    def walk(node):
        if isinstance(node, dict):
            for k in node.get("required", []) or []:
                names.append(k)
            for v in (node.get("properties") or {}).values():
                walk(v)
            if isinstance(node.get("items"), dict):
                walk(node["items"])
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(s)
    return sorted(set(names))


def _followup_example_ok(template: str) -> bool:
    """prompt 里那段 json 示例必须真的能被 json.loads。"""
    import json as _json
    import re as _re
    m = _re.search(r"```json\n(.*?)\n```", template, _re.S)
    if not m:
        return False
    try:
        obj = _json.loads(m.group(1))
    except Exception:
        return False
    return all(k in obj for k in ("line", "ask", "prepare", "pitfall", "risk"))


def run() -> int:
    checks: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        checks.append((name, ok, detail))

    # ---------- 1. prompt 渲染 ----------
    prompt = run_eval.build_prompt(RESUME, CASE, VERSION)

    add("占位符全部替换", "{{" not in prompt,
        f"残留：{re.findall(r'{{[a-z_]+}}', prompt)}" if "{{" in prompt else "")
    add("简历正文已嵌入", RESUME.strip() in prompt)
    add("few-shot 已注入", "参考输出" in prompt,
        "examples 没有拼进 prompt，模型看不到合格线")
    # 样例占比过高会让模型"照抄样例"而不是"遵守规则"
    share = 1 - len((PROMPT_DIR / f"system-{VERSION}.md").read_text(encoding="utf-8")) / max(1, len(prompt))
    add("few-shot 占比 < 60%", share < 0.60, f"当前 {share:.0%}，样例快要盖过规则了")
    add("未泄露内部脚本名", "check_output.py" not in prompt and "scripts/" not in prompt,
        "prompt 提到了内部脚本，独立使用时会造成误导")

    # ---------- 2. 字段漂移：schema 必填但 prompt 模板没提 ----------
    # 必须查「模板」而不是渲染后的 prompt：few-shot 样例里包含全部字段名，
    # 查渲染结果是自欺欺人（v1.3 开发时就踩过这个坑）。
    template = (PROMPT_DIR / f"system-{VERSION}.md").read_text(encoding="utf-8")
    missing = [f for f in schema_required_fields(VERSION) if f not in template]
    add("schema 必填字段都在 prompt 模板中出现", not missing,
        f"模板里没提到：{missing}")

    # ---------- 3. 关键规则仍在（用"机制词"而不是"章节名"，避免改名就误报）
    anchor = {
        "禁止编造数字": "禁止编造数字",
        "可执行锚点机制": "[待补充",
        "弱动词": "弱动词",
        "自评词": "自评词",
        "三秒可读性": "盖住姓名",
    }
    if tuple(int(x) for x in re.findall(r"\d+", VERSION)) >= (1, 4):
        anchor.update({
            "ATS 文本层": "ATS",
            "JD 关键词覆盖": "coverage_rate",
            "权属升级标注": "ownership_upgrade",
            "禁止概率": "通过率",
        })
    for name, kw in anchor.items():
        add(f"规则未丢失：{name}", kw in template)

    # ---------- 3b. 字段表「行内」一致性 ----------
    # 上面那条漂移检查只要求"字段名在模板任意位置出现过"，粗到会骗人：
    # v1.8 之前 `hard_blockers` 那一行压根没写 assertion（schema 却要求必填），
    # 而 "assertion" 在 issues 那一行出现过，于是检查一路绿灯、真跑直接报结构错误。
    rows = dict(re.findall(r"^\| `([a-z_]+)\[\]` \| (.+)$", template, re.M))
    schema = json.loads((PROMPT_DIR / f"output-schema-{VERSION}.json").read_text(encoding="utf-8"))
    drift: list[str] = []
    for key, row in rows.items():
        items = (schema.get("properties", {}).get(key) or {}).get("items") or {}
        for req in items.get("required") or []:
            if req not in row:
                drift.append(f"{key}[].{req}")
    add("字段表每行写全了该数组的必填项", not drift,
        f"表里漏写：{drift}（schema 要求必填，模型看不到就会漏）")
    add("数字溯源：查过也要留痕（clear 必须写）", "查过就要留痕" in template,
        "只写「答不清才写」会与校验器的覆盖率要求冲突，真跑必挂")

    # ---------- 3c. 行业权重表：prompt 里的数字必须来自 rubric ----------
    # v2.3 D1 起总分按行业加权。这张表是手写进 prompt 的，最容易和配置走偏：
    # 配置里金融 30%，prompt 里还写 25%，模型就会按错权重算总分，然后被
    # 「分数不自洽」拦下——用户看到的是「模型算错了」，实际是文档没跟上。
    import rubric  # noqa: E402
    ind_rows = {
        m[0]: (m[1], m[2])
        for m in re.findall(r"^\| `([a-z_]+)` \| ([^|]+?) \| (\d+(?:/\d+){4}) \|$", template, re.M)
    }
    bad_ind: list[str] = []
    for key, label, emphasis in rubric.list_industries():
        if key not in ind_rows:
            bad_ind.append(f"{key}（prompt 无此行）")
            continue
        row_label, row_nums = ind_rows[key]
        want = "/".join(
            str(round(rubric.weights_for(key)[d] * 100))
            for d in ("role_fit", "impact", "evidence_quality", "structure", "competitiveness")
        )
        if row_nums != want:
            bad_ind.append(f"{key} 权重 {row_nums} ≠ 配置 {want}")
        if label.split("/")[0] not in row_label:
            bad_ind.append(f"{key} 行业名对不上：{row_label.strip()!r}")
    add("行业权重表与 rubric 逐项一致", not bad_ind, f"漂移：{bad_ind}")
    add("行业段说明了「只改权重、不改维度分」", "不改变维度打分" in template,
        "不写这句，模型会以为换行业就该重打维度分，同一份简历两个行业两套理由")
    add("行业缺省规则写清楚了", "一律用 `default` 权重" in template,
        "未给行业时的兜底必须写明，否则模型可能自造一个 industry 值")

    # ---------- 3d. 行业必须真的送到模型面前 ----------
    p_no_ind = run_eval.build_prompt(RESUME, CASE, VERSION, image_path=None)
    add("未选行业时写明「按通用权重」", "未指定（按通用权重 default）" in p_no_ind,
        "不写这句，模型会自己猜一个行业，分数就不可复现了")
    p_fin = run_eval.build_prompt(RESUME, dict(CASE, industry="finance"), VERSION, image_path=None)
    add("选行业后把中文名与权重写进输入行",
        "金融/银行" in p_fin and "role_fit 30%" in p_fin,
        "只写行业名模型还要回表查权重，容易算错总分")

    # ---------- 3e. 分数段表：prompt 里的锚点必须来自 rubric ----------
    # v2.3 D2 起 prompt 多了一张「分数段」表（先定段、再在段内定分）。
    # 这张表同样是手写进 prompt 的：rubric 把 pass 段改成 65~79，prompt 还写 65~74，
    # 模型就会照着旧区间选分，报告里的 verdict 与段位对不上。
    band_rows = {
        m[0]: m[1:]
        for m in re.findall(
            r"^\| `([a-z]+)` \| ([^|]+?) \| ([^|]+?) \| ([^|]+?) \| (\d+) \|$", template, re.M
        )
    }
    bad_band: list[str] = []
    for key, label, _lo, _hi, suggest, feature, typical, misjudge in rubric.bands()[::-1]:
        if key not in band_rows:
            bad_band.append(f"{key}（prompt 无此行）")
            continue
        row_label, row_typical, row_misjudge, row_suggest = band_rows[key]
        if row_label.strip() != f"{label}：{feature}".strip():
            bad_band.append(f"{key} 段说明与配置不一致：{row_label.strip()!r}")
        if row_typical.strip() != typical.strip():
            bad_band.append(f"{key} 典型画像与配置不一致")
        if row_misjudge.strip() != misjudge.strip():
            bad_band.append(f"{key} 常见误判与配置不一致")
        if int(row_suggest) != suggest:
            bad_band.append(f"{key} 建议分 {row_suggest} ≠ 配置 {suggest}")
    add("分数段表与 rubric 逐项一致", not bad_band, f"漂移：{bad_band}")
    add("分数段表写明「先定段再定分」", "先按这张表定段" in template,
        "不写这句，模型会直接拍一个分数再倒推理由")
    add("分数段表点名 65~79 最易被塞满", "最容易被塞满" in template,
        "不点出来，中间段的堆积问题不会自己消失")

    # ---------- 3f. 面试追问：只给"会问什么"等于把球踢回去 ----------
    # v2.4 D3 起 followup_risks 要求四件套：line / ask / prepare / pitfall。
    # prompt 里少了 prepare 或 pitfall，模型必然只给一串问题清单。
    add("追问契约写明四件套字段", all(k in template for k in
        ("`line`", "`ask`", "`prepare`", "`pitfall`")),
        "契约表缺字段，模型不会自己发明")
    add("追问契约要求 ask 是一句原话", "一句原话" in template or "那一句原话" in template,
        "不写这句，模型会写成 3 个问题清单")
    add("追问契约要求 pitfall 能当场对上号", "能当场对上号" in template,
        "pitfall 写成「答不好会减分」就失去了价值")
    add("追问示例必须能解析成合法 JSON", _followup_example_ok(template),
        "示例 JSON 坏了，模型会照着抄坏格式")
    add("契约表那一行已升级为四件套", "`{line, ask, prepare, pitfall, risk}`" in template,
        "契约表还写着 questions[]，模型会照旧格式给")

    # ---------- 4. 图片：绝不撒谎 ----------
    def has_image_flag(p: str) -> str:
        m = re.search(r"本次是否附带简历截图：(\w+)", p)
        return m.group(1) if m else "?"

    p_text = run_eval.build_prompt(RESUME, CASE, VERSION, image_path=None)
    add("无图时 has_image=false", has_image_flag(p_text) == "false")
    add("无图时提示不得编排版", "禁止出现任何" in p_text)

    msgs_text = run_eval.build_messages(p_text, None)
    add("无图时 content 是字符串", isinstance(msgs_text[0]["content"], str))

    # 用临时 1x1 png 验证多模态分支真的挂了图
    tmp = ROOT / "outputs" / "_probe.png"
    tmp.parent.mkdir(exist_ok=True)
    tmp.write_bytes(
        bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6300010000050001d0e0c4a10000000049454e44ae426082"
        )
    )
    p_img = run_eval.build_prompt(RESUME, CASE, VERSION, image_path=str(tmp))
    add("有图时 has_image=true", has_image_flag(p_img) == "true")
    msgs_img = run_eval.build_messages(p_img, str(tmp))
    parts = msgs_img[0]["content"]
    add("有图时 content 是数组", isinstance(parts, list) and len(parts) == 2)
    has_img = any(p.get("type") == "image_url" for p in parts) if isinstance(parts, list) else False
    add("有图时真的附了 image_url", has_img, "只改 has_image 不传图 = 骗模型去幻觉")
    if has_img:
        url = next(p for p in parts if p.get("type") == "image_url")["image_url"]["url"]
        add("图片是 data URL", url.startswith("data:image/") and ";base64," in url)
    tmp.unlink(missing_ok=True)

    # ---------- 5. 图片缺失必须报错，不能静默 ----------
    try:
        run_eval.build_messages(p_img, str(ROOT / "no_such_file.png"))
        add("图片不存在时明确报错", False, "本该 SystemExit 却继续执行")
    except SystemExit:
        add("图片不存在时明确报错", True)

    # ---------- 输出 ----------
    print(f"== 端到端接线测试（{VERSION}）==")
    bad = 0
    for name, ok, detail in checks:
        if not ok:
            bad += 1
        line = f"  {'PASS' if ok else 'FAIL'}  {name}"
        if not ok and detail:
            line += f"\n        → {detail}"
        print(line)
    print(f"\n{len(checks) - bad}/{len(checks)} 通过")
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="端到端接线测试")
    ap.add_argument("--version", default=None, help="要检查的 prompt 版本，默认取 run_eval 的默认值")
    a = ap.parse_args()
    if a.version:
        VERSION = a.version
    raise SystemExit(run())
