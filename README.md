# 简历评审助手

> **当前发布：v2.0.1（2026-10-29）**　规则版本仍为 v1.8（评分口径一条没改）。
> **2.0 换的不是模型，是「凭什么相信这个分数」**：阈值有了唯一真源（`config/rubric.yaml`）、
> 能一键出基准报告（`scripts/benchmark.py`）、能发盲评稿请人打分（`human_eval.py blind`）、
> 能算人与人之间的一致性、能看改写有没有被用上（`scripts/closure.py`）。
> **但这仍然是「未验证」状态**：数据集 8/60、留出集 0/18、独立标注者 0/2 人 —— 所以页面横幅是琥珀色，
> `benchmark.py` 必然输出 `NOT_READY`，任何「准确率 xx%」的说法都不许出现。
> 完整说明见 [docs/发布说明-2.0.md](docs/发布说明-2.0.md)，逐版变更见
> [docs/CHANGELOG-v1.10.md](docs/CHANGELOG-v1.10.md)。
>
> v1.12 新增**改稿闭环落实率**（`/api/metrics.closure`）：只有「先后两版都评过」才产生样本，
> 条数不够就返回 null 并说明原因。
> v1.11 新增**可信度状态**：页面顶部与每份报告里都会写明「这套分数线有没有被人工验证过」，
> 数据不够时明确显示「未验证」而不是装作有准确率；配套 `GET /api/metrics`。
> v1.10 把「用不用模型」变成页面上的**两个按钮**：选「AI 深度评审」必须填自己的 key，
> 选「基础评测」就完全不调模型；另加 `POST /api/key/check`，几秒内验证 key 能不能用。
> **站长 key 永不代用**，且换 key / 换模型 / 换接口地址评价标准一个字都不变。

给室友/同学评审实习简历，指出**哪句话会被筛掉**，并给出可逐句替换的写法。涉及事实强度或待补数字的改写**必须先由本人确认**，不会进入一键复制区。

## 本地网站

**双击 `start.bat`** 就会起服务并自动开浏览器（推荐，会自己挑装好依赖的 Python）。或者：

```powershell
.\run_web.ps1              # → http://127.0.0.1:8765
.\run_web.ps1 -Mock        # 不调模型，只看界面
.\run_web.ps1 -Port 9000   # 换端口
```

或在 VS Code 里 `Ctrl+Shift+P` → `Run Task` → `8 · 启动本地网站（默认 8765）`。

页面三步：**拖入文件 → 填岗位（可选贴 JD）→ 开始评审**。实测 **13~45 秒**出报告。
报告可复制、可下载 `.md` / `.docx`。只有事实已确认、且不含 `[待补充]` 的改写才进入“一键复制”区；其余仍展示，但必须本人补齐或确认后再用。

**v1.1 相对 PRD 补的三件事**（对应 kickresume 的功能缺口，见 [docs/kickresume-对比.md](docs/kickresume-对比.md)）：

| 能力 | 说明 |
|---|---|
| 岗位 JD 匹配 | 粘贴 JD → 报告多一段「关键词覆盖 62%」：哪些有证据、哪些只在技能栏堆着、哪些完全没有。没贴就不出现 |
| 改完的闭环 | `.docx` 导出 +「复制可直接使用的改写片段」（按原文顺序）；待确认事实和占位符不会混进复制区 |
| 进步可见 | 同一份文件再评一次，顶部显示「比上次 +9 分 / 与上次同分」；只存分数与岗位，不存正文 |

详细操作与排错见 [使用说明.md](使用说明.md)，改动明细见 [docs/CHANGELOG-v1.1.md](docs/CHANGELOG-v1.1.md)。

> 速度来自 `.env` 里的 `DSH_LLM_THINKING=off`：关掉推理模型的长 CoT，
> 一份从 5~10 分钟降到 15 秒；输出先过 schema + 事实校验，再经过交付门禁：不合格结果不展示分数、不提供复制下载、不写正常历史。

技术上是**零依赖**的：`http.server` + 一个 HTML 文件，没装 Flask/Celery 之类。
文件走 base64 JSON（Python 3.13 已移除 `cgi`，不解析 multipart）。

### 谁付模型的钱：你自己按按钮决定

页面**最上方通栏**是**两个互斥的按钮**，选一个再往下走（不选默认是「AI 深度评审」，Key 输入框就在旁边；没有 Key 就点「基础评测」）：

| 你选的 | 走哪条路 | 花谁的钱 | 报告里能看到什么 |
|---|---|---|---|
| **AI 深度评审（用我自己的 Key）** | 你的 key → 你指定的模型 | **你自己的额度**（约 2 分钟内出结果） | 全套：两道关、量化口径、面试追问、可直接替换的改写 |
| **基础评测（不调用模型）** | **本地规则引擎，零成本**（1~3 秒） | 不花钱 | 结构、排版、关键词覆盖、量化率、动词强度、缺失章节这些**机械可判定**的硬指标；语义点评与可直接替换的改写要填 key 才有 |

**不填 key 不会「自动降级」**：选「AI 深度评审」却没填 key，页面会直接把按钮加红并提示，接口也会返回
`code=LLM_NO_KEY`，不会悄悄给你一份规则报告——否则你会以为那是大模型评的。
反过来，选了「基础评测」时，即使输入框里还留着上次的 key，也**一个请求都不会发出去**。

填了 key 可以先点**「验证 Key」**（`POST /api/key/check`）：1~3 秒告诉你这个 key 能不能用、是哪个模型、往返多久，
省掉「等了 40 秒才发现 key 填错」。它只发一句 `max_tokens=16` 的握手请求，响应里永远不会出现 key 本身。

**站长的 key 永远不会被代用**。网页端只认「访客自己填的 key」和「规则引擎」两条路，服务器 `.env` 里的
`DSH_LLM_API_KEY` 只用于命令行批量跑评审（`scripts/review.py`），
这样公开部署上去之后，不会有人拿陌生人的简历刷爆你的额度。所以 Render/GitHub 上不配 `DSH_LLM_API_KEY` 是正常状态。

**换 key、换模型、换接口地址，评价标准一个字都不变**：同一份 prompt（`prompts/system-v1.8.md`）、
同一套权重（`check_output.WEIGHTS` = 25/25/20/15/15）、同一批通过线（`GATE_ROLE_MIN` 等）。
这条承诺由 `scripts/test_engine_choice.py::RubricIdentityTest` 的 5 个断言钉住：
prompt 与 key/model/url 无关、key 不进 prompt、权重与通过线是常量、接口上没有 `version`/`weights` 这类可改标准的口子、两个引擎共用同一份权重。

规则引擎那一份报告顶部会有一段黄条写清楚「这份没调用大模型」，不会伪造成模型结论——两种模式的分数**不可横向比较**，
所以换引擎后历史里不会给出「比上次 +N」。

关于 key 的处理，边界是这样的：

- 只存在**当前浏览器标签页**的 `sessionStorage`：关标签页即失效，换设备不会有。
- 不写服务器数据库、不写报告、不写历史、不写访问日志；`LLMConfig.describe()` 也只输出模型名与地址，不输出 key。
- 这次评审会把它随请求发到本站后端（后端替你调模型），所以**别在公共/不可信的部署上填 key**——后端理论上能看到它。
- 接口地址默认 `https://api.deepseek.com/v1`，可以在「高级」里换成任何 OpenAI 兼容服务。指向本机/内网（`127.0.0.1`、`192.168.*`、`169.254.*` 等）会被后端拒绝，防止把服务器当跳板；自己跑 Ollama 时要显式设 `DSH_LLM_ALLOW_LOCAL=1`。

