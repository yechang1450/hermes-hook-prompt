#!/usr/bin/env python3
"""selfcheck.py — 检查 hermes-hook-prompt 依赖的官方机制是否仍然可用。

Hermes 每次更新后，官方 hooks 的实现细节可能变化。本脚本做一次「机制体检」：

  1. 源码：`agent/shell_hooks.py` 仍认识 `pre_llm_call` 事件
  2. 源码：`agent/turn_context.py` 仍把 hook 返回值当 `plugin_user_context` 注入
  3. 配置：本档 config.yaml 有 `hooks.pre_llm_call` 且指向 `hud_inject.py`
  4. 同意：allowlist 里有 `pre_llm_call` + 该命令的批准记录
  5. 端到端：用样例 payload 跑 `hud_inject.py`，能拿到非空 `context`
  6. 面板：`hudctl.py check` 通过

结果写 `state/selfcheck.json`（机制指纹 + 各项结果）；指纹变了或任何一项失败，
`hud_inject.py` 会在注入尾行提醒「先跑 selfcheck 并适配」。

退出码：0 全绿；2 有失败（需要适配新版本）。
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent

for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():  # 管道/CI 下统一 UTF-8，避免 Windows 默认代码页报错
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
STATE = Path(os.environ.get("HUD_STATE") or (ROOT / "state"))
STATE_FILE = STATE / "selfcheck.json"
HERMES_HOME = Path(os.environ.get("HERMES_HOME") or (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "hermes"))
SOURCE = HERMES_HOME / "hermes-agent"
PROFILE = os.environ.get("HUD_PROFILE", "default")


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


MECH_FILES = (
    "agent/turn_context.py",
    "hermes_cli/plugins_dispatch.py",
    "hermes_cli/hooks.py",
    "agent/shell_hooks.py",
)
SEARCH_GLOBS = ("agent/*.py", "hermes_cli/*.py", "hermes_cli/subcommands/*.py")


def _find_event() -> list[str]:
    """在有限范围里找 pre_llm_call 的落点（事件可能换文件）。"""
    hits = []
    for pattern in SEARCH_GLOBS:
        for path in SOURCE.glob(pattern):
            if path.is_file() and "pre_llm_call" in _read(path):
                hits.append(str(path.relative_to(SOURCE)))
    return hits


def fingerprint() -> str:
    """机制指纹：关键源码文件的大小+mtime。更新/迁移后必变，平时稳定。"""
    parts = []
    for rel in MECH_FILES:
        p = SOURCE / rel
        try:
            st = p.stat()
            parts.append(f"{rel}:{st.st_size}:{st.st_mtime_ns}")
        except OSError:
            parts.append(f"{rel}:missing")
    return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def check() -> tuple[list[dict], bool]:
    results: list[dict] = []

    def add(name: str, ok: bool, detail: str = "") -> None:
        results.append({"check": name, "ok": ok, "detail": detail})

    hits = _find_event()
    add("source:pre_llm_call 事件", bool(hits),
        f"在 {', '.join(SEARCH_GLOBS)} 里没找到 pre_llm_call" if not hits else ", ".join(hits[:4]))

    turn_src = _read(SOURCE / "agent" / "turn_context.py")
    add("source:上下文注入点", ("pre_llm_call" in turn_src) and ("plugin_user_context" in turn_src),
        "agent/turn_context.py 里找不到 pre_llm_call / plugin_user_context")

    profile_dir = HERMES_HOME if PROFILE == "default" else HERMES_HOME / "profiles" / PROFILE
    cfg = _read(profile_dir / "config.yaml")
    has_hook = "pre_llm_call" in cfg and "hud_inject.py" in cfg
    add("config:hooks.pre_llm_call", has_hook, f"{profile_dir / 'config.yaml'} 缺声明的钩子")

    allow = _read(profile_dir / "shell-hooks-allowlist.json")
    approved = "pre_llm_call" in allow and "hud_inject.py" in allow
    add("allowlist:已同意", approved, "该档没有 hud_inject.py 的 pre_llm_call 批准记录")

    panel = Path(os.environ.get("HUD_PANEL") or (ROOT / "panel.md"))
    env = dict(os.environ, HUD_PANEL=str(panel), HUD_LOG=str(STATE / "selfcheck-inject.log"))
    try:
        proc = subprocess.run(
            [sys.executable, str(ROOT / "hud_inject.py")],
            input=json.dumps({"session_id": "selfcheck", "profile": PROFILE}),
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            env=env, timeout=60,
        )
        ctx = ""
        if proc.stdout.strip():
            try:
                ctx = json.loads(proc.stdout)["context"]
            except Exception:
                ctx = ""
        add("e2e:注入返回 context", bool(ctx), (proc.stderr or "")[-200:])
    except Exception as exc:  # noqa: BLE001
        add("e2e:注入返回 context", False, f"{type(exc).__name__}: {exc}")

    try:
        ctl = subprocess.run([sys.executable, str(ROOT / "hudctl.py"), "check"],
                             capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
        add("panel:格式自检", ctl.returncode == 0, (ctl.stdout + ctl.stderr)[-200:])
    except Exception as exc:  # noqa: BLE001
        add("panel:格式自检", False, f"{type(exc).__name__}: {exc}")

    return results, all(r["ok"] for r in results)


def main() -> int:
    STATE.mkdir(parents=True, exist_ok=True)
    fp = fingerprint()
    results, ok = check()
    payload = {
        "checked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hermes_home": str(HERMES_HOME),
        "profile": PROFILE,
        "fingerprint": fp,
        "ok": ok,
        "checks": results,
    }
    STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": ok, "fingerprint": fp, "failed": [r["check"] for r in results if not r["ok"]]},
                     ensure_ascii=False))
    return 0 if ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
