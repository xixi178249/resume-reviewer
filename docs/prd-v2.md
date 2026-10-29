# 简历评审助手 · 产品需求文档 v2（A 路线：可信度）

| 项目 | 内容 |
|---|---|
| 文档版本 | **v2.0-draft** |
| 日期 | 2026-10-29 |
| 对应代码版本 | 现状 `VERSION` = 1.10.1（规则 v1.8 / prompt v1.8）→ 目标 **2.0.0** |
| 写作时快照 | 2026-10-29 13:58。仓库里已有部分 v2 组件由并行任务落地，逐项状态见 §5.1；本文档不假装它们不存在，也不替它们验收 |
| 状态 | 规划稿。§3.1、§3.4 的部分机制已实现（`config/rubric.yaml`、`scripts/rubric.py`、`scripts/benchmark.py`、`scripts/stats_metrics.py` 及两个新测试套件），其余为待实施设计 |
| 读者 | 自己、接手维护的人、两名独立标注者与协调者 |
| 上游文档 | `docs/prd.md`（v1.1，683 行，描述已上线现状）、`docs/人工评测方案.md`（196 行，盲评与指标口径） |

## 与 v1 的差异摘要

| 维度 | v1.1（现状） | v2.0（本版） | 性质 |
|---|---|---|---|
| 评价标准来源 | 数字散在 `check_output.py`、`version_diff.py`、`ingest.py`，prompt 里再抄一遍；v1.7、v1.8 各漏过一次 | `config/rubric.yaml` 单一真源；代码 import 配置；守卫测试禁止字面量回退、校验 prompt 数字一致 | 已落地主体，Schema 缺口待补 |
| 数据集 | 8 份，全部 `development` | 60 份 = 42 `development` + 18 `heldout`；heldout 代码级锁死 | 待做 |
| 人工标注 | 0 名独立标注者、0 份双人、0 份 heldout | 双人盲评 60/60；分歧 >15 分进仲裁 | 待做（标注者 0/2） |
| 一致性指标 | 工具能算，但没有数据；旧 MAE=8.0 是 4 份单人非盲的代理值 | MAE、95% CI、Pearson、Spearman、Cohen's κ（四档）、QWK、各维度 QWK、两关一致率、repeat=3 极差 | 统计内核已落地，repeat 极差待补 |
| benchmark | 无；`human_eval.py report` 输出到 `reports/eval/` | `python scripts/benchmark.py` 一条命令出 `bench/report.md`（待建）+ `bench/report.json`（待建），数据不够就是 NOT_READY | 已落地，首次产物待生成 |
| 报告页指标 | 无 | 只对有人工标注（双人）的样本显示「与人工均值差 X 分」，其余 `not_ready`；绝不写「准确率」 | 待做 |
| 改稿闭环 | `version_diff.check_applied` 已实现四态，报告 Markdown 里有一句推断 | 报告页显式显示「上次 N 条建议落实了几条」，四态分开计数 | 部分（页面待接结构化字段） |
| 等待体验 | 同步请求 + 前端计时 | SSE 四阶段（脱敏→读格式→逐项检验→出报告）+ `/api/metrics` | 待做 |
| 历史存储 | `render.yaml:34` 设 `REPORTS_DIR=/tmp/reports`，容器重建即丢 | 公网版 UI 明写「不留历史」，或对象存储；本版选前者 | 待做（需改 render.yaml 与 UI） |
| 版本 | 1.10.1 | 2.0.0 | 待做 |
| 明确不做 | v1 §5.2 的 9 项里 6 项仍不做 | 新增「换更强模型」「对象存储」进不做表，并写理由 | 本文档新增 |

## 0. 一句话

v2.0 不改「上传→几十秒→一页能改的报告」，只补一件事：**把「评得准不准」从口号变成可复现的数字**——60 份样本、18 份锁死的 heldout、双人盲评、一条命令出报告；数据不够就继续显示 `not_ready`。

## 1. 产品目标与使用场景

### 1.1 v2 的唯一目标

| 项 | 内容 |
|---|---|
| 业务目标 | 让「这个评审器与人工判断差多少」成为可以在 README、简历、面试里说出口的数字，并且任何人都能用一条命令重算 |
| 用户目标 | 求职者知道「这分数能不能信」；维护者知道「改阈值/换模型有没有变好」 |
| 非目标 | 不提升报告文采、不新增简历模板、不做 B 端、不做账号体系（见 §1.5） |
| 成功定义 | §1.3 的 9 条硬门槛全部达标，且 `bench/report.md`（待建）状态为 READY；缺一条就是没完成，不调低门槛 |

### 1.2 用户与场景

| 角色 | 场景 | 与 v1 的差异 |
|---|---|---|
| 求职者（主用户） | S1 上传简历+岗位→拿报告；S2 按「今天先做这一件事」改一处→再评；S3 换版本→看改版对比；S4 没 key→基础评测 | 沿用 v1，不改交互 |
| 独立标注者（新增） | S5 只看脱敏正文与岗位/JD，独立打 5 维分、给四档结论、判两关；看不到模型分、看不到另一人的分 | 新增，v2 的数据来源 |
| 仲裁者（新增） | S6 只处理两人总分差 >15 分的样本与不一致维度，给出仲裁分与一句理由；不参与首轮 | 新增 |
| 维护者（新增） | S7 改一个阈值→跑 `--suite rubric`→跑 `benchmark.py`；被代码禁止在调参时读 heldout | 新增，v2 的日常操作 |

### 1.3 v2 成功指标（硬门槛）

| # | 指标 | v1 现状 | v2 门槛 | 判定方式 | 对应 AC |
|---|---|---|---|---|---|
| 1 | 数据集总量 | 8 份（全 development） | ≥60 份 | `dataset-v2.json` 通过 `validate_dataset`，`benchmark` readiness 显示 60 | AC-A2.1 |
| 2 | heldout | 0 份 | 18 份，锁死，只用于最终验收 | 不加 `--allow-heldout` 读 heldout 必须退出 2 | AC-A2.2 |
| 3 | 双人盲评覆盖 | 0/60 | 60/60 = 100% | 两份标注文件各 60 份，`blind_to_model=true`，互不可见 | AC-A3.1 |
| 4 | 总分 MAE | 8.0（4 份单人非盲，代理值，**不可继承**，见 §1.4） | ≤8，且必须报 95% CI | heldout 上 `benchmark --allow-heldout` 的 `metrics.mae` | AC-A4.5 |
| 5 | Cohen's κ（四档 verdict） | 未测 | ≥0.6 | 同一次 heldout 运行的 `metrics.kappa_verdict` | AC-A4.5 |
| 6 | repeat=3 分差 | 单次抖动 ±5 分，未量化 | 每份 3 次跑分 max−min ≤6 | 稳定性 12~20 份 × 同一配置 3 次 | AC-A4.6 |
| 7 | 引用可追溯率 | 100%（197/197，8 份） | 100%（60 份上同样） | `check_output.evaluate_dir` 逐字命中 | AC-A9.3 |
| 8 | 编造数字 | 0 | 0 | 同上，`number_audit` 未在原文出现的数字条数 | AC-A9.3 |
| 9 | 真实用户闭环 | 未验证 | ≥3 名真实用户完成「改一处→再评」，且每人能说出一处被采纳的改动 | 人工记录表 + 复评 `review_id` 对 | AC-A6.4 |

> 门槛只写在这里和 `config/rubric.yaml` 的 `eval:` 段（`eval.dataset_target=60`、`eval.heldout_target=18`、`eval.mae_max=8`、`eval.repeat_spread_max=6`、`eval.kappa_min=0.6`、`eval.arbitration_threshold=15`），其它文档不得另立一套数字。

### 1.4 旧 MAE=8.0 为什么不能继承

| 问题 | 事实 |
|---|---|
| 这个 8.0 是什么 | `samples/gold.json` 里 4 份旧人工分与模型分的平均绝对误差；单人、非盲、写权重时就看过这些样本 |
| 为什么是代理值 | 样本太少且不是独立标注；权重与阈值是照着这批样本调的，等于自己出题自己判卷 |
| 旧 MAE=2.0 更不能用 | mock 剧本先于权重写成，指标是自我实现的（README 已知局限已写明） |
| v2 怎么替代 | 双人盲评 + 未参与调参的 heldout + 独立数据重测；`bench/report.md` 的 READY 状态是唯一出口 |
| 报告里怎么写旧值 | 只能写「旧值 8.0 是探索性代理，不可继承」，不能写「准确率 8 分」；`benchmark.py` 报告底部已固定这句话 |

### 1.5 v2 明确不做（每条都给理由）

| 不做的事 | 理由 |
|---|---|
| 账号 / 登录 | 可信度问题与登录无关；引入账号=引入密码存储、找回、注销、隐私合规，W1~W4 排期装不下，且现在没有多用户需求 |
| 在线编辑简历 | 编辑器是另一个产品，且「边改边评」会让分数刷新频繁、A/B 对比失效；用户用 Word/WPS 改完再传，链路更短 |
| 多语言 | rubric 的判据是中文简历习惯（900 字/页、句首长 12 字、中文长句阈值 60 字）；翻译等于重写一套 rubric + prompt + 标注，不是 v2 的范围 |
| 批量上传 | 单份 45~110 秒，10 份排队 8~18 分钟，体验比逐份更差；且批量会掩盖单份失败；有明确批量需求时再做队列 |
| 图表看板 | 与「今天先做这一件事」直接冲突；图表会把「改哪一句」稀释成「看趋势」；bench 报告用表格足够 |
| 换更强模型 | 未校准的评分换模型不会变可信：换模型只改变误差分布，不改变「没人量过误差」这件事；先把尺子造出来，再谈换模型 |
| 对象存储（本版） | 见 §3.8：没有账号体系时对象存储只是把临时目录换个地方丢；公网版明写「不留历史」+ 当场下载，成本最低且不撒谎 |
| 自动清理定时任务 | `REPORTS_DIR` 在容器里本来就是临时的；本地运行由用户手动删；等磁盘告警再做 |

> v1 §5.2 里其余 3 项继续不做，理由不变：多用户账号与云同步（要做多人时）、移动端专属优化（有真实数据后）、队列/并发控制（ThreadingHTTPServer 够用）。

### 1.6 人工验证现状（v2 起点，2026-10-29）

| 项 | 现状 | v2 目标 |
|---|---|---|
| 数据集 | 8 份，全 `development`（`samples/eval/dataset-v1.json`） | 60 份 = 42 dev + 18 heldout（`dataset-v2.json`，待建） |
| 独立标注者 | 0 人 | 2 人 |
| 双人共同标注 | 0 份 | 60 份 |
| heldout | 0 份 | 18 份，锁死 |
| 仲裁样本 | 0 份 | 预估 12~18 份（分歧 >15 分） |
| 模型意见 / 改写审阅 | 0 份 | 可选（v1 方案第二轮），不阻塞 READY |
| 重复评审（repeat=3） | 0 组 | 12~20 份 × 3 次 |
| OCR 对照 | 0 组 | 可选，不阻塞 |
| 测试规模 | 13 个套件（v1 为 11 个 202 条）；新增 rubric 21 条、stats 43 条 | 新增指标类断言只增不减 |
| 评价标准 | 已从代码抽到 `config/rubric.yaml`，三模块已接线，prompt 有守卫测试 | 补 Schema 断言，之后改阈值只动一处 |

## 2. 页面结构与视觉（只写变化）

### 2.1 不变的部分

三步单页、820px 内容宽、卡片 12px 圆角、颜色只表达状态、不出现雷达图/进度环——全部沿用 `docs/prd.md` §2，v2 一个字不改。下面只写新增的四处。

### 2.2 评审进度：SSE 四阶段

