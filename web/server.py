"""本地网站：上传/粘贴简历 → 一页评审报告。

启动：
    python web/server.py                # http://127.0.0.1:8765
    python web/server.py --mock         # 不调模型，用假数据验界面
    python web/server.py --port 9000

只用标准库，不引入 Flask。
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sys
import tempfile
import threading
import time
import traceback
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ingest  # noqa: E402
import quality_gate  # noqa: E402
import run_eval  # noqa: E402
import review as review_mod  # noqa: E402

WEB = Path(__file__).resolve().parent
try:
    APP_VERSION = (ROOT / "VERSION").read_text(encoding="utf-8").strip() or "0.0.0"
except OSError:
    APP_VERSION = "0.0.0"
MAX_FILE = 30 * 1024 * 1024          # 单个文件 30MB（业务上限）
# 请求体上限要按 base64 膨胀算：30MB 文件 → 约 40MB JSON，再留点余量。
MAX_BODY = 45 * 1024 * 1024
SESSION_TTL = 30 * 60            # 会话保留 30 分钟

# 报告落盘目录：容器里磁盘是临时的，可用 REPORTS_DIR 指到挂载卷或 /tmp
REPORTS = Path(os.environ.get("REPORTS_DIR") or (ROOT / "reports")).resolve()
REPORTS.mkdir(parents=True, exist_ok=True)

# 公开部署时的最低限度防刷：单 IP 每小时 N 次、全站每天 M 次模型调用
RATE_PER_HOUR = int(os.environ.get("RATE_PER_HOUR", "20"))
DAILY_LIMIT = int(os.environ.get("DAILY_LIMIT", "300"))
RL = {"ips": {}, "day": "", "count": 0}

SESSIONS: dict[str, dict] = {}
LOCK = threading.Lock()
RL_LOCK = threading.Lock()
MOCK = False

HISTORY = REPORTS / ".history.json"
HISTORY_VERSION = 2
MAX_HISTORY = 500
MAX_PREVIEW_TEXT = 200_000


def _safe_stem(name: str) -> str:
    """历史记录名本身就是不带目录的文件 stem；允许中间点号，但禁止路径分隔与隐藏文件。"""
    s = (name or "").strip()
    if not s or s in {".", ".."} or s.startswith(".") \
            or Path(s).name != s or "\x00" in s:
        return ""
    return s


def _valid_review_id(review_id: str) -> bool:
    return bool(re.fullmatch(r"(?:[0-9a-f]{32}|legacy-[0-9a-f]{16})", review_id or ""))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _review_config_hash(job: str, jd: str, visual: bool, repeat: int) -> str:
    """只有这些配置一致，分数增量才有可比性。不含任何 API Key。"""
    payload = {
        "job": job,
        "jd_sha256": _sha256(jd),
        "prompt_version": run_eval.DEFAULT_PROMPT_VERSION,
        "gate_version": quality_gate.GATE_VERSION,
        "schema_version": quality_gate.SCHEMA_VERSION,
        "model": os.environ.get("DSH_LLM_MODEL", "deepseek-chat"),
        "base_url": os.environ.get("DSH_LLM_BASE_URL", "https://api.deepseek.com/v1"),
        "thinking": os.environ.get("DSH_LLM_THINKING", "on"),
        "visual": bool(visual),
        "repeat": int(repeat),
    }
    return _sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _artifact_stem(name: str, review_id: str) -> str:
    safe = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name or "resume").strip(" .")
    safe = (safe or "resume")[:80]
    return f"{safe}-{review_id[:12]}"


def _legacy_review_id(stem: str, ts: str, score) -> str:
    raw = f"{stem}\x00{ts}\x00{score}".encode("utf-8")
    return "legacy-" + hashlib.sha256(raw).hexdigest()[:16]


def _empty_history() -> dict:
    return {"version": HISTORY_VERSION, "reviews": []}


def _normalise_history(raw) -> dict:
    """把 v1 的 stem→[runs] 历史转换成 v2；旧报告只保留每个名字最新一份摘要。"""
    if isinstance(raw, dict) and raw.get("version") == HISTORY_VERSION:
        reviews = raw.get("reviews")
        if isinstance(reviews, list):
            return {
                "version": HISTORY_VERSION,
                "reviews": [r for r in reviews if isinstance(r, dict) and r.get("review_id")],
            }
        return _empty_history()

    reviews: list[dict] = []
    if isinstance(raw, dict):
        for stem, runs in raw.items():
            if not isinstance(runs, list) or not runs:
                continue
            safe = _safe_stem(str(stem))
            if not safe or not (REPORTS / f"{safe}.md").exists():
                continue
            last = runs[-1] if isinstance(runs[-1], dict) else {}
            ts = str(last.get("ts") or "")
            score = last.get("score")
            rid = _legacy_review_id(safe, ts, score)
            reviews.append({
                "review_id": rid,
                "name": safe,
                "file_stem": safe,
                "created_at": ts,
                "score": score,
                "verdict": last.get("verdict"),
                "job": last.get("job") or "",
                "jd_provided": bool(last.get("jd")),
                "content_sha256": "",
                "config_hash": "",
                "prompt_version": "",
                "model": "",
                "visual": None,
                "repeat": None,
                "legacy": True,
                "legacy_runs": len(runs),
            })
    reviews.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"version": HISTORY_VERSION, "reviews": reviews[:MAX_HISTORY]}


def _history_load() -> dict:
    try:
        raw = json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return _empty_history()
    return _normalise_history(raw)


def _history_save(data: dict) -> None:
    """原子写 v2 历史。v1 兼容数据在首次写入时转换为 v2。"""
    HISTORY.parent.mkdir(parents=True, exist_ok=True)
    tmp = HISTORY.with_name(".history.json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(HISTORY)


def _history_append(entry: dict) -> None:
    with LOCK:
        data = _history_load()
        reviews = [r for r in data["reviews"] if r.get("review_id") != entry["review_id"]]
        reviews.append(entry)
        reviews.sort(key=lambda x: x.get("created_at") or "", reverse=True)
        data["reviews"] = reviews[:MAX_HISTORY]
        _history_save(data)


def _history_remove(review_id: str) -> bool:
    with LOCK:
        data = _history_load()
        kept = [r for r in data["reviews"] if r.get("review_id") != review_id]
        if len(kept) == len(data["reviews"]):
            return False
        data["reviews"] = kept
        _history_save(data)
        return True


def _history_find(review_id: str) -> dict | None:
    if not review_id:
        return None
    for item in _history_load()["reviews"]:
        if item.get("review_id") == review_id:
            return item
    return None


def _history_for_content(content_sha256: str) -> list[dict]:
    if not content_sha256:
        return []
    return [
        r for r in _history_load()["reviews"]
        if r.get("content_sha256") == content_sha256 and not r.get("legacy")
    ]


def _history_public(entry: dict) -> dict:
    file_stem = _safe_stem(entry.get("file_stem") or "")
    md = REPORTS / f"{file_stem}.md" if file_stem else None
    tx = REPORTS / f"{file_stem}.txt" if file_stem else None
    return {
        "review_id": entry.get("review_id"),
        "name": entry.get("name") or entry.get("file_stem") or "未命名",
        "created_at": entry.get("created_at") or "",
        "ts": entry.get("created_at") or "",
        "score": entry.get("score"),
        "verdict": entry.get("verdict"),
        "job": entry.get("job") or "",
        "jd_provided": bool(entry.get("jd_provided")),
        "has_report": bool(md and md.exists()),
        "preview_available": bool(tx and tx.exists()),
        "legacy": bool(entry.get("legacy")),
        "legacy_runs": entry.get("legacy_runs"),
        "visual": entry.get("visual"),
        "repeat": entry.get("repeat"),
    }


def _report_paths(entry: dict) -> dict[str, Path] | None:
    stem = _safe_stem(entry.get("file_stem") or "")
    if not stem:
        return None
    paths = {
        ext: (REPORTS / f"{stem}{ext}").resolve()
        for ext in (".md", ".json", ".txt", ".docx", ".raw.txt", ".retry.txt")
    }
    if any(REPORTS not in p.parents for p in paths.values()):
        return None
    return paths


def _client_ip(handler) -> str:
    """Cloudflare / 反代后面拿真实 IP；本地直连就是 client_address。"""
    for h in ("CF-Connecting-IP", "X-Forwarded-For"):
        v = handler.headers.get(h)
        if v:
            return v.split(",")[0].strip()
    return handler.client_address[0]


def allow_request(ip: str, cost: int = 1) -> tuple[bool, str]:
    """滑动窗口限流。cost = 这次要消耗几次模型调用（评审 3 次就记 3）。"""
    now = time.time()
    day = time.strftime("%Y-%m-%d")
    with RL_LOCK:
        if RL["day"] != day:
            RL["day"], RL["count"] = day, 0
        if DAILY_LIMIT and RL["count"] + cost > DAILY_LIMIT:
            return False, "今天全站名额用完了，明天再来（也可以自己部署一份）"
        hits = [t for t in RL["ips"].get(ip, []) if now - t < 3600]
        if RATE_PER_HOUR and len(hits) + cost > RATE_PER_HOUR:
            left = max(0, int(3600 - (now - hits[0]))) if hits else 0
            return False, f"同一个 IP 每小时最多 {RATE_PER_HOUR} 次，约 {left // 60 + 1} 分钟后再试"
        hits.extend([now] * cost)
        RL["ips"][ip] = hits
        RL["count"] += cost
        if len(RL["ips"]) > 5000:      # 别让字典无限长
            RL["ips"] = {k: v for k, v in RL["ips"].items() if any(now - t < 3600 for t in v)}
    return True, ""



def gc_sessions() -> None:
    now = time.time()
    with LOCK:
        expired = [k for k, v in SESSIONS.items() if now - v["ts"] > SESSION_TTL]
        for k in expired:
            sess = SESSIONS.pop(k, None) or {}
            # 先清敏感内容再交给 GC，避免图片字节在字典里多留一轮。
            sess["text"] = ""
            (sess.get("images") or []).clear()


# ---------------------------------------------------------------- API
def api_extract(payload: dict) -> dict:
    name = payload.get("name") or "resume"
    b64 = payload.get("b64")
    raw_text = payload.get("text")
    allow_images = payload.get("allow_images") is True

    if raw_text:
        text = ingest.desensitize(ingest.normalize(raw_text))
        images: list[bytes] = []
        kind, notes = "pasted-text", ["粘贴文本"]
    elif b64:
        try:
            blob = base64.b64decode(b64.split(",")[-1])
        except Exception:  # noqa: BLE001
            return {"ok": False, "error": "文件内容解析失败"}
        if len(blob) > MAX_FILE:
            return {"ok": False, "error": "文件太大（上限 30MB），压缩后再试", "_status": 413}
        suffix = Path(name).suffix.lower() or ".txt"
        tmpdir = tempfile.TemporaryDirectory(prefix="resume-reviewer-")
        try:
            tmp = Path(tmpdir.name) / f"upload{suffix}"
            tmp.write_bytes(blob)
            ing = ingest.load(tmp, ocr=allow_images, include_images=allow_images)
        except ingest.ImageConsentRequired as exc:
            return {
                "ok": False,
                "code": "IMAGE_CONSENT_REQUIRED",
                "error": str(exc),
                "retryable": True,
            }
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
        finally:
            # 正常、业务错误、解析异常都清理本次上传文件。
            tmpdir.cleanup()
        text, images, kind, notes = ing.text, ing.images, ing.kind, ing.notes
    else:
        return {"ok": False, "error": "没有收到文件或文本"}

    if len(text) < 80:
        return {
            "ok": False,
            "error": f"只提取到 {len(text)} 字，可能是空白页或扫描质量太差。请换一份文件，或直接粘贴文本。",
        }

    sid = uuid.uuid4().hex
    gc_sessions()
    stem = Path(name).stem
    content_sha256 = _sha256(text)
    with LOCK:
        SESSIONS[sid] = {
            "text": text,
            "images": images,
            "images_allowed": allow_images,
            "name": stem,
            "kind": kind,
            "notes": notes,
            "content_sha256": content_sha256,
            "ts": time.time(),
        }

    related = _history_for_content(content_sha256)
    prev = _history_public(related[0]) if related else None
    return {
        "ok": True,
        "session_id": sid,
        "chars": len(text),
        "kind": kind,
        "pages": len(images),
        "images_allowed": allow_images,
        "visual_available": bool(images),
        "ocr_used": kind in {"scanned-pdf", "image"},
        "preview": text[:600],
        "resume_text": text[:MAX_PREVIEW_TEXT],
        "text_truncated": len(text) > MAX_PREVIEW_TEXT,
        "notes": notes,
        "runs": len(related),
        "prev": prev,
    }


def api_review(payload: dict) -> dict:
    sid = payload.get("session_id") or ""
    job = (payload.get("job") or "").strip()[:120] or "未提供（按简历里的求职意向推断）"
    jd = (payload.get("jd") or "").strip()[:6000]
    # 默认不发送页面图。即使前端误传 visual=true，也必须同时存在用户同意和页面图。
    visual = payload.get("visual") is True

    with LOCK:
        sess = SESSIONS.get(sid)
    if not sess:
        return {"ok": False, "error": "会话已过期，请重新上传简历"}

    images = (
        sess["images"]
        if visual and sess.get("images_allowed") is True
        else []
    )
    case = {
        "job_target": job,
        "jd": jd or "（未提供 JD，按岗位名与通用标准评估）",
        "background": "",
    }
    prompt = run_eval.build_prompt(
        sess["text"], case, run_eval.DEFAULT_PROMPT_VERSION, has_image=bool(images)
    )

    try:
        repeat = int(payload.get("repeat") or 1)
    except (TypeError, ValueError):
        repeat = 1
    repeat = max(1, min(3, repeat))   # 最多 3 次，防止误触把额度烧光

    if MOCK:
        raw = run_eval.mock_review(sess["name"], {}, sess["text"])
        quality = quality_gate.evaluate(raw, sess["text"])
        scores = [quality["doc"].get("overall_score")]
    else:
        try:
            doc, scores, quality = review_mod.run_reviews(
                prompt, images, repeat, resume=sess["text"]
            )
        except quality_gate.QualityGateError as exc:
            return {
                "ok": False,
                "code": exc.code,
                "error": str(exc),
                "details": exc.public_details(),
                "retryable": True,
                "attempts": exc.attempts,
            }
        except SystemExit as exc:
            return {"ok": False, "error": str(exc)}
        except json.JSONDecodeError:
            return {"ok": False, "error": "模型返回的不是合法 JSON，请重试一次"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": f"评审失败：{exc}"}

    if not quality["eligible"]:
        return {
            "ok": False,
            "code": quality_gate.QualityGateError.code,
            "error": "这次结果未通过事实与结构校验，没有可作为正式结果交付的版本。请重试一次。",
            "details": [
                {"code": p["code"], "message": p["message"]}
                for p in quality["blockers"][:5]
            ],
            "retryable": True,
        }

    doc = quality["doc"]
    src = Path(sess["name"] or "resume")
    review_id = uuid.uuid4().hex
    content_sha256 = sess.get("content_sha256") or _sha256(sess["text"])
    config_hash = _review_config_hash(job, jd, bool(images), repeat)

    related = _history_for_content(content_sha256)
    comparable = [r for r in related if r.get("config_hash") == config_hash]
    prev_entry = comparable[0] if comparable else None
    prev_public = _history_public(prev_entry) if prev_entry else None
    delta = None
    if prev_entry and isinstance(prev_entry.get("score"), int) \
            and isinstance(doc.get("overall_score"), int):
        delta = doc["overall_score"] - prev_entry["score"]
    comparison_note = ""
    if delta is None and related:
        comparison_note = "这份简历评过，但岗位、JD、模型或图片设置与本次不同，不直接比较分差。"

    warning_msgs = [p["message"] for p in quality["warnings"]]
    body = review_mod.render(
        doc, src, job, sess["notes"], warning_msgs[:3], sess["text"], scores, delta,
        quality=quality,
    )
    block = review_mod.paste_md(doc, sess["text"])

    file_stem = _artifact_stem(src.stem, review_id)
    paths = _report_paths({"file_stem": file_stem})
    if not paths:
        return {"ok": False, "error": "报告文件名生成失败，请换一个文件名再试"}
    created: list[Path] = []
    try:
        REPORTS.mkdir(parents=True, exist_ok=True)
        paths[".md"].write_text(body, encoding="utf-8", newline="\n")
        created.append(paths[".md"])
        paths[".json"].write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        created.append(paths[".json"])
        paths[".txt"].write_text(sess["text"], encoding="utf-8", newline="\n")
        created.append(paths[".txt"])
        try:
            review_mod.write_docx(body, paths[".docx"], title=f"简历评审 · {src.stem}")
            created.append(paths[".docx"])
        except Exception:  # noqa: BLE001
            pass
    except OSError as exc:
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": False, "error": f"报告写入失败：{exc}", "retryable": True}

    entry = {
        "review_id": review_id,
        "name": src.stem,
        "file_stem": file_stem,
        "created_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "score": doc.get("overall_score"),
        "verdict": doc.get("verdict"),
        "job": job,
        "jd_provided": bool(jd),
        "content_sha256": content_sha256,
        "config_hash": config_hash,
        "prompt_version": run_eval.DEFAULT_PROMPT_VERSION,
        "model": os.environ.get("DSH_LLM_MODEL", "deepseek-chat"),
        "visual": bool(images),
        "repeat": repeat,
        "legacy": False,
    }
    try:
        _history_append(entry)
    except Exception as exc:  # noqa: BLE001
        for path in created:
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
        return {"ok": False, "error": f"历史记录写入失败，报告未保留：{exc}", "retryable": True}

    return {
        "ok": True,
        "report": body,
        "paste": block,
        "name": src.stem,
        "review_id": review_id,
        "score": doc.get("overall_score"),
        "verdict": doc.get("verdict"),
        "gates": {
            "hr": bool((doc.get("gate_hr") or {}).get("pass")),
            "interview": bool((doc.get("gate_interview") or {}).get("pass")),
        },
        "format": (doc.get("format_review") or {}).get("verdict"),
        "delta": delta,
        "comparison_note": comparison_note,
        "prev": prev_public,
        "errors": [],
        "warnings": warning_msgs[:3],
        "filtered": len(quality["filtered"]),
        "visual_used": bool(images),
        "ocr_used": sess.get("kind") in {"scanned-pdf", "image"},
        "scores": scores,
        "quality": {
            "eligible": True,
            "gate_version": quality_gate.GATE_VERSION,
            "filtered_count": len(quality["filtered"]),
            "warning_count": len(quality["warnings"]),
        },
    }


# ---------------------------------------------------------------- 只读接口
def api_history(limit: int = 12) -> dict:
    """只返回摘要，不返回简历正文；每项用 review_id 关联自己的报告与预览。"""
    items = [_history_public(r) for r in _history_load()["reviews"]]
    items = [it for it in items if it["has_report"]]
    items.sort(key=lambda x: x.get("created_at") or "", reverse=True)
    return {"ok": True, "items": items[:limit]}


def api_report(review_id: str) -> dict:
    """按 review_id 读取一份历史报告及其自己的脱敏原文。"""
    if not _valid_review_id(review_id):
        return {"ok": False, "error": "记录 ID 不对"}
    entry = _history_find(review_id)
    if not entry:
        return {"ok": False, "error": "找不到这份记录"}
    paths = _report_paths(entry)
    if not paths or not paths[".md"].exists():
        return {"ok": False, "error": "找不到这份报告文件"}
    doc = {}
    if paths[".json"].exists():
        try:
            doc = json.loads(paths[".json"].read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            doc = {}
    resume = paths[".txt"].read_text(encoding="utf-8") if paths[".txt"].exists() else ""
    try:
        block = review_mod.paste_md(doc, resume) if doc else ""
    except Exception:  # noqa: BLE001
        block = ""
    return {
        "ok": True,
        "review_id": entry["review_id"],
        "name": entry.get("name") or entry.get("file_stem") or "未命名",
        "report": paths[".md"].read_text(encoding="utf-8"),
        "paste": block,
        "resume_text": resume[:MAX_PREVIEW_TEXT],
        "resume_available": bool(resume),
        "text_truncated": len(resume) > MAX_PREVIEW_TEXT,
        "preview": resume[:600],
        "score": doc.get("overall_score", entry.get("score")),
        "verdict": doc.get("verdict", entry.get("verdict")),
        "gates": {
            "hr": bool((doc.get("gate_hr") or {}).get("pass")),
            "interview": bool((doc.get("gate_interview") or {}).get("pass")),
        },
        "format": (doc.get("format_review") or {}).get("verdict"),
        "delta": None,
        "prev": None,
        "errors": [],
        "warnings": [],
        "historical": True,
        "legacy": bool(entry.get("legacy")),
        "legacy_runs": entry.get("legacy_runs"),
        "created_at": entry.get("created_at"),
        "job": entry.get("job") or "",
    }


def api_delete_report(review_id: str) -> dict:
    """按 review_id 删除一份报告的全部落盘文件与历史摘要。"""
    if not _valid_review_id(review_id):
        return {"ok": False, "error": "记录 ID 不对", "_status": 400}
    entry = _history_find(review_id)
    if not entry:
        return {"ok": False, "error": "找不到这份记录", "_status": 404}
    paths = _report_paths(entry)
    if not paths:
        return {"ok": False, "error": "报告路径异常", "_status": 500}

    removed: list[str] = []
    for ext in (".md", ".json", ".txt", ".docx", ".raw.txt", ".retry.txt"):
        f = paths[ext]
        if f.exists():
            try:
                f.unlink()
                removed.append(f.name)
            except OSError as exc:
                return {"ok": False, "error": f"删除失败：{exc}", "_status": 500}
    try:
        had_history = _history_remove(review_id)
    except OSError as exc:
        return {
            "ok": False,
            "error": f"报告文件已删除，但历史摘要清理失败：{exc}",
            "_status": 500,
        }
    if not removed and not had_history:
        return {"ok": False, "error": "找不到这份记录", "_status": 404}
    return {
        "ok": True,
        "review_id": review_id,
        "name": entry.get("name") or entry.get("file_stem") or "",
        "removed": removed,
        "history_removed": had_history,
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = f"ResumeReview/{APP_VERSION}"

    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json.dumps(obj, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def do_GET(self):  # noqa: N802
        if self.path in ("/", "/index.html"):
            html = (WEB / "index.html").read_bytes()
            self._send(200, html, "text/html; charset=utf-8")
        elif self.path == "/api/health":
            self._json({"ok": True, "mock": MOCK, "version": APP_VERSION,
                        "model": os.environ.get("DSH_LLM_MODEL", "deepseek-chat")})
        elif self.path.startswith("/api/download"):
            self._download()
        elif self.path.startswith("/api/history"):
            self._json(api_history())
        elif self.path.startswith("/api/report"):
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            self._json(api_report((q.get("id") or [""])[0]))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _download(self) -> None:
        """按 review_id 下载它自己的报告，客户端不能传任意文件路径。"""
        from urllib.parse import parse_qs, quote, urlparse

        q = parse_qs(urlparse(self.path).query)
        review_id = (q.get("id") or [""])[0]
        kind = (q.get("kind") or ["md"])[0]
        if kind not in {"md", "docx", "json"}:
            kind = "md"
        ext = {"md": ".md", "docx": ".docx", "json": ".json"}[kind]
        if not review_id:
            self._json({"ok": False, "error": "缺少记录 ID"}, 400)
            return
        if not _valid_review_id(review_id):
            self._json({"ok": False, "error": "记录 ID 不对"}, 400)
            return
        entry = _history_find(review_id)
        if not entry:
            self._json({"ok": False, "error": "找不到这份记录"}, 404)
            return
        paths = _report_paths(entry)
        f = paths[ext] if paths else None
        if not f or not f.exists():
            self._json({"ok": False, "error": "文件还没生成，先评一次"}, 404)
            return
        data = f.read_bytes()
        ctype = {
            "md": "text/markdown; charset=utf-8",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "json": "application/json; charset=utf-8",
        }[kind]
        download_name = (entry.get("name") or "resume") + ext
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(download_name)}")
        self.end_headers()
        self.wfile.write(data)

    def do_DELETE(self):  # noqa: N802
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(self.path)
        if parsed.path == "/api/report":
            q = parse_qs(parsed.query)
            res = api_delete_report((q.get("id") or [""])[0])
            code = res.pop("_status", 200) if isinstance(res, dict) else 200
            self._json(res, code)
            return
        self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        if length > MAX_BODY:
            # 必须把请求体读掉再回：直接 413 关连接，客户端会拿到 connection reset，
            # 页面上就变成"服务端异常"而不是「文件太大」。
            left = min(length, 80 * 1024 * 1024)
            while left > 0:
                chunk = self.rfile.read(min(256 * 1024, left))
                if not chunk:
                    break
                left -= len(chunk)
            self.close_connection = True
            self._json({"ok": False, "error": "文件太大（上限 30MB），压缩后再试"}, 413)
            return
        try:
            payload = json.loads(self.rfile.read(length).decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return

        try:
            if self.path == "/api/extract":
                ok_rl, why = allow_request(_client_ip(self), 1)
                if not ok_rl:
                    self._json({"ok": False, "error": why}, 429)
                    return
                res = api_extract(payload)
                code = res.pop("_status", 200) if isinstance(res, dict) else 200
                self._json(res, code)
            elif self.path == "/api/review":
                try:
                    reps = int(payload.get("repeat") or 1)
                except (TypeError, ValueError):
                    reps = 1
                ok_rl, why = allow_request(_client_ip(self), max(1, min(3, reps)))
                if not ok_rl:
                    self._json({"ok": False, "error": why}, 429)
                    return
                self._json(api_review(payload))
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"服务端异常：{exc}"}, 500)


def main() -> int:
    global MOCK
    ap = argparse.ArgumentParser(description="简历评审助手 · 本地网站")
    ap.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8765")))
    ap.add_argument("--host", default=os.environ.get("HOST", "127.0.0.1"),
                    help="默认只监听本机；容器/公开部署用 0.0.0.0")
    ap.add_argument("--mock", action="store_true", help="不调模型，用假数据验界面")
    args = ap.parse_args()
    MOCK = args.mock

    run_eval.load_dotenv(ROOT / ".env")

    if not MOCK and not os.environ.get("DSH_LLM_API_KEY"):
        print("[warn] 没有找到 DSH_LLM_API_KEY，评审会失败。可以先加 --mock 看界面。",
              file=sys.stderr)

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    srv.daemon_threads = True

    # 浏览器打开 localhost 时可能走 IPv6(::1)，只监听 IPv4 会 fetch 失败。
    # 额外起一个 ::1 的监听，失败就算了（系统没开 IPv6 很常见）。
    def _serve_v6():
        import socket

        class V6(ThreadingHTTPServer):
            address_family = socket.AF_INET6

        try:
            s6 = V6(("::1", args.port), Handler)
            s6.daemon_threads = True
            s6.serve_forever()
        except OSError:
            pass

    threading.Thread(target=_serve_v6, daemon=True).start()

    url = f"http://{args.host}:{args.port}"
    print(f"简历评审助手已启动：{url}")
    print(f"  如果 localhost 打不开，用 127.0.0.1；两个地址都已监听")
    print(f"模型：{os.environ.get('DSH_LLM_MODEL', 'deepseek-chat')}"
          f"{'（MOCK 模式，不调模型）' if MOCK else ''}")
    print(f"限流：单 IP {RATE_PER_HOUR} 次/小时，全站 {DAILY_LIMIT} 次/天"
          f"（可用 RATE_PER_HOUR / DAILY_LIMIT 调）")
    print(f"报告目录：{REPORTS}")
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
