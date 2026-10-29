# v1.16.0（2026-10-29）

一句话：**发出去的稿子里要有锚点，否则两个人打的分不是同一把尺子。**

## 起因

v1.15 能把稿子干净地发出去了，但稿子里只写了五个维度名。真实情况是：
「impact 3 分」在一个人脑子里是「完成了 12 个接口」，在另一个人脑子里是「P99 降了 5 倍」。
两人都觉得自己按标准打的，算出来 κ 很低——那个 κ 量的是两人各自的标准，不是模型的能力。

## 加了什么

### 1. `docs/盲评锚点卡.md`（给标注者的一页纸）

- 五个维度 × 1/3/5 三档，每档配**具体例子**（同一件事的不同写法：1 分长什么样、5 分长什么样）
- 每一维单独写「最容易打错的地方」：把「学这个专业」当匹配度、把「工作量大」当影响、
  把「名词多」当深度、把「学校好/公司大」当竞争力
- 两道关写成 if-then：「role_fit ≤ 2 或整份没有一个可验证结果 → 初筛不过」
- 五个「不要」：不要因为排版好看给高分、不要补全他没写的数字、不要一次评超过 10 份、
  不要看别人分数再改自己、不要写改进建议（一写建议，分数就滑向「改完的样子」）

### 2. 稿子自动带锚点摘要

`human_eval.py blind` 生成的 `*-盲评稿.md` 末尾自动附：
五维 1/3/5 锚点表 + 长句 60 字口径 + 两关判定 + 「只判断现状不写建议」+ 指向锚点卡的路径。

理由很实际：**稿子会被单独转发**（微信发过去、手机上看），锚点不在稿子里就等于没发。

## 验证

- `scripts/test_blind_annotation.py` 16 项（原 14）：新增「稿子里要有锚点表」「锚点卡必须真实存在，
  且包含五个维度与那条最容易被违反的规则」——后者防的是「稿子里指向一个不存在的文件」
- 全仓 `python scripts/run_tests.py` → **20 套件 374 断言全绿**

# v1.15.0（2026-10-29）

一句话：**把「双人盲评」从一句承诺变成一条命令。**

## 起因

v2.0 A 路线第③项（双人盲评 + 一致性指标）卡了两天，卡住的不是代码而是流程：数据集 8/60、标注者 0/2。
想请同学来评，就必须先回答三个问题——他看到的稿子里会不会泄露模型分数？两个人拿到的顺序能不能一样？
两人吵起来（分差 20 分）时怎么发现？这三件事以前全靠我口头保证，而口头保证不是工具。

## 加了什么

### 1. `human_eval.py blind`：发出去的稿子是干净的

```bash
python scripts/human_eval.py blind --reviewer R01 --out samples/eval/handouts
```

产出两个文件：

| 文件 | 内容 | 为什么 |
|---|---|---|
| `R01-盲评稿.md` | 简历正文 + 盲号（`ANON-T001`）+ 打分表 | 给人读的。**没有内部编号 S01，没有模型分数，没有 `model_output` 路径** |
| `R01-待填.json` | 与稿子同序的骨架，维度全 `null` | 给机器读的。顺序必须与稿子一致，否则填完对不上号 |

两条设计：

- **顺序按 `hash(seed + blind_id)` 排**：同一个标注者每次拿到相同顺序（可以分几天做、中断了接着来），
  不同标注者顺序不同（两人不能互相抄）。
- **没有正文的样本两边都剔除**（稿子里标「已跳过」）。这是本轮修出来的真 bug：原来只从稿子剔除、
  待填表里仍留着那一项，标注者要么凭记忆瞎填、要么空着交，两种都会把脏数据混进金标。

### 2. `human_eval.py arbitration`：分差要能指到具体维度

```bash
python scripts/human_eval.py arbitration --dataset samples/eval/dataset-v1.json \
    --annotations samples/eval/annotations/ --out bench/arbitration.md
```