| 阶段（事件 name） | 页面文案 | 时间预算 | 失败时 |
|---|---|---|---|
| `desensitize` | 正在脱敏：手机号、邮箱、身份证会替换成「已脱敏」 | 45 秒 | 提示「脱敏失败，文件可能损坏」+ 重试 |
| `format` | 正在读格式：PDF / DOCX / 扫描件 | 5 秒 | 区分「格式不支持」「扫描件文字太少」两种文案 |
| `check` | 正在逐项检验：19 个字段、三档门禁 | 540 秒 | 超时提示「模型这次没返回合格结果」+ 重试 |
| `render` | 正在出报告 | 10 秒 | 提示「报告生成失败，可下载 JSON 排查」 |

- 四段预算合计 600 秒，与 `docs/prd.md` 的超时口径一致；页面不再只显示一个转圈计时，而是把「现在在干什么」写出来。
- 每个阶段事件带 `elapsed_ms`，页面显示「已用 N 秒」；不显示百分比（假进度比没进度更伤信任）。
- `check` 阶段内部的 `repeat=1~3` 不拆成子阶段，只显示「第 i/3 次」。
- 进度条/动效仍不做（v1 设计原则：不用图表装饰）。

### 2.3 报告页「人工基准」徽标

| 状态 | 触发条件 | 文案 | 颜色 |
|---|---|---|---|
| `not_ready`（默认） | 本份简历不在 `human-scores.json`，或该样本人工标注 <2 人 | 「本份暂无人工基准（not_ready）」 | 灰 `--muted`，不抢眼 |
| `ready` | 命中哈希且 `reviewer_count ≥ 2` | 「与人工均值差 X 分（n=2，口径见附录 B）」 | 蓝 `--accent`，仅此一处 |
| `stale` | 命中但人工基准的 `rubric_version` 与当前不同 | 「人工基准来自旧规则版本，仅供参考」 | 黄 `#b45309` |

- X 保留 1 位小数（如「差 4.3 分」），等于「模型总分 − 人工共识均分」的绝对值；不取整、不美化，同时并列显示人工均分原值，避免「差 4 分」被误读成「4%」。
- 徽标旁必须有一行小字：「单个样本的差值不能推总体准确度」。
- 页面与 `/api/report` 响应都不得出现「准确率」「准确度」「正确率」四个词；口径说明统一叫「与人工均值的差」。

### 2.4 改稿闭环区块

| 元素 | 内容 |
|---|---|
| 位置 | 报告页「和上一版比」区块顶部，一行摘要 + 可展开明细 |
| 摘要文案 | 「上次 3 条建议：1 条看起来已改 / 1 条原句还在 / 1 条改成了别的写法 / 0 条无法判断」 |
| 明细 | 最多列 3 条，每条显示「建议原句 → 现在的位置」；未知态显式写「无法判断（文本差异太大或相似度不足）」 |
| 数字来源 | 接口结构化字段 `compare.applied_counts`，**不解析 Markdown** |
| 禁忌 | 不把 `unknown` 计入「没改」；总数必须等于上一版建议条数（2~6 条） |

### 2.5 公网版「不留历史」提示

| 位置 | 文案 |
|---|---|
| 首页拖拽区下方（`retention=ephemeral` 时） | 「公网体验版不保存历史报告，请在页面上直接下载 .md / .docx」 |
| 历史抽屉空态 | 「公网版不留历史；本地运行时才会保留」 |
| 下载按钮旁 | 「下载后文件在你本地，服务器不留」 |

- 判断依据是服务端返回的 `retention` 字段，不是前端写死环境变量；本地运行不显示这些提示（见 §3.8）。

### 2.6 文案约束

- 不用「精准」「权威」「专业评测」这类无口径形容词。
- 数字必须带 n 与来源（「基于 18 份 heldout」），不得只写「误差 4 分」。
- 错误文案沿用 `docs/prd.md` 附录 A，v2 新增错误码见本文附录 A。

## 3. 模块功能与流程

### 3.1 M1 · 评价标准单一真源（已落地主体，Schema 缺口待补）

#### 3.1.1 文件与职责

| 文件 | 行数 / 现状 | 职责 |
|---|---|---|
| `config/rubric.yaml` | 125 行，已存在 | 唯一真源：阈值、权重、四档分界、版本号、评测门槛（`eval:` 段） |
| `scripts/rubric.py` | 225 行，零依赖，已存在 | 受限 YAML 子集解析器 + 取值 API + 加载期校验；`CONFIG_PATH` 指向仓库根 `config/rubric.yaml` |
| `scripts/check_output.py` | 已接线 | `WEIGHTS`、`VERDICT_BANDS`、`PAGES_CHARS`、`GATE_*`、`HEAD_CHARS`、`LONG_SENTENCE_MIN`、`MIN_QUOTE`、`MAX_QUOTE`、`WEAK_LINE_MIN_REST` 全部来自 rubric |
| `scripts/version_diff.py` | 已接线 | `MIN_SIMILARITY`、`MAX_CANDIDATES`、`MAX_DIFF_ITEMS`、`MIN_PROBE` |
| `scripts/ingest.py` | 已接线 | `MIN_TEXT_CHARS` |
| `scripts/test_rubric_single_source.py` | 215 行 / 21 条，已存在 | 守卫：代码常量 == 配置、禁止字面量回退、prompt 数字一致、配置错误要在加载期炸 |

`config/` 目录在 v1 里不存在（v2 新增）；`prompts/` 与 `samples/` 之外新增的只有它。

#### 3.1.2 键值总表（值与仓库现状一致）

| rubric 键 | 值 | 消费方 | 代码位置 |
|---|---|---|---|
| `version` | `v1.8` | 报告元信息、`/api/health.rubric_version` | `check_output` / `web/server.py` |
| `prompt_version` | `v1.8` | 选择 `prompts/system-<version>.md` | `run_eval` 与守卫测试 |
| `format.pages_chars` | 900 | 篇幅换算「约 N 页」 | `check_output.py:77` |
| `readability.long_sentence_chars` | 60 | 长句判据 | `check_output.py:92` |
| `readability.weak_line_min_rest` | 4 | 「只有动词没有实字」的弱行判据 | `check_output.py:214` |
| `evidence.min_quote` | 6 | 引用最短字数 | `check_output.py:112` |
| `evidence.max_quote` | 200 | 引用最长字数 | `check_output.py:113` |
| `evidence.head_chars` | 12 | 句首比对长度（找原句位置） | `check_output.py:90` |
| `gates.hr.role_fit_min` | 3 | 初筛关：岗位匹配最低分 | `check_output.py:80` |
| `gates.hr.impact_min` | 2 | 初筛关：结果与影响最低分 | `check_output.py:81` |
| `gates.interview.evidence_quality_min` | 3 | 面试关：证据质量最低分 | `check_output.py:82` |
| `gates.interview.high_risk_max` | 1 | 面试关：高风险项最多几条 | `check_output.py:83` |
| `verdict.strong_pass_min` / `pass_min` / `borderline_min` | 80 / 65 / 50 | 四档结论分界（reject=0） | `check_output.py:32` |
| `weights.*` | 25 / 25 / 20 / 15 / 15（合计必须 100） | 加权总分 | `check_output.py:31` |
| `version_diff.min_similarity` | 0.62 | 改版对比是否同版 | `version_diff.py:31` |
| `version_diff.max_candidates` | 40 | 历史候选上限 | `version_diff.py:34` |
| `version_diff.max_diff_items` | 200 | 文本差异条数上限 | `version_diff.py:37` |
| `version_diff.min_probe` | 8 | 建议落实判定的最短探针 | `version_diff.py:40` |
| `ingest.min_text_chars` | 120 | 扫描件「文字太少」阈值 | `ingest.py:31` |
| `eval.dataset_target` | 60 | benchmark 就绪门槛 | `benchmark.py`（`readiness`） |
| `eval.heldout_target` | 18 | 同上 | `benchmark.py`（`readiness`） |
| `eval.mae_max` | 8 | 同上，且 READY 判据 | `benchmark.py`（`thresholds`） |
| `eval.kappa_min` | 0.6 | 同上 | `benchmark.py`（`thresholds`） |
| `eval.repeat_spread_max` | 6 | repeat 极差门槛 | `benchmark.py`（`thresholds`；目前只展示门槛，计算待补） |
| `eval.arbitration_threshold` | 15 | 双人总分差进仲裁线 | 待接入 `human_eval` |

> `eval.arbitration_threshold=15` 目前只有配置、没有代码消费，属于 §5.1 的待做项；`eval.repeat_spread_max` 同理（benchmark 目前只打印门槛，不算 3 次极差）。

#### 3.1.3 三处一致的机制（现状 + 待补）

| 消费方 | v2 机制 | 状态 |
|---|---|---|
| checker（代码） | 三个模块 `import rubric`，常量写成 `int(rubric.num("..."))` / `rubric.weights()` / `rubric.verdict_bands()`；保留同名模块常量，避免破坏现有 202 条断言与外部引用 | ✅ 已落地 |
| prompt | **不引入模板占位符**：`prompts/system-v1.8.md` 仍保持人能直接读的完整文本；由守卫测试断言「60 字内」「6~200 字」与配置一致，并用正则拦住过期阈值（`每句 N 字内` 中 N ≠ 60 即红） | ✅ 已落地（21 条中 4 条） |
| JSON Schema | 计划新增断言：`prompts/output-schema-v1.8.json` 的 `evidence.minLength/maxLength` 必须等于 `evidence.min_quote/max_quote`（6/200）；`rewrites` 的 2~6 条若要保持一致，需先把这两个数补进 rubric | ❌ 未覆盖，v2 必须补（AC-A1.6） |
| 其它 prompt 副本 | schema 里的 2~6、0~6 等数字目前不在 rubric；v2 要么补键，要么在 PRD 明确「不进 rubric 的只有结构性数量，不含阈值」 | 决策点，见 §3.9.3 |

为什么不用占位符注入：prompt 是评审「法律条文」，必须能被标注者和人类审阅者直接通读；注入会把一句人话变成模板，且一旦渲染路径出错会让模型看到空串。用测试守数字，比用模板生成句子更稳。

#### 3.1.4 加载期校验（写错就炸，不静默兜底）

| 错误场景 | 行为 |
|---|---|
| 重复键（同一缩进层级） | `RubricError: 重复的键 ...`，带行号 |
| 不认识的行（多行字符串、锚点、标签） | `RubricError`，提示只支持受限子集，带行号 |
| 取不存在的键 | `RubricError` 带完整键路径（`gates.hr.不存在的键`），不返回默认值 |
| `num()` 取到非数字 | `RubricError: ... 不是数字` |
| `weights()` 合计 ≠ 100 | `RubricError`，提示权重必须合计 100 |
| `verdict_bands()` 非严格降序 | `RubricError`，不替用户「修正」排序 |
| 文件缺失 / 读不了 | 启动即失败，`/api/health` 不可用；不做「用内置默认值继续跑」 |

> 服务端表现：`rubric` 加载失败时 `GET /api/health` 返回 503 + `code= RUBRIC_INVALID`，评审入口直接拒绝，不降级到硬编码——否则线上会出现「代码是 60、prompt 是 40」的老毛病。

#### 3.1.5 改一个阈值要动哪里（runbook）

1. 改 `config/rubric.yaml` 对应键（例如 `readability.long_sentence_chars: 60 → 55`）。
2. 改 `prompts/system-v1.8.md` 里同一句话（「60 字内」→「55 字内」），并同步 `output-schema-v1.8.json` 若涉及。
3. 跑 `python scripts/run_tests.py --suite rubric`：应绿；若 prompt 没改，会红并直接指出缺「55 字内」。
4. 跑 `python scripts/benchmark.py`：报告顶部应记录本次阈值（待补：报告 meta 增加 rubric 版本/配置哈希）。
5. 若阈值属于「判定规则」而非「展示口径」，必须同时升 `rubric.version` 与 `prompt_version`，并接受历史分数不可比（`config_hash` 变化）。

