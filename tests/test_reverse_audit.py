"""反向审查 10 项回归：profile 自检假红 / 指纹跨档一致 / 尾行措辞 / estimate 过估 /
200KB 面板自救 / 零条目广播 / 续行非法标注 / restore --file 与校验 / enabled 白名单，
外加 scope 行首标签 / 锁路径与快照目录类型 / snapshot glob 等低优先项。
"""
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
SCRIPTS = ("hud_inject.py", "hudctl.py", "hud_panel.py", "hud_report.py", "panel.example.md")
CLEAR = ("HUD_PANEL", "HUD_LOG", "HUD_SNAPS", "HUD_STATE", "HUD_PROFILE",
         "HERMES_HOME", "HERMES_PROFILE", "HERMES_SESSION_PROFILE")


class Base(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)
        self.dir = self.root / "run"
        self.dir.mkdir()
        for name in SCRIPTS:
            (self.dir / name).write_bytes((REPO / name).read_bytes())
        self.panel = self.dir / "panel.md"
        self.log = self.dir / "hud.log"
        self.snaps = self.dir / "snaps"
        self.state = self.dir / "state"

    def tearDown(self):
        self.td.cleanup()

    # ---- 环境 / 夹具 -------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in CLEAR}
        env.update(PYTHONIOENCODING="utf-8", HUD_PANEL=str(self.panel),
                   HUD_LOG=str(self.log), HUD_SNAPS=str(self.snaps), HUD_STATE=str(self.state))
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def panel_text(self, body="1. 正文", session="any",
                   expires="2099-01-01T00:00:00+08:00", title="测试面板",
                   enabled="true", extra_head=""):
        self.panel.write_text(
            f"session_id: {session}\nexpires_at: {expires}\ntitle: {title}\n"
            f"enabled: {enabled}\n{extra_head}\n{body}\n", encoding="utf-8")

    def inject(self, sid="AAA", profile="default", extra_env=None):
        proc = subprocess.run([PY, str(self.dir / "hud_inject.py")],
                              input=json.dumps({"session_id": sid, "profile": profile}),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**(extra_env or {})),
                              cwd=str(self.dir), timeout=180)
        return proc

    @staticmethod
    def ctx(proc) -> str:
        return json.loads(proc.stdout)["context"] if proc.stdout.strip() else ""

    def ctl(self, *args, extra_env=None, stdin_text=None):
        return subprocess.run([PY, str(self.dir / "hudctl.py"), *args], input=stdin_text,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**(extra_env or {})),
                              cwd=str(self.dir), timeout=180)

    def logtext(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def make_db(self, path: Path, rows=(("AAA", "a"),)):
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        con.execute("create table sessions (id text primary key, title text, "
                    "last_activity_at real, message_count int, archived int default 0, "
                    "hidden int default 0, source text default 'chat')")
        con.executemany("insert into sessions values (?,?,?,?,0,0,'chat')",
                        [(sid, title, float(i), 1) for i, (sid, title) in enumerate(rows)])
        con.commit()
        con.close()

    def hook_ready(self, base: Path, rows=(("AAA", "a"),), source: bool = True):
        base.mkdir(parents=True, exist_ok=True)
        if source:
            src = base / "hermes-agent"
            (src / "agent").mkdir(parents=True, exist_ok=True)
            (src / "hermes_cli").mkdir(exist_ok=True)
            (src / "agent" / "turn_context.py").write_text(
                "# pre_llm_call\nplugin_user_context = 1\n", encoding="utf-8")
            (src / "agent" / "shell_hooks.py").write_text("# pre_llm_call\n", encoding="utf-8")
            (src / "hermes_cli" / "plugins_dispatch.py").write_text("# pre_llm_call\n", encoding="utf-8")
        (base / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (base / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        self.make_db(base / "state.db", rows)

    def snapshot(self, name: str, body: str, head: str | None = None):
        self.snaps.mkdir(parents=True, exist_ok=True)
        text = head if head is not None else (
            "session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
            "title: 快照\nenabled: true\n\n")
        (self.snaps / name).write_text(f"{text}{body}\n", encoding="utf-8")


# --------------------------------------------------------------------------- #
# P0-1 / P0-2 / P0-3：profile 自检、指纹、尾行措辞
# --------------------------------------------------------------------------- #
class SelfcheckProfileCase(Base):
    def run_selfcheck(self, **extra):
        return subprocess.run([PY, str(REPO / "selfcheck.py")],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**extra), timeout=240)

    def state_data(self):
        return json.loads((self.state / "selfcheck.json").read_text(encoding="utf-8"))

    def test_profile_home_selfcheck_is_not_red(self):
        base = self.root / "home"
        self.hook_ready(base)
        profile = base / "profiles" / "ceo"
        self.hook_ready(profile, rows=(("BBB", "b"),), source=False)
        self.panel_text("1. 正文")
        r = self.run_selfcheck(HERMES_HOME=profile, HUD_PROFILE="ceo")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self.state_data()
        self.assertTrue(data["ok"], data["checks"])
        self.assertEqual([c["check"] for c in data["checks"] if not c["ok"]], [])
        self.assertTrue(data["fingerprint"])

    def test_missing_source_degrades_to_notice(self):
        base = self.root / "nogit"
        self.hook_ready(base, source=False)
        self.panel_text("1. 正文")
        r = self.run_selfcheck(HERMES_HOME=base)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = self.state_data()
        self.assertTrue(data["ok"], data["checks"])
        self.assertTrue(any("未找到 Hermes 源码" in note for note in data["notices"]))
        source_checks = [c for c in data["checks"] if c["check"].startswith("source:")]
        self.assertTrue(source_checks)
        self.assertTrue(all(c["ok"] for c in source_checks))

    def test_profile_home_without_profile_env_is_not_red(self):
        # 有的启动方式不传 HUD_PROFILE/HERMES_SESSION_PROFILE：家目录本身就是档案目录
        base = self.root / "home"
        self.hook_ready(base)
        profile = base / "profiles" / "ceo"
        self.hook_ready(profile, rows=(("BBB", "b"),), source=False)
        self.panel_text("1. 正文")
        r = self.run_selfcheck(HERMES_HOME=profile)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue(self.state_data()["ok"], self.state_data()["checks"])

    def test_fingerprint_is_independent_of_profile_home(self):
        base = self.root / "home"
        self.hook_ready(base)
        profile = base / "profiles" / "ceo"
        self.hook_ready(profile, rows=(("BBB", "b"),), source=False)
        self.panel_text("1. 正文")
        self.assertEqual(self.run_selfcheck(HERMES_HOME=base).returncode, 0)
        base_fp = self.state_data()["fingerprint"]
        self.assertEqual(self.run_selfcheck(HERMES_HOME=profile, HUD_PROFILE="ceo").returncode, 0)
        profile_fp = self.state_data()["fingerprint"]
        self.assertTrue(base_fp)
        self.assertEqual(base_fp, profile_fp)

    def write_state(self, checks):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "selfcheck.json").write_text(json.dumps(
            {"status": "error", "ok": False, "fingerprint": "x", "checks": checks}),
            encoding="utf-8")

    def test_note_panel_only_suggests_hudctl_check(self):
        self.panel_text()
        self.write_state([{"check": "panel:格式自检", "ok": False, "detail": ""}])
        ctx = self.ctx(self.inject())
        self.assertIn("hudctl.py check", ctx)
        self.assertNotIn("适配新版", ctx)

    def test_note_source_only_mentions_new_version(self):
        self.panel_text()
        self.write_state([{"check": "source:上下文注入点", "ok": False, "detail": ""}])
        ctx = self.ctx(self.inject())
        self.assertIn("适配新版", ctx)
        self.assertNotIn("hudctl.py check", ctx)

    def test_note_config_failure_does_not_blame_new_version(self):
        self.panel_text()
        self.write_state([{"check": "allowlist:已同意", "ok": False, "detail": ""}])
        ctx = self.ctx(self.inject())
        self.assertIn("按安装步骤补齐", ctx)
        self.assertNotIn("适配新版", ctx)

    def test_note_mixed_failure_uses_two_lines(self):
        self.panel_text()
        self.write_state([{"check": "panel:格式自检", "ok": False, "detail": ""},
                          {"check": "source:pre_llm_call 事件", "ok": False, "detail": ""}])
        ctx = self.ctx(self.inject())
        self.assertIn("hudctl.py check", ctx)
        self.assertIn("适配新版", ctx)
        self.assertEqual(sum(1 for line in ctx.splitlines() if line.startswith("⚠")), 2)


# --------------------------------------------------------------------------- #
# P0-4：estimate 过估
# --------------------------------------------------------------------------- #
class EstimateCase(Base):
    def test_1403_body_with_28char_session_can_be_saved(self):
        sid = "20261025_120358_06278b3c9d1f"
        self.assertEqual(len(sid), 28)
        self.panel_text()
        body = self.dir / "body.md"
        text = "字" * 1403
        body.write_text(text, encoding="utf-8")
        r = self.ctl("set", "--ttl", "1h", "--session", sid, "--body", str(body))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(text, self.panel.read_text(encoding="utf-8"))
        self.assertEqual(self.ctl("check").returncode, 0)
        ctx = self.ctx(self.inject(sid=sid))
        self.assertIn(text, ctx)
        self.assertLessEqual(len(ctx), 1600)

    def test_set_warns_but_does_not_refuse_block_over_limit(self):
        self.panel_text()
        body = self.dir / "body.md"
        body.write_text("字" * 1590, encoding="utf-8")
        r = self.ctl("set", "--ttl", "1h", "--body", str(body))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("⚠", r.stdout)
        self.assertIn("1600", r.stdout)
        self.assertIn("字" * 1590, self.panel.read_text(encoding="utf-8"))
        self.assertEqual(self.ctl("check").returncode, 0)      # 整块超限只是状态提示

    def test_set_still_rejects_body_over_1600(self):
        self.panel_text()
        body = self.dir / "body.md"
        body.write_text("字" * 1601, encoding="utf-8")
        r = self.ctl("set", "--ttl", "1h", "--body", str(body))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("上限", r.stdout + r.stderr)


# --------------------------------------------------------------------------- #
# P0-5：超大面板可自救
# --------------------------------------------------------------------------- #
class OversizedSelfRescueCase(Base):
    def test_set_can_overwrite_oversized_panel(self):
        self.panel.write_bytes(b"x" * 250_000)
        body = self.dir / "body.md"
        body.write_text("1. [scope: all] 救回来了", encoding="utf-8")
        r = self.ctl("set", "--ttl", "1h", "--session", "any", "--body", str(body))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.ctl("check").returncode, 0, self.ctl("check").stdout)
        self.assertIn("救回来了", self.ctx(self.inject()))

    def test_status_still_reports_oversized_without_reading_whole(self):
        self.panel.write_bytes(b"x" * 250_000)
        r = self.ctl("status")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("面板过大", r.stdout)


