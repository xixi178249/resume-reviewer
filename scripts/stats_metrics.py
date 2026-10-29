"""v2.0「A 路线：可信度」的统计内核 —— 全部是纯函数，只依赖标准库。

为什么要单独一个模块：
    对外报告里的每个数字（MAE、κ、相关系数、置信区间）都会被写进 README 和
    评测文档，算错一次就把整条路线的可信度打折。所以口径集中在这里：
    只接收纯数据，不打印、不读写文件、不持有全局可变状态，可以用构造数据
    一条条钉死。

统一约定（改代码前先看这几条，它们是刻意的）：
  1. 「无定义」一律返回 None，绝不返回 0 冒充。
     0 本身是合法统计值（MAE=0 就是完全一致），用 0 表示"算不出来"会让报告说谎。
  2. 返回 float 的聚合函数（mae / rmse / agreement_within）没有 None 可用，
     空输入直接抛 ValueError。没有样本就没有"平均误差"，返回 0.0 会被读成
     "零误差"——比崩掉更危险。
  3. 长度不一致、元素不是二元组、标签不可哈希 → ValueError。
     这是调用方的 bug，静默截断会让两份榜单错位对上，比直接崩更难查。
  4. 相关系数夹到 [-1, 1]。浮点误差能算出 1.0000000000000002，
     报告里出现大于 1 的相关系数属于低级错误。
     κ 不夹：Cohen's κ 在边际分布极偏时确实可以小于 -1，那是真实结果。

本模块没有 main，也不被 scripts 以外的东西 import。
"""

from __future__ import annotations

import math
import random
import statistics
from collections import Counter

# 差值的分位数口径：与 statistics.quantiles(method="inclusive") 一致。
# 单独实现是因为要能对已排序序列反复取分位，且要在单点样本上直接返回该点。
DIST_KEYS = ("n", "min", "p10", "p25", "median", "p75", "p90", "max", "mean")


# ---------------------------------------------------------------- 输入清洗
def _as_floats(values, what: str) -> list[float]:
    """把输入统一成 float 列表；类型不对就报错，不猜调用方想干什么。"""
    try:
        return [float(v) for v in values]
    except (TypeError, ValueError):
        raise ValueError(f"{what} 必须是数值序列") from None


def _as_pairs(pairs, what: str = "pairs") -> list[tuple[float, float]]:
    """把输入统一成 [(实际值, 预测值), ...]，并挡掉空输入。"""
    try:
        data = [(float(a), float(b)) for a, b in pairs]
    except (TypeError, ValueError):
        raise ValueError(f"{what} 必须是 (实际值, 预测值) 二元组序列") from None
    if not data:
        raise ValueError(f"{what} 为空：没有样本就没有平均误差，不能用 0 代替")
    return data


def _quantile(ordered: list[float], q: float) -> float | None:
    """在**已排序**序列上取分位数；位置落在两个样本之间时线性插值。

    单点样本直接返回该点，不插值——否则会出现 min < p10 之类的怪事。
    """
    if not ordered:
        return None
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    low = math.floor(pos)
    high = math.ceil(pos)
    if low == high:
        return ordered[low]
    frac = pos - low
    return ordered[low] * (1.0 - frac) + ordered[high] * frac


def _rank(values: list[float]) -> list[float]:
    """秩次（1 起），并列取平均秩：[10, 20, 20, 30] → [1, 2.5, 2.5, 4]。"""
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def _clamp_unit(value: float) -> float:
    """把相关系数夹回 [-1, 1]，只用于浮点误差修正。"""
    return max(-1.0, min(1.0, value))


# ---------------------------------------------------------------- 误差
def mae(pairs) -> float:
    """平均绝对误差。pairs = [(人工分, 机器分), ...]。

    空输入抛 ValueError：返回 0.0 会被读成「机器和人工完全一致」。
    """
    data = _as_pairs(pairs)
    return sum(abs(a - b) for a, b in data) / len(data)


def rmse(pairs) -> float:
    """均方根误差。大偏差比 mae 罚得更重，用来抓"个别样本错得离谱"。"""
    data = _as_pairs(pairs)
    return math.sqrt(sum((a - b) ** 2 for a, b in data) / len(data))