#### 3.1.6 已落地测试（21 条，按类分组）

| 类 | 条数 | 钉住什么 |
|---|---|---|
| `RubricParseTest` | 8 | 嵌套/标量/行内列表解析、引号内 `#`、重复键、未知行带行号、缺键带路径、非数字报错、真实文件含 9 个 section |
| `CodeUsesRubricTest` | 6 | check_output 10 个常量、version_diff 4 个常量、ingest 1 个常量、weights 合计 1.0 且 5 项、四档分界与配置一致、三模块源码禁止字面量回退 |
| `PromptMatchesRubricTest` | 4 | prompt 文件存在、`60 字内` 出现、`6~200 字` 出现、过期阈值（`每句 40 字内`）必须为 0 |
| `GuardTest` | 3 | 权重≠100 报错、四档非降序报错、临时配置热加载读到新值 |

#### 3.1.7 与 v1 的差异与影响

- v1 的 15 个散落数字（check_output 10 + version_diff 4 + ingest 1）现在只有唯一来源；`WEIGHTS` 与 `VERDICT_BANDS` 也一并迁进 `weights:` / `verdict:` 段，因此「权重未校准」的注脚仍在，但至少不会再出现两处不一致。
- `rubric.version` 仍是 `v1.8`：本次没有改任何数值，只有来源变化，历史分数与新分数可比；2.0.0 是工具链版本，不是评分规则版本（详见 §3.9.3）。
- 影响面：`check_output`、`version_diff`、`ingest`、`web/server.py`（无改动）、`qa_smoke.py`（无改动）、CI（无改动，测试自动纳入）。**Dockerfile 尚未 COPY `config/`，容器内 import 会失败**，见 §7.3 依赖。

### 3.2 M2 · 数据集 60 份与切分护栏（待做）

#### 3.2.1 分层配额

| 层 | 目标 | 理由 |
|---|---|---|
| 岗位族：后端 / 前端或数据 / 产品 / 算法 / 其他 | 18 / 12 / 12 / 10 / 8 = 60 | 覆盖权重最高的 role_fit 判定差异 |
| 简历强弱：强 / 中 / 弱 | 各约 20 | 避免只样本「好简历」导致分数分布偏 |
| 载体：文本 / 扫描件 | ≥48 / ≥6 | 扫描件走 OCR，阈值 120 字，单独看 OCR 是否改分 |
| JD：有 / 无 | ≥40 / ≤20 | role_fit 判据依赖 JD |
| 切分 | development 42 / heldout 18 | 约 70/30，沿用人工评测方案 |

#### 3.2.2 `samples/eval/dataset-v2.json`（待建）字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `schema_version` | str | 固定 `human-eval-1`（沿用 `validate_dataset` 契约） |
| `dataset_id` / `created_at` | str | 版本与日期 |
| `external_sharing` | str | `not_approved` / `approved`；未批准不得外发原文 |
| `targets` | obj | `total_samples=60`、`heldout_min=18`、`reviewers=2`、`repeat=3` |
| `samples[].sample_id` | str | `S01`…`S60` |
| `samples[].blind_id` | str | 随机匿名编号，标注包只给这个 |
| `samples[].split` | enum | `development` / `heldout` |
| `samples[].source_group` | str | 同一人/同一来源的不同版本必须同组，禁止跨 split |
| `samples[].stratum` | obj | `role_family` / `strength` / `carrier` / `has_jd` |
| `samples[].text_file` | str | `samples/txt/01-后端-无量化.txt`（现状命名；不入库，见 `.gitignore`） |
| `samples[].content_sha256` | str | 脱敏正文哈希，用于报告页命中与「同一份」判定 |
| `samples[].model_output` | str | `outputs/01-后端-无量化.real.json`（可选，现状命名） |
| `samples[].consent` | obj | `collected_at` / `channel` / `allow_annotation` / `allow_external` |

#### 3.2.3 三层护栏

| 层 | 实现位置 | 行为 |
|---|---|---|
| 数据校验层 | `human_eval.validate_dataset`（`human_eval.py:66`） | 缺 `sample_id`/`job_target`/`text_file` 不存在的样本报错；`split` 非枚举报错；同一 `source_group` 跨 split 报错（`:92-94`） |
| 读入锁 | `benchmark.select_samples`（`benchmark.py:69`），`HeldoutLocked` | `--split heldout` 或 `all` 且无 `--allow-heldout` → 退出码 2，stderr 打印「heldout 只用于最终验收」 |
| 访问留痕 | `bench/heldout-access.jsonl`（待建） | 每次成功读 heldout 追加 `{ts, actor, purpose, n_samples, allow_heldout:true}`；无记录视为流程违规 |

#### 3.2.4 heldout 解封流程（只在 W4 做一次）

1. 冻结：prompt 版本、rubric 版本、权重、门禁、模型名全部写进 `bench/report.json` 的 `config`，此后不再改。
2. 跑 development：`python scripts/benchmark.py --predictor outputs --split development`，确认指标与人工判断无明显异常。
3. 跑 heldout：`python scripts/benchmark.py --allow-heldout --split heldout --require-ready`。
4. 记录：把 `bench/report.md`（含 READY/NOT_READY 与 reasons）原样入库，写访问日志。
5. 冻结后不再根据 heldout 结果改任何阈值；若必须改，heldout 作废并重新采集 18 份（不回炉已看过的）。

### 3.3 M3 · 双人盲评与仲裁（待做）

#### 3.3.1 流程

1. 协调者招募 2 名独立标注者 + 1 名仲裁者（协调者本人不得当独立标注者）。
2. 30 分钟培训：讲 5 个维度锚点、四档含义、两关规则；用 2 份 `samples/demo/` 试标（不计入数据）。
3. 生成标注包：`python scripts/human_eval.py init --dataset samples/eval/dataset-v2.json --reviewer R01 --out samples/eval/annotations/R01.json`；包内只有 `blind_id`、脱敏正文、岗位、JD、量表。
4. 两人各自独立标注，不得讨论同一份；完成后各自 `human_eval.py validate`。
5. 一致性：`human_eval.py report`（工具已算 `overall_mae`、`overall_spearman`、`dimension_qwk`、`verdict_exact_rate`）。
6. 仲裁：总分差 >15 分（`eval.arbitration_threshold`）的样本进队列，由仲裁者只看两份标注与该简历，给出仲裁分 + 一句理由。
7. 冻结：两份标注 + 仲裁结果入库（脱敏、只含分数与 ≤120 字逐字摘录），此后不再改。

#### 3.3.2 盲法（5 条硬要求）

| # | 要求 | 检查方式 |
|---|---|---|
| 1 | 标注者看不到模型分、档位、门禁、报告 | 标注包由 `human_eval.py init` 生成，不含这些字段；脚本扫描标注文件不得出现 `model_score`/`overall_score`/`verdict_model` |
| 2 | 两名标注者互不可见 | 各自只拿到自己的包；协调者不得转发对方文件；`blind_to_model=true`、`completed_at` 为实际日期 |
| 3 | 样本顺序随机 | `blind_id` 与顺序由脚本随机化，不用 `S01` 原序 |
| 4 | 首轮冻结后才可看模型输出 | 审阅模板在 `samples/eval/audits/`，首轮标注 `completed_at` 全部早于审阅开始 |
| 5 | heldout 不进标注包 | `init` 默认只取 `development`；heldout 解封后才标注（避免标注者也参与调参） |

#### 3.3.3 仲裁规则与共识分

| 情形 | 处理 |
|---|---|
| 两人总分差 ≤15 | 共识分 = 两人加权总分的均值（`human_eval.consensus` 现行口径，保留 1 位小数） |
| 两人总分差 >15 | 共识分 = 仲裁分；两人原分保留在报告里，不进 MAE 主口径 |
| 维度分差 ≥2 分 | 该维度标记「不一致」，仲裁者只裁该维度，不改其它维度 |
| 档位/两关不一致 | 仲裁者裁档位与两关；不能只改总分不改档位（会自相矛盾） |
| 仲裁者也拿不准 | 记 `confidence=low`，该样本从主口径剔除并计入「剔除数」，不得悄悄按均值算 |

**现状缺口**：`consensus()` 目前直接对两人取均值，没有「优先仲裁分」；`arbitration_threshold` 在 rubric 里但无代码消费。v2 需在 `consensus()` 增加 `adjudication` 入参并优先取仲裁分（AC-A3.3）。

#### 3.3.4 工具现状（`scripts/human_eval.py`，990 行）

| 能力 | 现状 |
|---|---|
| 子命令 | `inventory` / `init` / `validate` / `report`（argparse 在文件尾部） |
| 数据集校验 | `validate_dataset`（`:66`），含 split 枚举与 `source_group` 跨 split 检查 |
| 标注校验 | 必填 5 维 + verdict + `gate_hr` + `gate_interview` + `confidence`；`issues` 0~5 条、`evidence` 逐字且 ≤120 字 |
| 一致性 | `agreement_metrics`（`:296`）：每维度 QWK、总分 MAE、Spearman、档位完全一致率；<2 人时全返回 None |
| 共识分 | `consensus`（`:275`）：加权总分均值、多数派 verdict/两关，平票返回 None |
| 就绪判定 | `readiness`（`:632`）：目标 60、heldout 18、≥2 名有效标注者、双人覆盖、模型配对 |
| 旧 gold 处理 | `legacy_proxy`（`:677`）：明确标注「单人非盲，仅探索性代理」 |
| 缺什么 | 仲裁队列生成/校验、`human-scores.json` 聚合导出、`repeat=3` 稳定性命令 |

#### 3.3.5 成本（详见 §7.2）

60 份 × 2 人 × 8~12 分钟 ≈ 16~24 人时；培训 2 人时；仲裁 12~18 份 × 5~8 分钟 ≈ 1~2.5 人时；合计 23~33 人时，且必须在 W1 锁定 2 名标注者，否则 W2 无法开工。

### 3.4 M4 · 一致性指标与 benchmark（部分已落地）

#### 3.4.1 指标口径（唯一版本，实现见 `scripts/stats_metrics.py`）

| 指标 | 定义 | 实现 | 门槛 |
|---|---|---|---|
| MAE | `mean(abs(机器总分 − 人工共识分))` | `stats_metrics.mae` | ≤8（`eval.mae_max`） |
| MAE 95% CI | 自助法 2000 次重采样取 2.5%/97.5% | `stats_metrics.bootstrap_ci(n=2000)` | 必须报，不设门槛 |
| RMSE | 均方根误差，对大误差更敏感 | `stats_metrics.rmse` | 参考 |
| Pearson | 总分线性相关，夹在 [-1, 1] | `stats_metrics.pearson` | 参考，不设硬门槛 |
| Spearman | 总分秩相关 | `stats_metrics.spearman` | 参考 |
| Cohen's κ | 四档 verdict（strong_pass/pass/borderline/reject）名义一致性 | `stats_metrics.cohen_kappa` | ≥0.6（`eval.kappa_min`） |
| QWK | 0~100 分二次加权 κ | `stats_metrics.quadratic_weighted_kappa` | 参考 |
| ±5 / ±10 分内 | 与共识分差 ≤5 / ≤10 的比例 | `stats_metrics.agreement_within` | 参考 |
| 两关一致率 | `gate_hr`、`gate_interview` 过/不过一致比例 | `benchmark.compute_metrics` | 参考 |
| repeat 极差 | 同份同配置 3 次跑分的 max−min | **待补** | 每份 ≤6（`eval.repeat_spread_max`） |
| 各维度 QWK | 5 个维度各自与人工的一致性 | `human_eval.agreement_metrics` | 参考，报全部 5 维 |