# --------------------------------------------------------------------------- #
# P0-6：零条目 + [scope:] fail-closed
# --------------------------------------------------------------------------- #
class ZeroEntryScopeCase(Base):
    def test_zero_entries_with_scope_is_not_broadcast(self):
        self.panel_text("前言 [scope: all] 说明\n  1. [scope: session:ZZZ] 缩进的不算条目")
        proc = self.inject("BBB")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("scope-without-entry", self.logtext())

    def test_check_errors_on_scope_without_entries(self):
        self.panel_text("前言 [scope: all] 说明\n  1. 缩进的不算条目")
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("没有任何可识别条目", r.stdout)

    def test_filter_body_never_returns_tagged_zero_entry_text(self):
        sys.path.insert(0, str(self.dir))
        try:
            import hud_panel
            self.assertEqual(hud_panel.filter_body("说明 [scope: all] x", "AAA", "default"),
                             ("", 0, 0))
            self.assertEqual(hud_panel.filter_body("纯自由正文", "AAA", "default"),
                             ("纯自由正文", 1, 1))
        finally:
            sys.path.remove(str(self.dir))
            sys.modules.pop("hud_panel", None)

    def test_impact_reports_scope_without_entries(self):
        base = self.root / "home"
        self.hook_ready(base)
        self.panel_text("前言 [scope: all] 说明\n  1. 缩进的不算条目")
        r = self.ctl("impact", "--json", extra_env={"HERMES_HOME": base})
        data = json.loads(r.stdout)
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertIn("没有可识别条目", data["sessions"][0]["reason"])


