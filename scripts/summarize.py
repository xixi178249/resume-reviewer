"""把 outputs/ 里的评审结果汇总成一张表，方便横向对比。

用法：
    python scripts/summarize.py
    python scripts/summarize.py --kind real
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "outputs"
TXT = ROOT / "samples" / "txt"


def main() -> int:
    ap = argparse.ArgumentParser(description="汇总 outputs 结果")
    ap.add_argument("--kind", default="real", help="real / mock / all")
    args = ap.parse_args()

    pattern = "*.json" if args.kind == "all" else f"*.{args.kind}.json"
    files = sorted(OUT.glob(pattern))
    if not files:
        print(f"[error] {OUT} 下没有 {pattern}", file=sys.stderr)
        return 2

    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import check_output as co

    rows = []
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        case = f.name.split(".")[0]
        resume = TXT / f"{case}.txt"
        res = co.check(d, resume.read_text(encoding="utf-8") if resume.exists() else "")
        fr = d.get("format_review") or {}
        dims = d.get("dimension_scores") or {}
        rows.append({
            "case": case,
            "score": d.get("overall_score"),
            "verdict": d.get("verdict"),
            "hr": "过" if (d.get("gate_hr") or {}).get("pass") else "不过",
            "iv": "过" if (d.get("gate_interview") or {}).get("pass") else "不过",
            "fmt": fr.get("verdict", "-"),
            "dims": "".join(str((dims.get(k) or {}).get("score", "-")) for k in co.WEIGHTS),
            "issues": len(d.get("issues") or []),
            "err": len(res["errors"]),
            "warn": len(res["warnings"]),
            "hit": f"{res['stats'].get('evidence_hit')}/{res['stats'].get('evidence_total')}",
        })

    w = max(len(r["case"]) for r in rows) + 2
    print(f"{'简历':<{w}}{'分':>4}{'结论':>12}{'初筛':>6}{'面试':>6}{'格式':>10}{'维度':>8}{'问题':>4}{'错':>4}{'警':>4}{'引用':>9}")
    print("-" * (w + 71))
    for r in rows:
        print(
            f"{r['case']:<{w}}{r['score']:>4}{r['verdict']:>12}{r['hr']:>6}{r['iv']:>6}"
            f"{r['fmt']:>10}{r['dims']:>8}{r['issues']:>4}{r['err']:>4}{r['warn']:>4}{r['hit']:>9}"
        )

    print(f"\n共 {len(rows)} 份｜有错 {sum(1 for r in rows if r['err'])} 份"
          f"｜格式阻断 {sum(1 for r in rows if r['fmt'] == 'blocking')} 份")
    print("维度顺序：role_fit / impact / evidence_quality / structure / competitiveness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
