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
