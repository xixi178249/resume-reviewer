"""批量跑简历评审 + 计算指标。

用法：
    python scripts/run_eval.py --mock                 # 不调模型，验证链路与指标（推荐第一步）
    python scripts/run_eval.py --selftest             # 验证"校验器"能抓到编造和空话
    python scripts/run_eval.py --real                 # 调真实模型，需环境变量

真实模型需要（OpenAI 兼容接口，DeepSeek / 硅基流动 / 通义 / 本地 vLLM 都行）：
    $env:DSH_LLM_API_KEY  = "sk-..."
    $env:DSH_LLM_MODEL    = "deepseek-chat"          # 可选，默认 deepseek-chat
    $env:DSH_LLM_BASE_URL = "https://api.deepseek.com/v1"   # 可选
"""
from __future__ import annotations

import argparse
import ipaddress
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent.parent
SAMPLES = ROOT / "samples"
TXT_DIR = SAMPLES / "txt"
GOLD = SAMPLES / "gold.json"
PROMPT_DIR = ROOT / "prompts"
DEFAULT_PROMPT_VERSION = "v1.8"
OUT_DIR = ROOT / "outputs"

sys.path.insert(0, str(Path(__file__).resolve().parent))
import check_output  # noqa: E402


# ---------------- 模型调用配置 ----------------
# 两种来源：服务器自己的 .env（origin=env），或使用者自己在网页上填的 key（origin=request）。
# 后者是"别人来用"的唯一正确姿势：他花自己的钱，我这边不出 key、也不存 key。

class LLMError(RuntimeError):
    """模型调用失败：带机器可读的 code，和一句能直接显示给用户看的中文。

    故意不继承 SystemExit。Web 服务是多线程的，SystemExit 只会杀掉当前请求线程，
    浏览器看到的是"连接被关闭"，前端拿不到任何原因；而鉴权/余额/地址错误
    恰恰是用户自己能修的那类，必须原样告诉他是哪一步错了。
    """

    def __init__(self, code: str, message: str, *, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable

    def as_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "retryable": self.retryable}


# 错误信息会显示在网页上，任何情况下都不能把 key 带出去
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_\-]{4})[A-Za-z0-9_\-]{4,}")


def scrub(text: object) -> str:
    """遮掉文本里的 key，并截断到一个能放在一张卡片里的长度。"""
    return SECRET_RE.sub(r"\1***", str(text)).strip()[:400]


@dataclass(frozen=True)
class LLMConfig:
    api_key: str
    base_url: str = "https://api.deepseek.com/v1"
    model: str = "deepseek-chat"
    thinking: str = "on"
    timeout: int = 900
    origin: str = "env"          # env = 服务器自带 / request = 使用者自己填的

    def describe(self) -> dict:
        """给前端看的摘要，**不含 key**。"""
        return {"model": self.model, "base_url": self.base_url, "origin": self.origin}


def config_from_env() -> LLMConfig:
    """服务器自己的配置。没配 key 就说清楚"可以用本地规则评测"，不要报错吓人。"""
    key = os.environ.get("DSH_LLM_API_KEY", "").strip()
    if not key:
        raise LLMError(
            "LLM_NO_KEY",
            "这台服务器没有配置模型 key。填上你自己的 API Key 就能出完整报告；"
            "不填也可以，会走本地规则评测（不调用模型）。",
        )
    return LLMConfig(
        api_key=key,
        base_url=os.environ.get("DSH_LLM_BASE_URL", "https://api.deepseek.com/v1").rstrip("/"),
        model=os.environ.get("DSH_LLM_MODEL", "deepseek-chat"),
        thinking=(os.environ.get("DSH_LLM_THINKING", "on") or "on").strip().lower(),
        timeout=int(os.environ.get("DSH_LLM_TIMEOUT", "900")),
        origin="env",
    )


DEFAULT_BASE_URL = "https://api.deepseek.com/v1"
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/@\-]{1,79}$")


