"""人工准确度评测工具（阶段 D）。

设计原则：没有独立人工标注，就不输出“准确率”。
本工具只计算有数据支持的指标，并把未采集、样本不足和不可比项显式列出。

常用：
    python scripts/human_eval.py inventory --dataset samples/eval/dataset-v1.json
    python scripts/human_eval.py init --dataset samples/eval/dataset-v1.json --reviewer R01
    python scripts/human_eval.py validate --dataset samples/eval/dataset-v1.json
    python scripts/human_eval.py report --dataset samples/eval/dataset-v1.json
    python scripts/human_eval.py report --dataset ... --annotations samples/eval/annotations \
        --audits samples/eval/audits --runs-dir samples/eval/stability \
        --out samples/eval/reports/report.md --json-out samples/eval/reports/report.json

评测数据约定：
  - 评分标注：samples/eval/annotations/<reviewer>.json
  - 模型输出审阅：samples/eval/audits/<reviewer>.json
  - 重复评审：samples/eval/stability/<sample_id>.run<N>.json
  - OCR 对照：samples/eval/ocr-pairs.json
"""
from __future__ import annotations

import argparse
import json
import math
import random
import statistics
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

import check_output  # noqa: E402
import rubric  # noqa: E402

DIMS = ["role_fit", "impact", "evidence_quality", "structure", "competitiveness"]
VERDICTS = ["strong_pass", "pass", "borderline", "reject"]
DEFAULT_DATASET = ROOT / "samples" / "eval" / "dataset-v1.json"
DEFAULT_ANNOTATIONS = ROOT / "samples" / "eval" / "annotations"
DEFAULT_AUDITS = ROOT / "samples" / "eval" / "audits"
DEFAULT_RUNS = ROOT / "samples" / "eval" / "stability"
ANNOTATION_SCHEMA = ROOT / "samples" / "eval" / "annotation-schema.json"


def _resolve(path: str | Path) -> Path:
    p = Path(path)
    return p if p.is_absolute() else ROOT / p


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def load_dataset(path: str | Path) -> dict:
    data = _load_json(_resolve(path))
    data.setdefault("samples", [])
    return data


def _sample_map(dataset: dict) -> dict[str, dict]:
    return {s["sample_id"]: s for s in dataset.get("samples") or [] if s.get("sample_id")}


def validate_dataset(dataset: dict) -> list[str]:
    problems: list[str] = []
    if dataset.get("schema_version") != "human-eval-1":
        problems.append("dataset schema_version 必须是 human-eval-1")
    if not dataset.get("dataset_id"):
        problems.append("缺少 dataset_id")
    samples = dataset.get("samples")
    if not isinstance(samples, list) or not samples:
        return problems + ["samples 不能为空"]
    seen: set[str] = set()
    groups: dict[str, set[str]] = defaultdict(set)
    for i, s in enumerate(samples):
        sid = s.get("sample_id")
        if not sid:
            problems.append(f"samples[{i}] 缺少 sample_id")
            continue
        if sid in seen:
            problems.append(f"sample_id 重复：{sid}")
        seen.add(sid)
        if not s.get("job_target"):
            problems.append(f"{sid} 缺少 job_target")
        if s.get("split") not in {"development", "heldout"}:
            problems.append(f"{sid} split 必须是 development/heldout")
        text = _resolve(s.get("text_file") or "")
        if not s.get("text_file") or not text.exists():
            problems.append(f"{sid} 简历文本不存在：{s.get('text_file')}")
        group = str(s.get("source_group") or "")
        if not group:
            problems.append(f"{sid} 缺少 source_group")
        else:
            groups[group].add(s.get("split") or "")
    for group, splits in groups.items():
        if "development" in splits and "heldout" in splits:
            problems.append(f"同一来源组跨开发/留出集：{group}")
    return problems


def _weighted_score(dims: dict) -> int | None:
    try:
        vals = {k: int(dims[k]) for k in DIMS}
    except (KeyError, TypeError, ValueError):
        return None
    if any(v < 1 or v > 5 for v in vals.values()):
        return None
    return round(sum(vals[k] * w for k, w in check_output.WEIGHTS.items()) * 20)


def _valid_annotation(ann: dict, dataset: dict) -> list[str]:
    problems: list[str] = []
    if ann.get("schema_version") != "human-eval-1":
        problems.append("schema_version 必须是 human-eval-1")
    if ann.get("dataset_id") != dataset.get("dataset_id"):
        problems.append("dataset_id 与数据集不一致")
    if ann.get("reviewer_type") != "independent_human":
        problems.append("reviewer_type 必须是 independent_human")
    if ann.get("blind_to_model") is not True:
        problems.append("blind_to_model 必须为 true")
    if len(str(ann.get("completed_at") or "")) < 10:
        problems.append("completed_at 缺少年月日")
    if not ann.get("reviewer_id"):
        problems.append("缺少 reviewer_id")
    samples = ann.get("samples")
    if not isinstance(samples, dict) or not samples:
        problems.append("没有 samples 标注")
        return problems
    known = set(_sample_map(dataset))
    for sid, item in samples.items():
        if sid not in known:
            problems.append(f"{sid}: 不在数据集中")
            continue
        if not isinstance(item, dict):
            problems.append(f"{sid}: 标注不是对象")
            continue
        if not (item.get("blind_id") or "").strip():
            problems.append(f"{sid}: 缺少 blind_id")
        if not (item.get("job_target") or "").strip():
            problems.append(f"{sid}: 缺少 job_target")
        dims = item.get("dimensions")
        if not isinstance(dims, dict) or _weighted_score(dims) is None:
            problems.append(f"{sid}: 五个维度分必须都是 1~5 的整数")
        if item.get("verdict") not in VERDICTS:
            problems.append(f"{sid}: verdict 非法")
        if not isinstance(item.get("gate_hr"), bool):
            problems.append(f"{sid}: gate_hr 必须是布尔值")
        if not isinstance(item.get("gate_interview"), bool):
            problems.append(f"{sid}: gate_interview 必须是布尔值")
        conf = item.get("confidence")
        if not isinstance(conf, int) or not 1 <= conf <= 5:
            problems.append(f"{sid}: confidence 必须是 1~5")
        issues = item.get("issues")
        if not isinstance(issues, list):
            problems.append(f"{sid}: issues 必须是数组")
        else:
            for i, issue in enumerate(issues):
                if not isinstance(issue, dict) or issue.get("dimension") not in DIMS:
                    problems.append(f"{sid}.issues[{i}]: dimension 非法")
                    continue
                if not (issue.get("evidence") or "").strip():
                    problems.append(f"{sid}.issues[{i}]: 缺少证据")
                if issue.get("severity") not in {"high", "medium", "low"}:
                    problems.append(f"{sid}.issues[{i}]: severity 非法")
    return problems


def load_annotations(path: str | Path, dataset: dict) -> tuple[list[dict], list[dict]]:
    root = _resolve(path)
    if not root.exists():
        return [], [{"file": str(root), "error": "目录不存在"}]
    valid: list[dict] = []
    rejected: list[dict] = []
    seen_reviewers: set[str] = set()
    for f in sorted(root.glob("*.json")):
        try:
            ann = _load_json(f)
        except Exception as exc:  # noqa: BLE001
            rejected.append({"file": f.name, "error": f"JSON 解析失败：{exc}"})
            continue
        problems = _valid_annotation(ann, dataset)
        rid = ann.get("reviewer_id")
        if rid in seen_reviewers:
            problems.append(f"reviewer_id 重复：{rid}")
        if problems:
            rejected.append({"file": f.name, "reviewer_id": rid, "errors": problems})
        else:
            valid.append(ann)
            seen_reviewers.add(str(rid))
    return valid, rejected


def _rank(values: list[float]) -> list[float]:
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


