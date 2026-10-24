"""selfcheck.py 的自测：机制齐全时 0，注入点缺失时 2。"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

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
        self.panel.write_text("session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
                              "title: t\nenabled: true\n\n正文\n", encoding="utf-8")
        self.state = self.home / "state"

    def tearDown(self):
        self.td.cleanup()

    def run_check(self):
        env = dict(os.environ, HERMES_HOME=str(self.home), HUD_STATE=str(self.state),
                   HUD_PANEL=str(self.panel), HUD_PROFILE="default")
        return subprocess.run([sys.executable, str(REPO / "selfcheck.py")],
                              capture_output=True, text=True, encoding="utf-8", errors="replace",
                              env=env, timeout=180)

    def test_all_green(self):
        r = self.run_check()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads((self.state / "selfcheck.json").read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])
        self.assertTrue(data["fingerprint"])

    def test_missing_injection_point(self):
        (self.home / "hermes-agent" / "agent" / "turn_context.py").write_text("nothing\n", encoding="utf-8")
        r = self.run_check()
        self.assertEqual(r.returncode, 2)
        data = json.loads((self.state / "selfcheck.json").read_text(encoding="utf-8"))
        self.assertFalse(data["ok"])
        self.assertTrue(any(not c["ok"] for c in data["checks"]))


if __name__ == "__main__":
    unittest.main()
