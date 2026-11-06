#!/usr/bin/env python3
"""`GET /rules` 的页面：把 `docs/评分规则.md` 渲染成一页。

为什么单独一个模块：
    server.py 已经 1,900 行，把一整段 HTML 模板塞进去只会更难读。这里只管
    「读 md → 转 HTML → 套外壳」，不碰任何请求状态，便于单测。

为什么要暴露到网页上：
    用户在报告里看到 62 分，第一反应是「凭什么」。让他能就地打开这张表，
    比在 README 里写一段更有效。文档本身由 scripts/gen_rules_doc.py 生成，
    所以页面永远不会与 rubric 配置漂移。
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import md_render  # noqa: E402

SOURCE = ROOT / "docs" / "评分规则.md"

_cache: dict[str, str] = {}

SHELL = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>评分规则 · 简历评审助手</title>
<style>
:root{{--fg:#1f2328;--muted:#5b6470;--line:#e3e6ea;--bg:#fff;--soft:#f6f7f9}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--soft);color:var(--fg);
  font:15px/1.9 system-ui,-apple-system,"PingFang SC","Hiragino Sans GB",
  "Microsoft YaHei","Noto Sans CJK SC",sans-serif}}
main{{max-width:880px;margin:24px auto 64px;background:var(--bg);border:1px solid var(--line);
  border-radius:12px;padding:28px 32px}}
h1{{font-size:24px;margin:0 0 18px;font-weight:700}}
h2{{font-size:18px;margin:32px 0 10px;font-weight:700;padding-top:8px;border-top:1px solid var(--line)}}
h3{{font-size:16px;margin:20px 0 8px;font-weight:700}}
p{{margin:10px 0}}
code{{background:#eef0f3;border-radius:4px;padding:1px 5px;font-size:13px;
  font-family:ui-monospace,Menlo,Consolas,monospace;overflow-wrap:anywhere}}
pre{{background:#0f172a;color:#e6edf6;border-radius:8px;padding:12px 14px;overflow-x:auto}}
pre code{{background:none;color:inherit;padding:0}}
table{{border-collapse:collapse;width:100%;margin:12px 0;font-size:14px}}
th,td{{border:1px solid var(--line);padding:7px 10px;text-align:left;vertical-align:top}}
th{{background:var(--soft);font-weight:700;white-space:nowrap}}
.note{{background:#fff8e6;border-left:3px solid #d9a72b;padding:10px 14px;margin:14px 0;
  border-radius:0 6px 6px 0;font-size:14px;color:#4a4132}}
ul{{margin:10px 0;padding-left:22px}}
nav{{margin-top:28px;padding-top:14px;border-top:1px solid var(--line);font-size:13px;color:var(--muted)}}
a{{color:#0b63c5}}
@media (max-width:640px){{main{{margin:0;border:0;border-radius:0;padding:20px 16px}}}}
</style></head>
<body><main>
__BODY__
<nav>这份表随 <code>config/rubric.yaml</code> 生成 ·
<a href="/">回评审页</a> · <a href="/chat">在线改简历</a></nav>
</main></body></html>"""

MISSING = (
    "<p>评分规则文档不可用：既没带上 <code>docs/评分规则.md</code>，"
    "也没法从 <code>config/rubric.yaml</code> 现场生成。"
    "这不影响评审，但说明发布包不完整。</p>"
)


def _body_without_doc() -> str:
    """发布包里没带 `docs/` 时，从 `config/rubric.yaml` 现算一份。

    为什么需要这条路径：`.dockerignore` 把 `docs/` 整目录排除了，线上 `/rules`
    曾经只显示一句「发布包不完整」——「规则公开」这件事在公网版等于没做。
    现算的代价是毫秒级的一次 `gen_rules_doc.render()`，换来「只要 config 在，
    页面就在」；而且现算与提交的 md 出自同一个生成器，不可能漂移。
    """
    try:
        import gen_rules_doc

        return md_render.render(gen_rules_doc.render())
    except Exception:  # noqa: BLE001 - 生成器坏了才允许退化成一句人话
        return MISSING


def render() -> str:
    """返回整页 HTML（首次读盘后缓存；文档是生成的，运行期不会变）。"""
    if "html" in _cache:
        return _cache["html"]
    try:
        body = md_render.render(SOURCE.read_text(encoding="utf-8"))
    except OSError:
        body = _body_without_doc()
    html = SHELL.replace("__BODY__", body)
    _cache["html"] = html
    return html


def reset_cache() -> None:
    """改了 md 之后用（测试与本地调试）。"""
    _cache.clear()
