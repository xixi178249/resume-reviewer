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