# ---------------------------------------------------------------- 相关
def pearson(xs, ys) -> float | None:
    """Pearson 线性相关系数。

    少于 3 个点时返回 None：两点必然落在一条直线上，r 恒为 ±1，
    看起来是"完美相关"，其实什么都没测到。任一侧方差为 0 时同样返回 None
    （公式分母为 0，返回 0 会被误读成"不相关"——那是另一个结论）。
    """
    x = _as_floats(xs, "xs")
    y = _as_floats(ys, "ys")
    if len(x) != len(y):
        raise ValueError(f"xs 与 ys 长度不一致：{len(x)} != {len(y)}")
    if len(x) < 3:
        return None
    mx = sum(x) / len(x)
    my = sum(y) / len(y)
    dx = [v - mx for v in x]
    dy = [v - my for v in y]
    sxx = sum(v * v for v in dx)
    syy = sum(v * v for v in dy)
    if sxx == 0 or syy == 0:
        return None
    sxy = sum(a * b for a, b in zip(dx, dy))
    return _clamp_unit(sxy / math.sqrt(sxx * syy))


def spearman(xs, ys) -> float | None:
    """Spearman 秩相关系数：先取秩次再算 Pearson，并列取平均秩。

    分数量表只有 0~100 且大量并列，Spearman 比 Pearson 更稳：
    它只关心"谁比谁高"，不受分数分布偏斜影响。
    """
    x = _as_floats(xs, "xs")
    y = _as_floats(ys, "ys")
    if len(x) != len(y):
        raise ValueError(f"xs 与 ys 长度不一致：{len(x)} != {len(y)}")
    if len(x) < 3:
        return None
    return pearson(_rank(x), _rank(y))


# ---------------------------------------------------------------- 一致性
def cohen_kappa(a_labels, b_labels) -> float | None:
    """Cohen's κ，用于四档 verdict（strong_pass / pass / borderline / reject）。

    接受任意可哈希标签；返回 (Po - Pe) / (1 - Pe)。
    Pe == 1（两人把全部样本都放进同一个档位）时返回 None：
    此时偶然一致率已经是 100%，κ 的分子分母同时为 0，没有可解释的一致性。
    样本少于 2 个也返回 None——单个样本算不出"比随机好多少"。
    """
    try:
        a = list(a_labels)
        b = list(b_labels)
    except TypeError:
        raise ValueError("a_labels / b_labels 必须是标签序列") from None
    if len(a) != len(b):
        raise ValueError(f"两个标注者样本数不一致：{len(a)} != {len(b)}")
    n = len(a)
    if n < 2:
        return None
    try:
        counts_a = Counter(a)
        counts_b = Counter(b)
    except TypeError:
        raise ValueError("标签必须可哈希（verdict 字符串、数字等）") from None
    po = sum(1 for x, y in zip(a, b) if x == y) / n
    pe = sum(c * counts_b.get(k, 0) for k, c in counts_a.items()) / (n * n)
    if pe >= 1.0:
        return None
    return (po - pe) / (1.0 - pe)


def quadratic_weighted_kappa(a_scores, b_scores, lo=0, hi=100) -> float | None:
    """二次加权 κ，用于 0~100 分的分数量表。

    权重 (i - j)² / (hi - lo)²：差 1 分几乎不罚，差 100 分罚满。
    这就是它比 cohen_kappa 适合分数的原因——把 82 判成 80 和判成 40
    不该被当成同一类错误。

    几个约定：
      - 分数先四舍五入到整数档位（分数本来就是整数口径，0.4 分的差别无意义）；
      - 越界分数（如 105 分）按边界档计，不抛错：一份脏数据不该让整张报告
        算不出来，越界本身已经在质量门那一层被拦下了；
      - 所有人都打同一档 → 期望不一致量为 0，返回 None（同 cohen_kappa 的道理）；
      - 样本少于 2 个返回 None。
    """
    a = _as_floats(a_scores, "a_scores")
    b = _as_floats(b_scores, "b_scores")
    if len(a) != len(b):
        raise ValueError(f"两方样本数不一致：{len(a)} != {len(b)}")
    lo_i, hi_i = int(lo), int(hi)
    if hi_i <= lo_i:
        raise ValueError(f"分档区间非法：lo={lo!r} 必须严格小于 hi={hi!r}")
    n = len(a)
    if n < 2:
        return None

    span = hi_i - lo_i
    span_sq = float(span * span)
    bins = span + 1

    def band(value: float) -> int:
        """分数 → 档位偏移（0 起）。先四舍五入，再夹进 [lo, hi]。"""
        k = int(round(value))
        return min(hi_i, max(lo_i, k)) - lo_i

    counts: dict[tuple[int, int], int] = {}
    for x, y in zip(a, b):
        key = (band(x), band(y))
        counts[key] = counts.get(key, 0) + 1

    row = [0] * bins
    col = [0] * bins
    for (i, j), c in counts.items():
        row[i] += c
        col[j] += c

    obs_dis = 0.0
    for (i, j), c in counts.items():
        obs_dis += ((i - j) * (i - j) / span_sq) * c
    exp_dis = 0.0
    for i in range(bins):
        ri = row[i]
        if not ri:
            continue
        for j in range(bins):
            cj = col[j]
            if not cj:
                continue
            exp_dis += ((i - j) * (i - j) / span_sq) * ri * cj / n

    if exp_dis == 0.0:
        return None
    return 1.0 - obs_dis / exp_dis


