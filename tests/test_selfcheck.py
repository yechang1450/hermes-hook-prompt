"""selfcheck.py 的自测：机制齐全时 0；注入点缺失时 2；面板状态不该造成假红。"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO = Path(__file__).resolve().parent.parent


class SelfcheckCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        src = self.home / "hermes-agent"
        (src / "agent").mkdir(parents=True)
        (src / "hermes_cli").mkdir()
        (src / "agent" / "turn_context.py").write_text(
            "def f():\n    # pre_llm_call context\n    plugin_user_context = 1\n", encoding="utf-8")
        (src / "agent" / "shell_hooks.py").write_text("# pre_llm_call\n", encoding="utf-8")
        (src / "hermes_cli" / "plugins_dispatch.py").write_text("# pre_llm_call\n", encoding="utf-8")
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n      timeout: 10\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}), encoding="utf-8")
        self.panel = self.home / "panel.md"
        self.write_panel()
        self.state = self.home / "state"

    def tearDown(self):
        self.td.cleanup()

    # ---- helpers ---------------------------------------------------
    def write_panel(self, session="any", expires="2099-01-01T00:00:00+08:00",
                    title="t", enabled="true", body="正文"):
        self.panel.write_text(
            f"session_id: {session}\nexpires_at: {expires}\ntitle: {title}\n"
            f"enabled: {enabled}\n\n{body}\n", encoding="utf-8")

    def env(self, **extra):
        env = dict(os.environ, HERMES_HOME=str(self.home), HUD_STATE=str(self.state),
                   HUD_PANEL=str(self.panel), HUD_PROFILE="default", PYTHONIOENCODING="utf-8",
                   HUD_LOG=str(self.home / "hud.log"))   # 别写到仓库里的真 hud.log
        env.update(extra)
        return env

    def run_check(self, **extra):
        return subprocess.run([sys.executable, str(REPO / "selfcheck.py")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=self.env(**extra), timeout=180)

    def state_data(self):
        return json.loads((self.state / "selfcheck.json").read_text(encoding="utf-8"))

    def inject(self):
        proc = subprocess.run([sys.executable, str(REPO / "hud_inject.py")],
                              input=json.dumps({"session_id": "selfcheck", "profile": "default"}),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(), timeout=120)
        return proc.stdout

    # ---- 基本 ------------------------------------------------------
    def test_all_green(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self.state_data()
        self.assertTrue(data["ok"])
        self.assertEqual(data["status"], "ok")
        self.assertTrue(data["fingerprint"])
        self.assertEqual(data["notices"], [])
        self.assertEqual(list(self.state.glob("*.tmp")), [])   # 原子写不留垃圾

    def test_missing_injection_point(self):
        (self.home / "hermes-agent" / "agent" / "turn_context.py").write_text("nothing\n", encoding="utf-8")
        r = self.run_check()
        self.assertEqual(r.returncode, 2)
        data = self.state_data()
        self.assertFalse(data["ok"])
        self.assertEqual(data["status"], "error")
        self.assertTrue(any(not c["ok"] for c in data["checks"]))

    # ---- P0-1：面板状态不是机制故障 --------------------------------
    def test_expired_live_panel_is_not_a_failure(self):
        self.write_panel(expires="2020-01-01T00:00:00+08:00")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self.state_data()
        self.assertTrue(data["ok"])
        self.assertTrue(any("过期" in note for note in data["notices"]))

    def test_scoped_elsewhere_panel_is_not_a_failure(self):
        self.write_panel(session="20260101_000000_abcdef")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any("只作用于会话" in note for note in self.state_data()["notices"]))

    def test_disabled_panel_is_not_a_failure(self):
        self.write_panel(enabled="false")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(any("暂停" in note for note in self.state_data()["notices"]))

    def test_all_entries_filtered_is_not_a_failure(self):
        self.write_panel(body="1. [scope: session:somebodyelse] 只给别人的条目")
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_real_format_error_still_fails(self):
        self.panel.write_text("session_id: any\n\n缺元数据\n", encoding="utf-8")
        r = self.run_check()
        self.assertEqual(r.returncode, 2)
        failed = [c for c in self.state_data()["checks"] if not c["ok"]]
        self.assertEqual([c["check"] for c in failed], ["panel:格式自检"])

    # ---- P1-10：HUD_STATE 指到文件时友好退出 ----------------------
    def test_hud_state_pointing_at_file_exits_cleanly(self):
        blocker = self.home / "statefile"
        blocker.write_text("x", encoding="utf-8")
        r = self.run_check(HUD_STATE=str(blocker))
        self.assertEqual(r.returncode, 2)
        self.assertIn("HUD_STATE", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_internal_error_writes_status_error(self):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "selfcheck.json").write_text(
            json.dumps({"ok": True, "status": "ok", "fingerprint": "old"}), encoding="utf-8")
        spec = importlib.util.spec_from_file_location("selfcheck_under_test", REPO / "selfcheck.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.STATE = self.state
        mod.STATE_FILE = self.state / "selfcheck.json"

        def boom():
            raise RuntimeError("boom")

        with mock.patch.object(mod, "check", boom), mock.patch.object(mod, "fingerprint", lambda: "fp"):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = mod.main()
        self.assertEqual(rc, 2)
        data = self.state_data()
        self.assertEqual(data["status"], "error")
        self.assertFalse(data["ok"])
        self.assertEqual(list(self.state.glob("*.tmp")), [])

    # ---- P1-11：指纹是内容哈希，两端口径一致 -----------------------
    def test_fingerprint_ignores_mtime_and_tracks_content(self):
        self.assertEqual(self.run_check().returncode, 0)
        fp1 = self.state_data()["fingerprint"]
        target = self.home / "hermes-agent" / "agent" / "turn_context.py"
        os.utime(target, (1, 1))          # 只改 mtime：内容没变 → 指纹必须不变
        self.assertEqual(self.run_check().returncode, 0)
        self.assertEqual(fp1, self.state_data()["fingerprint"])
        target.write_text(
            target.read_text(encoding="utf-8").replace("plugin_user_context = 1",
                                                       "plugin_user_context = 2"),
            encoding="utf-8")             # 改内容 → 指纹必须变
        self.assertEqual(self.run_check().returncode, 0)
        self.assertNotEqual(fp1, self.state_data()["fingerprint"])

    def test_inject_note_agrees_with_selfcheck_fingerprint(self):
        self.assertEqual(self.run_check().returncode, 0)
        self.assertNotIn("Hermes 已更新", self.inject())
        target = self.home / "hermes-agent" / "agent" / "turn_context.py"
        os.utime(target, (1, 1))          # 只碰 mtime，不该触发「已更新」提醒
        self.assertNotIn("Hermes 已更新", self.inject())


if __name__ == "__main__":
    unittest.main()