| 端点 | 作用 |
|---|---|
| `POST /api/extract` | 上传文件或粘贴文本 → 返回字数、预览、会话 id |
| `POST /api/review` | 传会话 id + 岗位 + `mode`（`"llm"` / `"local"`，省略则按有无 key 推断）(+ 可选 `api_key` / `base_url` / `model` / `repeat` / `visual`) → 返回一页报告与 `review_id`；无合格结果时返回 `ok:false` + `code=REVIEW_VALIDATION_FAILED`，不落盘 |
| `POST /api/review/start` | 同上参数 → 立刻返回 `202 {job_id, stream}`，不阻塞等结果；结果从下面这条流取 |
| `GET /api/review/stream?job_id=…` | `text/event-stream`：四阶段 `stage` 事件严格 1→2→3→4，最后一次 `done` 或 `error`；断线重连带终态但不重放历史阶段 |
| `POST /api/key/check` | 传 `api_key` (+ 可选 `base_url` / `model`) → 几秒内返回 `{ok, model, latency_ms}` 或错误码；只发握手请求，回显中不含 key，限流 10 次/小时、200 次/天 |
| `GET /api/history` | 最近评审摘要，只含 `review_id`、名字、时间、岗位、分数，不含正文 |
| `GET /api/report?id=…` | 按 `review_id` 重看该份报告、自己的脱敏原文与可复制片段 |
| `GET /api/download?id=…&kind=md\|docx\|json` | 按 `review_id` 下载对应报告 |
| `DELETE /api/report?id=…` | 按 `review_id` 删除全部落盘文件与历史摘要 |
| `GET /api/metrics` | 只读聚合：请求量/失败率/拒收率/延迟分位/四阶段耗时/`tokens` + 可信度与改稿闭环状态；不含任何单次请求标识 |
| `GET /api/health` | 版本、mock 开关、`requires_user_key`、可用引擎、`rubric_version`、模型名默认值 |

会话只存内存、30 分钟过期；上传的临时文件在处理结束或异常返回时都会清理。页面图默认不生成、不发送，只有用户明确允许时才保留并交给模型。

### 数据会去哪

- 应用在本机运行，但识别和评审会把必要内容发给你配置的模型服务，不是“完全不联网”。**目标岗位和 JD 也会随评审请求发送给模型。**
- **文本**里的手机号、邮箱、身份证会替换成脱敏标记；姓名、学校、公司、项目描述**不脱敏**，因为评审需要。
- **文字型 PDF / DOCX / txt** 默认只发送脱敏文本，不渲染或发送页面图。
- **扫描件和图片**必须在上传区勾选“允许发送页面图像”才能识别。页面图可能包含姓名、照片、联系方式等，当前**不做图像脱敏**。
- 页面图即使已为 OCR 提取，也要在确认页再次勾选“额外检查排版”，才会进入评审批次。
- 本地运行时报告写入 `reports/`；Cloudflare 等云端临时实例重建后历史会清空。侧栏历史可逐份删除对应 `.md/.json/.txt/.docx` 与分数记录。

### 历史归属与分差

- 每份评审生成唯一 `review_id`，报告文件名带 ID 后缀，同名简历不会互相覆盖。
- 历史列表、报告、预览、下载、删除全部按 `review_id` 关联；历史只保存报告时，预览会明确显示“没有原文”，不会沿用上一份简历。
- 历史抽屉在桌面和手机都可打开；原文超过 600 字可展开全文。
- “比上次 +N”只在**脱敏文本内容和岗位/JD/模型/图片设置/评审次数都一致**时计算；配置不同只提示不可比，不给差值。
- 旧版按文件名保存的历史会以 `legacy-*` 记录兼容读取，只保留每个名字最新一份报告，并标注旧记录；不清空旧报告。

### 改版前后对比

改完简历换上来的**新版本**（内容变了，哈希必然不同）会单独出一节「和上一版比」，而不是复用上面的“比上次 +N”：

| 内容 | 依据 | 说明 |
|---|---|---|
| 分数、5 个维度、两道关、格式结论 | 两份报告的 JSON | 只有岗位/JD/模型/发图/评审次数**和规则版本**都一致才报分差 |
| 新增/删除/改写了几行 | 按行做差异比对 | 改写按段配对，两边行数不等时多出来的算纯增删 |
| 上次的建议落实了没有 | 改写后的说法在不在新文本里 | 三态：看起来已改 / 原句还在 / 改成了别的写法 |

几条边界：

- **找错上一版比不报分差更糟**：正文相似度低于 0.62 就当两份不相干的简历，宁可不比。
- **不以文件名为准**：`简历_v2.pdf` 改成 `简历_终版.pdf` 也能接上，判据是内容相似度，不是名字。
- **规则版本变了不比分差**：v1.7 和 v1.8 的评分口径不同，强行相减没有意义，此时只报文本变化。
- **“建议落实了没有”是推断**：按文本比对推出来的，报告里明确写了这一行不是模型判断。你把建议换成自己的说法改，它看不出来，只会说“改成了别的写法”。
- 同一份内容再评一次（哈希相同）走的是“分数抖动”那条路，不会同时叠一节空的版本对比。

## 一条命令用起来（命令行版）

```powershell
python scripts/review.py 简历.pdf --job "后端开发实习生"
python scripts/review.py 简历.png --job "产品经理实习生"
python scripts/review.py 简历.docx
```

**输入什么格式都行**，程序自己判断走哪条路：

| 输入 | 处理方式 | 花不花钱 |
|---|---|---|
| `.txt` / `.docx` | 直接抽文本 | 免费 |
| 文字型 `.pdf` | PyMuPDF 抽文本 | 免费 |
| **扫描型 `.pdf`** | 渲染成图 → 视觉模型转写 | 少量 |
| `.png` / `.jpg` | 视觉模型转写 | 少量 |

**输出**：屏幕上一页报告 + `reports/<文件名>.md`（报告）+ `.json`（完整数据）+ `.txt`（脱敏后的原文）。

报告长这样：

```
## 结论：60 / 100　勉强
初筛 ✅ 过　面试 ❌ 会挂　✅ 格式没问题
> （一句话说清为什么不是 65+，也不是 reject）

## 三处建议改写（确认事实后再替换）
1. 原来：`【成果】2万字、70页毕业论文…`
   改成：`【成果】[待补充：上线状态或试用人数]…`
   为什么：论文字数是交付物不是结果…

## 今天先做这一件事
**把毕设段【成果】换成可被追问的产品结果**（约 20 分钟）
```

### 隐私：只填必要的

- **只需要一个文件**，姓名、电话、公司名都不用填。
- 手机号、邮箱、身份证会在**发给模型之前**替换成 `[手机号已脱敏]`；姓名、学校、公司和项目内容不会自动脱敏。
- 出生日期只保留年份，学历/工作起止时间保留（评审要用）。
- 图片和扫描件默认不会发送；必须在上传区明确勾选页面图授权。文字型 PDF 不勾选也能正常评审，只是没有排版检查。
- `reports/`、`samples/txt/`、`_test/`、上传临时目录全部不会入库。
- 历史条目旁的 `×` 会删除该份报告的全部本地文件和分数记录。

