"""M8 留存策略：公网版必须当场说明「不留历史」，且不把数据写进持久目录。

要防的错（都是真会发生的那种）：
- `RETENTION_NOTICE` 拼错，代码静默当 ephemeral → 用户以为在本地跑，结果重启全丢
- 公网版仍然把报告写进 REPORTS_DIR → 页面说「不留」但文件还在，等于骗人
- `/api/history` 里留着一个空列表，界面照旧显示「历史报告」→ 用户以为记录被清了，其实是根本不留
- 临时目录进程退出不删 → 「不留历史」变成「留在 /tmp 里等别人捡」

运行：
    python scripts/test_retention.py -v
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT / "web"))

import server  # noqa: E402


class RetentionModeTest(unittest.TestCase):
    """环境变量解析：拼错不能变成丢数据。"""

    def test_default_is_local(self):
        self.assertEqual(server._retention_mode(None), "local")
        self.assertEqual(server._retention_mode(""), "local")
        self.assertEqual(server._retention_mode("   "), "local")

    def test_explicit_values(self):
        self.assertEqual(server._retention_mode("ephemeral"), "ephemeral")
        self.assertEqual(server._retention_mode("EPHEMERAL"), "ephemeral")
        self.assertEqual(server._retention_mode(" Ephemeral "), "ephemeral")
        self.assertEqual(server._retention_mode("local"), "local")

    def test_typo_falls_back_to_local_not_ephemeral(self):
        # 关键方向：写错时要更保守（留数据），不能更激进（丢数据）。
        for bad in ("ephemral", "eph", "tmp", "true", "1"):
            self.assertEqual(server._retention_mode(bad), "local", bad)


class HistoryNoticeTest(unittest.TestCase):
    """接口层：ephemeral 必须把话说出来，local 不必加噪音。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        reports = Path(self.tmp.name)
        self.patches = [
            mock.patch.object(server, "REPORTS", reports),
            mock.patch.object(server, "HISTORY", reports / ".history.json"),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self.patches):
            p.stop()
        self.tmp.cleanup()

    def test_local_has_no_notice(self):
        with mock.patch.object(server, "EPHEMERAL", False), \
                mock.patch.object(server, "RETENTION", "local"), \
                mock.patch.object(server, "RETENTION_NOTICE", ""):
            out = server.api_history()
        self.assertEqual(out["retention"], "local")
        self.assertNotIn("notice", out)

    def test_ephemeral_states_it_plainly(self):
        notice = "公网版不留历史：报告只存在于当前运行进程，服务重启即清空。要留档请当场下载。"
        with mock.patch.object(server, "EPHEMERAL", True), \
                mock.patch.object(server, "RETENTION", "ephemeral"), \
                mock.patch.object(server, "RETENTION_NOTICE", notice):
            out = server.api_history()
        self.assertEqual(out["retention"], "ephemeral")
        self.assertEqual(out["notice"], notice)
        # 文案必须点明「重启就没了」，否则用户会当成普通的历史面板
        self.assertIn("重启", out["notice"])
        self.assertIn("清空", out["notice"])


class EphemeralStorageTest(unittest.TestCase):
    """进程级：ephemeral 不许写持久目录，退出后临时目录要消失。"""

    def _spawn(self, retention: str, reports_dir: Path) -> subprocess.CompletedProcess:
        env = dict(os.environ)
        env["PYTHONUTF8"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env["RETENTION_NOTICE"] = retention
        env["REPORTS_DIR"] = str(reports_dir)
        code = (
            "import json, sys;"
            f"sys.path.insert(0, {str(ROOT / 'web')!r});"
            f"sys.path.insert(0, {str(ROOT / 'scripts')!r});"
            "import server;"
            "print(json.dumps({'reports': str(server.REPORTS),"
            " 'ephemeral': server.EPHEMERAL,"
            " 'retention': server.RETENTION,"
            " 'notice': server.RETENTION_NOTICE}))"
        )
        return subprocess.run(
            [sys.executable, "-X", "utf8", "-c", code],
            capture_output=True, text=True, encoding="utf-8", env=env,
            cwd=str(ROOT), timeout=120,
        )

    def test_ephemeral_writes_nowhere_persistent(self):
        with tempfile.TemporaryDirectory() as t:
            persistent = Path(t) / "reports"
            proc = self._spawn("ephemeral", persistent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            info = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertTrue(info["ephemeral"])
            self.assertEqual(info["retention"], "ephemeral")
            self.assertIn("重启", info["notice"])
            # ① 报告目录不是配置的那个（配置的是持久目录）
            self.assertNotEqual(Path(info["reports"]).resolve(), persistent.resolve())
            # ② 配置的持久目录里什么都没写
            self.assertFalse(persistent.exists() and any(persistent.iterdir()))
            # ③ 临时目录随进程退出被删掉（否则等于留在 /tmp 等人捡）
            self.assertFalse(Path(info["reports"]).exists())

    def test_local_uses_configured_dir_and_keeps_it(self):
        with tempfile.TemporaryDirectory() as t:
            persistent = Path(t) / "reports"
            proc = self._spawn("local", persistent)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            info = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertFalse(info["ephemeral"])
            self.assertEqual(info["retention"], "local")
            self.assertEqual(info["notice"], "")
            self.assertEqual(Path(info["reports"]).resolve(), persistent.resolve())
            # local 就是要留着：目录必须存在（重启后报告还在）
            self.assertTrue(persistent.is_dir())

    def test_wrong_value_warns_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as t:
            persistent = Path(t) / "reports"
            proc = self._spawn("ephemral", persistent)   # 故意拼错
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("不认识", proc.stdout)
            info = json.loads(proc.stdout.strip().splitlines()[-1])
            self.assertEqual(info["retention"], "local")
            self.assertTrue(persistent.is_dir())


if __name__ == "__main__":
    unittest.main(verbosity=2)
