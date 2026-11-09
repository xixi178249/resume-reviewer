#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""项目测评：跑真命令、收真数字、出评分卡。

用法：
    python eval_project.py                      # 测当前目录（项目根）
    python eval_project.py --root ../resume-reviewer
    python eval_project.py --base https://xxx.onrender.com
    python eval_project.py --skip-run           # 不重跑测试/自测，只测静态与线上
    python eval_project.py --json out.json --md out.md

原则（写死在这份脚本里，改口径要改代码并同步 references/评分口径.md）：
1. 每个分数都必须能追到一条命令或一个文件；测不出来写「判不出」，不猜 0 分。
2. 判不出的维度不参与加权，权重按剩余维度重新归一，并在报告里明说。
3. 只读：不改被测项目的任何文件（唯一写盘是 --json/--md 指定的输出）。
"""

from __future__ import annotations

import argparse
import ast
import json
import pathlib
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request

WEIGHTS = {"verify": 25, "e2e": 20, "live": 15, "docs": 10, "code": 10, "deliver": 10, "evidence": 10}

DIM_CN = {
    "verify": "可验证性",
    "e2e": "端到端可用",
    "live": "线上可用",
    "docs": "文档一致",
    "code": "代码卫生",
    "deliver": "交付完整",
    "evidence": "评测资产",
}

BANDS = ((9.0, "优秀"), (7.5, "良好"), (6.0, "合格"), (0.0, "待改"))

LIVE_PAGES = ("/", "/rules", "/chat")


def _run(cmd: list[str], cwd: pathlib.Path, timeout: int) -> tuple[int, str]:
    try:
        p = subprocess.run(
            cmd, cwd=str(cwd), capture_output=True, timeout=timeout,
            env={**__import__("os").environ, "PYTHONIOENCODING": "utf-8"},
        )
        out = (p.stdout or b"") + (p.stderr or b"")
        return p.returncode, out.decode("utf-8", "replace")
    except subprocess.TimeoutExpired:
        return 124, f"超时（>{timeout}s）"
    except FileNotFoundError as e:
        return 127, f"命令不存在：{e}"


# ---------------------------------------------------------------- 测：测试套件

def measure_tests(root: pathlib.Path, timeout: int = 420) -> dict:
    runner = root / "scripts" / "run_tests.py"
    if not runner.exists():
        return {"status": "unknown", "why": "没有 scripts/run_tests.py，无法验证"}
    code, out = _run([sys.executable, "scripts/run_tests.py"], root, timeout)
    m_ok = re.search(r"全部通过：(\d+)/(\d+) 个套件，(\d+) 条断言", out)
    m_bad = re.search(r"失败 (\d+)/(\d+) 个套件", out)
    if m_ok:
        suites_pass, suites_total, assertions = map(int, m_ok.groups())
        failed = []
    elif m_bad:
        failed = re.findall(r"-\s+(\S+)（退出码", out)
        suites_total = int(m_bad.group(2))
        suites_pass = suites_total - int(m_bad.group(1))
        m_a = re.search(r"断言 (\d+)/(\d+)", out)
        assertions = int(m_a.group(1)) if m_a else 0
    else:
        return {"status": "unknown", "why": f"没能解析输出（退出码 {code}）", "tail": out[-500:]}
    return {
        "status": "ok",
        "suites_pass": suites_pass,
        "suites_total": suites_total,
        "assertions": assertions,
        "failed_suites": failed,
        "exit_code": code,
        "tail": out[-400:],
    }


# ---------------------------------------------------------------- 测：端到端探针

def measure_selfcheck(root: pathlib.Path, timeout: int = 420) -> dict:
    probe = root / "scripts" / "selfcheck.py"
    if not probe.exists():
        return {"status": "unknown", "why": "没有 scripts/selfcheck.py，端到端判不出"}
    code, out = _run([sys.executable, "scripts/selfcheck.py"], root, timeout)
    m = re.search(r"共 (\d+) 条：PASS (\d+) / PARTIAL (\d+) / FAIL (\d+)", out)
    if not m:
        return {"status": "unknown", "why": f"没能解析输出（退出码 {code}）", "tail": out[-500:]}
    total, n_pass, n_partial, n_fail = map(int, m.groups())
    fails = []
    for line in out.splitlines():
        mm = re.match(r"^(F\d+\.\d+)\s+(\S+)\s+FAIL\s+(.*)$", line.strip())
        if mm:
            fails.append({"id": mm.group(1), "area": mm.group(2), "detail": mm.group(3)[:160]})
    return {
        "status": "ok",
        "total": total,
        "pass": n_pass,
        "partial": n_partial,
        "fail": n_fail,
        "fails": fails,
        "exit_code": code,
    }


# ---------------------------------------------------------------- 测：线上

def _fetch(url: str, timeout: int = 25) -> tuple[int, bytes]:
    """先走 urllib；被本地代理/证书链搞出 SSL EOF 时退回 curl -sk。

    真实踩过：这台机器上 urllib 打 Render 报 `SSL: UNEXPECTED_EOF_WHILE_READING`，
    而 `curl -sk` 同一条 URL 直接 200 —— 那是环境问题，不该记成「线上挂了」。
    """
    req = urllib.request.Request(url, headers={"User-Agent": "project-eval/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except (urllib.error.URLError, OSError) as first:
        try:
            p = subprocess.run(
                ["curl", "-sk", "-m", str(timeout), "-w", "\n%{http_code}", url],
                capture_output=True, timeout=timeout + 10,
            )
        except (OSError, subprocess.TimeoutExpired):
            raise first
        if p.returncode != 0 or not p.stdout:
            raise first
        body, _, code = p.stdout.decode("utf-8", "replace").rpartition("\n")
        return int(code.strip() or 0), body.encode("utf-8")



def measure_live(base: str | None, timeout: int = 25, retries: int = 3) -> dict:
    if not base:
        return {"status": "unknown", "why": "没给 --base，线上维度不评"}
    base = base.rstrip("/")
    health = None
    for i in range(retries):
        try:
            code, body = _fetch(base + "/api/health", timeout)
            health = json.loads(body.decode("utf-8", "replace"))
            break
        except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as e:
            last = str(e)[:120]
            if i == retries - 1:
                return {"status": "unknown", "why": f"/api/health 打不通：{last}"}
            time.sleep(3)
    pages = {}
    for p in LIVE_PAGES:
        try:
            code, body = _fetch(base + p, timeout)
            pages[p] = {"status": code, "bytes": len(body)}
        except urllib.error.HTTPError as e:
            pages[p] = {"status": e.code, "bytes": 0}
        except Exception as e:  # noqa: BLE001 - 记录而不是崩掉
            pages[p] = {"status": 0, "bytes": 0, "error": str(e)[:80]}
    return {"status": "ok", "base": base, "health": health, "pages": pages}


# ---------------------------------------------------------------- 测：文档

_MD_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)\)")


def _broken_links(root: pathlib.Path, files: list[pathlib.Path]) -> tuple[int, list[dict]]:
    total = 0
    broken: list[dict] = []
    for f in files:
        try:
            text = f.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for target in _MD_LINK.findall(text):
            if target.startswith(("http://", "https://", "mailto:", "#")):
                continue
            path_part = target.split("#", 1)[0].strip()
            if not path_part:
                continue
            total += 1
            resolved = (f.parent / path_part).resolve()
            if not resolved.exists():
                broken.append({"file": str(f.relative_to(root)), "link": target})
    return total, broken


def measure_docs(root: pathlib.Path) -> dict:
    out: dict = {"status": "ok", "issues": []}
    version = (root / "VERSION").read_text(encoding="utf-8").strip() if (root / "VERSION").exists() else ""
    out["version"] = version
    vers = {"VERSION": version}
    for name in ("package.json", "package-lock.json"):
        p = root / name
        if p.exists():
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
                vers[name] = d.get("version")
                if name == "package-lock.json":
                    vers["lock.packages[''].version"] = (d.get("packages") or {}).get("", {}).get("version")
            except ValueError:
                vers[name] = "解析失败"
    out["versions"] = vers
    distinct = {v for v in vers.values() if isinstance(v, str) and v}
    out["version_consistent"] = len(distinct) == 1
    if not out["version_consistent"]:
        out["issues"].append("版本号不一致：" + json.dumps(vers, ensure_ascii=False))

    readme = root / "README.md"
    out["readme_head_has_version"] = False
    if readme.exists():
        head = "\n".join(readme.read_text(encoding="utf-8", errors="replace").splitlines()[:40])
        out["readme_head_has_version"] = bool(version) and version in head
        if not out["readme_head_has_version"]:
            out["issues"].append(f"README 头部 40 行里没写当前版本 {version}")

    changelog = root / "docs" / f"CHANGELOG-v{version}.md"
    out["changelog_for_version"] = changelog.exists()
    if not out["changelog_for_version"]:
        out["issues"].append(f"缺 docs/CHANGELOG-v{version}.md")

    md_files = [p for p in root.rglob("*.md") if ".git" not in p.parts and "node_modules" not in p.parts]
    total, broken = _broken_links(root, md_files)
    out["md_files"] = len(md_files)
    out["links_total"] = total
    out["links_broken"] = broken[:20]
    out["links_broken_n"] = len(broken)
    if broken:
        out["issues"].append(f"有 {len(broken)} 条相对链接指向不存在的文件")
    return out


# ---------------------------------------------------------------- 测：代码卫生

_SILENT = re.compile(r"except[^:\n]*:\s*\n\s+(?:pass|continue)\b", re.M)


def _imports_of(text: str) -> tuple[set[str], set[str], set[str]]:
    """把 import 分成三层：顶层（装上才能跑）/ 函数内（用到才需要）/ try 内（没有也能跑）。

    区别很重要：`import pdfplumber` 写在 `try: ... except ImportError:` 里说明没有它
    也能跑；写在函数里说明只有那条链路需要它；只有裸在模块顶层的 import 才是「装不上
    就跑不起来」。早先按正则一刀切，把 PDF/DOCX 解析用的库全算成硬依赖，直接判 0 分。
    """
    top: set[str] = set()
    lazy: set[str] = set()
    soft: set[str] = set()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        for m in re.finditer(r"^\s*(?:from|import)\s+([A-Za-z_][\w.]*)", text, re.M):
            top.add(m.group(1).split(".")[0])
        return top, lazy, soft

    def add(name: str | None, guarded: bool, in_func: bool) -> None:
        if not name:
            return
        mod = name.split(".")[0]
        if guarded:
            soft.add(mod)
        elif in_func:
            lazy.add(mod)
        else:
            top.add(mod)

    def walk(node: ast.AST, guarded: bool, in_func: bool) -> None:
        # 先判断「节点自己」是什么，再往下走。早先只在子节点上判断，函数体里的
        # `import pdfplumber` 会因为 walk(Import) 只看到 alias 而整条漏掉。
        if isinstance(node, ast.Try):
            # try 里的 import 一律当可选：真·必装依赖没人会包在 try 里
            for sub in node.body:
                walk(sub, True, in_func)
            for handler in node.handlers:
                for sub in handler.body:
                    walk(sub, True, in_func)
            for sub in node.orelse + node.finalbody:
                walk(sub, guarded, in_func)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for sub in node.body:
                walk(sub, guarded, True)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                add(alias.name, guarded, in_func)
        elif isinstance(node, ast.ImportFrom):
            if not node.level:
                add(node.module, guarded, in_func)
        else:
            for child in ast.iter_child_nodes(node):
                walk(child, guarded, in_func)

    walk(tree, False, False)
    return top, lazy, soft


def measure_code(root: pathlib.Path) -> dict:
    files = [p for p in root.rglob("*.py") if ".git" not in p.parts]
    lines_total = 0
    biggest = ("", 0)
    silent: list[dict] = []
    hard: set[str] = set()
    lazy: set[str] = set()
    soft: set[str] = set()
    local = {p.stem for p in files}
    for f in files:
        text = f.read_text(encoding="utf-8", errors="replace")
        n = len(text.splitlines())
        lines_total += n
        if n > biggest[1]:
            biggest = (str(f.relative_to(root)), n)
        for m in _SILENT.finditer(text):
            ln = text[: m.start()].count("\n") + 1
            silent.append({"file": str(f.relative_to(root)), "line": ln})
        h, lz, s = _imports_of(text)
        hard |= h
        lazy |= lz
        soft |= s
    stdlib = set(getattr(sys, "stdlib_module_names", set()))
    skip = stdlib | local | {"__future__"}
    third = sorted(i for i in hard if i not in skip)
    lazy_third = sorted(i for i in lazy if i not in skip and i not in hard)
    optional = sorted(i for i in soft if i not in skip and i not in hard)
    return {
        "status": "ok",
        "py_files": len(files),
        "py_lines": lines_total,
        "biggest_file": biggest,
        "silent_except": silent[:20],
        "silent_except_n": len(silent),
        "third_party": third,
        "lazy_deps": lazy_third,
        "optional_deps": optional,
    }


# ---------------------------------------------------------------- 测：交付物

def measure_deliver(root: pathlib.Path) -> dict:
    docs = list((root / "docs").glob("*")) if (root / "docs").exists() else []
    changelogs = [p for p in docs if p.name.startswith("CHANGELOG")]
    shots = list((root / "docs" / "screenshots").glob("*.png")) if (root / "docs" / "screenshots").exists() else []
    zips = list(root.rglob("*.zip"))
    return {
        "status": "ok",
        "docs_files": len(docs),
        "changelogs": len(changelogs),
        "screenshots": len(shots),
        "packages": [str(z.relative_to(root)) for z in zips][:5],
        "has_rules_route": "rules_page" in (root / "web" / "server.py").read_text(encoding="utf-8", errors="replace")
        if (root / "web" / "server.py").exists() else False,
    }


# ---------------------------------------------------------------- 测：评测资产

def measure_evidence(root: pathlib.Path) -> dict:
    """金标数据、留出集、基准报告、局限清单 —— 「它凭什么说自己准」的四件实物。

    这一维是故意加的：一个 588 条断言全绿的项目，也可能只有 8 份人工金标、
    留出集 0 份、基准报告 status=NOT_READY。工程分再高，可信度也不能跟着虚高。
    """
    out: dict = {"status": "ok", "datasets": [], "samples": 0, "heldout": 0,
                 "bench": None, "limits_doc": ""}
    for p in root.rglob("*.json"):
        if any(x in p.parts for x in (".git", "node_modules", "versions")):
            continue
        low = p.name.lower()
        if not any(k in low for k in ("dataset", "heldout", "held-out")):
            continue
        try:
            d = json.loads(p.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            continue
        n = len(d.get("samples") or []) if isinstance(d, dict) else len(d)
        out["datasets"].append({"path": str(p.relative_to(root)), "samples": n})
        if "held" in low:
            out["heldout"] += n
        else:
            out["samples"] = max(out["samples"], n)

    bench = root / "bench" / "report.json"
    if bench.exists():
        try:
            d = json.loads(bench.read_text(encoding="utf-8", errors="replace"))
            ready = d.get("readiness") or {}
            status = ""
            if isinstance(ready, dict):
                if "ready" in ready:
                    status = "READY" if ready.get("ready") else ("PILOT" if ready.get("pilot") else "NOT_READY")
                elif "status" in ready:
                    status = str(ready.get("status"))
                else:
                    status = next((str(v) for v in ready.values() if isinstance(v, str)), "")
            out["bench"] = {"path": str(bench.relative_to(root)), "status": status[:40]}
        except (OSError, ValueError):
            out["bench"] = {"path": str(bench.relative_to(root)), "status": "解析失败"}

    docs_dir = root / "docs"
    if docs_dir.exists():
        cands = list(docs_dir.glob("*.md"))
        # 先找专门写局限的那几份，找不到再退回任何提过「测不到」的文档
        pref = [p for p in cands if any(k in p.name for k in ("自评", "局限", "评测方案"))]
        for p in pref + [p for p in cands if p not in pref]:
            text = p.read_text(encoding="utf-8", errors="replace")
            if "测不到" in text or "已知局限" in text or "未验证" in text:
                out["limits_doc"] = p.name
                break
    return out


# ---------------------------------------------------------------- 算分

def _clamp(x: float, lo: float = 0.0, hi: float = 10.0) -> float:
    return max(lo, min(hi, x))


def score_verify(t: dict) -> dict:
    if t.get("status") != "ok":
        return {"status": "unknown", "why": t.get("why", "")}
    a = t["assertions"]
    if a >= 600:
        base = 10.0
    elif a >= 400:
        base = 9.0
    elif a >= 250:
        base = 8.0
    elif a >= 120:
        base = 6.0
    elif a >= 50:
        base = 4.0
    else:
        base = 2.0
    green = t["suites_pass"] == t["suites_total"]
    score = _clamp(base * (1.0 if green else 0.3))
    reason = f"{t['assertions']} 条断言 / {t['suites_total']} 套件，{'全绿' if green else '有失败'}（基线 {base}，未全绿 ×0.3）"
    return {"status": "ok", "score": round(score, 1), "why": reason}


def score_e2e(s: dict) -> dict:
    if s.get("status") != "ok":
        return {"status": "unknown", "why": s.get("why", "")}
    total = max(1, s["total"])
    rate = (s["pass"] + 0.5 * s["partial"]) / total
    score = _clamp(10 * rate - 0.5 * s["fail"])
    return {
        "status": "ok",
        "score": round(score, 1),
        "why": f"探针 {s['pass']}/{s['total']} 通过（PARTIAL {s['partial']}、FAIL {s['fail']}），每条 FAIL 再扣 0.5",
    }


def score_live(l: dict, version: str) -> dict:
    if l.get("status") != "ok":
        return {"status": "unknown", "why": l.get("why", "")}
    live_v = str((l.get("health") or {}).get("version") or "")
    pages = l.get("pages") or {}
    bad_pages = [p for p, v in pages.items() if v.get("status") != 200]
    if live_v == version and not bad_pages:
        return {"status": "ok", "score": 10.0, "why": f"线上 {live_v} 与仓库一致，关键页全部 200"}
    if live_v and live_v != version and not bad_pages:
        return {"status": "ok", "score": 7.0, "why": f"线上跑 {live_v}、仓库是 {version}（部署落后一版）"}
    if bad_pages:
        return {"status": "ok", "score": 4.0, "why": f"线上 {live_v or '未知'}；这些页面没回 200：{'、'.join(bad_pages)}"}
    return {"status": "ok", "score": 6.0, "why": f"能连通，但读不到版本（health={live_v or '空'}）"}


def score_docs(d: dict) -> dict:
    if d.get("status") != "ok":
        return {"status": "unknown", "why": d.get("why", "")}
    s = 10.0
    notes = []
    if not d["version_consistent"]:
        s -= 3
        notes.append("版本号不一致 −3")
    if not d["readme_head_has_version"]:
        s -= 2
        notes.append("README 头部没写版本 −2")
    if not d["changelog_for_version"]:
        s -= 3
        notes.append("缺当期 CHANGELOG −3")
    n = d["links_broken_n"]
    if n:
        s -= min(4, n)
        notes.append(f"{n} 条死链 −{min(4, n)}")
    return {"status": "ok", "score": round(_clamp(s), 1), "why": "；".join(notes) or "版本、README、CHANGELOG、链接全部对齐"}


def score_code(c: dict) -> dict:
    if c.get("status") != "ok":
        return {"status": "unknown", "why": c.get("why", "")}
    s = 10.0
    notes = []
    if c["third_party"]:
        s -= min(6, 2 * len(c["third_party"]))
        notes.append("顶层硬依赖 " + "、".join(c["third_party"]) + f" −{min(6, 2 * len(c['third_party']))}")
    if c["silent_except_n"]:
        cut = min(3.0, 0.5 * c["silent_except_n"])
        s -= cut
        notes.append(f"静默吞异常 {c['silent_except_n']} 处 −{cut:g}")
    if c.get("lazy_deps"):
        notes.append("函数内延迟依赖 " + "、".join(c["lazy_deps"]) + "（不扣分，但换环境要装）")
    name, lines = c["biggest_file"]
    if lines > 2500:
        s -= 1
        notes.append(f"单文件 {lines} 行（{name}）−1")
    return {"status": "ok", "score": round(_clamp(s), 1), "why": "；".join(notes) or "顶层零第三方依赖、无静默吞异常、单文件不超过 2500 行"}


def score_deliver(d: dict) -> dict:
    if d.get("status") != "ok":
        return {"status": "unknown", "why": d.get("why", "")}
    s = 0.0
    notes = []
    if d["changelogs"] >= 1:
        s += 4
    if d["screenshots"] >= 1:
        s += 2
        notes.append(f"截图 {d['screenshots']} 张")
    if d["packages"]:
        s += 2
        notes.append(f"可分发包 {len(d['packages'])} 个")
    if d["has_rules_route"]:
        s += 2
        notes.append("规则页可访问")
    return {"status": "ok", "score": round(_clamp(s), 1), "why": "；".join(notes) or "缺截图/分发包/规则页"}


def score_evidence(e: dict) -> dict:
    if e.get("status") != "ok":
        return {"status": "unknown", "why": e.get("why", "")}
    n = e["samples"]
    if n >= 50:
        s, why = 4.0, f"金标 {n} 份"
    elif n >= 20:
        s, why = 3.0, f"金标 {n} 份"
    elif n >= 8:
        s, why = 2.0, f"金标只有 {n} 份（<20，量表还验不准）"
    elif n > 0:
        s, why = 1.0, f"金标只有 {n} 份（<8，连最小验证都不够）"
    else:
        s, why = 0.0, "没找到金标数据集"
    notes = [why]
    if e["heldout"] > 0:
        s += 2
        notes.append(f"留出集 {e['heldout']} 份 +2")
    else:
        notes.append("留出集 0 份 +0")
    bench = e.get("bench") or {}
    status = str(bench.get("status") or "").upper()
    if status == "READY":
        s += 2
        notes.append("基准报告 READY（可对外引用）+2")
    elif status == "PILOT":
        s += 1
        notes.append("基准报告 PILOT（只够内部看，不得对外引用）+1")
    elif bench:
        notes.append(f"基准报告 status={status or '空'}（不可引用）+0")
    else:
        notes.append("没有 bench/report.json +0")
    if e["limits_doc"]:
        s += 2
        notes.append(f"有局限清单（{e['limits_doc']}）+2")
    else:
        notes.append("没写「测不到什么」 +0")
    return {"status": "ok", "score": round(_clamp(s), 1), "why": "；".join(notes)}


# ---------------------------------------------------------------- 汇总与渲染

def evaluate(root: pathlib.Path, base: str | None, skip_run: bool, timeout: int) -> dict:
    t = {"status": "unknown", "why": "跳过（--skip-run）"} if skip_run else measure_tests(root, timeout)
    s = {"status": "unknown", "why": "跳过（--skip-run）"} if skip_run else measure_selfcheck(root, timeout)
    live = measure_live(base, retries=3)
    docs = measure_docs(root)
    code = measure_code(root)
    deliver = measure_deliver(root)
    evidence = measure_evidence(root)
    version = docs.get("version") or ""

    dims = {
        "verify": score_verify(t),
        "e2e": score_e2e(s),
        "live": score_live(live, version),
        "docs": score_docs(docs),
        "code": score_code(code),
        "deliver": score_deliver(deliver),
        "evidence": score_evidence(evidence),
    }
    known = {k: v for k, v in dims.items() if v.get("status") == "ok"}
    wsum = sum(WEIGHTS[k] for k in known)
    total = round(sum(known[k]["score"] * WEIGHTS[k] for k in known) / wsum, 1) if wsum else None
    band = next(name for lo, name in BANDS if (total or 0) >= lo)

    defects: list[dict] = []
    for f in t.get("failed_suites") or []:
        defects.append({"level": "高", "area": "测试", "what": f"套件 {f} 失败"})
    for f in s.get("fails") or []:
        defects.append({"level": "高", "area": "端到端", "what": f"{f['id']} {f['area']}：{f['detail']}"})
    for issue in docs.get("issues") or []:
        defects.append({"level": "中", "area": "文档", "what": issue})
    for b in (docs.get("links_broken") or [])[:10]:
        defects.append({"level": "低", "area": "文档", "what": f"{b['file']} 里的链接 {b['link']} 指向不存在的文件"})
    for e in (code.get("silent_except") or [])[:10]:
        defects.append({"level": "中", "area": "代码", "what": f"{e['file']}:{e['line']} 静默吞异常"})
    for dep in code.get("third_party") or []:
        defects.append({"level": "低", "area": "代码", "what": f"依赖第三方库 {dep}"})
    if dims["live"].get("status") == "ok" and dims["live"]["score"] < 10:
        defects.append({"level": "中", "area": "线上", "what": dims["live"]["why"]})
    if evidence.get("samples", 0) < 20:
        defects.append({"level": "高", "area": "评测资产",
                        "what": f"人工金标只有 {evidence.get('samples', 0)} 份（<20），量表精度还证不出来"})
    if evidence.get("heldout", 0) == 0:
        defects.append({"level": "高", "area": "评测资产", "what": "留出集 0 份：所有指标都是样本内数字"})
    bench = evidence.get("bench") or {}
    if "NOT_READY" in str(bench.get("status", "")).upper():
        defects.append({"level": "中", "area": "评测资产", "what": f"bench 报告 status={bench.get('status')}，不能对外引用"})
    elif "PILOT" in str(bench.get("status", "")).upper():
        defects.append({"level": "中", "area": "评测资产", "what": "bench 处于 pilot 模式：只够内部看，对外只能说「未验证」"})

    return {
        "root": str(root),
        "version": version,
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "weights": WEIGHTS,
        "dims": dims,
        "total": total,
        "band": band,
        "weight_used": wsum,
        "excluded": [DIM_CN[k] for k in dims if dims[k].get("status") != "ok"],
        "defects": defects,
        "raw": {"tests": t, "selfcheck": s, "live": live, "docs": docs, "code": code,
                "deliver": deliver, "evidence": evidence},
    }


def render_md(r: dict) -> str:
    L: list[str] = []
    L.append(f"# 项目测评报告 · {pathlib.Path(r['root']).name} v{r['version']}")
    L.append("")
    L.append(f"> 生成时间：{r['generated_at']}　总分 **{r['total']} / 10（{r['band']}）**")
    if r["excluded"]:
        L.append(f"> ⚠️ 判不出、未计入加权的维度：{'、'.join(r['excluded'])}（权重按剩余 {r['weight_used']} 归一）")
    L.append("")
    L.append("## 评分卡")
    L.append("")
    L.append("| 维度 | 权重 | 得分 | 依据 |")
    L.append("|---|---|---|---|")
    for k, w in r["weights"].items():
        d = r["dims"][k]
        if d.get("status") != "ok":
            L.append(f"| {DIM_CN[k]} | {w}% | 判不出 | {d.get('why', '')} |")
        else:
            L.append(f"| {DIM_CN[k]} | {w}% | **{d['score']}** | {d['why']} |")
    L.append("")
    if r["defects"]:
        L.append("## 缺陷清单")
        L.append("")
        L.append("| 级别 | 领域 | 是什么 |")
        L.append("|---|---|---|")
        for d in r["defects"]:
            L.append(f"| {d['level']} | {d['area']} | {d['what']} |")
        L.append("")
    else:
        L.append("## 缺陷清单")
        L.append("")
        L.append("没有发现缺陷。")
        L.append("")

    raw = r["raw"]
    t, s, live = raw["tests"], raw["selfcheck"], raw["live"]
    L.append("## 实测原始数据")
    L.append("")
    if t.get("status") == "ok":
        L.append(f"- 测试：{t['suites_pass']}/{t['suites_total']} 套件、{t['assertions']} 条断言")
    else:
        L.append(f"- 测试：{t.get('why', '判不出')}")
    if s.get("status") == "ok":
        L.append(f"- 端到端探针：{s['pass']}/{s['total']} 通过（FAIL {s['fail']}）")
    else:
        L.append(f"- 端到端探针：{s.get('why', '判不出')}")
    if live.get("status") == "ok":
        pages = "、".join(f"{p} {v.get('status')} {v.get('bytes')}B" for p, v in (live.get("pages") or {}).items())
        L.append(f"- 线上：{live.get('base')} version={((live.get('health') or {}).get('version'))}；{pages}")
    else:
        L.append(f"- 线上：{live.get('why', '判不出')}")
    c = raw["code"]
    L.append(f"- 代码：{c['py_files']} 个 py 文件、{c['py_lines']} 行；最大单文件 {c['biggest_file'][1]} 行（{c['biggest_file'][0]}）；第三方依赖 {len(c['third_party'])} 个")
    d = raw["docs"]
    L.append(f"- 文档：{d['md_files']} 份 md；相对链接 {d['links_total']} 条，坏 {d['links_broken_n']} 条")
    ev = raw.get("evidence") or {}
    bench = ev.get("bench") or {}
    L.append(
        f"- 评测资产：金标 {ev.get('samples', 0)} 份、留出集 {ev.get('heldout', 0)} 份；"
        f"bench={bench.get('path', '无')} status={bench.get('status', '—')}；"
        f"局限清单={ev.get('limits_doc') or '无'}"
    )
    L.append("")
    L.append("## 这张卡测不到什么")
    L.append("")
    L.append("- 不测「判得准不准」：没有真实人工金标与留出集时，分数只能说明工程做完了，不能说明量表可信。")
    L.append("- 不测模型质量：`--mock` 或规则引擎下跑通的链路，换成真模型仍需人工复核。")
    L.append("- 不测并发与成本：没有压测与账单数据，多用户下的延迟、限流、开销都不在这张卡里。")
    L.append("")
    L.append("## 口径")
    L.append("")
    L.append("- 每格分数都能追到上面那行实测数字；测不出来写「判不出」，不当 0 分算。")
    L.append("- 判不出的维度不占权重，剩余维度按原权重比例归一，避免「没测=没做事」的误判。")
    L.append("- 详细公式见 `references/评分口径.md`。")
    return "\n".join(L) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="项目测评：跑真命令、出评分卡")
    ap.add_argument("--root", default=".", help="被测项目根目录")
    ap.add_argument("--base", default=None, help="线上地址，如 https://xxx.onrender.com")
    ap.add_argument("--skip-run", action="store_true", help="不重跑测试与端到端探针")
    ap.add_argument("--timeout", type=int, default=420, help="单次命令超时（秒）")
    ap.add_argument("--json", default=None, help="把明细写进这个 json")
    ap.add_argument("--md", default=None, help="把评分卡写进这个 md")
    args = ap.parse_args(argv)

    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 老终端没有 reconfigure
        pass

    root = pathlib.Path(args.root).resolve()
    if not root.exists():
        print(f"根目录不存在：{root}")
        return 2
    r = evaluate(root, args.base, args.skip_run, args.timeout)
    md = render_md(r)
    print(md)
    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(r, ensure_ascii=False, indent=2), encoding="utf-8")
    if args.md:
        pathlib.Path(args.md).write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