def pearson(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2 or len(xs) != len(ys):
        return None
    mx, my = sum(xs) / len(xs), sum(ys) / len(ys)
    dx = [x - mx for x in xs]
    dy = [y - my for y in ys]
    den = math.sqrt(sum(x * x for x in dx) * sum(y * y for y in dy))
    if den == 0:
        return None
    return sum(a * b for a, b in zip(dx, dy)) / den


def spearman(xs: list[float], ys: list[float]) -> float | None:
    if len(xs) < 2:
        return None
    return pearson(_rank(xs), _rank(ys))


def quadratic_weighted_kappa(pairs: list[tuple[int, int]]) -> float | None:
    if not pairs:
        return None
    labels = list(range(1, 6))
    index = {v: i for i, v in enumerate(labels)}
    n = len(pairs)
    obs = [[0] * len(labels) for _ in labels]
    for a, b in pairs:
        if a in index and b in index:
            obs[index[a]][index[b]] += 1
    row = [sum(r) for r in obs]
    col = [sum(obs[i][j] for i in range(len(labels))) for j in range(len(labels))]
    obs_disagree = 0.0
    expected_disagree = 0.0
    max_w = (len(labels) - 1) ** 2
    for i in range(len(labels)):
        for j in range(len(labels)):
            w = ((i - j) ** 2) / max_w
            obs_disagree += w * obs[i][j]
            expected_disagree += w * row[i] * col[j] / n
    if expected_disagree == 0:
        return None
    return 1 - obs_disagree / expected_disagree


def _mostly_boolean(values: list[bool]) -> bool | None:
    if not values:
        return None
    c = Counter(values)
    if len(c) == 1:
        return values[0]
    if c[True] == c[False]:
        return None
    return c.most_common(1)[0][0]


def _mostly_verdict(values: list[str]) -> str | None:
    if not values:
        return None
    c = Counter(values)
    if len(c) == 1:
        return values[0]
    if len(c.most_common(2)) > 1 and c.most_common(2)[0][1] == c.most_common(2)[1][1]:
        return None
    return c.most_common(1)[0][0]


def consensus(annotations: list[dict]) -> dict[str, dict]:
    by_sample: dict[str, list[dict]] = defaultdict(list)
    for ann in annotations:
        for sid, item in ann.get("samples", {}).items():
            by_sample[sid].append(item)
    out: dict[str, dict] = {}
    for sid, items in by_sample.items():
        scores = [s for s in (_weighted_score(i.get("dimensions") or {}) for i in items) if s is not None]
        out[sid] = {
            "reviewers": len(items),
            "scores": scores,
            "score": round(sum(scores) / len(scores), 1) if scores else None,
            "verdict": _mostly_verdict([i.get("verdict") for i in items if i.get("verdict")]),
            "gate_hr": _mostly_boolean([i.get("gate_hr") for i in items if isinstance(i.get("gate_hr"), bool)]),
            "gate_interview": _mostly_boolean(
                [i.get("gate_interview") for i in items if isinstance(i.get("gate_interview"), bool)]
            ),
        }
    return out


def agreement_metrics(annotations: list[dict]) -> dict:
    if len(annotations) < 2:
        return {
            "reviewer_pairs": 0,
            "samples_compared": 0,
            "dimension_qwk": {k: None for k in DIMS},
            "overall_mae": None,
            "overall_spearman": None,
            "verdict_exact_rate": None,
        }
    pair_data = []
    for i in range(len(annotations)):
        for j in range(i + 1, len(annotations)):
            a = annotations[i].get("samples", {})
            b = annotations[j].get("samples", {})
            common = sorted(set(a) & set(b))
            if common:
                pair_data.append((a, b, common))
    dim_pairs: dict[str, list[tuple[int, int]]] = {k: [] for k in DIMS}
    score_pairs: list[tuple[float, float]] = []
    verdict_pairs: list[tuple[str, str]] = []
    samples_compared: set[str] = set()
    for a, b, common in pair_data:
        for sid in common:
            samples_compared.add(sid)
            for k in DIMS:
                av = (a[sid].get("dimensions") or {}).get(k)
                bv = (b[sid].get("dimensions") or {}).get(k)
                if isinstance(av, int) and isinstance(bv, int):
                    dim_pairs[k].append((av, bv))
            avs = _weighted_score(a[sid].get("dimensions") or {})
            bvs = _weighted_score(b[sid].get("dimensions") or {})
            if avs is not None and bvs is not None:
                score_pairs.append((avs, bvs))
            if a[sid].get("verdict") and b[sid].get("verdict"):
                verdict_pairs.append((a[sid]["verdict"], b[sid]["verdict"]))
    return {
        "reviewer_pairs": len(pair_data),
        "samples_compared": len(samples_compared),
        "dimension_qwk": {
            k: (round(q, 3) if q is not None else None)
            for k, q in ((k, quadratic_weighted_kappa(v)) for k, v in dim_pairs.items())
        },
        "overall_mae": (
            round(sum(abs(a - b) for a, b in score_pairs) / len(score_pairs), 1)
            if score_pairs else None
        ),
        "overall_spearman": (
            round(s, 3) if (s := spearman([a for a, _ in score_pairs], [b for _, b in score_pairs])) is not None else None
        ),
        "verdict_exact_rate": (
            round(sum(1 for a, b in verdict_pairs if a == b) / len(verdict_pairs), 3)
            if verdict_pairs else None
        ),
    }


def _model_score(path: Path) -> dict | None:
    if not path.exists():
        return None
    try:
        data = _load_json(path)
    except Exception:  # noqa: BLE001
        return None
    if isinstance(data, dict) and isinstance(data.get("review"), dict):
        data = data["review"]
    return data if isinstance(data, dict) else None


def score_metrics(dataset: dict, annotations: list[dict], model_overrides: dict[str, dict] | None = None) -> dict:
    human = consensus(annotations)
    pairs = []
    gate_pairs = {"hr": [], "interview": []}
    verdict_conf = {h: {m: 0 for m in VERDICTS} for h in VERDICTS}
    missing_outputs = []
    for sample in dataset.get("samples") or []:
        sid = sample.get("sample_id")
        if sid not in human or human[sid]["score"] is None:
            continue
        model = (model_overrides or {}).get(sid)
        if model is None:
            model = _model_score(_resolve(sample.get("model_output") or ""))
        if not model or not isinstance(model.get("overall_score"), int):
            missing_outputs.append(sid)
            continue
        h = human[sid]
        pairs.append({
            "sample_id": sid,
            "human_score": h["score"],
            "model_score": model["overall_score"],
            "human_verdict": h["verdict"],
            "model_verdict": model.get("verdict"),
            "human_hr": h["gate_hr"],
            "model_hr": bool((model.get("gate_hr") or {}).get("pass")),
            "human_interview": h["gate_interview"],
            "model_interview": bool((model.get("gate_interview") or {}).get("pass")),
        })
        if h["verdict"] in verdict_conf and model.get("verdict") in VERDICTS:
            verdict_conf[h["verdict"]][model["verdict"]] += 1
        if isinstance(h["gate_hr"], bool):
            gate_pairs["hr"].append((h["gate_hr"], bool((model.get("gate_hr") or {}).get("pass"))))
        if isinstance(h["gate_interview"], bool):
            gate_pairs["interview"].append(
                (h["gate_interview"], bool((model.get("gate_interview") or {}).get("pass")))
            )

    if not pairs:
        return {
            "n": 0,
            "pairs": [],
            "mae": None,
            "rmse": None,
            "spearman": None,
            "rank_concordance": None,
            "verdict_exact_rate": None,
            "verdict_confusion": verdict_conf,
            "gate_metrics": {},
            "mae_ci95": None,
            "missing_outputs": missing_outputs,
        }

    errors = [p["model_score"] - p["human_score"] for p in pairs]
    mae = sum(abs(e) for e in errors) / len(errors)
    rmse = math.sqrt(sum(e * e for e in errors) / len(errors))
    hs = [p["human_score"] for p in pairs]
    ms = [p["model_score"] for p in pairs]
    rank_total = rank_ok = 0
    for i in range(len(pairs)):
        for j in range(i + 1, len(pairs)):
            dh = hs[i] - hs[j]
            dm = ms[i] - ms[j]
            if dh == 0:
                continue
            rank_total += 1
            if dm == 0:
                rank_ok += 0.5
            elif (dh > 0) == (dm > 0):
                rank_ok += 1
    verdict_pairs = [(p["human_verdict"], p["model_verdict"]) for p in pairs
                     if p["human_verdict"] in VERDICTS and p["model_verdict"] in VERDICTS]
    gate_metrics = {}
    for key, vals in gate_pairs.items():
        tp = sum(1 for h, m in vals if h and m)
        fp = sum(1 for h, m in vals if not h and m)
        fn = sum(1 for h, m in vals if h and not m)
        tn = sum(1 for h, m in vals if not h and not m)
        gate_metrics[key] = {
            "n": len(vals),
            "tp": tp, "fp": fp, "fn": fn, "tn": tn,
            "precision": round(tp / (tp + fp), 3) if tp + fp else None,
            "recall": round(tp / (tp + fn), 3) if tp + fn else None,
            "accuracy": round((tp + tn) / len(vals), 3) if vals else None,
        }

    rng = random.Random(42)
    boot = []
    for _ in range(1000):
        sample = [errors[rng.randrange(len(errors))] for _ in errors]
        boot.append(sum(abs(e) for e in sample) / len(sample))
    boot.sort()
    ci = (round(boot[24], 1), round(boot[-25], 1)) if len(errors) >= 3 else None

    return {
        "n": len(pairs),
        "pairs": pairs,
        "mae": round(mae, 1),
        "rmse": round(rmse, 1),
        "spearman": round(s, 3) if (s := spearman(hs, ms)) is not None else None,
        "rank_concordance": round(rank_ok / rank_total, 3) if rank_total else None,
        "verdict_exact_rate": (
            round(sum(1 for a, b in verdict_pairs if a == b) / len(verdict_pairs), 3)
            if verdict_pairs else None
        ),
        "verdict_confusion": verdict_conf,
        "gate_metrics": gate_metrics,
        "mae_ci95": list(ci) if ci else None,
        "missing_outputs": missing_outputs,
    }


def audit_metrics(path: str | Path, dataset: dict) -> dict:
    root = _resolve(path)
    if not root.exists():
        return {"files": 0, "issue_rates": {}, "rewrite_rates": {}, "n_issues": 0, "n_rewrites": 0}
    files = sorted(root.glob("*.json"))
    known = set(_sample_map(dataset))
    counters = {
        "supported_by_source": [0, 0], "job_relevant": [0, 0], "severity_appropriate": [0, 0],
        "actionable": [0, 0], "fact_preserved": [0, 0], "better_than_original": [0, 0],
        "usable_as_is": [0, 0],
    }
    total_issues = total_rewrites = 0
    valid_files = 0
    for f in files:
        try:
            data = _load_json(f)
        except Exception:  # noqa: BLE001
            continue
        if data.get("schema_version") != "human-eval-audit-1":
            continue
        valid_files += 1
        for sid, item in (data.get("outputs") or {}).items():
            if sid not in known or not isinstance(item, dict):
                continue
            for issue in item.get("issues") or []:
                if not isinstance(issue, dict):
                    continue
                total_issues += 1
                for key in ("supported_by_source", "job_relevant", "severity_appropriate", "actionable"):
                    if isinstance(issue.get(key), bool):
                        counters[key][1] += 1
                        counters[key][0] += int(issue[key])
            for rw in item.get("rewrites") or []:
                if not isinstance(rw, dict):
                    continue
                total_rewrites += 1
                for key in ("fact_preserved", "better_than_original", "usable_as_is"):
                    if isinstance(rw.get(key), bool):
                        counters[key][1] += 1
                        counters[key][0] += int(rw[key])
    def rates(keys):
        out = {}
        for k in keys:
            y, n = counters[k]
            out[k] = {"yes": y, "n": n, "rate": round(y / n, 3) if n else None}
        return out
    return {
        "files": valid_files,
        "issue_rates": rates(("supported_by_source", "job_relevant", "severity_appropriate", "actionable")),
        "rewrite_rates": rates(("fact_preserved", "better_than_original", "usable_as_is")),
        "n_issues": total_issues,
        "n_rewrites": total_rewrites,
    }


def stability_metrics(path: str | Path, dataset: dict) -> dict:
    root = _resolve(path)
    if not root.exists():
        return {"samples": 0, "runs": 0, "score_std_mean": None, "verdict_flip_rate": None}
    groups: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(root.glob("*.json")):
        sid = f.name.split(".", 1)[0]
        if sid not in _sample_map(dataset):
            continue
        try:
            data = _load_json(f)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(data.get("review"), dict):
            data = data["review"]
        if isinstance(data, dict) and isinstance(data.get("overall_score"), int):
            groups[sid].append(data)
    if not groups:
        return {"samples": 0, "runs": 0, "score_std_mean": None, "verdict_flip_rate": None}
    stdevs, flips, issue_jaccards = [], [], []
    hr_flips, iv_flips = [], []
    for runs in groups.values():
        scores = [r["overall_score"] for r in runs]
        if len(scores) > 1:
            stdevs.append(statistics.pstdev(scores))
            flips.append(len({r.get("verdict") for r in runs}) > 1)
            hr_flips.append(len({bool((r.get("gate_hr") or {}).get("pass")) for r in runs}) > 1)
            iv_flips.append(len({bool((r.get("gate_interview") or {}).get("pass")) for r in runs}) > 1)
        sets = []
        for r in runs:
            sets.append({
                ((i.get("dimension") or ""), check_output.canon((i.get("evidence") or "")))
                for i in (r.get("issues") or []) if isinstance(i, dict)
            })
        for i in range(len(sets)):
            for j in range(i + 1, len(sets)):
                union = sets[i] | sets[j]
                if union:
                    issue_jaccards.append(len(sets[i] & sets[j]) / len(union))
    return {
        "samples": len(groups),
        "runs": sum(len(v) for v in groups.values()),
        "score_std_mean": round(statistics.mean(stdevs), 2) if stdevs else None,
        "score_range_max": max(
            (max(r["overall_score"] for r in runs) - min(r["overall_score"] for r in runs))
            for runs in groups.values()
        ),
        "verdict_flip_rate": round(sum(flips) / len(flips), 3) if flips else None,
        "gate_hr_flip_rate": round(sum(hr_flips) / len(hr_flips), 3) if hr_flips else None,
        "gate_interview_flip_rate": round(sum(iv_flips) / len(iv_flips), 3) if iv_flips else None,
        "issue_jaccard_mean": round(statistics.mean(issue_jaccards), 3) if issue_jaccards else None,
    }


def edit_distance(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(cur[-1] + 1, prev[j] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def ocr_metrics(path: str | Path) -> dict:
    p = _resolve(path)
    if not p.exists():
        return {"pairs": 0, "cer": None, "digit_error_rate": None}
    data = _load_json(p)
    rows = []
    for pair in data.get("pairs") or []:
        ref = pair.get("reference") or ""
        got = pair.get("recognized") or ""
        if pair.get("reference_file"):
            ref = _resolve(pair["reference_file"]).read_text(encoding="utf-8")
        if pair.get("recognized_file"):
            got = _resolve(pair["recognized_file"]).read_text(encoding="utf-8")
        ref = ref.strip()
        got = got.strip()
        if not ref:
            continue
        dist = edit_distance(ref, got)
        rows.append({
            "sample_id": pair.get("sample_id"),
            "cer": dist / len(ref),
            "digit_error_rate": (
                edit_distance("".join(c for c in ref if c.isdigit()),
                              "".join(c for c in got if c.isdigit()))
                / max(1, sum(c.isdigit() for c in ref))
            ),
        })
    return {
        "pairs": len(rows),
        "cer": round(sum(r["cer"] for r in rows) / len(rows), 4) if rows else None,
        "digit_error_rate": round(sum(r["digit_error_rate"] for r in rows) / len(rows), 4) if rows else None,
        "details": rows,
    }


def readiness(dataset: dict, annotations: list[dict], rejected: list[dict],
              model_metrics: dict, dataset_problems: list[str] | None = None) -> dict:
    samples = dataset.get("samples") or []
    targets = dataset.get("targets") or {}
    valid_ids = sorted({str(a.get("reviewer_id")) for a in annotations if a.get("reviewer_id")})
    reviewed = Counter()
    for ann in annotations:
        for sid in (ann.get("samples") or {}):
            reviewed[sid] += 1
    double = sum(1 for v in reviewed.values() if v >= 2)
    heldout = [s for s in samples if s.get("split") == "heldout"]
    development = [s for s in samples if s.get("split") == "development"]
    roles = sorted({(s.get("stratum") or {}).get("role_family") for s in samples
                    if (s.get("stratum") or {}).get("role_family")})
    target_total = int(targets.get("total_samples") or 60)
    target_heldout = int(targets.get("heldout_min") or 18)
    blockers = list(dataset_problems or [])
    if len(samples) < target_total:
        blockers.append(f"样本数 {len(samples)}/{target_total}")
    if len(valid_ids) < 2:
        blockers.append(f"有效独立标注者 {len(valid_ids)}/2")
    if double < target_total:
        blockers.append(f"双人标注样本 {double}/{target_total}")
    if len(heldout) < target_heldout:
        blockers.append(f"留出集 {len(heldout)}/{target_heldout}")
    if model_metrics.get("n", 0) == 0:
        blockers.append("没有可比的人工-模型配对（需独立标注 + 模型输出）")
    return {
        "status": "ready" if not blockers else "not_ready",
        "samples": len(samples),
        "target_samples": target_total,
        "development_samples": len(development),
        "heldout_samples": len(heldout),
        "target_heldout": target_heldout,
        "role_families": roles,
        "valid_reviewers": len(valid_ids),
        "reviewer_ids": valid_ids,
        "samples_with_two_reviews": double,
        "rejected_annotation_files": len(rejected),
        "compared_model_outputs": model_metrics.get("n", 0),
        "dataset_problems": list(dataset_problems or []),
        "blockers": blockers,
    }


def legacy_proxy(dataset: dict, gold_path: str | Path) -> dict:
    """旧 gold 是单人非盲标注，只能作为探索性代理，不算准确度验证。"""
    p = _resolve(gold_path)
    if not p.exists():
        return {"n": 0}
    gold = _load_json(p)
    pairs = []
    for sample in dataset.get("samples") or []:
        stem = Path(sample.get("text_file") or "").stem
        g = gold.get(stem) or {}
        if not isinstance(g.get("human_score"), int):
            continue
        model = _model_score(_resolve(sample.get("model_output") or ""))
        if not model or not isinstance(model.get("overall_score"), int):
            continue
        pairs.append({
            "sample_id": sample.get("sample_id"),
            "human_score": g["human_score"],
            "model_score": model["overall_score"],
            "reviewer": g.get("reviewer"),
        })
    if not pairs:
        return {"n": 0}
    hs = [p["human_score"] for p in pairs]
    ms = [p["model_score"] for p in pairs]
    return {
        "n": len(pairs),
        "mae": round(sum(abs(m - h) for h, m in zip(hs, ms)) / len(pairs), 1),
        "spearman": round(s, 3) if (s := spearman(hs, ms)) is not None else None,
        "pairs": pairs,
        "warning": "legacy 单人非盲标注，只用于发现明显偏差，不构成准确度验证。",
    }


def build_report(
    dataset_path: str | Path,
    annotations_path: str | Path,
    audits_path: str | Path,
    runs_path: str | Path,
    ocr_path: str | Path,
    legacy_gold: str | Path | None = None,
) -> tuple[dict, str]:
    dataset = load_dataset(dataset_path)
    dataset_problems = validate_dataset(dataset)
    annotations, rejected = _load_annotations_dir(annotations_path, dataset)
    model_metrics = score_metrics(dataset, annotations)
    agree = agreement_metrics(annotations)
    audits = audit_metrics(audits_path, dataset)
    stability = stability_metrics(runs_path, dataset)
    ocr = ocr_metrics(ocr_path)
    ready = readiness(dataset, annotations, rejected, model_metrics, dataset_problems)
    legacy = legacy_proxy(dataset, legacy_gold) if legacy_gold else {"n": 0}

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "dataset": {
            "id": dataset.get("dataset_id"),
            "schema_version": dataset.get("schema_version"),
            "frozen": dataset.get("frozen"),
            "split_policy": dataset.get("split_policy"),
            "validation": dataset_problems,
        },
        "readiness": ready,
        "agreement": agree,
        "model_comparison": model_metrics,
        "model_audit": audits,
        "stability": stability,
        "ocr": ocr,
        "legacy_proxy": legacy,
        "rejected_annotations": rejected,
        "limitations": [
            "没有独立双人盲评时，不能声称模型评分准确。",
            "development 样本已参与开发，不能当作泛化效果。",
            "旧 gold 为单人非盲标注，只作探索性偏差提示。",
            "模型输出审阅不是评分盲评的替代品。",
        ],
    }

    L = [
        "# 简历评审助手 · 人工评测报告",
        "",
        f"生成时间：{result['generated_at']}",
        "",
        "## 结论",
        "",
        f"**评测状态：{ready['status']}**",
        "",
    ]
    if ready["status"] != "ready":
        L.append("当前数据不足以得出准确度结论。未满足项：")
        L += [f"- {b}" for b in ready["blockers"]]
    else:
        L.append("数据达到预设规模，可运行正式评测；仍需人工审核限制与适用边界。")
    L += [
        "",
        "## 数据规模",
        "",
        "| 项目 | 数值 |",
        "|---|---|",
        f"| 样本 | {ready['samples']} / {ready['target_samples']} |",
        f"| development | {ready['development_samples']} |",
        f"| heldout | {ready['heldout_samples']} / {ready['target_heldout']} |",
        f"| 有效独立标注者 | {ready['valid_reviewers']} |",
        f"| 双人标注样本 | {ready['samples_with_two_reviews']} |",
        f"| 可比模型输出 | {ready['compared_model_outputs']} |",
        f"| 角色分层 | {', '.join(ready['role_families'])} |",
    ]
    if dataset_problems:
        L += ["", "数据集完整性问题："]
        L += [f"- {x}" for x in dataset_problems]
    L += [
        "",
        "## 标注者一致性",
        "",
    ]
    if agree["reviewer_pairs"] == 0:
        L.append("尚无可计算的一致性：没有两份有效独立标注。")
    else:
        L += [
            f"比较样本：{agree['samples_compared']}；评审者对：{agree['reviewer_pairs']}",
            "",
            "| 维度 | 二次加权 Kappa |",
            "|---|---|",
        ]
        for k in DIMS:
            L.append(f"| {k} | {agree['dimension_qwk'].get(k)} |")
        L += ["", f"总分 MAE：{agree['overall_mae']}；Spearman：{agree['overall_spearman']}；档位完全一致率：{agree['verdict_exact_rate']}"]
    L += ["", "## 人工分 vs 模型分", ""]
    if model_metrics["n"] == 0:
        L.append("尚无可比样本：需要有效人工盲评且模型输出存在。")
    else:
        L += [
            f"n = {model_metrics['n']}",
            "",
            "| 指标 | 结果 |",
            "|---|---|",
            f"| MAE | {model_metrics['mae']} |",
            f"| RMSE | {model_metrics['rmse']} |",
            f"| Spearman | {model_metrics['spearman']} |",
            f"| 成对排序一致率 | {model_metrics['rank_concordance']} |",
            f"| 档位完全一致率 | {model_metrics['verdict_exact_rate']} |",
            f"| MAE 95% bootstrap CI | {model_metrics['mae_ci95']} |",
            "",
            "> 这些数值只有在独立双人盲评、留出集和足够样本下才能作为准确度证据。",
        ]
        gates = model_metrics.get("gate_metrics") or {}
        for key, label in (("hr", "初筛"), ("interview", "面试")):
            g = gates.get(key)
            if g and g.get("n"):
                L.append(
                    f"- {label}闸门：precision={g['precision']}，recall={g['recall']}，"
                    f"accuracy={g['accuracy']}（n={g['n']}）"
                )
        L += ["", "档位混淆矩阵（人工→模型）：", "", "```json",
              json.dumps(model_metrics.get("verdict_confusion") or {}, ensure_ascii=False, indent=2),
              "```"]
    L += ["", "## 模型意见与改写审阅", ""]
    if audits["files"] == 0:
        L.append("尚未采集模型输出审阅。")
    else:
        L += [f"审阅文件：{audits['files']}；意见 {audits['n_issues']} 条；改写 {audits['n_rewrites']} 条", ""]
        for group, label in ((audits["issue_rates"], "意见"), (audits["rewrite_rates"], "改写")):
            for k, v in group.items():
                L.append(f"- {label} `{k}`：{v['yes']}/{v['n']}（{v['rate']}）")
    L += ["", "## 重复评审稳定性", ""]
    if not stability.get("runs"):
        L.append("尚无重复运行数据；当前不能报告分数抖动、档位翻转率或意见稳定性。")
    else:
        L += [
            f"样本 {stability['samples']}，运行 {stability['runs']}",
            "",
            f"- 平均标准差：{stability.get('score_std_mean')}",
            f"- 最大分数极差：{stability.get('score_range_max')}",
            f"- 档位翻转率：{stability.get('verdict_flip_rate')}",
            f"- 初筛闸门翻转率：{stability.get('gate_hr_flip_rate')}",
            f"- 面试闸门翻转率：{stability.get('gate_interview_flip_rate')}",
            f"- 意见 Jaccard：{stability.get('issue_jaccard_mean')}",
        ]
    L += ["", "## OCR 误差", ""]
    if not ocr.get("pairs"):
        L.append("尚无 OCR 对照数据。")
    else:
        L += [f"样本 {ocr['pairs']}；CER（字符）{ocr['cer']}；数字错误率 {ocr['digit_error_rate']}"]
    if legacy.get("n"):
        L += [
            "",
            "## 旧版标注代理（不可当作准确度）",
            "",
            f"n={legacy['n']}；MAE={legacy['mae']}；Spearman={legacy['spearman']}。",
            "",
            f"> {legacy['warning']}",
        ]
    if rejected:
        L += ["", "## 被拒绝的标注文件", ""]
        for r in rejected:
            L.append(f"- {r.get('file')}：{r.get('errors') or r.get('error')}")
    L += ["", "## 限制", ""]
    L += [f"- {x}" for x in result["limitations"]]
    return result, "\n".join(L) + "\n"


def _init_annotation(dataset_path: str | Path, reviewer: str, out: str | Path) -> Path:
    dataset = load_dataset(dataset_path)
    payload = {
        "schema_version": "human-eval-1",
        "dataset_id": dataset.get("dataset_id"),
        "reviewer_id": reviewer,
        "reviewer_type": "independent_human",
        "blind_to_model": True,
        "conflicts_disclosed": "",
        "completed_at": "",
        "samples": {
            s["sample_id"]: {
                "blind_id": s.get("blind_id") or "",
                "job_target": s.get("job_target") or "",
                "jd": s.get("jd") or "",
                "dimensions": {k: None for k in DIMS},
                "verdict": "",
                "gate_hr": None,
                "gate_interview": None,
                "confidence": None,
                "issues": [],
                "notes": "",
            }
            for s in dataset.get("samples") or []
        },
    }
    p = _resolve(out)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return p


# ---------------- 盲评发放包 + 仲裁清单（AC-A3.*） ----------------
# 为什么需要它：`init` 只生成空白 json，但两个标注者拿到的东西如果顺序一样、
# 或者能看到模型给了几分，那就不叫盲评——κ 会虚高，MAE 会失真。
# 这里做三件事：① 只给简历正文和盲号，绝不给模型输出与分层标签；
# ② 每个标注者一套独立打乱顺序（同一个人可复现，两个人不同）；
# ③ 输出「发给标注者的手读稿」，与要回收的空白 json 一一对应。

BLIND_HANDOUT_DIR = ROOT / "samples" / "eval" / "handouts"


def _blind_order(dataset: dict, reviewer: str, seed: str | None = None) -> list[dict]:
    """按标注者确定性地打乱样本顺序。

    同一个 reviewer + 同一个 seed 必须每次都得到同样顺序（否则回收时对不上）；
    不同 reviewer 默认不同（用 reviewer 名参与种子），避免两人按同一顺序互相参照。
    """
    samples = list(dataset.get("samples") or [])
    key = seed if seed is not None else f"{dataset.get('dataset_id')}:{reviewer}"
    random.Random(key).shuffle(samples)
    return samples


def _read_resume_text(sample: dict) -> str | None:
    """取简历正文。只读 `text_file`，绝不碰 `model_output`。"""
    rel = sample.get("text_file") or ""
    if not rel:
        return None
    path = _resolve(rel)
    if not path.exists():
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def render_blind_markdown(dataset: dict, reviewer: str, order: list[dict],
                          skipped: list[str] | None = None) -> str:
    """给标注者看的手读稿：只有盲号、岗位和正文。"""
    L = [
        f"# 简历盲评手读稿 · {reviewer}",
        "",
        "请**只看简历本身**评分，不要猜作者是谁、也不要问别人给了几分。",
        "",
        "## 怎么填",
        "",
        "1. 打开同一目录下你自己的 `*.json`；",
        "2. 每个样本按盲号（`ANON-XXXX`）对应过去，给 5 个维度打 0~5 的整数分：",
        "   `role_fit`（岗位匹配）/ `impact`（成果影响）/ `evidence_quality`（证据可核验）/"
        "`structure`（结构可读）/ `competitiveness`（相对竞争力）；",
        "3. 再给 `verdict`（`strong_pass` / `pass` / `borderline` / `reject`）、"
        "`gate_hr` 与 `gate_interview`（`true` / `false`）；",
        "4. `notes` 写一句理由，`confidence` 填 1~3（1=凭直觉，3=很有把握）。",
        "",
        "> 维度分不要凑整数好看：**看不清就给中间分并写在 notes 里**，比编一个精确分有用。",
        "",
        "---",
        "",
    ]
    for i, sample in enumerate(order, 1):
        text = _read_resume_text(sample)
        blind = sample.get("blind_id") or sample.get("sample_id")
        if text is None:
            L += [f"## {blind}", "", "（简历文本未入库，无法评，跳过）", ""]
            continue
        L += [f"## {blind}", ""]
        job = (sample.get("job_target") or "").strip()
        jd = (sample.get("jd") or "").strip()
        L.append(f"- 目标岗位：{job or '未提供（按简历自身推断）'}")
        if jd:
            L.append(f"- JD 要求：{jd}")
        L += ["", "```text", text, "```", ""]
    if skipped:
        L += ["---", "", "以下样本本次没发：", ""]
        L += [f"- {sid}" for sid in skipped]
        L.append("")
    # 锚点必须跟着稿子一起发：只给维度名，两个人对「3 分」的理解能差出 20 分，
    # 这样算出来的 κ 量的是两人各自的标准，不是模型的能力。完整版见 docs/盲评锚点卡.md
    L += [
        "---",
        "",
        "## 打分锚点（摘要，务必先看再打分）",
        "",
        "| 维度 | 1 分 | 3 分 | 5 分 |",
        "|---|---|---|---|",
        "| role_fit 岗位匹配 | 主要经历与此岗无关 | 有 1~2 段相关，但技术栈/场景只对上一半 | "
        "主要经历就是这个岗位的日常 |",
        "| impact 成果影响 | 只有职责，没有结果 | 有结果，但是过程量或不可验证 | "
        "有对照、有量级、能看出对谁有好处 |",
        "| evidence_quality 证据 | 只有结论，问「怎么做」就没词 | 有做法，但缺口径或本人边界不清 | "
        "做法+数字+边界齐全，数字有口径 |",
        "| structure 结构 | 找不到意向，或大段不分段 | 有分节有意图，但重点埋在第三行之后 | "
        "首屏就是意向岗位 + 最强一条结果 |",
        "| competitiveness 竞争位 | 同岗明显偏弱、无差异化 | 达到及格线，但没有让人记住的点 | "
        "有稀缺经历/可验证强结果/清晰技术主线 |",
        "",
        "- 一行超过 **60 字**算长句；长句占比高要在 structure 上扣分。",
        "- 两道关只看「过/不过」：初筛看岗位对口与第一眼亮点；面试关看顺着问三层答不答得上。",
        "- **只判断现状，不写改进建议**；不要因为排版好看给高分，也不要替他把没写的数字补上。",
        "- 拿不准就写进 `notes`，不要硬给分。完整口径见 `docs/盲评锚点卡.md`。",
        "",
    ]
    return "\n".join(L)


def _blind_package(dataset_path: str | Path, reviewer: str, out_dir: str | Path,
                   seed: str | None = None, limit: int | None = None) -> dict:
    dataset = load_dataset(dataset_path)
    order = _blind_order(dataset, reviewer, seed)
    if limit:
        order = order[: max(1, limit)]
    skipped = [s.get("sample_id") for s in order if _read_resume_text(s) is None]

    out = _resolve(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    md_path = out / f"{reviewer}-盲评稿.md"
    json_path = out / f"{reviewer}-待填.json"

    md = render_blind_markdown(dataset, reviewer, order, skipped)
    md_path.write_text(md, encoding="utf-8")

    # 空白 json 用打乱后的顺序（dict 保序），并显式声明盲评与参与人数
    payload = {
        "schema_version": "human-eval-1",
        "dataset_id": dataset.get("dataset_id"),
        "reviewer_id": reviewer,
        "reviewer_type": "independent_human",
        "blind_to_model": True,
        "blind_order_seed": seed or f"{dataset.get('dataset_id')}:{reviewer}",
        "conflicts_disclosed": "",
        "completed_at": "",
        "samples": {
            s["sample_id"]: {
                "blind_id": s.get("blind_id") or "",
                "job_target": s.get("job_target") or "",
                "jd": s.get("jd") or "",
                "dimensions": {k: None for k in DIMS},
                "verdict": "",
                "gate_hr": None,
                "gate_interview": None,
                "confidence": None,
                "issues": [],
                "notes": "",
            }
            for s in order
            # 稿子里没有正文的样本不能进待填表：标注者要么凭记忆打分，要么空着交，
            # 两种都会把脏数据混进金标（这就是「有这个编号但没有内容」的坑）
            if s.get("sample_id") not in set(skipped)
        },
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "reviewer": reviewer,
        "markdown": str(md_path),
        "blank": str(json_path),
        "samples": len(order) - len(skipped),
        "skipped": skipped,
    }


def _arbitration(annotations: list[dict], dataset: dict | None = None,
                 threshold: int | None = None) -> dict:
    """挑出必须仲裁的样本：两人分差超过阈值，或只有一个人评。

    为什么单独列：分差 20 分不是「取平均」就完事的——那说明两人对标准的理解不同，
    取平均只是把分歧藏起来。这类样本要第三个人裁，或者两人当面校准后重打。
    """
    if threshold is None:
        try:
            import rubric
            threshold = int(rubric.num("eval.arbitration_threshold"))
        except Exception:                              # noqa: BLE001
            threshold = 15
    by_sample: dict[str, list[tuple[str, int]]] = defaultdict(list)
    dims_by_sample: dict[str, dict[str, dict]] = defaultdict(dict)
    for ann in annotations:
        reviewer = ann.get("reviewer_id") or "?"
        for sid, item in (ann.get("samples") or {}).items():
            dims = item.get("dimensions") or {}
            score = _weighted_score(dims)
            if score is not None:
                by_sample[sid].append((reviewer, score))
                dims_by_sample[sid][reviewer] = dims
    conflicts, singles, done = [], [], []
    for sid, pairs in by_sample.items():
        if len(pairs) < 2:
            singles.append({"sample_id": sid, "reviewers": [r for r, _ in pairs],
                            "score": pairs[0][1]})
            continue
        scores = [s for _, s in pairs]
        spread = max(scores) - min(scores)
        # 分差只说「差多少」，维度差才说「差在哪」——后者才是能拿去对齐标准的东西
        per_dim = dims_by_sample[sid]
        dim_spread = {}
        for key in DIMS:
            vals = {r: (per_dim.get(r) or {}).get(key) for r, _ in pairs}
            vals = {r: v for r, v in vals.items() if isinstance(v, int)}
            if len(vals) >= 2:
                dim_spread[key] = vals
        row = {
            "sample_id": sid,
            "scores": {r: s for r, s in pairs},
            "spread": spread,
            "mean": round(sum(scores) / len(scores), 1),
            "dim_spread": dim_spread,
        }
        (conflicts if spread > threshold else done).append(row)
    expected = [s.get("sample_id") for s in (dataset or {}).get("samples") or []]
    missing = [sid for sid in expected if sid not in by_sample]
    return {
        "threshold": threshold,
        "needs_arbitration": sorted(conflicts, key=lambda r: -r["spread"]),
        "single_reviewer": sorted(singles, key=lambda r: r["sample_id"]),
        "agreed": sorted(done, key=lambda r: r["sample_id"]),
        "never_scored": missing,
    }


def render_arbitration_markdown(result: dict) -> str:
    L = [
        "# 盲评分歧清单",
        "",
        f"仲裁阈值：两人加权总分差 **> {result['threshold']}** 分即列为待仲裁。",
        "",
        "## 需要第三人仲裁",
        "",
    ]
    if not result["needs_arbitration"]:
        L += ["（无）", ""]
    else:
        L += ["| 样本 | 分差 | 两人分数 | 均值 | 差在哪 |", "|---|---|---|---|---|"]
        for row in result["needs_arbitration"]:
            scores = "，".join(f"{r} {s}" for r, s in row["scores"].items())
            detail = "；".join(
                f"{key} " + " vs ".join(f"{r} {v}" for r, v in vals.items())
                for key, vals in (row.get("dim_spread") or {}).items()
                if max(vals.values()) - min(vals.values()) >= 1
            ) or "（各维度一致，分差来自权重汇总）"
            L.append(f"| {row['sample_id']} | {row['spread']} | {scores} | "
                     f"{row['mean']} | {detail} |")
        L += ["", "> 分差大通常不是某个样本的问题，而是两人对某条标准理解不同。"
              "「差在哪」列只列差 ≥1 档的维度：先就这几个维度对齐标准，再决定要不要重打。", ""]
    L += ["## 只有一人评过（缺第二人）", ""]
    L += [f"- {row['sample_id']}：{row['reviewers'][0]} 给了 {row['score']}"
          for row in result["single_reviewer"]] or ["（无）"]
    L += ["", "## 尚未有人评", ""]
    L += [f"- {sid}" for sid in result["never_scored"]] or ["（无）"]
    L.append("")
    return "\n".join(L)


def _load_annotations_dir(path: str | Path, dataset: dict) -> tuple[list[dict], list[dict]]:
    """把标注读进来：给目录就全读，给单个文件就只读它。坏文件单独返回，不静默丢弃。

    注意 `load_annotations()` 只接受**目录**（内部 glob *.json）。曾经在这里
    对每个文件调用它，结果每次都匹配不到文件、返回空列表——标注被静默吃掉，
    arbitration/status 永远显示「0 人已评」。所以这里按文件逐个走 `_valid_annotation`。
    """
    p = _resolve(path)
    if p.is_dir():
        return load_annotations(p, dataset)
    if not p.exists():
        return [], [{"file": str(p), "error": "文件不存在"}]
    try:
        ann = _load_json(p)
    except Exception as exc:  # noqa: BLE001
        return [], [{"file": p.name, "error": f"JSON 解析失败：{exc}"}]
    problems = _valid_annotation(ann, dataset)
    if problems:
        return [], [{"file": p.name, "reviewer_id": ann.get("reviewer_id"),
                     "errors": problems}]
    return [ann], []


REVIEWER_BRIEF = """# 怎么填（{reviewer}）

1. 先看同目录下的 `打分锚点卡.md`（约 3 分钟）。**照例子对，不要照感觉打。**
2. 打开 `{reviewer}-盲评稿.md`，里面有 {samples} 份简历，编号是 `ANON-xxx`。
   每份约 4~5 分钟，全部大概 {minutes} 分钟；可以分几次做完，顺序不用改。
3. 每份简历填进 `{reviewer}-待填.json` 对应的 `ANON-xxx`：
   - 五个维度各 0~5 的整数分：`role_fit` / `impact` / `evidence_quality` / `structure` / `competitiveness`
   - `verdict`：`strong_pass` / `pass` / `borderline` / `reject`
   - `gate_hr`、`gate_interview`：初筛 / 面试关过没过（true / false）
   - `confidence`：1~3（1=凭直觉，3=很有把握）
   - `notes`：一句「如果只能对 HR 说一句」，拿不准的也写这里
4. 填完把 json 发回给发起人。**不要改名、不要合并两份稿子、不要和另一位打分的人讨论。**
5. 两关都过 → `pass`；一关不过 → `borderline`；两关都不过 → `reject`。

> 你评的是「这份简历现在几分」，不是「你会怎么改」。一写改进建议，分数会滑向改完的样子。
"""


def _packet(dataset_path: str | Path, reviewers: list[str], out_root: str | Path,
            seed: str | None = None, limit: int | None = None,
            force: bool = False) -> dict:
    """给一组标注者各打一个可直接转发的包：稿子 + 待填表 + 锚点卡 + 怎么填。"""
    out = _resolve(out_root)
    card = ROOT / "docs" / "盲评锚点卡.md"
    if not force:
        busy = [r for r in reviewers if (out / r).is_dir() and any((out / r).iterdir())]
        if busy:
            raise SystemExit(
                f"{'、'.join(busy)} 的目录里已有文件，未覆盖。\n"
                f"  · 想重发到同一位置：加 --force（旧稿会被覆盖，发之前确认那两位还没开评）\n"
                f"  · 想保留两版：换 --out，例如 --out samples/eval/handouts-2026-10-30")
    packs = []
    for reviewer in reviewers:
        rdir = out / reviewer
        rdir.mkdir(parents=True, exist_ok=True)
        result = _blind_package(dataset_path, reviewer, rdir, seed=seed, limit=limit)
        if card.exists():
            (rdir / "打分锚点卡.md").write_text(
                card.read_text(encoding="utf-8"), encoding="utf-8", newline="\n")
        brief = REVIEWER_BRIEF.format(
            reviewer=reviewer, samples=result["samples"],
            minutes=max(5, round(result["samples"] * 4.5 / 5) * 5),
        )
        (rdir / "怎么填.md").write_text(brief, encoding="utf-8", newline="\n")
        packs.append({
            "reviewer": reviewer,
            "dir": str(rdir),
            "files": sorted(p.name for p in rdir.glob("*")),
            "samples": result["samples"],
            "skipped": result["skipped"],
        })
    return {"out": str(out), "packs": packs}


def _status(dataset_path: str | Path, annotations_path: str | Path) -> dict:
    """一屏看进度：数据够不够、谁交了、还差什么、下一步做什么。"""
    dataset = load_dataset(dataset_path)
    problems = validate_dataset(dataset)
    anns, rejected = _load_annotations_dir(annotations_path, dataset)
    mm = score_metrics(dataset, anns)
    r = readiness(dataset, anns, rejected, mm, problems)
    next_steps = []
    if r["samples"] < r["target_samples"]:
        next_steps.append(
            f"攒简历：还差 {r['target_samples'] - r['samples']} 份 → 丢进 samples/raw，"
            "跑 `python scripts/dataset_build.py --source samples/raw`")
    if r["heldout_samples"] < r["target_heldout"]:
        next_steps.append(
            f"留出集：还差 {r['target_heldout'] - r['heldout_samples']} 份 → 同上，"
            "切分由 dataset_build 自动按来源组粘连")
    if r["valid_reviewers"] < 2:
        next_steps.append(
            "找人打分：跑 `python scripts/human_eval.py packet --reviewers R01 R02`，"
            "把 dist/盲评包/<编号>/ 整个目录发过去")
    elif r["samples_with_two_reviews"] < r["samples"]:
        left = r["samples"] - r["samples_with_two_reviews"]
        next_steps.append(f"催第二人：还有 {left} 份只有一个人评过 → 跑 `human_eval.py arbitration` 看是谁")
    if r["status"] == "ready":
        next_steps.append("出基准：`python scripts/benchmark.py`（页首横幅会自动转绿）")
    pilot_min_pairs = int(rubric.num("eval.pilot_min_pairs"))
    pilot_min_reviewers = int(rubric.num("eval.pilot_min_reviewers"))
    pilot = {
        "ready": (r["valid_reviewers"] >= pilot_min_reviewers
                  and r["samples_with_two_reviews"] >= pilot_min_pairs),
        "pairs": r["samples_with_two_reviews"],
        "min_pairs": pilot_min_pairs,
        "min_reviewers": pilot_min_reviewers,
    }
    if pilot["ready"] and r["status"] != "ready":
        next_steps.insert(0, "可以跑 pilot：`python scripts/benchmark.py --pilot`"
                             "（只看两人一致性 κ，不看准确度）")
    return {
        "pilot": pilot,
        "dataset_id": dataset.get("dataset_id"),
        "split": {"development": r["development_samples"], "heldout": r["heldout_samples"]},
        "samples": r["samples"], "target_samples": r["target_samples"],
        "target_heldout": r["target_heldout"],
        "reviewers": r["valid_reviewers"], "pairs": r["samples_with_two_reviews"],
        "rejected_files": len(rejected),
        "status": r["status"], "blockers": r["blockers"],
        "dataset_problems": problems,
        "next_steps": next_steps,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="阶段 D 人工准确度评测")
    sub = ap.add_subparsers(dest="cmd", required=True)
    inv = sub.add_parser("inventory", help="检查数据集规模与分层")
    inv.add_argument("--dataset", default=str(DEFAULT_DATASET))
    inv.add_argument("--json", action="store_true")

    init = sub.add_parser("init", help="生成一份空白独立标注文件")
    init.add_argument("--dataset", default=str(DEFAULT_DATASET))
    init.add_argument("--reviewer", required=True)
    init.add_argument("--out", required=True)

    blind = sub.add_parser("blind", help="生成盲评发放包（手读稿 + 空白 json）")
    blind.add_argument("--dataset", default=str(DEFAULT_DATASET))
    blind.add_argument("--reviewer", required=True)
    blind.add_argument("--out-dir", default=str(BLIND_HANDOUT_DIR))
    blind.add_argument("--seed", default=None,
                       help="打乱种子；默认用 dataset_id:reviewer，保证可复现且两人不同")
    blind.add_argument("--limit", type=int, default=None, help="只发前 N 份（试跑用）")

    packet = sub.add_parser("packet", help="给多名标注者各打一包（稿子+待填表+锚点卡+怎么填）")
    packet.add_argument("--dataset", default=str(DEFAULT_DATASET))
    packet.add_argument("--reviewers", required=True, nargs="+",
                        help="一位或多位标注者编号，例如 R01 R02（也支持 R01,R02）")
    packet.add_argument("--out", default=str(ROOT / "dist" / "盲评包"))
    packet.add_argument("--seed", default=None)
    packet.add_argument("--limit", type=int, default=None)
    packet.add_argument("--force", action="store_true",
                        help="目录里已有文件时也覆盖（默认拒绝，避免发出半新半旧的稿子）")

    st = sub.add_parser("status", help="一屏看进度：还差什么、下一步做什么")
    st.add_argument("--dataset", default=str(DEFAULT_DATASET))
    st.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    st.add_argument("--json", action="store_true")

    arb = sub.add_parser("arbitration", help="列两人分歧超过阈值的样本")
    arb.add_argument("--dataset", default=str(DEFAULT_DATASET))
    arb.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    arb.add_argument("--threshold", type=int, default=None)
    arb.add_argument("--out", default=None)

    val = sub.add_parser("validate", help="校验标注文件")
    val.add_argument("--dataset", default=str(DEFAULT_DATASET))
    val.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))

    rep = sub.add_parser("report", help="生成人工评测报告")
    rep.add_argument("--dataset", default=str(DEFAULT_DATASET))
    rep.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    rep.add_argument("--audits", default=str(DEFAULT_AUDITS))
    rep.add_argument("--runs-dir", default=str(DEFAULT_RUNS))
    rep.add_argument("--ocr-pairs", default=str(ROOT / "samples" / "eval" / "ocr-pairs.json"))
    rep.add_argument("--legacy-gold", default=str(ROOT / "samples" / "gold.json"))
    rep.add_argument("--out", default=None)
    rep.add_argument("--json-out", default=None)

    args = ap.parse_args()
    if args.cmd == "inventory":
        dataset = load_dataset(args.dataset)
        dataset_problems = validate_dataset(dataset)
        annotations, rejected = load_annotations(DEFAULT_ANNOTATIONS, dataset)
        mm = score_metrics(dataset, annotations)
        r = readiness(dataset, annotations, rejected, mm, dataset_problems)
        print(json.dumps(r, ensure_ascii=False, indent=2) if args.json else
              "\n".join([f"数据集：{dataset.get('dataset_id')}",
                         f"样本：{r['samples']}/{r['target_samples']}",
                         f"development：{r['development_samples']}；heldout：{r['heldout_samples']}/{r['target_heldout']}",
                         f"有效标注者：{r['valid_reviewers']}；双人标注：{r['samples_with_two_reviews']}",
                         f"状态：{r['status']}",
                         "数据集问题：" + ("；".join(dataset_problems) if dataset_problems else "无"),
                         "未满足：" + ("；".join(r["blockers"]) if r["blockers"] else "无")]))
        return 0 if r["status"] == "ready" else 1
    if args.cmd == "init":
        p = _init_annotation(args.dataset, args.reviewer, args.out)
        print(f"已生成空白标注：{p}")
        return 0

    if args.cmd == "blind":
        result = _blind_package(args.dataset, args.reviewer, args.out_dir,
                                seed=args.seed, limit=args.limit)
        print(f"已生成盲评发放包：{result['samples']} 份样本")
        print(f"  手读稿：{result['markdown']}")
        print(f"  待填表：{result['blank']}")
        if result["skipped"]:
            print(f"  跳过（文本未入库）：{'、'.join(result['skipped'])}")
        return 0

    if args.cmd == "packet":
        reviewers = [part.strip() for item in args.reviewers
                     for part in item.split(",") if part.strip()]
        if not reviewers:
            print("至少给一个标注者编号", file=sys.stderr)
            return 2
        result = _packet(args.dataset, reviewers, args.out, seed=args.seed,
                         limit=args.limit, force=args.force)
        print(f"已生成 {len(result['packs'])} 个包：{result['out']}")
        for pack in result["packs"]:
            print(f"  {pack['reviewer']}/  {pack['samples']} 份  "
                  f"{'、'.join(pack['files'])}")
            if pack["skipped"]:
                print(f"    跳过（文本未入库）：{'、'.join(pack['skipped'])}")
        print("\n把每个 `编号/` 目录整个发过去；他们只填 *-待填.json 并把它发回。")
        print("收回后：`python scripts/human_eval.py status` → `arbitration` → `report`")
        return 0

    if args.cmd == "status":
        info = _status(args.dataset, args.annotations)
        if args.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
            return 0 if info["status"] == "ready" else 1
        print(f"数据集：{info['dataset_id']}")
        print(f"样本：{info['samples']}/{info['target_samples']}"
              f"（development {info['split']['development']}，"
              f"heldout {info['split']['heldout']}/{info['target_heldout']}）")
        print(f"标注：{info['reviewers']} 人，双人评过的样本 {info['pairs']} 份"
              + (f"，被拒绝的文件 {info['rejected_files']} 个" if info["rejected_files"] else ""))
        print(f"状态：{info['status']}")
        pilot = info["pilot"]
        print(f"pilot：{'就绪' if pilot['ready'] else '未就绪'}"
              f"（需 ≥{pilot['min_reviewers']} 名标注者且双人评过 ≥{pilot['min_pairs']} 份；"
              f"现在 {info['reviewers']} 人 / {pilot['pairs']} 份）")
        if info["blockers"]:
            print("未满足：" + "；".join(info["blockers"]))
        print("下一步：")
        for i, step in enumerate(info["next_steps"], 1):
            print(f"  {i}. {step}")
        return 0 if info["status"] == "ready" else 1

    if args.cmd == "arbitration":
        dataset = load_dataset(args.dataset)
        annotations, rejected = _load_annotations_dir(args.annotations, dataset)
        result = _arbitration(annotations, dataset, args.threshold)
        md = render_arbitration_markdown(result)
        if args.out:
            out = _resolve(args.out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(md, encoding="utf-8")
            print(f"已写出：{out}")
        else:
            print(md)
        if rejected:
            print(f"⚠ 有 {len(rejected)} 个标注文件被拒绝，未计入", file=sys.stderr)
        return 0
    if args.cmd == "validate":
        dataset = load_dataset(args.dataset)
        dataset_problems = validate_dataset(dataset)
        valid, rejected = _load_annotations_dir(args.annotations, dataset)
        print("数据集：OK" if not dataset_problems else "数据集问题：" + "；".join(dataset_problems))
        if not valid and not rejected:
            print("没有标注文件。")
            return 1
        print(f"有效标注者：{len(valid)}；被拒绝：{len(rejected)}")
        for r in rejected:
            print(f"  - {r['file']}: {r.get('errors') or r.get('error')}")
        return 0 if not rejected and not dataset_problems else 1
    if args.cmd == "report":
        result, md = build_report(
            args.dataset, args.annotations, args.audits, args.runs_dir,
            args.ocr_pairs, args.legacy_gold,
        )
        if args.out:
            p = _resolve(args.out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(md, encoding="utf-8", newline="\n")
            print(f"报告：{p}")
        else:
            print(md)
        if args.json_out:
            p = _resolve(args.json_out)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            print(f"JSON：{p}")
        return 0 if result["readiness"]["status"] == "ready" else 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
