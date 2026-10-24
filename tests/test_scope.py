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


    def test_preview_reports_lost_sessions(self):
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(
            json.dumps({"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        make_db(self.home / "state.db", [("AAA", "a"), ("BBB", "b")])
        write_panel(self.panel, "1. [scope: all] 全部")
        candidate = self.home / "new.md"
        candidate.write_text("1. [scope: session:AAA] 只给 A\n", encoding="utf-8")
        env = dict(os.environ, HERMES_HOME=str(self.home), HUD_PANEL=str(self.panel),
                   HUD_STATE=str(self.state), PYTHONIOENCODING="utf-8")
        r = subprocess.run([PY, str(REPO / "hudctl.py"), "preview", "--body", str(candidate), "--json"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace",
                           env=env, timeout=180)
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["effective"], {"before": 2, "after": 1})
        self.assertEqual([s["id"] for s in data["lost"]], ["BBB"])
        self.assertEqual(data["gained"], [])

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


class ImpactFilterCase(unittest.TestCase):
    """impact 的 --profile / --kinds 筛选：表格与 JSON 同口径，totals 筛后 + totals_all 全量。"""

    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.home = Path(self.td.name)
        self.panel = self.home / "panel.md"
        self.state = self.home / "state"
        write_panel(self.panel, "1. [scope: all] 全部")
        self.hook_ready(self.home, [("AAA", "a", "chat"), ("BBB", "b", "chat"),
                                    ("cron_1", "c", "cron"), ("kan_1", "k", "kanban")])
        ceo = self.home / "profiles" / "ceo"
        ceo.mkdir(parents=True)
        self.hook_ready(ceo, [("CCC", "d", "chat"), ("sub_1", "e", "subagent")])

    def tearDown(self):
        self.td.cleanup()

    def hook_ready(self, base: Path, sessions):
        """让 base 档声明钩子并同意，然后建库（带 source 列，便于造分类）。"""
        base.mkdir(parents=True, exist_ok=True)
        (base / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (base / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        con = sqlite3.connect(base / "state.db")
        con.execute("create table sessions (id text, title text, last_activity_at real,"
                    " archived int default 0, hidden int default 0, source text default 'chat')")
        con.executemany("insert into sessions values (?,?,1,0,0,?)",
                        [(sid, title, source) for sid, title, source in sessions])
        con.commit()
        con.close()

    def impact(self, *args):
        env = dict(os.environ, HERMES_HOME=str(self.home), HUD_PANEL=str(self.panel),
                   HUD_STATE=str(self.state), PYTHONIOENCODING="utf-8")
        return subprocess.run([PY, str(REPO / "hudctl.py"), "impact", *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=env, timeout=180)

    def test_profile_filter(self):
        r = self.impact("--json", "--profile", "ceo")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["panel"]["filters"], {"profiles": ["ceo"], "kinds": []})
        self.assertEqual([p["profile"] for p in data["profiles"]], ["ceo"])
        self.assertEqual(sorted(s["id"] for s in data["sessions"]), ["CCC", "sub_1"])
        self.assertEqual(data["totals"]["sessions"], 2)
        self.assertEqual(data["totals"]["effective"], 2)
        # 全量仍在，便于对照
        self.assertEqual(data["totals_all"]["sessions"], 6)
        self.assertEqual(data["totals_all"]["effective"], 6)

    def test_kind_filter_and_profile_recount(self):
        r = self.impact("--json", "--kinds", "chat")
        self.assertEqual(r.returncode, 0, r.stderr)
        data = json.loads(r.stdout)
        self.assertEqual(data["panel"]["filters"], {"profiles": [], "kinds": ["chat"]})
        self.assertEqual(sorted(s["id"] for s in data["sessions"]), ["AAA", "BBB", "CCC"])
        self.assertEqual(data["totals"]["sessions"], 3)
        self.assertEqual(data["totals_all"]["sessions"], 6)
        rows = {p["profile"]: p for p in data["profiles"]}
        self.assertEqual((rows["default"]["sessions"], rows["ceo"]["sessions"]), (2, 1))
        self.assertEqual(data["kinds"], {"chat": 3})

        r2 = self.impact("--json", "--kinds", "cron,subagent")
        self.assertEqual(r2.returncode, 0, r2.stderr)
        got = json.loads(r2.stdout)
        self.assertEqual(sorted(s["id"] for s in got["sessions"]), ["cron_1", "sub_1"])
        self.assertEqual(got["totals"]["sessions"], 2)

    def test_table_respects_filters(self):
        r = self.impact("--profile", "default", "--kinds", "cron", "--verbose")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("筛选：档=default 分类=cron", r.stdout)
        self.assertIn("合计 1 条会话", r.stdout)
        self.assertIn("全量 6 条", r.stdout)
        self.assertIn("cron_1", r.stdout)
        self.assertNotIn("AAA", r.stdout)  # 被 kind 筛掉
        self.assertNotIn("CCC", r.stdout)  # 被档筛掉

    def test_profile_without_db_is_noted(self):
        (self.home / "profiles" / "ghost").mkdir()  # 目录在、没有 state.db
        r = self.impact("--profile", "ghost")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("没有会话库", r.stdout)
        self.assertIn("合计 0 条会话", r.stdout)

    def test_invalid_filter_values(self):
        bad_profile = self.impact("--json", "--profile", "nope")
        self.assertEqual(bad_profile.returncode, 1)
        self.assertIn("不认识的档名", bad_profile.stderr)
        self.assertIn("ceo", bad_profile.stderr)  # 现有的档列给用户看

        bad_kind = self.impact("--kinds", "nope")
        self.assertEqual(bad_kind.returncode, 1)
        self.assertIn("不认识的分类", bad_kind.stderr)

        empty = self.impact("--profile", "")
        self.assertEqual(empty.returncode, 1)
        self.assertIn("--profile 不能为空", empty.stderr)

        wrong_case = self.impact("--profile", "CEO")
        self.assertEqual(wrong_case.returncode, 1)
        self.assertIn("大小写敏感", wrong_case.stderr)


if __name__ == "__main__":
    unittest.main()
