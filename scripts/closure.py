"""改稿闭环落实率：模型给的三条改写，用户第二次上传时到底改了没有。

为什么单独一个脚本
------------------
`version_diff.check_applied()` 只在「这次评审」的响应里算一次三态，算完就过去了。
一个 55% 的落实率如果只活在某一篇报告里，它既不会被验证，也不会被质疑——
而这恰恰是最容易被拿来当卖点的数字（「我们的建议被采纳了 78%」）。

所以这里做两件事：
1. **落盘**：评审时把对比结果写成 `reports/<stem>.meta.json` 旁挂文件（server.py 负责）；
2. **汇总**：本脚本扫这些旁挂文件，给出带分母、带样本量门槛的落实率。

口径（每一条都是为了不骗人）
--------------------------
- 只有 `.meta.json` 里 `compare` 非空、即**同一份简历的先后两版都被系统看过**，才算一个可判定样本；
  单独一份报告不产生落实率。
- 只有 `applied`（新文本里出现了改写后的说法）与 `untouched`（原句还在）进分母。
  `changed`（原句和改写都没了，用户自己另写了一套）和 `unknown`（判定不了）**不能算失败**，
  它们只是「没信息」，塞进分母会把落实率压成假的偏低值。
- 这是**文本比对推理**，不是用户自述，也不是编辑行为日志：它只能说明「新正文里出现了这个说法」，
  不能说明用户是因为这句话才改的。
- 可判定样本 < `min_pairs`（默认取 `config/rubric.yaml` 的 `eval.closure_min_pairs`）时
  `apply_rate` 返回 `None`，`status=insufficient_data`。3 次观察算出来的 67% 会被当成结论，
  这与 v2.0 反对的「把自我实现的 8.0 当准确率」是同一个错误。

用法
----
    python scripts/closure.py                    # 汇总 reports/ 下的旁挂文件
    python scripts/closure.py --reports /tmp/reports --json
    python scripts/closure.py --min-pairs 5      # 临时放宽门槛（报告里会写明）
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = Path(__file__).resolve().parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

import rubric  # noqa: E402

DEFAULT_REPORTS = ROOT / "reports"
META_SUFFIX = ".meta.json"
STATES = ("applied", "untouched", "changed", "unknown")


def default_min_pairs() -> int:
    """判定门槛的唯一来源是 rubric.yaml；读不到时退回 10 并把理由写进 summary。"""
    try:
        return int(rubric.num("eval.closure_min_pairs"))
    except Exception:  # noqa: BLE001
        return 10


def load_meta(path: Path) -> dict | None:
    """读一个旁挂文件；坏文件跳过而不是让整个汇总失败。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def collect(reports_dir: Path | str = DEFAULT_REPORTS) -> list[dict]:
    """收集所有带 compare 的旁挂记录，按时间倒序。"""
    directory = Path(reports_dir)
    if not directory.is_dir():
        return []
    records: list[dict] = []
    for path in sorted(directory.glob(f"*{META_SUFFIX}")):
        meta = load_meta(path)
        if not meta:
            continue
        compare = meta.get("compare")
        if not isinstance(compare, dict):
            continue
        counts = compare.get("applied_counts")
        if not isinstance(counts, dict):
            continue
        record = {
            "file_stem": meta.get("file_stem") or path.stem.replace(".meta", ""),
            "name": meta.get("name") or "",
            "created_at": meta.get("created_at") or "",
            "engine": meta.get("engine") or "",
            "prev_stem": (compare.get("prev") or {}).get("review_id") or "",
            "prev_at": (compare.get("prev") or {}).get("created_at") or "",
            "similarity": compare.get("similarity"),
            "source": path.name,
        }
        for state in STATES:
            try:
                record[state] = max(0, int(counts.get(state) or 0))
            except (TypeError, ValueError):
                record[state] = 0
        records.append(record)
    records.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return records


