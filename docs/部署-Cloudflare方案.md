# 把简历评审助手变成"所有人都能用"的网站：Cloudflare 三条路

> 核实时间：2026-10-23。价格与限额以 Cloudflare 官方文档为准（文末列了出处）。
> 先给结论，再给每一步命令。

---

## 0. 结论先说

现在的服务是**本地 Python 进程**（`http.server` + PyMuPDF + Pillow + 本地磁盘），
Cloudflare Workers **跑不了它**：Workers 只有 128MB 内存、没有文件系统、没有 PyMuPDF/OCR，
而"扫描件转写"和"页面排版检查"正依赖这些。所以只有这几条路：

| 路径 | 改动量 | 上线时间 | 成本 | 能力损失 | 适合 |
|---|---|---|---|---|---|
| **A. Tunnel（隧道指到你电脑）** | 0 行代码 | 10 分钟 | 免费 | 无，但**电脑关机就挂** | 只给自己/临时演示，**不算上线** |
| **A+. Fly.io / Render（容器托管）** | 0 行代码（用现成 Dockerfile） | **10~20 分钟** | 免费档 / 约 $2~5 月 | 无（扫描件、排版检查都在） | ✅ **要"别人随时能用"就走这条** |
| **B. Cloudflare Containers** | 少量（Dockerfile + 存储改造） | 1~2 天 | $5/月起 | 报告落盘要改到 R2；重启丢历史 | 想统一在 Cloudflare 生态里 |
| **C. Workers 全重写（JS）** | 重写解析 + 校验器 | 1~2 周 | 近乎免费 | 扫描件 OCR、页面视觉排版会丢 | 有量以后再说 |

**建议节奏**：想摆脱"必须开着自己的电脑"，最省事的是 **A+（Fly.io / Render）**，代码不用改；
要留在 Cloudflare，就做 **B**；**C** 等有量再说。A 只能算临时分享。

---

## 1. 为什么不能直接 `wrangler deploy`

| 现在依赖 | Workers 有没有 | 后果 |
|---|---|---|
| Python 运行时 + PyMuPDF（PDF 取文本、页面转图） | ❌（Python Workers 跑 Pyodide，装不了 PyMuPDF） | PDF 解析要重写 |
| Pillow（图片缩放转码） | ❌ | 图片处理要重写 |
| 页面转图 → 视觉模型（10 项排版检查） | ❌（无 canvas / 无浏览器） | 排版检查要砍掉或换外部服务 |
| 本地 `reports/` 磁盘 | ❌（无持久磁盘） | 报告要写 R2/D1 或干脆不存 |
| 内存里的会话（30 分钟） | ❌（Worker 无状态） | 要换 KV/R2 + 签名 token |
| 128MB 内存 / 请求体 | ✅ 但有限 | 30MB 简历走 base64 约 40MB，Workers 免费/Pro 上限 100MB，够用；但内存不够解大 PDF |

一句话：**能用 Workers 做的只是"文本 + 图片调模型"这一半**，另一半是文件处理。

---

## 2. 路径 A：Cloudflare Tunnel（先让人能用，10 分钟）

原理：本机服务不动，Cloudflare 建一条加密隧道，把公网域名指到你电脑的 8765 端口。

### A-1 装 cloudflared

```powershell
winget install --id Cloudflare.cloudflared
cloudflared --version
```

### A-2 先拿一个临时公网地址（不用域名、不用登录）

```powershell
# 另开一个窗口，先把服务跑起来
cd "D:\嘻嘻哈哈\vibecoding项目\resume-reviewer"
.\run_web.ps1

# 再开一个窗口
cloudflared tunnel --url http://127.0.0.1:8765
```

输出里会出现 `https://xxxx-yyyy.trycloudflare.com`——**这个地址任何人都能打开**。
每次重启换一个新地址，适合临时分享。

### A-3 要固定域名的话

