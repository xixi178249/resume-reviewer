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
