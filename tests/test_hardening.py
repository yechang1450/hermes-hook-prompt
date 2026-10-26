"""13 项必修的回归测试（自检假红 / scope fail-closed / 空 session / restore 备份 /
prune 默认不删 / on 续期 / 未知键 / 续行 scope / set 校验 / 状态原子写 / 内容指纹 /
面板大小与并发锁 / parse_scope 与 use 精确匹配）。

每个用例把脚本复制到临时目录再跑；环境里的 HUD_* / HERMES_HOME 先清掉。
"""
from __future__ import annotations

import argparse
import contextlib
import importlib.util
import io
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
from unittest import mock

REPO = Path(__file__).resolve().parent.parent
PY = sys.executable
SCRIPTS = ("hud_inject.py", "hudctl.py", "hud_panel.py", "hud_report.py", "panel.example.md")
CLEAR = ("HUD_PANEL", "HUD_LOG", "HUD_SNAPS", "HUD_STATE", "HUD_PROFILE", "HERMES_HOME")


class Base(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="hud-hard-"))
        self.addCleanup(shutil.rmtree, self.dir, True)
        for name in SCRIPTS:
            shutil.copy2(REPO / name, self.dir / name)
        self.panel = self.dir / "panel.md"
        self.log = self.dir / "hud.log"
        self.snaps = self.dir / "snaps"
        self.state = self.dir / "state"

    # ---- 环境与夹具 -------------------------------------------------
    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in CLEAR}
        env["PYTHONIOENCODING"] = "utf-8"
        env.update(HUD_PANEL=str(self.panel), HUD_LOG=str(self.log),
                   HUD_SNAPS=str(self.snaps), HUD_STATE=str(self.state))
        env.update(extra)
        return env

    def panel_text(self, body="1. 正文", session="any",
                   expires="2099-01-01T00:00:00+08:00", title="测试面板",
                   enabled="true", extra_head=""):
        self.panel.write_text(
            f"session_id: {session}\nexpires_at: {expires}\ntitle: {title}\n"
            f"enabled: {enabled}\n{extra_head}\n{body}\n", encoding="utf-8")

    def inject(self, sid="AAA", profile="default", payload=None, extra_env=None):
        data = payload if payload is not None else {"session_id": sid, "profile": profile}
        proc = subprocess.run([PY, str(self.dir / "hud_inject.py")], input=json.dumps(data),
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**(extra_env or {})),
                              cwd=str(self.dir), timeout=120)
        return proc

    @staticmethod
    def ctx(proc) -> str:
        return json.loads(proc.stdout)["context"] if proc.stdout.strip() else ""

    def ctl(self, *args, extra_env=None, stdin_text=None):
        proc = subprocess.run([PY, str(self.dir / "hudctl.py"), *args], input=stdin_text,
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**(extra_env or {})),
                              cwd=str(self.dir), timeout=120)
        return proc

    def logtext(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def load_module(self, name: str):
        """在进程内加载脚本（为了打桩「写快照失败」这类不可控分支）。"""
        sys.path.insert(0, str(self.dir))
        self.addCleanup(lambda: sys.path.remove(str(self.dir)))
        saved = sys.modules.get("hud_panel")   # 别把临时目录里的副本留给别的用例

        def restore_hud_panel():
            if saved is not None:
                sys.modules["hud_panel"] = saved
            else:
                sys.modules.pop("hud_panel", None)

        self.addCleanup(restore_hud_panel)
        spec = importlib.util.spec_from_file_location(f"{name}_under_test", self.dir / f"{name}.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        mod.PANEL = self.panel
        mod.SNAPS = self.snaps
        mod.LOG = self.log
        return mod

    def make_db(self, path: Path, rows, source_col=True):
        """rows: [(id, title)]；可选带 source 列（impact 分类用）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        con = sqlite3.connect(path)
        if source_col:
            con.execute("create table sessions (id text primary key, title text, "
                        "last_activity_at real, message_count int, archived int default 0, "
                        "hidden int default 0, source text default 'chat')")
            con.executemany("insert into sessions values (?,?,?,?,0,0,'chat')",
                            [(sid, title, float(i), 1) for i, (sid, title) in enumerate(rows)])
        else:
            con.execute("create table sessions (id text primary key, title text, "
                        "last_activity_at real, message_count int, archived int default 0, hidden int default 0)")
            con.executemany("insert into sessions values (?,?,?,?,0,0)",
                            [(sid, title, float(i), 1) for i, (sid, title) in enumerate(rows)])
        con.commit()
        con.close()

    def hook_ready(self, home: Path, rows):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(
            "hooks:\n  pre_llm_call:\n    - command: python hud_inject.py\n", encoding="utf-8")
        (home / "shell-hooks-allowlist.json").write_text(json.dumps(
            {"approvals": [{"event": "pre_llm_call", "command": "python hud_inject.py"}]}),
            encoding="utf-8")
        self.make_db(home / "state.db", rows)


# --------------------------------------------------------------------------- #
# 2) scope fail-closed：缺 hud_panel / 围栏未闭合
# --------------------------------------------------------------------------- #
class DegeneratePanelCase(Base):
    def test_missing_hud_panel_module_skips_instead_of_injecting_raw(self):
        (self.dir / "hud_panel.py").unlink()
        self.panel_text("1. [scope: session:ZZZ] 只给 Z")
        proc = self.inject("AAA")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")   # 绝不原样注入（连 [scope:] 都不许漏）
        self.assertIn("degenerate-panel", self.logtext())

    def test_unclosed_fence_skips_whole_block(self):
        self.panel_text("1. [scope: session:AAA] 秘密\n```\n2. [scope: all] 泄漏点")
        proc = self.inject("BBB")
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip degenerate-panel", self.logtext())
        self.assertIn("unclosed-code-fence", self.logtext())

    def test_check_reports_unclosed_fence_as_format_error(self):
        self.panel_text("1. [scope: all] 真条目\n```")
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("围栏未闭合", r.stdout)

    def test_impact_marks_degenerate_panel_ineffective(self):
        home = self.dir / "home"
        self.hook_ready(home, [("AAA", "a")])
        self.panel_text("1. [scope: all] x\n```")
        r = self.ctl("impact", "--json", extra_env={"HERMES_HOME": str(home)})
        data = json.loads(r.stdout)
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertIn("无法安全解析", data["sessions"][0]["reason"])


# --------------------------------------------------------------------------- #
# 3) session_id 为空/缺省
# --------------------------------------------------------------------------- #
class EmptySessionCase(Base):
    def test_payload_without_session_id_is_skipped(self):
        self.panel_text()
        proc = self.inject(payload={"profile": "default"})
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip no-session", self.logtext())

    def test_payload_with_empty_session_id_is_skipped(self):
        self.panel_text()
        proc = self.inject(payload={"session_id": "", "profile": "default"})
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip no-session", self.logtext())

    def test_empty_panel_scope_is_skipped_and_check_errors(self):
        self.panel_text(session="")
        proc = self.inject()
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip empty-scope", self.logtext())
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("session_id 为空", r.stdout)

    def test_empty_scope_marks_every_session_ineffective(self):
        home = self.dir / "home"
        self.hook_ready(home, [("AAA", "a")])
        self.panel_text(session="")
        data = json.loads(self.ctl("impact", "--json", extra_env={"HERMES_HOME": str(home)}).stdout)
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertIn("session_id 为空", data["sessions"][0]["reason"])


# --------------------------------------------------------------------------- #
# 4) restore 前自动打快照；写不进就拒绝恢复
# --------------------------------------------------------------------------- #
class RestoreBackupCase(Base):
    def test_restore_takes_pre_restore_snapshot(self):
        self.panel_text("旧版正文")
        self.assertEqual(self.ctl("snapshot").returncode, 0)
        self.panel_text("当前正文")
        r = self.ctl("restore", "latest", "--ttl", "1h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("旧版正文", self.panel.read_text(encoding="utf-8"))
        pres = sorted(self.snaps.glob("pre-restore-*.md"))
        self.assertEqual(len(pres), 1, [p.name for p in self.snaps.glob("*.md")])
        self.assertIn("当前正文", pres[0].read_text(encoding="utf-8"))

    def test_pre_restore_snapshot_does_not_win_latest(self):
        self.panel_text("旧版正文")
        self.ctl("snapshot")
        self.panel_text("当前正文")
        self.ctl("restore", "latest", "--ttl", "1h")
        # 再恢复一次 latest：仍应取用户快照，而不是刚生成的 pre-restore
        self.panel_text("又被改过")
        r = self.ctl("restore", "latest", "--ttl", "1h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("旧版正文", self.panel.read_text(encoding="utf-8"))

    def test_restore_refuses_when_snapshot_cannot_be_written(self):
        self.panel_text("旧版正文")
        self.ctl("snapshot")
        self.panel_text("当前正文")
        mod = self.load_module("hudctl")

        def boom(_target):
            raise OSError("disk full")

        with mock.patch.object(mod, "_snapshot_to", boom):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    mod.cmd_restore(argparse.Namespace(date="latest", ttl="1h", session=None))
        self.assertEqual(ctx.exception.code, 2)
        self.assertIn("当前正文", self.panel.read_text(encoding="utf-8"))  # 没被覆盖

    def test_prune_ignores_pre_restore_snapshots(self):
        for name in ("2026-10-20.md", "2026-10-21.md", "2026-10-22.md"):
            self.snaps.mkdir(parents=True, exist_ok=True)
            (self.snaps / name).write_text(
                f"session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\ntitle: t\nenabled: true\n\n{name}\n",
                encoding="utf-8")
        self.snaps.mkdir(parents=True, exist_ok=True)
        (self.snaps / "pre-restore-2026-10-22-120000.md").write_text("现场\n", encoding="utf-8")
        r = self.ctl("prune", "--keep", "1", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.snaps / "pre-restore-2026-10-22-120000.md").exists())
        self.assertEqual({p.name for p in self.snaps.glob("2*.md")}, {"2026-10-22.md"})


# --------------------------------------------------------------------------- #
# 5) prune 默认只列清单
# --------------------------------------------------------------------------- #
class PruneCase(Base):
    def _snaps(self, names):
        self.snaps.mkdir(parents=True, exist_ok=True)
        for name in names:
            (self.snaps / name).write_text(
                f"session_id: any\nexpires_at: 2099-01-01T00:00:00+08:00\n"
                f"title: {name}\nenabled: true\n\n{name}\n", encoding="utf-8")

    def test_default_only_lists(self):
        names = ("2026-10-20.md", "2026-10-21.md", "2026-10-22.md")
        self._snaps(names)
        r = self.ctl("prune", "--keep", "1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(list(self.snaps.glob("*.md"))), 3)
        self.assertIn("--yes", r.stdout)
        self.assertIn("未删除", r.stdout)
        for name in names[:2]:          # 只列将被删的那两份
            self.assertIn(name, r.stdout)

    def test_yes_really_deletes(self):
        self._snaps(("2026-10-20.md", "2026-10-21.md", "2026-10-22.md"))
        r = self.ctl("prune", "--keep", "1", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({p.name for p in self.snaps.glob("*.md")}, {"2026-10-22.md"})

    def test_manual_files_are_refused_even_with_yes(self):
        self._snaps(("2026-10-20.md", "2026-10-22.md"))
        (self.snaps / "handwritten.md").write_text("手工文件\n", encoding="utf-8")
        r = self.ctl("prune", "--keep", "1", "--yes")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertTrue((self.snaps / "handwritten.md").exists())
        self.assertIn("手工文件", r.stdout)


# --------------------------------------------------------------------------- #
# 6) on：过期/空正文要说清楚，支持 --ttl 顺手续期
# --------------------------------------------------------------------------- #
class ResumeCase(Base):
    def test_on_expired_panel_refuses_with_hint(self):
        self.panel_text(expires="2020-01-01T00:00:00+08:00", enabled="false")
        r = self.ctl("on")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("--ttl", r.stdout + r.stderr)
        self.assertIn("enabled: false", self.panel.read_text(encoding="utf-8"))

    def test_on_with_ttl_renews_and_injects(self):
        self.panel_text(expires="2020-01-01T00:00:00+08:00", enabled="false")
        r = self.ctl("on", "--ttl", "8h")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("测试面板", self.ctx(self.inject()))

    def test_on_empty_body_refuses(self):
        self.panel_text(body="")
        r = self.ctl("on")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("正文为空", r.stdout + r.stderr)

    def test_on_empty_scope_refuses(self):
        self.panel_text(session="")
        r = self.ctl("on")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("session_id 为空", r.stdout + r.stderr)

    def test_on_valid_panel_still_works(self):
        self.panel_text(enabled="false")
        r = self.ctl("on")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("测试面板", self.ctx(self.inject()))


# --------------------------------------------------------------------------- #
# 7) 头部未知键：注入 fail-closed、check 报错
# --------------------------------------------------------------------------- #
class UnknownMetaCase(Base):
    def test_unknown_key_blocks_injection(self):
        self.panel_text("1. 正文", extra_head="owner: 我")
        proc = self.inject()
        self.assertEqual(proc.returncode, 0)
        self.assertEqual(proc.stdout.strip(), "")
        self.assertIn("skip unknown-meta", self.logtext())
        self.assertEqual(self.ctx(proc), "")

    def test_check_explains_unknown_key_kills_whole_block(self):
        self.panel_text("1. 正文", extra_head="owner: 我")
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("未知键", r.stdout)
        self.assertIn("整块失效", r.stdout)

    def test_impact_counts_unknown_key_as_ineffective(self):
        home = self.dir / "home"
        self.hook_ready(home, [("AAA", "a")])
        self.panel_text("1. 正文", extra_head="owner: 我")
        data = json.loads(self.ctl("impact", "--json", extra_env={"HERMES_HOME": str(home)}).stdout)
        self.assertEqual(data["panel"]["unknown_meta"], ["owner"])
        self.assertEqual(data["totals"]["effective"], 0)
        self.assertIn("未知键", data["sessions"][0]["reason"])


# --------------------------------------------------------------------------- #
# 8) 续行上的 [scope:]
# --------------------------------------------------------------------------- #
class ContinuationScopeCase(Base):
    def test_continuation_scope_is_honoured(self):
        self.panel_text("1. [scope: all] 公共\n2. 只给 A\n   [scope: session:AAA]\n   续行")
        a = self.ctx(self.inject("AAA"))
        b = self.ctx(self.inject("BBB"))
        self.assertIn("只给 A", a)
        self.assertIn("续行", a)
        self.assertNotIn("只给 A", b)
        self.assertNotIn("[scope:", a)
        self.assertNotIn("[scope:", b)
        self.assertEqual(self.ctl("check").returncode, 0)

    def test_two_scopes_on_one_entry_invalidate_it(self):
        self.panel_text("1. [scope: all] 两处\n   [scope: session:AAA]")
        self.assertEqual(self.inject("AAA").stdout.strip(), "")
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout)
        self.assertIn("两处", r.stdout)

    def test_scope_command_clears_continuation_tag(self):
        self.panel_text("1. 一号\n2. 二号\n   [scope: session:AAA]\n   续行")
        r = self.ctl("scope", "2", "all")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        text = self.panel.read_text(encoding="utf-8")
        self.assertIn("2. [scope: all] 二号", text)
        self.assertEqual(text.count("[scope:"), 1, text)   # 续行那个已被清掉


# --------------------------------------------------------------------------- #
# 9) set 写入前校验
# --------------------------------------------------------------------------- #
class SetValidationCase(Base):
    def test_rejects_empty_body(self):
        self.panel_text()
        before = self.panel.read_text(encoding="utf-8")
        r = self.ctl("set", "--ttl", "1h", stdin_text="")
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("正文为空", r.stdout + r.stderr)
        self.assertEqual(before, self.panel.read_text(encoding="utf-8"))

    def test_rejects_oversized_body(self):
        self.panel_text()
        big = self.dir / "big.md"
        big.write_text("字" * 1700, encoding="utf-8")
        r = self.ctl("set", "--ttl", "1h", "--body", str(big))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("上限", r.stdout + r.stderr)
        self.assertNotIn("字" * 50, self.panel.read_text(encoding="utf-8"))

    def test_rejects_non_utf8_body(self):
        self.panel_text()
        bad = self.dir / "bad.md"
        bad.write_bytes(b"\xff\xfe\x00bad")
        r = self.ctl("set", "--ttl", "1h", "--body", str(bad))
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("UTF-8", r.stdout + r.stderr)

    def test_tty_without_body_prompts_instead_of_blocking(self):
        self.panel_text()
        mod = self.load_module("hudctl")

        class FakeTTY:
            def isatty(self):
                return True

            def read(self):
                raise AssertionError("TTY 且没给正文时不能读 stdin（会阻塞）")

        with mock.patch("sys.stdin", FakeTTY()):
            with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                with self.assertRaises(SystemExit) as ctx:
                    mod.cmd_set(argparse.Namespace(ttl="1h", session=None, title=None, body=None))
        self.assertEqual(ctx.exception.code, 2)

    def test_accepts_normal_body(self):
        self.panel_text()
        body = self.dir / "b.md"
        body.write_text("新正文", encoding="utf-8")
        r = self.ctl("set", "--ttl", "2h", "--body", str(body))
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("新正文", self.panel.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 12) 面板大小上限 / 写失败清 tmp / 并发读改写加锁
# --------------------------------------------------------------------------- #
class PanelIOHardeningCase(Base):
    def test_oversized_panel_is_refused(self):
        self.panel.write_bytes(b"x" * 250_000)
        r = self.ctl("status")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("面板过大", r.stdout + r.stderr)

    def test_oversized_panel_is_refused_by_check(self):
        self.panel.write_bytes(b"x" * 250_000)
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1)
        self.assertIn("面板过大", r.stdout)

    def test_write_leaves_no_tmp_file(self):
        self.panel_text()
        self.assertEqual(self.ctl("scope", "1", "all").returncode, 0)
        self.assertEqual(list(self.dir.glob("panel.md.*.tmp")), [])

    def test_concurrent_scope_updates_are_not_lost(self):
        self.panel_text("\n".join(f"{i}. 条目{i}" for i in range(1, 7)))
        procs = [subprocess.Popen(
            [PY, str(self.dir / "hudctl.py"), "scope", str(i), f"profile:p{i}"],
            env=self.env(), cwd=str(self.dir),
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            encoding="utf-8", errors="replace") for i in range(1, 7)]
        for proc in procs:
            out, err = proc.communicate(timeout=120)
            self.assertEqual(proc.returncode, 0, out + err)
        text = self.panel.read_text(encoding="utf-8")
        for i in range(1, 7):
            self.assertIn(f"[scope: profile:p{i}]", text)

    def test_concurrent_scope_updates_under_double_contention(self):
        """12 个进程抢同一把**新建**锁，终态仍必须 6 条齐全。

        每条条目安排两个写者、但写同一个值，所以终态与调度顺序无关，断言不必放松。
        新增这条是为了覆盖「锁文件刚被并发创建」的窗口：旧 `_unsafe_path()` 在这里
        会把刚建出来的普通锁文件误判成重解析点（CI windows/3.12 的失败形态）。
        """
        self.panel_text("\n".join(f"{i}. 条目{i}" for i in range(1, 7)))
        procs = []
        for n in range(12):
            entry = n % 6 + 1
            procs.append(subprocess.Popen(
                [PY, str(self.dir / "hudctl.py"), "scope", str(entry), f"profile:p{entry}"],
                env=self.env(), cwd=str(self.dir),
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace"))
        for proc in procs:
            out, err = proc.communicate(timeout=180)
            self.assertEqual(proc.returncode, 0, out + err)
        text = self.panel.read_text(encoding="utf-8")
        for i in range(1, 7):
            self.assertIn(f"[scope: profile:p{i}]", text)
        self.assertEqual(list(self.dir.glob("panel.md.*.tmp")), [])
        lock = self.dir / "panel.md.lock"
        self.assertTrue(lock.is_file())
        self.assertFalse(lock.is_symlink())


# --------------------------------------------------------------------------- #
# 13) parse_scope 半个 token / use 精确匹配不限 300 条
# --------------------------------------------------------------------------- #
class ScopeParseCase(Base):
    def test_parse_scope_splits_and_rejects_half_tokens(self):
        mod = self.load_module("hud_panel")
        self.assertEqual(mod.parse_scope("session:AAA,BBB"), ("session", {"AAA", "BBB"}))
        self.assertEqual(mod.parse_scope("session:AAA BBB"), ("session", {"AAA", "BBB"}))
        self.assertEqual(mod.parse_scope("profile:default, ceo"), ("profile", {"default", "ceo"}))
        self.assertIsNone(mod.parse_scope("session:AAA profile:default"))
        self.assertIsNone(mod.parse_scope("group:AAA"))
        self.assertIsNone(mod.parse_scope("session:AAA*"))
        self.assertIsNone(mod.parse_scope("session:"))

    def test_half_token_scope_does_not_leak(self):
        self.panel_text("1. [scope: session:AAA profile:default] 半个 token")
        self.assertEqual(self.inject("AAA").stdout.strip(), "")
        self.assertIn("invalid_scope=1", self.logtext())

    def test_use_finds_session_beyond_recent_300(self):
        home = self.dir / "home"
        home.mkdir(parents=True, exist_ok=True)
        rows = [("20200101_000000_oldest", "很久以前的会话")]
        rows += [(f"20260101_{i:06d}_x", f"近期 {i}") for i in range(400)]
        self.make_db(home / "state.db", rows, source_col=False)
        self.panel_text()
        r = self.ctl("use", "20200101_000000_oldest", "--ttl", "1h",
                     extra_env={"HERMES_HOME": str(home)})
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("session_id: 20200101_000000_oldest",
                      self.panel.read_text(encoding="utf-8"))


# --------------------------------------------------------------------------- #
# 1b) check --json：真格式错 vs 状态提示
# --------------------------------------------------------------------------- #
class CheckClassificationCase(Base):
    def test_json_separates_errors_from_status(self):
        self.panel_text(session="20260101_000000_abcdef", enabled="false",
                        expires="2020-01-01T00:00:00+08:00")
        r = self.ctl("check", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["errors"], [])
        notices = " ".join(data["notices"])
        self.assertIn("过期", notices)
        self.assertIn("暂停", notices)
        self.assertIn("只作用于会话", notices)
        self.assertEqual(self.ctl("check", "--json", "--strict").returncode, 1)

    def test_json_reports_format_errors(self):
        self.panel.write_text("session_id: any\n\n只有正文\n", encoding="utf-8")
        r = self.ctl("check", "--json")
        self.assertEqual(r.returncode, 1)
        data = json.loads(r.stdout)
        self.assertFalse(data["ok"])
        self.assertTrue(data["errors"])
        self.assertTrue(any("缺少元数据" in item for item in data["errors"]))


if __name__ == "__main__":
    unittest.main()
