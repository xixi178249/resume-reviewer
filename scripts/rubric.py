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
    "INDUSTRY_DEFAULT",
    "parse",
    "load",
    "get",
    "num",
    "thin",
    "ATS_PROBES",
    "ats_visual_items",
    "ats_blocking_items",
    "ats_platforms",
    "weights",
    "weights_for",
    "dimensions",
    "industries",
    "list_industries",
    "industry_profile",
    "verdict_bands",
    "bands",
    "band_for",
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


def bands() -> list[tuple[str, str, int, int, int, str, str, str]]:
    """分数段锚点，按分数从高到低。

    返回 [(key, label, min, max, suggest, feature, typical, misjudge)]。
    **段必须 0~100 连续全覆盖、且每段完整落在同一个 verdict 区间内**，
    否则抛 RubricError —— 两套分档（verdict 阈值 vs 分数段）漂移是最隐蔽的错：
    分数看着合理，结论标签却是隔壁那段的。
    """
    raw = get("bands")
    if not isinstance(raw, dict) or not raw:
        raise RubricError("rubric 缺少 bands 段（分数段锚点），见 config/rubric.yaml")
    rows = []
    for key, item in raw.items():
        if not isinstance(item, dict):
            raise RubricError(f"bands.{key} 应是字典（label/min/max/…），实际是 {type(item).__name__}")
        for field in ("label", "min", "max", "suggest", "feature", "typical", "misjudge"):
            if field not in item:
                raise RubricError(f"bands.{key} 缺少字段：{field}")
        lo, hi = int(item["min"]), int(item["max"])
        rows.append((key, str(item["label"]), lo, hi, int(item["suggest"]),
                     str(item["feature"]), str(item["typical"]), str(item["misjudge"])))

    rows.sort(key=lambda r: r[2])
    # 连续且全覆盖
    expect = 0
    for key, _label, lo, hi, _sg, _f, _t, _m in rows:
        if lo != expect:
            raise RubricError(f"分数段有断档或重叠：{key} 从 {lo} 开始，但前一段到 {expect - 1}")
        if hi < lo:
            raise RubricError(f"分数段 {key} 的 max({hi}) 小于 min({lo})")
        expect = hi + 1
    if expect - 1 != 100:
        raise RubricError(f"分数段没有覆盖到 100：最后一段到 {expect - 1}")

    # 每段完整落在同一个 verdict 区间内
    cut = _verdict_cut_points()
    for key, _label, lo, hi, _sg, _f, _t, _m in rows:
        holder = [c for c in cut if c <= lo]
        verdict = max(holder) if holder else 0
        for c in cut:
            if lo < c <= hi:
                raise RubricError(
                    f"分数段 {key}（{lo}-{hi}）跨过了 verdict 阈值 {c}："
                    "改 verdict 阈值时必须同步改分数段，否则分数和结论会对不上"
                )
    return rows


def _verdict_cut_points() -> list[int]:
    """verdict 区间下界（含 0），升序。"""
    bands = verdict_bands()          # [(min_score, verdict), ...] 从高到低
    cuts = sorted({lo for lo, _v in bands} | {0})
    return cuts


def band_for(score: int | float) -> tuple[str, str, int, int, int, str, str, str]:
    """分数落在哪一段。超出 0~100 一律夹到边界，不抛错（分数是外部输入）。"""
    try:
        s = int(round(float(score)))
    except (TypeError, ValueError):
        s = 0
    s = max(0, min(100, s))
    for row in bands():
        if row[2] <= s <= row[3]:
            return row
    raise RubricError(f"分数 {s} 不落在任何分数段（bands 覆盖不全）")


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


def thin() -> dict[str, int]:
    """低信息简历的三个字数阈值（见 config/rubric.yaml 的 `thin:` 段）。

    校验放在这里而不是在调用方：三个数的相对关系错了（比如 min_chars 比
    advisory_max 还大），表现是「40 字的走正常评分、120 字的反而被拒」这种
    反直觉行为，光看调用点看不出来。
    """
    raw = get("thin")
    if not isinstance(raw, dict) or not raw:
        raise RubricError("rubric 缺少 thin 段（低信息简历阈值），见 config/rubric.yaml")
    out: dict[str, int] = {}
    for field in ("min_chars", "advisory_max", "max_questions"):
        if field not in raw:
            raise RubricError(f"thin.{field} 缺失，见 config/rubric.yaml 的 thin 段")
        value = raw[field]
        if isinstance(value, bool) or not isinstance(value, int):
            raise RubricError(f"thin.{field} 必须是整数，实际是 {value!r}")
        if value <= 0:
            raise RubricError(f"thin.{field} 必须大于 0，实际是 {value}")
        out[field] = value
    if out["min_chars"] >= out["advisory_max"]:
        raise RubricError(
            f"thin.min_chars({out['min_chars']}) 必须小于 thin.advisory_max"
            f"({out['advisory_max']})，否则「建议模式」区间是空的"
        )
    if out["max_questions"] < 1:
        raise RubricError("thin.max_questions 至少为 1，否则建议模式问不出任何问题")
    return out


