#!/usr/bin/env python3
"""把 `docs/评分规则.md` 渲染成网页（`GET /rules`）。零依赖，只认这份文档用到的语法。

为什么自己写而不装 markdown 库：
    项目零第三方依赖（部署是单文件 + 标准库）。我们需要支持的语法就六种：
    标题、表格、代码块、列表、引用、段落，加上两种行内标记（`**粗**`、`` `码` ``）。
    60 行搞定，且能被测试逐条钉住——装一个库反而多一份「它会怎么转义」的未知。

安全：所有文本先转义 `<>&`，再拼标签，标题/单元格里出现 `<script>` 也只会是一段字。
"""

from __future__ import annotations

import re

_ALLOWED_HEAD = re.compile(r"^(#{1,3}) (.*)$")
_TABLE_ROW = re.compile(r"^\|(.+)\|$")
_TABLE_SEP = re.compile(r"^\|[\s:|-]+\|$")
_CODE_FENCE = "```"


def _escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _inline(text: str) -> str:
    """行内标记：`**粗**` 与反引号代码。先转义，再放行这两个标签。"""
    out = _escape(text)
    out = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return out


def _cells(row: str) -> list[str]:
    body = row.strip()
    if body.startswith("|"):
        body = body[1:]
    if body.endswith("|"):
        body = body[:-1]
    return [c.strip() for c in body.split("|")]


def render(markdown: str) -> str:
    """返回 HTML 片段（不含 <html>/<head>，由调用方套外壳）。"""
    lines = markdown.splitlines()
    out: list[str] = []
    i = 0
    n = len(lines)

    while i < n:
        line = lines[i]
        stripped = line.strip()

        # 空行
        if not stripped:
            i += 1
            continue

        # 代码块
        if stripped.startswith(_CODE_FENCE):
            i += 1
            buf: list[str] = []
            while i < n and not lines[i].strip().startswith(_CODE_FENCE):
                buf.append(_escape(lines[i]))
                i += 1
            i += 1  # 跳过收尾的 ```
            out.append("<pre><code>" + "\n".join(buf) + "</code></pre>")
            continue

        # 标题
        m = _ALLOWED_HEAD.match(stripped)
        if m:
            level = len(m.group(1))
            out.append(f"<h{level}>{_inline(m.group(2))}</h{level}>")
            i += 1
            continue

        # 表格（首行表头 + 分隔行 + 若干数据行）
        if _TABLE_ROW.match(stripped) and i + 1 < n and _TABLE_SEP.match(lines[i + 1].strip()):
            header = _cells(lines[i])
            i += 2
            rows: list[list[str]] = []
            while i < n and _TABLE_ROW.match(lines[i].strip()):
                rows.append(_cells(lines[i]))
                i += 1
            head_html = "".join(f"<th>{_inline(c)}</th>" for c in header)
            body_html = "".join(
                "<tr>" + "".join(f"<td>{_inline(c)}</td>" for c in r) + "</tr>" for r in rows
            )
            out.append(f"<table><thead><tr>{head_html}</tr></thead><tbody>{body_html}</tbody></table>")
            continue

        # 引用块（连续 `> ` 行合并成一段）
        if stripped.startswith(">"):
            buf = []
            while i < n and lines[i].strip().startswith(">"):
                buf.append(_inline(lines[i].strip().lstrip(">").strip()))
                i += 1
            out.append("<div class=\"note\">" + "<br>".join(buf) + "</div>")
            continue

        # 列表（- 开头，连续行合并）
        if stripped.startswith("- "):
            items = []
            while i < n and lines[i].strip().startswith("- "):
                items.append(f"<li>{_inline(lines[i].strip()[2:])}</li>")
                i += 1
            out.append("<ul>" + "".join(items) + "</ul>")
            continue

        # 段落（连续非空行合并，行内换行保留）
        buf = []
        while i < n:
            cur = lines[i].strip()
            if not cur or cur.startswith(("#", "|", ">", "- ")) or cur.startswith(_CODE_FENCE):
                break
            buf.append(_inline(cur))
            i += 1
        if buf:
            out.append("<p>" + "<br>".join(buf) + "</p>")

    return "\n".join(out)