### 几个省钱的开关

```powershell
--repeat 3      同一份评 3 次取中位（单次会抖 ±5 分，3 次更稳，成本也 3 倍）
--visual        明确允许把页面图发给模型；图片/扫描件不加此参数会直接报错
--no-ocr        禁止 OCR（扫描件/图片会直接报错）
--from-json     改完排版后重新渲染报告，不重新调模型
```

> **为什么要有 `--repeat`**：同一份简历单次评审会在 ±5 分内抖动
> （模型本身非确定性 + OCR 每次转写略有差异）。
> 实测同一份简历两次跑出 60 和 55——**这种差别没有意义**。
> 想稳就跑 3 次取中位，报告里会写成 `55（3 次：53/55/58，取中位）`。

> ⚠️ 推理型模型（如 `deepseek-flash`）单次评审约 **25k 输出 token**（大部分是思考）。
> 想省钱可以换非推理模型：`$env:DSH_LLM_MODEL = "deepseek-chat"`。
> **不要在推理模型上设 `max_tokens`** —— 思考会占满额度，返回半截 JSON。

## 规则版本：v1.7（产品版本 v1.7.0）

### v1.7：格式真正进入决策链

v1.6 把格式排到了最前面，但没让它参与判断——`gate_hr` 公式里没有格式项，
所以一份"解析失败"的简历仍然可以判"初筛会留下"。v1.7 补上这个闭环。

| v1.6 的问题 | v1.7 怎么改 |
|---|---|
| **格式优先只是口号**：`gate_hr` 不含格式项 | `gate_hr.pass` 增加 `format_review ≠ blocking` 条件——**看不见就等于没投** |
| **照片规则互斥**：视觉项要求判「头像/照片」，硬性规则又说"禁止评价照片" | 拆开：照片的**内容**（长相/性别）禁止评价；照片的**版面**（有没有放、占多大）必须评 |
| **prompt 说 10 项、脚本只查 6 项** | 统一为「必须判满 10 项」，一项都不能少 |
| **视觉判断没有不确定性** | 每项加 `certainty: certain/uncertain`，拿不准就标不确定并在 note 写"请对照原文件确认" |
| **`verdict` 可以自由裁量** | 给出确定映射：致命项≥1→blocking；非致命≥2 或 density=high→risky；其余→clean |
| **`sections_missing` 会重复上报** | 改按语义判，同一段不能既在 found 又在 missing；非标准标题归到「章节命名标准」 |

关键实现细节：`gate_hr` 和 `next_actions[0]` 都改用 `format_is_blocking()`——
**不采信模型自报的 verdict**，只要致命项判了 `ok=false` 就算 blocking。
否则模型把 verdict 写成 `clean` 就能绕过。

实测：

```
原始（78 分强简历，格式 risky）    gate_hr 期望 = True
改成「项目经历是图片」后           format_is_blocking = True
                                  gate_hr 期望 = False   ← 内容再好也进不了初筛
```

### v1.6：格式优先（顺序）

**原则：格式塌了，简历根本没被看见。**

| 之前 | 现在 |
|---|---|
| `ats_check` 是检验 8，排在最后 | `format_review` 是**检验 1**，永远第一个做 |
| 只有文本层 | 文本层（永远可判）+ **视觉层 10 项固定检查**（有图才判） |

视觉层 10 项是**白名单**，模型不许自造检查项：

```
图片化文字 · 分栏或表格 · 字号与行距 · 留白密度 · 是否超出1页
照片占位 · 页眉页脚占用 · 对齐与间距一致 · 章节命名标准 · 联系方式可提取
```

> 顺带修了一个真实误差：**长句阈值从 40 字改到 60 字**。
> 真实简历的合格 bullet 普遍 40~60 字，40 字会把一半内容误判成长句。

### v1.5：客观性三条

**每条结论必须归入三级，推断不许写成事实。**

| 级别 | 要求 |
|---|---|
| `fact` | 必须有逐字 `evidence` |
| `inference` | 必须给 `verify_question`，措辞带"疑似/需确认" |
| `judgment` | 用"我认为" |

**这五类默认只能是推断**：学历性质（专升本/自考/非全日制）、时间线、证书专利有效性、数字口径、个人身份。
**`hard_blockers` 只允许 `fact`** —— 一票否决必须看得见。

> 起因：我曾把"3 年专科 + 2 年本科"推成"专升本"当事实说，还用图片识别结果指控别人专利号写错。
> 现在这类断言会被脚本拦下：`fact` 必须逐字引用原文，编不出来。


### v1.6：格式优先

**原则：格式塌了，简历根本没被看见。**

所以评估顺序整个反过来：先判"能不能被看见"（格式与可解析性），再判"值不值得留"（内容）。

| 之前 | 现在 |
|---|---|
| `ats_check` 是检验 8，排在最后 | `format_review` 是**检验 1**，永远第一个做 |
| 只有文本层 | 文本层（永远可判）+ **视觉层 10 项固定检查**（有图才判） |
| 格式问题可以晚点改 | `verdict=blocking` 时，`next_actions[0]` **必须**先修格式 |

视觉层 10 项是**白名单**，模型不许自造检查项：

```
图片化文字 · 分栏或表格 · 字号与行距 · 留白密度 · 是否超出1页
头像/照片 · 页眉页脚占用 · 对齐与间距一致 · 章节命名标准 · 联系方式可提取
```

脚本会查：有图时至少判 6 项、item 必须在白名单里、不能重复、
致命项（图片化文字/超页/联系方式/分栏/章节命名）判了 `ok=false` 时结论不能是 `clean`。

> 顺带修了一个真实误差：**长句阈值从 40 字改到 60 字**。
> 真实简历的合格 bullet 普遍 40~60 字，40 字会把一半内容误判成长句。

### v1.5：客观性三条

**每条结论必须归入三级，推断不许写成事实。**

| 级别 | 要求 |
|---|---|
| `fact` | 必须有逐字 `evidence` |
| `inference` | 必须给 `verify_question`，措辞带"疑似/需确认" |
| `judgment` | 用"我认为" |

**这五类默认只能是推断**：学历性质（专升本/自考/非全日制）、时间线、证书专利有效性、数字口径、个人身份。
**`hard_blockers` 只允许 `fact`** —— 一票否决必须看得见。

> 起因：我曾把"3 年专科 + 2 年本科"推成"专升本"当事实说，还用图片识别结果指控别人专利号写错。
> 现在这类断言会被脚本拦下：`fact` 必须逐字引用原文，编不出来。


v1.4 的改动来自一次**同类产品对照**（睿简智能「AI简历优化助手」的 5 条选购标准）。
对照下来我们**缺两条基础能力**，另外我们自己有三条别人没有的东西，正好互为补充。

| 他们的标准 | v1.3 有没有 | v1.4 怎么补 |
|---|---|---|
| ① 目标 JD 匹配（关键词覆盖、相关性、自然度、无关篇幅） | ❌ 只有"换个岗位名读不读得通"的二元判断 | 新增检验 7 + `jd_match`：逐个关键词标 `natural`（经历里有证据）/ 堆砌 / 缺失，算覆盖率 |
| ② ATS 友好诊断（章节、图片化文字、分栏、联系方式） | ⚠️ 有字段但基本放弃——"没图不能评" | 新增检验 8：把 ATS 拆成**文本层（永远可判）**+ **视觉层（有图才判）**。章节完整性、联系方式可提取、乱码、页数全是文本问题 |
| ③ 不虚构经历 | ✅ 防了数字，没防**权属** | 新增 `fact_delta`：把「参与」改成「主导」必须标 `ownership_upgrade` 且 `needs_user_confirm` |
| ④ 输出可编辑可迭代 | ⚠️ 有改写，但没有"本人确认"环节 | 新增 `before_submit`：投递前必须自己核实的事项 |
| ⑤ 隐私 | ❌ 未提及 | 新增硬规则 5：输出不得回显完整手机/邮箱/身份证 |