```powershell
cloudflared tunnel login                  # 浏览器里选你的域名（域名要先托管在 Cloudflare）
cloudflared tunnel create resume-review
cloudflared tunnel route dns resume-review review.你的域名.com
cloudflared tunnel run resume-review
```

再装成开机自启的服务：

```powershell
cloudflared service install
```

### A-4 这条路的真实限制

| 限制 | 说明 |
|---|---|
| 电脑必须开着 | 关机/休眠 → 502 |
| 单机并发 | 你现在是 `ThreadingHTTPServer`，几个人同时传大 PDF 会排队 |
| 暴露面 | 直接开公网等于把本地服务交出去，**至少加一层 Cloudflare Access**（邮箱验证） |
| API Key | 还在你本机 `.env` 里，别人用不了你的 key（这是好事），但意味着服务只能用你的额度 |

> 加 Access：Cloudflare Zero Trust → Access → Applications → 新建 Self-hosted，绑定 `review.你的域名.com`，
> 策略选 "Emails = 你的邮箱"，这样只有通过邮箱验证的人能进。

---

## 2A. 路径 A+：Fly.io / Render（真上线，0 行代码改动）

这两家都**直接用仓库里的 Dockerfile 在云端构建**，你本机不需要装 Docker。
仓库里已经放好 `fly.toml` 和 `render.yaml`。

### A+1 Fly.io（推荐：香港区，国内访问相对快）

```powershell
# 1) 装 CLI 并登录（登录会打开浏览器）
winget install --id Superfly.flyctl
fly auth signup          # 已有账号用 fly auth login

# 2) 项目根目录，用仓库里的 fly.toml（--no-deploy 先不部署）
cd "D:\嘻嘻哈哈\vibecoding项目\resume-reviewer"
fly launch --no-deploy --copy-config

# 3) 填密钥（只写进服务器，不进仓库）
fly secrets set DSH_LLM_API_KEY=sk-你的key

# 4) 部署并打开
fly deploy
fly open
```

- `fly.toml` 已设 `auto_stop_machines = "suspend"`：没人用就挂起、有人访问自动唤醒，按秒计费；
- 规格 `shared-cpu-1x / 512mb`，PyMuPDF 处理 30MB PDF 够用；
- 报告写 `/tmp/reports`，**实例重建就清空**——公开站点不该长期留别人的简历，这是有意的。

### A+2 Render（连 GitHub，有免费档）

1. 把仓库推到 GitHub（`.env`、`reports/`、`_test/` 都已在 `.gitignore`）；
2. Render 控制台 → New → Blueprint → 选仓库，自动读 `render.yaml`；
3. Environment 里手填 `DSH_LLM_API_KEY`（`sync: false` 表示只存面板、不进代码）；
4. 拿到 `https://xxx.onrender.com`。

⚠️ 免费档 15 分钟无访问会休眠，**第一次打开等 30~60 秒**；介意就升 `starter`（$7/月）。

### A+3 Railway / Zeabur

同样读 Dockerfile，连 GitHub 或 `railway up` 即可，无需额外配置。Zeabur 有香港/东京节点，国内延迟更低。

### A+4 代码里已经替你准备好的

| 能力 | 怎么做的 |
|---|---|
| 监听公网 | `--host 0.0.0.0`；端口读 `PORT` 环境变量（平台给什么用什么） |
| 报告目录可配 | `REPORTS_DIR`，容器里指到 `/tmp/reports` |
| 防刷 | 单 IP `RATE_PER_HOUR`（默认 20 次/小时）、全站 `DAILY_LIMIT`（默认 300 次/天），超限返回 429 + 中文提示 |
| 健康检查 | `GET /api/health`，两个平台都挂了探针 |
| 密钥 | 只从环境变量读，仓库里没有 `.env` |

> 限流是**单进程内存计数**，多实例时各算各的（小站够用）。要严格全局限流得上 Redis/KV。

---

## 3. 路径 B：Cloudflare Containers（正式上线，保留全部代码）