> 统计内核约定（刻意的）：算不出来返回 `None`，不返回 0；空输入抛 `ValueError`；长度不一致/元素不是二元组直接报错。这样报告里「—」永远表示没数据，0 永远表示真的一致。

#### 3.4.2 `scripts/benchmark.py` 契约（439 行，已存在）

| 项 | 契约 |
|---|---|
| 用法 | `python scripts/benchmark.py`（默认规则引擎 + development） |
| 机器分来源 | `--predictor local`（规则引擎现算，免费、确定性）或 `outputs`（读已有 `outputs/*.real.json`） |
| 切分 | `--split` 取 `development`、`heldout` 或 `all`；默认 development；heldout/all 必须加 `--allow-heldout` |
| 产物 | `bench/report.md`（人读，待建——首次运行生成）与 `bench/report.json`（机器读，待建） |
| 就绪状态 | 报告顶部只有 `READY` / `NOT_READY` 两种；NOT_READY 时逐条列未达标项 |
| 退出码 | 0 正常（含 NOT_READY）；1 运行出错（数据集校验失败等）；2 heldout 被锁，或 `--require-ready` 且未就绪 |
| 参数 | `--dataset`、`--annotations`、`--out`、`--json`、`--require-ready`、`--quiet` |
| 数据流 | `human_eval.load_dataset` → `validate_dataset`（不合格直接退出）→ `load_annotations`（拒收列表打 stderr）→ `select_samples`（锁 heldout）→ 逐份取人工共识分与机器分 → 指标 → 报告 |

#### 3.4.3 READY 判据（数据够不够与结果好不好分开）

| 条件 | 门槛来源 | 不满足时的文案 |
|---|---|---|
| 数据集 ≥60 份 | `eval.dataset_target` | 「数据集 N 份 < 目标 60 份」 |
| heldout ≥18 份 | `eval.heldout_target` | 「heldout N 份 < 目标 18 份」 |
| 独立标注者 ≥2 人 | 硬编码 `reviewers < 2` | 「独立标注者 N 人 < 2 人（双人盲评是硬要求）」 |
| 已标注样本 ≥60 份 | `eval.dataset_target` | 「已标注样本 N 份 < 目标 60 份」 |
| 已做 heldout 最终验收 | 必须带 `--allow-heldout` | 「heldout 尚未做最终验收（未加 --allow-heldout）」 |

> 结果好不好（MAE ≤8、κ ≥0.6）不参与 `ready` 判断，但 READY 报告必须在正文里同时给出 MAE/κ 与门槛对照；任一不达时报告状态降为 NOT_READY（见 AC-A4.5）。这样「数据够但效果差」不会被误读成「可以发布」。

#### 3.4.4 报告结构（`bench/report.md`，固定 7 节）

1. 标题与元信息：生成时间、数据集路径、切分、标注目录、机器分来源、评测样本数。
2. `## 状态：READY | NOT_READY`：未就绪时列出 reasons 表。
3. `## 指标`：n、MAE、MAE 95% CI、RMSE、Pearson、Spearman、Cohen's κ（四档）、QWK、±5、±10、初筛一致率、面试一致率。
4. `## 逐份`：样本、切分、人工共识、机器分、差（带符号）、两侧结论。
5. `## 凑不齐的样本`：没有人工共识或读不到机器分的样本与原因（不静默丢）。
6. `## 口径与禁忌`：旧 MAE=8.0 不得继承；NOT_READY 不得引用；heldout 不许调参；复现命令。
7. 页脚：报告生成命令与 `bench/report.json` 路径。

#### 3.4.5 现状缺口（必须修，否则 AC 过不了）

| 缺口 | 说明 | 修法 |
|---|---|---|
| repeat 极差未算 | `compute_metrics` 的 `spread` 实际是「机器分 − 人工分」的差值分布，而报告注释写的是「同份重复跑的极差」，两者不是一回事 | 把该字段改名 `diff_spread`；新增 `repeat_spread`：读稳定性 3 次跑分算 max−min，报告单列一节 |
| 元信息缺阈值指纹 | 报告没有记录本次 rubric 版本/权重/门禁，换了配置无法复现 | 报告 meta 增加 `rubric_version`、`prompt_version`、权重摘要、`content_sha256` 的数据集哈希 |
| 无 `--repeat` 能力 | 需要人工跑 3 次再对齐；容易漏 | 新增 `--repeat N --stability-dir` 选项，或单独 `stability.py`（W3 决定） |
| READY 与 heldout 强绑 | 只有跑过 `--allow-heldout` 才可能 READY；development 永远 NOT_READY | 保持该行为（防止拿 dev 数字对外），但报告要写一句「READY 只可能出现在最终验收之后」 |

#### 3.4.6 统计内核的边界（`stats_metrics.py`，356 行，43 条测试）

- 空输入：`mae([])` / `rmse([])` / `agreement_within([])` 抛 `ValueError`；`pearson` / `spearman` / `cohen_kappa` 在样本不足时返回 `None`（无定义，不冒充 0）。
- 相关系数夹到 [-1, 1]；κ 不夹（边际分布极偏时 κ 可以小于 -1，是真实结果）。
- 差值分布 `DIST_KEYS = (n, min, p10, p25, median, p75, p90, max, mean)`；单点样本直接返回该点，不插值。
- `bootstrap_ci` 默认 2000 次、95%、固定 seed=0，保证同一输入结果可复现。
- 不打印、不读写文件、不持有全局可变状态——所以能被构造数据一条条钉死。

### 3.5 M5 · 报告页接真实指标（待做）

#### 3.5.1 数据链路

```
标注文件（不入库）               聚合导出（入库，无正文）
samples/eval/annotations/*.json ──► samples/eval/agg/human-scores.json
                                          │
评审请求：脱敏正文 ── sha256 ──► 查表 ──► 报告页徽标（ready / not_ready / stale）
```

- 服务器上不会存在 `annotations/`（`.gitignore` 已忽略、不进镜像），所以报告页只能读聚合件；聚合件只含哈希与聚合值，泄露不出简历内容。
- `human-scores.json` 由标注完成后一次性导出，随 2.0.0 入库并进 Docker 镜像（`COPY samples ./samples` 已覆盖）。

#### 3.5.2 聚合件字段（`samples/eval/agg/human-scores.json`，待建）

| 字段 | 说明 |
|---|---|
| `schema_version` | 固定 `human-scores-1` |
| `generated_at` | 导出时间；`/api/health` 原样返回 |
| `rubric_version` / `prompt_version` | 标注时的规则版本，用于判定 `stale` |
| `samples`（对象，键为 `content_sha256`） | `sample_id`、`reviewer_count`、`human_mean`（1 位小数）、`human_sd`、`verdict`、`gate_hr`、`gate_interview`、`adjudicated`（是否仲裁过） |
| `n_total` / `n_ready` | 总样本数 / 达到「≥2 名标注」的样本数 |

- 导出命令（待建）：`python scripts/human_eval.py export-scores --dataset samples/eval/dataset-v2.json --annotations samples/eval/annotations --out samples/eval/agg/human-scores.json`；无标注时导出空表并退出 0（不伪造）。
- `human_mean` 用 §3.3.3 的共识分口径（仲裁优先），不是简单两人均值；`reviewer_count < 2` 的样本不进 `samples`。

#### 3.5.3 展示判定（服务端算，前端只渲染）

| 判定顺序 | 条件 | 返回 |
|---|---|---|
| 1 | 聚合件缺失 / 解析失败 | `human_baseline.status = "unavailable"`，页面不显示徽标，只显示「人工基准未配置」 |
| 2 | 哈希不在 `samples` 里 | `status = "not_ready"`，文案「本份暂无人工基准」 |
| 3 | 命中但 `rubric_version` ≠ 当前 | `status = "stale"`，文案「人工基准来自旧规则版本」 |
| 4 | 命中且 `reviewer_count ≥ 2` | `status = "ready"`，`diff = abs(model_score − human_mean)`、`human_mean`、`n`、`adjudicated` |

- 只在**评审成功**的响应里出现 `human_baseline`；失败响应不带，避免前端拿旧值拼接。
- 历史报告查看（`GET /api/report`）时按当前聚合件重算，不把旧徽标写死进 Markdown。
- 导出下载的 `.md`/`.docx` 里带同一行，但不带颜色（Markdown 无状态色）。

#### 3.5.4 禁止事项

- 不写「准确率 X%」：单份差值推不出准确度，总体才有 MAE；这是 v2 最容易犯的错。
- 不用 `reviewer_count = 1` 的样本显示「与人工差」——一人标注不是基准。
- 不把 `not_ready` 藏起来：宁可显示灰字，也不能显示一个看起来像结论的分数。
- 不把 `stale` 当 `ready`：旧规则版本的差值与当前打分不是同一把尺子。

### 3.6 M6 · 改稿闭环（部分已落地）

#### 3.6.1 现状

| 已有能力 | 位置 |
|---|---|
| 建议落实判定：`check_applied` 已实现四态（`applied` / `untouched` / `changed` / `unknown`），并给每条建议附 `evidence_at` | `version_diff.py:228` |
| `build()` 计算 `applied_counts`（含 `total`） | `version_diff.py:274-275` |
| `to_markdown()` 在「上次的建议」段落打印计数，并给每条打「✅ 看起来已经改过 / ◻️ 原句还在」 | `version_diff.py:371-382` |
| 「文本比对推断，不是检测」免责句 | `version_diff.py:392` |

#### 3.6.2 v2 要求

| # | 要求 |
|---|---|
| 1 | `/api/review` 顶层返回 `compare.applied_counts`：`{applied, untouched, changed, unknown, total}`，四键之和 == `total` |
| 2 | 报告页从结构化字段渲染「上次 N 条建议：…」一行，不解析 Markdown |
| 3 | `unknown` 单独计数，文案「无法判断」，不得并入「没改」 |
| 4 | 每条建议返回 `state` 与 `evidence_at`（能在新文本里定位时给出行号/片段），前端明细最多展示 3 条 |
| 5 | 计数只在「同版对比」成立时出现：相似度 ≥ `version_diff.min_similarity`(0.62)、`config_hash` 相同；否则整块隐藏 |
| 6 | 下载的 Markdown 与页面数字必须一致（同一份 `compare.applied_counts` 渲染） |

#### 3.6.3 四态语义（写进报告与页面文案）

| 状态 | 判定（摘要） | 页面文案 |
|---|---|---|
| `applied` | 旧建议原句的关键片段在新文本里以「改后形式」出现，且新文本不再逐字含原句 | ✅ 看起来已改 |
| `untouched` | 旧建议原句（≥ `min_probe` 个实字）仍逐字出现在新文本 | ◻️ 原句还在 |
| `changed` | 同一位置换成了别的写法，语义接近但原句已不在 | ✏️ 改成了别的写法 |
| `unknown` | 相似度不足、文本截断、或前后两份不是同一份简历 | ❔ 无法判断 |

#### 3.6.4 边界与禁忌

- 相似度 <0.62：不做同版对比，不显示建议落实，只提示「这两份看起来不是同一份简历」。
- 规则版本不同（`rubric_version` 变化）：不推断建议落实，只报文本变化（`delta=null`）。
- 文本差异最多 200 条（`max_diff_items`），超出只显示「差异过多，建议人工对比」。
- 建议条数 2~6（`rewrites` 契约）：`total` 必须等于上一版建议条数；对不上说明数据损坏，页面显示「对比数据异常」而不是猜。

### 3.7 M7 · SSE 流式与 `/api/metrics`（待做）

#### 3.7.1 端点契约