反向来看，我们有三条他们没提的：

**两道关**（初筛 vs 面试，淘汰原因完全不同）、**数字溯源**（查简历里*已有*数字的来源）、**展示力检验**（弱动词/结果前置/自评词/主线）。

### 顺带修的两条边界

- **禁止输出概率**：不许说"通过率 70%"——无法预测结果，给概率就是编造（脚本会查）
- 检验从 7 个变成 8 个：原来的"三秒测试 + 单句可读性"合并成「检验 1：可读性」，腾出两个位置给 JD 和 ATS

### v1.3 的改动（历史）

v1.3 修的不是规则，是**接线**——规则在 v1.2 基本对了，但有 3 处"写了却没用上"，
而 `--mock` / `--selftest` 全都测不出来，因为它们只验证校验器，不验证**送给模型的东西**。

| v1.2 的问题 | v1.3 怎么修 | 验证方式 |
|---|---|---|
| **`--image` 在骗模型**：只把 `has_image` 改成 true，从不真的传图 | 改成 `--image <图片路径>`，真发 `image_url`（data URL）；文件不存在直接报错 | 接线测试断言 `content` 是数组且含 `image_url` |
| **few-shot 从没发给过模型**：`build_prompt` 只读 `system-*.md` | 注入 good 样例，并加"禁止复用其中句子"的护栏 | 接线测试断言 prompt 含「参考输出」 |
| **必填字段 prompt 里没写**：schema 要 `long_sentences`，prompt 一次没提 | 字段表补全（实测 v1.2 缺 15 个字段名） | 接线测试比对 schema 必填 × prompt 模板 |
| `gate_hr` 无判定标准，却驱动硬约束 | 改成**推导式**，脚本核对一致性 | 自检「抓到两关不自洽」 |
| 证据下限 `20~80 字` 与真实简历冲突（实测样例是 8/15/16/19 字） | 放宽到 `6~120 字` | 每条引用都查长度 |
| `number_audit` 至少 2 条 → 逼模型拿年份凑数 | 改为 0~6 条；规则换成"有百分比就必须覆盖 3 个" | 自检「抓到数字溯源覆盖不足」 |
| 6.2「结果被埋」看数字位置 → 误判 `项目成果：` 开头的行 | 改看**句首 12 字有无结果信号** | 自检「抓到句首结果误报」 |
| 禁用词黑名单可绕过 | 换成**可执行锚点** | 自检「抓到无可执行锚点」 |
| `不许落在 60~75` 会推高分数 | 允许落在 65~74，但必须说清为什么不是 64 或 75 | prompt 自检第 1 条 |
| prompt 引用内部脚本，不可移植 | 删掉全部脚本引用 | 接线测试断言无 `check_output.py` |

### v1.8 的改动：把「看不见」变成一句需要证据的话

用真模型连跑同一份简历时，连续 4 轮被门禁拒收，每轮都是一个我自己造的 bug。
v1.8 修的是**校验器与 prompt 彼此矛盾**的地方——模型没错，是我让它无所适从。

| 症状 | 真实原因 | 修法 |
|---|---|---|
| `verdict=blocking` 但 10 项视觉检查里没有一项不 ok | 校验器只在 `known=true` 时核对 verdict，没截图时一句「看不见」就能让初筛直接不过 | 判 blocking 必须有证据：无截图时只认「联系方式提取不到 / 乱码 ≥50 字 / 关键章节缺 3 类」；有截图时必须有致命项不 ok |
| 数字溯源覆盖不足，报了却不知道漏哪个 | 报错只说数量不说名字，重试的模型不知道自己漏了什么 | 报错里写明漏掉的数字（`漏了：3%`）；重试时把**上次被拒原因原文回灌**给模型 |
| `schema_version` 写成 `v1.7` | prompt 字段表还写着「固定填 v1.7」，我只改了代码没改模板 | 接线测试新增一条：**字段表的必填项必须与 schema 一致**（旧检查只验字段名出现过，`assertion` 藏在别的行也能蒙混过关） |
| `issues[].verify_question` 填 null 被判类型错 | 这个字段只在「推断」时需要，schema 却只允许 string | 允许 `["string", "null"]` |
| `one_line_summary` 206 > 200 | 上限按我自己的手写样例定的，模型一句话自然更长 | 放宽到 260 |

> 教训：**校验器太紧会让模型说假话，太松会让用户看到编造**。每一条硬错误都要问一句
> 「模型有没有正当理由做不到」，有就改规则，没有才拒。重试时必须告诉它错在哪，
> 否则只是把同一个错误再赌一次。

### v1.8.1 的改动：别再因为「没说什么」把整份结果丢掉

室友真上传了一份分栏/表格排版的 PDF，结果被门禁拦下，屏幕上只有三行报错、没有任何可用内容。
三件事同时发生，没有一件是"模型说错了事实"：

| 症状 | 真实原因 | 修法 |
|---|---|---|
| `漏报弱动词行 1 条：…完成` | PDF 表格被拍平后，单元格「完成」单独占一行，被当成一行经历 | 动词后面至少还要有 4 个实字，才算经历行；分栏碎片不算 |
| 漏报弱动词 → 整份拒收 | 漏报是"少说一条"，不是"说错" | 降级为 warning，只计入质量统计（`weak_verb_missed`） |
| 结论没提格式、第一条动作修的是内容 | 措辞/顺序偏差，本身可以用两步修好 | 质量门禁**自动修**：结论前补「【先修格式】」、把 format_review 动作提到第一位，修完再校验一次；报告里记一条 `AUTO_REPAIRED` warning |

> 交付策略从此固定为三分法：**说错了（编造、回显隐私）→ 拒收；能修的（分数、顺序、措辞）→ 自动修；
> 少说了（漏报某条弱动词）→ 只记警告**。用户等一分钟是为了看到报告，不是为了看校验日志。

### 黄金原则


```
凡是能写成脚本的，就不写进 prompt。
凡是能写成断言的，就不靠模型自觉。
```

## 一条命令跑完全部测试

```powershell
cd resume-reviewer
python scripts/run_tests.py                    # 20 个套件，384 条断言，约 13 秒，不花钱不联网
```

```
  [ PASS  ] satisfiability      0.1s  断言 4/4
  [ PASS  ] wiring              0.3s  断言 25/25
  [ PASS  ] quality-gate        0.5s  断言 34/34
  [ PASS  ] privacy             0.9s  断言 13/13
  [ PASS  ] history             1.1s  断言 9/9
  [ PASS  ] version-diff        0.7s  断言 33/33
  [ PASS  ] human-eval          0.3s  断言 10/10
  [ PASS  ] blind               0.5s  断言 16/16
  [ PASS  ] engine-choice       0.4s  断言 28/28
  [ PASS  ] local-engine        0.3s  断言 30/30
  [ PASS  ] mode-keycheck       0.7s  断言 16/16
  [ PASS  ] rubric              0.2s  断言 24/24
  [ PASS  ] stats               0.1s  断言 43/43
  [ PASS  ] benchmark           0.6s  断言 19/19
  [ PASS  ] dataset-build       0.7s  断言 16/16
  [ PASS  ] trust               0.2s  断言 7/7
  [ PASS  ] closure             0.2s  断言 16/16
  [ PASS  ] retention           0.8s  断言 8/8
  [ PASS  ] sse                 1.2s  断言 23/23
  [ PASS  ] selftest            0.2s  断言 —
全部通过：20/20 个套件，384 条断言。
```