# 平台层里 checks 的 key 前缀决定这条能不能从正文判出来（见 rubric.yaml 的 ats 段）
ATS_PROBES = ("dates", "sections", "contact", "keywords", "length", "layout", "form", "filename")
_CHECK_ID = re.compile(r"^([a-z][a-z0-9]*)_([a-z][a-z0-9_]*)$")


def _ats_raw() -> dict[str, Any]:
    raw = get("ats")
    if not isinstance(raw, dict) or not raw:
        raise RubricError("rubric 缺少 ats 段（视觉项与平台层清单），见 config/rubric.yaml")
    return raw


def _text_list(raw: dict[str, Any], field: str) -> list[str]:
    items = raw.get(field)
    if not isinstance(items, list) or not items:
        raise RubricError(f"ats.{field} 必须是非空列表，见 config/rubric.yaml")
    out: list[str] = []
    for i, item in enumerate(items):
        if not isinstance(item, str) or not item.strip():
            raise RubricError(f"ats.{field}[{i}] 必须是非空字符串，实际是 {item!r}")
        if "\n" in item:
            raise RubricError(f"ats.{field}[{i}] 不能换行")
        out.append(item.strip())
    if len(set(out)) != len(out):
        raise RubricError(f"ats.{field} 有重复项：{out}")
    return out


def ats_visual_items() -> tuple[str, ...]:
    """有截图时必须逐项判满的固定 10 项（顺序即 prompt 表格顺序）。"""
    return tuple(_text_list(_ats_raw(), "visual_items"))


def ats_blocking_items() -> set[str]:
    """其中「一项不 ok 就直接 blocking」的致命项。"""
    items = set(ats_visual_items())
    blocking = set(_text_list(_ats_raw(), "blocking_items"))
    extra = blocking - items
    if extra:
        raise RubricError(
            f"ats.blocking_items 里的 {'、'.join(sorted(extra))} 不在 ats.visual_items 里"
            "（致命项必须是视觉项之一，否则永远不会被判定）"
        )
    return blocking


def ats_platforms() -> list[tuple[str, str, str, dict[str, str]]]:
    """平台层清单：`[(key, label, note, {check_id: 要检查什么})]`，按配置顺序。

    每条 check 的 key 必须带探针前缀（`ATS_PROBES`）。理由：写了条目却没人判，
    等于给用户一句假的「已检查」；前缀不认识就抛错，逼着实现方补判定或删条目。
    """
    raw = _ats_raw().get("platforms")
    if not isinstance(raw, dict) or not raw:
        raise RubricError("ats.platforms 必须是非空映射，见 config/rubric.yaml")
    out: list[tuple[str, str, str, dict[str, str]]] = []
    for key, prof in raw.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise RubricError(f"ats.platforms 的键 {key!r} 必须是 ASCII 标识符（中文名放 label）")
        if not isinstance(prof, dict):
            raise RubricError(f"ats.platforms.{key} 必须是映射（label/note/checks）")
        for field in ("label", "note"):
            value = prof.get(field)
            if not isinstance(value, str) or not value.strip():
                raise RubricError(f"ats.platforms.{key}.{field} 必须是非空字符串")
            if "\n" in value:
                raise RubricError(f"ats.platforms.{key}.{field} 不能换行")
        checks = prof.get("checks")
        if not isinstance(checks, dict) or not checks:
            raise RubricError(f"ats.platforms.{key}.checks 必须是非空映射")
        if len(checks) < 3:
            raise RubricError(
                f"ats.platforms.{key}.checks 只有 {len(checks)} 条；"
                "少于 3 条的平台说明不了问题，要么补全要么删掉这个平台"
            )
        clean: dict[str, str] = {}
        for cid, text in checks.items():
            m = _CHECK_ID.match(cid)
            if not m:
                raise RubricError(
                    f"ats.platforms.{key}.checks.{cid} 必须形如 <探针>_<细节>，"
                    f"探针只能取 {'/'.join(ATS_PROBES)}"
                )
            if m.group(1) not in ATS_PROBES:
                raise RubricError(
                    f"ats.platforms.{key}.checks.{cid} 的探针 {m.group(1)!r} 不认识，"
                    f"可选：{'/'.join(ATS_PROBES)}"
                )
            if not isinstance(text, str) or not text.strip():
                raise RubricError(f"ats.platforms.{key}.checks.{cid} 必须是非空字符串")
            if "\n" in text:
                raise RubricError(f"ats.platforms.{key}.checks.{cid} 不能换行")
            clean[cid] = text.strip()
        out.append((key, prof["label"].strip(), prof["note"].strip(), clean))
    return out