输出三栏：**需要第三人仲裁**（两人加权总分差 > `rubric.eval.arbitration_threshold`，默认 15）/
**只有一人评过** / **还没人评**。

每行多一列「差在哪」——列出差 ≥1 档的维度。理由：分差 20 分只说「差多少」，而
`role_fit 1 vs 5` 这种维度差才说「差在哪」，才是能拿去对齐标准（或重打）的信息。

单位换算写清楚：单维差 1 档 = 该维权重 × 4 分（role_fit 25% → 5 分，evidence 20% → 4 分，structure 15% → 3 分），
五维各差 1 档 = 20 分 > 15 —— 也就是说**系统性的理解差异一定会进仲裁**，不会被平均掉。

## 验证

- `scripts/test_blind_annotation.py` 14 项：盲号不泄露、模型输出不出现在稿里、待填表顺序与稿一致、
  同一标注者顺序稳定、两人顺序不同、缺失正文两侧都剔除、`--limit` 生效、分差 20 进仲裁、差 10 判一致、
  单维差 1 档能指到维度、阈值来自 rubric、markdown 能渲染冲突行
- 真实数据跑通：8 份 → `samples/eval/handouts/R01-盲评稿.md`（8 份全在，无模型分数）
- 全仓 `python scripts/run_tests.py` → **20 套件 372 断言全绿**

## 仍然没变的事实

数据集还是 8 份、标注者还是 0 人。这套工具只保证「发出去的稿子是干净的、收回来能发现分歧」，
**不代表已经有了一致性数据**。所以报告页仍然显示「未验证」，benchmark 仍然输出 `NOT_READY`。

# v1.14.0（2026-10-29）

> 一句话：**进度必须是真的，指标必须能解释自己。**
> 等 40 秒的时候，用户要知道「现在在哪一步」；出问题时，我要知道「慢在哪一步」。

## 起因

v2.0 A 路线的第 6 项：SSE 流式 + `/api/metrics`。

之前一次评审 15~60 秒，页面只有一个转圈。用户分不清是"在算"还是"卡死了"，
于是一半人刷新页面（连接断了，那次调用白花钱），另一半人以为坏了。

同时作为开发者，我只有一个总耗时，看不出是脱敏慢、还是模型慢——这直接决定我该优化哪里。

## 改了什么

### 1. 四阶段流式（prd-v2 §3.7）

| 阶段 | 页面文案 | 实际发生的动作 |
|---|---|---|
| `desensitize` | 正在脱敏 | 手机号/邮箱/身份证替换 |
| `format` | 正在读格式 | 复用 `/api/extract` 已判定的格式结果 |
| `check` | 正在逐项检验 | 模型调用 + 19 字段校验 + 三档门禁（`repeat` 在这段串行） |
| `render` | 正在出报告 | 组装报告、写盘、算改版对比 |

- `POST /api/review/start` 立刻返回 202 + `job_id`，`GET /api/review/stream?job_id=` 推
  `text/event-stream`；心跳 15 秒一次 `: ping`。
- 事件严格 1→2→3→4，不跳段；`done`/`error` 只有一个且必达。重连时若已完成直接补发终态，
  **不重放历史 stage**（否则前端把"已用 12 秒"累加成 24 秒）。
- 同步接口 `POST /api/review` 一字不改：CLI、`qa_smoke.py`、老前端都还用它。
  `ENABLE_SSE=0` 可一键退回；前端先探 `/api/health` 的 `sse` 字段，没有就走旧路径。

### 2. 前端四阶段进度灯

「① 脱敏 ② 读格式 ③ 逐项检验 ④ 出报告」四个小圆点：未开始灰、当前高亮、完成打勾。
**没有假进度条**——阶段只能由服务端真实事件点亮，任何"按时间推进百分比"的写法都被测试禁止。

### 3. `/api/metrics` 的请求量与阶段耗时

