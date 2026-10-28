# 在 Render 上部署成公网网址（逐步骤照做版）

> 目标：让任何人打开一个 `https://xxx.onrender.com` 就能用这个简历评审助手。
> 选定路线：**Render 免费 Web Service（Docker）**。不需要信用卡、不需要装 Docker、不需要买域名。
> 仓库里已经准备好 `render.yaml` 与 `Dockerfile`，所以下面 90% 的操作是"点几下"。
>
> 阅读顺序建议：①→②→③ 只做一次（约 10 分钟），④以后每次发版看一遍。

---

## ① 你需要准备什么

| 需要的 | 说明 |
|---|---|
| 一个 GitHub 账号 | 你已经有了：`xixi178249/resume-reviewer` |
| 浏览器 | 随便 |
| 时间 | 首次 10 分钟（其中 3~6 分钟是等构建） |
| 信用卡 | **不需要** |
| Docker / 命令行 | **不需要**，构建在 Render 那边跑 |
| 模型 API Key | **不需要**。v1.9 起网站只用「访客自己填的 key」或「规则引擎」 |

---

## ② 先确认仓库是最新的（本地 30 秒）

在项目目录里跑：

```powershell
cd "D:\嘻嘻哈哈\vibecoding项目\resume-reviewer"
git status --short          # 应该没有输出（有输出就先提交）
git push origin main        # 直连失败就加代理，见 note
```

> 推送要代理时用：`git -c http.proxy=http://127.0.0.1:7897 push origin main`
> （直连 GitHub 会 `Recv failure: Connection was reset`，是网络问题不是权限问题。）

推完确认这两个文件在仓库里、且是最新版：

- `render.yaml` —— Blueprint 配置文件（Render 照着它建服务）
- `Dockerfile` —— 镜像定义（`python:3.13-slim` + 中文字体 + 依赖）

`Dockerfile` 最后一行必须长这样（**写死 8080 会导致 502**）：

```
CMD ["sh", "-c", "exec python web/server.py --host 0.0.0.0 --port ${PORT:-8080}"]
```

---

## ③ 创建服务（约 5 分钟）

### 3.1 登录

1. 打开 <https://dashboard.render.com/>
2. 点 **Get Started / Sign in**，选 **GitHub**（用同一个账号登录，省掉后面授权）。

### 3.2 用 Blueprint 建（推荐，一条命令都不用敲）

1. 点右上角 **New +** → 选 **Blueprint**。
   - ⚠️ 别选 **Web Service**：那样要手填一堆配置，容易漏。
2. 页面问 **Connect a repository**：找到 `xixi178249/resume-reviewer` → 点 **Connect**。
   - 第一次会跳 GitHub 授权页：选 **Only select repositories**，勾这一个仓库 → **Install & Authorize**。
3. Render 读完 `render.yaml` 后，会显示一张服务卡片，应该是：

   | 项 | 值 |
   |---|---|
   | Name | `resume-reviewer` |
   | Language / Runtime | `Docker` |
   | Branch | `main` |
   | Instance Type | `Free` |
   | Region | `Singapore` |
   | Health Check Path | `/api/health` |

4. **环境变量**那里只会剩常规几项（模型名、接口地址、超时、限流）。
   如果它提示一个叫 `DSH_LLM_API_KEY` 的变量 —— 我们已经在 v1.9 把它从 `render.yaml` 删掉了，
   **如果你看到它，留空即可**：网站不靠站长的 key 工作。
5. 点 **Apply**（有的版本叫 **Create Service**）。

### 3.3 等它构建

页面会进入 **Logs**，形态是：

```
==> Cloning from https://github.com/xixi178249/resume-reviewer
==> Checking out commit ...
==> Building Docker image
... apt-get install fonts-noto-cjk ...
... Successfully installed python-docx pymupdf pillow ...
==> Starting service...
简历评审站点：http://0.0.0.0:10000            ← 端口由 Render 注入，不是 8080
==> Your service is live 🎉
```

- 首次构建 **3~6 分钟**（装中文字体那一步最慢）。
- 日志里出现 `Your service is live` 就成功了。
- 如果卡在 `apt-get`：在 Render 页面点 **Manual Deploy** → **Clear build cache & deploy** 重试一次。

---

## ④ 拿到网址并验收

### 4.1 网址

服务页顶部会显示形如下面的地址（`xxxx` 是随机串，别照抄）：

```
https://resume-reviewer-xxxx.onrender.com
```

