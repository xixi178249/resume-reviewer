"""版本快照：每次「采用补丁」都把整篇文本另存为一个不可变版本，用 parent 串成链。

为什么存全文而不是存补丁：
- 靠反向应用补丁回滚，边界情况永远处理不完（同一句出现两次、用户中间手改过、占位符被填过）；
- 简历只有一两页，几十 KB，存全文最笨也最不会错；
- 撤回 = 切回 parent，不需要"逆向计算"，出错概率接近 0。

目录形态（默认 reports/versions/）：
    {doc_id}/v1.txt   v1.json
    {doc_id}/v2.txt   v2.json      ← parent=1，source=patch
    {doc_id}/v3.txt   v3.json      ← parent=2
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

MAX_VERSIONS = 10

# 版本正文/元信息文件名：v1.json、v12.json；不含 v1.review.json
_VERSION_FILE = re.compile(r"^v\d+\.json$")

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ROOT = ROOT / "reports" / "versions"


class VersionError(RuntimeError):
    """版本链相关的错误，文案直接给用户看。"""


class VersionLimit(VersionError):
    """版本数超过上限。"""


def doc_id_for(text: str) -> str:
    """用正文指纹当链 id：同一份简历无论叫什么文件名都归到一条链上。"""
    return hashlib.sha256((text or "").strip().encode("utf-8")).hexdigest()[:12]


def _now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


class VersionStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else DEFAULT_ROOT

    # ------------------------------------------------------------ 内部
    def _dir(self, doc_id: str) -> Path:
        if not doc_id or "/" in doc_id or "\\" in doc_id or ".." in doc_id:
            raise VersionError(f"doc_id 不合法：{doc_id!r}")
        return self.root / doc_id

    def _meta_path(self, doc_id: str, version: int) -> Path:
        return self._dir(doc_id) / f"v{version}.json"

    def _text_path(self, doc_id: str, version: int) -> Path:
        return self._dir(doc_id) / f"v{version}.txt"

    def _write(self, doc_id: str, version: int, text: str, meta: dict) -> dict:
        d = self._dir(doc_id)
        d.mkdir(parents=True, exist_ok=True)
        # 文本不加 BOM：这个项目里 VERSION 文件曾因 BOM 把 Server 头搞崩，文本文件一律 utf-8 裸写
        self._text_path(doc_id, version).write_text(text, encoding="utf-8")
        record = {
            "doc_id": doc_id,
            "version": version,
            "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            "chars": len(text),
            "created_at": _now(),
            **meta,
        }
        self._meta_path(doc_id, version).write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return record

    # ------------------------------------------------------------ 读
    def list(self, doc_id: str) -> list[dict]:
        d = self._dir(doc_id)
        if not d.is_dir():
            return []
        out: list[dict] = []
        # 只认 v<数字>.json：评审结果存成 v1.review.json，用 v*.json 会把它们也匹配进来，
        # int("1.review") 直接 ValueError（第一次端到端就在这炸的）
        for p in sorted(
            (q for q in d.glob("v*.json") if _VERSION_FILE.match(q.name)),
            key=lambda x: int(x.stem[1:]),
        ):
            try:
                out.append(json.loads(p.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue
        return sorted(out, key=lambda r: r.get("version", 0))

    def get(self, doc_id: str, version: int) -> dict:
        mp = self._meta_path(doc_id, version)
        if not mp.exists():
            raise VersionError(f"版本不存在：{doc_id} v{version}")
        record = json.loads(mp.read_text(encoding="utf-8"))
        record["text"] = self._text_path(doc_id, version).read_text(encoding="utf-8")
        return record

    def latest(self, doc_id: str) -> dict:
        items = self.list(doc_id)
        if not items:
            raise VersionError(f"版本链不存在：{doc_id}")
        return self.get(doc_id, items[-1]["version"])

    def chain_summary(self, doc_id: str) -> list[dict]:
        """给 UI 左侧时间线用的精简列表（不含正文，避免一次传几十 KB）。"""
        return [
            {
                "version": r.get("version"),
                "parent": r.get("parent"),
                "source": r.get("source"),
                "created_at": r.get("created_at"),
                "score": r.get("score"),
                "verdict": r.get("verdict"),
                "chars": r.get("chars"),
                "applied": r.get("applied_patch_ids") or [],
            }
            for r in self.list(doc_id)
        ]

    # ------------------------------------------------------------ 写
    def create(self, text: str, *, source: str = "upload", meta: dict | None = None) -> dict:
        if not (text or "").strip():
            raise VersionError("简历正文为空，不能建档")
        doc_id = doc_id_for(text)
        if self.list(doc_id):
            # 同一份简历再传一次：直接接着用现有链，不新建 v1（否则时间线会出现两条一样的起点）
            return self.latest(doc_id)
        return self._write(doc_id, 1, text, {
            "parent": None,
            "source": source,
            "applied_patch_ids": [],
            **(meta or {}),
        })

    def append(
        self,
        doc_id: str,
        text: str,
        *,
        parent: int,
        source: str = "patch",
        applied_patch_ids: list[str] | None = None,
        meta: dict | None = None,
    ) -> dict:
        if not (text or "").strip():
            raise VersionError("新版本正文为空")
        items = self.list(doc_id)
        if not items:
            raise VersionError(f"版本链不存在：{doc_id}")
        latest = items[-1]["version"]
        if parent != latest:
            # 只允许线性增长：分叉会让"上一版"这个词失去意义，前后对比也就没法算
            raise VersionError(f"只能基于最新版 v{latest} 追加，收到 parent=v{parent}")
        if len(items) >= MAX_VERSIONS:
            raise VersionLimit(
                f"这份简历已经存了 {len(items)} 个版本（上限 {MAX_VERSIONS}）。"
                "先撤销回旧版本、或下载需要的版本再继续。"
            )
        return self._write(doc_id, latest + 1, text, {
            "parent": parent,
            "source": source,
            "applied_patch_ids": list(applied_patch_ids or []),
            **(meta or {}),
        })

    def update_meta(self, doc_id: str, version: int, **fields) -> dict:
        """给某个版本补记评分等元信息（重评完成后回填）。"""
        record = self.get(doc_id, version)
        record.pop("text", None)
        record.update(fields)
        self._meta_path(doc_id, version).write_text(
            json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return record

    # ------------------------------------------------------------ 评审结果
    def save_review(self, doc_id: str, version: int, doc: dict) -> Path:
        """把这一版的完整评审结果单独存一份。

        为什么不塞进 v{n}.json：评审结果有几十个字段、上万字符，
        混在元信息里会让人打开版本文件时以为自己在看别的东西。
        """
        p = self._dir(doc_id) / f"v{version}.review.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return p

    def load_review(self, doc_id: str, version: int) -> dict | None:
        p = self._dir(doc_id) / f"v{version}.review.json"
        if not p.exists():
            return None
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    def undo(self, doc_id: str) -> dict:
        """撤回：返回 parent 版本（不删除新版本，历史留着可查）。"""
        cur = self.latest(doc_id)
        parent = cur.get("parent")
        if not parent:
            raise VersionError("已经是最早的版本，没有可撤回的内容")
        return self.get(doc_id, parent)

    def delete(self, doc_id: str) -> bool:
        d = self._dir(doc_id)
        if not d.is_dir():
            return False
        shutil.rmtree(d)
        return True
