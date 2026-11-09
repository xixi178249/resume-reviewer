# v2.6.1：把「没改却多出一个版本」堵掉

发布日期：2026-11-09　规则版本仍为 v1.8（评分口径没动）

## 一句话

v2.6.0 上线后自己上线验收，抓到一个只在这个功能下才会犯的错：
**正文一个字没改，点一次「保存并重评」，版本链里就多出一个一模一样的版本。**

## 怎么发现的

线上真机走一遍完整链路（粘贴 → 评审 54 分 → 在线改简历 → 点「自己改这一版」→ 保存），
结果版本时间线出现两条内容相同的记录：

```
v2 手改 54 临界
v1 原文 54 临界
```

而我并没有改动正文。

## 根因

`web/server.py::api_chat_edit` 用逐字比较判断「有没有改」：

```python
if text == (prev.get("text") or "").strip():   # 改前
    return {"ok": False, "code": "CHAT_UNCHANGED", ...}
```

浏览器 `<textarea>` 交上来的换行是 CRLF（`\r\n`），存档里是 LF（`\n`）；
用户在 Windows 上**什么都没改**，字符串却已经不相等。
行尾多一个空格同理。

这属于「判据选错了」，不是「边界没考虑到」——**比较的是字节，不是内容**。

## 改了什么

新增 `web/server.py::_same_text()`，先归一化再比：CRLF/CR 统一成 LF、每行去掉行尾空白、整体 strip。

```python
def _same_text(a: str, b: str) -> bool:
    def norm(s: str) -> str:
        s = (s or "").replace("\r\n", "\n").replace("\r", "\n")
        return "\n".join(line.rstrip() for line in s.split("\n")).strip()
    return norm(a) == norm(b)
```

`api_chat_edit` 改用 `if _same_text(text, prev.get("text") or "")` 判「没改」。

故意**不**做「自动帮你规范化再存」：用户没改就不该产出新版本，
否则时间线会变成一堆看不出差别的 v2/v3/v4，而「版本链」正是这个产品的卖点之一。

## 验证

| 项 | 结果 |
| --- | --- |
| 新测试 `test_edit_rejects_crlf_only_change` | 只差 CRLF → `CHAT_UNCHANGED`；只差行尾空格 → `CHAT_UNCHANGED`；且**不留下版本** |
| 全量 | **28/28 套件、586 条断言**（v2.6.0 是 585） |
| 本地复现 | 改前：CRLF 保存 → `version=2`；改后：→ `CHAT_UNCHANGED` |

## 顺手记下的一条规矩

凡是「有没有发生变化」这类判断，**先归一化再比较**（换行、空白、全半角、URL 尾斜杠），
否则 Windows / 浏览器 / 代理随便哪一层换个编码，就会出现「没改也算改」。

## 补一个 v2.6.2（同日）

修完之后在线上走了一遍「什么都不改，点保存」：请求被 `CHAT_UNCHANGED` 正确拒掉了，
版本链也没多出记录——但**编辑器自己合上了**，只在框里写了一句提示，框一关就看不见了。
用户看到的是「点保存 → 框没了 → 什么都没变」，与「存成功了」几乎无法区分。

改法：`web/chat.html::saveEdit()` 在 `CHAT_UNCHANGED` 时先 `closeEditor()`，
再用横幅说「正文和当前版一样，没存新版本。要改就往里动几个字。」
——状态是对的，但**无声的正确等于没说**。
