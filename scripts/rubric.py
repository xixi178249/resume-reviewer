"""评分规范（rubric）的统一入口：所有阈值只从 `config/rubric.yaml` 读。

为什么要有这一层：
    v1.x 里「多长算长句」「引用最短几个字」「两关的分数下限」这些数字散在
    check_output.py / version_diff.py / ingest.py 三个文件里，prompt 模板又
    各写一遍。改一次阈值要同步三处，v1.7、v1.8 各漏过一次（prompt 写 60 字、
    脚本按 40 字判）。v2.0 起：**数字只在 rubric.yaml 里出现**。

零依赖：
    不希望为了读一个配置文件就拖进 PyYAML（本项目卖点之一就是 clone 即跑）。
    这里实现的是 YAML 的一个**受限子集**，够用且行为明确：

        - 缩进表示层级（2 空格，不要求严格）
        - `key: value`，value 支持 int / float / true / false / 字符串 / [a, b]
        - `#` 到行尾是注释（值里的 # 需加引号，但本文件没有这种值）
        - 不支持：多行字符串、锚点、标签、多文档

    超出子集的写法请先扩展本模块并补测试（scripts/test_rubric_single_source.py）。

用法：
    import rubric
    rubric.num("readability.long_sentence_chars")   # -> 60
    rubric.get("gates.hr.role_fit_min")             # -> 3
    rubric.weights()                                # -> {"role_fit": 0.25, ...}
    rubric.verdict_bands()                          # -> [(80, "strong_pass"), ...]

找不到文件或键时抛 RubricError，错误信息里带键的路径，不静默给默认值
（静默兜底会让「配置写错」表现成「评测结果诡异」）。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

__all__ = [
    "RubricError",
    "CONFIG_PATH",
    "parse",
    "load",
    "get",
    "num",
    "weights",
    "verdict_bands",
    "reload",
]

# 仓库根目录下的 config/rubric.yaml
CONFIG_PATH = Path(__file__).resolve().parent.parent / "config" / "rubric.yaml"


class RubricError(RuntimeError):
    """rubric 文件缺失 / 语法不支持 / 缺键。"""


_KEY_RE = re.compile(r"^(?P<indent>[ ]*)(?P<key>[A-Za-z_][A-Za-z0-9_]*)[ ]*:[ ]*(?P<val>.*)$")
_INT_RE = re.compile(r"^-?\d+$")
_FLOAT_RE = re.compile(r"^-?\d+\.\d+$")
_LIST_RE = re.compile(r"^\[(?P<body>.*)\]$")

_MISSING = object()

# 解析结果缓存：key 是文件绝对路径 + mtime，改文件后自动失效
_cache: dict[tuple[str, float], dict[str, Any]] = {}


def _strip_comment(line: str) -> str:
    """去掉行尾注释；引号内的 # 不动（本文件没有这种值，但别留坑）。"""
    out, quote = [], ""
    for ch in line:
        if quote:
            if ch == quote:
                quote = ""
            out.append(ch)
            continue
        if ch in "\"'":
            quote = ch
            out.append(ch)
            continue
        if ch == "#":
            break
        out.append(ch)
    return "".join(out).rstrip()


def _scalar(token: str) -> Any:
    """把右侧字符串转成 Python 值。"""
    tok = token.strip()
    if tok == "":
        return ""
    if (tok[0] == tok[-1]) and tok[0] in "\"'" and len(tok) >= 2:
        return tok[1:-1]
    if tok in ("true", "True"):
        return True
    if tok in ("false", "False"):
        return False
    if tok in ("null", "None", "~"):
        return None
    m = _LIST_RE.match(tok)
    if m:
        body = m.group("body").strip()
        if not body:
            return []
        return [_scalar(part) for part in body.split(",")]
    if _INT_RE.match(tok):
        return int(tok)
    if _FLOAT_RE.match(tok):
        return float(tok)
    return tok


