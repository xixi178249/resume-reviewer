# v1.9.0（2026-10-28）

> 触发原因：用户要求「别人要使用时，我想让其使用自己的 apikey 进行审查，改成这个调用入口」。
>
> 规则版本：**v1.8**（本次不改 rubric，只改「谁来调模型」）
> 产品/包版本：**1.9.0**
> 新增测试：`scripts/test_engine_choice.py`（23 条断言）；套件总数 181 条

---

## 1. 三条入口，页面上一眼看明白

| 你填了 | 走哪条路 | 花谁的钱 |
|---|---|---|
| 自己的 API Key | 你的 key → 你指定的模型 | 你的额度 |
| 不填，服务器 `.env` 有 key | 服务器 key | 站点所有者 |
| 不填，服务器也没 key | 本地规则引擎（`scripts/local_engine.py`） | 零成本 |

`web/server.py` 的 `api_review()` 里这是唯一一处分支：

```python
api_key = (payload.get("api_key") or "").strip()
engine = "local-rules"
if api_key:                      # 自带 key
    llm_cfg = run_eval.config_from_request(api_key, base_url=..., model=...)
    engine = "llm"
elif os.environ.get("DSH_LLM_API_KEY", "").strip():   # 服务器自己的 key
    engine = "llm"
```

规则引擎是确定性的，所以 `repeat` 在 `local-rules` 下被强制为 **1**——多跑两次分数一模一样，只是浪费等待时间。

## 2. key 的生命周期：只活在一个标签页里

- 前端存 `sessionStorage`（键 `rr.llm`），关标签页即失；页面没有登录态，不落任何持久存储。
- 后端只在这一次请求的作用域里用它构造 `LLMConfig`，不写报告、不写历史、不写访问日志（`log_message` 本来就静音）。
- `LLMConfig.describe()` 只输出模型名/地址/来源，用的命令行报错里也拿不到 key。
- 历史记录里写入的是 `engine` 与模型名，不写 key；因为 key 不进 `_review_config_hash()`。

## 3. 换引擎不能算「涨了 N 分」

`_review_config_hash()` 的 payload 新增两个字段：

```python
"engine": engine,                                  # llm / local-rules
"model": model_name, "base_url": base_url,         # 不再是只读环境变量
```

理由：规则引擎的 65 分和模型的 65 分不是同一把尺子。哈希不同 → 历史里只提示「配置不同，不可比」，不给分差。

## 4. 挡住「填个地址让服务器替我去访问内网」

`config_from_request()` 用 `_assert_public_base()` 校验地址：协议必须是 http(s)；
两道检查：① 字面量 `localhost` / `127.0.0.1` / `::1` / 私网段 / 链路本地段（含 `169.254.169.254` 云元数据）直接拒绝；
② 域名再解析一次，任何 A/AAAA 落在内网也拒绝（防 `localhost.` 变体与"把域名解析到 127.0.0.1"的 rebinding）。
错误码 `LLM_BAD_BASE_URL`。自己跑 Ollama 的人显式设 `DSH_LLM_ALLOW_LOCAL=1` 才放行。
解析失败**不**当攻击处理：离线或临时 DNS 故障不该把正常用户挡在门外，让真实请求去报网络错误。

同时收紧了 key 的格式校验（`LLM_BAD_KEY`）：不能含空白或引号；但**外层引号会被自动剥掉**——
从 `.env`/JSON/终端复制粘贴最常见的形态就是 `"sk-..."`，不该因此报错。

## 5. 限额改成「谁付钱谁受限」

`allow_request()` 新增 `limit` / `daily` 覆盖参数：

- 自带 key：`limit=max(RATE_PER_HOUR*10, 120)`，`daily=0`（不占全站额度，只防脚本刷）
- 用服务器 key：按 `repeat` 计（真的会烧钱）
- 规则引擎：按 1 次请求计，`daily=0`（不调模型，不烧钱）

界面上对应一处自适应提示：`/api/health` 返回 `has_key` / `engines` / `allow_local_base`，
前端据此把「不填 key 会怎样」写成三句不同的话（用你自己的 key / 用本站 key 但有限额 / 走规则引擎）。

## 6. 规则引擎的 4 处误报一并修掉

真跑才发现，规则引擎拿到样例文首的「转录说明」会把备注行当成经历条目：

- `_SKIP_BULLET` 增加 `转录说明|人工转写|字符识别|OCR|脱敏|原件核对|识别误差|主修课程|核心课程|课程：`
- 改写脚手架先剥掉句末标点再拼占位符，避免出现 `……全流程；，[待补充：…]`
- 报告里「这条改了事实强度」只对 `fact_delta` 为 `ownership_upgrade` 之类的改写显示；
  `fact_delta=wording` 的占位符改写只提示「还有待补充事实」，不吓人
- 「处理说明：粘贴文本」→「处理说明：来自粘贴的文本」

## 7. 测试

```
[ PASS  ] engine-choice       0.3s  断言 23/23
[ PASS  ] local-engine        0.3s  断言 30/30
...
全部通过：10/10 个套件，181 条断言。
```

`test_engine_choice.py` 覆盖四类：key 格式（空/含空白/引号/过长）、SSRF 拦截（8 个内网字面地址逐个断言被拒 +
域名解析到私网被拒 + 解析到公网放行 + DNS 失败按正常放行）、配置哈希可比性（换引擎/换模型/改次数都必须不等）、
限额覆盖（自带 key 放宽但仍封顶）。

`test_privacy.py` 里两处 `fake_run()` 补了 `config=None` 形参——`run_reviews()` 现在多传一个 `config`，
这是签名变化唯一漏掉的测试替身，跑全量才暴露。