| 端点 | 方法 | 行为 |
|---|---|---|
| `/api/review/start` | POST | 校验参数（同现 `/api/review`），创建 job，返回 202 `{job_id, stream, expires_in}`；不阻塞等结果 |
| `/api/review/stream?job_id=` | GET | `text/event-stream`，推送四阶段与终态；心跳每 15 秒 `: ping` |
| `/api/review` | POST | **保留**同步旧路径（CLI、`qa_smoke.py` 53 项、老前端都用它）；SSE 是新增，不改旧契约 |
| `/api/metrics` | GET | 聚合指标 JSON，只读；不返回任何单次请求标识 |

- job 存放：进程内 `dict` + `threading.Lock`，TTL 10 分钟；服务重启即丢（页面提示「服务重启了，请重试」）。
- 同一时间不限制并发 job 数，但每个评审仍受模型侧限流；`repeat=3` 在 `check` 阶段内串行。

#### 3.7.2 阶段与时间预算

| 顺序 | `stage` | 页面文案 | 预算（秒） | 说明 |
|---|---|---|---|---|
| 1 | `desensitize` | 正在脱敏 | 45 | 手机号/邮箱/身份证替换；纯文本秒级，扫描件占用 OCR 时间 |
| 2 | `format` | 正在读格式 | 5 | 判定 PDF/DOCX/扫描件、页数、字数；代码里这一步实际在 `/api/extract` 先做过，SSE 只做「确认」并复用结果 |
| 3 | `check` | 正在逐项检验 | 540 | 19 字段校验 + 三档门禁 + `repeat`；模型调用都在这段 |
| 4 | `render` | 正在出报告 | 10 | 组装报告、写盘、计算改版对比 |
| — | 合计 | — | 600 | 与 v1 超时口径一致 |

#### 3.7.3 事件格式

```
event: stage
data: {"stage":"check","label":"正在逐项检验","elapsed_ms":3210,"repeat_index":1}

event: done
data: {"ok":true,"review_id":"…","score":72,"report":"…","compare":{…},"human_baseline":{…},"elapsed_ms":41230}

event: error
data: {"ok":false,"code":"REVIEW_VALIDATION_FAILED","message":"…","hint":"…","stage":"check"}
```

- 事件顺序必须严格 1→2→3→4，`done`/`error` 只有一个且必达。
- 重连（`GET /api/review/stream` 再来一次）：若 job 已完成，直接补发 `done`/`error`；未完成则从当前阶段继续，不重放历史 `stage` 事件（避免前端重复计数）。
- `Cache-Control: no-cache`、`X-Accel-Buffering: no`；反代/Cloudflare 不缓冲。

#### 3.7.4 `/api/metrics` 字段

| 字段 | 含义 | 隐私 |
|---|---|---|
| `requests_total` | 收到 `/api/review*` 请求数 | 聚合 |
| `review_ok` / `review_failed` | 结果成功/失败数 | 聚合 |
| `fail_rate` | `review_failed / requests_total` | 聚合 |
| `refusal_rate` | `REVIEW_VALIDATION_FAILED / requests_total`（被门禁拒收的比例，是 fail 的子集） | 聚合 |
| `latency_ms` | 成功请求的 `p50` / `p90` / `p95` / `max` | 聚合 |
| `stage_ms` | 四阶段各自 `p50` / `p95` | 聚合 |
| `tokens` | `llm` 模式累计 `input` / `output`（无 key 时不增加） | 聚合 |
| `engines` | `llm` / `local` 各自请求数 | 聚合 |
| `uptime_s` / `version` / `rubric_version` | 进程启动时长与版本 | 公开 |

- 计数在进程内累加，`reports/_metrics.json`（待建）只在本地模式落盘；重启清零，页面要写「本次进程」。
- 绝不记录：简历正文、文件名、`api_key`、IP、`review_id`、模型原始响应。
- `fail_rate` 与 `refusal_rate` 的定义要写进页面脚注，否则「0% 失败」会被读成「从没失败过」。

#### 3.7.5 失败与超时

| 情形 | 行为 |
|---|---|
| 客户端中途断开 | 服务端继续跑完并落盘（工作不浪费）；重连可拿终态 |
| 模型侧超时/限流 | `error` 事件 `code=LLM_TIMEOUT` / `LLM_RATE_LIMIT`，提示重试，不降级到规则引擎（沿用 v1「不自动降级」） |
| 全部候选不合格 | `code=REVIEW_VALIDATION_FAILED`，不落盘、不计入 `review_ok`；计入 `refusal_rate` |
| 服务重启 | job 丢失；重连返回 `SSE_JOB_EXPIRED`，页面提示重试 |
| job_id 不存在 | 404 + `SSE_JOB_NOT_FOUND` |

#### 3.7.6 兼容与回滚

- 前端先探测 `/api/health` 是否有 `sse: true`，没有就走旧同步路径；后端开关 `ENABLE_SSE=0` 可一键退回。
- `POST /api/review` 的请求/响应字段一律不改；`qa_smoke.py` 与 CLI 不受影响。
- 若 SSE 在部署环境不可用（反代缓冲写死），2.0.0 仍可发布：前端降级为「计时 + 阶段文案轮播」的假进度**是不允许的**，只能退回旧的同步计时。

### 3.8 M8 · 持久化决策（待做）

#### 3.8.1 现状

| 事实 | 位置 |
|---|---|
| 报告目录由环境变量指定，默认 `reports/` | `web/server.py:59`：`REPORTS = Path(os.environ.get("REPORTS_DIR") or ROOT / "reports")` |
| Render 把报告写进容器 `/tmp/reports` | `render.yaml:34`：`REPORTS_DIR=/tmp/reports` |
| 容器重建/重启即丢 | 无持久卷、无对象存储；`/tmp` 不是持久层 |
| UI 现在没有任何提示 | 历史抽屉会「看起来还在」，实际重启后为空 |

#### 3.8.2 三种方案对比

| 方案 | 成本 | 隐私 | 复杂度 | 结论 |
|---|---|---|---|---|
| A. 对象存储（S3/R2） | 要账号、密钥、SDK、生命周期规则 | 报告含脱敏正文与学校/项目名，外存需加密与保留策略 | 中高 | 本版不做，列触发条件 |
| B. UI 明写「公网版不留历史」 | 一次文案 + 一个开关 | 不落盘 = 不新增泄露面 | 低 | **本版采用** |
| C. 挂持久卷 | Render 付费盘 | 同 A | 低 | 已有 `REPORTS_DIR` 支持，部署侧可选，不写进产品 |

#### 3.8.3 决策

- 2.0.0 采用 **B**：公网部署显式声明临时保留；本地运行维持现状（历史存 `reports/`）。
- 触发 A 的条件（任一满足再做）：出现「找回上周那份报告」的真实需求 ≥3 次；有付费用户；需要跨设备历史。
- 不做「假装保存」：不写「已保存」文案，不显示无法兑现的历史承诺。

#### 3.8.4 实现要点

| 项 | 要求 |
|---|---|
| 开关 | `render.yaml` 增加 `RETENTION_NOTICE=ephemeral`；本地不设 |
| 健康检查 | `GET /api/health` 增加 `retention` 字段，取值为 `ephemeral` 或 `local` |
| 历史行为 | `ephemeral`：历史只存内存（当次进程内可做改版对比），重启清空；`GET /api/history` 返回 `{items: [], notice: "公网版不留历史"}` 或当次进程内的条目 |
| 落盘策略 | `ephemeral` 不写 `reports/` 与 `.history.json`；报告生成后直接进响应，用户当场下载 |
| 页面 | §2.5 的三处文案由 `retention` 字段控制；不靠前端判断域名 |
| 备份/恢复 | `local` 模式恢复方式沿用 v1 §4.4；`ephemeral` 模式无恢复概念，页面写明 |

### 3.9 M9 · 发布 2.0.0（待做）

#### 3.9.1 版本一致性

| 位置 | 现状 | 2.0.0 要求 |
|---|---|---|
| `VERSION` | `1.10.1` | `2.0.0` |
| `README.md` 头部 | 写 v1.10.0（与 VERSION 漂移 0.0.1） | 与 VERSION 一致，并加「准确度口径」段 |
| `/api/health.version` | 读 `VERSION` | 2.0.0 |
| `scripts/qa_smoke.py:154-161` | 已做三处一致性检查（VERSION / health / package 文件） | 继续通过 |
| `docs/CHANGELOG-v2.0.md` | 不存在 | 新建，逐条列 A1~A9 |

#### 3.9.2 文件清单（状态 = 写作时快照）

| 文件 | 状态 | 说明 |
|---|---|---|
| `config/rubric.yaml` | ✅ 已存在 | 单一真源，125 行 |
| `scripts/rubric.py` | ✅ 已存在 | 零依赖受限 YAML 解析器，225 行 |
| `scripts/stats_metrics.py` | ✅ 已存在 | 统计内核，356 行 |
| `scripts/benchmark.py` | ✅ 已存在 | 439 行；产物目录待首次运行生成 |
| `scripts/test_rubric_single_source.py` | ✅ 已存在 | 21 条 |
| `scripts/test_stats_metrics.py` | ✅ 已存在 | 43 条 |
| `scripts/check_output.py` / `version_diff.py` / `ingest.py` / `run_tests.py` | ✅ 已改 | 常量改为读配置；`run_tests.py` 增 rubric、stats 两套件（13 套件） |
| `bench/report.md` / `bench/report.json` | ⏳ 待建 | 第一次 `python scripts/benchmark.py` 生成；`bench/` 目录当前不存在 |
| `samples/eval/dataset-v2.json` | ⏳ 待建 | 60 份数据集 |
| `samples/eval/agg/human-scores.json` | ⏳ 待建 | 报告页聚合件 |
| `samples/eval/adjudication/queue.json` | ⏳ 待建 | 仲裁队列 |
| `prompts/system-v2.0.md` / `output-schema-v2.0.json` | ⏳ 待定 | 若改阈值才新建；不改则沿用 v1.8（见 §3.9.3） |
| `web/server.py` | ⏳ 待改 | SSE、`/api/metrics`、`retention`、`human_baseline`、`export-scores` 接线 |
| `web/index.html` | ⏳ 待改 | 四阶段进度、人工基准徽标、改稿闭环摘要、不留历史文案 |
| `render.yaml` | ⏳ 待改 | 加 `RETENTION_NOTICE=ephemeral` |
| `Dockerfile` | ⏳ 待改 | **必须加 `COPY config ./config`**，否则容器内 `import rubric` 失败 |
| `VERSION` / `README.md` / `docs/CHANGELOG-v2.0.md` | ⏳ 待改 | 2.0.0 三件套（CHANGELOG 待建） |

#### 3.9.3 版本号决策（重要）

- `rubric.version` 与 `prompt_version` 保持 `v1.8`：本次只改「数字住在哪」，没改任何数值，历史分数可比。
- 如果 W2 真的调了阈值（例如长句 60→55），必须同时升 `rubric.version` 与 `prompt_version`，并接受「旧报告与新报告的分数不可直接比较」；`config_hash` 会自动变化，改版对比会退化为「只比文本」。
- 发布号 `2.0.0` 表示「工具链与可信度体系升级」，不表示评分规则变了；CHANGELOG 与 README 都要写这句，避免用户以为分数含义变了。
- Schema 一致性（AC-A1.6）先补断言、不动文件；`rewrites 2~6` 这类结构性数量是否进 rubric，见 §3.1.3 决策点。

#### 3.9.4 发布检查单