`requests_total` / `review_ok` / `review_failed` / `fail_rate` / `refusal_rate` /
`latency_ms{p50,p90,p95,max}` / `stage_ms{每阶段 p50,p95,n}` / `tokens{prompt,completion,calls}`。

三条口径写进代码注释，因为它们最容易被误读：

- **没有样本给 `None` 不给 0**：`fail_rate=0` 会被读成"从来没失败过"，
  恰好和事实相反（还没人用过）。
- **`refusal_rate` 是 `fail_rate` 的子集**：只统计被质量门禁拒收的那类。
  页面脚注必须写清，否则"拒收率高"会被当成"服务不稳定"。
- **只记聚合量**：绝不保存正文、文件名、`api_key`、IP、`review_id`、模型原始响应。

### 4. 阶段耗时记在外壳里

`api_review` 有七八个返回点，逐个写计时必然漏一个。所以拆成
`api_review`（计时外壳）+ `_api_review_impl`（业务），`try/finally` 收尾最后一段：
少一段的指标比没有指标更容易被误读。

## 验证

- 19 套件 **358 条断言**全绿（新增 `sse` 套件 23 条）
- `qa_smoke.py` 61/61
- 本地 headless Chrome 截图确认：进度灯在"开始评审"下方、四段依次点亮、打通时全部打勾
- 公网 `/api/metrics` 实测含 `stage_ms`（四段齐全，无样本段为 `null`）与 `tokens`（规则引擎模式下为 `null`）

## 文件

| 文件 | 改动 |
|---|---|
| `web/server.py` | job 注册表 / 四阶段推送 / SSE 端点 / 指标累加 / 计时外壳 |
| `web/index.html` | 进度灯 + EventSource 接入 + 失败退回同步 |
| `scripts/test_sse.py` | 新增 23 条（含"不许假进度"的静态断言） |
| `scripts/run_eval.py` | `USAGE_TOTALS` 用量累加（跨线程加锁） |

# v1.13.0（2026-10-29）

> 一句话：**公网版不留历史**这件事，必须写在脸上，而不是等用户重启后发现报告没了。

## 起因

v2.0 A 路线的第 7 项：持久化决策。

现状是自相矛盾的：`render.yaml` 把 `REPORTS_DIR` 指到 `/tmp/reports`（免费实例的临时盘，重启即清空），
而页面左侧有一个「历史报告」抽屉，看起来像个正常的档案柜。用户评完三份、第二天回来点开，
看到的是「还没有记录」——他会以为是自己操作错了，而不是我们没保存。

平台不给持久盘不是我们的错；**让界面暗示「存了」，是**。

## 决策：方案 B —— 明说不留，而不是假装能留

三个方案里选 B：

| 方案 | 做法 | 代价 |
|---|---|---|
| A 上持久盘 | Render 付费盘/对象存储 | 要钱，且免费用户一样没有，等于把问题推给预算 |
| **B 明说不留（选中）** | `RETENTION_NOTICE=ephemeral`，界面三处写明，不写持久目录 | 少了「历史」这个卖点，但它是假的卖点 |
| C 继续假装 | 什么都不改 | 每次重启都在骗用户一次 |

## 改了什么

### 1. `web/server.py`：新增留存策略开关

```python
RETENTION_MODES = ("local", "ephemeral")
RETENTION = _retention_mode(os.environ.get("RETENTION_NOTICE"))   # 默认 local
EPHEMERAL = RETENTION == "ephemeral"
```

- `ephemeral` 时 `REPORTS` 指向 `tempfile.mkdtemp(prefix="rr-ephemeral-")`，并 `atexit.register(shutil.rmtree, ...)`：
  **不写 `REPORTS_DIR`**，进程退出即删（否则「不留历史」会变成「留在 /tmp 等人捡」）；