def _is_local_host(host: str) -> bool:
    h = (host or "").strip("[]").lower()
    if h in {"localhost", "127.0.0.1", "::1", "0.0.0.0"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return ip.is_private or ip.is_loopback or ip.is_link_local


def _assert_public_base(base_url: str) -> None:
    """挡住"填个地址让服务器替我去访问内网"这条路。

    两道检查：
    1. 字面量：localhost / 环回 / 私网 / 链路本地（含 169.254.169.254 云元数据）
    2. 域名：解析一次，任何 A/AAAA 落在内网就拒。防的是 "localhost." 这种变体
       和把域名解析到 127.0.0.1 的 DNS rebinding——完整防住要连到解析出的 IP，
       这里只做解析期拦截，够挡住"随手填个内网地址"这个量级。
    解析失败不当错误：离线环境/临时 DNS 故障不等于恶意，让真正的请求去报错。
    自己跑 Ollama 的人可以设 DSH_LLM_ALLOW_LOCAL=1 放开限制。
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in ("http", "https"):
        raise LLMError("LLM_BAD_BASE_URL", "接口地址必须以 http:// 或 https:// 开头")
    if not parsed.hostname:
        raise LLMError("LLM_BAD_BASE_URL", "接口地址里没有主机名，检查一下是不是少写了")
    if os.environ.get("DSH_LLM_ALLOW_LOCAL") == "1":
        return
    if _is_local_host(parsed.hostname):
        raise LLMError(
            "LLM_BAD_BASE_URL",
            "接口地址指向本机/内网地址，已拦下。"
            "如果你确实在本地跑模型（比如 Ollama），启动服务时设 DSH_LLM_ALLOW_LOCAL=1。",
        )
    try:
        infos = socket.getaddrinfo(parsed.hostname, parsed.port or None, proto=socket.IPPROTO_TCP)
    except (OSError, UnicodeError):
        return
    for info in infos:
        addr = str(info[4][0])
        if _is_local_host(addr):
            raise LLMError(
                "LLM_BAD_BASE_URL",
                f"接口地址的域名解析到了内网地址（{addr}），已拦下。",
            )


def config_from_request(
    api_key: str, base_url: str = "", model: str = "", timeout: int = 0
) -> LLMConfig:
    """使用者自己填的 key。只在这一次请求里用，不写文件、不进历史、不进日志。

    默认 thinking=off：别人等不起 5~10 分钟，而且 rubric 的约束力来自 prompt
    与校验器，不靠思维链。想开就用自己的环境变量管。
    """
    key = (api_key or "").strip()
    # 从 .env / JSON / 终端里复制，最常见的形态是 key 带着一层引号
    if len(key) >= 2 and key[0] == key[-1] and key[0] in "\"'":
        key = key[1:-1].strip()
    if not key:
        raise LLMError("LLM_NO_KEY", "没有填写 API Key")
    if len(key) < 8 or len(key) > 300 or re.search(r"[\s\"']", key):
        raise LLMError(
            "LLM_BAD_KEY",
            "API Key 格式不对：不能有空格，长度也不像正常的 key。检查是否多复制了引号或换行。",
        )
    base = (base_url or "").strip() or os.environ.get("DSH_LLM_BASE_URL", DEFAULT_BASE_URL)
    base = base.rstrip("/")
    _assert_public_base(base)
    mdl = (model or "").strip() or os.environ.get("DSH_LLM_MODEL", "deepseek-chat")
    if not MODEL_RE.match(mdl):
        raise LLMError("LLM_BAD_MODEL", "模型名不合法：只允许字母、数字与 . _ : / @ -")
    return LLMConfig(
        api_key=key,
        base_url=base,
        model=mdl,
        thinking=(os.environ.get("DSH_LLM_THINKING", "off") or "off").strip().lower(),
        timeout=timeout or int(os.environ.get("DSH_LLM_TIMEOUT", "300")),
        origin="request",
    )


def load_dotenv(path: Path | None = None) -> None:
    """读取项目根的 .env，让 VS Code 的 F5 也能拿到 key（零依赖实现）。

    已存在的真实环境变量优先，不会被 .env 覆盖。
    """
    p = path or (ROOT / ".env")
    if not p.exists():
        return
    for raw in p.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


load_dotenv()

EXAMPLE_NOTE = (
    "\n\n---\n\n"
    "# 参考输出（校准用）\n\n"
    "上面是评审规则与**待评审的简历**。下面这份 JSON 是**另一份完全不同的简历**的合格输出，\n"
    "只用于校准字段组织方式与严格程度。\n"
    "**禁止复用它里面的任何句子、数字、维度分数或结论。**\n\n"
    "```json\n{example}\n```\n"
)


# ---------------------------------------------------------------- 提示词
def build_prompt(
    resume: str,
    case: dict,
    version: str = DEFAULT_PROMPT_VERSION,
    image_path: str | None = None,
    with_example: bool = True,
    has_image: bool | None = None,
) -> str:
    """把模板渲染成最终要发出去的 prompt。

    注意：这里是"送给模型的东西"的唯一入口，端到端快照测试会直接断言它。
    """
    tpl = (PROMPT_DIR / f"system-{version}.md").read_text(encoding="utf-8")
    prompt = (
        tpl.replace("{{job_target}}", case.get("job_target", "后端开发实习生"))
        .replace("{{jd}}", case.get("jd", "（未提供 JD，按通用后端实习生标准评估）"))
        .replace("{{background}}", case.get("background", "（未提供）"))
        .replace(
            "{{has_image}}",
            "true" if (has_image if has_image is not None else bool(image_path)) else "false",
        )
        .replace("{{resume}}", resume)
    )
    if with_example:
        ex_path = PROMPT_DIR / f"example-output-{version}-good.json"
        if ex_path.exists():
            ex = json.loads(ex_path.read_text(encoding="utf-8"))
            prompt += EXAMPLE_NOTE.format(example=json.dumps(ex, ensure_ascii=False, indent=2))
    return prompt


def image_part(path: str) -> dict:
    """把本地图片转成 OpenAI 兼容的 image_url 片段（data URL）。"""
    import base64
    import mimetypes

    p = Path(path)
    if not p.exists():
        raise SystemExit(f"[error] 图片不存在：{p}")
    mime = mimetypes.guess_type(p.name)[0] or "image/png"
    b64 = base64.b64encode(p.read_bytes()).decode("ascii")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}}


def build_messages(prompt: str, image_path: str | None = None) -> list[dict]:
    if image_path:
        return [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    image_part(image_path),
                ],
            }
        ]
    return [{"role": "user", "content": prompt}]


def chat(
    messages: list[dict],
    json_mode: bool = False,
    timeout: int | None = None,
    max_tokens: int | None = None,
    config: LLMConfig | None = None,
) -> str:
    """最底层的模型调用，返回原始文本。

    config=None 时用服务器自己的 .env；传进来则用调用方给的那份（别人自己的 key）。
    除 CLI 外不要在别处读环境变量取 key —— 网页是多用户并发，环境只有一份。

    max_tokens 默认不传。
    ⚠️ 实测陷阱：推理型模型（如 deepseek-flash）会把"思考 token"计入 max_tokens。
    设成 8192 时，模型还没开始写正文就把额度烧完了，返回半截 JSON；
    设成 16000 甚至会返回空字符串。不传则按服务端默认预算，输出完整。

    思考开关：DSH_LLM_THINKING=off 时下发 thinking={"type":"disabled"}。
    一份完整评审在"思考开"时要 5~10 分钟（输出 2 万+ token），页面等到超时；
    关掉后通常 1 分钟内出结果，rubric 的约束力来自 prompt 与校验器，不靠 CoT。
    json_mode=True 时要求服务端返回 JSON（部分模型不支持，会自动降级重试）。
    """
    cfg = config or config_from_env()
    key, base, model, thinking = cfg.api_key, cfg.base_url, cfg.model, cfg.thinking
    if timeout is None:
        timeout = cfg.timeout

    def _post(use_json: bool, use_thinking: bool = True):
        payload = {
            "model": model,
            "messages": messages,
            "temperature": 0.2,
        }
        if max_tokens:
            payload["max_tokens"] = max_tokens
        if use_json:
            payload["response_format"] = {"type": "json_object"}
        if use_thinking and thinking == "off":
            payload["thinking"] = {"type": "disabled"}
        req = urllib.request.Request(
            f"{base}/chat/completions",
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
            method="POST",
        )
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                data["_elapsed"] = round(time.time() - t0, 1)
                return data
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "ignore")[:400]
            if use_json and exc.code in (400, 422):
                return None  # 这个模型不支持 json_object，退回普通模式
            if use_thinking and exc.code in (400, 422) and "thinking" in detail.lower():
                return _post(use_json, use_thinking=False)  # 服务商不认这个字段，去掉重发
            hint = {
                401: "key 无效或已过期",
                402: "余额不足",
                403: "key 没有该模型权限",
                404: "BASE_URL 路径不对（注意要带 /v1）",
                413: "内容太长，超出模型上下文",
                429: "触发限流，稍后再试",
            }.get(exc.code, "")
            raise LLMError(
                f"LLM_HTTP_{exc.code}",
                f"HTTP {exc.code}{'（' + hint + '）' if hint else ''}：{scrub(detail)}"
                f"\n  接口地址 = {base}｜模型 = {model}",
                retryable=exc.code in (429, 500, 502, 503, 504),
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                "LLM_NETWORK",
                f"连不上模型服务，检查网络或接口地址。\n  接口地址 = {base}\n  原因：{scrub(exc.reason)}",
                retryable=True,
            ) from exc
        except TimeoutError:
            raise LLMError(
                "LLM_TIMEOUT",
                f"请求超时（{timeout}s）。简历太长或模型太慢，可换更快的模型再试。",
                retryable=True,
            )

    data = _post(json_mode) if json_mode else _post(False)
    if data is None:
        data = _post(False)
    if "choices" not in data:
        raise LLMError(
            "LLM_BAD_RESPONSE",
            f"返回体里没有 choices，可能 key 或模型名不对：{scrub(data)}",
        )
    choice = data["choices"][0]
    finish = choice.get("finish_reason")
    if finish == "length":
        print("[warn] 模型输出被截断（finish_reason=length），结果可能不完整", file=sys.stderr)
    usage = data.get("usage") or {}
    if usage.get("completion_tokens"):
        reason = usage.get("completion_tokens_details", {}).get("reasoning_tokens")
        print(
            f"[cost] 本次输出 {usage.get('completion_tokens')} tokens"
            f"（其中思考 {reason}）｜输入 {usage.get('prompt_tokens')} tokens"
            f"｜耗时 {data.get('_elapsed', '?')}s",
            file=sys.stderr,
        )
    return choice["message"]["content"] or ""


def call_llm(
    messages: list[dict], dump_to: Path | None = None, config: LLMConfig | None = None
) -> dict:
    """调用模型并解析成 JSON 对象。

    输出被截断或夹带说明文字时：先落盘原文，再用一句"只输出 JSON"重试一次。
    """
    text = chat(messages, json_mode=True, config=config)
    try:
        return parse_json(text)
    except json.JSONDecodeError:
        if dump_to:
            dump_to.write_text(text, encoding="utf-8")
        nudge = messages + [
            {"role": "assistant", "content": text[:2000]},
            {
                "role": "user",
                "content": "上面这条输出不是合法 JSON。请重新完整输出一个 JSON 对象，"
                           "不要任何解释文字、不要 markdown 代码块。字段一个都不能少。",
            },
        ]
        text2 = chat(nudge, json_mode=True, config=config)
        try:
            return parse_json(text2)
        except json.JSONDecodeError:
            if dump_to:
                dump_to.with_suffix(".retry.txt").write_text(text2, encoding="utf-8")
            raise


def parse_json(text: str) -> dict:
    s = text.strip()
    if s.startswith("```"):
        s = s.split("```")[1] if "```" in s[3:] else s[3:]
        s = s.removeprefix("json").strip()
    # 兜底：截取第一个 { 到最后一个 }
    start, end = s.find("{"), s.rfind("}")
    if start != -1 and end > start:
        s = s[start : end + 1]
    return json.loads(s)


# ---------------------------------------------------------------- mock
def mock_review(case: str, case_meta: dict, resume: str) -> dict:
    """用固定"剧本"生成一份结构合法的评审，让校验器和指标先跑起来。

    这不是模型输出，只用于验证工程链路。剧本故意做得不完美：
    分数略微偏离人工分，好让 MAE 指标有值可看。
    """
    plan = case_meta.get("mock", {})
    overall = plan.get("score", 60)
    dims = plan.get(
        "dims",
        {"role_fit": 3, "impact": 2, "evidence_quality": 2, "structure": 3, "competitiveness": 2},
    )
    # 让 overall 与维度自洽，避免 mock 自己被判失败
    overall = round(sum(dims[k] * w for k, w in check_output.WEIGHTS.items()) * 20)
    verdict = check_output.expected_verdict(overall)

    rewards = plan.get("rewrites", [])
    quotes = plan.get("quotes", [])
    if not quotes:
        # 没有 mock 剧本的新简历：从原文挑几条真实 bullet 当引用，
        # 保证 evidence / rewrites 仍然满足"逐字引用 + 至少 2 条改写"
        bullets = [
            ln.strip() for ln in resume.splitlines()
            if ln.strip().startswith(("•", "·", "-", "*")) and len(ln.strip()) > 12
        ]
        quotes = bullets[:3]
    if not quotes:
        # 连 bullet 都没有（纯段落简历）：取前两行非空文本
        lines = [ln.strip() for ln in resume.splitlines() if len(ln.strip()) > 12]
        quotes = lines[:2]
    issues = []
    for i, q in enumerate(quotes[:3]):
        issues.append(
            {
                "id": f"I{i + 1}",
                "dimension": "impact",
                "severity": "high" if i == 0 else "medium",
                "issue": "只写了做了什么，没有写结果和影响",
                "evidence": q,
                "why": "面试官看不出这段经历的价值，容易在初筛被略过",
                "fix": f"把「{q[:14]}…」后面补一句：做了什么 → 带来了什么可核验的变化",
                "assertion": "fact",
            }
        )
    if not issues:
        issues = [{"id": "I1", "dimension": "structure", "severity": "low",
                   "issue": "缺少可直接引用的段落", "evidence": resume[:30] or "（空）",
                   "why": "无法定位问题",
                   "fix": f"把「{resume[:10]}…」所在段落补成「做了什么 + 带来什么变化」",
                   "assertion": "fact"}]

    import re as _re

    resume_nums = set(_re.findall(r"\d+(?:\.\d+)?%?", resume))
    rewrites = []
    for i, q in enumerate(quotes[:2]):
        body = rewards[i] if i < len(rewards) else q.split("，")[0]
        # 模拟"正确行为"：改写只保留原文已有的数字，其余一律占位
        body = _re.sub(
            r"\d+(?:\.\d+)?%?",
            lambda m: m.group(0) if m.group(0) in resume_nums else "[待补充：数字]",
            body,
        )
        if body.rstrip("，。") == q.rstrip("，。"):
            body = f"{q.rstrip('。')}，[待补充：具体产出与规模]"
        rewrites.append(
            {
                    "section": "项目经历",
                "original": q,
                "rewritten": body,
                "rationale": "把动作换成'动作 + 结果'，让面试官看到价值",
                "fact_delta": "ownership_upgrade" if check_output.ownership_upgrade(q, body) else "wording",
                "needs_user_confirm": bool(check_output.ownership_upgrade(q, body)),
            }
        )
    while len(rewrites) < 2:
        src = quotes[len(rewrites)] if len(quotes) > len(rewrites) else (quotes[0] if quotes else resume[:20])
        rewrites.append(
            {
                "section": "项目经历",
                "original": src or "（空）",
                "rewritten": f"「{src[:16]}…」 + [待补充：具体产出与规模]",
                "rationale": "模板占位，等你补充真实信息",
                "fact_delta": "none",
                "needs_user_confirm": False,
            }
        )

    # 长句：模拟 v1.1「单句可读性测试」的产出
    long_sentences = [ln.strip() for ln in resume.splitlines() if len(_re.sub(r"[\s，。、；：]", "", ln)) > 40][:2]

    # 数字溯源：只审百分比指标，最多 3 个；简历没有百分比就留空（v1.3 不再凑数）
    pct_nums = sorted({n for n in resume_nums if n.endswith("%")})[:3]
    number_audit = []
    for num in pct_nums:
        ctx = next((ln.strip() for ln in resume.splitlines() if num in ln), quotes[0] if quotes else resume[:30])
        number_audit.append(
            {
                "number": num,
                "context": ctx,
                "source_clarity": "unclear",
                "denominator": "未交代",
                "measured_or_estimated": "unknown",
                "questions": [f"这个 {num} 的分子分母分别是什么？", "是自己实测的还是估算的？"],
                "fix": f"在「{ctx[:20]}…」后面补充 {num} 的计算口径与统计方式",
            }
        )

    # ATS 文本层：章节存在性 / 联系方式 / 乱码 / 页数，全部由简历原文推出
    # 按「语义分组」判章节：同一段的不同叫法只能报一次，否则 found/missing 会重复
    section_groups = {
        "教育背景": ["教育背景", "教育经历", "学习经历"],
        "实习经历": ["实习经历", "实践经历", "工作经历"],
        "项目经历": ["项目经历", "项目经验"],
        "专业技能": ["专业技能", "技能特长", "技能"],
        "获奖": ["获奖", "荣誉", "奖项"],
    }
    found, missing = [], []
    for canon_name, aliases in section_groups.items():
        hit = next((a for a in aliases if a in resume), None)
        if hit:
            found.append(hit)
        else:
            missing.append(canon_name)
    ats_text = {
        "sections_found": found,
        "sections_missing": missing,
        "contact_parseable": bool(
            check_output.PHONE_RE.search(resume)
            or check_output.EMAIL_RE.search(resume)
            or _re.search(r"手机|电话|邮箱", resume)
        ),
        "garbled_char_count": len(check_output.GARBLED_RE.findall(resume)),
        "estimated_pages": max(1, round(len(check_output.canon(resume)) / check_output.PAGES_CHARS)),
        "risk": "medium" if missing else "low",
        "findings": [],
    }

    # JD 覆盖：有 JD 才做，从 JD 里粗抽关键词，再逐个人工核对是否真有证据
    jd_text = (case_meta.get("jd") or "").strip()
    if jd_text:
        tokens = _re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{1,20}|[\u4e00-\u9fff]{2,6}", jd_text)
        seen: list[str] = []
        for t in tokens:
            if t not in seen and len(seen) < 6:
                seen.append(t)
        kws = [
            {"term": t, "in_resume": t in resume, "natural": t in resume, "where": "经历/技能"}
            for t in seen
        ]
        nat = sum(1 for k in kws if k["natural"])
        jd_match = {
            "jd_provided": True,
            "keywords": kws,
            "coverage_rate": round(nat / len(kws), 2) if kws else 0.0,
            "stuffed_terms": [],
            "irrelevant_note": "未逐条估算篇幅占比",
            "gap_summary": "" if nat == len(kws) else "有关键词在简历中无证据",
        }
    else:
        jd_match = {
            "jd_provided": False,
            "keywords": [],
            "coverage_rate": 0.0,
            "stuffed_terms": [],
            "irrelevant_note": "未提供 JD，无法对照",
            "gap_summary": "未提供 JD",
        }

    # 动作优先级：修最低分维度（强制规则）
    lowest = min(check_output.WEIGHTS, key=lambda k: (dims[k], -check_output.WEIGHTS[k]))
    next_actions = [
        {
            "action": f"补齐 {lowest} 维度最缺的证据：把「{quotes[0][:20] if quotes else '项目经历'}」改成可核验写法",
            "targets": lowest,
            "est_minutes": 20,
            "expected_gain": "verdict_upgrade",
        },
        {"action": "给每个百分比补上分子分母与统计方式", "targets": "impact", "est_minutes": 15, "expected_gain": "risk_down"},
        {"action": "压掉超过 40 字的长句，每句只讲一件事", "targets": "structure", "est_minutes": 10, "expected_gain": "score_up"},
    ]

    review = {
        "schema_version": "v1.8",
        "gate_hr": {"pass": False, "reason": ""},
        "gate_interview": {"pass": False, "reason": ""},
        "three_second_test": {
            "job_target_clear": True,
            "strongest_point": (quotes[0][:40] if quotes else "抓不到"),
            "readable_in_3s": dims.get("structure", 3) >= 3,
            "long_sentences": long_sentences,
        },
        "format_review": {
            "verdict": "risky" if ats_text["sections_missing"] else "clean",
            "text_level": ats_text,
            "visual_level": {
                # mock 没有截图：checks 必须为空、density 必须 unknown
                "known": False,
                "checks": [],
                "density": "unknown",
            },
            "summary": "纯文本输入，只判了文本层",
        },
        "showcase": {
            # 展示力是纯文本可判的，mock 直接算真值，保证覆盖检查能过
            "positioning": plan.get("positioning", "一个经历尚可但看不出专长的学生"),
            "has_thread": dims.get("competitiveness", 2) >= 4,
            "weak_verb_lines": [
                ln.strip() for ln in resume.splitlines() if check_output.is_weak_verb_line(ln)
            ][:8],
            "buried_results": [],
            "self_praise": [w for w in check_output.SELF_PRAISE if w in resume][:8],
        },
        "jd_match": jd_match,
        "verdict": verdict,
        "overall_score": overall,
        "one_line_summary": plan.get("summary", "经历以‘参与/负责’为主，缺结果证据，需重写 2 段。"),
        "dimension_scores": {k: {"score": v, "comment": "见对应问题"} for k, v in dims.items()},
        "hard_blockers": [],
        "issues": issues,
        "rewrites": rewrites,
        "number_audit": number_audit,
        "followup_risks": [
            {
                "line": q,
                "questions": ["这一段你具体做了什么？", "为什么用这个方案而不是别的？"],
                "risk": "high" if i == 0 else "medium",
            }
            for i, q in enumerate(quotes[:2])
        ],
        "missing_info": ["每段经历的具体产出与规模"],
        "before_submit": [
            "逐条核对所有 [待补充] 处，确认数字与事实由你本人提供",
            "确认任何把「参与/协助」改写成「主导/负责」的表述有真实依据",
        ],
        "next_actions": next_actions,
    }

    # 两关的 pass 由维度推导，保证与 v1.3 公式自洽
    review["gate_hr"] = {
        "pass": check_output.gate_hr_expected(review),
        "reason": "岗位匹配度与结果证据都达标，初筛会留下",
    }
    review["gate_interview"] = {
        "pass": check_output.gate_interview_expected(review),
        "reason": "经历有可追问的方法与边界" if check_output.gate_interview_expected(review)
        else "多段经历答不出为什么这么做，追问两层就崩",
    }
    return review


# ---------------------------------------------------------------- 主流程
def main() -> int:
    ap = argparse.ArgumentParser(description="简历评审批量评测")
    ap.add_argument("--mock", action="store_true", help="用固定剧本跑通链路，不调模型")
    ap.add_argument("--real", action="store_true", help="调用真实模型")
    ap.add_argument("--selftest", action="store_true", help="验证校验器能否抓到编造")
    ap.add_argument("--limit", type=int, default=0, help="只跑前 N 份，控制成本")
    ap.add_argument("--prompt", default=DEFAULT_PROMPT_VERSION, help="prompt 版本，如 v1.3 / v1.2 / v1.0")
    ap.add_argument(
        "--image",
        metavar="PATH",
        default=None,
        help="附带简历截图（本地图片路径）。不传时模型只拿到纯文本，layout_check 必须填 known=false",
    )
    ap.add_argument(
        "--no-example",
        action="store_true",
        help="不注入 few-shot 样例（省一半 token，但会失去格式校准，仅用于对照实验）",
    )
    args = ap.parse_args()

    if args.selftest:
        return selftest()

    if not args.mock and not args.real:
        print("请指定 --mock 或 --real（或 --selftest）", file=sys.stderr)
        return 2

    if not (PROMPT_DIR / f"system-{args.prompt}.md").exists():
        print(f"[error] 找不到 prompts/system-{args.prompt}.md", file=sys.stderr)
        return 2

    if not TXT_DIR.exists():
        print(f"[error] 找不到 {TXT_DIR}，先运行：python scripts/extract_text.py samples --desensitize", file=sys.stderr)
        return 2

    cases = sorted(TXT_DIR.glob("*.txt"))
    if args.limit:
        cases = cases[: args.limit]
    if not cases:
        print(f"[error] {TXT_DIR} 下没有 txt", file=sys.stderr)
        return 2

    if args.prompt != DEFAULT_PROMPT_VERSION:
        print(
            f"[warn] 你显式指定了 --prompt {args.prompt}，而当前默认是 "
            f"{DEFAULT_PROMPT_VERSION}。两者的规则集不同，校验结果不可直接比较。",
            file=sys.stderr,
        )

    gold = json.loads(GOLD.read_text(encoding="utf-8")) if GOLD.exists() else {}
    OUT_DIR.mkdir(exist_ok=True)

    ran_files: list[Path] = []
    for txt in cases:
        case = txt.stem
        resume = txt.read_text(encoding="utf-8")
        meta = gold.get(case, {})
        try:
            if args.mock:
                review = mock_review(case, meta, resume)
            else:
                prompt = build_prompt(
                    resume, meta, args.prompt,
                    image_path=args.image, with_example=not args.no_example,
                )
                review = call_llm(build_messages(prompt, args.image))
        except json.JSONDecodeError as exc:
            print(f"[fail] {case}: 模型输出不是合法 JSON（{exc}）", file=sys.stderr)
            continue
        except LLMError as exc:
            # 鉴权/余额/地址错误每份简历都会一样地失败，没必要继续跑
            print(f"[error] {exc.code}｜{exc.message}", file=sys.stderr)
            return 1
        except SystemExit:
            raise
        suffix = "mock" if args.mock else "real"
        out = OUT_DIR / f"{case}.{suffix}.json"
        out.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
        ran_files.append(out)
        print(f"[ok] {case} -> {out.name} ({review.get('overall_score')} 分)", file=sys.stderr)

    print("\n--- 批量指标 ---")
    result = check_output.evaluate_dir(
        OUT_DIR, GOLD if GOLD.exists() else None, only_files=ran_files
    )
    print(check_output._pretty_batch(result))
    if args.mock:
        print("\n[MOCK] 以上为固定剧本输出，未调用真实模型；指标用于验证链路，不代表模型效果。")

    failed = result.get("failed_cases") or []
    total = result.get("cases")
    print("\n" + "=" * 60)
    if "error" in result:
        print(f"⚠️  {result['error']}")
        print("    outputs/ 里没有可校验的结果，先确认模型调用是否成功")
    elif failed:
        print(f"❌ {len(failed)}/{total} 份未通过校验")
        print("   原因见上方 [FAIL] 段落。退出码 1 =『校验没通过』，不是程序崩溃。")
    else:
        print(f"✅ {total} 份全部通过校验")
    print("=" * 60)
    sys.stdout.flush()
    return 0 if not failed else 1


def selftest() -> int:
    """确认校验器能抓到各类错误——含 v1.1 幻觉/v1.2 展示力漏报。"""
    resume = (SAMPLES / "fixtures" / "fake-resume.txt").read_text(encoding="utf-8")
    # v1.6/v1.7 的坏样例是从 04 号简历派生的，必须用同一份原文校验，否则全是引用错误
    r04 = SAMPLES / "txt" / "04-Agent算法-强简历.txt"
    if not r04.exists():
        r04 = SAMPLES / "demo" / "04-Agent算法-强简历.txt"
    resume04 = r04.read_text(encoding="utf-8")
    bad_v10 = json.loads((SAMPLES / "fixtures" / "bad-output.json").read_text(encoding="utf-8"))
    bad_v11 = json.loads((SAMPLES / "fixtures" / "bad-output-v11.json").read_text(encoding="utf-8"))
    bad_v12 = json.loads((SAMPLES / "fixtures" / "bad-output-v12.json").read_text(encoding="utf-8"))
    bad_v13 = json.loads((SAMPLES / "fixtures" / "bad-output-v13.json").read_text(encoding="utf-8"))
    bad_v14 = json.loads((SAMPLES / "fixtures" / "bad-output-v14.json").read_text(encoding="utf-8"))
    bad_v15 = json.loads((SAMPLES / "fixtures" / "bad-output-v15.json").read_text(encoding="utf-8"))
    bad_v16 = json.loads((SAMPLES / "fixtures" / "bad-output-v16.json").read_text(encoding="utf-8"))
    bad_v17 = json.loads((SAMPLES / "fixtures" / "bad-output-v17.json").read_text(encoding="utf-8"))

    res10 = check_output.check(bad_v10, resume)
    joined10 = "\n".join(res10["errors"] + res10["warnings"])
    res11 = check_output.check(bad_v11, resume)
    joined11 = "\n".join(res11["errors"] + res11["warnings"])
    res12 = check_output.check(bad_v12, resume)
    joined12 = "\n".join(res12["errors"] + res12["warnings"])
    res13 = check_output.check(bad_v13, resume)
    joined13 = "\n".join(res13["errors"] + res13["warnings"])
    res14 = check_output.check(bad_v14, resume)
    joined14 = "\n".join(res14["errors"] + res14["warnings"])
    res15 = check_output.check(bad_v15, resume)
    joined15 = "\n".join(res15["errors"] + res15["warnings"])
    res16 = check_output.check(bad_v16, resume04)
    joined16 = "\n".join(res16["errors"] + res16["warnings"])
    res17 = check_output.check(bad_v17, resume04)
    joined17 = "\n".join(res17["errors"] + res17["warnings"])

    expectations = {
        # v1.0 基础能力
        "抓到编造引用": "引用不在原文中" in joined10,
        "抓到编造数字": "出现了简历里没有的数字" in joined10,
        "抓到分数不自洽": "分数不自洽" in joined10 or "verdict 与分数不一致" in joined10,
        "抓到空话建议": "空话" in joined10,
        # v1.1 新增能力
        "抓到无图却谈排版": "无截图却声称看到视觉元素" in joined11,
        "抓到有图无图自相矛盾": "parse_risk 必须是 unknown" in joined11,
        "抓到强推却有硬伤": "verdict=strong_pass 却写了 hard_blockers" in joined11,
        "抓到虚构数字被审计": "在简历中不存在" in joined11,
        "抓到来源不清却不提问": "却没给出面试官会问的问题" in joined11,
        "抓到动作优先级错误": "优先级错误" in joined11,
        "抓到动作超时": "est_minutes 必须是 1~30" in joined11,
        # v1.2 展示力
        "抓到漏报弱动词": "漏报弱动词行" in joined12,
        "抓到漏报自评词": "漏报自评词" in joined12,
        "抓到结果位置误报": "标为「结果被埋」，但该行没有任何数字" in joined12,
        # v1.3
        "抓到两关不自洽": "gate_hr 与维度不自洽" in joined13,
        "抓到无可执行锚点": "没有任何可执行锚点（空话）" in joined13,
        "抓到数字溯源覆盖不足": "数字溯源覆盖不足" in joined13,
        "抓到句首结果误报": "已含结果信号" in joined13,
        # v1.4
        "抓到ATS章节造假": "在简历原文中找不到" in joined14,
        "抓到JD关键词造假": "但原文连关键词块都找不到" in joined14,
        "抓到权属升级未标注": "fact_delta 必须是 ownership_upgrade" in joined14,
        "抓到隐私回显": "回显了完整联系方式" in joined14,
        "抓到概率承诺": "不得给出通过率" in joined14,
        # v1.5 客观性
        "抓到敏感断言写成事实": "但 assertion=" in joined15 and "原文没写明就必须按 inference" in joined15,
        "抓到推断缺确认问题": "是推断级，必须给 verify_question" in joined15,
        "抓到硬伤非事实级": "只允许 fact" in joined15,
        # v1.6 格式优先
        "抓到视觉项太少": "必须判满 10 项" in joined16,
        "抓到自造检查项": "不在固定 10 项里" in joined16,
        "抓到格式结论矛盾": "format_review.verdict 必须是 blocking" in joined16,
        "抓到格式未优先修": (
            "第一条动作本该是 format_review" in joined16
            and "format_first_action" in ((res16.get("stats") or {}).get("repairs") or [])
        ),
        # v1.7 格式进入决策链
        "抓到格式阻断仍判初筛过": "必须先修格式" in joined17,
        "抓到章节重复上报": "不能两处都报" in joined17,
        "抓到判不满10项": "必须判满 10 项" in joined17,
        "抓到verdict与明细不符": "format_review.verdict 必须是 blocking" in joined17,
        "抓到density矛盾": "二者矛盾" in joined17,
        "抓到视觉判断未标不确定": "应写明「请对照原文件确认」" in joined17,
    }
    print("== 校验器自检 ==")
    for name, ok in expectations.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")

    passed = sum(1 for ok in expectations.values() if ok)
    bad = [n for n, ok in expectations.items() if not ok]
    print("\n" + "=" * 60)
    if bad:
        print(f"❌ {passed}/{len(expectations)} 通过，失败项：{'、'.join(bad)}")
        print("   说明校验器本身退化了，不是简历的问题")
    else:
        print(f"✅ {passed}/{len(expectations)} 全部通过")
    print("=" * 60)
    sys.stdout.flush()

    if res14["errors"]:
        print("\n检出问题（v1.7 样例，这是故意的）：")
        for e in res14["errors"][:6]:
            print(f"  - {e}")
    return 0 if all(expectations.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
