#!/usr/bin/env python3
"""selfcheck.py — 检查 hermes-hook-prompt 依赖的官方机制是否仍然可用。

Hermes 每次更新后，官方 hooks 的实现细节可能变化。本脚本做一次「机制体检」：

  1. 模块：同目录 `hud_panel.py` 在（解析器是注入的前提，缺了注入会整块跳过）
  2. 源码：跨 `agent/*.py` 与 `hermes_cli/*.py` 搜 `pre_llm_call` 落点（事件可能换文件）
  3. 源码：`agent/turn_context.py` 仍把 hook 返回值当 `plugin_user_context` 注入
  4. 配置：本档 config.yaml 有 `hooks.pre_llm_call` 且指向 `hud_inject.py`
  5. 同意：allowlist 里有 `pre_llm_call` + 该命令的批准记录
  6. 端到端：用**临时 fixture 面板**跑 `hud_inject.py`，能拿到非空 `context`
     （绝不用现网面板：它限定别的会话/过期/暂停/条目全被筛空都会正常退出，
       拿它当机制判据会把「面板状态」误报成「机制坏了」）
  7. 面板格式：`hudctl.py check --json` 的 **errors** 才算失败；过期/暂停/范围不匹配
     属「状态提示」，不影响机制判定

结果写 `state/selfcheck.json`（原子替换；自身异常也会落一份 status=error，不留旧 ok）；
指纹变了或任何一项失败，`hud_inject.py` 会在注入尾行提醒「先跑 selfcheck 并适配」。

退出码：0 全绿；2 有失败（需要适配新版本 / 自检本身出错）。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
try:
    import hud_panel  # 同目录；元数据/作用域/指纹的唯一实现
except Exception:  # pragma: no cover
    hud_panel = None

for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():  # 管道/CI 下统一 UTF-8，避免 Windows 默认代码页报错
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

STATE = Path(os.environ.get("HUD_STATE") or (ROOT / "state"))
STATE_FILE = STATE / "selfcheck.json"
if hud_panel is not None:
    HERMES_HOME = Path(hud_panel.hermes_home())
else:  # 兜底：与 hud_panel.hermes_home() 同口径
    _local = os.environ.get("LOCALAPPDATA")
    HERMES_HOME = Path(os.environ.get("HERMES_HOME")
                       or (Path(_local) / "hermes" if _local
                           else Path.home() / "AppData" / "Local" / "hermes"))
# 源码根向上解析：HERMES_HOME 可能指到 profiles/<名>（那里没有 hermes-agent）
_SOURCE_ROOT = (hud_panel.source_root(str(HERMES_HOME)) if hud_panel is not None else None)
SOURCE = Path(_SOURCE_ROOT) if _SOURCE_ROOT else None

PROFILE = (os.environ.get("HUD_PROFILE") or os.environ.get("HERMES_SESSION_PROFILE")
           or "default")
PROFILE_DIR = (Path(hud_panel.profile_home(str(HERMES_HOME), PROFILE)) if hud_panel is not None
               else (HERMES_HOME if PROFILE == "default" else HERMES_HOME / "profiles" / PROFILE))



def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


SEARCH_GLOBS = ("agent/*.py", "hermes_cli/*.py", "hermes_cli/subcommands/*.py")


def _find_event() -> list[str]:
    """在有限范围里找 pre_llm_call 的落点（事件可能换文件）。"""
    if SOURCE is None:
        return []
    hits = []
    for pattern in SEARCH_GLOBS:
        for path in SOURCE.glob(pattern):
            if path.is_file() and "pre_llm_call" in _read(path):
                hits.append(str(path.relative_to(SOURCE)))
    return hits


def fingerprint() -> str:
    """机制指纹：关键源码文件的**内容** sha256（与 hud_inject 同一实现、同一家目录口径）。"""
    if hud_panel is None:
        return ""
    return hud_panel.mech_fingerprint(str(HERMES_HOME))


def _fixture_panel(folder: Path) -> Path:
    """自检专用面板：session_id=any + 未来到期 + 一条**无 scope** 条目。

    只验「注入道路是否通」，与现网面板的状态（过期/暂停/限定会话）完全解耦。
    """
    panel = folder / "panel.md"
    expires = (datetime.now().astimezone() + timedelta(days=3650)).isoformat(timespec="seconds")
    panel.write_text(
        "session_id: any\n"
        f"expires_at: {expires}\n"
        "title: 自检面板\n"
        "enabled: true\n\n"
        "1. 自检条目：注入通道正常\n",
        encoding="utf-8")
    return panel


def _e2e() -> tuple[bool, str]:
    folder = Path(tempfile.mkdtemp(prefix="hud-selfcheck-"))
    try:
        panel = _fixture_panel(folder)
        env = {k: v for k, v in os.environ.items() if k != "HUD_PANEL"}
        env["HUD_PANEL"] = str(panel)
        env["HUD_LOG"] = str(STATE / "selfcheck-inject.log")
        env.setdefault("PYTHONIOENCODING", "utf-8")
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hud_inject.py")],
            input=json.dumps({"session_id": "selfcheck", "profile": PROFILE}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        ctx = ""
        if proc.stdout.strip():
            try:
                ctx = json.loads(proc.stdout).get("context") or ""
            except Exception:
                ctx = ""
        ok = bool(ctx) and "自检条目" in ctx
        if ok:
            return True, f"fixture 面板注入 {len(ctx)} 字"
        return False, (f"fixture 面板没注进来（stdout={proc.stdout.strip()[:80]!r} "
                       f"stderr={(proc.stderr or '')[-120:]!r}）")
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def _panel_check() -> tuple[bool, str, list[str]]:
    """面板格式：只把 check 的 errors 当失败；notices（过期/暂停/范围）只作提示。

    全新克隆里没有 panel.md（面板是本地数据，不入库）：这不算机制故障，
    按「未安装」提示，否则 fresh-clone 一跑自检就红。
    """
    panel = Path(os.environ.get("HUD_PANEL") or (ROOT / "panel.md"))
    if not panel.exists():
        return True, "未安装面板（panel.md 缺失）", ["面板未安装：复制 panel.example.md 为 panel.md 后跑 hudctl check"]
    try:
        proc = subprocess.run([sys.executable, str(ROOT / "hudctl.py"), "check", "--json"],
                              capture_output=True, text=True, encoding="utf-8",
                              errors="replace", timeout=120)
    except Exception as exc:  # noqa: BLE001
        return False, f"{type(exc).__name__}: {exc}", []
    try:
        data = json.loads(proc.stdout)
    except Exception:
        return False, (proc.stdout or proc.stderr or "")[-200:], []
    if not isinstance(data, dict):
        return False, "check --json 的输出不是对象", []
    errors = [str(item) for item in (data.get("errors") or [])]
    notices = [str(item) for item in (data.get("notices") or [])]
    if errors:
        return False, "；".join(errors)[:240], notices
    return True, ("；".join(notices)[:160] if notices else "格式正常"), notices


def check() -> tuple[list[dict], bool, list[str]]:
    results: list[dict] = []
    notices: list[str] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append({"check": name, "ok": ok, "detail": detail})

    add("modules:hud_panel", hud_panel is not None,
        "缺 hud_panel.py：注入会整块跳过（fail-closed）")

    if SOURCE is None:
        # 找不到源码 ≠ 机制坏了：profile 家目录、仅装运行包、源码被挪走都可能这样。
        # 只作状态提示，不写 error 进共享 state（否则所有档每轮都会告警）。
        note = (f"未找到 Hermes 源码（{HERMES_HOME} 及其上两级都没有 hermes-agent）："
                "跳过源码检查，机制是否可用以端到端注入为准")
        notices.append(note)
        add("source:pre_llm_call 事件", True, "跳过：未找到源码根")
        add("source:上下文注入点", True, "跳过：未找到源码根")
    else:
        hits = _find_event()
        add("source:pre_llm_call 事件", bool(hits),
            f"在 {', '.join(SEARCH_GLOBS)} 里没找到 pre_llm_call" if not hits else ", ".join(hits[:4]))

        turn_src = _read(SOURCE / "agent" / "turn_context.py")
        add("source:上下文注入点", ("pre_llm_call" in turn_src) and ("plugin_user_context" in turn_src),
            "agent/turn_context.py 里找不到 pre_llm_call / plugin_user_context")

    cfg = _read(PROFILE_DIR / "config.yaml")
    has_hook = "pre_llm_call" in cfg and "hud_inject.py" in cfg
    add("config:hooks.pre_llm_call", has_hook, f"{PROFILE_DIR / 'config.yaml'} 缺声明的钩子")

    allow = _read(PROFILE_DIR / "shell-hooks-allowlist.json")
    approved = "pre_llm_call" in allow and "hud_inject.py" in allow
    add("allowlist:已同意", approved, "该档没有 hud_inject.py 的 pre_llm_call 批准记录")

    ok_e2e, detail_e2e = _e2e()
    add("e2e:注入返回 context", ok_e2e, detail_e2e)

    ok_panel, detail_panel, panel_notices = _panel_check()
    add("panel:格式自检", ok_panel, detail_panel)
    notices.extend(panel_notices)

    return results, all(r["ok"] for r in results), notices


def _write_state(payload: dict) -> None:
    """原子写：同目录临时文件 + os.replace；失败时清掉临时文件。"""
    data = json.dumps(payload, ensure_ascii=False, indent=2)
    tmp = STATE_FILE.with_name(f"{STATE_FILE.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(data, encoding="utf-8")
        os.replace(tmp, STATE_FILE)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def main() -> int:
    if STATE.exists() and not STATE.is_dir():
        print(f"✗ HUD_STATE 指向的是文件，应该是目录：{STATE}", file=sys.stderr)
        return 2
    try:
        STATE.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"✗ 建不了 state 目录：{STATE}（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 2

    notices: list[str] = []
    try:
        fp = fingerprint()
        results, ok, notices = check()
    except Exception as exc:  # noqa: BLE001 - 自检自己炸了也要落一份 error，不能留旧 ok
        fp = ""
        results = [{"check": "selfcheck:内部异常", "ok": False,
                    "detail": f"{type(exc).__name__}: {exc}"}]
        ok = False

    payload = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hermes_home": str(HERMES_HOME),
        "profile": PROFILE,
        "fingerprint": fp,
        "status": "ok" if ok else "error",
        "ok": ok,
        "notices": notices,
        "checks": results,
    }
    try:
        _write_state(payload)
    except Exception as exc:  # noqa: BLE001
        print(f"✗ 写不进自检状态：{STATE_FILE}（{type(exc).__name__}: {exc}）", file=sys.stderr)
        return 2
    print(json.dumps({"ok": ok, "fingerprint": fp,
                      "failed": [r["check"] for r in results if not r["ok"]],
                      "notices": notices}, ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