- `_retention_mode()` 里写着方向：**拼错一律退回 `local`**。`ephemral` 这种笔误绝不能悄悄切成
  「不保存」——丢数据的方向必须保守，同时启动横幅把它喊出来；
- `/api/health` 增 `retention` / `retention_notice`，`/api/history` 增 `retention` / `notice`；
- 启动横幅打印留存策略（`local` 写明目录，`ephemeral` 写明「重启即清空」）。

### 2. `web/index.html`：文案随状态改，而不是写死

- 隐私行（侧栏）：`local` 是「本地报告在 `reports/`」；`ephemeral` 是「报告只存活于本次进程，重启即清空
  （公网版不留档）」；
- 结果页说明：`ephemeral` 换成「报告只存在于当前运行进程…需要留档请当场点『下载』保存到本机」；
- 历史抽屉：`ephemeral` 且有记录时，列表顶部加一行灰字提示；无记录时提示语前面也带上这句实话。

### 3. `render.yaml`：`RETENTION_NOTICE=ephemeral`

### 4. 测试 `scripts/test_retention.py`（8 项）

- 解析：默认/显式值/大写空格归一到 `local`/`ephemeral`；**五个拼错值全部落回 `local`**；
- 接口：`local` 不出现 `notice` 键（不加噪音）；`ephemeral` 的文案必须含「重启」「清空」；
- 进程级（起真子进程）：`ephemeral` 下 `REPORTS` 不等于配置的 `REPORTS_DIR`、配置目录一个字节都没写、
  临时目录在进程退出后**确实不存在**；`local` 下用的就是配置目录且目录保留；拼错值会打印「不认识」。

## 验证

```
[ PASS  ] retention           0.9s  断言 8/8
全部通过：18/18 个套件，335 条断言。
```

公网部署后 `GET /api/health` 应为 `"retention":"ephemeral"`（若仍是 `local`，说明该服务的环境变量没跟着
blueprint 走，需要在 Render 控制台手动加一条 `RETENTION_NOTICE=ephemeral`）。

## 顺带

- 版本 `1.12.0 → 1.13.0`（`VERSION` / `package.json` / `package-lock.json` 三处一致，`VERSION` 不许带 BOM）；
- README 测试数字同步 18/335，新增「v1.13：公网版不留历史，写在脸上」一节。

---

# v1.12.0（2026-10-29）

> 一句话：把「我们的建议被采纳了多少」也变成一个**要么有分母、要么没有数**的指标。

## 起因

v2.0 A 路线的第 5 项：改稿闭环落实率。

`scripts/version_diff.py` 早在 v1.9 就会做三态判断（applied / untouched / changed / unknown），
但那个结果只活在**那一次的响应里**——算完就随页面消失了。这意味着：

- 无法回答「用户到底采纳了多少建议」，只能凭印象说「大家反馈挺好」；
- 更糟的是，一旦有人问起，最省事的做法就是抽几份报告估个百分比，而 3 次观察的 67%
  会被当成结论——这正是 v2.0 一直在反对的「把自我实现的值当准确率」。

所以这次不是改算法，是**把已有的判定持久化并汇总**，同时把口径钉死。

## 改了什么

### 1. 评审时落盘 `reports/<stem>.meta.json`

评审完成后除 `.md/.json/.txt` 之外，再写一个旁挂元数据：`engine`/`model`/`base_url`/`schema_version`/
`overall_score`/`verdict`/`compare`（含 `applied_counts` 与 `similarity`）。

为什么单独一个文件而不是塞进 `.json`：那个文件是**评分结果本身**，标注工具与基准脚本会直接当 doc 解析，
多出字段会让它们莫名报「多余字段」。旁挂文件是加法，不动既有契约。
写失败也不影响报告——报告才是用户要的东西。

### 2. 新脚本 `scripts/closure.py`（汇总 + 口径）

```
python scripts/closure.py                 # 人读的 Markdown
python scripts/closure.py --json           # 给接口/脚本用
python scripts/closure.py --min-pairs 5    # 临时放宽门槛（报告里会写明）
```

