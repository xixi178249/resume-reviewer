"""v2.0 基准：一条命令算出「这个评审器的分数和人对得上吗」。

为什么要有它：
    v1 里所有关于「准不准」的说法都来自 8 份旧 gold（单人、非盲、写权重时就
    看过的数据），那个 MAE=8.0 是**自我实现**的结果，不能当准确度。
    要给出能写进简历/能对外说的数字，必须：
      数据集 ≥60 份、双人盲评、heldout 留出、指标口径固定、一条命令可复现。

这个脚本只做三件事：
    1. 按数据集切分取样本（**默认只碰 development，heldout 默认锁死**）
    2. 拿到「机器分」：规则引擎现算（免费）或读已有的 outputs/*.real.json
    3. 算指标并落盘 bench/report.md + bench/report.json

刻意不做的事：
    - 不改任何阈值、不挑参数、不提「最优 prompt」——那是调参。
      调参脚本读 heldout 的那一刻，留出集就废了，所以这里用代码锁住。
    - 标注不足时不给数字。报告顶部只有两种状态：READY / NOT_READY。

口径（写进报告的固定说法，别在别处另立一套）：
    MAE        —— 与「两名标注者的共识分」的平均绝对误差，越小越好
    Pearson    —— 线性相关；Spearman 为秩相关（对分数尺度的非线性不敏感）
    Cohen's κ  —— 四档 verdict（strong_pass/pass/borderline/reject）的一致性
    QWK        —— 0~100 分的二次加权 κ，扣分与「差 30 分」成比例
    ±5 / ±10   —— 与共识分差距不超过 5 / 10 分的比例
    spread     —— 同一份简历重复跑的极差（抖动，不是准确度）

用法：
    python scripts/benchmark.py                          # 规则引擎 + development
    python scripts/benchmark.py --predictor outputs      # 用已有的模型输出
    python scripts/benchmark.py --allow-heldout --split heldout   # 最终验收才允许
    python scripts/benchmark.py --require-ready          # 未就绪 → 退出码 2（CI 用）

退出码：0 正常；1 运行出错；2 未就绪（仅 --require-ready 时）或 heldout 被锁。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import human_eval  # noqa: E402
import rubric  # noqa: E402
import stats_metrics as sm  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "samples" / "eval" / "dataset-v1.json"
DEFAULT_ANNOTATIONS = ROOT / "samples" / "eval" / "annotations"
DEFAULT_OUT_MD = ROOT / "bench" / "report.md"
DEFAULT_OUT_JSON = ROOT / "bench" / "report.json"

SPLITS = ("development", "heldout", "all")
VERDICTS = ("strong_pass", "pass", "borderline", "reject")


class HeldoutLocked(RuntimeError):
    """在未明确允许时试图读取 heldout 集。"""


def resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else (ROOT / p)


def select_samples(dataset: dict, split: str = "development",
                   allow_heldout: bool = False) -> list[dict]:
    """按切分取样本。

    heldout 是留出集：一旦在调参/挑 prompt 时看过它的标签，它就不再能证明
    泛化能力。所以默认路径下它**根本不进内存**，要读必须显式写 --allow-heldout。
    """
    if split not in SPLITS:
        raise ValueError(f"split 只能是 {SPLITS}，收到：{split!r}")
    samples = [s for s in dataset.get("samples") or [] if s.get("sample_id")]
    if split == "heldout" or split == "all":
        if not allow_heldout:
            heldout = [s["sample_id"] for s in samples if s.get("split") == "heldout"]
            raise HeldoutLocked(
                "拒绝读取 heldout 集"
                f"（{len(heldout)} 份：{', '.join(heldout[:5]) or '无'}）。\n"
                "heldout 只用于最终验收：调参、改 prompt、挑阈值时都不许看。\n"
                "确实要做最终验收，请显式加 --allow-heldout。"
            )
    if split == "all":
        return samples
    return [s for s in samples if s.get("split") == split]


def predict_local(sample: dict) -> tuple[dict | None, str]:
    """用规则引擎现算一份（免费、确定性，随时可重跑）。"""
    import local_engine

    text_path = resolve(sample.get("text_file") or "")
    if not text_path.exists():
        return None, f"简历文本不存在：{sample.get('text_file')}"
    text = text_path.read_text(encoding="utf-8", errors="replace")
    if not text.strip():
        return None, "简历文本为空"
    doc = local_engine.build(
        text,
        job=sample.get("job_target") or "",
        jd=sample.get("jd") or "",
    )
    return doc, ""


def predict_outputs(sample: dict) -> tuple[dict | None, str]:
    """读已有的模型输出（省时间，但只能反映当时那次跑的结果）。"""
    path = resolve(sample.get("model_output") or "")
    doc = human_eval._model_score(path)  # noqa: SLF001  复用同一份读取口径
    if not isinstance(doc, dict):
        return None, f"模型输出不存在或不可读：{sample.get('model_output')}"
    return doc, ""


PREDICTORS = {"local": predict_local, "outputs": predict_outputs}


def build_pairs(samples: list[dict], annotations: list[dict],
                predictor: str) -> tuple[list[dict], list[str]]:
    """把「人工共识分 × 机器分」凑成一对一对，凑不齐的进 notes（不静默丢弃）。"""
    fn = PREDICTORS[predictor]
    consensus = human_eval.consensus(annotations) if annotations else {}
    pairs: list[dict] = []
    notes: list[str] = []

    for sample in samples:
        sid = sample["sample_id"]
        human = consensus.get(sid) or {}
        if human.get("score") is None:
            notes.append(f"{sid}：没有可用的人工共识分")
            continue
        doc, why = fn(sample)
        if doc is None:
            notes.append(f"{sid}：{why}")
            continue
        model_score = doc.get("overall_score")
        if not isinstance(model_score, int):
            notes.append(f"{sid}：机器分不是整数（{model_score!r}）")
            continue
        # 人工共识分是「多人维度的加权平均」，可能是 78.5 这种半档值，不能硬截成整数。
        pairs.append({
            "sample_id": sid,
            "split": sample.get("split") or "development",
            "role_family": (sample.get("stratum") or {}).get("role_family") or "",
            "human_score": round(float(human["score"]), 1),
            "model_score": int(model_score),
            "human_verdict": human.get("verdict"),
            "model_verdict": doc.get("verdict"),
            "human_hr": human.get("gate_hr"),
            "model_hr": bool((doc.get("gate_hr") or {}).get("pass")),
            "human_interview": human.get("gate_interview"),
            "model_interview": bool((doc.get("gate_interview") or {}).get("pass")),
        })
    return pairs, notes


def compute_metrics(pairs: list[dict]) -> dict:
    """口径固定的一组指标；样本不足时返回 None 而不是 0（0 会被读成「完全不一致」）。"""
    n = len(pairs)
    if n == 0:
        return {
            "n": 0, "mae": None, "mae_ci95": None, "rmse": None,
            "pearson": None, "spearman": None,
            "kappa_verdict": None, "qwk": None,
            "within5": None, "within10": None,
            "spread": None, "gate_hr_agreement": None,
            "gate_interview_agreement": None,
        }
    score_pairs = [(p["human_score"], p["model_score"]) for p in pairs]
    hs = [p["human_score"] for p in pairs]
    ms = [p["model_score"] for p in pairs]
    kappa = sm.cohen_kappa(
        [p["human_verdict"] for p in pairs], [p["model_verdict"] for p in pairs])
    hr_pairs = [(p["human_hr"], p["model_hr"]) for p in pairs if isinstance(p["human_hr"], bool)]
    it_pairs = [(p["human_interview"], p["model_interview"]) for p in pairs
                if isinstance(p["human_interview"], bool)]
    return {
        "n": n,
        "mae": sm.mae(score_pairs),
        "mae_ci95": sm.bootstrap_ci(score_pairs, sm.mae),
        "rmse": sm.rmse(score_pairs),
        "pearson": sm.pearson(hs, ms),
        "spearman": sm.spearman(hs, ms),
        "kappa_verdict": kappa,
        # QWK 只对整数档有意义：人工半档分四舍五入到整数再算。
        "qwk": sm.quadratic_weighted_kappa([round(v) for v in hs], ms),
        "within5": sm.agreement_within(score_pairs, 5),
        "within10": sm.agreement_within(score_pairs, 10),
        "spread": sm.diff_distribution([m - h for h, m in score_pairs]),
        "gate_hr_agreement": (sum(1 for a, b in hr_pairs if a == b) / len(hr_pairs)) if hr_pairs else None,
        "gate_interview_agreement": (
            sum(1 for a, b in it_pairs if a == b) / len(it_pairs)) if it_pairs else None,
    }


def readiness(dataset: dict, annotations: list[dict], samples: list[dict],
              pairs: list[dict], allow_heldout: bool) -> dict:
    """数据集与标注是否达到「可以出数字」的门槛（门槛来自 config/rubric.yaml）。

    这里刻意把「数据够不够」和「结果好不好」分开：数据不够时连数字都不该出现，
    否则很容易拿 3 份样本的 MAE 去对外说。
    """
    targets = dataset.get("targets") or {}
    dataset_target = int(rubric.num("eval.dataset_target"))
    heldout_target = int(rubric.num("eval.heldout_target"))
    all_samples = [s for s in dataset.get("samples") or [] if s.get("sample_id")]
    heldout_n = sum(1 for s in all_samples if s.get("split") == "heldout")
    reviewers = sorted({str(a.get("reviewer_id")) for a in annotations if a.get("reviewer_id")})
    # 标注文件是「一个标注者一个文件、内含多份样本」，所以覆盖数要展开 samples 再数。
    annotated = {
        sid
        for a in annotations
        for sid, item in (a.get("samples") or {}).items()
        if isinstance(item, dict) and isinstance(item.get("dimensions"), dict)
    }

    reasons: list[str] = []
    if len(all_samples) < dataset_target:
        reasons.append(f"数据集 {len(all_samples)} 份 < 目标 {dataset_target} 份")
    if (targets.get("heldout_min") or heldout_target) and heldout_n < heldout_target:
        reasons.append(f"heldout {heldout_n} 份 < 目标 {heldout_target} 份")
    if len(reviewers) < 2:
        reasons.append(f"独立标注者 {len(reviewers)} 人 < 2 人（双人盲评是硬要求）")
    if len(annotated) < dataset_target:
        reasons.append(f"已标注样本 {len(annotated)} 份 < 目标 {dataset_target} 份")
    if not allow_heldout:
        reasons.append("heldout 尚未做最终验收（未加 --allow-heldout）")

    return {
        "ready": not reasons,
        "reasons": reasons,
        "dataset_size": len(all_samples),
        "dataset_target": dataset_target,
        "heldout_size": heldout_n,
        "heldout_target": heldout_target,
        "reviewers": reviewers,
        "annotated_samples": len(annotated),
        "evaluated_samples": len(pairs),
        "thresholds": {
            "mae_max": rubric.num("eval.mae_max"),
            "kappa_min": rubric.num("eval.kappa_min"),
            "repeat_spread_max": rubric.num("eval.repeat_spread_max"),
        },
    }


def _fmt(value, digits: int = 2) -> str:
    if value is None:
        return "—"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (int, float)):
        return f"{value:.{digits}f}"
    return str(value)


def render_report(metrics: dict, ready: dict, pairs: list[dict], notes: list[str],
                  args: dict) -> str:
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    status = "READY" if ready["ready"] else "NOT_READY"
    lines = [
        "# 基准报告（bench/report.md）",
        "",
        f"- 生成时间：{stamp}",
        f"- 数据集：`{args.get('dataset')}`（切分：{args.get('split')}）",
        f"- 标注：`{args.get('annotations')}`",
        f"- 机器分来源：{args.get('predictor')}",
        f"- 评测样本数：{ready['evaluated_samples']}",
        "",
        f"## 状态：{status}",
        "",
    ]
    if ready["ready"]:
        lines += [
            "数据集与标注达到门槛，下面的数字可以对外使用。",
            "",
        ]
    else:
        lines += [
            "**未就绪，下面的数字不代表准确度，不得对外引用。**",
            "",
            "| 未达标项 |",
            "|---|",
        ]
        lines += [f"| {r} |" for r in ready["reasons"]]
        lines.append("")

    m = metrics
    lines += [
        "## 指标",
        "",
        "| 指标 | 值 | 口径 |",
        "|---|---|---|",
        f"| n | {m['n']} | 有人工共识分且拿到机器分的样本数 |",
        f"| MAE | {_fmt(m['mae'])} | 与共识分的平均绝对误差，越小越好 |",
        f"| MAE 95% CI | {_fmt_ci(m['mae_ci95'])} | 自助法 2000 次重采样 |",
        f"| RMSE | {_fmt(m['rmse'])} | 对高分误差更敏感 |",
        f"| Pearson | {_fmt(m['pearson'])} | 线性相关 |",
        f"| Spearman | {_fmt(m['spearman'])} | 秩相关（只看排序） |",
        f"| Cohen's κ（四档） | {_fmt(m['kappa_verdict'])} | 结论分档一致性 |",
        f"| QWK（0~100） | {_fmt(m['qwk'])} | 二次加权，差异按平方惩罚 |",
        f"| ±5 分内 | {_fmt_ratio(m['within5'])} | 与共识分差距 ≤5 的比例 |",
        f"| ±10 分内 | {_fmt_ratio(m['within10'])} | 与共识分差距 ≤10 的比例 |",
        f"| 初筛一致率 | {_fmt_ratio(m['gate_hr_agreement'])} | 第一关过/不过是/否 一致 |",
        f"| 面试一致率 | {_fmt_ratio(m['gate_interview_agreement'])} | 第二关过/不过是/否 一致 |",
        "",
        "> `spread`（同份重复跑的极差）不是准确度，见 `docs/人工评测方案.md` 的「稳定性」。",
        "",
    ]

    if pairs:
        lines += [
            "## 逐份",
            "",
            "| 样本 | 切分 | 人工共识 | 机器分 | 差 | 人工结论 | 机器结论 |",
            "|---|---|---|---|---|---|---|",
        ]
        for p in sorted(pairs, key=lambda x: x["sample_id"]):
            lines.append(
                f"| {p['sample_id']} | {p['split']} | {p['human_score']} | "
                f"{p['model_score']} | {(p['model_score'] - p['human_score']):+.1f} | "
                f"{p.get('human_verdict') or '—'} | {p.get('model_verdict') or '—'} |"
            )
        lines.append("")

    if notes:
        lines += ["## 凑不齐的样本（不是错误，但要知道为什么）", ""]
        lines += [f"- {n}" for n in notes]
        lines.append("")

    lines += [
        "## 口径与禁忌",
        "",
        "- **旧 MAE=8.0 不得继承**：那来自 8 份单人非盲 gold，且权重是先看过这些样本才定的。",
        "- 本报告只在 status=READY 时才能引用为「准确度」；NOT_READY 时最多说「工具就绪」。",
        "- heldout 只用于最终验收；调 prompt、改阈值、挑权重都不许看它的标签。",
        "- 复现：`python scripts/benchmark.py --predictor local --split development`。",
        "",
    ]
    return "\n".join(lines)


def _fmt_ci(ci) -> str:
    if not ci:
        return "—"
    lo, hi = ci
    return f"{lo:.2f} ~ {hi:.2f}"


def _fmt_ratio(value) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def run(dataset_path, annotations_path, predictor: str, split: str,
        allow_heldout: bool) -> dict:
    dataset = human_eval.load_dataset(dataset_path)
    problems = human_eval.validate_dataset(dataset)
    if problems:
        raise SystemExit("数据集不合格：\n  " + "\n  ".join(problems))

    annotations: list[dict] = []
    ann_dir = Path(annotations_path)
    if ann_dir.exists():
        annotations, rejected = human_eval.load_annotations(ann_dir, dataset)
        if rejected:
            print(f"注意：{len(rejected)} 份标注被拒收（格式/越界），不计入基准。", file=sys.stderr)

    samples = select_samples(dataset, split=split, allow_heldout=allow_heldout)
    pairs, notes = build_pairs(samples, annotations, predictor)
    metrics = compute_metrics(pairs)
    ready = readiness(dataset, annotations, samples, pairs, allow_heldout)
    return {
        "metrics": metrics,
        "readiness": ready,
        "pairs": pairs,
        "notes": notes,
        "events": [],
    }


def state(dataset_path=DEFAULT_DATASET, annotations_path=DEFAULT_ANNOTATIONS,
          predictor: str = "local") -> dict:
    """给网站 / API 用的「当前可信度状态」。

    与 run() 的区别：**永不抛异常、永不读 heldout**，数据不够也照样返回，
    让页面能老老实实写「未验证」，而不是什么都不显示（不显示会被读成「没问题」）。
    """
    dataset_target = int(rubric.num("eval.dataset_target"))
    heldout_target = int(rubric.num("eval.heldout_target"))
    out = {
        "ready": False,
        "reasons": [],
        "counts": {
            "dataset_size": 0,
            "dataset_target": dataset_target,
            "heldout_size": 0,
            "heldout_target": heldout_target,
            "reviewers": 0,
            "annotated_samples": 0,
            "evaluated_samples": 0,
        },
        "thresholds": {
            "mae_max": rubric.num("eval.mae_max"),
            "kappa_min": rubric.num("eval.kappa_min"),
            "repeat_spread_max": rubric.num("eval.repeat_spread_max"),
        },
        "metrics": None,
        "checked_at": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    try:
        dataset = human_eval.load_dataset(resolve(dataset_path))
    except (OSError, ValueError) as exc:
        out["reasons"].append(f"数据集读不出来：{exc}")
        return out

    annotations: list[dict] = []
    ann_dir = Path(annotations_path)
    if ann_dir.exists():
        try:
            annotations, _rejected = human_eval.load_annotations(ann_dir, dataset)
        except (OSError, ValueError):
            annotations = []

    samples = select_samples(dataset, split="development")  # 不传 allow_heldout，从代码上锁死
    pairs, _notes = build_pairs(samples, annotations, predictor)
    ready = readiness(dataset, annotations, samples, pairs, allow_heldout=False)
    out["reasons"] = ready["reasons"]
    out["counts"].update({
        "dataset_size": ready["dataset_size"],
        "heldout_size": ready["heldout_size"],
        "reviewers": len(ready["reviewers"]),
        "annotated_samples": ready["annotated_samples"],
        "evaluated_samples": ready["evaluated_samples"],
    })
    out["metrics"] = compute_metrics(pairs)
    out["ready"] = ready["ready"]
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="v2.0 基准：算准确度，数据不够就说不够")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    parser.add_argument("--predictor", choices=sorted(PREDICTORS), default="local")
    parser.add_argument("--split", choices=SPLITS, default="development")
    parser.add_argument("--allow-heldout", action="store_true",
                        help="允许读取 heldout（只在最终验收时用）")
    parser.add_argument("--out", default=str(DEFAULT_OUT_MD))
    parser.add_argument("--json", dest="json_out", default=str(DEFAULT_OUT_JSON))
    parser.add_argument("--require-ready", action="store_true",
                        help="未就绪时退出码 2（给 CI/发布脚本用）")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        result = run(args.dataset, args.annotations, args.predictor, args.split,
                     args.allow_heldout)
    except HeldoutLocked as exc:
        print(f"[heldout 已锁定] {exc}", file=sys.stderr)
        return 2

    report = render_report(
        result["metrics"], result["readiness"], result["pairs"], result["notes"],
        {"dataset": args.dataset, "annotations": args.annotations,
         "predictor": args.predictor, "split": args.split},
    )
    out_md = resolve(args.out)
    out_md.parent.mkdir(parents=True, exist_ok=True)
    out_md.write_text(report, encoding="utf-8", newline="\n")

    payload = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "config": {"dataset": args.dataset, "annotations": args.annotations,
                   "predictor": args.predictor, "split": args.split,
                   "allow_heldout": args.allow_heldout},
        "readiness": result["readiness"],
        "metrics": result["metrics"],
        "pairs": result["pairs"],
        "notes": result["notes"],
    }
    out_json = resolve(args.json_out)
    out_json.parent.mkdir(parents=True, exist_ok=True)
    out_json.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")

    if not args.quiet:
        print(f"状态：{'READY' if result['readiness']['ready'] else 'NOT_READY'}")
        print(f"样本：{result['metrics']['n']}   MAE：{_fmt(result['metrics']['mae'])}")
        if not result["readiness"]["ready"]:
            for reason in result["readiness"]["reasons"]:
                print(f"  · {reason}")
        print(f"报告：{out_md}")
        print(f"数据：{out_json}")

    if args.require_ready and not result["readiness"]["ready"]:
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
