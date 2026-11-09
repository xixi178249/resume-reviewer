---
name: project-eval
display_name: 项目测评
display_name_en: Project Evaluator
description: "给一个做完的项目打分：跑真测试、跑真探针、查线上版本、查文档一致、算评测资产，出可核对的评分卡与缺陷清单。测不出来的写「判不出」，不拿 0 分糊弄。"
description_zh: "项目测评技能——不靠印象，靠命令。跑测试套件与端到端探针、对线上版本、查文档与配置是否漂移、数金标与留出集，出七维评分卡 + 带证据的缺陷清单。测不出来的维度标「判不出」并按比例归一权重。"
description_en: "Score a finished project with evidence, not impressions: run its test suite and end-to-end probes, check the deployed version, verify docs/config consistency, count gold data and holdout sets, then emit a seven-dimension scorecard plus a defect list where every number traces back to a command. Unmeasurable dimensions are marked unknown and excluded from weighting instead of being scored zero."
capabilities:
  - 跑测试套件并解析套件数/断言数/是否全绿
  - 跑端到端探针并收集 FAIL 明细
  - 对线上版本与关键页面可用性
  - 查版本号、README、CHANGELOG、相对链接是否漂移
  - 分三层统计依赖（顶层/函数内/try 内）与静默吞异常
  - 数金标样本、留出集、基准报告状态、局限清单
  - 输出 Markdown 评分卡与 JSON 明细
allowed-tools:
  - eval_project
triggers:
  - 项目测评
  - 测评这个项目
  - 给项目打分
  - 项目体检
  - 自评
  - 评分卡
  - 质量评估
  - 工程成熟度
  - 验收
version: 1.0.0
---

# 项目测评（Project Evaluator）

## 这个技能解决什么问题

「做完了」和「做好了」之间差的不是感觉，是**证据**。绝大多数自评是这样收尾的：
功能列表打勾、截图放两张、结论写「整体完成度较高」——这种报告给不了任何决策。

这个技能只做一件事：**把能测的测出来，把测不出来的明说**。每个分数后面都站着一条命令
或一个文件；凡是跑不了、连不上、拿不到的，一律写「判不出」，既不扣分也不充数。

## 怎么用

```bash
# 测当前目录（会自己跑测试与端到端探针，约 2-4 分钟）
python scripts/eval_project.py --root . --base https://your-app.example.com

# 只测静态与线上，不重跑测试（几十秒）
python scripts/eval_project.py --root . --base https://your-app.example.com --skip-run

# 落盘：既出人看的 md，也出机器读的 json
python scripts/eval_project.py --json out/eval.json --md out/scorecard.md
```

退出码恒为 0（这是测评，不是门禁）。要当门禁用，读 json 里的 `total` 或 `defects`。

## 七个维度与权重

| 维度 | 权重 | 靠什么测 |
|---|---|---|
| 可验证性 | 25% | 跑 `scripts/run_tests.py`，取套件数、断言数、是否全绿 |
| 端到端可用 | 20% | 跑 `scripts/selfcheck.py`，取探针通过率与 FAIL 明细 |
| 线上可用 | 15% | 打 `/api/health` 与首页/规则页，对版本号与状态码 |
| 文档一致 | 10% | VERSION / package.json / README 头部 / CHANGELOG / 相对链接 |
| 代码卫生 | 10% | 三层依赖统计、静默吞异常、最大单文件行数 |
| 交付完整 | 10% | CHANGELOG、截图、可分发包、规则页 |
| 评测资产 | 10% | 金标样本数、留出集、bench 报告状态、局限清单 |

三个反直觉的设计，都是踩出来的：

1. **「判不出」不占权重**。没连上线上就把权重按剩余维度归一，而不是记 0 分——
   否则「今天网不好」会被读成「线上挂了」。
2. **依赖分三层**。写在 `try: ... except ImportError` 里的是可选增强，写在函数里的是
   延迟依赖，只有裸在模块顶层的才算「装不上就跑不起来」。一刀切会把 PDF 解析库
   全算成硬依赖，好项目被打成 0 分。
3. **单独留一维给评测资产**。620 条断言全绿，也可能只有 8 份人工金标、留出集 0 份——
   工程分不能替可信度背书。这一维低于 5 分就说明「它能跑，但还证不出它准」。

## 输出长什么样

```text
# 项目测评报告 · resume-reviewer v2.7.0

> 生成时间：2026-11-09 17:41　总分 9.0 / 10（优秀）

| 维度 | 权重 | 得分 | 依据 |
|---|---|---|---|
| 可验证性 | 25% | 10.0 | 620 条断言 / 29 套件，全绿 |
| 评测资产 | 10% | 5.0 | 金标只有 8 份（<20）；留出集 0 份；基准报告 PILOT… |
```

完整报告（含缺陷清单与实测数据）见 `examples/示例-本项目评分卡.md`。

后面接「缺陷清单」（每条带级别、领域、具体位置）与「实测原始数据」（每维一行真数字），
最后固定一段「这张卡测不到什么」，防止拿工程分去吹产品分。

## 边界

- **不测判得准不准**。没有真实金标与留出集时，分数只说明工程做完了。
- **不测模型质量**。mock/规则引擎下跑通的链路，换真模型仍要人复核。
- **不测并发与成本**。没有压测与账单数据，延迟、限流、开销都不在卡里。
- **只读**。除了 `--json/--md` 指定的输出文件，不改被测项目任何内容。

## 想加一维怎么办

在 `scripts/eval_project.py` 里加一对 `measure_xxx()` + `score_xxx()`，把
`WEIGHTS`/`DIM_CN`/`evaluate()` 的 `dims` 字典各加一项，再同步
`references/评分口径.md`。权重改动必须同步改文档，否则这张卡自己就成了「文档漂移」的样本。