Containers 2026-04-13 已 GA：把你的 Python 服务打成镜像，Workers 做入口，容器按需拉起、空闲休眠。
需要 **Workers Paid（$5/月起）**。每月含 25 GiB-小时内存、375 vCPU-分钟、200 GB-小时磁盘。

### B-1 先修一个真问题：依赖清单

`requirements.txt` 原来只写了 `python-docx` + `pypdf`，但代码实际用的是 **PyMuPDF（import fitz）和 Pillow**。
新机器 `pip install -r requirements.txt` 会在第一次传 PDF/图片时崩。已修正为：

```text
python-docx>=1.1.0
pymupdf>=1.24.0
pillow>=10.0.0
```

已在**干净虚拟环境**验证：`pip install -r requirements.txt` 装出 PyMuPDF 2.0 + Pillow 13.0 + python-docx 1.2，`import ingest` 与 PDF 文本解析均正常（服务器上 42 分钟是网络慢，不是依赖问题）。

> 另：PyMuPDF 2.x 起 `fitz` 只是兼容别名，会告警。代码已改成 `import pymupdf as fitz`，1.x / 2.x 都能跑。

### B-2 Dockerfile（仓库根目录已附）

```dockerfile
FROM python:3.13-slim
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# 中文字体：docx 里的中文要能渲染，含 CJK 的镜像才不出现方块
RUN apt-get update && apt-get install -y --no-install-recommends fonts-noto-cjk \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY web ./web
COPY scripts ./scripts
COPY prompts ./prompts
COPY samples ./samples

EXPOSE 8080
CMD ["python", "web/server.py", "--host", "0.0.0.0", "--port", "8080"]
```

> 注意 `--host 0.0.0.0`：默认只监听 127.0.0.1，容器里外部访问不到。

### B-3 部署

```powershell
npm i -g wrangler
wrangler login

# 在项目根目录（有 Dockerfile 的那层）
wrangler containers build --tag resume-reviewer:latest
wrangler deploy
```

`wrangler.jsonc` 大致长这样（**字段名随版本变化，以官方 Containers 文档为准**）：

```jsonc
{
  "name": "resume-reviewer",
  "main": "src/index.ts",
  "compatibility_date": "2026-10-01",
  "containers": [{
    "class_name": "ReviewApp",
    "image": "./Dockerfile",
    "max_instances": 3,
    "instance_type": "lite"
  }],
  "durable_objects": { "bindings": [{ "name": "REVIEW_APP", "class_name": "ReviewApp" }] },
  "migrations": [{ "tag": "v1", "new_sqlite_classes": ["ReviewApp"] }]
}
```

Worker 入口（把所有请求转给容器）：

```ts
import { Container, getContainer } from "@cloudflare/containers";

export class ReviewApp extends Container {
  defaultPort = 8080;
  sleepAfter = "10m";        // 空闲 10 分钟休眠，省钱
}

export default {
  async fetch(req: Request, env: Env) {
    const id = env.REVIEW_APP.idFromName("default");
    return getContainer(env.REVIEW_APP, id.toString()).fetch(req);
  },
};
```

密钥：

```powershell
wrangler secret put DSH_LLM_API_KEY
wrangler secret put DSH_LLM_BASE_URL
```

### B-4 必须改的三处代码（不改也能跑，但会漏）

| 问题 | 现状 | 改法 |
|---|---|---|
| `reports/` 是容器本地磁盘，实例重启就没了 | 写 `ROOT/reports` | 改成写 R2（S3 兼容 API），或接受"只返回给用户、不落盘" |
| 会话在内存，容器休眠就丢 | `SESSIONS` 字典 | 可接受（30 分钟本来就短）；否则用 R2/KV 存 `sid -> text`，TTL 30 分钟 |
| 服务是单进程多线程 | `ThreadingHTTPServer` | 3 个实例起步够用；真要并发再换 gunicorn/uvicorn |

### B-5 成本怎么估

一次评审 ≈ 20 秒（等模型的时间不烧 CPU，但**内存按容器醒着的秒数计费**）。