> 测试只依赖**入库的样例**（`samples/demo/`、`samples/fixtures/`），
> 不依赖 `samples/txt/` 里的真实简历——那些永远不入库。
> 否则 CI 一进全新检出就 FileNotFoundError，整条流水线永久变红（真红过一次）。

失败时只打印挂了的那几套的输出尾部，直接定位；退出码非零，CI 据此拦截部署。

```powershell
python scripts/run_tests.py --list             # 看有哪些套件、各自防什么
python scripts/run_tests.py --only hist -v     # 单独重跑一个，看完整输出
python scripts/run_tests.py --out r.json       # 另存 UTF-8 JSON（CI 产物用）
```

`.github/workflows/deploy-cloudflare.yml` 现在是 **test → deploy** 两段：测试不过就不部署。
以前 push 到 main 直接上线，一条测试都没跑。

想单独跑某一条时（调试用）：

```powershell
python scripts/run_eval.py --selftest          # 校验器能抓到什么错
python scripts/test_prompt_wiring.py           # 25 项：送给模型的东西对不对
python scripts/test_quality_gate.py            # 34 项：不合格结果不能当成功报告交付
python scripts/test_privacy.py                 # 13 项：图片默认不发送、同意链路、临时文件与删除
python scripts/test_history.py                 # 9 项：review_id、预览归属、旧记录、并发写
python scripts/test_version_diff.py            # 33 项：改版对比、不可比不报分差、建议落实推断
python scripts/test_human_eval.py              # 10 项：人工评测指标与数据契约
python scripts/test_engine_choice.py           # 28 项：谁能用哪个模型、换 key/换引擎不改标准
python scripts/test_local_engine.py            # 30 项：规则引擎可回指、不自造数字、可复现
python scripts/test_mode_and_keycheck.py       # 16 项：按键选的路必须照办、key 检测不回显 key
python scripts/run_eval.py --mock              # 全链路 + 指标（不调模型）
```

实测结果：

```
== 校验器自检 ==                          36/36 通过
  编造引用 / 编造数字 / 分数不自洽 / 空话 / 无图谈排版 / 图与标志矛盾
  强推却有硬伤 / 虚构数字被审计 / 来源不清却不提问 / 优先级错误 / 动作超时
  漏报弱动词 / 漏报自评词 / 结果位置误报 / 两关不自洽 / 无可执行锚点
  数字溯源覆盖不足 / 句首结果误报

== 端到端接线测试（v1.4）==                23/23 通过
  占位符替换 / 简历嵌入 / few-shot 注入 / 样例占比 <60% / 未泄露脚本名
  schema 必填字段无漂移 / 规则未丢失 / 无图不撒谎 / 有图真附图 / 图缺失报错

== 质量门禁 ==                              15/15 通过
  合格结果可交付 / 分数越界重算 / 单条坏改写撤下 / 全部改写不安全则拒绝
  假引用局部撤下 / 假硬伤阻断 / 缺字段阻断 / JD 误报降级并重算覆盖率
  待确认与占位符不进复制区 / 中位选择忽略不合格候选 / 失败不覆盖旧报告

== 隐私边界 ==                              13/13 通过
  文字 PDF 默认不渲染页面图 / 扫描件与图片未同意时不调用模型
  文字 PDF 不勾选也能评审 / 页面图需同时勾选排版检查才会再次发送
  临时上传目录异常路径也清理 / 会话过期清空文本与图片 / 历史逐份可删

== 历史归属 ==                              9/9 通过
  同名不同内容不覆盖 / 报告与预览按 review_id 绑定 / 无原文给显式空态
  只删选中记录 / 旧版历史兼容 / 并发写不丢记录 / 配置不同不算分差

== 改版对比 ==                              33/33 通过
  不相干的简历不认成同版 / 配置或规则版本不同不报分差 / 改写按段配对
  建议落实三态不硬猜 / 短探针不误判 / 报告里不出现 None / 整条链路接得进

== 人工评测工具 ==                          9/9 通过
  量表总分计算 / Kappa 与排序相关 / 闸门混淆指标 / 数据集泄漏检查
  标注文件拒绝规则 / 模型意见与改写审阅率 / 重复稳定性 / OCR 误差

[MOCK] 样本数 4｜引用命中 1.0｜编造数字 0｜无图谈排版 0｜展示力漏报 0｜MAE 2.0（仅链路，不代表真实效果）
```

> 接线测试对 v1.2 跑会 **5 项 FAIL**（13/18）——这正是它存在的意义。
> 它的"字段漂移"检查必须查 **prompt 模板**而不是渲染结果，否则 few-shot 里的字段名会让检查失效。

## 人工准确度验证做到哪了

已有：

- `samples/eval/dataset-v1.json`：8 份分层样本，全部标记为 development；
- 独立盲评标注契约、模型意见/改写审阅模板；
- 双人一致性（二次加权 Kappa）、MAE/RMSE、Spearman、排序一致率、两关混淆指标；
- 重复评审稳定性、档位/闸门翻转率、意见 Jaccard；
- OCR 字符错误率与数字错误率；
- 开发/留出集泄漏检查。
- **v2.0 新增**：`config/rubric.yaml` 作为阈值单一真源、`scripts/benchmark.py` 一键出基准报告（heldout 默认锁死）、`scripts/dataset_build.py` 批量扩建数据集（按内容去重 + 同来源组粘连 + 已标注样本切分冻结）。

**还没有**：

- 独立人工标注（0 名）；
- 双人共同标注（0 份）；
- heldout 集（0 份）；
- 正式稳定性和 OCR 对照数据。

所以当前只能报告“旧版 4 份单人非盲样本上 MAE=8.0”这种探索性代理，**不能对外说模型准确率是多少**。完整方案见 [docs/人工评测方案.md](docs/人工评测方案.md)。

### v1.11：把「这个分数准不准」写进产品，而不是只写在 README 里

上面那段话以前只存在于文档里，而文档没人读。一份报告写着 78 分，人会当成「准的」；把免责声明放在
第 736 行，等于没写。所以 v1.11 把状态直接做到界面里：

- 页面顶部一条**可信度条**：未验证时是琥珀色，写明「样本 8/60 份、独立打分者 0/2 人、人工分 0/60 份，
  分数可以当参照，但不要当成准确率」，点「为什么」展开全部未达标项；验证过之后变绿色，写
  「已用人工标注验证：与人工分平均差 x.x 分（95% 区间 a~b），κ=0.xx，样本 n 份」。
- 每份 `.md` 报告在**结论行下面**（不是文末）加一行 `> 分数可信度：…`，因为报告会被截图、转发、
  存进手机，是唯一会脱离网站单独流传的东西。
- `GET /api/metrics` 返回同一份状态，供页面和外部脚本复用；读取失败时按「未验证」处理，**不会静默省略**
  （省略会被读成没问题）。
