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
import json
import os
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

import check_output  # noqa: E402
import ingest  # noqa: E402
import run_eval  # noqa: E402
import review as review_mod  # noqa: E402

WEB = Path(__file__).resolve().parent
MAX_FILE = 30 * 1024 * 1024          # 单个文件 30MB（业务上限）
# 请求体上限要按 base64 膨胀算：30MB 文件 → 约 40MB JSON，再留点余量。
MAX_BODY = 45 * 1024 * 1024
SESSION_TTL = 30 * 60            # 会话保留 30 分钟

SESSIONS: dict[str, dict] = {}
LOCK = threading.Lock()
MOCK = False

HISTORY = ROOT / "reports" / ".history.json"
MAX_HIST_PER_FILE = 20


def _history_load() -> dict:
    try:
        return json.loads(HISTORY.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def _history_of(stem: str) -> list[dict]:
    return (_history_load().get(stem) or [])[-MAX_HIST_PER_FILE:]


def _history_push(stem: str, entry: dict) -> None:
    """本地历史：只记分数/档位/岗位，不记简历内容。给"比上次 +N"用。"""
    data = _history_load()
    data.setdefault(stem, []).append(entry)
    data[stem] = data[stem][-MAX_HIST_PER_FILE:]
    try:
        HISTORY.parent.mkdir(exist_ok=True)
        HISTORY.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass


def gc_sessions() -> None:
    now = time.time()
    with LOCK:
        for k in [k for k, v in SESSIONS.items() if now - v["ts"] > SESSION_TTL]:
            SESSIONS.pop(k, None)


# ---------------------------------------------------------------- API
def api_extract(payload: dict) -> dict:
    name = payload.get("name") or "resume"
    b64 = payload.get("b64")
    raw_text = payload.get("text")

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
        tmp = Path(tempfile.mkdtemp()) / f"upload{suffix}"
        tmp.write_bytes(blob)
        try:
            ing = ingest.load(tmp)
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "error": str(exc)}
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
    with LOCK:
        SESSIONS[sid] = {
            "text": text,
            "images": images,
            "name": stem,
            "kind": kind,
            "notes": notes,
            "ts": time.time(),
        }

    hist = _history_of(stem)
    return {
        "ok": True,
        "session_id": sid,
        "chars": len(text),
        "kind": kind,
        "pages": len(images),
        "preview": text[:600],
        "notes": notes,
        "runs": len(hist),
        "prev": hist[-1] if hist else None,
    }


def api_review(payload: dict) -> dict:
    sid = payload.get("session_id") or ""
    job = (payload.get("job") or "").strip()[:120] or "未提供（按简历里的求职意向推断）"
    jd = (payload.get("jd") or "").strip()[:6000]
    visual = bool(payload.get("visual", True))

    with LOCK:
        sess = SESSIONS.get(sid)
    if not sess:
        return {"ok": False, "error": "会话已过期，请重新上传简历"}

    images = sess["images"] if visual else []
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
        doc, scores = run_eval.mock_review(sess["name"], {}, sess["text"]), [1]
    else:
        try:
            doc, scores = review_mod.run_reviews(prompt, images, repeat)
        except SystemExit as exc:
            return {"ok": False, "error": str(exc)}
        except json.JSONDecodeError:
            return {"ok": False, "error": "模型返回的不是合法 JSON，请重试一次"}
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            return {"ok": False, "error": f"评审失败：{exc}"}

    res = check_output.check(doc, sess["text"])
    src = Path(sess["name"] or "resume")
    hist = _history_of(src.stem)
    prev_score = hist[-1].get("score") if hist and isinstance(hist[-1], dict) else None
    delta = None
    if isinstance(prev_score, int) and isinstance(doc.get("overall_score"), int):
        delta = doc["overall_score"] - prev_score

    body = review_mod.render(
        doc, src, job, sess["notes"], res["errors"][:3], sess["text"], scores, delta
    )
    block = review_mod.paste_md(doc, sess["text"])

    # 顺手把报告落盘，方便用户拿走
    try:
        rep = ROOT / "reports"
        rep.mkdir(exist_ok=True)
        (rep / f"{src.stem}.md").write_text(body, encoding="utf-8", newline="\n")
        (rep / f"{src.stem}.json").write_text(
            json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (rep / f"{src.stem}.txt").write_text(sess["text"], encoding="utf-8", newline="\n")
        try:
            review_mod.write_docx(body, rep / f"{src.stem}.docx", title=f"简历评审 · {src.stem}")
        except Exception:  # noqa: BLE001
            pass
    except Exception:  # noqa: BLE001
        pass

    try:
        _history_push(src.stem, {
            "ts": time.strftime("%Y-%m-%d %H:%M"),
            "score": doc.get("overall_score"),
            "verdict": doc.get("verdict"),
            "job": job,
            "jd": bool(jd),
        })
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "report": body,
        "paste": block,
        "name": src.stem,
        "score": doc.get("overall_score"),
        "verdict": doc.get("verdict"),
        "gates": {
            "hr": bool((doc.get("gate_hr") or {}).get("pass")),
            "interview": bool((doc.get("gate_interview") or {}).get("pass")),
        },
        "format": (doc.get("format_review") or {}).get("verdict"),
        "delta": delta,
        "prev": hist[-1] if hist else None,
        "errors": res["errors"][:3],
        "warnings": res["warnings"][:3],
        "scores": scores,
    }