1. `python scripts/run_tests.py` 13 套件全绿。
2. `python scripts/benchmark.py --require-ready` 在 READY 数据上退出 0；在现有 8 份数据上退出 2（预期）。
3. `python scripts/qa_smoke.py --mock` 通过（含版本一致性）。
4. `.\run_web.ps1 -Mock` 手工走一遍：拖入 → 四阶段 → 报告 → 徽标 → 下载。
5. Docker 构建后启动，`GET /api/health` 200 且 `retention` 字段正确（验证 `config/` 已 COPY）。
6. README 更新：能力、测试规模、准确度口径（只能引用 READY 报告）。
7. `docs/CHANGELOG-v2.0.md`（待建）写入 AC-A1~A9 的完成状态。

#### 3.9.5 回滚

- 镜像回滚到 v1.10.1 镜像（`retention`、SSE 都不影响旧前端）。
- `ENABLE_SSE=0` 关闭流式；`/api/metrics` 无副作用可保留。
- 数据回滚不涉及：`config/`、`bench/`、`samples/eval/agg/` 都是代码/派生数据，不写用户数据。

## 4. 数据保存、导出和恢复（变化部分）

### 4.1 新增数据分级

| 级别 | 新增内容 | 规则 |
|---|---|---|
| 敏感（绝不入库） | 简历原文、标注文件（`annotations/`、`adjudication/`）、`samples/txt/` | 沿用 v1：`.gitignore` + `.dockerignore` 已排除；标注文件只含分数与 ≤120 字逐字摘录 |
| 一般（可入库） | `samples/eval/dataset-v2.json`（待建；路径+哈希+分层，无正文）、`bench/report.md`（待建）/`bench/report.json`（待建；只有分数与样本 id）、`samples/eval/agg/human-scores.json`（待建；只有哈希与聚合值）、`bench/heldout-access.jsonl`（待建；审计） | 入库前人工过一遍：不得出现手机号/邮箱/身份证/完整姓名 |
| 衍生（本地） | `reports/` 报告与历史、`outputs/*.json` 模型输出、`reports/_metrics.json`（待建） | 沿用 v1；`ephemeral` 模式不落盘 |

### 4.2 存储位置

| 数据 | 位置 | 生命周期 |
|---|---|---|
| 上传临时文件 | 系统临时目录 | 读完即删（v1 口径） |
| 页面图（`visual`） | 进程内存 | 30 分钟 |
| 脱敏正文 + 报告 Markdown/JSON | `REPORTS_DIR`（本地 `reports/`；公网 `/tmp/reports`） | `local` 永久；`ephemeral` 当次进程 |
| 历史索引 | `reports/.history.json` | 同上；`ephemeral` 不写 |
| 数据集/标注/聚合件/bench | 仓库 `samples/eval/`、`bench/` | 随版本入库 |
| 模型输出 | `outputs/*.json` | 永久（v1 口径，用于 A/B） |

### 4.3 隐私与入库自检

- `bench/report.md` 的「逐份」表只有 `sample_id`、切分、分数、结论，不含姓名/学校/公司；`sample_id` 用 `S01` 这类编号，不映射到真实身份（映射表在协调者本地）。
- `human-scores.json` 只有哈希与聚合值，泄露不出正文；哈希不加盐（简历正文相同则哈希相同，这是有意为之，用于「同一份」判定）。
- 提交前跑一遍敏感词扫描（待建脚本 `scripts/check_artifacts.py`，或手工 grep 手机号/邮箱正则）。
- `samples/txt/`、`samples/raw/` 永不入库，新检出仓库时测试只依赖 `samples/demo/` 与 `samples/fixtures/`（v1 已有约束，v2 不变）。

### 4.4 恢复与删除

| 场景 | `local` 模式 | `ephemeral` 模式 |
|---|---|---|
| 单份删除 | `DELETE /api/report?id=`，v1 口径 | 同左，删内存条目 |
| 全量删除 | 手动删 `reports/` | 重启进程即空 |
| 恢复历史 | 从 `reports/.history.json` 与报告文件恢复 | 无历史可恢复，页面写明 |
| 下载 | `.md` / `.docx` / `.json` 三形式，报告页当场下载 | 同左，必须当场下载 |

### 4.5 与改版对比的关系（容易踩的坑）

- 改版对比依赖「最近 40 份历史」。`ephemeral` 模式下历史只在当次进程，**用户刷新/隔天再来就失去对比能力**；这是 §3.8 决策的已知代价，页面文案要写「关掉页面后再打开，就无法对比上一版」。
- 不为此偷偷落盘：要跨天对比，就用本地运行或导出报告后手工对比。

## 5. 本版必须完成 / 明确不做 / 已知代价

### 5.1 v2.0 必须完成（状态 = 写作时快照）

| # | 项 | 状态 | 完成判据 |
|---|---|---|---|
| 1 | rubric 单一真源 | 部分 ✅ | 配置+解析器+三模块接线+21 条守卫测试已落地；Schema 断言（AC-A1.6）与 Dockerfile `COPY config` 待补 |
| 2 | 数据集 60 份 + 切分护栏 | 待做 | `dataset-v2.json` 通过校验；heldout 锁死；访问日志（AC-A2.*） |
| 3 | 双人盲评 | 待做 | 2 人 × 60 份、盲法通过、仲裁队列（AC-A3.*） |
| 4 | 一致性指标 | 部分 ✅ | `stats_metrics.py` 356 行 + 43 条测试 + benchmark 已算 12 项；缺 repeat 极差与仲裁后共识（AC-A4.6、A3.3） |
| 5 | benchmark | 部分 ✅ | `benchmark.py` 439 行可跑；首次生成 `bench/report.md`（AC-A4.1） |
| 6 | 报告页接真实指标 | 待做 | 聚合件 + 三态徽标 + 禁止措辞（AC-A5.*） |
| 7 | 改稿闭环 | 部分 ✅ | `check_applied` 四态与 Markdown 计数已有；接口结构化字段 + 页面摘要（AC-A6.*） |
| 8 | SSE + `/api/metrics` | 待做 | 四阶段事件、终态必达、指标字段（AC-A7.*） |
| 9 | 持久化决策 | 待做 | `RETENTION_NOTICE` + `/api/health.retention` + 三处文案（AC-A8.*） |
| 10 | 发布 2.0.0 | 待做 | 版本三处一致、13 套件全绿、CHANGELOG（AC-A9.*） |

### 5.2 v2 明确不做

见 §1.5 表（账号、在线编辑、多语言、批量上传、图表看板、换更强模型、对象存储、自动清理），每条都写了理由。

### 5.3 已知代价（v2 新增）

| 代价 | 说明 | 缓解 |
|---|---|---|
| 标注是一次性重投入 | 23~33 人时，且 2 名标注者要在 W1 锁定 | 分批（先 30 份 pilot）、锚点培训 30 分钟、每批 ≤10 份防疲劳 |
| heldout 用一次就废 | 18 份在最终验收后公开过，就不能再证明泛化；重采要再来一轮 | 冻结流程（§3.2.4）、只在 W4 跑一次 |
| 模型分 benchmark 需要批量跑 | 60 份 × 25k 输出 token × repeat 3 ≈ 4.5M 输出 token，按需分批 | 默认先用规则引擎（免费）；模型分只在最终验收前跑一次并缓存 `outputs/` |
| 公网版不留历史 | 跨天无法做改版对比，用户必须当场下载 | 页面明写（§2.5）；本地运行保留历史 |
| 改配置要重启服务 | 模块常量在 import 时求值，热改 `rubric.yaml` 不会刷新常量 | runbook 写明；`rubric.load` 按 mtime 缓存只对进程内后续 `num()` 生效 |
| 单一真源只覆盖阈值 | 结构性数量（`rewrites 2~6`、`number_audit 0~6`）仍在 schema，未进 rubric | §3.1.3 决策；要么补键，要么在 README 明确边界 |
| 13 套件变慢 | v1 约 7 秒，新增 rubric 21 条 + stats 43 条 | 仍远低于 CI 超时；以 `run_tests.py` 实测为准 |

## 6. 验收标准（AC-A1 ~ AC-A9）

> 编号用 `AC-A` 前缀，避免与 v1 的 `AC-1`…`AC-7` 撞号。每条都能被脚本或人工判「过/不过」；「体验流畅」「基本可用」不写进表里。
> 类型列：**自动** = 一条命令可判；**人工** = 需要人看/人操作并留下记录。
> 说明：本节提到的 `bench/report.md`、`bench/heldout-access.jsonl`、`samples/eval/dataset-v2.json`、`samples/eval/agg/human-scores.json`、`samples/eval/adjudication/queue.json`、`reports/_metrics.json`、`docs/CHANGELOG-v2.0.md` 在写作时都还不存在，属于 v2 交付物（待建）；AC 描述的是交付后应成立的判定。

### AC-A1 · rubric 单一真源

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A1.1 | 守卫测试可跑 | `python scripts/run_tests.py --suite rubric` | 退出码 0；`--list` 里有 `rubric` 套件；该套件报 21 条全过 | 自动 |
| AC-A1.2 | 配置驱动代码 | 把 `readability.long_sentence_chars` 改 60→55，同步把 prompt 的「60 字内」改「55 字内」，跑 rubric 套件 | `check_output.LONG_SENTENCE_MIN == 55`；套件仍全过 | 自动 |
| AC-A1.3 | 禁止字面量回退 | 把 `check_output.py` 的 `PAGES_CHARS` 改成 `PAGES_CHARS = 900`（字面量），跑 rubric 套件 | 套件红，失败信息含「这些阈值只能来自 config/rubric.yaml」 | 自动 |
| AC-A1.4 | prompt 不许落后 | 只把 `prompts/system-v1.8.md` 的「60 字内」改「40 字内」，不动配置，跑 rubric 套件 | 套件红，失败信息含「prompt 里必须出现「60 字内」」 | 自动 |
| AC-A1.5 | 加载期校验 | 用临时配置依次制造：重复键、未知行、权重合计 90、四档非降序、取不存在的键 | 五次都抛 `rubric.RubricError`；异常文本分别含行号或键路径或「100」或「verdict」 | 自动 |
| AC-A1.6 | Schema 一致性（待补） | 新增断言：`prompts/output-schema-v1.8.json` 的 `evidence.minLength/maxLength` == rubric 的 6/200；再故意把 schema 改成 5/200 | 正常时 rubric 套件全过；改坏后套件红并指出 schema 与配置不一致 | 自动 |

### AC-A2 · 数据集与切分护栏

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A2.1 | 数据集达标 | `python scripts/benchmark.py --dataset samples/eval/dataset-v2.json --predictor local`，看 `bench/report.json` 的 `readiness` | 数据集 60 份；`heldout_size == 18`；`reviewers` 含 2 人；`validate_dataset` 返回 0 条问题 | 自动 |
| AC-A2.2 | heldout 默认锁死 | `python scripts/benchmark.py --split heldout`（不加 `--allow-heldout`） | 退出码 2；stderr 含「heldout 已锁定」；不生成/不覆盖 `bench/report.md` | 自动 |
| AC-A2.3 | 同源不跨切分 | 造一份 `source_group` 相同、split 分别 dev/heldout 的数据集，跑 `human_eval.validate_dataset` | 返回问题列表，含该 `source_group` 与两个 `sample_id` | 自动 |
| AC-A2.4 | 访问留痕 | 带 `--allow-heldout --split heldout` 跑一次 | `bench/heldout-access.jsonl` 追加一行：时间、样本数 18、目的、`allow_heldout: true`；文件为空或缺失判不过 | 自动 |