口径四条，每条都是为了不骗人：

1. **只有「先后两版都被系统看过」才算一个样本**。单独一份报告不产生落实率。
2. **changed / unknown 不进分母**。它们只是「没信息」，塞进分母会把落实率压成假的偏低值。
3. **规则引擎（`local-rules`）的改写不进总落实率**。它给的是占位模板（`负责…«补：接口数»`），
   用户永远不会原样抄进去，逐字比对必然判 `untouched`——把它混进来，落实率会被结构性压到接近 0，
   那是**度量方法造成的假象**，不是产品没被用。汇总里按引擎分开列。
4. **可判定条数 < 门槛（默认 10，读 `config/rubric.yaml` 的 `eval.closure_min_pairs`）时，
   `apply_rate` 返回 `None`**，并写明原因是「只有 N 条」还是「没有可判定条目」。
   10 条以下 ±1 条就差 10 个点，给百分比等于给错觉。

还差一句必须说清的：这是**文本比对推理**，不是编辑日志，也不是用户自述。它只能说明
「新正文里出现了改后的说法」，不能证明用户是因为这句话才改的。

### 3. `GET /api/metrics` 增加 `closure` 块

与 `metrics`（分数准不准）并列——一个是「模型对不对」，一个是「产品有没有用」，都属于可信度。
同样是算不出来给 `null` + 原因，不填 0。

## 验证

- 新增 `scripts/test_closure.py` **16 项**：坏 JSON 跳过、负数计数夹到 0、时间倒序、
  未达门槛必须 `None`、changed/unknown 不进分母、**规则引擎记录被排除且给出原因**、
  只规则引擎时状态必须是 `insufficient_data`、CLI `--json` 端到端。
- `run_tests.py` → **17 套件 / 327 断言全绿**（原 16/311）。
- 本地端到端：同一份简历传两版（相似度 0.8661）→ `meta.json` 落盘、`compare` 有 `applied_counts`、
  `/api/metrics.closure` 返回 `insufficient_data` + 原因。实测发现并修正了一个真实偏差：
  第一版按「全部记录」汇总会把规则引擎的 9 条 untouched 算进去，落实率直接变 0。

## 文件

- 新：`scripts/closure.py`、`scripts/test_closure.py`、`config/rubric.yaml` 增 `eval.closure_min_pairs: 10`
- 改：`web/server.py`（写 `.meta.json`、`api_metrics` 增 `closure`、`_closure_summary()`）、
  `scripts/run_tests.py`、`VERSION`/`package.json`/`package-lock.json` → 1.12.0
# v1.11.0（2026-10-29）

> 一句话：一个 77 分，到底是「77 分」还是「某个规则算出来的 77 分」？这个区别以前只写在 README 里，现在写在界面上。

## 起因

v2.0 A 路线第 4 项「报告页接真实指标」。动手前先问了一个问题：过期的是报告里的数字，还是读者对数字的**信任**？

真实场景是这样的：室友拿这份报告去改简历，改了，又投了。他不会读 README 里那句「只有 8 份探索性样本」。
他看到的是一份写着「结论：77 / 100 可以投」的报告。**这是我们在替他承担一个我们没资格承担的判断。**

原来把答案放在 `docs/人工评测方案.md` 里，等于对用户隐身。所以改法不是「把免责声明写得更长」，而是：

- 让「这套分数线被验证过没有」变成产品的**一等状态**，页面顶部和每份报告都带着它；
- 没验证时就明写「未验证」，而不是把 MAE 一栏填个 8.0（那是自我实现的代理值，早已判死）。

## 改了什么

### 1. `GET /api/metrics`：可信度状态的唯一来源

```json
{"ok": true, "state": {"status": "not_ready", "verified": false, "n_verified": 0},
 "reasons": ["数据集 8/60 份", "独立打分者 0/2 人", "人工分 0/60 份", "heldout 集 0/18 份"]}
```