按 lite（256 MiB）、每天 100 次、每次唤醒 30 秒算：

```
内存：0.25 GiB × 30s × 100 次 × 30 天 / 3600 ≈ 6.25 GiB-小时/月  → 在免费的 25 GiB-小时内
CPU： 等模型的时间不算，实际 CPU 也就几秒 → 远低于 375 vCPU-分钟
```

**结论：$5/月基本够，除非有人疯狂上传大 PDF。** 记得设 `max_instances` 和每日调用上限，防止被刷。

---

## 4. 路径 C：Workers 全重写（有量以后）

如果决定重写，能力会这样缩水：

| 能力 | Workers 里怎么做 | 结论 |
|---|---|---|
| txt / md 输入 | 直接读 | ✅ |
| docx 输入 | `mammoth` 或 JSZip + XML | ✅ |
| 文字型 PDF | `unpdf` / `pdfjs-dist` 取文本 | ✅ |
| 扫描件 PDF | Workers 无 OCR | ❌ 要接外部 OCR（阿里/腾讯/Google Vision） |
| 图片简历 | 直传视觉模型 | ✅ |
| 10 项排版检查 | 需要页面渲染 | ❌ 砍掉，或改用 Browser Rendering 截 HTML（不适用 PDF） |
| 导出 .docx | `docx` npm 包 | ✅ |
| 19 字段校验器 | 700 行 Python 要翻成 TS | ⚠️ 3~5 天 |
| 历史记录 | D1 | ✅ |
| 报告文件 | R2 | ✅ |

成本几乎为 0（10 万请求/天免费），但**重写期间两套逻辑会分叉**，这才是真正的代价。

---

## 5. 上线前必须补的（和 Cloudflare 无关，是产品问题）

1. **匿名配额**：按 IP 每天 3 次（KV 计数），否则一个链接发出去就被刷爆。
2. **Turnstile**：免费人机验证，挡脚本。前端加 widget，Worker 校验 token。
3. **限流**：Cloudflare Rate Limiting 规则，或 Durable Object 计数。
4. **隐私政策 + 数据保留**：现在报告存本地没问题；上线后必须写清"简历发给了哪个模型、存多久、怎么删"。建议**默认不保存原简历**，只返回报告。
5. **免责声明**：不预测通过率、不做录用承诺（这一点代码里已经守住，页面上也要写）。
6. **成本闸门**：每日总调用上限，超了返回"今天名额用完了"，别让账单失控。
7. **错误可见**：Workers Logs / Sentry，至少知道 500 是哪一步。
8. **域名与备案**：如果服务器/主体在境外但面向国内用户，注意合规与访问速度；国内访问 Cloudflare 免费节点速度一般。

---

## 6. 几种路径的取舍，一句话版

- **只想让自己电脑以外的人临时看一眼** → A（Tunnel），10 分钟，免费，但电脑得开着。
- **想真正上线、别人随时能打开，又不想改代码** → **A+（Fly.io / Render）**，10~20 分钟，免费档或几美元/月。
- **一定要放在 Cloudflare 里** → B（Containers），$5/月，保留 OCR 与排版检查，接受"报告不长期保存"。
- **想做成产品、追求成本与稳定性** → C，但要接受砍掉扫描件和排版视觉检查。

---

## 附：数据出处（2026-10 核实）

| 事实 | 来源 |
|---|---|
| Workers 免费 10ms CPU / 付费 5 分钟；内存 128MB；请求体 100MB（Free/Pro） | developers.cloudflare.com/workers/platform/limits |
| Containers 2026-04-13 GA；需 Workers Paid；含 25 GiB-h 内存、375 vCPU-min、200 GB-h 磁盘 | cloudflare.com/products/containers、bex.co（2026-07 计费拆解） |
| Containers 按活跃 CPU + 唤醒时长计内存/磁盘 | 同上 |
| Python Workers 基于 Pyodide，装不了 PyMuPDF | Cloudflare Python Workers 文档 |