### AC-A3 · 双人盲评与仲裁

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A3.1 | 双人覆盖 100% | 两名标注者各完成 60 份 → `human_eval.py validate` 两个文件 → `human_eval.py report` | 两份都 60/60 通过；报告显示 `reviewers == 2`、`samples_compared == 60`；缺 1 份时 benchmark `readiness.reasons` 含「已标注样本 59 份 < 目标 60 份」 | 自动 |
| AC-A3.2 | 盲法成立 | 检查标注包与标注文件 | 包内无模型分/档位/门禁/报告；文件不含 `model_score`/`overall_score`/`verdict_model` 字段；两人顺序不同；`blind_to_model == true` 且 `reviewer_type == independent_human` | 自动 |
| AC-A3.3 | 仲裁覆盖 | 统计两份标注总分差 >15 的样本 | 全部出现在 `samples/eval/adjudication/queue.json`；每条有 `arbitrator_score` 与 `arbitrator_note`；`human_eval.consensus` 对这些样本返回仲裁分（不是两人均值） | 自动 |
| AC-A3.4 | 一致性可复算 | 用 `human_eval.py report --json-out` 的数字与按附录 B 公式手算两个标注文件的结果对比 | 总分 MAE、Spearman、5 维 QWK、档位完全一致率四项误差 ≤0.1 | 自动 |

### AC-A4 · 指标与 benchmark

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A4.1 | 一条命令出报告 | 仓库根执行 `python scripts/benchmark.py` | 退出码 0；生成 `bench/report.md`（待建）与 `bench/report.json`（待建）；报告含「## 状态：NOT_READY」「\| MAE \|」行与「口径与禁忌」段 | 自动 |
| AC-A4.2 | 数据不足不给数字 | 在现有 8 份、0 份标注上跑默认命令 | `metrics.mae == null`，报告显示「—」；reasons 至少含「数据集 8 份 < 目标 60 份」「独立标注者 0 人 < 2 人」「heldout 0 份 < 目标 18 份」；不得出现任何具体 MAE 数值 | 自动 |
| AC-A4.3 | 未就绪不许过 CI | `python scripts/benchmark.py --require-ready`（NOT_READY 数据）；再用 fixtures 造 READY 数据跑一次 | NOT_READY 时退出码 2；READY 时退出码 0 | 自动 |
| AC-A4.4 | 统计边界 | 跑 `python scripts/run_tests.py --suite stats`；另手工调用 `stats_metrics.mae([])` | stats 套件 43 条全过；`mae([])` 抛 `ValueError`；`pearson([5],[7])` 返回 `None` 而非 0 | 自动 |
| AC-A4.5 | READY 门槛与效果 | 在 60 份 + 双人标注 + heldout 上：`python scripts/benchmark.py --allow-heldout --split heldout --require-ready` | 退出码 0；`readiness.ready == true`；`mae ≤ 8`、`kappa_verdict ≥ 0.6`；报告同时列出门槛对照与 95% CI | 自动 |
| AC-A4.6 | repeat=3 极差（待补） | 同份简历同配置跑 3 次（12~20 份分层样本），对比 3 个总分 | 每份 `max − min ≤ 6`；benchmark 报告出现「重复极差」小节；现有 `spread`（机器分−人工分差值分布）改名后不与之混淆 | 自动 |

### AC-A5 · 报告页接真实指标

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A5.1 | 无标注显示 not_ready | 用一份不在 `human-scores.json` 哈希表里的简历走完整评审 | 响应 `human_baseline.status == "not_ready"`；页面显示「本份暂无人工基准（not_ready）」；不显示任何差值 | 自动 |
| AC-A5.2 | 有标注显示差值 | 用聚合件里 `reviewer_count == 2` 的样本评审 | `status == "ready"`；`diff == abs(model_score − human_mean)`（保留 1 位小数）；页面文案含 n=2 与「单个样本不能推总体」 | 自动 |
| AC-A5.3 | 禁止「准确率」措辞 | 对页面 HTML、`/api/report` 响应、下载的 `.md` 做关键词扫描 | 「准确率」「准确度」「正确率」命中 0；「准确度」只允许出现在 `bench/report.md` 的口径说明里，不允许出现在 Web 报告与下载件 | 自动 |
| AC-A5.4 | 口径可追溯 | 看徽标脚注与 `/api/health` | 脚注给 n、口径说明链接、聚合件生成时间；`/api/health.human_scores.generated_at` 与聚合件文件一致 | 自动 |

### AC-A6 · 改稿闭环

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A6.1 | 结构化字段 | 对一份有上一版历史的简历调 `/api/review` | `compare.applied_counts` 含 `applied`/`untouched`/`changed`/`unknown` 四键与 `total`；四键之和 == `total` == 上一版建议条数（2~6） | 自动 |
| AC-A6.2 | 页面摘要 | 打开报告页「和上一版比」区块 | 显示「上次 N 条建议：A 条看起来已改 / B 条原句还在 / C 条改成了别的写法 / D 条无法判断」；A+B+C+D=N；unknown 不并入 untouched | 自动 + 人工 |
| AC-A6.3 | 四态各有样本 | 构造新文本：原句微调（applied）、原句保留（untouched）、改写（changed）、截断/空文本（unknown） | 四种状态各 ≥1 条；接口计数与页面数字一致；`unknown` 文案为「无法判断」 | 自动 |
| AC-A6.4 | 真实用户闭环 | 3 名真实用户各自「按一条建议改一处 → 再评」 | 每人留下 `review_id` 对（改前/改后）与一句「改了哪条」的记录；再评结果能定位到该条建议；3 人全部完成才算过（对应 §1.3 第 9 条） | 人工 |

### AC-A7 · SSE 与观测

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A7.1 | 事件顺序 | POST `/api/review/start` 拿 `job_id`，再连 `/api/review/stream` | 依次收到 `stage` 的 `desensitize → format → check → render`（顺序断言）；最后收到唯一 `done`，带 `review_id`/`score`/`report` | 自动 |
| AC-A7.2 | 阶段耗时真实 | mock 模式跑一份；真实模式跑 3 份取 p95 | 四阶段 `elapsed_ms` 之和与 `done.elapsed_ms` 差 ≤1000ms；mock 总耗时 ≤5 秒；真实 p95 ≤110 秒 | 自动 |
| AC-A7.3 | 失败可见 | 无 key 调 `/api/review/start`；再故意让模型全失败 | 收到 `error` 事件，`code` 分别为 `LLM_NO_KEY` 与 `REVIEW_VALIDATION_FAILED`；不返回半份报告；重连拿到同一终态 | 自动 |
| AC-A7.4 | metrics 字段 | `GET /api/metrics` | 200；含 `requests_total`、`review_ok`、`review_failed`、`fail_rate`、`refusal_rate`、`latency_ms.p50/p90/p95`、`stage_ms`、`tokens.input/output`、`uptime_s`、`version`；`requests_total == review_ok + review_failed` | 自动 |
| AC-A7.5 | 隐私边界 | 对 metrics 响应、服务端日志、`reports/_metrics.json` 扫描 | 不含简历正文、文件名、`api_key`、IP、`review_id`；只含聚合值 | 自动 |

### AC-A8 · 持久化

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A8.1 | 公网临时保留 | 按 2.0.0 的 `render.yaml` 启动，`GET /api/health` | `retention == "ephemeral"`；首页与历史空态出现「公网版不留历史」文案；`reports/` 不新增文件 | 自动 + 人工 |
| AC-A8.2 | 本地保留不变 | 本地 `.\run_web.ps1`（不设 `RETENTION_NOTICE`） | `retention == "local"`；不显示「不留历史」；历史可读、可删；`test_history.py` 9 条仍全过 | 自动 |
| AC-A8.3 | 不留历史不误导 | ephemeral 模式下重启服务再看历史抽屉 | 历史为空且带 `notice` 文案；报告页下载入口仍在，能当场导出 `.md`/`.docx`/`.json` | 自动 + 人工 |

### AC-A9 · 发布 2.0.0

| 编号 | 场景 | 步骤 | 预期 | 类型 |
|---|---|---|---|---|
| AC-A9.1 | 版本三处一致 | 发布后 `GET /api/health`；`python scripts/qa_smoke.py --mock` | `VERSION == "2.0.0"`；`health.version == "2.0.0"`；qa_smoke 版本一致性检查通过 | 自动 |
| AC-A9.2 | 全量回归 | `python scripts/run_tests.py` | 退出码 0；13 个套件全过；rubric 报 21 条、stats 报 43 条；断言总数不低于 266（202 + 21 + 43，selftest 不计） | 自动 |
| AC-A9.3 | 质量门禁不回退 | 对 60 份数据集产物跑 `check_output.evaluate_dir` + 跑 `run_tests` 的质量门禁套件 | 引用可追溯率 100%；编造数字 0；无图谈排版 0；展示力漏报 0；质量门禁套件全过 | 自动 |
| AC-A9.4 | 发布物齐备 | 检查仓库与镜像 | `docs/CHANGELOG-v2.0.md` 存在且逐条对应 AC-A1~A9；README 头部版本与「准确度口径」已更新；镜像内 `config/rubric.yaml` 存在且 `/api/health` 200；READY 数据上 `benchmark.py --require-ready` 退出 0 | 自动 |

## 7. 风险与依赖

### 7.1 最大风险：真实简历来源

现状：只有 8 份人工转写（`samples/txt/`，与 `dataset-v1.json` 的 S01~S08 对应，全部 `development`），用户手上没有 60 份。

| 渠道 | 预计可得 | 许可 / 隐私 | 状态 |
|---|---|---|---|
| 自己 + 同学/室友人工转写 | 10~15 份 | 本人同意；脱敏后入库；`consent.channel="self_friend"` | 已有 8 份 |
| 校园社群 / 招聘群征集（海报 + 知情同意 + 可匿名 + 只交脱敏文本） | 20~30 份 | 需逐份记录 `allow_annotation` / `allow_external`；未同意不得外发 | 未启动，W1 必须启动 |
| 公开数据集 / GitHub 简历 | 0~10 份 | 逐份核对许可；许可不明的一律不进 eval（可做格式/OCR 测试） | 不推荐，仅兜底 |
| 购买 / 爬取简历 | 0 | 侵犯隐私、许可不明 | **红线，禁止** |

拿不到 60 份时的降级（按实际 n 报，不调低 `eval.dataset_target`）：

| 档位 | 实际 n | 动作 | 对外口径 |
|---|---|---|---|
| T1 | 30~44 份 | 按人工评测方案的「试点 ≥30 份」跑完双人盲评；benchmark 永远 NOT_READY | 只能写「试点数据上 MAE=X（n=30），不足以代表总体」 |
| T2 | 45~59 份 | 里程碑顺延；继续采集，不发布一致性数字 | 「数据集建设中」 |
| T3 | <30 份 | 2.0.0 降级为「工具版」：rubric、benchmark、SSE、metrics 照常上线，报告页全量 `not_ready` | 只发布工具能力，不发布任何准确度相关数字 |
| 红线 | 任意 | 不伪造 heldout、不用 8 份凑 60、不拿 development 数字冒充 heldout、不改 `eval.dataset_target` 来「达标」 | — |

### 7.2 第二风险：双人标注的时间成本

| 项 | 估算 |
|---|---|
| 标注者人数 | 2 人（现状 0/2，W1 必须锁定，否则 W2 无法开工） |
| 首轮 | 60 份 × 2 人 × 8~12 分钟 = 16~24 人时 |
| 培训与试标 | 2 人 × 1 小时 = 2 人时 |
| 仲裁 | 12~18 份（预估分歧率 20~30%）× 5~8 分钟 = 1~2.5 人时 |
| 协调与工具 | 4 人时 |
| 合计 | 约 23~33 人时，集中在 W2 |

降级：只招到 1 人 → κ 无法计算、`agreement_metrics` 返回空、benchmark 永远 NOT_READY；此时只能写「单人一致性」参考，不能写「双人盲评」。

### 7.3 依赖与前置

