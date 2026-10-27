"""第二轮复审 hook 残留的回归：管理端行尾归一 / selfcheck 事件域 / 日志行清洗 /
scope 续行缩进 / 截断保留尾行警告。

每条都先按复审给出的坏输入复现，再钉住期望行为；不依赖真实 Hermes 安装。
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import shutil
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
CR, LF = chr(13), chr(10)
CRLF = CR + LF
CR_B = bytes([13])


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

    def env(self, **extra):
        env = {k: v for k, v in os.environ.items() if k not in CLEAR}
        env.update(PYTHONIOENCODING="utf-8", HUD_PANEL=str(self.panel),
                   HUD_LOG=str(self.log), HUD_SNAPS=str(self.snaps),
                   HUD_STATE=str(self.state), HERMES_HOME=str(self.root / "home"))
        env.update({k: str(v) for k, v in extra.items()})
        return env

    def panel_text(self, body="1. 正文", session="any",
                   expires="2099-01-01T00:00:00+08:00", title="测试面板", enabled="true"):
        self.panel.write_text(
            f"session_id: {session}\nexpires_at: {expires}\ntitle: {title}\n"
            f"enabled: {enabled}\n\n{body}\n", encoding="utf-8")

    def ctl(self, *args, **extra):
        return subprocess.run([PY, str(self.dir / "hudctl.py"), *args],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", env=self.env(**extra), cwd=str(self.dir),
                              timeout=180)

    def inject_raw(self, payload, **extra):
        return subprocess.run([PY, str(self.dir / "hud_inject.py")],
                              input=json.dumps(payload), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=self.env(**extra),
                              cwd=str(self.dir), timeout=180)

    def inject(self, sid="AAA", profile="default", **extra):
        return self.inject_raw({"session_id": sid, "profile": profile}, **extra)

    @staticmethod
    def ctx(proc) -> str:
        return json.loads(proc.stdout)["context"] if proc.stdout.strip() else ""

    def logtext(self) -> str:
        return self.log.read_text(encoding="utf-8") if self.log.exists() else ""

    def write_state(self, checks):
        self.state.mkdir(parents=True, exist_ok=True)
        (self.state / "selfcheck.json").write_text(json.dumps({
            "status": "ok" if all(c.get("ok") for c in checks) else "error",
            "ok": all(c.get("ok") for c in checks),
            "fingerprint": "",
            "checks": checks,
        }), encoding="utf-8")


# --------------------------------------------------------------------------- #
# P2：管理端读面板 / 快照必须归一 CRLF 与双 CR（头部双 CR 曾假红、restore 拒收）
# --------------------------------------------------------------------------- #
class DoubleCrHeaderCase(Base):
    HEAD_CR = (f"session_id: any{CR}{CRLF}expires_at: 2099-01-01T00:00:00+08:00{CR}{CRLF}"
               f"title: t{CR}{CRLF}enabled: true{CR}{CRLF}{CRLF}")

    def test_check_tolerates_double_cr_header(self):
        """头部是旧版叠出来的双 CR 行尾：元数据仍要完整读出，不能报缺 expires_at。"""
        self.panel.write_bytes((self.HEAD_CR + f"1. 正文{CRLF}").encode("utf-8"))
        r = self.ctl("check", "--json")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads(r.stdout)
        self.assertTrue(data["ok"], data)
        self.assertEqual(data["errors"], [])
        self.assertEqual(data["expires_at"], "2099-01-01T00:00:00+08:00")
        self.assertEqual(data["body_chars"], len("1. 正文"))

    def test_check_still_reports_real_error_on_crlf_panel(self):
        """修的是归一，不是把格式错一起放过：CRLF 面板缺 enabled 仍要红。"""
        self.panel.write_bytes((f"session_id: any{CRLF}expires_at: 2099-01-01T00:00:00+08:00{CRLF}"
                                f"title: t{CRLF}{CRLF}1. 正文{CRLF}").encode("utf-8"))
        r = self.ctl("check")
        self.assertEqual(r.returncode, 1, r.stdout + r.stderr)
        self.assertIn("enabled", r.stdout)

    def test_restore_accepts_snapshot_with_double_cr_header(self):
        """快照头部带双 CR 是合法快照：restore 不能拒收，落盘必须归一成 LF。"""
        self.snaps.mkdir()
        (self.snaps / "2026-10-01.md").write_bytes(
            (self.HEAD_CR + f"1. 恢复正文{CRLF}").encode("utf-8"))
        self.panel_text("1. 现网正文")
        r = self.ctl("restore", "--file", "2026-10-01.md")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        raw = self.panel.read_bytes()
        self.assertNotIn(CR_B, raw, f"恢复后仍有 CR：{raw[:120]!r}")
        self.assertIn("1. 恢复正文".encode("utf-8"), raw)
        self.assertEqual(self.ctl("check").returncode, 0)


class PhysicalPanelNormalizationCase(Base):
    """把 live 面板文件的行尾一次性规范成 LF：管理与注入的结果必须逐字不变。

    真实形状：旧版 `hudctl set` 在 Windows 上把已含 CR 的正文又叠一层换行转换，
    落盘的 `panel.md` 头部与正文都是 `\\r\\r\\n`。管理端改成 `newline=""` 归一是
    「读的时候不假红」，但文件本身仍是脏的；物理规范化属于另一件事——它不许
    改变 check 报的字数/到期，也不许改变注入块。
    """

    BODY = ("## 硬约束\n"
            "1. 第一条：用 delegate_task 派活。\n"
            "2. 第二条：只做可判定的事。\n"
            "\n"
            "## 红线\n"
            "- 不重启。\n"
            "- 不改凭据。\n")
    HEAD = (f"session_id: any{CR}{CRLF}expires_at: 2099-01-01T00:00:00+08:00{CR}{CRLF}"
            f"title: 物理行尾{CR}{CRLF}enabled: true{CR}{CRLF}{CR}{CRLF}")

    def dirty_panel(self) -> bytes:
        """头部与正文都带双 CR：旧版 set 在 Windows 上留下的真实形状。"""
        body = "".join(line + CR + CRLF for line in self.BODY.splitlines())
        return (self.HEAD + body).encode("utf-8")

    def inject_ctx(self) -> str:
        # 用非 SID 形状的会话 id：面板是 any，取值不影响判据，也免得触发公开仓的会话 id 守卫
        r = self.inject(sid="physical-lf-probe")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return self.ctx(r)

    def test_double_cr_panel_survives_physical_lf_normalization(self):
        self.panel.write_bytes(self.dirty_panel())
        self.assertIn(CR_B, self.panel.read_bytes(), "fixture 里没有双 CR，测不到目标形状")

        # ① 头部/正文带双 CR 时不许假红：元数据完整，字数按归一后的正文算
        before = self.ctl("check", "--json")
        self.assertEqual(before.returncode, 0, before.stdout + before.stderr)
        before_data = json.loads(before.stdout)
        self.assertTrue(before_data["ok"], before_data)
        self.assertEqual(before_data["errors"], [])
        self.assertEqual(before_data["expires_at"], "2099-01-01T00:00:00+08:00")
        self.assertEqual(before_data["body_chars"], len(self.BODY.strip()))
        ctx_before = self.inject_ctx()
        self.assertNotIn(CR, ctx_before, "注入块里还有 CR")
        self.assertNotIn("\n\n\n", ctx_before, f"双 CR 注出了多余空行：{ctx_before!r}")
        self.assertIn("entries=4/4", self.logtext(), self.logtext())
        self.assertIn(f"chars={len(ctx_before)}", self.logtext(), self.logtext())

        # ② 物理规范化：一串 CR 只算一个换行（与注入端 normalize_newlines 同口径）
        raw = self.panel.read_bytes()
        self.panel.write_bytes(re.sub(rb"\r+\n|\r+", b"\n", raw))
        clean = self.panel.read_bytes()
        self.assertNotIn(CR_B, clean, f"规范化后仍有 CR：{clean[:120]!r}")
        self.assertTrue(clean.endswith(LF.encode()), clean[-8:])

        # ③ 只改了行尾：check 的 JSON 与注入块必须逐字不变
        after = self.ctl("check", "--json")
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertEqual(json.loads(after.stdout), before_data)
        self.assertEqual(self.inject_ctx(), ctx_before)
        self.assertEqual(self.logtext().count("entries=4/4"), 2, self.logtext())


# --------------------------------------------------------------------------- #
# P2：selfcheck 的 config / allowlist 判定必须按事件域，不能是子串
# --------------------------------------------------------------------------- #
class SelfcheckEventDomainCase(Base):
    def make_home(self, config: str, approvals: list[dict]) -> Path:
        home = self.root / "home"
        src = home / "hermes-agent"
        (src / "agent").mkdir(parents=True)
        (src / "hermes_cli").mkdir()
        (src / "agent" / "turn_context.py").write_text(
            "def f():\n    # pre_llm_call context\n    plugin_user_context = 1\n", encoding="utf-8")
        (src / "agent" / "shell_hooks.py").write_text("# pre_llm_call\n", encoding="utf-8")
        (home / "config.yaml").write_text(config, encoding="utf-8")
        (home / "shell-hooks-allowlist.json").write_text(
            json.dumps({"approvals": approvals}), encoding="utf-8")
        self.panel_text()
        return home

    def run_check(self, home: Path):
        return subprocess.run([PY, str(REPO / "selfcheck.py")], capture_output=True,
                              text=True, encoding="utf-8", errors="replace",
                              env=self.env(HERMES_HOME=str(home)), timeout=180)

    def checks(self) -> dict[str, dict]:
        data = json.loads((self.state / "selfcheck.json").read_text(encoding="utf-8"))
        return {c["check"]: c for c in data["checks"]}

    def test_hook_only_under_pre_tool_call_is_not_declared(self):
        """hud_inject 只挂在 pre_tool_call：config/allowlist 两项都必须红。"""
        home = self.make_home(
            "hooks:\n"
            "  pre_tool_call:\n"
            "    - command: python D:/x/hud_inject.py\n"
            "      timeout: 10\n"
            "  pre_llm_call:\n"
            "    - command: python D:/x/somebody-else.py\n"
            "      timeout: 10\n",
            [{"event": "pre_tool_call", "command": "python D:/x/hud_inject.py"},
             {"event": "pre_llm_call", "command": "python D:/x/somebody-else.py"}])
        r = self.run_check(home)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        checks = self.checks()
        self.assertFalse(checks["config:hooks.pre_llm_call"]["ok"])
        self.assertFalse(checks["allowlist:已同意"]["ok"])
        self.assertIn("pre_llm_call", checks["config:hooks.pre_llm_call"]["detail"])

    def test_pre_llm_call_with_other_command_is_not_ours(self):
        """pre_llm_call 下挂的是别人：同样是没声明/没同意。"""
        home = self.make_home(
            "hooks:\n  pre_llm_call:\n    - command: python D:/x/other.py\n",
            [{"event": "pre_llm_call", "command": "python D:/x/other.py"}])
        r = self.run_check(home)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        checks = self.checks()
        self.assertFalse(checks["config:hooks.pre_llm_call"]["ok"])
        self.assertFalse(checks["allowlist:已同意"]["ok"])

    def test_four_space_indent_block_still_green(self):
        """不能为了事件域把合法写法判死：hooks 子键缩进 4 空格也要认。"""
        home = self.make_home(
            "hooks:\n    pre_llm_call:\n        - command: python D:/x/hud_inject.py\n"
            "          timeout: 10\n",
            [{"event": "pre_llm_call", "command": "python D:/x/hud_inject.py"}])
        r = self.run_check(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        data = json.loads((self.state / "selfcheck.json").read_text(encoding="utf-8"))
        self.assertTrue(data["ok"])

    def test_inline_flow_form_still_green(self):
        home = self.make_home(
            "hooks: {pre_llm_call: [{command: python D:/x/hud_inject.py, timeout: 10}]}\n",
            [{"event": "pre_llm_call", "command": "python D:/x/hud_inject.py"}])
        r = self.run_check(home)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class HookParserCase(Base):
    """hud_panel 的解析器是唯一实现：直接钉住事件域 / 注释 / 缩进 / allowlist 边界。"""

    def setUp(self):
        super().setUp()
        sys.path.insert(0, str(self.dir))
        self.addCleanup(lambda: sys.path.remove(str(self.dir)))
        saved = sys.modules.get("hud_panel")

        def restore():
            if saved is not None:
                sys.modules["hud_panel"] = saved
            else:
                sys.modules.pop("hud_panel", None)

        self.addCleanup(restore)
        import hud_panel
        self.hp = hud_panel

    def test_commented_or_other_event_hooks_ignored(self):
        cfg = ("# hooks:\n"
               "#   pre_llm_call:\n"
               "#     - command: python D:/x/hud_inject.py\n"
               "hooks:\n"
               "  pre_tool_call:\n"
               "    - command: python D:/x/hud_inject.py\n"
               "  pre_llm_call:\n"
               "    - command: python D:/x/other.py\n")
        self.assertEqual(self.hp.hook_commands(cfg), [])

    def test_block_inline_and_indent_forms_parsed(self):
        block = ("hooks:\n  pre_llm_call:\n    - command: python D:/x/hud_inject.py\n"
                 "      timeout: 10\n")
        inline = "hooks: {pre_llm_call: [{command: python D:/x/hud_inject.py}]}\n"
        deep = "hooks:\n    pre_llm_call:\n        - command: python D:/x/hud_inject.py\n"
        same_indent = "hooks:\n  pre_llm_call:\n  - command: python D:/x/hud_inject.py\n"
        for cfg in (block, inline, deep, same_indent):
            self.assertEqual(self.hp.hook_commands(cfg), ["python D:/x/hud_inject.py"], cfg)

    def test_inline_yaml_comment_is_stripped_from_command(self):
        cfg = ("hooks:\n  pre_llm_call:\n"
               "    - command: python D:/x/hud_inject.py  # live\n")
        self.assertEqual(
            self.hp.hook_commands(cfg),
            ["python D:/x/hud_inject.py"],
        )

    def test_backup_suffix_is_not_the_live_hook(self):
        cfg = ("hooks:\n  pre_llm_call:\n"
               "    - command: python D:/workspace/temp/hud/hud_inject.py.bak-20261024\n")
        self.assertEqual(self.hp.hook_commands(cfg), [])

    def test_tilde_backup_variants_are_not_the_live_hook(self):
        for path in ("D:/x/hud_inject.py~", "D:/x/~hud_inject.py"):
            cfg = ("hooks:\n  pre_llm_call:\n"
                   f"    - command: python {path}\n")
            self.assertEqual(self.hp.hook_commands(cfg), [], path)

    def test_quoted_hash_inside_command_is_not_a_comment(self):
        cfg = ("hooks:\n  pre_llm_call:\n"
               '    - command: "python D:/x/hud_inject.py --tag #1"\n')
        self.assertEqual(
            self.hp.hook_commands(cfg),
            ["python D:/x/hud_inject.py --tag #1"],
        )

    def test_command_in_a_comment_is_not_declared(self):
        cfg = ("hooks:\n  pre_llm_call: []  # 旧: [{command: python D:/x/hud_inject.py}]\n")
        self.assertEqual(self.hp.hook_commands(cfg), [])

    def test_top_level_hook_key_outside_hooks_block_ignored(self):
        cfg = "pre_llm_call: python D:/x/hud_inject.py\nhooks:\n  post_tool_call:\n    - command: python D:/x/hud_inject.py\n"
        self.assertEqual(self.hp.hook_commands(cfg), [])

    def test_malformed_scope_is_fail_closed_and_warned(self):
        body = "1. [ scope: all] 这条不能被广播\n"
        item = self.hp.entries(body)[0]
        self.assertIsNone(item["scope"], item)
        self.assertIn("scope", " ".join(self.hp.validate(body)))

    def test_all_malformed_scope_variants_fail_closed(self):
        for body in (
            "1. ［scope: profile:ceo］ 只给 ceo\n",
            "1. [scope: all 缺右括号\n",
            "1. [Scope : all] x\n",
        ):
            item = self.hp.entries(body)[0]
            self.assertIsNone(item["scope"], (body, item))
            self.assertTrue(item["malformed_scope"], (body, item))
            self.assertTrue(self.hp.validate(body), body)

    def test_body_text_mentioning_scope_is_not_a_tag(self):
        item = self.hp.entries("1. 正文提到 [scope: all] 但不是标注\n")[0]
        self.assertFalse(item["malformed_scope"], item)

    def test_valid_tag_with_fullwidth_colon_in_body_is_not_malformed(self):
        item = self.hp.entries("1. [scope: all] 注意：只给 ceo 看\n")[0]
        self.assertEqual(item["scope"], ("all", set()), item)
        self.assertFalse(item["malformed_scope"], item)

    def test_valid_ascii_tag_with_fullwidth_brackets_in_body(self):
        item = self.hp.entries("1. [scope: all] 见［附件］\n")[0]
        self.assertEqual(item["scope"], ("all", set()), item)
        self.assertFalse(item["malformed_scope"], item)

    def test_missing_colon_or_chinese_brackets_fail_closed(self):
        for body in (
            "1. [scope profile:ceo] 机密XYZ\n",
            "1. 【scope: profile:ceo】 机密XYZ\n",
        ):
            item = self.hp.entries(body)[0]
            self.assertIsNone(item["scope"], (body, item))
            self.assertTrue(item["malformed_scope"], (body, item))

    def test_curly_angle_and_bare_scope_fail_closed(self):
        for body in (
            "1. ｛scope: profile:ceo｝ 机密XYZ\n",
            "1. {scope: profile:ceo} 机密XYZ\n",
            "1. (scope: profile:ceo) 机密XYZ\n",
            "1. <scope: profile:ceo> 机密XYZ\n",
            "1. 「scope: profile:ceo」 机密XYZ\n",
            "1. ＜scope: profile:ceo＞ 机密XYZ\n",
            "1. 〈scope: profile:ceo〉 机密XYZ\n",
            "1. 〖scope: profile:ceo〗 机密XYZ\n",
            "1. \u200bscope: profile:ceo 机密XYZ\n",
            "1. [scopes: profile:ceo] 机密XYZ\n",
            "1. scope: profile:ceo 机密XYZ\n",
            "1. scop: profile:ceo 机密XYZ\n",
        ):
            item = self.hp.entries(body)[0]
            self.assertIsNone(item["scope"], (body, item))
            self.assertTrue(item["malformed_scope"], (body, item))

    def test_zero_entry_bare_scope_is_not_broadcast(self):
        for body in (
            "scope: profile:ceo 机密XYZ",
            "scope：profile:ceo 机密XYZ",
            "〈scope: profile:ceo〉 机密XYZ",
            "\u200bscope: profile:ceo 机密XYZ",
            "ｓｃｏｐｅ：profile:ceo 机密XYZ",
            "sc\u200bope: profile:ceo 机密XYZ",
            "1.〈scope\ufe0f: profile:ceo〉 机密XYZ",
            "1. scope\ufe0f: profile:ceo 机密XYZ",
            "1. sc\u0301ope: profile:ceo 机密XYZ",
            "1. scope\u20dd: profile:ceo 机密XYZ",
            "1. scope\u2800: profile:ceo 机密XYZ",
            "1. scope\ue000: profile:ceo 机密XYZ",
            "1. [ѕcope: profile:ceo] 机密XYZ",
            "1. [ѕсоре: profile:ceo] 机密XYZ",
            "1. [ѕсορε: profile:ceo] 机密XYZ",
            "1.〈scope: profile:ceo〉 机密XYZ",
        ):
            text, kept, _total = self.hp.filter_body(body, "S-BUILD", "build")
            self.assertEqual((text, kept), ("", 0), body)

    def test_fullwidth_closing_bracket_is_not_a_valid_tag(self):
        item = self.hp.entries("1. [scope: all］ 说明\n")[0]
        self.assertIsNone(item["scope"], item)
        self.assertTrue(item["malformed_scope"], item)

    def test_malformed_continuation_drops_the_whole_entry(self):
        body = "1. 公共\n   [ scope: profile:ceo] 只给 ceo\n"
        text, kept, total = self.hp.filter_body(body, "AAA", "build")
        self.assertEqual((kept, total), (0, 1), body)
        self.assertEqual(text.strip(), "", text)

    def test_uppercase_hook_filename_is_recognized(self):
        cfg = ("hooks:\n  pre_llm_call:\n"
               "    - command: python D:/x/HUD_INJECT.PY\n")
        self.assertEqual(self.hp.hook_commands(cfg), ["python D:/x/HUD_INJECT.PY"])

    def test_approved_commands_event_domain_and_bad_json(self):
        data = json.dumps({"approvals": [
            {"event": "pre_tool_call", "command": "python D:/x/hud_inject.py"},
            {"event": "pre_llm_call", "command": "python D:/x/hud_inject.py"},
            {"event": "pre_llm_call", "command": "python D:/x/other.py"},
            "not-a-dict",
        ]})
        self.assertEqual(self.hp.approved_commands(data), {"python D:/x/hud_inject.py"})
        self.assertEqual(self.hp.approved_commands("{oops"), set())
        self.assertEqual(self.hp.approved_commands('["list"]'), set())
        upper = json.dumps({"approvals": [
            {"event": "pre_llm_call", "command": "python D:/x/HUD_INJECT.PY"},
            {"event": "pre_llm_call", "command": "python D:/x/hud_inject.py.bak"},
        ]})
        self.assertEqual(self.hp.approved_commands(upper), {"python D:/x/HUD_INJECT.PY"})


# --------------------------------------------------------------------------- #
# P3：hud.log 的 payload 字段不能注入换行 / 控制符
# --------------------------------------------------------------------------- #
class LogSanitizeCase(Base):
    def test_payload_newlines_cannot_forge_a_log_line(self):
        self.panel_text()
        forged = ("AAA" + LF + "2099-01-01T00:00:00+08:00 inject session=BBB profile=default "
                  "chars=1 entries=9/9" + CR + "tail" + chr(27) + "[31m"
                  + chr(0x2028) + chr(0x85) + "sneaky")
        proc = self.inject_raw({"session_id": forged, "profile": "default" + LF + "FORGED"})
        self.assertEqual(proc.returncode, 0, proc.stderr)
        text = self.logtext()
        lines = text.splitlines()          # splitlines 也把 U+2028 / NEL 当换行，一并钉住
        self.assertEqual(len(lines), 1, f"payload 换行被写成了多行日志：{text!r}")
        self.assertIn("inject session=AAA", lines[0])
        self.assertIn("FORGED", lines[0])          # 清洗成空格保留，仍能排障
        self.assertIn("sneaky", lines[0])
        self.assertNotIn(chr(27), text)
        self.assertFalse([ln for ln in lines if ln.startswith("2099-01-01")])

    def test_normal_log_line_still_readable_by_report(self):
        """清洗不能把正常行写坏：report 仍认得出 inject 行。"""
        self.panel_text()
        self.inject()
        env = self.env()
        r = subprocess.run([PY, str(self.dir / "hud_report.py")], capture_output=True,
                           text=True, encoding="utf-8", errors="replace", env=env,
                           cwd=str(self.dir), timeout=120)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("inject", r.stdout)


# --------------------------------------------------------------------------- #
# P3：hudctl scope 改写续行时必须保留缩进（否则内容被提升成新条目）
# --------------------------------------------------------------------------- #
class ScopeIndentCase(Base):
    def body(self) -> str:
        text = self.panel.read_text(encoding="utf-8")
        return text.split("\n\n", 1)[1]

    def test_scope_rewrite_keeps_continuation_indent(self):
        self.panel_text("1. 第一条\n   [scope: all] - 续行内容")
        r = self.ctl("scope", "1", "all")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        lines = self.body().splitlines()
        self.assertEqual(lines[0], "1. [scope: all] 第一条")
        self.assertEqual(lines[1], "   - 续行内容")
        sys.path.insert(0, str(self.dir))
        try:
            import hud_panel
            self.assertEqual(len(hud_panel.entries(self.body())), 1,
                             f"续行被提升成新条目：{self.body()!r}")
        finally:
            sys.path.remove(str(self.dir))
            sys.modules.pop("hud_panel", None)

    def test_filter_body_keeps_tagged_continuation_indent(self):
        sys.path.insert(0, str(self.dir))
        try:
            import hud_panel
            text, kept, total = hud_panel.filter_body(
                "1. 公共\n   [scope: all] - 续行\n", "AAA", "default")
            self.assertEqual((kept, total), (1, 1))
            self.assertIn("\n   - 续行", text)
        finally:
            sys.path.remove(str(self.dir))
            sys.modules.pop("hud_panel", None)


# --------------------------------------------------------------------------- #
# P3：截断到 1600 字不能吃掉尾行的自检警告 / 执行机制建议
# --------------------------------------------------------------------------- #
class TruncationCase(Base):
    def test_truncation_keeps_selfcheck_warning(self):
        self.panel_text("1. " + "字" * 3000)
        self.write_state([{"check": "panel:格式自检", "ok": False, "detail": "x"}])
        ctx = self.ctx(self.inject())
        self.assertLessEqual(len(ctx), 1600, f"截断后仍超限：{len(ctx)}")
        self.assertIn("面板过长", ctx)
        self.assertIn("执行机制建议", ctx)
        self.assertIn("⚠ 面板格式自检失败：先跑 python hudctl.py check", ctx)
        self.assertTrue(ctx.index("⚠") > ctx.index("面板过长"))

    def test_truncation_keeps_hint_when_no_warning(self):
        """自检健康时没有警告行，但执行机制建议也不能被截掉。"""
        self.panel_text("1. " + "字" * 3000)
        self.write_state([{"check": "panel:格式自检", "ok": True, "detail": "ok"}])
        ctx = self.ctx(self.inject())
        self.assertLessEqual(len(ctx), 1600)
        self.assertIn("面板过长", ctx)
        self.assertIn("执行机制建议", ctx)
        self.assertNotIn("⚠", ctx)

    def test_compose_block_never_cuts_suffix(self):
        """单元级：给一个小上限，尾行必须完整留在块尾，正文被截并带标记。"""
        sys.path.insert(0, str(self.dir))
        try:
            spec = importlib.util.spec_from_file_location(
                "hud_inject_under_test", self.dir / "hud_inject.py")
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            block = mod._compose_block("P" * 10 + LF, "x" * 500, "S" * 20, limit=80)
            self.assertLessEqual(len(block), 80)
            self.assertTrue(block.endswith("S" * 20))
            self.assertIn("面板过长", block)
            short = "P\n" + "正文" + "\nS"
            self.assertEqual(mod._compose_block("P\n", "正文", "\nS", limit=80), short)
        finally:
            sys.path.remove(str(self.dir))
            sys.modules.pop("hud_panel", None)


if __name__ == "__main__":
    unittest.main()