def agreement_within(pairs, tol) -> float:
    """|人工分 - 机器分| <= tol 的比例。tol 含边界：差值恰好等于 tol 算"在内"。

    「差 5 分以内就算一致」这类容差指标，边界必须是闭区间：
    否则 tol=5 时，差 5 分与差 6 分同样被排除，容差形同虚设。
    """
    data = _as_pairs(pairs)
    try:
        tol_f = float(tol)
    except (TypeError, ValueError):
        raise ValueError(f"tol 必须是数值，实际 {tol!r}") from None
    if tol_f < 0:
        raise ValueError(f"tol 不能为负：{tol!r}")
    hit = sum(1 for a, b in data if abs(a - b) <= tol_f)
    return hit / len(data)


# ---------------------------------------------------------------- 分布
def diff_distribution(values) -> dict:
    """差值分布。空输入返回 n=0、其余全 None（"没有数据"要能被看见）。"""
    data = _as_floats(values, "values")
    if not data:
        return {"n": 0, **{k: None for k in DIST_KEYS[1:]}}
    ordered = sorted(data)
    return {
        "n": len(ordered),
        "min": ordered[0],
        "p10": _quantile(ordered, 0.10),
        "p25": _quantile(ordered, 0.25),
        "median": _quantile(ordered, 0.50),
        "p75": _quantile(ordered, 0.75),
        "p90": _quantile(ordered, 0.90),
        "max": ordered[-1],
        "mean": statistics.mean(ordered),
    }


def summarize(scores) -> dict:
    """分数概况：n / mean / sd / min / max。

    sd 用**样本标准差**（除以 n-1）：这里的分数是从更大的简历池里抽出来的样本，
    估的是总体波动。注意与 human_eval.stability_metrics 的 pstdev 口径不同——
    那个衡量的是同一份简历 3 次运行的全部离散程度，不是抽样估计。
    n < 2 时 sd 无定义，返回 None。
    """
    data = _as_floats(scores, "scores")
    if not data:
        return {"n": 0, "mean": None, "sd": None, "min": None, "max": None}
    return {
        "n": len(data),
        "mean": statistics.mean(data),
        "sd": statistics.stdev(data) if len(data) >= 2 else None,
        "min": min(data),
        "max": max(data),
    }


# ---------------------------------------------------------------- 置信区间
def bootstrap_ci(pairs, stat_fn, n=2000, ci=0.95, seed=0):
    """对配对样本做百分位 bootstrap，返回 stat_fn 的 (下界, 上界)。

    stat_fn 接收 [(实际值, 预测值), ...]，返回一个数或 None（例如 pearson 在
    重采样样本方差为 0 时会返回 None）。返回 None 的重采样直接丢掉。

    可复现：随机数生成器固定用 random.Random(seed)，同样输入必然得到同样区间，
    否则报告里的"95% 置信区间"每次刷新都变，没人敢引用。

    区间会被撑到包住点估计 stat_fn(pairs)：报告里出现"点估计落在区间外"，
    读者第一反应是算错了；而且百分位法在分布偏斜时（MAE 的分布天然右偏）
    确实可能漏掉点估计。
    """
    data = list(pairs)
    if not data:
        return None
    try:
        point = stat_fn(data)
    except ValueError:
        # 单点 / 空样本这类 stat_fn 自己会拒绝的情况
        return None
    if point is None:
        return None
    if len(data) < 2:
        # 只抽一个样本时每次重采样都等于它自己，区间退化成一个点，没有信息量
        return None
    if not isinstance(n, int) or n < 1:
        raise ValueError(f"重采样次数 n 必须是正整数：{n!r}")
    try:
        ci_f = float(ci)
    except (TypeError, ValueError):
        raise ValueError(f"ci 必须是数值，实际 {ci!r}") from None
    if not 0.0 < ci_f < 1.0:
        raise ValueError(f"ci 必须在 (0, 1) 之间：{ci!r}")

    rng = random.Random(seed)
    size = len(data)
    stats: list[float] = []
    for _ in range(n):
        sample = [data[rng.randrange(size)] for _ in range(size)]
        value = stat_fn(sample)
        if value is not None:
            stats.append(float(value))
    if not stats:
        return None

    stats.sort()
    alpha = (1.0 - ci_f) / 2.0
    low = _quantile(stats, alpha)
    high = _quantile(stats, 1.0 - alpha)
    point_f = float(point)
    return (min(low, point_f), max(high, point_f))
