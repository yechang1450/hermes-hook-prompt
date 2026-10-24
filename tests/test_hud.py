"""tests/test_hud.py — 纯标准库单元测试（python -m unittest discover -s tests）。

每个用例把三个脚本复制到临时目录再跑，避免碰到仓库里真实的 panel.md / hud.log。
"""
import json
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
SCRIPTS = ["hud_inject.py", "hudctl.py", "hud_report.py", "hud_panel.py", "panel.example.md"]
# 这三个变量若被外部环境污染（如同 shell 里跑过隔离实验），测试会读写错面板
ENV_KEYS = ("HUD_PANEL", "HUD_LOG", "HUD_SNAPS", "HUD_STATE")


class HudCase(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hud-test-"))
        for name in SCRIPTS:
            shutil.copy2(REPO / name, self.dir / name)
        self.panel = self.dir / "panel.md"
        self.log = self.dir / "hud.log"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    # ---- helpers -------------------------------------------------
    @staticmethod
    def base_env(**extra):
        env = {k: v for k, v in os.environ.items() if k not in ENV_KEYS}
        env.update(extra)
        return env

    def write_panel(self, body="正文", session="any", expires="2099-01-01T00:00:00+08:00",
                    title="测试面板", enabled="true", prefix=""):
        self.panel.write_text(
            f"{prefix}session_id: {session}\n"
            f"expires_at: {expires}\n"
            f"title: {title}\n"
            f"enabled: {enabled}\n\n{body}\n",
            encoding="utf-8",
        )

    def inject_json(self, payload):
        env = self.base_env(HUD_PANEL=str(self.panel), HUD_LOG=str(self.log))
        out = subprocess.run([sys.executable, str(self.dir / "hud_inject.py")], input=payload,
                             capture_output=True, text=True, encoding="utf-8", errors="replace",
                             env=env, timeout=180)
        return out.returncode, out.stdout.strip()

    def inject(self, session="s1", profile="default"):
        env = self.base_env(HUD_PANEL=str(self.panel), HUD_LOG=str(self.log))
        out = subprocess.run(
            [sys.executable, str(self.dir / "hud_inject.py")],
            input=json.dumps({"session_id": session, "profile": profile}),
            capture_output=True, text=True, env=env, timeout=60,
        )
        return out.returncode, out.stdout.strip()

    @staticmethod
    def context(stdout):
        return json.loads(stdout)["context"] if stdout else ""

    def write_snapshot(self, root, name, body):
        """在临时快照目录里造一份指定命名的快照（restore/prune 用固定文件名才可复现）。"""
        snaps = Path(root)
        snaps.mkdir(parents=True, exist_ok=True)
        (snaps / name).write_text(
            "session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
            f"title: 快照 {name}\nenabled: true\n\n{body}\n",
            encoding="utf-8",
        )

    def history_snaps(self):
        """三天四份快照：同日既有 YYYY-MM-DD.md 也有带时间的。"""
        snaps = self.dir / "snaps"
        self.write_snapshot(snaps, "2026-10-23.md", "昨天版本")
        self.write_snapshot(snaps, "2026-10-24.md", "早上版本")
        self.write_snapshot(snaps, "2026-10-24-101500.md", "上午版本")
        self.write_snapshot(snaps, "2026-10-24-194500.md", "晚上版本")
        return self.base_env(HUD_SNAPS=str(snaps), HUD_PANEL=str(self.panel))

    # ---- 注入行为 ------------------------------------------------
    def test_inject_any_session(self):
        self.write_panel(body="第一条\n第二条")
        rc, out = self.inject()
        self.assertEqual(rc, 0)
        ctx = self.context(out)
        self.assertIn("测试面板", ctx)
        self.assertIn("第一条", ctx)

    def test_disabled(self):
        self.write_panel(enabled="false")
        self.assertEqual(self.inject()[1], "")

    def test_expired(self):
        self.write_panel(expires="2020-01-01T00:00:00+08:00")
        self.assertEqual(self.inject()[1], "")
        self.assertIn("skip expired", self.log.read_text(encoding="utf-8"))

    def test_other_session(self):
        self.write_panel(session="other")
        self.assertEqual(self.inject(session="s1")[1], "")
        self.assertIn("skip other-session", self.log.read_text(encoding="utf-8"))

    def test_zulu_expiry_accepted(self):
        self.write_panel(expires="2099-01-01T00:00:00Z")
        self.assertIn("测试面板", self.context(self.inject()[1]))

    def test_bom_is_tolerated(self):
        self.write_panel(body="带 BOM", prefix="\ufeff")
        self.assertIn("带 BOM", self.context(self.inject()[1]))

    def test_missing_panel_is_silent(self):
        self.assertEqual(self.inject()[1], "")
        self.assertIn("skip no-panel", self.log.read_text(encoding="utf-8"))

    def test_truncation(self):
        self.write_panel(body="字" * 5000)
        ctx = self.context(self.inject()[1])
        self.assertLessEqual(len(ctx), 1700)
        self.assertIn("面板过长", ctx)

    def test_log_rotation(self):
        self.log.write_bytes(b"x" * (1_000_001))
        self.write_panel()
        self.inject()
        self.assertTrue((self.dir / "hud.log.1").exists())

    # ---- hudctl ---------------------------------------------------
    def run_ctl(self, *args, env=None):
        env = env or self.base_env()
        return subprocess.run([sys.executable, str(self.dir / "hudctl.py"), *args],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              cwd=self.dir, timeout=60, env=env)

    def test_ctl_off_on(self):
        self.write_panel()
        self.run_ctl("off")
        self.assertEqual(self.inject()[1], "")
        self.run_ctl("on")
        self.assertIn("测试面板", self.context(self.inject()[1]))

    def test_ctl_set_and_status(self):
        self.write_panel()
        body = self.dir / "body.md"
        body.write_text("新正文", encoding="utf-8")
        self.run_ctl("set", "--ttl", "2h", "--body", str(body), "--session", "any")
        out = self.run_ctl("status").stdout
        self.assertIn("新正文", self.panel.read_text(encoding="utf-8"))
        self.assertIn("范围：any", out)

    def test_ctl_restore_rejects_path_traversal(self):
        self.write_panel()
        r = self.run_ctl("restore", "../../etc/passwd")
        self.assertNotEqual(r.returncode, 0)
        # stderr 在极端代码页下可能被转义成 \uXXXX，所以断言稳定的 ASCII 片段
        self.assertIn("YYYY-MM-DD", (r.stdout or "") + (r.stderr or ""))

    def test_ctl_creates_panel_from_example(self):
        self.run_ctl("status")  # 面板不存在 → 自动从样例复制
        self.assertTrue(self.panel.exists())
        self.assertIn("expires_at:", self.panel.read_text(encoding="utf-8"))

    def test_ctl_check_ok(self):
        self.write_panel()
        r = self.run_ctl("check")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("✓", r.stdout)

    def test_ctl_check_reports_problems(self):
        self.panel.write_text("session_id: any\n\n只有正文\n", encoding="utf-8")
        r = self.run_ctl("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("缺少元数据", r.stdout)


    def test_inline_comment_stripped(self):
        self.panel.write_text(
            "session_id: any  # 注释\nexpires_at: 2099-01-01T00:00:00+08:00  # 到点失效\n"
            "title: t\nenabled: true\n\n正文\n", encoding="utf-8")
        self.assertIn("正文", self.context(self.inject()[1]))

    def test_meta_after_leading_blank_lines(self):
        self.panel.write_text("\n\n\nsession_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
                              "title: t\nenabled: true\n\n正文\n", encoding="utf-8")
        self.assertIn("正文", self.context(self.inject()[1]))

    def test_bad_payload_is_silent(self):
        self.write_panel()
        rc, out = self.inject_json("[]")
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("skip bad-payload", self.log.read_text(encoding="utf-8"))

    def test_invalid_utf8_is_silent(self):
        self.panel.write_bytes(b"session_id: any\n\nexpires_at: 2099-01-01T00:00:00+08:00\n\xff\n")
        rc, out = self.inject()
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("skip unreadable-panel", self.log.read_text(encoding="utf-8"))

    def test_write_rejects_newline_in_meta(self):
        self.write_panel()
        r = self.run_ctl("set", "--ttl", "1h", "--title", "a\nb")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("换行", r.stdout + r.stderr)

    def test_check_flags_oversized_block(self):
        self.panel.write_text("session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
                              "title: t\nenabled: true\n\n" + "字" * 1590 + "\n", encoding="utf-8")
        r = self.run_ctl("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("1600", r.stdout)


    def test_sessions_across_profiles(self):
        home = self.dir / "home"
        (home / "profiles" / "proj-9").mkdir(parents=True)
        for db, sid, title in ((home / "state.db", "20260101_000000_aaaaaa", "默认会话"),
                               (home / "profiles" / "proj-9" / "state.db", "20260102_000000_bbbbbb", "九号会话")):
            con = sqlite3.connect(db)
            con.execute("create table sessions (id text primary key, title text, profile_name text,"
                        " last_activity_at real, message_count int, archived int default 0, hidden int default 0)")
            con.execute("insert into sessions values (?,?,?,?,?,0,0)", (sid, title, "x", 1760000000, 1))
            con.commit(); con.close()
        env = self.base_env(HERMES_HOME=str(home))
        out = self.run_ctl("sessions", env=env).stdout
        self.assertIn("默认会话", out)
        self.assertIn("九号会话", out)
        self.assertIn("proj-9", out)

    def test_report_reads_rotated_log(self):
        self.write_panel(enabled="false")
        self.log.with_name(self.log.name + ".1").write_text(
            "2026-01-01T00:00:00+08:00 inject session=old profile=work chars=10\n", encoding="utf-8")
        self.inject()
        env = self.base_env(HUD_LOG=str(self.log), HUD_PANEL=str(self.panel))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180)
        self.assertIn("work", r.stdout)
        self.assertIn("总行数 2", r.stdout)


    def test_ctl_honours_panel_env(self):
        alt = self.dir / "alt.md"
        alt.write_text("session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\ntitle: alt\n"
                       "enabled: true\n\nALT\n", encoding="utf-8")
        env = self.base_env(HUD_PANEL=str(alt))
        out = self.run_ctl("status", env=env).stdout
        self.assertIn("alt.md", out)

    def test_ctl_rejects_bad_ttl(self):
        self.write_panel()
        for ttl in ("0h", "999999999999d"):
            r = self.run_ctl("set", "--ttl", ttl, "--body", str(self.dir / "panel.md"))
            self.assertNotEqual(r.returncode, 0, ttl)
            self.assertIn("TTL", r.stdout + r.stderr)

    def test_prune_rejects_non_positive_keep(self):
        self.write_panel()
        self.run_ctl("snapshot")
        r = self.run_ctl("prune", "--keep", "0")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("--keep", r.stdout + r.stderr)
        self.assertTrue(any((self.dir / "snapshots").glob("*.md")))

    def test_restore_latest_ignores_stray_md(self):
        self.write_panel(body="当日快照")
        self.run_ctl("snapshot")
        (self.dir / "snapshots" / "zz-notes.md").write_text(
            "session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\ntitle: stray\nenabled: true\n\n乱入\n",
            encoding="utf-8")
        self.write_panel(body="被改过")
        r = self.run_ctl("restore", "latest")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("当日快照", self.panel.read_text(encoding="utf-8"))

    def test_symlink_panel_is_refused(self):
        target = self.dir / "real.md"
        target.write_text("session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\ntitle: t\nenabled: true\n\n正文\n", encoding="utf-8")
        try:
            os.symlink(target, self.panel)
        except (OSError, NotImplementedError):
            self.skipTest("当前系统不允许创建符号链接")
        rc, out = self.inject()
        self.assertEqual((rc, out), (0, ""))
        self.assertIn("skip unsafe-panel", self.log.read_text(encoding="utf-8"))


    def test_check_flags_duplicate_meta(self):
        self.panel.write_text("session_id: any\nsession_id: other\nexpires_at: 2099-01-01T00:00:00+08:00\n"
                              "title: t\nenabled: true\n\n正文\n", encoding="utf-8")
        r = self.run_ctl("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("重复元数据", r.stdout)

    def test_set_missing_body_file_is_friendly(self):
        self.write_panel()
        r = self.run_ctl("set", "--ttl", "1h", "--body", str(self.dir / "nope.md"))
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("正文文件不存在", r.stdout + r.stderr)

    def test_utc_expiry_shown_in_local_time(self):
        self.write_panel(expires="2099-01-01T00:00:00Z")
        ctx = self.context(self.inject()[1])
        self.assertIn("01-01 08:00", ctx.splitlines()[0])


    def test_execution_hint_by_profile(self):
        self.write_panel()
        ctx_default = self.context(self.inject(profile="default")[1])
        self.assertIn("default bot：优先用 Hermes 官方机制", ctx_default)
        ctx_bot = self.context(self.inject(profile="proj-80")[1])
        self.assertIn("proj-80 bot：优先用 OTUI", ctx_bot)


    def test_selfcheck_warning_when_state_missing(self):
        self.write_panel()
        ctx = self.context(self.inject()[1])
        self.assertIn("⚠", ctx)  # 临时目录没有 state/selfcheck.json

    def test_ctl_sessions_and_use(self):
        home = self.dir / "home"
        home.mkdir()
        con = sqlite3.connect(home / "state.db")
        con.execute("create table sessions (id text primary key, title text, profile_name text,"
                    " last_activity_at real, message_count int, archived int default 0, hidden int default 0)")
        con.execute("insert into sessions values ('20260101_000000_aaaaaa', '测试会话', 'default', 1760000000, 3, 0, 0)")
        con.commit()
        con.close()
        env = self.base_env(HERMES_HOME=str(home))
        out = self.run_ctl("sessions", env=env).stdout
        self.assertIn("测试会话", out)
        self.run_ctl("use", "测试会话", "--ttl", "1h", env=env)
        self.assertIn("session_id: 20260101_000000_aaaaaa", self.panel.read_text(encoding="utf-8"))

    def test_ctl_snapshot_restore(self):
        self.write_panel(body="快照正文")
        self.run_ctl("snapshot")
        self.write_panel(body="改过的")
        r = self.run_ctl("restore", "latest", "--ttl", "1h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("快照正文", self.panel.read_text(encoding="utf-8"))

    def test_snapshot_same_day_keeps_both(self):
        snaps = self.dir / "snapshots"
        self.write_panel(body="第一份")
        r1 = self.run_ctl("snapshot")
        self.assertEqual(r1.returncode, 0, r1.stdout + r1.stderr)
        self.write_panel(body="第二份")
        r2 = self.run_ctl("snapshot")
        self.assertEqual(r2.returncode, 0, r2.stdout + r2.stderr)
        names = {p.name for p in snaps.glob("*.md")}
        plain = [n for n in names if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", n)]
        stamped = [n for n in names if re.fullmatch(r"\d{4}-\d{2}-\d{2}-\d{6}\.md", n)]
        self.assertEqual(len(plain), 1, names)
        self.assertEqual(len(stamped), 1, names)
        self.assertIn("第一份", (snaps / plain[0]).read_text(encoding="utf-8"))
        self.assertIn("第二份", (snaps / stamped[0]).read_text(encoding="utf-8"))

    def test_snapshot_repeats_never_overwrite(self):
        snaps = self.dir / "snapshots"
        bodies = [f"第 {i} 份" for i in range(1, 6)]
        for body in bodies:
            self.write_panel(body=body)
            r = self.run_ctl("snapshot")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        files = list(snaps.glob("*.md"))
        self.assertEqual(len(files), len(bodies), [p.name for p in files])
        text = "\n".join(p.read_text(encoding="utf-8") for p in files)
        for body in bodies:
            self.assertIn(body, text)
        self.assertFalse(list(snaps.glob("*.tmp")), "原子写的临时文件没清干净")

    def test_restore_latest_prefers_newest_same_day(self):
        env = self.history_snaps()
        self.write_panel(body="当前版本")
        r = self.run_ctl("restore", "latest", "--ttl", "1h", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("晚上版本", self.panel.read_text(encoding="utf-8"))

    def test_restore_date_picks_latest_of_that_day(self):
        env = self.history_snaps()
        self.write_panel(body="当前版本")
        r = self.run_ctl("restore", "2026-10-24", "--ttl", "1h", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("晚上版本", self.panel.read_text(encoding="utf-8"))

    def test_restore_date_works_with_plain_name_only(self):
        env = self.history_snaps()
        self.write_panel(body="当前版本")
        r = self.run_ctl("restore", "2026-10-23", "--ttl", "1h", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("昨天版本", self.panel.read_text(encoding="utf-8"))

    def test_restore_missing_date_is_rejected(self):
        env = self.history_snaps()
        r = self.run_ctl("restore", "2026-01-01", env=env)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("没有这份快照", r.stdout + r.stderr)

    def test_prune_keeps_newest_across_naming_schemes(self):
        snaps = self.dir / "snaps"
        for name in ("2026-10-22.md", "2026-10-23.md", "2026-10-24.md",
                     "2026-10-24-083015.md", "2026-10-24-194500.md"):
            self.write_snapshot(snaps, name, name)
        env = self.base_env(HUD_SNAPS=str(snaps), HUD_PANEL=str(self.panel))
        r = self.run_ctl("prune", "--keep", "2", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({p.name for p in snaps.glob("*.md")},
                         {"2026-10-24-083015.md", "2026-10-24-194500.md"})

    def test_prune_leaves_non_snapshot_md(self):
        snaps = self.dir / "snaps"
        self.write_snapshot(snaps, "2026-10-24-101010.md", "正经快照")
        (snaps / "notes.md").write_text("随手记", encoding="utf-8")
        env = self.base_env(HUD_SNAPS=str(snaps), HUD_PANEL=str(self.panel))
        r = self.run_ctl("prune", "--keep", "1", env=env)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((snaps / "notes.md").exists())
        self.assertTrue((snaps / "2026-10-24-101010.md").exists())

    # ---- hud_report ------------------------------------------------
    def test_report_counts(self):
        self.write_panel()
        self.inject(profile="default")
        self.inject(profile="default")
        self.write_panel(enabled="false")
        self.inject(profile="default")
        env = self.base_env(HUD_LOG=str(self.log), HUD_PANEL=str(self.panel))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("default", r.stdout)
        self.assertIn("inject", self.log.read_text(encoding="utf-8"))

    def test_report_bad_since(self):
        env = self.base_env(HUD_LOG=str(self.log))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py"), "--since", "10-22"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("YYYY-MM-DD", r.stdout)

    def test_report_missing_log(self):
        env = self.base_env(HUD_LOG=str(self.dir / "absent.log"))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py")],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=60)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