def parse(text: str, source: str = "<text>") -> dict[str, Any]:
    """解析受限 YAML 子集，返回嵌套 dict。"""
    root: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(-1, root)]

    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw)
        if not line.strip():
            continue
        m = _KEY_RE.match(line)
        if not m:
            raise RubricError(
                f"{source}:{lineno} 这一行解析不了：{raw.strip()!r}\n"
                "本模块只支持 `key: value`（值可为 int/float/true/false/字符串/[a, b]）。"
            )
        indent = len(m.group("indent"))
        key = m.group("key")
        val = m.group("val").strip()

        while len(stack) > 1 and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if key in parent:
            raise RubricError(f"{source}:{lineno} 键重复：{key}")

        if val == "":
            child: dict[str, Any] = {}
            parent[key] = child
            stack.append((indent, child))
        else:
            parent[key] = _scalar(val)

    return root


def load(path: str | Path | None = None) -> dict[str, Any]:
    """读取并解析 rubric；同一文件 + 同一 mtime 只解析一次。"""
    p = Path(path) if path is not None else CONFIG_PATH
    p = p.resolve()
    if not p.exists():
        raise RubricError(f"找不到 rubric 文件：{p}\n期望它在仓库根的 config/rubric.yaml。")
    key = (str(p), p.stat().st_mtime)
    if key not in _cache:
        try:
            _cache[key] = parse(p.read_text(encoding="utf-8"), source=str(p))
        except RubricError:
            raise
        except OSError as exc:
            raise RubricError(f"读不了 rubric 文件 {p}：{exc}") from exc
    return _cache[key]


def reload() -> dict[str, Any]:
    """清缓存重读（测试与改配置后用）。"""
    _cache.clear()
    return load()


def get(dotted: str, default: Any = _MISSING) -> Any:
    """按 a.b.c 取配置；缺键抛错，除非显式给了 default。"""
    node: Any = load()
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            if default is not _MISSING:
                return default
            raise RubricError(
                f"config/rubric.yaml 缺少键：{dotted}\n"
                f"（已走到 `{part}` 这一层就没了；检查缩进或补上这一项）"
            )
        node = node[part]
    return node


def num(dotted: str) -> float:
    """取数字型配置；非数字（写成了字符串/列表）直接报错，避免拿字符串比大小。"""
    value = get(dotted)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RubricError(f"config/rubric.yaml 里的 {dotted} 不是数字：{value!r}")
    return value


def weights() -> dict[str, float]:
    """五维权重（0~1 小数）。合计必须为 1。"""
    raw = get("weights")
    total = sum(float(v) for v in raw.values())
    if abs(total - 100.0) > 1e-6:
        raise RubricError(
            f"config/rubric.yaml 的 weights 合计是 {total}，必须等于 100。"
            "（权重和不是 100 会让总分永远落不到 100）"
        )
    return {k: float(v) / 100.0 for k, v in raw.items()}


def verdict_bands() -> list[tuple[float, str]]:
    """结论分档，从高到低：[(80, 'strong_pass'), (65, 'pass'), (50, 'borderline'), (0, 'reject')]。

    为什么校验「原始顺序」而不是排完序再校验：键名本身就是档位语义，
    把 strong_pass_min 写成 50、borderline_min 写成 80 时，排序会把它们
    “修正”成一个看起来合理的序列，等于掩盖了配置错误。
    """
    v = get("verdict")
    raw = [
        (float(v["strong_pass_min"]), "strong_pass"),
        (float(v["pass_min"]), "pass"),
        (float(v["borderline_min"]), "borderline"),
    ]
    for (hi, name_hi), (lo, name_lo) in zip(raw, raw[1:]):
        if hi <= lo:
            raise RubricError(
                f"config/rubric.yaml 的 verdict 分档不递增：{name_hi}={hi:g} 必须大于 {name_lo}={lo:g}"
                "（强推进的门槛不能低于通过的门槛）"
            )
    return raw + [(0.0, "reject")]
