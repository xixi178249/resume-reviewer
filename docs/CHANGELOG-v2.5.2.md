# v2.5.2：修线上 `/rules` 是空壳

> 一句话：本地 `/rules` 有 9 张表，公网那页只有一句「发布包不完整」。
> 原因不是代码写错，是 `.dockerignore` 把 `docs/` 排除了。

## 改前什么样

我推完 v2.5.1 之后照着发布流程实机验：

```text
$ curl -s https://resume-reviewer-x3pf.onrender.com/api/health | ...
version 2.5.1
industries 11 通用（不指定行业）

$ curl -s https://resume-reviewer-x3pf.onrender.com/rules | grep -o "不可用[^<]*"
不可用（发布包里缺少
```

`GET /rules` 返回 200、2,099 字节，内容是 `web/rules_page.py` 里那句兜底文案：

```python
MISSING = (
    "<p>评分规则文档不可用（发布包里缺少 <code>docs/评分规则.md</code>）。"
    "这不影响评审，但说明发布包不完整。</p>"
)
```

而本地同一页是 10,473 字节、9 张表。也就是说 v2.3 花了一整个批次做的
「规则公开」，在公网版上等于没做——用户点「评分规则公开」，看到的是一句道歉。

## 根因

`.dockerignore` 里两行：

```text
docs/
*.md
!README.md
```

`rules_page.render()` 只认 `docs/评分规则.md` 这一个文件来源，容器里没有它，
于是走了兜底分支。**本地全绿、镜像里没有**——这正是自评 v2.5 里写的那类
「测不到的地方」，只是这次真的被线上验出来了。

## 改后什么样

`web/rules_page.py` 加了第二条来源：**没带文档就现场生成**。

```python
def _body_without_doc() -> str:
    try:
        import gen_rules_doc
        return md_render.render(gen_rules_doc.render())
    except Exception:
        return MISSING
```

为什么选「现算」而不是「把 docs 打进镜像」：

1. **同一个生成器**。`gen_rules_doc.render()` 就是生成 `docs/评分规则.md` 的那个函数，
   现算出来的表和仓库里那 9 张出自同一段代码，不可能漂移；把文件 COPY 进镜像反而多一份可漂移的副本。
2. **镜像不带中文文件名**。`docs/评分规则.md` 是中文文件名，在 Windows 上写 Dockerfile 的
   `COPY docs/评分规则.md` 有编码风险（换成 `COPY docs ./docs` 又得改 `.dockerignore` 的两条排除规则，
   两条规则互相打架，容易写成「有时带上有时不带」）。
3. **代价可忽略**。一次生成是毫秒级，而且 `render()` 本来就有进程内缓存。

兜底文案也改了，现在它只在**两条路都断了**的时候出现：

```text
评分规则文档不可用：既没带上 docs/评分规则.md，也没法从 config/rubric.yaml 现场生成。
```

## 钉住它的断言

`scripts/test_rules.py` 里原来那条 `test_rules_page_survives_missing_doc` 断言的是
「文档丢了也要给人话」，等于**把错误行为写进了测试**。改成：

```python
def test_rules_page_falls_back_to_generating_from_config(self):
    # 把 SOURCE 指到不存在的文件，模拟「镜像里没带 docs/」
    ...
    self.assertNotIn("不可用", html)                      # 不许退化成一句道歉
    self.assertGreaterEqual(html.count("<table>"), 6)     # 表要齐全
    self.assertEqual(html.count("<table>"),
                     normal.count("<table>"))             # 与读 md 的路径表数一致
```

实测：缺文档时 **9 张表**、无「不可用」、9,459 字节；正常路径同样 9 张表。

## 数字

| 项 | 改前 | 改后 |
|---|---|---|
| 线上 `/rules` 体积 | 2,099 B（一句道歉） | 9,459 B（9 张表） |
| 本机 `test_rules.py` | 25 项 | 26 项（改了 1 项语义） |
| 全量测试 | 27 套件 / 569 断言 | 27 套件 / 569 断言（只换语义，不加减） |

## 顺手记一笔：推送是怎么恢复的

这次是**先把凭据问题解决，才可能看到线上是空壳**。

- 推送从 10-29 之后一直失败：`could not read Username for 'https://github.com'`；
  Windows 凭据管理器里没有 github 条目、没有 `~/.git-credentials`、没装 `gh`。
- 22 端口在这台机器上是封的：`ssh: connect to host github.com port 22: Connection refused`。
- 解法：本地生成 `~/.ssh/id_ed25519` → 在仓库 Settings → Deploy keys 加**带 Allow write access**
  的部署密钥 → `~/.ssh/config` 把 `github.com` 指向 `ssh.github.com:443` →
  `git remote set-url origin git@github.com:xixi178249/resume-reviewer.git`。
- 结果：`3bc6af4..05de1a8  main -> main`，13 个提交一次性上去，Render 自动部署，
  两分钟内 `/api/health` 从 `2.0.2` 跳到 `2.5.1`。

## 没做的

1. **没有把 `docs/` 打进镜像**。现算已经够，打进镜像多一份副本、多一处漂移。
2. **没有给 `.dockerignore` 加白名单例外**。`docs/` + `*.md` 两条规则叠加，例外写法很容易出错
   （`!docs/评分规则.md` 在父目录被排除时不生效）。
3. **没有做「发布包完整性自检」的自动门**。现在只有 `/rules` 这一处会暴露漏拷，
   更系统的做法是在 `selfcheck.py` 里加一条「镜像内文件清单核对」，等下次动发布流程再做。