# ---------------------------------------------------------------- 只读接口
def api_history(limit: int = 12) -> dict:
    """侧边栏「最近评审」：只读 .history.json 里的分数摘要，不含简历正文。"""
    items = []
    for stem, runs in _history_load().items():
        if not runs:
            continue
        last = runs[-1] or {}
        items.append({
            "name": stem,
            "ts": last.get("ts"),
            "score": last.get("score"),
            "verdict": last.get("verdict"),
            "runs": len(runs),
            "has_report": (ROOT / "reports" / f"{stem}.md").exists(),
        })
    items.sort(key=lambda x: x.get("ts") or "", reverse=True)
    return {"ok": True, "items": items[:limit]}


def api_report(name: str) -> dict:
    """重看一份历史报告：把 reports/ 里的 md/json/txt 重新组装成和刚评完一样的结构。"""
    stem = Path(name or "").stem
    rep = (ROOT / "reports").resolve()
    md = (rep / f"{stem}.md").resolve()
    js = (rep / f"{stem}.json").resolve()
    tx = (rep / f"{stem}.txt").resolve()
    if not stem or rep not in md.parents or not md.exists():
        return {"ok": False, "error": "找不到这份报告"}
    doc = {}
    if js.exists():
        try:
            doc = json.loads(js.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            doc = {}
    resume = tx.read_text(encoding="utf-8") if tx.exists() else ""
    try:
        block = review_mod.paste_md(doc, resume) if doc else ""
    except Exception:  # noqa: BLE001
        block = ""
    return {
        "ok": True,
        "name": stem,
        "report": md.read_text(encoding="utf-8"),
        "paste": block,
        "score": doc.get("overall_score"),
        "verdict": doc.get("verdict"),
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
    }


# ---------------------------------------------------------------- HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ResumeReview/1.0"

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
            self._json({"ok": True, "mock": MOCK,
                        "model": os.environ.get("DSH_LLM_MODEL", "deepseek-chat")})
        elif self.path.startswith("/api/download"):
            self._download()
        elif self.path.startswith("/api/history"):
            self._json(api_history())
        elif self.path.startswith("/api/report"):
            from urllib.parse import parse_qs, urlparse

            q = parse_qs(urlparse(self.path).query)
            self._json(api_report((q.get("name") or [""])[0]))
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def _download(self) -> None:
        """把 reports/ 下生成的 .md / .docx 交给浏览器下载。只允许读 reports 目录。"""
        from urllib.parse import parse_qs, quote, urlparse

        q = parse_qs(urlparse(self.path).query)
        stem = Path((q.get("name") or [""])[0]).stem
        kind = (q.get("kind") or ["md"])[0]
        ext = {"md": ".md", "docx": ".docx", "json": ".json"}.get(kind, ".md")
        if not stem or stem in {".", ".."}:
            self._json({"ok": False, "error": "文件名不对"}, 400)
            return
        rep = (ROOT / "reports").resolve()
        f = (rep / f"{stem}{ext}").resolve()
        if rep not in f.parents or not f.exists():
            self._json({"ok": False, "error": "文件还没生成，先评一次"}, 404)
            return
        data = f.read_bytes()
        ctype = {
            "md": "text/markdown; charset=utf-8",
            "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            "json": "application/json; charset=utf-8",
        }[kind if kind in {"md", "docx", "json"} else "md"]
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Disposition",
                         f"attachment; filename*=UTF-8''{quote(stem + ext)}")
        self.end_headers()
        self.wfile.write(data)

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
                res = api_extract(payload)
                code = res.pop("_status", 200) if isinstance(res, dict) else 200
                self._json(res, code)
            elif self.path == "/api/review":
                self._json(api_review(payload))
            else:
                self._json({"ok": False, "error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            traceback.print_exc()
            self._json({"ok": False, "error": f"服务端异常：{exc}"}, 500)


def main() -> int:
    global MOCK
    ap = argparse.ArgumentParser(description="简历评审助手 · 本地网站")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--host", default="127.0.0.1", help="默认只监听本机")
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
    print("按 Ctrl+C 停止")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
