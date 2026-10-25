"""安全审计回归：临时文件/锁/快照软链（M3/M5/M4）、会话标题转义（M10）、
公开仓脱敏（M8）、日志会话 id 脱敏开关（M9）。

每个用例把脚本复制到临时目录再跑；符号链接需要系统权限，建不了就 skip。
"""
from __future__ import annotations

import importlib.util
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
SCRIPTS = ("hud_inject.py", "hudctl.py", "hud_panel.py", "hud_report.py", "selfcheck.py",
           "panel.example.md")
CLEAR = ("HUD_PANEL", "HUD_LOG", "HUD_SNAPS", "HUD_STATE", "HUD_PROFILE", "HERMES_HOME",
         "HUD_LOG_SESSIONS")
PANEL_BODY = ("session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\ntitle: 测试面板\n"
              "enabled: true\n\n1. 正文\n")
VICTIM_PANEL = ("session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\ntitle: 受害者面板\n"
                "enabled: true\n\n1. 受害者内容\n")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hud-fs-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name in SCRIPTS:
            shutil.copy2(REPO / name, self.dir / name)
        self.panel = self.dir / "panel.md"
        self.log = self.dir / "hud.log"
        self.snaps = self.dir / "snaps"
        self.state = self.dir / "state"
        self.home = self.dir / "home"

    # ---- 夹具 --------------------------------------------------------
    def write_panel(self, text: str = PANEL_BODY) -> None:
        self.panel.write_text(text, encoding="utf-8")

    def victim(self, text: str = VICTIM_PANEL) -> Path:
        path = self.dir / "victim.md"
        path.write_text(text, encoding="utf-8")
        return path

    def symlink(self, link: Path, target: Path) -> None:
        link.parent.mkdir(parents=True, exist_ok=True)
        if link.exists() or link.is_symlink():
            link.unlink()
        try:
            os.symlink(str(Path(target).resolve()), str(link))
        except (OSError, NotImplementedError) as exc:  # 需要开发者模式/管理员
            self.skipTest(f"当前系统不允许创建符号链接：{exc}")

    def env(self, **extra) -> dict:
        env = {k: v for k, v in os.environ.items() if k not in CLEAR}
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(HUD_PANEL=str(self.panel), HUD_LOG=str(self.log),
                   HUD_SNAPS=str(self.snaps), HUD_STATE=str(self.state))
        env.update(extra)
        return env

    def ctl(self, *args, extra_env=None, stdin_text=None):
        return subprocess.run([PY, str(self.dir / "hudctl.py"), *args], input=stdin_text,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**(extra_env or {})),
                              cwd=str(self.dir), timeout=120)

    def inject(self, sid="AAA", extra_env=None):
        return subprocess.run([PY, str(self.dir / "hud_inject.py")],
                              input=f'{{"session_id": "{sid}", "profile": "default"}}',
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**(extra_env or {})),
                              cwd=str(self.dir), timeout=120)

    def load_module(self, name: str):
        """同进程加载脚本副本（要复用「当前 pid」构造旧版可预测的临时文件名）。"""
        sys.path.insert(0, str(self.dir))
        self.addCleanup(lambda: sys.path.remove(str(self.dir)))
        saved = sys.modules.get("hud_panel")

        def restore():
            if saved is not None:
                sys.modules["hud_panel"] = saved
            else:
                sys.modules.pop("hud_panel", None)

        self.addCleanup(restore)
        spec = importlib.util.spec_from_file_location(f"{name}_audit", self.dir / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.PANEL = self.panel
        mod.SNAPS = self.snaps
        mod.LOG = self.log
        return mod

    def make_home(self, rows, source="chat"):
        """rows: [(id, title)]；建最小可用的 Hermes 家目录（config/allowlist/state.db）。"""
        self.home.mkdir(parents=True, exist_ok=True)
        (self.home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (self.home / "shell-hooks-allowlist.json").write_text(
            '{"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}',
            encoding="utf-8")
        con = sqlite3.connect(self.home / "state.db")
        con.execute("create table sessions (id text primary key, title text, "
                    "last_activity_at real, message_count int, archived int default 0, "
                    "hidden int default 0, source text default 'chat')")
        con.executemany("insert into sessions values (?,?,?,?,0,0,?)",
                        [(sid, title, float(i), 1, source) for i, (sid, title) in enumerate(rows)])
        con.commit()
        con.close()
        return self.home


class TempAndLockSymlinkCase(Base):
    """M3/M5：可预测的临时文件名与锁文件都不能跟随软链。"""

    def test_lock_symlink_is_refused_and_victim_untouched(self):
        self.write_panel()
        victim = self.victim("VICTIM-LOCK")
        self.symlink(self.dir / "panel.md.lock", victim)
        r = self.ctl("off")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("面板锁", r.stdout + r.stderr)
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM-LOCK")
        self.assertIn("enabled: true", self.panel.read_text(encoding="utf-8"))  # 没写进去

    def test_write_panel_ignores_predictable_tmp_symlink(self):
        """旧实现用 panel.md.<pid>.tmp：预置同名软链就会把面板内容写进受害文件。"""
        self.write_panel()
        victim = self.victim("VICTIM-TMP")
        mod = self.load_module("hudctl")
        legacy = self.dir / f"panel.md.{os.getpid()}.tmp"
        self.symlink(legacy, victim)
        mod.write_panel({"session_id": "any", "expires_at": "2099-01-01T00:00:00+08:00",
                         "title": "t", "enabled": "true"}, "1. 新正文")
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM-TMP")
        self.assertIn("1. 新正文", self.panel.read_text(encoding="utf-8"))
        self.assertTrue(os.path.islink(legacy))     # 预置的软链没被当成临时文件

    def test_selfcheck_state_ignores_predictable_tmp_symlink(self):
        self.state.mkdir(parents=True, exist_ok=True)
        victim = self.victim("VICTIM-STATE")
        legacy = self.state / f"selfcheck.json.{os.getpid()}.tmp"
        self.symlink(legacy, victim)
        saved = os.environ.get("HUD_STATE")
        os.environ["HUD_STATE"] = str(self.state)
        self.addCleanup(lambda: os.environ.__setitem__("HUD_STATE", saved) if saved is not None
                        else os.environ.pop("HUD_STATE", None))
        mod = self.load_module("selfcheck")
        mod._write_state({"status": "ok", "ok": True})
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM-STATE")
        self.assertIn('"status": "ok"', (self.state / "selfcheck.json").read_text(encoding="utf-8"))


class PanelReadSymlinkCase(Base):
    """M4：管理端读路径（status / impact / check / set）拒绝软链面板。"""

    def test_status_refuses_symlink_panel(self):
        victim = self.victim(VICTIM_PANEL)
        self.symlink(self.panel, victim)
        r = self.ctl("status")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("符号链接", r.stdout + r.stderr)
        self.assertNotIn("受害者面板", r.stdout)

    def test_impact_refuses_symlink_panel(self):
        victim = self.victim(VICTIM_PANEL)
        self.symlink(self.panel, victim)
        r = self.ctl("impact")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("符号链接", r.stdout + r.stderr)

    def test_set_refuses_symlink_panel_and_keeps_victim(self):
        victim = self.victim("VICTIM-SET")
        self.symlink(self.panel, victim)
        body = self.dir / "body.md"
        body.write_text("1. 攻击者正文", encoding="utf-8")
        r = self.ctl("set", "--body", str(body), "--ttl", "1h")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("符号链接", r.stdout + r.stderr)
        self.assertEqual(victim.read_text(encoding="utf-8"), "VICTIM-SET")

    def test_dangling_symlink_panel_not_created_through(self):
        """面板是悬空软链时，read_panel_ex 的「自动复制样例」不能顺着链接建出受害文件。"""
        target = self.dir / "created-by-link.md"
        self.symlink(self.panel, target)
        self.assertFalse(target.exists())
        r = self.ctl("status")
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("符号链接", r.stdout + r.stderr)
        self.assertFalse(target.exists(), "内容被顺着悬空软链写到了链接目标")


class SnapshotSymlinkCase(Base):
    """M4：快照读/写都拒绝软链（面板或快照本身）。"""

    def test_snapshot_refuses_symlink_panel(self):
        victim = self.victim(VICTIM_PANEL)
        self.symlink(self.panel, victim)
        r = self.ctl("snapshot")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("符号链接", r.stdout + r.stderr)
        self.assertFalse(self.snaps.exists() and any(self.snaps.glob("*.md")))
        self.assertIn("受害者内容", victim.read_text(encoding="utf-8"))

    def test_restore_ignores_symlink_snapshot(self):
        self.write_panel()
        victim = self.victim(VICTIM_PANEL)
        self.symlink(self.snaps / "2026-01-01.md", victim)
        latest = self.ctl("restore", "latest")
        self.assertNotEqual(latest.returncode, 0, latest.stdout + latest.stderr)
        self.assertIn("没有快照", latest.stdout + latest.stderr)
        named = self.ctl("restore", "--file", "2026-01-01.md")
        self.assertNotEqual(named.returncode, 0, named.stdout + named.stderr)
        self.assertIn("符号链接", named.stdout + named.stderr)
        self.assertIn("1. 正文", self.panel.read_text(encoding="utf-8"))   # 面板没被覆盖


class TitleEscapeCase(Base):
    """M10：会话标题里的 ANSI / C0 不能原样打到终端。"""

    EVIL = "标题\x1b[31m红\x1b[0m\x07\x1b]0;owned\x07needle"

    def test_sessions_strips_ansi_and_c0(self):
        self.write_panel()
        self.make_home([("20260101_000000_aaaaaaaa", self.EVIL)])
        r = self.ctl("sessions", extra_env={"HERMES_HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("\x1b", r.stdout)
        self.assertNotIn("\x07", r.stdout)
        self.assertIn("标题红needle", r.stdout)

    def test_use_multi_hit_listing_strips_ansi(self):
        self.write_panel()
        self.make_home([("20260101_000000_aaaaaaaa", self.EVIL),
                        ("20260101_000000_bbbbbbbb", "另一个 needle")])
        r = self.ctl("use", "needle", extra_env={"HERMES_HOME": str(self.home)})
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("匹配到多个会话", r.stdout + r.stderr)
        self.assertNotIn("\x1b", r.stdout + r.stderr)
        self.assertNotIn("\x07", r.stdout + r.stderr)

    def test_impact_verbose_strips_ansi(self):
        self.write_panel()
        self.make_home([("20260101_000000_aaaaaaaa", self.EVIL)])
        r = self.ctl("impact", "--verbose", extra_env={"HERMES_HOME": str(self.home)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("\x1b", r.stdout)
        self.assertNotIn("\x07", r.stdout)
        self.assertIn("标题红needle", r.stdout)


class LogPrivacyCase(Base):
    """M9：HUD_LOG_SESSIONS=0 时日志只记会话 id 的哈希前缀。"""

    SID = "20260101_000000_abcdef"

    def test_default_keeps_full_session_id(self):
        self.write_panel()
        self.inject(self.SID)
        self.assertIn(f"session={self.SID}", self.log.read_text(encoding="utf-8"))

    def test_hashed_session_id_when_disabled(self):
        self.write_panel()
        self.inject(self.SID, extra_env={"HUD_LOG_SESSIONS": "0"})
        text = self.log.read_text(encoding="utf-8")
        self.assertNotIn(self.SID, text)
        self.assertRegex(text, r"session=#[0-9a-f]{8}\b")
        self.assertIn("inject", text)       # 注入本身照常


class PublicRepoCase(unittest.TestCase):
    """M8：公开仓里只能出现合成会话 id（不把真 id 写进测试本身）。"""

    SYNTHETIC = "20260101_000000_abcdef"
    SID_RE = re.compile(r"\b20\d{6}_\d{6}_[0-9a-f]{6}\b")

    def test_only_synthetic_session_ids_in_shipped_docs(self):
        files = [p for p in REPO.glob("*.py")] + [p for p in REPO.glob("*.md")]
        files += list((REPO / "docs").glob("*.md"))
        found = {}
        for path in files:
            hits = set(self.SID_RE.findall(path.read_text(encoding="utf-8", errors="replace")))
            if hits:
                found[path.name] = hits
        self.assertEqual(found, {"README.md": {self.SYNTHETIC},
                                 "hud_panel.py": {self.SYNTHETIC}})

    def test_example_uses_synthetic_id(self):
        text = (REPO / "hud_panel.py").read_text(encoding="utf-8")
        self.assertIn(self.SYNTHETIC, text)


if __name__ == "__main__":
    unittest.main()
