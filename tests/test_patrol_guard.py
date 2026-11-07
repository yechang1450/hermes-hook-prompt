"""tests/test_patrol_guard.py — Phase 0 巡查规则纯函数 + dry-run CLI 测试。

不注册 hook、不读真实 config；全部用合成 payload 和临时 git 仓库。
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
sys.path.insert(0, str(REPO))

# 假 token 运行期拼装：仓库是公开的，静态 secret scanner 会把完整字面量报成
# CRITICAL（2026-11-07 巡检两轮误报同一夹具）。拼装后扫描器拿不到整串，
# 而 _SECRET_RE 的 ghp_ 分支（8 位以上）与 token= 分支照旧可验。
_FAKE_TOKEN = "ghp" + "_" + "abcdefgh12345678"

import patrol_guard as pg  # noqa: E402


class RuleCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rules = pg.load_rules()

    def decide(self, tool_name, args):
        return pg.evaluate_pre_tool(tool_name, args, self.rules)

    def assert_action(self, action, tool_name, args):
        decision = self.decide(tool_name, args)
        self.assertIsNotNone(decision, f"{tool_name} {args} should hit a rule")
        assert decision is not None
        self.assertEqual(decision.action, action, f"{decision.rule_id}: {decision.message}")
        return decision

    def assert_clean(self, tool_name, args):
        self.assertIsNone(self.decide(tool_name, args), f"{tool_name} {args} should be allowed")

    # ---- 红线 3：force push / 重写历史 --------------------------------
    def test_force_push_variants(self):
        for command in (
            "git push --force origin main",
            "git push -f origin main",
            "git push origin +main",
            "git push --force-with-lease origin main",
            "git -C C:/repo push --force",
        ):
            with self.subTest(command=command):
                self.assert_action("approve", "terminal", {"command": command})

    def test_normal_push_allowed(self):
        for command in (
            "git push origin master",
            "git push --follow-tags",
            "git status --porcelain",
            "git log --oneline -3",
        ):
            with self.subTest(command=command):
                self.assert_clean("terminal", {"command": command})

    def test_filter_repo_needs_approval(self):
        self.assert_action("approve", "terminal", {"command": "git filter-branch --force --all"})
        self.assert_action("approve", "terminal", {"command": "git filter-repo --path secrets"})

    def test_execute_code_force_push(self):
        self.assert_action(
            "approve",
            "execute_code",
            {"code": "subprocess.run(['git', 'push', '--force'], check=True)"},
        )

    # ---- 红线 1：重启/停止 Hermes ------------------------------------
    def test_restart_hermes(self):
        for command in (
            "hermes gateway restart",
            "hermes gateway stop",
            "taskkill /F /IM Hermes.exe",
            "Stop-Process -Name Hermes",
            "pkill -f hermes",
        ):
            with self.subTest(command=command):
                self.assert_action("approve", "terminal", {"command": command})

    # ---- 红线 2：SOUL / 凭据 / config，删数据 -------------------------
    def test_protected_paths(self):
        for path in (
            "C:/repo/config.yaml",
            "C:/repo/SOUL.md",
            "C:/repo/.env",
            "C:/repo/auth.json",
            "C:/repo/deploy.pem",
            "C:/repo/key.key",
        ):
            with self.subTest(path=path):
                self.assert_action("approve", "write_file", {"path": path})

    def test_protected_paths_do_not_scan_content(self):
        self.assert_clean("write_file", {"path": "C:/repo/README.md", "content": "config.yaml"})

    def test_config_command(self):
        for command in (
            "hermes config set hooks.pre_tool_call '[]'",
            "echo 'x: 1' > config.yaml",
            "Set-Content C:/repo/config.yaml 'x'",
        ):
            with self.subTest(command=command):
                self.assert_action("approve", "terminal", {"command": command})

    def test_delete_data_blocked(self):
        for command in (
            "rm -rf memories",
            "rm -rf C:/Users/x/AppData/Local/hermes/memories",
            "Remove-Item -Recurse -Force C:/Users/x/AppData/Local/hermes/sessions",
            "rm -f state.db",
            "Remove-Item backup-2026.bundle",
        ):
            with self.subTest(command=command):
                self.assert_action("block", "terminal", {"command": command})

    def test_delete_unrelated_allowed(self):
        for command in (
            "rm -rf temp/build-cache",
            "rm -f notes.md",
            "Remove-Item C:/workspace/temp/foo.log",
        ):
            with self.subTest(command=command):
                self.assert_clean("terminal", {"command": command})

    def test_memory_modification_allowed(self):
        # 用户口径：记忆可以改，不可以删。
        self.assert_clean("write_file", {"path": "C:/Users/x/AppData/Local/hermes/memories/notes.md"})

    # ---- 红线 3：公开 / 发布 -----------------------------------------
    def test_public_and_release(self):
        for command in (
            "gh repo edit yechang1450/x --visibility public",
            "gh release create v1.0.0",
            "npm publish",
            "cargo publish",
            "twine upload dist/*",
            "docker push repo/image:tag",
        ):
            with self.subTest(command=command):
                self.assert_action("approve", "terminal", {"command": command})

    # ---- 红线 5：磁盘/设备 -------------------------------------------
    def test_disk_device_blocked(self):
        for command in (
            "diskpart",
            "format D: /fs:NTFS",
            "mkfs.ext4 /dev/sda1",
            "bcdedit /set safeboot minimal",
            "pnputil /disable-device",
            "Clear-Disk -Number 1",
        ):
            with self.subTest(command=command):
                self.assert_action("block", "terminal", {"command": command})

    def test_cargo_fmt_allowed(self):
        self.assert_clean("terminal", {"command": "cargo fmt --all"})

    # ---- 红线 6：广谱/定点杀进程 -------------------------------------
    def test_broad_kill_blocked(self):
        for command in (
            "taskkill /F /IM python.exe",
            "Stop-Process -Name node -Force",
            "pkill -f python",
        ):
            with self.subTest(command=command):
                self.assert_action("block", "terminal", {"command": command})

    def test_pid_kill_needs_approval(self):
        for command in (
            "taskkill /PID 1234 /F",
            "Stop-Process -Id 1234",
            "kill 1234",
        ):
            with self.subTest(command=command):
                self.assert_action("approve", "terminal", {"command": command})

    # ---- 硬约束 7：外部安装 ------------------------------------------
    def test_external_install_needs_approval(self):
        for command in (
            "pip install requests",
            "npm install -g typescript",
            "winget install Git.Git",
            "cargo install ripgrep",
        ):
            with self.subTest(command=command):
                self.assert_action("approve", "terminal", {"command": command})

    def test_local_npm_install_allowed(self):
        self.assert_clean("terminal", {"command": "npm install"})
        self.assert_clean("terminal", {"command": "npm ci"})

    # ---- 优先级 ------------------------------------------------------
    def test_block_wins_over_approve(self):
        decision = self.assert_action("block", "terminal", {"command": "diskpart && git push --force"})
        assert decision is not None
        self.assertEqual(decision.rule_id, "RL-R5-DISK-DEVICE")

    def test_evidence_redacts_secret(self):
        decision = self.assert_action(
            "approve",
            "terminal",
            {"command": f"git push --force https://x-access-token:{_FAKE_TOKEN}"},
        )
        self.assertNotIn(_FAKE_TOKEN, decision.message)
        self.assertIn("[redacted]", pg._compact(f"token={_FAKE_TOKEN}"))


class PreVerifyCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="patrol-git-"))
        self.repo = self.tmp / "demo-repo"
        self.repo.mkdir()
        self._git("init", "-q", str(self.repo))
        self._git("-C", str(self.repo), "config", "user.email", "test@example.com")
        self._git("-C", str(self.repo), "config", "user.name", "Test")
        (self.repo / "a.txt").write_text("one\n", encoding="utf-8")
        self._git("-C", str(self.repo), "add", "a.txt")
        self._git("-C", str(self.repo), "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    @staticmethod
    def _git(*args):
        return subprocess.run(["git", *args], capture_output=True, text=True, timeout=30)

    def test_dirty_repo_nudges(self):
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        message = pg.repo_sync_message([str(self.repo / "a.txt")], roots=[self.tmp])
        self.assertIsNotNone(message)
        assert message is not None
        self.assertIn("硬约束 8", message)
        self.assertIn("未提交", message)
        self.assertIn("demo-repo", message)

    def test_clean_repo_is_quiet(self):
        self.assertIsNone(pg.repo_sync_message([str(self.repo / "a.txt")], roots=[self.tmp]))

    def test_outside_roots_ignored(self):
        (self.repo / "a.txt").write_text("two\n", encoding="utf-8")
        other = self.tmp / "elsewhere"
        other.mkdir()
        self.assertIsNone(pg.repo_sync_message([str(self.repo / "a.txt")], roots=[other]))


class CliCase(unittest.TestCase):
    def _run(self, payload, *args, env=None):
        proc = subprocess.run(
            [sys.executable, str(REPO / "patrol_guard.py"), *args],
            input=json.dumps(payload, ensure_ascii=False),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(REPO),
            env={**os.environ, **(env or {})},
            timeout=60,
        )
        return proc

    def test_cli_force_push(self):
        proc = self._run(
            {"tool_name": "terminal", "tool_input": {"command": "git push --force"}},
            "--event", "pre_tool_call", "--explain",
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["action"], "approve")
        self.assertIn("RL-R3-FORCE-PUSH", proc.stderr)

    def test_cli_survives_non_utf8_console(self):
        """stdout 走 ANSI 代码页（英文 Windows 的 cp1252）时，中文消息不得吞掉决策。

        回归 2026-11-07 windows-latest CI：UnicodeEncodeError 被 fail-open 吞掉，
        stdout 变成 {}，force push 的 approve 静默丢失。
        """
        env = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
        proc = self._run(
            {"tool_name": "terminal", "tool_input": {"command": "git push --force"}},
            "--event", "pre_tool_call", "--explain",
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        data = json.loads(proc.stdout)
        self.assertEqual(data["action"], "approve")
        self.assertIn("红线 3", data["message"])  # 中文按 UTF-8 原样往返
        self.assertIn("RL-R3-FORCE-PUSH", proc.stderr)

    def test_cli_reads_utf8_payload_on_non_utf8_console(self):
        """payload 里的中文（git commit 说明等）也要按 UTF-8 读进来，不能变成乱码。"""
        env = {"PYTHONIOENCODING": "cp1252", "PYTHONUTF8": "0"}
        proc = self._run(
            {"tool_name": "terminal", "tool_input": {"command": "git push --force # 中文备注"}},
            "--event", "pre_tool_call",
            env=env,
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout)["action"], "approve")

    def test_cli_clean(self):
        proc = self._run(
            {"tool_name": "terminal", "tool_input": {"command": "git push origin master"}},
            "--event", "pre_tool_call",
        )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {})

    def test_cli_bad_rules_fail_open(self):
        with tempfile.TemporaryDirectory() as tmp:
            bad = Path(tmp) / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            proc = self._run(
                {"tool_name": "terminal", "tool_input": {"command": "git push --force"}},
                "--event", "pre_tool_call", "--rules", str(bad),
            )
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(json.loads(proc.stdout), {})


if __name__ == "__main__":
    unittest.main()