def summarize(records: list[dict], min_pairs: int | None = None) -> dict:
    """把记录汇总成一个可进接口/报告的字典。算不出来就 None，不填 0。

    为什么要按引擎分开算：规则引擎（local-rules）给的「改写」是**占位模板**
    （`负责…«补：接口数 / QPS»`），用户永远不会原样抄进去，所以逐字比对必然判 untouched。
    把两种引擎混在一起算，落实率会被结构性压低到接近 0——那是度量方法造成的假象，
    不是产品没被用。所以口径是：**只有模型引擎的改写进总落实率**，其余单独列出。
    """
    threshold = default_min_pairs() if min_pairs is None else int(min_pairs)
    TEXT_ENGINES_EXCLUDED = {"", "local-rules"}

    def _sum(subset: list[dict]) -> dict:
        totals = {state: 0 for state in STATES}
        for record in subset:
            for state in STATES:
                try:
                    totals[state] += max(0, int(record.get(state) or 0))
                except (TypeError, ValueError):
                    pass
        decided = totals["applied"] + totals["untouched"]
        return {**totals, "decided": decided,
                "apply_rate": round(totals["applied"] / decided, 4) if decided else None}

    textual = [r for r in records if (r.get("engine") or "") not in TEXT_ENGINES_EXCLUDED]
    totals = _sum(textual)
    by_engine: dict[str, dict] = {}
    for record in records:
        key = record.get("engine") or "unknown"
        by_engine.setdefault(key, []).append(record)
    engine_rows = {key: _sum(rows) for key, rows in sorted(by_engine.items())}

    rate = totals["apply_rate"]
    enough = totals["decided"] >= threshold and threshold > 0

    if not records:
        reason = "还没有任何一次「先评过、后来又传了新版本」的记录，落实率暂时算不出来"
    elif totals["decided"] == 0 and any(r.get("engine") == "local-rules" for r in records):
        reason = ("只有基础评测（规则引擎）的记录：它的改写给的是占位模板，逐字比对没有意义，"
                  "落实率只统计模型引擎的改写")
    elif totals["decided"] == 0:
        reason = "有对比记录，但每条改写都落在 changed/unknown（改成了别的说法或判定不了），没有可判定的条目"
    elif not enough:
        reason = f"可判定改写只有 {totals['decided']} 条，少于 {threshold} 条的门槛"
    else:
        reason = ""

    return {
        "status": "ok" if (enough and rate is not None) else "insufficient_data",
        "reason": reason,
        "min_pairs": threshold,
        "reports_with_meta": len(records),
        "rewrites_total": sum(totals[state] for state in STATES),
        "decided": totals["decided"],
        "apply_rate": rate if enough else None,
        "applied": totals["applied"],
        "untouched": totals["untouched"],
        "changed": totals["changed"],
        "unknown": totals["unknown"],
        "by_engine": engine_rows,
        "excluded_engines": sorted(TEXT_ENGINES_EXCLUDED - {""}),
    }


def to_markdown(summary: dict) -> str:
    lines = ["## 改稿闭环落实率", ""]
    if summary["status"] == "ok":
        pct = f"{summary['apply_rate'] * 100:.0f}%"
        lines.append(
            f"上一版给出的可判定改写中，**{summary['applied']}/{summary['decided']} 条（{pct}）**"
            "在新正文里出现了改后说法。"
        )
    else:
        lines.append(f"**数据不够，暂不给出落实率**：{summary['reason']}。")
    lines += [
        "",
        "| 项 | 值 |",
        "|---|---|",
        f"| 有对比记录的报告 | {summary['reports_with_meta']} 份 |",
        f"| 改写总数 | {summary['rewrites_total']} 条 |",
        f"| 已改（applied） | {summary['applied']} 条 |",
        f"| 原句还在（untouched） | {summary['untouched']} 条 |",
        f"| 改成了别的说法（changed） | {summary['changed']} 条 |",
        f"| 判定不了（unknown） | {summary['unknown']} 条 |",
        f"| 可判定条数 / 门槛 | {summary['decided']} / {summary['min_pairs']} |",
    ]
    by_engine = summary.get("by_engine") or {}
    if len(by_engine) > 1:
        lines += ["", "| 引擎 | 可判定 | 已改 |", "|---|---|---|"]
        for name, row in by_engine.items():
            mark = "（不进总落实率）" if name in (summary.get("excluded_engines") or []) else ""
            lines.append(f"| {name}{mark} | {row['decided']} | {row['applied']} |")
    lines += [
        "",
        "> 口径：changed 与 unknown 不进分母（它们没有信息，算进去会把落实率压低）；"
        "规则引擎的改写给的是占位模板，不进总落实率；"
        "落实率来自**文本比对**，不等于用户接受度。",
    ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="汇总改稿闭环落实率")
    parser.add_argument("--reports", default=str(DEFAULT_REPORTS), help="报告目录（含 *.meta.json）")
    parser.add_argument("--min-pairs", type=int, default=None, help="可判定条数门槛，默认取 rubric")
    parser.add_argument("--json", action="store_true", help="只输出 JSON")
    args = parser.parse_args(argv)

    records = collect(args.reports)
    summary = summarize(records, args.min_pairs)
    payload = {"ok": True, "summary": summary, "records": records}
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(to_markdown(summary))
        print(f"\n（扫描目录：{args.reports}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