### 4.2 五步验收（建议按顺序点一遍）

| # | 操作 | 期望结果 |
|---|---|---|
| 1 | 打开 `https://你的地址/api/health` | 返回 JSON：`{"ok":true,"mock":false,"version":"1.9.1","requires_user_key":true,"engines":["llm","local-rules"],"rubric_version":"v1.8",...}` |
| 2 | 打开首页 | 三步式界面；右上角没有红色横幅（有红条=health 探测失败） |
| 3 | 粘贴一段简历文字 → **下一步** → **不填 key** → 开始评审 | **1~3 秒**出报告，头上标着「规则引擎（未调模型）」 |
| 4 | 输入框里填一个假 key（如 `sk-123`）→ 开始评审 | 立刻报 `LLM_BAD_KEY key 格式不对`，不会白等 40 秒 |
| 5 | 填真的 DeepSeek key → 开始评审（次数选 1） | 15~45 秒出报告，标注模型名；再点一次「3 次取中位」约 1~2 分钟 |

额外两项可以找个室友做：

- 手机 4G 打开同一地址：单栏布局，能完整走完流程（证明公网可达）。
- 上传一份 PDF：自动抽文字；扫描件会提示"是否把页面图发给模型"，取消则退回纯文本评审。

> 第 5 步花的是**填 key 那个人自己**的额度。你看不到、也不经手别人的账单。

### 4.3 免费实例的两个脾气

| 现象 | 原因 | 处理 |
|---|---|---|
| 15 分钟没人访问后，第一次打开要等 **40~60 秒**白屏 | 免费实例休眠了，正在冷启动 | 正常，等着就行。想一直在线：升级 Starter（$7/月），或定期访问保活 |
| 休眠后点"开始评审"报 `会话已过期，请重新上传` | 休眠重启，内存里的会话没了 | 重新上传即可。公开站点本来就不在服务器留简历 |
| 大 PDF（>10MB）上传后 502 / 转圈很久 | 免费实例只有 **512MB 内存**，PDF 转图会吃满 | 在本地压到 5MB 以内再传；或直接粘文字 |

---

## ⑤ 环境变量（都已在 `render.yaml` 里，想改在面板 Environment 改）

| 变量 | 值 | 作用 |
|---|---|---|
| `HOST` | `0.0.0.0` | 必须监听所有网卡，否则平台健康检查连不上 |
| `DSH_LLM_MODEL` | `deepseek-flash` | 访客填 key 但没填模型名时的默认模型 |
| `DSH_LLM_BASE_URL` | `https://api.deepseek.com/v1` | 默认接口地址（OpenAI 兼容） |
| `DSH_LLM_THINKING` | `off` | 关掉思维链：访客等 40 秒还能忍，等 3 分钟就关了页面 |
| `DSH_LLM_TIMEOUT` | `300` | 单次调用超时（秒） |
| `REPORTS_DIR` | `/tmp/reports` | 报告落盘目录（临时盘，重启即清） |
| `RATE_PER_HOUR` | `20` | 单 IP 每小时允许的**模型**评审次数（自带 key 的只防刷，规则引擎不计） |
| `DAILY_LIMIT` | `300` | 全站每天上限（不花钱，但防被人刷崩内存） |

**不要设置**：

- `PORT` —— Render 自己注入，覆盖它会 502。
- `DSH_LLM_API_KEY` —— v1.9 起网页永不代用站长 key。命令行批量跑评审时才需要，那是在你本机 `.env` 里配。
- `DSH_LLM_ALLOW_LOCAL` —— 除非你确实要允许访客填内网地址（比如自建 Ollama 隧道），默认一律拒绝，防跳板。

---

## ⑥ 以后怎么更新

`render.yaml` 里 `autoDeploy: true`，所以：

```powershell
cd "D:\嘻嘻哈哈\vibecoding项目\resume-reviewer"
git add -A
git commit -m "feat: ..."
git -c http.proxy=http://127.0.0.1:7897 push origin main
```

推上去后 Render 会自动重建（3~5 分钟），旧实例一直服务到新的起来为止，访问不中断。

手动触发：Render 服务页 → **Manual Deploy** → **Deploy latest commit**。

回滚：Render 服务页 → **Deploys** 标签 → 找到上一个绿勾 → 右侧 **⋯** → **Redeploy**。

---

## ⑦ 故障排查（按现象查）