- 未就绪时指标一律 `null` 而不是 0：给 0 会被前端画成「误差 0，完美」。

### v1.12：改稿闭环落实率——有分母才给数

```powershell
python scripts/closure.py            # 人读的汇总
python scripts/closure.py --json     # 给接口/脚本
```

口径（四条，缺一条这个数就没意义）：

1. 只有**先后两版都被系统评过**才算一个样本，单独一份报告不产生落实率；
2. `changed`（改成了别的说法）与 `unknown`（判定不了）**不进分母**——它们没有信息；
3. **规则引擎的改写不进总落实率**：它给的是占位模板（`负责…«补：接口数»`），
   用户不会原样抄，逐字比对必然判 `untouched`，混进来会把落实率结构性压到接近 0；
4. 可判定条数 < `config/rubric.yaml` 的 `eval.closure_min_pairs`（默认 10）时返回 `null` + 原因。

同时 `/api/metrics` 多了 `closure` 块，与「分数准不准」并列——一个是模型对不对，一个是产品有没有用。
落盘方式是旁挂文件 `reports/<stem>.meta.json`，不动 `.json`（那是评分结果本身，别的工具直接当 doc 解析）。

### v1.14：进度是真的，指标能解释自己

一次评审要 15~60 秒。之前页面只有一个转圈，用户分不清是「在算」还是「卡死了」——
一半人刷新（那次调用白花钱），另一半人以为坏了。

现在四个阶段由服务端真实事件点亮：**脱敏 → 读格式 → 逐项检验 → 出报告**。
没有假进度条：任何「按时间推进百分比」的写法都被测试禁止，因为假进度比没进度更伤信任。

同时 `/api/metrics` 补上了阶段耗时（`stage_ms` 每段 p50/p95）和 `tokens` 累计。
空旷的字段一律给 `null` 而不是 0，因为读者会把默认值当事实：

| 情况 | 返回 | 为什么不能反过来 |
|---|---|---|
| 还没人用过 | `fail_rate: null` | 给 `0` 会被读成「从来没失败过」，与事实相反 |
| 规则引擎模式 | `tokens: null` | 给 `0` 会被读成「调了很多次但没花钱」 |
| 某阶段无样本 | `p50: null, n: 0` | 给 `0ms` 会被读成「快得不像话」 |

### v1.13：公网版不留历史，这件事写在脸上

免费实例的 `/tmp` 本来就是临时的，服务一重启报告就没了。可页面上有个「历史报告」抽屉，
人会默认「下周一还在」。**那不是平台的问题，是我们替平台撒的谎。**

所以加了开关 `RETENTION_NOTICE`：

| 取值 | 报告与历史落在哪 | 界面上写什么 |
|---|---|---|
| `local`（默认，本地跑） | `reports/`，重启还在 | 原文案不变 |
| `ephemeral`（公网部署） | 进程私有临时目录，进程退出即删 | 三处都写明「重启即清空，要留档请当场下载」 |

三条守则：

1. **拼错不猜**：`RETENTION_NOTICE=ephemral` 只退回 `local` 并打警告，绝不会因为拼错就悄悄切成
   「不保存」——丢数据的方向必须保守；
2. **不写持久目录**：ephemeral 时 `REPORTS_DIR` 里一个字节都不写，`/api/history` 带 `notice`、
   `/api/health` 带 `retention`，页面据此改文案；「空列表 + 一句实话」比假装正常的面板诚实；
3. **临时目录自己删**：`atexit` 里 `rmtree`，否则「不留历史」就成了「留在 `/tmp` 里等人捡」。

### v2.0：一条命令出基准，数据不够就直说

```powershell
python scripts/benchmark.py                  # 规则引擎 + development 集，产出 bench/report.md
python scripts/benchmark.py --require-ready   # 未达门槛 → 退出码 2（给 CI/发布脚本用）
python scripts/benchmark.py --allow-heldout --split heldout   # 最终验收才允许
```

现在的输出（诚实版）：

```
状态：NOT_READY
样本：0   MAE：—
  · 数据集 8 份 < 目标 60 份
  · heldout 0 份 < 目标 18 份
  · 独立标注者 0 人 < 2 人（双人盲评是硬要求）
  · 已标注样本 0 份 < 目标 60 份
```

**为什么会这样，以及这不是 bug**：门槛来自 `config/rubric.yaml` 的 `eval.*`。
没到 60 份、没有两个人独立评过，就不该出现 MAE 这种数字——一旦出现，下一个读的人
就会把它当准确率引用。报告顶部只有 `READY` / `NOT_READY` 两态，`NOT_READY` 时明写
「不得对外引用」，末尾固定一句「旧 MAE=8.0 不得继承」。

### 把数据集从 8 份扩到 60 份

```powershell
# 1) 把简历放进一个目录（pdf / docx / png / jpg / txt 都行；图片需要 --ocr，会花钱）
python scripts/dataset_build.py --source samples/raw --dry-run   # 先看会加什么
python scripts/dataset_build.py --source samples/raw             # 真写入

# 2) 检查切分有没有被破坏（发布前必跑）
python scripts/dataset_build.py --verify
```

脚本替你守住三件事，**不要手动改 dataset-v1.json 绕过它们**：

1. **按正文哈希去重**——同一份简历改个文件名再放进来不会变成两份样本；
2. **同来源组粘连**——同一个人/同一次投递的多份简历只在同一侧（否则就是留出集泄露）；
3. **已标注样本切分冻结**——只要有人评过这份简历，它就不许被挪去 heldout。

被跳过的文件会逐条打印原因（太短、读不出、类型不支持），不会静默吞掉。

```powershell
# 查看数据是否达到评测门槛
python scripts/human_eval.py inventory --dataset samples/eval/dataset-v1.json --json

# 一键打包给每位标注者（一人一目录：盲评稿 / 待填表 / 怎么填 / 锚点卡）
python scripts/human_eval.py packet --reviewers R01 R02 --out samples/eval/handouts

# 一屏看进度：还差什么、下一步该做什么（未就绪时退出码 1，可直接进 CI）
python scripts/human_eval.py status --dataset samples/eval/dataset-v1.json `
  --annotations samples/eval/annotations

# 只想重发某一位，或用固定顺序复现问题（默认随机序，seed 现转）
python scripts/human_eval.py blind --reviewer R01 --out samples/eval/handouts --seed 42

# 两人填完后，看分歧在哪（分差 > 阈值进「需仲裁」，并列出差在哪几个维度）
python scripts/human_eval.py arbitration --dataset samples/eval/dataset-v1.json `
  --annotations samples/eval/annotations --out bench/arbitration.md

# 标注完成后生成报告（未达门槛会以退出码 1 明确标 not_ready）
python scripts/human_eval.py report --dataset samples/eval/dataset-v1.json `
  --annotations samples/eval/annotations --audits samples/eval/audits `
  --runs-dir samples/eval/stability --out samples/eval/reports/report.md
```

> **为什么要单独发稿**：如果标注者看到模型给了 85 分，他评出来的一定贴着 85 走，那不是盲评。
> 两人顺序不同也是刻意的——同序就会出现「你第 3 份多少分」，评的是对方不是简历。
>
> **`packet` 和 `status` 是为「真的会找人打分」这一步做的最小说法**：手发稿容易出现
> 「R02 拿到的是 R01 的稿」「某位漏了锚点卡」这类低级错误，而人去核对 6 个文件、2 个目录
> 一定漏；`status` 则把「还差什么」从三段文档压缩成三行终端输出——否则第二次找人时，
> 你自己都记不清上次卡在哪。