- 直接复用 `scripts/benchmark.py` 的 `readiness()` 与 `metrics_state()`，**不新写一套判定**（两套判定一定会漂移）
- 已就绪时给出 `verified=true` 与 `mae`/`ci`/`kappa`/`n`；未就绪时这些字段是 `null` 而**不是 0**——0 会被前端画成「误差 0，完美」
- 接口本身失败时前端按「未验证」渲染，不会静默省略（省略会被读成没问题）

### 2. 页面顶部「可信度条」

- 未验证：琥珀色，写明「这套分数线还没有被人工作答验证过：样本 8/60 份、独立打分者 0/2 人、人工分 0/60 份。分数可以当参照，但不要当成准确率。」+「为什么」展开四条原因
- 已验证：绿色，写明「已用人工标注验证：与人工分平均差 x.x 分（95% 区间 a~b），κ=0.xx，样本 n 份」
- 样式用 `[hidden]` 配合，**不在类里写 `display`**（v1.10.1 踩过：类里写 `display:flex` 会让 `hidden` 失效）

### 3. 每份 `.md` 报告加一行

放在**结论行下面**，不进文末注释区。理由：报告会被截图、转发、存进手机，是唯一会脱离网站单独流传的东西。

```
> 分数可信度：未验证（这套分数线还没有被人工作答验证过，样本 8/60 份）。可以当参照，不要当准确率。
```

## 验证

- 新增 `scripts/test_trust_display.py` 7 项：未就绪时 metrics 不给 0、给定足量标注时转 `verified` 并给出 mae、报告行只在未验证时出现、`render()` 不传 trust 时不崩、接口失败按未验证处理
- `run_tests.py` → **16 套件 / 311 断言全绿**（原 15/301）
- 桌面 1512×900 与移动 430×932 截图核对：条在页面最上方、切「基础评测」后仍在、报告页首屏可见
- **移动端「看起来被截断」是截图裁切，不是横向溢出**：探针实测 `vw=500 cw=485 sw=485 offenders=0`（Chrome headless 最小窗口 500，截图宽 430 是裁切）

## 文件

- 改：`web/server.py`（`api_metrics` + 路由 + `render(... trust=)`）、`web/index.html`（`#trustStrip` + CSS + `loadTrust`）、`scripts/review.py`（`_trust_line` + `render(trust=)`）、`scripts/run_tests.py`、`README.md`、`VERSION`/`package.json`/`package-lock.json` → 1.11.0
- 新：`scripts/test_trust_display.py`

# v1.10.0（2026-10-29）

> 一句话：把「用不用大模型」从**猜**变成**按**，并给 key 一个几秒钟的体检入口。

## 起因

v1.9 的规则是「框里填了 key 就调模型，不填就走规则引擎」。真正挂到公网上之后
（`https://resume-reviewer-x3pf.onrender.com`）暴露了两个问题：

1. **没人知道要填 key**。页面默认不填 → 谁都只会拿到基础评测，还会以为「这网站就这水平」。
2. **填了的人也不确定调没调模型**。要等 40 秒、看报告有没有语义点评才知道，填错 key 更是要等一整轮。

用户原话：「我想要的是填apikey就走调用大模型的渠道，如果没有填就不走，走基础评价功能渠道，给用户选择的选项（做成按键选项）。加上填apikey 的接口」

## 改了什么

### 1. 评测方式变成两个按键（前置、互斥、显式）

- 页面第二步顶部：「**AI 深度评审（用我自己的 Key）**」/「**基础评测（不调用模型）**」
- 选中 AI 评审才展开 key 输入框；选中基础评测时 key 整块隐藏，并提示「一个请求都不会发出去」
- **不自动降级**：选了 AI 却没填 key → 前端拦截（红字提示 + 焦点跳到输入框），直接调接口则返回
  `code=LLM_NO_KEY`、`retryable=false`。宁可报错，也不把规则报告伪装成模型报告
