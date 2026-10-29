"""把简历文件批量加进评测数据集（M2：8 份 → 60 份），并且守住三条规矩。

为什么不能「把文件夹一拖就算加好了」：
    数据集是 v2.0 唯一能产生可信数字的东西，随手加会毁掉三件事：
      1. 同一份简历的两个版本被分到 development 和 heldout 两侧 → 留出集泄露
      2. 同一份简历被加两次 → 指标被同一份样本重复计入
      3. 已经有人评过的样本被重新切分 → 前面的人工标注作废

    所以这个脚本做三件事：去重（按正文哈希）、按来源组粘连切分（同一个人/同一次
    投递的多份简历只在同一侧）、冻结已标注样本的切分（标注一旦产生就不许动）。

关于隐私：
    数据集只记**文件路径**，正文留在本地 samples/txt/（已在 .gitignore 里）。
    所以仓库里永远没有简历原文，别人 clone 下来也拿不到。

用法：
    # 先看一眼会加什么（不改文件）
    python scripts/dataset_build.py --source samples/raw --dry-run

    # 真加进去
    python scripts/dataset_build.py --source samples/raw

    # 检查现有数据集有没有违规（CI/发布前跑）
    python scripts/dataset_build.py --verify

    # 每 4 份留出 1 份（默认），要改切分比例就调这个数
    python scripts/dataset_build.py --source samples/raw --heldout-every 3
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import ingest  # noqa: E402
import rubric  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DATASET = ROOT / "samples" / "eval" / "dataset-v1.json"
DEFAULT_ANNOTATIONS = ROOT / "samples" / "eval" / "annotations"
SUPPORTED_SUFFIXES = {".txt", ".md", ".pdf", ".docx", ".png", ".jpg", ".jpeg", ".webp"}


@dataclass
class Entry:
    path: Path
    text: str
    group: str
    sha: str = ""
    note: str = ""
    sample_id: str = ""
    blind_id: str = ""
    split: str = ""

    def __post_init__(self):
        self.sha = hashlib.sha256(self.text.encode("utf-8")).hexdigest()


@dataclass
class Plan:
    samples: list[dict] = field(default_factory=list)
    added: list[Entry] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def _char_digest(text: str) -> str:
    return hashlib.sha256(ingest.normalize(text).encode("utf-8")).hexdigest()


def scan(paths: list[Path], ocr: bool = False) -> tuple[list[Entry], list[str]]:
    """读出所有候选简历；读不出来的写进 skipped（不静默丢）。"""
    entries: list[Entry] = []
    skipped: list[str] = []
    for path in paths:
        if path.suffix.lower() not in SUPPORTED_SUFFIXES:
            skipped.append(f"{path.name}：不支持的类型 {path.suffix}")
            continue
        try:
            doc = ingest.load(path, ocr=ocr)
        except Exception as exc:  # noqa: BLE001  读不出来就跳过，但要说明原因
            skipped.append(f"{path.name}：{exc}")
            continue
        text = ingest.normalize(doc.text or "")
        if len(text) < ingest.MIN_TEXT_CHARS:
            skipped.append(f"{path.name}：正文只有 {len(text)} 字（< {ingest.MIN_TEXT_CHARS}）")
            continue
        entries.append(Entry(path=path, text=text, group=path.stem))
    return entries, skipped


def load_annotated_ids(ann_dir: Path) -> set[str]:
    """已被人工评过的样本号——它们的切分不许再动。"""
    ids: set[str] = set()
    if not ann_dir.exists():
        return ids
    for f in ann_dir.glob("*.json"):
        try:
            payload = json.loads(f.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        for sid, item in (payload.get("samples") or {}).items():
            if isinstance(item, dict) and item.get("dimensions"):
                ids.add(sid)
    return ids


def _next_id(existing: list[dict], prefix: str, width: int = 3) -> str:
    used = {str(s.get("sample_id")) for s in existing}
    i = 1
    while f"{prefix}{i:0{width}d}" in used:
        i += 1
    return f"{prefix}{i:0{width}d}"


def _blind_token(sha: str, taken: set[str]) -> str:
    """盲号只由内容哈希派生：标注者看不到文件名，也没法反推是谁。"""
    for salt in range(0, 200):
        token = "K" + hashlib.sha256(f"{sha}:{salt}".encode()).hexdigest()[:6].upper()
        if token not in taken:
            taken.add(token)
            return token
    raise RuntimeError("盲号空间不足（这不正常）")


# 文件名里常见的岗位词：能从文件名猜出来就先填上，省得每份都手写。
# 猜错不致命（会被要求确认），但空着会让 benchmark 直接拒跑，所以要有个兜底猜测。
ROLE_KEYWORDS = (
    "后端", "前端", "全栈", "算法", "机器学习", "数据", "开发", "测试", "运维",
    "产品", "运营", "设计", "机械", "硬件", "嵌入式", "财务", "市场", "人力",
    "咨询", "项目", "研究",
)


def guess_job_target(stem: str) -> str:
    """从文件名猜岗位，例如 `05-产品经理-本科` → `产品经理`。猜不出返回空串。"""
    for token in stem.replace("_", "-").split("-"):
        token = token.strip()
        if not token:
            continue
        if any(k in token for k in ROLE_KEYWORDS):
            return token
    return ""


def plan(entries: list[Entry], dataset: dict, annotated: set[str],
         heldout_every: int = 4) -> Plan:
    """把新简历排进数据集：去重 → 粘连切分 → 冻结已标注样本。"""
    if heldout_every < 2:
        raise ValueError("--heldout-every 至少为 2（否则不存在留出集）")
    samples = [s for s in dataset.get("samples") or [] if s.get("sample_id")]
    out = Plan(samples=list(samples))

    known_shas: dict[str, str] = {}
    for s in samples:
        text_file = s.get("text_file") or ""
        path = Path(text_file)
        if not path.is_absolute():
            path = ROOT / text_file
        if path.exists():
            try:
                known_shas[_char_digest(path.read_text(encoding="utf-8", errors="replace"))] = s["sample_id"]
            except OSError:
                pass

    group_split = {s.get("source_group"): s.get("split") for s in samples if s.get("source_group")}
    taken_blind = {s.get("blind_id") for s in samples if s.get("blind_id")}

    for entry in entries:
        if entry.sha in known_shas:
            out.duplicates.append(f"{entry.path.name}：与 {known_shas[entry.sha]} 正文相同")
            continue
        known_shas[entry.sha] = "新样本"
        entry.sample_id = _next_id(samples + [{"sample_id": e.sample_id} for e in out.added
                                             if e.sample_id], "S")
        entry.blind_id = _blind_token(entry.sha, taken_blind)

        # 切分：同来源组必须同侧；已标注样本的组一律留在 development。
        if entry.group in group_split:
            entry.split = group_split[entry.group] or "development"
            out.notes.append(f"{entry.path.name}：与已有样本同组，跟随 {entry.split}")
        else:
            index = len(out.added) + 1
            entry.split = "heldout" if index % heldout_every == 0 else "development"
            if entry.split == "heldout" and any(a in entry.group for a in annotated):
                entry.split = "development"
                out.notes.append(f"{entry.path.name}：同组已有人工标注，强制留在 development")
        group_split[entry.group] = entry.split
        out.added.append(entry)
        guessed = guess_job_target(entry.path.stem)
        if guessed:
            out.notes.append(f"{entry.path.name}：岗位按文件名猜为「{guessed}」，请确认或改掉")
        out.samples.append({
            "sample_id": entry.sample_id,
            "blind_id": entry.blind_id,
            "job_target": guessed,
            "split": entry.split,
            "source_group": entry.group,
            "text_file": str(entry.path.relative_to(ROOT)) if entry.path.is_relative_to(ROOT)
            else str(entry.path),
        })

    for sid in annotated:
        if sid not in {s["sample_id"] for s in out.samples}:
            out.notes.append(f"{sid}：有标注但已不在数据集里（请人工确认）")
    return out


def verify(dataset: dict, annotated: set[str],
           warnings: list[str] | None = None) -> list[str]:
    """发布前自检：切分粘连、盲号唯一、已标注样本没被挪到 heldout。

    problems 是**必须修**的（会造成泄露或标注作废）；填不全的 job_target 只是
    warning——因为「先加进来、再补岗位」是正常流程，卡在这里会让人没法开工。
    """
    problems: list[str] = []
    warn = warnings if warnings is not None else []
    samples = [s for s in dataset.get("samples") or [] if s.get("sample_id")]
    groups: dict[str, set[str]] = {}
    blinds: dict[str, str] = {}
    for s in samples:
        group = str(s.get("source_group") or "")
        groups.setdefault(group, set()).add(str(s.get("split")))
        blind = s.get("blind_id")
        if blind:
            if blind in blinds:
                problems.append(f"盲号重复：{blind}（{blinds[blind]} 与 {s['sample_id']}）")
            blinds[blind] = s["sample_id"]
        else:
            problems.append(f"{s['sample_id']}：缺少 blind_id")
        if not (s.get("job_target") or "").strip():
            warn.append(f"{s['sample_id']}：job_target 还是空的（填好才能跑 benchmark）")
    for group, splits in groups.items():
        if "development" in splits and "heldout" in splits:
            problems.append(f"来源组 {group or '(空)'} 跨 development/heldout，构成泄露")
    for sid in sorted(annotated):
        found = next((s for s in samples if s["sample_id"] == sid), None)
        if found and found.get("split") == "heldout":
            problems.append(f"{sid}：已有人工标注却被放进 heldout（切分被改动过）")
    return problems


def merge_into(dataset: dict, plan_result: Plan, targets: dict) -> dict:
    merged = dict(dataset)
    merged["samples"] = plan_result.samples
    merged["targets"] = dict(targets or {})
    merged["counts"] = {
        "total": len(plan_result.samples),
        "development": sum(1 for s in plan_result.samples if s.get("split") == "development"),
        "heldout": sum(1 for s in plan_result.samples if s.get("split") == "heldout"),
    }
    return merged


def _collect_sources(raw: list[str]) -> list[Path]:
    files: list[Path] = []
    for item in raw:
        path = Path(item)
        if path.is_dir():
            files += sorted(p for p in path.rglob("*") if p.suffix.lower() in SUPPORTED_SUFFIXES)
        else:
            files.append(path)
    return files


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="批量把简历加进评测数据集（含切分护栏）")
    parser.add_argument("--source", action="append", default=None,
                        help="简历文件或目录，可重复给")
    parser.add_argument("--dataset", default=str(DEFAULT_DATASET))
    parser.add_argument("--annotations", default=str(DEFAULT_ANNOTATIONS))
    parser.add_argument("--heldout-every", type=int,
                        default=max(2, int(rubric.num("eval.dataset_target")) //
                                    max(1, int(rubric.num("eval.heldout_target")))))
    parser.add_argument("--ocr", action="store_true", help="允许对图片简历调模型转写（花钱）")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verify", action="store_true", help="只检查现有数据集")
    args = parser.parse_args(argv)

    dataset_path = Path(args.dataset)
    dataset = json.loads(dataset_path.read_text(encoding="utf-8")) if dataset_path.exists() else {
        "schema_version": "human-eval-1", "dataset_id": "resume-bench-v1", "samples": []}
    annotated = load_annotated_ids(Path(args.annotations))

    if args.verify:
        warns: list[str] = []
        problems = verify(dataset, annotated, warns)
        for w in warns:
            print(f"[待补] {w}")
        for p in problems:
            print(f"[问题] {p}")
        if problems:
            print(f"\n不通过：{len(problems)} 个问题")
            return 1
        print(f"通过：{len(dataset.get('samples') or [])} 份样本，切分与盲号无问题")
        return 0

    if not args.source:
        parser.error("要加简历请给 --source，或加 --verify 只做检查")

    files = _collect_sources(args.source)
    if not files:
        print("没找到任何简历文件。", file=sys.stderr)
        return 1
    entries, skipped = scan(files, ocr=args.ocr)
    result = plan(entries, dataset, annotated, heldout_every=args.heldout_every)

    print(f"扫描 {len(files)} 个文件 → 可用 {len(entries)} 份，"
          f"新增 {len(result.added)} 份，重复 {len(result.duplicates)} 份，"
          f"跳过 {len(skipped)} 份")
    for line in result.duplicates:
        print(f"  [重复] {line}")
    for line in skipped:
        print(f"  [跳过] {line}")
    for line in result.notes:
        print(f"  [提示] {line}")
    for e in result.added:
        print(f"  + {e.sample_id} ({e.split}) {e.path.name}")

    if result.added:
        for e in result.added:
            print(f"  下一步：给 {e.sample_id} 填 job_target（岗位）"
                  f"——手改 {_rel(e.path)} 对应条目的 \"job_target\" 字段")

    if not result.added:
        print("没有新增样本。")
        return 0

    merged = merge_into(dataset, result, dataset.get("targets") or {})
    pending: list[str] = []
    problems = verify(merged, annotated, pending)
    for w in pending:
        print(f"  [待补] {w}")
    if problems:
        for p in problems:
            print(f"[问题] {p}")
        print("\n切分自检不通过，已中止（未写文件）。")
        return 1

    if args.dry_run:
        print("\n--dry-run：不写文件。去掉这个参数才会真正写入。")
        return 0

    dataset_path.parent.mkdir(parents=True, exist_ok=True)
    dataset_path.write_text(json.dumps(merged, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8", newline="\n")
    print(f"\n已写入 {dataset_path}（共 {len(merged['samples'])} 份）；"
          f"记得给新增样本补 job_target。")
    return 0


def _rel(path: Path) -> str:
    try:
        return str(path.relative_to(ROOT))
    except ValueError:
        return str(path)


if __name__ == "__main__":
    raise SystemExit(main())