# --------------------------------------------------------------------------- #
# P0-7：续行非法 [scope:] 保留原文，不整条消失
# --------------------------------------------------------------------------- #
class InvalidContinuationScopeCase(Base):
    def test_invalid_continuation_scope_keeps_entry_text(self):
        self.panel_text("1. 公共\n2. 只给 A\n   [scope: session 123]\n   续行")
        ctx = self.ctx(self.inject("BBB"))
        self.assertIn("只给 A", ctx)
        self.assertIn("续行", ctx)
        self.assertIn("[scope: session 123]", ctx)   # 非法标注按普通文本保留
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("续行标注非法", r.stdout)

    def test_valid_continuation_scope_still_stripped(self):
        self.panel_text("1. 公共\n2. 只给 A\n   [scope: session:AAA]\n   续行")
        a = self.ctx(self.inject("AAA"))
        b = self.ctx(self.inject("BBB"))
        self.assertIn("只给 A", a)
        self.assertNotIn("[scope:", a)
        self.assertNotIn("只给 A", b)


# --------------------------------------------------------------------------- #
# P0-8 / P0-9：restore --file、latest 不选 pre-restore、非法快照拒绝
# --------------------------------------------------------------------------- #
class RestoreCase(Base):
    def test_restore_file_can_bring_back_pre_restore(self):
        self.panel_text("旧版正文")
        self.assertEqual(self.ctl("snapshot").returncode, 0)
        self.panel_text("当前正文")
        self.assertEqual(self.ctl("restore", "latest", "--ttl", "1h").returncode, 0)
        pre = sorted(self.snaps.glob("pre-restore-*.md"))
        self.assertEqual(len(pre), 1)
        self.assertIn("当前正文", pre[0].read_text(encoding="utf-8"))
        self.panel_text("又改了一次")
        r = self.ctl("restore", "--file", pre[0].name, "--ttl", "1h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("当前正文", self.panel.read_text(encoding="utf-8"))

    def test_restore_positional_filename_also_works(self):
        self.panel_text("要找回的正文")
        self.ctl("snapshot")
        self.panel_text("被改掉")
        name = sorted(self.snaps.glob("*.md"))[0].name
        r = self.ctl("restore", name, "--ttl", "1h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("要找回的正文", self.panel.read_text(encoding="utf-8"))

    def test_restore_latest_ignores_pre_restore_only_dir(self):
        self.panel_text("现场")
        self.snapshot("pre-restore-2026-10-22-120000.md", "现场旧照")
        r = self.ctl("restore", "latest")
        self.assertNotEqual(r.returncode, 0, r.stdout)
        self.assertIn("没有快照可恢复", r.stdout + r.stderr)
        self.assertIn("现场", self.panel.read_text(encoding="utf-8"))

    def test_restore_refuses_invalid_snapshot(self):
        self.panel_text("现网现场")
        self.snapshot("2026-10-22.md", "正文", head="session_id: any\n\n")
        r = self.ctl("restore", "latest", "--ttl", "1h")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("不合法", r.stdout + r.stderr)
        self.assertIn("现网现场", self.panel.read_text(encoding="utf-8"))
        self.assertEqual(list(self.snaps.glob("pre-restore-*.md")), [])   # 拒绝时不留现场照

    def test_restore_refuses_unknown_header_key(self):
        self.panel_text("现网现场")
        self.snapshot("2026-10-22.md", "正文",
                      head="session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
                           "title: t\nenabled: true\nowner: 我\n\n")
        r = self.ctl("restore", "latest", "--ttl", "1h")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("未知键", r.stdout + r.stderr)
        self.assertIn("现网现场", self.panel.read_text(encoding="utf-8"))

    def test_prune_documents_manual_cleanup_for_pre_restore(self):
        self.panel_text("现场")
        self.snapshot("2026-10-22.md", "快照一")
        self.snapshot("pre-restore-2026-10-22-120000.md", "现场旧照")
        r = self.ctl("prune", "--keep", "1", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.snaps / "pre-restore-2026-10-22-120000.md").exists())
        self.assertIn("手动删除", r.stdout)


# --------------------------------------------------------------------------- #
# P0-10：enabled 白名单
# --------------------------------------------------------------------------- #
class EnabledWhitelistCase(Base):
    def test_illegal_enabled_does_not_inject(self):
        self.panel_text(enabled="maybe")
        proc = self.inject()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip bad-enabled", self.logtext())
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("enabled 取值不合法", r.stdout)

    def test_whitelisted_off_still_skips(self):
        self.panel_text(enabled="no")
        proc = self.inject()
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip disabled", self.logtext())
        self.assertEqual(self.ctl("check").returncode, 0)

    def test_impact_marks_illegal_enabled_ineffective(self):
        base = self.root / "home"
        self.hook_ready(base)
        self.panel_text(enabled="maybe")
        data = json.loads(self.ctl("impact", "--json", extra_env={"HERMES_HOME": base}).stdout)
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertIn("enabled 取值不合法", data["sessions"][0]["reason"])


# --------------------------------------------------------------------------- #
# 低优先：scope 只删行首标签 / 锁与快照目录类型 / snapshot glob
# --------------------------------------------------------------------------- #
class LowPriorityCase(Base):
    def test_scope_only_strips_leading_tag(self):
        self.panel_text("1. 正文里提到 [scope: session:AAA] 这个词")
        r = self.ctl("scope", "1", "session:BBB")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = self.panel.read_text(encoding="utf-8")
        self.assertIn("1. [scope: session:BBB] 正文里提到 [scope: session:AAA] 这个词", text)

    def test_lock_path_is_directory_exits_friendly(self):
        self.panel_text()
        (self.dir / "panel.md.lock").mkdir()
        r = self.ctl("scope", "1", "all")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("面板锁", r.stdout + r.stderr)
        self.assertNotIn("Traceback", r.stderr)

    def test_snaps_path_is_file_exits_friendly(self):
        self.panel_text()
        self.snaps.write_text("我是文件", encoding="utf-8")
        snapshot = self.ctl("snapshot")
        self.assertEqual(snapshot.returncode, 2, snapshot.stdout + snapshot.stderr)
        self.assertIn("快照目录", snapshot.stdout + snapshot.stderr)
        self.assertNotIn("Traceback", snapshot.stderr)
        restore = self.ctl("restore", "latest")
        self.assertEqual(restore.returncode, 2, restore.stdout + restore.stderr)
        self.assertNotIn("Traceback", restore.stderr)

    def test_snapshot_glob_skips_directories(self):
        self.panel_text("现场")
        self.snapshot("2026-10-22.md", "真快照")
        (self.snaps / "2026-10-23.md").mkdir()      # 同名目录：不是快照
        r = self.ctl("prune", "--keep", "1", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.snaps / "2026-10-23.md").is_dir())
        self.assertTrue((self.snaps / "2026-10-22.md").exists())


if __name__ == "__main__":
    unittest.main()