| 依赖 | 影响 | 状态 |
|---|---|---|
| `Dockerfile` 增加 `COPY config ./config` | 不加则容器内 `import rubric` 直接失败（当前 COPY 只有 VERSION/web/scripts/prompts/samples） | **P0 待改** |
| `render.yaml` 增加 `RETENTION_NOTICE=ephemeral` | 公网「不留历史」开关 | 待改 |
| CI `.github/workflows/deploy-cloudflare.yml` | 已跑 `run_tests.py`，新增 rubric/stats 套件自动纳入；无需改 | 已就绪 |
| `requirements.txt` | **不需要**新增依赖：rubric 解析器零依赖、stats 纯标准库 | 已就绪 |
| 2 名标注者 + 1 名仲裁者 | W2 的关键路径 | 0/2 |
| 60 份真实简历 | W1~W2 的关键路径（§7.1） | 8/60 |
| 模型额度 | 最终验收 60 份 × repeat 3 × 约 25k 输出 token ≈ 4.5M 输出 token；规则引擎免费 | 按需 |
| prompts / schema v2.0 | 仅当 W2 真改阈值才需要新建；否则沿用 v1.8 | 待决策（§3.9.3） |

### 7.4 风险登记

| 风险 | 概率 | 影响 | 触发条件 / 预警 | 缓解 |
|---|---|---|---|---|
| 简历来源不足 | 高 | 高 | W1 末 <20 份 | §7.1 三档降级；先跑 30 份 pilot |
| 标注者招不到或中途退出 | 中 | 高 | W1 末仍 0 人 | 提前 2 周约人；备选 3 人；先付定金/人情账 |
| 标注者不盲（偷看模型报告） | 中 | 高 | 标注文件出现模型字段或时间线重叠 | 独立目录 + 不转发 + 首轮冻结后才发模型输出 |
| heldout 被调参污染 | 中 | 高 | 访问日志出现非验收用途 | 代码锁 + 日志 + 冻结流程；污染即作废重采 |
| 指标被读成「准确率」 | 高 | 中 | 页面/README 出现该词 | AC-A5.3 扫词；统一文案「与人工均值的差」 |
| 容器缺 `config/` 导致线上 500 | 高 | 高 | 镜像构建后 `/api/health` 非 200 | 发布检查单第 5 步；AC-A9.4 |
| SSE 被反代缓冲 | 中 | 中 | 事件一次性到达或延迟 | `X-Accel-Buffering: no` + 15 秒心跳；`ENABLE_SSE=0` 回滚 |
| W4 集中交付撞车 | 中 | 中 | W3 末 AC-A5/A6 未过 | 可先发 2.0.0（工具版），指标保持 NOT_READY；不为了发版改口径 |

## 8. 里程碑（1 周粒度）

| 周 | 目标 | 交付物 | 出口条件（全部可判） |
|---|---|---|---|
| W1 | 地基：rubric 收尾 + benchmark 首次出报告 + 数据集起步 + 锁定标注者 | Schema 断言（AC-A1.6）、`COPY config`、`bench/report.md`（待建）首次生成、数据集 ≥30 份、2 名标注者口头确认 | AC-A1.1~A1.6 全过；AC-A4.1~A4.4 全过；`bench/report.md` 状态 NOT_READY 且 reasons 正确；数据集 ≥30 份且有 consent 记录 |
| W2 | 双人盲评 + heldout 锁死 | 60 份 `dataset-v2.json`（42 dev + 18 heldout）、两份 60/60 标注、仲裁队列、一致性数字 | AC-A2.1~A2.4、AC-A3.1~A3.4 全过；development 上 MAE/κ 可算（readiness 仍 NOT_READY 属预期） |
| W3 | 闭环 + 报告页 + 稳定性 | `/api/review` 的 `compare.applied_counts` 落接口与页面、`human-scores.json` 聚合件、三态徽标、repeat=3 稳定性数据 | AC-A5.1~A5.4、AC-A6.1~A6.3、AC-A4.6 全过；UI 无「准确率」字样 |
| W4 | 流式 + 观测 + 持久化 + 发布 | SSE 端点、`/api/metrics`、`RETENTION_NOTICE` 与三处文案、heldout 最终验收、2.0.0 发布物 | AC-A7.*、AC-A8.*、AC-A9.* 全过；AC-A4.5 在 heldout 上过；README/CHANGELOG 更新 |

排期备注：4 周是乐观值。W1 的数据集与 W2 的标注都是外部依赖，任一滑期则 W3/W4 顺延，或按 §7.1 T3 降级为「工具版」发布——不压缩验收、不改门槛。

## 附录 A · 错误码与文案（v2 新增）

| 错误码 | HTTP / 退出码 | 触发 | 文案 |
|---|---|---|---|
| `RUBRIC_INVALID` | 503（health）/ 启动失败 | rubric 文件缺失、解析失败、权重≠100、四档非降序 | 「评分规范配置有误，服务未启动；请检查 config/rubric.yaml」 |
| `RUBRIC_KEY_MISSING` | 同上 | 代码取了配置里不存在的键 | 异常文本带完整键路径（开发期直接抛，不进用户文案） |
| `HOLDOUT_LOCKED` | `benchmark.py` 退出码 2 | 未加 `--allow-heldout` 却读 heldout/all | 「拒绝读取 heldout 集…确实要做最终验收，请显式加 --allow-heldout」 |
| `BENCH_NOT_READY` | `benchmark.py --require-ready` 退出码 2 | 就绪条件未满足 | 报告列出 reasons；不是异常，是门禁 |
| `SSE_JOB_NOT_FOUND` | 404 | `job_id` 不存在 | 「这次评审已不存在，请重新提交」 |
| `SSE_JOB_EXPIRED` | 410 | job 超过 TTL 或服务重启 | 「服务重启了，请重试」 |
| `RETENTION_EPHEMERAL` | 非错误，`/api/health.retention` | 公网临时保留模式 | 「公网版不留历史」 |
| `LLM_NO_KEY` / `BAD_MODE` / `REVIEW_VALIDATION_FAILED` / `LLM_TIMEOUT` | 沿用 v1 | 沿用 v1 | 沿用 `docs/prd.md` 附录 A，不改文案 |

## 附录 B · 质量指标定义（唯一口径）

| 指标 | 公式 / 口径 | 实现 | 达标线 |
|---|---|---|---|
| 人工共识分 | 每人按 `round((role_fit×0.25 + impact×0.25 + evidence_quality×0.20 + structure×0.15 + competitiveness×0.15) × 20)`；两人差 ≤15 取均值，>15 取仲裁分 | `human_eval._weighted_score` / `consensus`（仲裁待补） | 必需 |
| MAE | `mean(abs(机器总分 − 人工共识分))` | `stats_metrics.mae` | ≤8 |
| MAE 95% CI | 2000 次自助重采样，2.5%~97.5% | `stats_metrics.bootstrap_ci` | 必须随 MAE 一起报 |
| RMSE | `sqrt(mean((y−ŷ)²))` | `stats_metrics.rmse` | 参考 |
| Pearson | 总分线性相关，夹 [-1, 1] | `stats_metrics.pearson` | 参考 |
| Spearman | 秩相关；并列取平均秩 | `stats_metrics.spearman` | 参考 |
| Cohen's κ（四档） | 名义一致性：strong_pass / pass / borderline / reject | `stats_metrics.cohen_kappa` | ≥0.6（解释阈值见人工评测方案：0.4~0.6 一般、0.6~0.8 较好） |
| QWK | 0~100 分二次加权 κ | `stats_metrics.quadratic_weighted_kappa` | 参考 |
| ±5 / ±10 | 与共识分差 ≤5 / ≤10 分的样本比例 | `stats_metrics.agreement_within` | 参考，报比例 |
| 两关一致率 | `gate_hr` / `gate_interview` 人工与机器一致的比例 | `benchmark.compute_metrics` | 参考 |
| repeat 极差 | 同份同配置 3 次总分的 max−min | 待补 | 每份 ≤6 |
| 各维度 QWK | 5 个维度各自与人工的一致性 | `human_eval.agreement_metrics` | 5 维全报 |
| 引用可追溯率 | 逐字命中引用数 / 总引用数 | `check_output.evaluate_dir` | 100% |
| 编造数字 | `number_audit` 中未在原文出现的数字条数 | `check_output` | 0 |

**权威产物**：`bench/report.md`（待建）是唯一可以对外引用的数字来源；`reports/eval/human-eval-report.md` 是过程报告；两者冲突时报废重跑。

## 附录 C · 版本对应

| 代码版本 | prompt | schema | rubric | 说明 |
|---|---|---|---|---|
| v1.1 ~ v1.5 | v1.1 ~ v1.5 | 同号 | 无（阈值散在代码） | 三次 prompt 重写，权重 25/25/20/15/15 起步 |
| v1.6 ~ v1.7 | v1.6 / v1.7 | 同号 | 无 | v1.7 出过「prompt 60、脚本 40」的错 |
| v1.8 ~ v1.10.1 | v1.8 | v1.8 | 无 | v1.8 再犯一次同类错；v1.9/v1.10 只改交互与引擎，规则不变 |
| **2.0.0** | v1.8（不变） | v1.8（不变） | **v1.8 新增单一真源** | 数字住在 `config/rubric.yaml`，值未变，历史分数可比 |
| 2.x 若改阈值 | 新号 | 新号 | 升号 | prompt/schema/rubric 三处同升，历史分数不可比 |

### 踩过的坑（不要重犯）

1. 同一数字写在 prompt 与代码两处，改一处必漏——v1.7、v1.8 各漏一次；现在由 rubric 守卫测试兜住。
2. 推理模型不要设 `max_tokens`（会截断思维链）；关思考是 prompt 层的事。
3. Python 3.13 没有 `cgi` 模块，文件上传必须走 base64 JSON。
4. `localhost` 在 Windows 可能先解析到 IPv6，服务器要双栈监听。
5. mock 剧本先于权重写成 → 指标自我实现；旧 MAE=2.0 因此不可信。
6. 单人非盲 gold 不能当准确度；只能叫「探索性代理」。
7. 容器只 COPY 了 `web/scripts/prompts/samples`，新增 `config/` 不 COPY 就会线上 500（v2 新坑）。

## 附录 D · 一条命令速查

| 目的 | 命令 |
|---|---|
| 全量回归 | `python scripts/run_tests.py` |
| 只跑评分规范守卫 | `python scripts/run_tests.py --suite rubric` |
| 只跑统计内核 | `python scripts/run_tests.py --suite stats` |
| 基准（规则引擎 + development） | `python scripts/benchmark.py` |
| 基准（已有模型输出） | `python scripts/benchmark.py --predictor outputs --split development` |
| 最终验收（heldout，锁必须显式开） | `python scripts/benchmark.py --allow-heldout --split heldout --require-ready` |
| 启动标注 | `python scripts/human_eval.py init --dataset samples/eval/dataset-v2.json --reviewer R01 --out samples/eval/annotations/R01.json` |
| 校验标注 | `python scripts/human_eval.py validate --dataset samples/eval/dataset-v2.json --annotations samples/eval/annotations` |
| 一致性报告 | `python scripts/human_eval.py report --dataset samples/eval/dataset-v2.json --annotations samples/eval/annotations --out reports/eval/human-eval-report.md` |
| 导出报告页聚合件（待建） | `python scripts/human_eval.py export-scores --dataset samples/eval/dataset-v2.json --annotations samples/eval/annotations --out samples/eval/agg/human-scores.json` |
| 本地网站 | `.\run_web.ps1`（`-Mock` 不花钱） |

---

本 PRD 是 **v2.0-draft 规划稿**。写作时已落地的组件见 §5.1 状态列；任何数字冲突，以 `config/rubric.yaml`、`bench/report.md`（待建）与 `python scripts/run_tests.py` 的输出为准。