- 选基础评测时，即使输入框里还留着旧 key，也不会调模型（`api_key` 被清空，`repeat` 强制 1）
- 选择记在 `sessionStorage`（与 key 同生命周期），刷新保留、关标签页即失

### 2. 新接口 `POST /api/key/check`

```
POST /api/key/check
{"api_key": "sk-…", "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"}

→ {"ok": true,  "model": "deepseek-flash", "base_url": "https://api.deepseek.com/v1", "latency_ms": 820}
→ {"ok": false, "code": "LLM_HTTP_401", "error": "HTTP 401（key 无效或已过期）：…", "retryable": false}
```

- 只发一句 `"回答两个字：可用"`，`max_tokens=16`，超时 20 秒 —— 握手，不是一次完整评审
- **永不回显 key**：响应由 `LLMConfig.describe()` 组装，只含模型名与地址；测试里用 `assertNotIn(key, json.dumps(响应))` 钉住
- 不写盘、不进历史、不产生报告；限流独立于评审：10 次/小时、200 次/天（同一 IP）
- 页面上的「验证 Key」按钮：1~3 秒给结论，省掉「等 40 秒才知道 key 填错」

### 3. `mode` 进契约、进历史

- `/api/review` 新增 `mode` 参数：`"llm"` / `"local"` / 省略（省略时保持 v1.9 行为，兼容旧客户端）
- 其它值 → `code=BAD_MODE`
- `mode` 与 `engine` 一起写进 `config_hash`：**规则引擎的分与模型的分不是一把尺子**，
  历史里不会跨引擎给出「比上次 +N」。响应里也回 `engine`，报告顶部黄条写明「这份没调用大模型」
- `MOCK` 只影响「怎么拿到模型结果」，不削弱「选了 AI 就必须有 key」这条规则

## 测试

新增 `scripts/test_mode_and_keycheck.py`（16 项，已并入 `scripts/run_tests.py` 的第 11 个套件）：

- `ModeChoiceTest`（9 项）：AI 无 key 必须拒、非法 mode 必须拒、基础评测即使有 key 也不发请求、
  基础评测强制单次、省略 mode 的兼容路径两边都对、MOCK 不豁免、跨引擎 `config_hash` 必须不同
- `KeyCheckTest`（7 项）：空 key / 格式错 / 内网地址三种拒绝、成功时返回模型名+耗时且不含 key、
  上游错误码透传、意外异常可重试、MOCK 不伪装成真调用

全量结果：**11 个套件 202 条断言全绿**（原 10 套件 186 条）。真模型端到端也验过：

| 场景 | 结果 |
|---|---|
| `/api/extract` 文本 87 字 | `ok=true charset=87` |
| `mode=local` | `ok=true engine=local-rules score=60`，1.3 秒 |
| `mode=llm` 不填 key | `code=LLM_NO_KEY engine=llm`，不产生报告 |
| `/api/key/check` 假 key | `code=LLM_HTTP_401`，透传上游 `Authentication Fai…` |
| `/api/key/check` 真 key | 返回模型名 + `latency_ms`，响应中无 key |

## 兼容与边界

- 旧客户端不带 `mode` 照旧能用；带 `mode` 的新前端在三种情况下行为都明确
- 无 key 的公开部署仍然是完整功能（基础评测），只是**不再伪装成 AI 评审**
- key 的存放边界没有变：只在 `sessionStorage`，会随请求发到后端（后端要替你调模型），
  不写库、不写报告、不写日志

## 文件

