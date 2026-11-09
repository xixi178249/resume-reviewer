# v2.6.0：没 Key 也能用（只是它不会替你写句子）

发布日期：2026-11-09　规则版本：仍为 v1.8（评分口径一条没动）

## 一句话

以前不填 Key 就开不了改稿工作台——那等于宣称「不用 Key 就用不了」。
现在没 Key 以**规则模式**开台：你自己动笔，系统按同一套 rubric 重评并当场报差；
Key 只决定多不多「逐句改写」和「排版视觉评估」。

## 改前长什么样（证据）

| 位置 | 代码 | 用户看到 |
| --- | --- | --- |
| 评审页 | `web/index.html:797` `let REVIEW_MODE = 'llm';` | 打开就是 AI 模式，第一眼是 Key 输入框 |
| 评审页点开始 | `if (REVIEW_MODE === 'llm' && !currentKey()) { showErr(...); return; }` | 「需要先填自己的 API Key」，没有下一步 |
| 改稿工作台 | `web/server.py::api_chat_start` 返回 `code: "CHAT_NO_KEY"` | 「改稿工作台要调模型，必须填你自己的 API Key」 |
| 改稿工作台前端 | `web/chat.html::ensureChat()` 中 `if (!k) { banner(...); return false; }` | 页面停在横幅，什么都不能做 |

用户报的正是最后一条：选「不用 Key」被挡住，且「并没有走基础规则评测」。

## 改了什么

### 1. 评审页：默认基础评测

- `REVIEW_MODE = 'local'`；`loadKey()` 里改成「存过 Key 的人才默认 AI 模式」（`o.key ? 'llm' : 'local'`）。
- AI 模式下没 Key 点开始：不再 `return`，改 `setMode('local')` + 一句说明，继续跑完出报告。
  理由：拦一次就等于「没 Key 用不了」，这条已经被人当成缺陷提了一次。

### 2. 改稿工作台：无 Key = 规则模式

`scripts/chat_engine.py`

- `ChatSession.send()` 开头 `if self.config is None: return self._rules_reply(message)`。
- `_rules_reply()`：用 `local_engine.review_text(text, job, jd, industry)` 拿同一份 doc，
  输出「当前 N 分 + 最该动的三处 + 按顺序做这几件事 + 想让我动句子就去填 Key」，
  返回 `{ok, manual: True, patches: [], problems: [], version, score}`。
  **刻意不生成补丁**：模板句拼出来塞进简历只会更难读。
- `_rereview()`：无 config 时走 `local_engine`，`scores = [doc["overall_score"]]`；
  模型那条路抽成 `_model_review()`，两条路共用后面「存档 + 比差 + 给建议」的逻辑。
- 版本元数据 `engine` 按模式写 `local-rules` / `llm`（原来写死 `"llm"`）。

`web/server.py`

- `api_chat_start()`：`cfg = None` 表示规则模式；返回体加 `"mode": "model"|"rules"`；
  合法 key 才走 `config_from_request`（key 格式错仍报 `LLM_BAD_KEY`，**不许悄悄降级**）。
- 无 Key 时**同步**用 `_score_version()` 打 v1 分（规则打分毫秒级），有 Key 才丢后台线程。
- `_score_version()`：无 config 走 `local_engine`，并写 `engine` 元数据。
- 新增 `api_chat_edit(payload)`：整篇替换正文 → `store.append(..., source='manual')` →
  `chat._rereview()` → 返回 `{ok, version, score, before, delta, mode, versions}`。
  拒收条件：空文本 `CHAT_EMPTY_TEXT`、短于 120 字 `CHAT_NO_TEXT`、超过 4 万字 `CHAT_TOO_LONG`、
  与原版一致 `CHAT_UNCHANGED`。
- `/api/chat/<step>` 的 handlers 里注册 `"edit": api_chat_edit`。

`web/chat.html`

- 去掉 `ensureChat()` 里「没 Key 就 return false」的硬门；Key 输入框文案改「可选 · 填了才有逐句改写」。
- 开台后按 `r.mode === 'rules'` 给横幅：说明「不调模型 + 自己改 + 填 Key 才解锁改写」。
- 正文标题旁新增 **「自己改这一版」** → 文本框（`#editText`）→ 「保存并重评」
  → `POST /api/chat/edit` → 收起编辑器、刷新到新版本、横幅报分差；
  网络失败时**不关编辑器**（字不能丢），只提示「再存一次」。
- `send()`：`r.manual` 为真时不再报「这一轮没有给出可用补丁」，改提示去点「自己改这一版」。
- 版本时间线：`source` 映射补上 `manual → 手改`（原来手改的版本也显示「原文」）。

## 验证

| 项 | 数字 |
| --- | --- |
| 全量测试 | **28/28 套件、585 条断言**（v2.5.2 是 27/569，新增 `chat-rules` 16 条） |
| 自评探针 | **40/40 PASS**（F13 改稿工作台 2 条 → 5 条） |
| 真浏览器（`--mock`，端口 8921） | 默认 `mode=local`；切 AI 无 Key 自动退回并出报告 48 分；点「在线改简历」→ `/chat?chat_id=…`、横幅=无 Key 模式、`v1 · 195 字 · 48 分`；手改后 `v2 · 246 字 · 49 分`、横幅「分数涨了：v2 49 分（+1）」 |

## 没做的

- **没让规则模式生成改写句子。** 那需要模型；用模板硬拼出来的句子会污染简历，也会让人以为系统在瞎编。
- **没把排版（视觉）评估搬进规则模式。** 视觉项（字号、留白、单页）在规则侧只能判「正文里看不到」，
  真有价值的部分要模型看图；现在只保留 ATS 的平台判读。
- **没做「改完自动提示哪里还是空的」。** 占位符提醒在报告侧已有，工作台这轮没接。
