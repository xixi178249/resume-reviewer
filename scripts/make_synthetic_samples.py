"""生成合成简历样本——只为验证评测流水线，不作任何准确率依据。

为什么需要它
------------
评测数据集要 60 份、其中 18 份留出集，才谈得上「切分护栏」和「指标能算」。
真实简历只有 8 份，剩下 52 份需要用户提供——在那之前，`dataset_build.py` 的
去重/粘连切分/冻结已标注样本这些护栏**一次都没在真实规模上跑过**，
`benchmark.py` 也永远只能看到 8 份、heldout 0 份。

这个脚本补的是「够不够跑」，不是「准不准」：

- 每份正文第一行写死 `【合成样本，用于验证流水线，不是真实简历】`；
- 数据集里 `provenance` 记为 `synthetic`，`data_sensitivity` 记为 `synthetic_fixture`；
- `benchmark.py` 的 READY 判定**不认**合成样本的人工标注（它本来也没有标注），
  所以分数不会因为多了 52 份假简历而变得好看——只会少一条「样本不足」。

用法
----
    python scripts/make_synthetic_samples.py              # 生成到 samples/synthetic/
    python scripts/make_synthetic_samples.py --count 52 --seed 7 --out 某个目录
    python scripts/make_synthetic_samples.py --check      # 只校验已有文件是否与本脚本一致

生成结果确定：同 seed + 同 count → 字节完全一致，便于提进仓库。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "samples" / "synthetic"
DEFAULT_COUNT = 52
DEFAULT_SEED = 20260101

BREAK = chr(13) + chr(10)
NL = chr(10)
MARKER = "【合成样本，用于验证流水线，不是真实简历】"

# 八个岗位族，每个给一套「强/中/弱」三档素材。强档带数字，弱档只有动词。
FAMILIES: dict[str, dict[str, object]] = {
    "后端开发": {
        "school": ["某理工大学", "某某大学", "某邮电大学"],
        "major": ["计算机科学与技术", "软件工程", "信息工程"],
        "skills": ["Java、Spring Boot、MySQL", "Go、gRPC、Redis", "Python、FastAPI、PostgreSQL"],
        "strong": [
            "把订单查询接口的 P99 从 1.8s 降到 210ms，慢查询由 34 条降到 2 条",
            "用 Redis 缓存热点商品，数据库 QPS 从 4200 降到 900，命中率 93%",
            "重写对账批处理，单次耗时从 46 分钟降到 7 分钟，覆盖 12 张表",
        ],
        "mid": [
            "参与订单模块开发，负责接口编写与联调",
            "用 Redis 做过缓存，减少了一部分数据库压力",
        ],
        "weak": [
            "负责后端接口开发与维护",
            "参与项目开发，使用 MySQL 完成数据存储",
            "配合团队完成需求",
        ],
    },
    "前端开发": {
        "school": ["某大学", "某某学院", "某师范大学"],
        "major": ["软件工程", "数字媒体技术", "计算机应用"],
        "skills": ["TypeScript、React、Vite", "Vue3、Pinia、Webpack", "JavaScript、小程序、Node"],
        "strong": [
            "把首屏加载从 3.4s 优化到 1.1s，Lighthouse 性能分 58→91",
            "抽了 18 个通用组件，重复代码减少 2600 行，需求平均交付时间从 5 天降到 3 天",
            "搭了前端埋点，覆盖 27 个关键路径，线上白屏率从 0.8% 降到 0.1%",
        ],
        "mid": [
            "参与活动页开发，负责页面还原与交互",
            "做过组件封装，复用在几个页面里",
        ],
        "weak": [
            "负责页面开发与样式调整",
            "参与前端项目，使用 Vue 完成页面",
            "配合后端联调接口",
        ],
    },
    "算法工程": {
        "school": ["某985高校", "某某大学", "某研究院"],
        "major": ["人工智能", "模式识别", "统计学"],
        "skills": ["PyTorch、Transformer、LangChain", "TensorFlow、推荐系统", "Python、向量检索、RAG"],
        "strong": [
            "检索召回率从 61% 提到 84%，评测集 1200 条，人工抽检 200 条一致率 92%",
            "把推理延迟从 480ms 压到 130ms，显存占用从 11GB 降到 6.4GB",
            "在 3 万条工单上做意图分类，F1 0.78→0.89，误判工单每月减少约 1500 单",
        ],
        "mid": [
            "参与问答系统开发，负责语料清洗与向量化",
            "调过一批 prompt，效果有提升但没做系统评测",
        ],
        "weak": [
            "负责算法模型训练与调参",
            "参与 AI 项目，使用深度学习框架",
            "阅读论文并复现部分实验",
        ],
    },
    "产品经理": {
        "school": ["某大学", "某某财经大学", "某师范大学"],
        "major": ["工商管理", "信息管理", "心理学"],
        "skills": ["Axure、SQL、数据分析", "Figma、埋点、A/B 实验", "需求管理、竞品分析"],
        "strong": [
            "主导会员体系改版，付费转化率 2.1%→3.4%，年化增收约 860 万",
            "把新用户引导流程从 7 步压到 4 步，次日留存 31%→44%",
            "牵头上线客服工单自动化，人工工单量下降 38%，日均节省 26 人时",
        ],
        "mid": [
            "负责需求文档撰写与原型设计",
            "参与 A/B 实验设计，跟进过两轮迭代",
        ],
        "weak": [
            "负责产品需求整理与跟进",
            "参与竞品分析，输出分析报告",
            "与研发沟通推进项目",
        ],
    },
    "运营": {
        "school": ["某大学", "某某学院", "某传媒大学"],
        "major": ["市场营销", "新闻传播", "汉语言文学"],
        "skills": ["内容运营、数据看板、活动策划", "用户分层、私域、SCRM", "投放优化、素材迭代"],
        "strong": [
            "搭了召回机制，7 日复购率 12%→19%，覆盖 4.2 万沉默用户",
            "投放获客成本从 26 元降到 17 元，月新增付费用户从 3200 涨到 5100",
            "做季度运营策略，GMV 环比 +23%，活动页转化率 4.6%→6.9%",
        ],
        "mid": [
            "负责社群日常运营与内容排期",
            "参与活动策划，跟进物料与执行",
        ],
        "weak": [
            "负责公众号内容更新",
            "参与用户运营，维护社群",
            "整理运营数据并汇报",
        ],
    },
    "数据分析": {
        "school": ["某财经大学", "某某大学", "某理工大学"],
        "major": ["统计学", "应用数学", "数据科学"],
        "skills": ["SQL、Python、Tableau", "Hive、Spark、指标体系", "Excel、A/B 实验、归因"],
        "strong": [
            "重建核心指标体系，覆盖 6 条业务线，报表口径争议从每周 5 次降到 0",
            "用漏斗归因定位注册流失点，改造后注册完成率 54%→71%",
            "搭自动化看板，把月报制作时间从 3 天压到 2 小时",
        ],
        "mid": [
            "负责日常取数与报表制作",
            "做过一次用户分层分析，结论被采纳",
        ],
        "weak": [
            "负责数据整理与报表",
            "使用 SQL 完成取数需求",
            "协助撰写分析报告",
        ],
    },
    "设计": {
        "school": ["某美术学院", "某某大学", "某艺术学院"],
        "major": ["视觉传达", "工业设计", "数字媒体艺术"],
        "skills": ["Figma、设计系统、动效", "Sketch、插画、品牌", "C4D、Blender、视觉规范"],
        "strong": [
            "搭了 42 个组件的设计系统，改版需求平均工时从 3 天降到 1 天",
            "主导 App 视觉改版，商店评分 3.9→4.6，差评中「看不清」类占比从 18% 降到 4%",
            "做了 3 套品牌视觉，落地 26 个物料，印刷与线上一致性返工 0 次",
        ],
        "mid": [
            "负责界面设计与切图交付",
            "参与改版，产出过若干版本稿",
        ],
        "weak": [
            "负责海报与banner设计",
            "参与视觉设计，按需求出图",
            "配合产品调整界面",
        ],
    },
    "财务": {
        "school": ["某财经大学", "某某大学", "某商学院"],
        "major": ["会计学", "财务管理", "审计"],
        "skills": ["Excel、用友、报表编制", "SAP、税务申报、成本核算", "Power BI、预算管理"],
        "strong": [
            "重做费用报销流程，平均处理时长从 6 天降到 2 天，差错率 3.1%→0.4%",
            "搭月度经营分析模板，覆盖 9 个成本中心，关账时间提前 3 天",
            "梳理应收台账，逾期 90 天以上账款从 480 万降到 190 万",
        ],
        "mid": [
            "负责凭证录入与月度结账",
            "参与预算编制，整理基础数据",
        ],
        "weak": [
            "负责日常账务处理",
            "协助整理发票与合同",
            "使用 Excel 完成统计",
        ],
    },
}

BANDS = ("strong", "mid", "weak")
COMPANIES = ("某某科技有限公司", "某互联网公司", "某集团财务部", "某创业公司")
NAMES = ("李同学", "王同学", "张同学", "刘同学", "陈同学")


def _synthetic_text(rng: random.Random, family: str, band: str, index: int) -> str:
    spec = FAMILIES[family]
    school = rng.choice(spec["school"])  # type: ignore[arg-type]
    major = rng.choice(spec["major"])  # type: ignore[arg-type]
    skills = rng.choice(spec["skills"])  # type: ignore[arg-type]
    bullets = list(spec[band])  # type: ignore[index]
    rng.shuffle(bullets)
    picked = bullets[: min(2, len(bullets))]
    company = rng.choice(COMPANIES)
    name = rng.choice(NAMES)
    start, end = rng.choice([("2025.07", "2025.12"), ("2025.03", "2025.09"), ("2024.09", "2025.06")])

    lines = [
        MARKER,
        f"姓名：{name}（合成）",
        "电话：138****0000　邮箱：synth{index:02d}@example.com".format(index=index),
        f"求职意向：{family}实习生",
        "",
        "教育背景",
        f"{school}　{major}　本科　2022.09 – 2026.06",
        f"主修课程：与本岗位相关的 5 门，GPA 3.{rng.randint(2, 8)}/4.0",
        "",
        "实习经历",
        f"{company}　{family}实习生　{start} – {end}",
    ]
    lines += [f"- {b}" for b in picked]
    lines += [
        "",
        "项目经历",
        f"{family}相关课程项目　2025.0{rng.randint(2, 6)} – 2025.0{rng.randint(7, 9)}",
    ]
    if band == "strong":
        lines.append("- 独立完成，产出可复用的脚本与文档，被同组 3 人接着用")
    elif band == "mid":
        lines.append("- 与 3 人小组协作，负责其中一部分")
    else:
        lines.append("- 参与了项目，完成分配的任务")
    lines += [
        "",
        "专业技能",
        f"- {skills}",
        "",
        "自我评价" if band != "weak" else "其他",
        "- 学习能力强，能快速上手新工具" if band != "weak" else "- 认真负责，服从安排",
    ]
    return "\n".join(lines) + "\n"


def build(count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> dict[str, str]:
    """返回 {文件名: 正文}，顺序与随机数消耗都只由 (count, seed) 决定。"""
    rng = random.Random(seed)
    families = list(FAMILIES)
    out: dict[str, str] = {}
    for i in range(1, count + 1):
        family = families[(i - 1) % len(families)]
        band = BANDS[(i - 1) % len(BANDS)]
        text = _synthetic_text(rng, family, band, i)
        band_cn = {"strong": "强", "mid": "中", "weak": "弱"}[band]
        out[f"{50 + i:02d}-{family}-合成-{band_cn}.txt"] = text
    return out


def digest(files: dict[str, str]) -> str:
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode("utf-8"))
        h.update(files[name].encode("utf-8"))
    return h.hexdigest()


def manifest(count: int, seed: int) -> dict:
    files = build(count, seed)
    return {
        "generator": "scripts/make_synthetic_samples.py",
        "purpose": "验证数据集规模、切分护栏与基准流水线；不作准确率依据",
        "synthetic": True,
        "count": len(files),
        "seed": seed,
        "digest": digest(files),
        "files": sorted(files),
    }


def write(out_dir: Path, count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED,
          force: bool = False) -> tuple[int, int]:
    files = build(count, seed)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = 0
    for name, text in files.items():
        path = out_dir / name
        if path.exists() and not force:
            continue
        path.write_text(text, encoding="utf-8", newline="\n")
        written += 1
    (out_dir / "MANIFEST.json").write_text(
        json.dumps(manifest(count, seed), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return written, len(files)


def check(out_dir: Path, count: int = DEFAULT_COUNT, seed: int = DEFAULT_SEED) -> list[str]:
    """已有文件与本脚本生成的是否一致；不一致逐条说明。"""
    files = build(count, seed)
    problems: list[str] = []
    for name, text in files.items():
        path = out_dir / name
        if not path.exists():
            problems.append(f"缺少 {name}")
            continue
        # 换行归一后再比：仓库在 Windows 上检出会把 LF 变 CRLF，那不是手改。
        if path.read_text(encoding="utf-8").replace(BREAK, NL) != text:
            problems.append(f"{name}：内容与生成器不一致（被手改过？）")
    extra = {p.name for p in out_dir.glob("*.txt")} - set(files)
    for name in sorted(extra):
        problems.append(f"多出 {name}（不是这个生成器的产物）")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成合成简历样本（只用于验证流水线）")
    ap.add_argument("--count", type=int, default=DEFAULT_COUNT)
    ap.add_argument("--seed", type=int, default=DEFAULT_SEED)
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--force", action="store_true", help="覆盖已存在的同名文件")
    ap.add_argument("--check", action="store_true", help="只校验，不写盘")
    args = ap.parse_args(argv)

    out_dir = Path(args.out)
    if args.check:
        problems = check(out_dir, args.count, args.seed)
        for p in problems:
            print(f"[问题] {p}")
        if problems:
            return 1
        print(f"一致：{args.count} 份，digest {digest(build(args.count, args.seed))[:16]}")
        return 0

    written, total = write(out_dir, args.count, args.seed, args.force)
    print(f"生成 {total} 份 → {out_dir}（新写入 {written} 份）")
    print(f"digest {digest(build(args.count, args.seed))[:16]}")
    print("提醒：这 52 份是合成样本，只验证流水线；真实准确率仍要真实简历 + 人工标注。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
