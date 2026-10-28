# v1.8.0（2026-10-28）

> 触发原因：用户要求「更新网站」，但网站从未上线过。修完 CI 之后用**真模型**跑，
> 同一份简历连续 4 轮被质量门禁拒收——每轮都是我自己的 bug，不是模型不听话。
>
> 规则版本：**v1.8**
> 产品/包版本：**1.8.0**
> 提示词与输出契约：`prompts/system-v1.8.md`、`prompts/output-schema-v1.8.json`

---

## 1. 「看不见内容」从一句话变成一条证据

`format_review.verdict = blocking` 是最贵的一句结论：它会直接把 `gate_hr` 判为不过。
之前只在「有截图（`visual_level.known=true`）」时核对它与 10 项检查是否一致；
没有截图时，模型写一句 `blocking` 就能让一份正常简历的初筛结论翻掉。

- 无截图（`known=false`）时判 `blocking`，必须给出**文本层证据**之一：
  联系方式提取不到 / 乱码 ≥50 字 / 关键章节缺 3 类以上。三项都不成立 → 报错，提示改判 `risky`。
- 有截图时判 `blocking`，10 项里必须有**致命项** `ok=false`（致命项仅限
  图片化文字、分栏或表格、是否超出1页、章节命名标准、联系方式可提取）。
  只有非致命问题时应判 `risky`。

## 2. 重试从「再赌一次」变成「照着错误改」

`scripts/review.py` 新增 `with_repair_note()`：某个候选被门禁拒绝后，
把它被拒的**具体原因原文**作为追加消息回灌给模型，再请求一次完整 JSON。

- `repeat=1` 时才会自动补一次（`repeat>1` 的候选本来就互为备份，不额外加调用）。
- 原因最多带 6 条，避免把上下文撑爆。
- 实测：不带原因重试，同一份简历会连续撞同一堵墙；带原因后第二轮基本逐条修掉。

## 3. 数字溯源的报错要能被行动

- 报错从「实际只审计了 1 个」改为列出具体漏掉的数字：`漏了：3%`。
- prompt 里的口径同步改成「查过就要留痕」：每个百分比指标都要进 `number_audit`，
  口径完全说得清也写 `source_clarity: clear`；≤3 个必须全写，>3 个至少 3 个。

## 4. 三处过紧的契约

| 项 | 原来 | 现在 | 依据 |
|---|---|---|---|
| `one_line_summary.maxLength` | 200 | 260 | 模型一句话自然写到 206，200 是按手写样例定的 |
| `issues[].verify_question` | 只允许 `string` | `["string", "null"]` | 该字段只在 `assertion=inference` 时需要 |
| `gate_hr.reason` / `gate_interview.reason` | 150 | 200 | 真模型自然写到 155 |
| `format_review.summary` | 160 | 200 | 同上 |

## 5. 接线测试补上了真正的漏洞

旧检查只验证「字段名在 prompt 模板**任意位置**出现过」，而 `hard_blockers` 的 `assertion`
恰好在 `issues` 那一行出现过，于是一路绿灯，模型却按缺字段的格式输出。

- 新增断言：**字段表每一行必须写全该数组的必填项**（抓表格行 → 比对 schema `items.required`）。
- 新增断言：prompt 必须写明「查过就要留痕」的数字溯源口径。
- 已验证：这两条对 v1.7 的 prompt 会 FAIL，对 v1.8 通过——测试有牙。

## 6. 测试与验证

- `python scripts/run_tests.py`：**8/8 套件，121 条断言**（v1.7 为 114）。
- 新增 `BlockingEvidenceTest`（4 条）：无证据判 blocking 被拒 / 有联系方式证据放行 /
  有截图但无致命项被拒 / 补上致命项后放行。
- 新增 `RepairNoteTest`（3 条）与 `NumberAuditMessageTest`（1 条）。
- 真模型实测（deepseek-flash，thinking 开）：连挂 3 次的「05 产品经理」简历现在一次通过，
  43 秒出结果，**62 分 / borderline**，4 个百分比全部审计，格式判 `risky` 而不是 `blocking`。

## 7. 仍未解决

- 8 份真实简历的人工金标仍是 0 份，MAE 无法作为结论（只能当探索性指标）。
- 阈值（长句 60 字、证据 6~200 字、覆盖 3 个）仍散落在代码与 prompt 两处，没有单一真源。
- Cloudflare Containers 路线需要付费 Workers 计划；免费路线走 Render。