| 现象 | 根因 | 怎么修 |
|---|---|---|
| 部署成功但打开是 **502** | 容器没监听平台注入的端口 | 确认 `Dockerfile` 的 `CMD` 用了 `${PORT:-8080}`；改完 push |
| 构建阶段红：`apt-get` 失败 | 装中文字体时网络抖动 | Manual Deploy → Clear build cache & deploy |
| 构建阶段红：`No such file requirements.txt` | 仓库根目录不对（应是 `resume-reviewer/` 为仓库根） | 确认 `git ls-files` 里 `requirements.txt` 在第一层 |
| health 一直不过、日志却没报错 | `/api/health` 被改成要鉴权，或端口不对 | `curl https://你的地址/api/health` 应返回 200 JSON |
| 点评审报 `LLM_BAD_BASE_URL ... 内网地址` | 访客填了 `http://127.0.0.1` / `192.168.x` | 这是有意拦的（防 SSRF）。要放行才设 `DSH_LLM_ALLOW_LOCAL=1` |
| 点评审报 `LLM_HTTP_401` | 访客 key 错/过期 | 换 key，或改完直接点重试（不用重传简历） |
| 点评审报 `LLM_HTTP_402` | 访客自己余额不足 | 让他充值或换服务商；本站不掏钱 |
| 点评审报 `LLM_TIMEOUT` | 模型太慢（>300s）或网络抖 | 重试；或把「3 次取中位」改成 1 次 |
| 报告写"有 1 处硬错误被过滤" | 质检把编造的引用/数字拦掉了 | 这是特性：宁可少一条，也不给错一条。重试一次通常就好 |
| 页面「服务连接不上」黄条 | 本地打开 `file://`，或服务已停 | 必须用 `https://` 地址打开；先看 `/api/health` |

---

## ⑧ 可选：绑自己的域名

1. Render 服务页 → **Settings** → **Custom Domains** → **Add Custom Domain**，填 `resume.example.com`。
2. 到你的 DNS 服务商加一条记录：
   - 类型 `CNAME`，主机名 `resume`，值 = Render 给的 `xxx.onrender.com`
3. 回 Render 点 **Verify**；TLS 证书自动签发（几分钟）。
4. 根域名（`example.com`）需要 `ALIAS`/`A` 记录，用 Render 面板给的具体值。

免费实例绑自定义域名不额外收费。

---

## ⑨ 备选方案（本次不用，留着以防万一）

### B. 不用 Blueprint，手动建 Web Service

1. **New +** → **Web Service** → 连同一个仓库。
2. 手动填：Runtime = **Docker**，Branch = `main`，Dockerfile Path = `./Dockerfile`，Region = Singapore，Plan = **Free**。
3. **Advanced** → Health Check Path = `/api/health`。
4. 环境变量照第 ⑤ 节一张表逐项添加（**不要加 PORT**）。
5. **Create Web Service**。

### C. 本地 + Cloudflare 隧道（临时演示用，5 秒起）

```powershell
cd "D:\嘻嘻哈哈\vibecoding项目\resume-reviewer"
python web/server.py --host 0.0.0.0 --port 8765                      # 窗口一
D:\嘻嘻哈哈\vibecoding项目\.tools\cloudflared.exe tunnel --url http://127.0.0.1:8765   # 窗口二
```

窗口二会打印一个 `https://xxx.trycloudflare.com`，谁拿到地址谁就能用。
地址每次重启都变，只适合临时给室友看。**访客仍要填自己的 key 才能走大模型**。

> 注意：本机跑时 `.env` 里的 `DSH_LLM_API_KEY` 只用于**命令行**（`scripts/review.py`）。
> 网页端不会拿它替访客付钱 —— 这是 v1.9 的硬规则，代码里那条路径已经删掉了。

---

## ⑩ 一页速查

```
仓库        github.com/xixi178249/resume-reviewer   (main)
建服务      Render → New + → Blueprint → 选仓库 → Apply
环境变量    照 render.yaml，别加 PORT、别加 DSH_LLM_API_KEY
构建        3~6 分钟；卡 apt 就 Clear build cache & deploy
验收        /api/health → 首页 → 无 key 评审(1~3s) → 假 key 报错 → 真 key 评审(15~45s)
免费限额    15 分钟休眠、512MB 内存、单 IP 20 次/小时、全站 300 次/天
发版        git push → 自动重建；回滚在 Deploys 里 Redeploy
出问题      先看 Render → Logs，再看本文件第 ⑦ 节
```
