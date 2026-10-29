"""一条命令跑完全部回归测试。

为什么要有它：现在 7 个测试分散在 7 条命令里，凭记忆跑必然漏跑；
CI 又只部署不测试，改坏了也会直接上线。这个脚本是唯一入口。

用法：

    python scripts/run_tests.py               # 跑全部，只打印一行一结论
    python scripts/run_tests.py --verbose     # 连同子进程完整输出
    python scripts/run_tests.py --only hist   # 名字里含 hist 的才跑
    python scripts/run_tests.py --list        # 看有哪些套件
    python scripts/run_tests.py --json        # 只输出 JSON（供管道/jq）
    python scripts/run_tests.py --out r.json  # 表格照常打印，同时把 JSON 写进文件

退出码：0 = 全过；1 = 至少一个失败或超时。

设计约束：
- 只用标准库。这个项目从头到尾坚持不装额外测试框架。
- 每个套件是独立子进程：一个套件把 import 搞坏，不会连坐后面全部。
- 失败时打印该套件输出的尾部。CI 日志里只看这一行就够定位。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
DEFAULT_TIMEOUT = 300

# (短名, 命令参数, 这个套件在防什么)
SUITES: list[tuple[str, list[str], str]] = [
    ("satisfiability", ["scripts/test_satisfiability.py"],
     "规则能不能被满足：合法输出必须通过、含致命项必须被拒"),
    ("wiring", ["scripts/test_prompt_wiring.py"],
     "送进模型的东西对不对：few-shot 注入、字段齐全、真图传递"),
    ("quality-gate", ["scripts/test_quality_gate.py"],
     "交付门禁：blocker/filtered/warning 分级与错误码稳定"),
    ("privacy", ["scripts/test_privacy.py"],
     "隐私边界：页面图默认不发、临时文件可清理、历史可删除"),
    ("history", ["scripts/test_history.py"],
     "历史归属：review_id 唯一、A/B 预览不串、并发写不乱"),
    ("version-diff", ["scripts/test_version_diff.py"],
     "改前改后对比：不相干当同版、不可比报分差、推断被当事实"),
    ("human-eval", ["scripts/test_human_eval.py"],
     "人工评测工具：标注校验、一致性系数、报告口径"),
    ("engine-choice", ["scripts/test_engine_choice.py"],
     "谁能用哪个模型：自带 key、内网地址拦截、换引擎不算分差"),
    ("local-engine", ["scripts/test_local_engine.py"],
     "规则引擎：不调模型也能出报告，且必须可回指、不自造数字"),
    ("mode-keycheck", ["scripts/test_mode_and_keycheck.py"],
     "按键选的那条路必须照办：选了 AI 没 key 就报错、选了基础评测不花 key"),
    ("selftest", ["scripts/run_eval.py", "--selftest"],
     "校验器自身：编造、空话、无证据能不能被抓到"),
]

COUNT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")
# unittest 的汇总行：`Ran 12 tests in 0.5s` + `OK` / `FAILED (failures=1, errors=2)`
RAN_RE = re.compile(r"^Ran (\d+) tests? in", re.MULTILINE)
BAD_RE = re.compile(r"^FAILED \(([^)]*)\)", re.MULTILINE)


def _child_env() -> dict[str, str]:
    """子进程统一 UTF-8，否则 Windows 控制台会把中文打成乱码。"""
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    return env


def _parse_count(output: str) -> tuple[int, int] | None:
    """从输出里捞计数，两种格式都认：
    - 自写脚本的 `18/18 通过`
    - unittest 的 `Ran 12 tests` + `OK` / `FAILED (failures=1, errors=2)`
    都没有就返回 None。
    """
    ran = RAN_RE.search(output)
    if ran:
        total = int(ran.group(1))
        bad = BAD_RE.search(output)
        failed = 0
        if bad:
            for key in ("failures", "errors", "unexpected successes"):
                m = re.search(rf"{key}=(\d+)", bad.group(1))
                if m:
                    failed += int(m.group(1))
        return max(0, total - failed), total

    for line in reversed(output.strip().splitlines()[-8:]):
        m = COUNT_RE.search(line)
        if m:
            ok, total = int(m.group(1)), int(m.group(2))
            if 0 < total and ok <= total:
                return ok, total
    return None


def _run_one(name: str, argv: list[str], timeout: int) -> dict:
    cmd = [PY] + argv
    started = time.perf_counter()
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(ROOT),
            env=_child_env(),
            capture_output=True,
            timeout=timeout,
        )
        output = proc.stdout.decode("utf-8", "replace") + proc.stderr.decode("utf-8", "replace")
        code = proc.returncode
        note = ""
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or b"").decode("utf-8", "replace")
        output = partial + f"\n[超时] {timeout} 秒内没跑完，已终止。\n"
        code = -1
        note = "超时"
    except OSError as exc:
        output = f"[无法启动] {exc}\n"
        code = -1
        note = "启动失败"

    elapsed = time.perf_counter() - started
    count = _parse_count(output)
    return {
        "name": name,
        "cmd": " ".join(argv),
        "code": code,
        "ok": code == 0,
        "elapsed": round(elapsed, 2),
        "count": list(count) if count else None,
        "output": output,
        "note": note,
    }


def _tag(result: dict) -> str:
    if result["ok"]:
        return "PASS"
    return "FAIL" if result["note"] != "超时" else "TIMEOUT"


def _print_result(result: dict, verbose: bool, stream=sys.stdout) -> None:
    tag = _tag(result)
    count = result["count"]
    cnt = f"{count[0]}/{count[1]}" if count else "—"
    print(f"  [{tag:^7}] {result['name']:<16} {result['elapsed']:>6.1f}s  断言 {cnt}", file=stream)
    if verbose or not result["ok"]:
        for line in result["output"].rstrip().splitlines():
            print(f"           | {line}", file=stream)
        print(file=stream)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="一条命令跑完全部回归测试",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="退出码：0 = 全过；1 = 有失败或超时。",
    )
    ap.add_argument("--verbose", action="store_true", help="连子进程完整输出一起打印")
    ap.add_argument("--only", default="", help="只跑名字含该子串的套件，如 --only hist")
    ap.add_argument("--list", action="store_true", help="列出全部套件后退出")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果（不含完整日志）")
    ap.add_argument("--out", default="", metavar="PATH", help="另外把结果以 UTF-8 JSON 写入该文件")
    ap.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help=f"单个套件超时秒数（默认 {DEFAULT_TIMEOUT}）")
    args = ap.parse_args()

    if args.list:
        print("可用套件：")
        for name, argv, why in SUITES:
            print(f"  {name:<16} {' '.join(argv):<34} {why}")
        return 0

    selected = [s for s in SUITES if args.only.lower() in s[0].lower()]
    if not selected:
        print(f"没有名字含「{args.only}」的套件；用 --list 看全部。", file=sys.stderr)
        return 1

    # --json 时 stdout 只留 JSON，人看的表格改走 stderr，便于 `> out.json` 而不丢日志
    ui = sys.stderr if args.json else sys.stdout
    if not args.json:
        print(f"共 {len(selected)} 个套件，工作目录 {ROOT}")
        print()

    results: list[dict] = []
    for name, argv, _why in selected:
        result = _run_one(name, argv, args.timeout)
        results.append(result)
        _print_result(result, args.verbose, stream=ui)

    failed = [r for r in results if not r["ok"]]
    total_assertions = sum(r["count"][1] for r in results if r["count"])
    payload = {
        "ok": not failed,
        "total": len(results),
        "failed": len(failed),
        "assertions": total_assertions,
        "results": [
            {k: r[k] for k in ("name", "cmd", "code", "ok", "elapsed", "count")}
            for r in results
        ],
    }

    if args.out:
        # 写成 UTF-8 文件，绕开 PowerShell 重定向会加 UTF-16 BOM 的坑
        Path(args.out).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
        )
        print(f"结果已写入 {args.out}", file=ui)

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not failed else 1

    print()
    if not failed:
        print(f"全部通过：{len(results)}/{len(results)} 个套件，{total_assertions} 条断言。")
        return 0

    print(f"失败 {len(failed)}/{len(results)} 个套件：")
    for r in failed:
        why = r["note"] or f"退出码 {r['code']}"
        print(f"  - {r['name']}（{why}）")
    print()
    print("单独重跑一个：python scripts/run_tests.py --only <名字> --verbose")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
