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
    annotations, rejected = load_annotations(annotations_path, dataset)
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
    if args.cmd == "validate":
        dataset = load_dataset(args.dataset)
        dataset_problems = validate_dataset(dataset)
        valid, rejected = load_annotations(args.annotations, dataset)
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