| 文件 | 改动 |
|---|---|
| `web/index.html` | 模式按钮（含键盘左右键切换 + `aria-checked`）、模式状态、`keyPayload()` 发送 `mode`、验证 Key 按钮与状态条、评审前拦截 |
| `web/server.py` | `api_review` 的 `mode` 分支与 `LLM_NO_KEY`/`BAD_MODE`、`api_key_check()`、`/api/key/check` 路由与限流、`config_hash` 计入 `engine`、响应回 `engine`/`mode`、`health` 增加 `modes`/`key_check`/`requires_user_key` |
| `scripts/test_mode_and_keycheck.py` | 新增 16 项测试 |
| `scripts/run_tests.py` | 套件 10 → 11 |
| `VERSION` / `package.json` | 1.9.1 → 1.10.0 |
| `docs/prd.md` | 新增 M3.2（F3.8~F3.10）与 AC-3.16~AC-3.21 |
| `README.md` | 「谁付模型的钱」改写为按键选择、端点表加 `/api/key/check`、测试数字更新 |

---

# v1.10.1（2026-10-29）

> 一句话：把评测方式与 API Key 搬到**页面最上方通栏**——它原来是右侧表单里的第 5 个字段，要滚动才看得到。

## 起因

v1.10.0 上线后用户反馈：「把 api 接口放置在简历预览的上方」。实际排查：桌面端布局是
`左侧栏 232px / 中间「简历预览」1fr / 右侧表单 442px`，而评测方式 + Key 输入框在右侧表单里
排在上传卡片、目标岗位、JD、评审次数、当前文件之后，**要往下滚才出现**；预览区和它左右并排，
视觉上更显得 Key「藏在角落里」。

## 改了什么

- `.app` 网格从「三列」变成「通栏一行 + 三列」：`.app{grid-template-rows:auto minmax(0,1fr)}`，
  `.topbar{grid-column:1/-1}`，打开页面第一眼就是评测方式 + Key 输入框
- 通栏内部：左边两个模式按键（紧凑胶囊，隐藏长描述），右边 Key 输入行 + 显示/清除/测试按钮 +
  状态条 + 「没有 Key？切成基础评测」退路条 + 隐私说明
- 长文案（有无 Key 的差异对照、接口地址/模型名）收进 `<details>`：
  `有无 Key 差在哪？· 高级：换成别的模型服务`，默认收起，点开是完整对照
- 选「基础评测」时，通栏里换成一条「已选基础评测：不会把简历发给任何模型」横条（`#localNote`），
  宽度撑满整行
- 响应式：≤900px 通栏 `order:-1` 变成页面第一个块并纵向排列；≤560px Key 输入框独占一行、
  「切到基础评测」按钮也独占一行，避免溢出
- 空状态修正：`setKeyStatus('', '')` 现在会移除 `show` 类，不再残留一个空白提示框

## 验证

| 场景 | 结果 |
|---|---|
| 桌面 1512×900 首屏 | 通栏在最上方：模式按键 / Key 行 / 退路条 / 隐私说明 / `<details>` 全可见，预览与表单在其下 |
| 切到「基础评测」 | Key 块整块消失、`#localNote` 横条撑满整行、`details` 仍可展开，无空洞 |
| 窄屏 500px（Chrome 实际最小窗口） | `document.documentElement.scrollWidth == clientWidth == 500`，无横向溢出 |
| 标签平衡 / 内联 JS 语法 | `html.parser` 无未闭合与错配；`node --check` 通过 |
| 与 v1.10.0 行为一致性 | 202 条断言全绿，`mode`/`keyPayload()`/`setMode()` 逻辑未改，只搬了位置 |

## 文件

| 文件 | 改动 |
|---|---|
| `web/index.html` | 新增 `.topbar` 及其样式与响应式规则；`modeField`/`engineField`/`localNote`/`modeNote`/`baseUrl`/`model` 从 `#step2` 搬到通栏（id 全保留，JS 不改）；`#noKeyHint` 换取更明确的短句 |
| `docs/CHANGELOG-v1.10.md` | 本节 |
| `VERSION` / `package.json` | 1.10.0 → 1.10.1 |