## 换成真模型

```powershell
$env:DSH_LLM_API_KEY  = "sk-..."                       # 必填
$env:DSH_LLM_MODEL    = "deepseek-chat"                # 可选
$env:DSH_LLM_BASE_URL = "https://api.deepseek.com/v1"  # 可选，任何 OpenAI 兼容端点

python scripts/run_eval.py --real                        # 纯文本
python scripts/run_eval.py --real --image 简历.png        # 附截图 → 才允许评排版
python scripts/run_eval.py --real --no-example           # 不注入 few-shot（省一半 token）
python scripts/run_eval.py --real --prompt v1.2          # 回退做 A/B
```

> **token 开销记在这里**：模板 7.3k 字符，注入 few-shot 后 16.2k 字符（样例占 55%）。
> 接线测试卡了「< 60%」的上限，防止样例以后盖过规则。

⚠️ `--image` 只在**真的把图片传给模型**时才加。不加时模型被告知"你拿不到排版信息"，
若它仍评价排版，脚本直接判错。

⚠️ 展示力检验（弱动词 / 结果位置 / 自评词）**不需要截图**，纯文本就能判，别因为没图就留空。

## 加入真实简历

```powershell
python scripts/extract_text.py samples --desensitize   # docx/pdf -> samples/txt/*.txt
# 在 samples/eval/dataset-v1.json 登记样本、岗位、JD 和 split
# 独立标注者在 samples/eval/annotations/ 提交双人盲评
python scripts/human_eval.py inventory --dataset samples/eval/dataset-v1.json
python scripts/human_eval.py report --dataset samples/eval/dataset-v1.json
```

> `samples/gold.json` 是旧版单人非盲分，只用于探索性 MAE；**不要再把补 gold 分当作准确度验证**。

## 结构/一致性指标

| 指标 | 目标 | 不达标说明 |
|---|---|---|
| `evidence_hit_rate` | ≥ 0.95 | 模型在引用简历里不存在的内容 → 它会给你编简历 |
| `invented_numbers_count` | = 0 | 改写里凭空造数字 → 面试一问就崩，**会害到用户** |
| `layout_hallucination_count` | = 0 | 没给图却评价排版 → 幻觉 |
| `weak_verb_missed` / `self_praise_missed` | = 0 | 该指出的展示力问题没指出 → 评审失去价值 |
| `mae_vs_human` | 仅旧版代理 | **不是准确度**；正式结果看 `human_eval.py` |
| `failed_cases` | 空 | 结构/分数/优先级/两关不自洽 → prompt 或 schema 要改 |

## 目录

```
VERSION                       当前发布版本：2.0.1
config/rubric.yaml            评分规范单一真源（阈值/权重/分档/验收门槛，改这里）
web/index.html                单页界面（三栏：导航 / 简历+评分 / 上传表单）
web/server.py                 零依赖后端（http.server）：上传、评审、导出、历史
src/worker.ts                 Cloudflare Worker 入口（Containers 部署用）
scripts/ingest.py             解析 pdf / 图片 / docx / txt，含 OCR 兜底与脱敏
scripts/review.py             调模型 → 质量门禁 → 渲染一页报告（CLI 也在这里）
scripts/quality_gate.py       交付门禁：schema、确定性字段、条目撤下、阻断判定
scripts/version_diff.py       两版简历对比：文本 diff、分数与维度变化、上次建议落实
scripts/rubric.py             读 config/rubric.yaml（零依赖 YAML 子集解析 + 缺键报错）
scripts/stats_metrics.py      统计内核：MAE / Pearson / Spearman / Cohen's κ / 自助区间
scripts/run_tests.py          一条命令跑完全部 20 个套件（CI 与本地同一入口）
scripts/run_eval.py           提示词渲染 + 批量评审（--mock / --real / --image）
scripts/check_output.py       结构 + 事实 + 规则三层校验（19 个必填字段）
scripts/local_engine.py       无 key 时的规则引擎：确定性打分，所有结论可回指原文
scripts/test_quality_gate.py  门禁与候选选择回归 34 项（不调模型）
scripts/test_privacy.py       隐私边界回归 13 项（图片同意 / 临时文件 / 删除）
scripts/test_history.py       历史归属与并发回归 9 项（review_id / 预览 / 旧记录）
scripts/test_version_diff.py  改版对比回归 33 项（相似度门限 / 不可比 / 落实推断 / 整链路）
scripts/test_engine_choice.py 谁能用哪个模型 28 项（自带 key / 内网拦截 / 换引擎不算分差）
scripts/test_local_engine.py  规则引擎契约 30 项（可回指 / 不自造数字 / 可复现）
scripts/test_mode_and_keycheck.py  模式按钮与 key 检测 16 项
scripts/test_rubric_single_source.py 规范单一真源 21 项（配置能读 / 代码只用配置 / prompt 不许落后）
scripts/test_stats_metrics.py 统计内核 43 项（算不出来要给 None，不能给 0）
scripts/benchmark.py          一键基准：heldout 默认锁死 → 指标 → bench/report.md（数据不够就报 NOT_READY）
scripts/test_benchmark.py     基准工具回归 19 项（留出集锁 / None 不给 0 / 报告口径）
scripts/dataset_build.py      数据集扩建：按内容去重 + 同组粘连 + 已标注样本切分冻结
scripts/test_dataset_build.py 数据集扩建回归 16 项（去重 / 切分泄露 / 冻结 / CLI 退出码）
scripts/rubric.py             读 config/rubric.yaml（零依赖受限 YAML 解析，重复键/坏行报错带行号）
config/rubric.yaml            所有阈值的唯一真源（每项都写了「为什么是这个数」）
scripts/human_eval.py         人工评测：数据就绪度、标注一致性、评分/改写/OCR 指标
scripts/test_human_eval.py    人工评测工具回归 10 项（构造数据，不冒充真实效果）
scripts/qa_smoke.py           接口冒烟 55 项（含版本一致性 / 错误文案 / 隐私 / 历史 / 体积 / 导出 / 删除）
prompts/system-v1.8.md        当前提示词：格式进决策链 + 八个检验
prompts/output-schema-v1.8.json  v1.8 契约
prompts/example-output-*-good.json  高分样例（同时作 few-shot）
samples/demo/                 4 份脱敏示例简历
samples/gold.json             旧版单人非盲人工分（仅探索性代理）
samples/eval/                 人工评测数据集、量表、标注/审阅模板与评测工具数据
docs/人工评测方案.md          双人盲评、留出集、稳定性、OCR 与发布门槛
docs/prd.md                   产品需求与验收标准
docs/                         PRD、部署、验收清单、变更记录（见下方索引）
Dockerfile / fly.toml / render.yaml / wrangler.jsonc   三种部署方式
```

### 为什么阈值要单独放一个文件

v1.x 里「多长算长句」「引用最短几个字」「两关的分数下限」散在 `check_output.py`、
`version_diff.py`、`ingest.py` 三个文件里，prompt 模板又各写一遍——改一次阈值要同步
三处，v1.7 和 v1.8 各漏过一次（prompt 写 60 字、脚本按 40 字判，报告的说辞和判定互相矛盾）。

