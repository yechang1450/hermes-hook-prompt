"""tests/test_hud.py — 纯标准库单元测试（python -m unittest discover -s tests）。

每个用例把三个脚本复制到临时目录再跑，避免碰到仓库里真实的 panel.md / hud.log。
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SCRIPTS = ["hud_inject.py", "hudctl.py", "hud_report.py", "panel.example.md"]


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
    def write_panel(self, body="正文", session="any", expires="2099-01-01T00:00:00+08:00",
                    title="测试面板", enabled="true", prefix=""):
        self.panel.write_text(
            f"{prefix}session_id: {session}\n"
            f"expires_at: {expires}\n"
            f"title: {title}\n"
            f"enabled: {enabled}\n\n{body}\n",
            encoding="utf-8",
        )

    def inject(self, session="s1", profile="default"):
        env = dict(os.environ, HUD_PANEL=str(self.panel), HUD_LOG=str(self.log))
        out = subprocess.run(
            [sys.executable, str(self.dir / "hud_inject.py")],
            input=json.dumps({"session_id": session, "profile": profile}),
            capture_output=True, text=True, env=env, timeout=60,
        )
        return out.returncode, out.stdout.strip()

    @staticmethod
    def context(stdout):
        return json.loads(stdout)["context"] if stdout else ""

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
    def run_ctl(self, *args):
        return subprocess.run([sys.executable, str(self.dir / "hudctl.py"), *args],
                              capture_output=True, text=True, cwd=self.dir, timeout=60)

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
        self.assertIn("日期格式", r.stdout + r.stderr)

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

    def test_ctl_snapshot_restore(self):
        self.write_panel(body="快照正文")
        self.run_ctl("snapshot")
        self.run_ctl("set", "--ttl", "1h", "--body", os.devnull) if False else None
        self.write_panel(body="改过的")
        r = self.run_ctl("restore", "latest", "--ttl", "1h")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("快照正文", self.panel.read_text(encoding="utf-8"))

    # ---- hud_report ------------------------------------------------
    def test_report_counts(self):
        self.write_panel()
        self.inject(profile="default")
        self.inject(profile="default")
        self.write_panel(enabled="false")
        self.inject(profile="default")
        env = dict(os.environ, HUD_LOG=str(self.log), HUD_PANEL=str(self.panel))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 0)
        self.assertIn("default", r.stdout)
        self.assertIn("inject", self.log.read_text(encoding="utf-8"))

    def test_report_bad_since(self):
        env = dict(os.environ, HUD_LOG=str(self.log))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py"), "--since", "10-22"],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 2)
        self.assertIn("YYYY-MM-DD", r.stdout)

    def test_report_missing_log(self):
        env = dict(os.environ, HUD_LOG=str(self.dir / "absent.log"))
        r = subprocess.run([sys.executable, str(self.dir / "hud_report.py")],
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual(r.returncode, 1)


if __name__ == "__main__":
    unittest.main()
