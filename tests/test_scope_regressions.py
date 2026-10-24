"""对抗审查发现的缺陷回归测试（fail-closed、缩进、前言、到期非法、钩子精确匹配等）。"""
from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
CLEAR = ("HUD_PANEL", "HUD_LOG", "HUD_SNAPS", "HUD_STATE")


class RegCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        self.panel = self.home / "panel.md"
        self.state = self.home / "state"
        self.log = self.home / "hud.log"

    def tearDown(self):
        self.td.cleanup()

    def base_env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in CLEAR}
        env.update({"PYTHONIOENCODING": "utf-8"})
        env.update(extra)
        return env

    def panel_text(self, body, expires="2099-01-01T00:00:00+08:00"):
        self.panel.write_text(
            f"session_id: any\nexpires_at: {expires}\ntitle: T\nenabled: true\n\n{body}\n",
            encoding="utf-8")

    def inject(self, sid="AAA", profile="default"):
        env = self.base_env(HUD_PANEL=str(self.panel), HUD_STATE=str(self.state),
                            HUD_LOG=str(self.log))
        return subprocess.run([PY, str(REPO / "hud_inject.py")],
                              input=json.dumps({"session_id": sid, "profile": profile}),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)

    def ctx(self, proc):
        return json.loads(proc.stdout)["context"] if proc.stdout.strip() else ""

    def ctl(self, *args, env_extra=None):
        env = self.base_env(HUD_PANEL=str(self.panel), HUD_STATE=str(self.state),
                            **(env_extra or {}))
        return subprocess.run([PY, str(REPO / "hudctl.py"), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)

    # ---- 高 1：非法 scope 必须 fail-closed ----
    def test_invalid_scope_does_not_broadcast(self):
        self.panel_text("1. [scope: session 123] 只给某条")
        proc = self.inject("BBB")
        self.assertEqual(proc.stdout.strip(), "")
        log = self.log.read_text(encoding="utf-8")
        self.assertIn("invalid_scope=1", log)

    # ---- 中 3：缩进子列表归上一条 ----
    def test_indented_sublist_is_continuation(self):
        self.panel_text("1. [scope: session:AAA] 主条\n   - 子项\n2. [scope: all] 第二条")
        self.assertIn("子项", self.ctx(self.inject("AAA")))
        b = self.ctx(self.inject("BBB"))
        self.assertIn("第二条", b)
        self.assertNotIn("子项", b)

    # ---- 中 4：条目之前的前言要保留 ----
    def test_preamble_is_kept(self):
        self.panel_text("## 硬约束\n提示文字\n1. [scope: all] 一条")
        c = self.ctx(self.inject())
        self.assertIn("## 硬约束", c)
        self.assertIn("提示文字", c)

    # ---- 中 5：行尾标注不生效，check 要告警 ----
    def test_inline_scope_warns(self):
        self.panel_text("1. 全部说明 [scope: session:AAA]")
        self.assertEqual(self.ctl("check").returncode, 1)

    # ---- 中 18：代码围栏里的编号不算条目 ----
    def test_code_fence_not_parsed(self):
        self.panel_text("1. [scope: all] 真条目\n```\n1. [scope: session:ZZZ] 代码里的\n```")
        c = self.ctx(self.inject("BBB"))
        self.assertIn("真条目", c)
        self.assertEqual(self.ctl("check").returncode, 0)

    # ---- 高 2：到期时间写坏 → impact 必须报全部不生效 ----
    def test_impact_bad_expiry(self):
        self.panel_text("1. [scope: all] x", expires="2026-13-45T00:00:00+08:00")
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        con = sqlite3.connect(self.home / "state.db")
        con.execute("create table sessions (id text, title text, last_activity_at real,"
                    " archived int default 0, hidden int default 0, source text default 'chat')")
        con.execute("insert into sessions values ('AAA','a',1,0,0,'chat')")
        con.commit(); con.close()
        r = self.ctl("impact", "--json", env_extra={"HERMES_HOME": str(self.home)})
        data = json.loads(r.stdout)
        self.assertTrue(data["panel"]["invalid_expiry"])
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertEqual(data["sessions"][0]["reason"], "到期时间无法解析（注入会整块跳过）")

    # ---- 中 8：注释掉的钩子不算声明；路径不一致不算同意 ----
    def test_hook_state_is_exact(self):
        self.panel_text("1. [scope: all] x")
        (self.home / "config.yaml").write_text(
            "# - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python other.py"}]}),
            encoding="utf-8")
        con = sqlite3.connect(self.home / "state.db")
        con.execute("create table sessions (id text, title text, last_activity_at real,"
                    " archived int default 0, hidden int default 0, source text default 'chat')")
        con.execute("insert into sessions values ('AAA','a',1,0,0,'chat')")
        con.commit(); con.close()
        r = self.ctl("impact", "--json", env_extra={"HERMES_HOME": str(self.home)})
        data = json.loads(r.stdout)
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertIn("没声明", data["sessions"][0]["reason"])

    # ---- 中 6：过期的面板 preview 要按“set 后续期”预演 ----
    def test_preview_simulates_set(self):
        self.panel_text("1. [scope: all] x", expires="2020-01-01T00:00:00+08:00")
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        con = sqlite3.connect(self.home / "state.db")
        con.execute("create table sessions (id text, title text, last_activity_at real,"
                    " archived int default 0, hidden int default 0, source text default 'chat')")
        con.execute("insert into sessions values ('AAA','a',1,0,0,'chat')")
        con.commit(); con.close()
        candidate = self.home / "new.md"
        candidate.write_text("1. [scope: all] y\n", encoding="utf-8")
        r = self.ctl("preview", "--body", str(candidate), "--json",
                     env_extra={"HERMES_HOME": str(self.home)})
        data = json.loads(r.stdout)
        self.assertEqual(data["effective"]["after"], 1)

    # ---- 中 7：--body 带 BOM 不应丢第一条 ----
    def test_bom_body_keeps_first_entry(self):
        self.panel_text("1. [scope: all] 旧")
        candidate = self.home / "new.md"
        candidate.write_text("\ufeff1. [scope: all] 新一\n2. [scope: all] 新二\n", encoding="utf-8")
        r = self.ctl("scope", "1", "all")  # 先确保面板可写
        self.assertEqual(r.returncode, 0)
        # preview 需要能读到会话库；建一个最小 home（CI 上没有真实 HERMES_HOME）
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        con = sqlite3.connect(self.home / "state.db")
        con.execute("create table sessions (id text, title text, last_activity_at real,"
                    " archived int default 0, hidden int default 0, source text default 'chat')")
        con.execute("insert into sessions values ('AAA','a',1,0,0,'chat')")
        con.commit(); con.close()
        # preview 只读，不应把 BOM 当正文
        p = self.ctl("preview", "--body", str(candidate), "--json",
                     env_extra={"HERMES_HOME": str(self.home)})
        data = json.loads(p.stdout)
        self.assertEqual(data["entries"]["after"], 2)

    # ---- 中 20：scope 值不能带换行 ----
    def test_scope_rejects_newline(self):
        self.panel_text("1. [scope: all] x")
        r = self.ctl("scope", "1", "session:AAA\n2. 新条目")
        self.assertNotEqual(r.returncode, 0)
        self.assertNotIn("2. 新条目", self.panel.read_text(encoding="utf-8"))

    # ---- 低 15：非 UTF-8 面板 check 不 traceback ----
    def test_check_non_utf8(self):
        self.panel.write_bytes(b"\xff\xfe\x00bad")
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("UTF-8", r.stdout)


if __name__ == "__main__":
    unittest.main()
