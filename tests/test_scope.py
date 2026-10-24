"""条目作用域（[scope: …]）与 impact 盘点的自测。"""
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


def write_panel(path: Path, body: str, scope: str = "any") -> None:
    path.write_text(
        f"session_id: {scope}\nexpires_at: 2099-01-01T00:00:00+08:00\n"
        f"title: T\nenabled: true\n\n{body}\n", encoding="utf-8")


def make_db(path: Path, sessions: list[tuple[str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("create table sessions (id text primary key, title text, "
                "last_activity_at real, message_count int, archived int, hidden int)")
    for sid, title in sessions:
        con.execute("insert into sessions values (?,?,?,?,0,0)", (sid, title, 1.0, 3))
    con.commit()
    con.close()


class ScopeCase(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        self.panel = self.home / "panel.md"
        self.state = self.home / "state"
        self.log = self.home / "hud.log"

    def tearDown(self):
        self.td.cleanup()

    def inject(self, sid: str, profile: str = "default"):
        env = dict(os.environ, HUD_PANEL=str(self.panel), HUD_STATE=str(self.state),
                   HUD_LOG=str(self.log), PYTHONIOENCODING="utf-8")
        return subprocess.run([PY, str(REPO / "hud_inject.py")],
                              input=json.dumps({"session_id": sid, "profile": profile}),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)

    def context(self, proc) -> str:
        return json.loads(proc.stdout)["context"]

    def test_scope_filters_entries(self):
        write_panel(self.panel, "1. [scope: all] 全部\n2. [scope: session:AAA] 只给 A\n"
                               "3. [scope: profile:ceo] 只给 ceo\n4. 无标注")
        a = self.context(self.inject("AAA", "default"))
        b = self.context(self.inject("BBB", "default"))
        c = self.context(self.inject("BBB", "ceo"))
        self.assertIn("全部", a)
        self.assertIn("只给 A", a)
        self.assertNotIn("只给 ceo", a)
        self.assertNotIn("只给 A", b)
        self.assertIn("无标注", b)
        self.assertIn("只给 ceo", c)
        # 标注本身不能进上下文
        self.assertNotIn("[scope:", a)

    def test_no_matching_entry_skips(self):
        write_panel(self.panel, "1. [scope: session:ZZZ] 只给 Z")
        proc = self.inject("AAA")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("no-entry-for-session", self.log.read_text(encoding="utf-8"))

    def test_ctl_scope_rewrites_annotation(self):
        write_panel(self.panel, "1. 一号\n2. 二号")
        env = dict(os.environ, HUD_PANEL=str(self.panel), HUD_SNAPS=str(self.home / "snap"))
        r = subprocess.run([PY, str(REPO / "hudctl.py"), "scope", "2", "session:AAA,BBB"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        text = self.panel.read_text(encoding="utf-8")
        self.assertIn("2. [scope: session:AAA,BBB] 二号", text)

    def test_impact_counts_and_reasons(self):
        # default 有钩子+同意；ceo 没有钩子 → 该档全部不生效
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(
            json.dumps({"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        make_db(self.home / "state.db", [("AAA", "a"), ("BBB", "b")])
        prof = self.home / "profiles" / "ceo"
        prof.mkdir(parents=True)
        (prof / "config.yaml").write_text("model: {}\n", encoding="utf-8")
        make_db(prof / "state.db", [("CCC", "c")])
        write_panel(self.panel, "1. [scope: all] 全部\n2. [scope: session:AAA] 只给 A")
        env = dict(os.environ, HERMES_HOME=str(self.home), HUD_PANEL=str(self.panel),
                   HUD_STATE=str(self.state), PYTHONIOENCODING="utf-8")
        r = subprocess.run([PY, str(REPO / "hudctl.py"), "impact", "--json"],
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", env=env, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["totals"]["sessions"], 3)
        self.assertEqual(data["totals"]["effective"], 2)
        ceo = [p for p in data["profiles"] if p["profile"] == "ceo"][0]
        self.assertEqual(ceo["effective"], 0)
        reasons = {s["id"]: s["reason"] for s in data["sessions"] if not s["effective"]}
        self.assertIn("没声明", reasons["CCC"])


if __name__ == "__main__":
    unittest.main()