def weights() -> dict[str, float]:
    """五维权重（0~1 小数），等价于 `weights_for(None)`。

    保留这个函数名是因为它是 v2.0~v2.2 的唯一入口，十几个模块和 453 条断言
    都在用它；行业权重上线后，它就是「通用行业」的权重，行为必须一模一样。
    """
    return weights_for(None)


def dimensions() -> dict[str, str]:
    """五维各量什么（一句话说明）。键集必须与顶层 weights 完全一致。

    放在这里而不是散在 UI/文档里：说法和数字同源，改一处不会三处不同步。
    """
    raw = get("dimensions")
    if not isinstance(raw, dict) or not raw:
        raise RubricError(
            "config/rubric.yaml 的 dimensions 缺失或为空。"
            "（v2.3 起五维说明由这里提供，键必须是 role_fit/impact/"
            "evidence_quality/structure/competitiveness）"
        )
    head = get("weights")
    unknown = [k for k in raw if k not in head]
    missing = [k for k in head if k not in raw]
    if unknown or missing:
        raise RubricError(
            "config/rubric.yaml 的 dimensions 与顶层 weights 键集不一致："
            f"多了 {unknown}、少了 {missing}"
        )
    for key, value in raw.items():
        if not isinstance(value, str) or not value.strip():
            raise RubricError(f"dimensions.{key} 必须是非空字符串说明，现在是 {value!r}")
    return {k: raw[k].strip() for k in head}


# 行业键的兜底值：用户没选行业 / 老报告没有 industry 字段时用它
INDUSTRY_DEFAULT = "default"


def industries() -> dict[str, dict[str, Any]]:
    """全部行业档案原始配置（每个含 label / emphasis / weights）。"""
    raw = get("industries")
    if not isinstance(raw, dict) or not raw:
        raise RubricError(
            "config/rubric.yaml 的 industries 缺失或为空。"
            "（v2.3 起行业权重由这里提供；只想要通用权重就把 default 留着）"
        )
    return raw


def list_industries() -> list[tuple[str, str, str]]:
    """行业下拉用：[(key, label, emphasis), ...]，default 排第一。"""
    rows = []
    for key, profile in industries().items():
        if not isinstance(profile, dict):
            raise RubricError(f"industries.{key} 必须是映射（含 label/emphasis/weights）")
        rows.append((key, str(profile.get("label") or key), str(profile.get("emphasis") or "")))
    rows.sort(key=lambda r: (r[0] != INDUSTRY_DEFAULT,))
    return rows


def weights_for(industry: str | None = None) -> dict[str, float]:
    """某行业的五维权重（0~1 小数）。None 或空串 → default。

    未知行业键直接抛错，不静默回落：行业是用户显式选的下拉项，键写错说明
    前端/调用方传了脏值，静默用通用权重会让「按金融权重评」的承诺悄悄失效。
    报告侧要容错的话，捕获 RubricError 后自己决定回落并打标记。
    """
    key = (industry or "").strip() or INDUSTRY_DEFAULT
    if key == INDUSTRY_DEFAULT:
        # 通用权重的唯一真源是顶层 `weights`（v1.x 起就在那儿）。
        # `industries.default.weights` 只是给下拉用的副本，测试断言两者一致，
        # 这样既不动老配置的语义，也不会让 copy 悄悄漂掉。
        table: dict[str, Any] = get("weights")
        where = "weights"
    else:
        raw = industries()
        if key not in raw:
            known = "、".join(sorted(raw))
            raise RubricError(
                f"未知行业：{key!r}。可选：{known}（config/rubric.yaml 的 industries）"
            )
        profile = raw[key]
        if not isinstance(profile, dict) or not isinstance(profile.get("weights"), dict):
            raise RubricError(f"industries.{key}.weights 缺失或不是映射")
        table = profile["weights"]
        where = f"industries.{key}.weights"
    total = sum(float(v) for v in table.values())
    if abs(total - 100.0) > 1e-6:
        raise RubricError(
            f"config/rubric.yaml 的 {where} 合计是 {total}，必须等于 100。"
            "（权重和不是 100 会让该行业的总分永远落不到 100）"
        )
    expected = set(get("weights"))
    if set(table) != expected:
        raise RubricError(
            f"industries.{key}.weights 的维度名与顶层 weights 不一致："
            f"多 {sorted(set(table) - expected)}，少 {sorted(expected - set(table))}"
        )
    return {k: float(v) / 100.0 for k, v in table.items()}


def industry_profile(industry: str | None = None) -> dict[str, Any]:
    """单个行业的 label / emphasis / weights（权重已转成 0~1）。"""
    key = (industry or "").strip() or INDUSTRY_DEFAULT
    raw = industries()
    if key not in raw:
        raise RubricError(f"未知行业：{key!r}")
    profile = raw[key]
    return {
        "key": key,
        "label": str(profile.get("label") or key),
        "emphasis": str(profile.get("emphasis") or ""),
        "weights": weights_for(key),
    }



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