v2.0 起改为：**数字只在 `config/rubric.yaml` 出现**，代码通过 `scripts/rubric.py` 读，
`scripts/test_rubric_single_source.py` 用三道闸口把它们钉在一起：

1. 代码里的常量必须**等于**配置里的值（不是"读到了就行"）；
2. 三个模块的源码里不允许再出现这些常量的字面量（防止改回去）；
3. prompt 模板里写的数字必须是配置里的数字（`60 字内`、`6~200 字`），过期阈值会 FAIL。

改阈值后的标准动作：

```powershell
# 只改 config/rubric.yaml，然后
python scripts/run_tests.py --only rubric -v
```

### 文档索引

| 想知道什么 | 看 |
|---|---|
| 怎么用（三步、报告怎么读） | [使用说明.md](使用说明.md) |
| 产品定义、验收标准、为什么这么设计 | [docs/prd.md](docs/prd.md) |
| 怎么上线（Render 主推） | **[docs/部署-Render.md](docs/部署-Render.md)**（逐步骤照做版）、[docs/上线操作单.md](docs/上线操作单.md)（总览与临时地址） |
| 想改走 Cloudflare Containers | [docs/部署-Cloudflare方案.md](docs/部署-Cloudflare方案.md) |
| 改了什么、为什么改 | [docs/CHANGELOG-v1.10.md](docs/CHANGELOG-v1.10.md)、[docs/CHANGELOG-v1.9.md](docs/CHANGELOG-v1.9.md)、[docs/CHANGELOG-v1.8.md](docs/CHANGELOG-v1.8.md)、[docs/CHANGELOG-v1.7.md](docs/CHANGELOG-v1.7.md)、[docs/CHANGELOG-v1.1.md](docs/CHANGELOG-v1.1.md) |
| 哪些必须人工验 | [docs/手动验收清单.md](docs/手动验收清单.md) |
| 准确度怎么验、样本和标注怎么做 | [docs/人工评测方案.md](docs/人工评测方案.md) |
| 和 kickresume 的差距 | [docs/kickresume-对比.md](docs/kickresume-对比.md) |

## 部署（给别人用）

本地跑和"所有人都能用"是两件事，前者现在就能用，后者见
[docs/部署-Cloudflare方案.md](docs/部署-Cloudflare方案.md)：三条路（Tunnel 0 改动 / Containers 保留 Python / Workers 重写）的取舍、命令与成本。

仓库根目录有 `Dockerfile`，任意容器平台（Cloudflare Containers / Fly / Render / 自己的服务器）都能直接用：

```bash
docker build -t resume-reviewer .
docker run -p 8080:8080 -e DSH_LLM_API_KEY=sk-xxx resume-reviewer
```

## 自检

```powershell
# 接口契约 / 隐私同意 / 错误文案 / 体积上限 / 导出 / 路径穿越 / 落盘 / 历史 / 删除（53 项，不花钱）
.\run_web.ps1 -Mock -Port 8790 -NoBrowser
python scripts\qa_smoke.py --base http://127.0.0.1:8790 --mock

# 真实模型 + 图片 OCR（会花几次调用，约 1~2 分钟）
python scripts\qa_smoke.py --base http://127.0.0.1:8765 --real --ocr
```

页面级（桌面 + 手机，真浏览器点一遍）用 `_test/ui_check.mjs`，需要 `playwright-core`，
属本地脚本、不入库。**脚本替代不了的手动验收**见 [docs/手动验收清单.md](docs/手动验收清单.md)。

## 已知局限（别当成已完成）

- **权重未校准**：25/25/20/15/15 是经验值，`check_output.WEIGHTS` 是唯一真源。
- **旧 MAE 2.0 不可信**：mock 剧本先于权重写，属于自我实现；旧 gold 也是单人非盲。
- **独立人工验证尚未完成**：0 名标注者、0 份双人标注、0 份 heldout；当前不得宣称准确率。
- **few-shot 只注入了 good**：故意不注入 weak，避免把模型往差里拉。
- **关思考后仍可能算错加权分**：超出 ±2 由代码重算；±2 内保留模型分，避免边界跳档。
- **单次评分抖动 ±5**：选「3 次取中位」缓解，不能消除。
- **规则引擎不是"轻量版大模型"**：它只看机械可判定的事实（结构、排版、关键词、量化率、动词强度、缺失章节），
  语义点评、说服力判断、改写质量都必须填 key 才有。两种模式的分数**不可横向比较**，历史里也不给分差。
- **key 会经过本站后端**：BYOK 模式下后端仍能看到 key（它要代你调模型）。要"谁都不经手"，
  只能自己在本地跑一份 `python web/server.py`，把 key 填在 `.env` 里。
- **图片/扫描件仍不做图像脱敏**：文字层脱敏只作用于提取出的文本。

## 下一步

v2.0 走了「可信度」这条线，进度：

| 项 | 状态 |
|---|---|
| 阈值单一真源（`config/rubric.yaml`） | ✅ 完成，三道闸口测试钉住 |
| 统计内核（MAE/κ/QWK/自助 CI） | ✅ 完成（`scripts/stats_metrics.py`，43 项测试） |
| 一键基准（`scripts/benchmark.py`） | ✅ 完成；**当前必然输出 NOT_READY**（数据不够） |
| 数据集扩建工具（`scripts/dataset_build.py`） | ✅ 完成；8 份 → 60 份需要真实简历 |
| 双人盲评 + 一致性 | 🟡 工具就绪（v1.15.0：`human_eval.py blind` 发稿、`arbitration` 找分歧；v2.0.1：`packet` 一键打包、`status` 一屏看进度）；仍卡在「0 名标注者」——只有人能解决 |
| 报告页接真实指标 | ✅ 完成（v1.11.0：页面可信度条 + 报告首屏一行 + `GET /api/metrics`；数据不够时显示「未验证」而不是 0） |
| 改稿闭环落实率 | ✅ 完成（v1.12.0：评审时落盘 `.meta.json`，`scripts/closure.py` 汇总；规则引擎的占位改写不进总落实率） |
| SSE 流式 | ✅ 完成（v1.14.0：四阶段事件 + 前端进度灯；假进度被测试禁止） |
| 2.0.0 发布 | ✅ 完成（2026-10-29，Render 已部署；2.0.1 补 `packet`/`status`） |

接下来按顺序：

1. **先攒数据**：把简历丢进 `samples/raw/`，`python scripts/dataset_build.py --source samples/raw`；
   到 60 份（其中 18 份 heldout）后补 `job_target`，再跑 `--verify`。
2. **找 2 名独立标注者**做盲评：`python scripts/human_eval.py packet --reviewers R01 R02 --out samples/eval/handouts`
   （一人一目录，含盲评稿 / 待填表 / 怎么填 / 锚点卡；v1.15 起稿子里只有正文 + 盲号，不含模型分数，两人顺序不同）。
   先拿 8 份 pilot 验证量表，用 `python scripts/human_eval.py status` 看还差什么，
   回来跑 `arbitration` 看分歧落在哪几个维度，分歧 >15 分的进第三人仲裁。
3. 标注齐了跑 `python scripts/benchmark.py`，`READY` 之后才能对外说准确率；
   报告页与 `/api/metrics` 会自动从「未验证」变成绿色并显示真实 MAE/κ（v1.11.0 已接好，只等数据）。
4. `python scripts/human_eval.py status` 通过（退出码 0）＝可以发 2.1；否则任何对外表述都必须带「未验证」。
