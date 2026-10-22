#!/usr/bin/env python3
"""hud_inject.py — 每轮注入一段「会话面板」到模型上下文（Hermes pre_llm_call 钩子）。

机制：Hermes 在每次模型调用前触发 `pre_llm_call`，把本轮信息以 JSON 从 stdin 交进来；
本脚本读面板文件，满足「未过期 + 会话匹配」就把面板作为 `{"context": "..."}` 打到 stdout，
Hermes 会把它追加到本轮用户消息后面。不满足就一声不响退出。

面板结构（同目录 panel.md）：
    session_id: any            # any = 所有会话（含以后新建的）；或填具体会话 id
    expires_at: 2026-12-31T07:00:00+08:00
    title: 显示名
    enabled: true

其后是正文（注入内容本身）。

退出码永远 0：钩子坏了不该拦住正常对话（fail-open）。
环境变量：HUD_PANEL / HUD_LOG 可覆盖默认路径。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
PANEL = os.environ.get("HUD_PANEL", os.path.join(ROOT, "panel.md"))
LOG = os.environ.get("HUD_LOG", os.path.join(ROOT, "hud.log"))
CST = timezone(timedelta(hours=8))
for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():  # 管道/CI 下统一 UTF-8，避免 Windows 默认代码页报错
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

MAX_CHARS = 1600
MAX_LOG_BYTES = 1_000_000


def log(line: str) -> None:
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        try:
            if os.path.getsize(LOG) > MAX_LOG_BYTES:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(CST).isoformat(timespec='seconds')} {line}\n")
    except Exception:
        pass


def parse_panel(text: str):
    meta, lines = {}, text.splitlines()
    body_start = 0
    for i, line in enumerate(lines[:8]):
        if ":" in line and not line.strip().startswith("#"):
            key, value = line.split(":", 1)
            if key.strip().lower() in {"session_id", "expires_at", "title", "enabled"}:
                meta[key.strip().lower()] = value.strip()
                body_start = i + 1
                continue
        if line.strip():
            break
    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    session_id = str(payload.get("session_id") or "")
    profile = str(payload.get("profile") or "")
    try:
        with open(PANEL, encoding="utf-8") as fh:
            text = fh.read(200_000)  # 面板不该有这么大；上限防误放巨型文件
    except OSError:
        log(f"skip no-panel session={session_id} profile={profile}")
        return 0
    text = text.lstrip("\ufeff")
    meta, body = parse_panel(text)
    if meta.get("enabled", "true").lower() in {"0", "false", "no", "off"}:
        log(f"skip disabled session={session_id}")
        return 0
    scope = meta.get("session_id", "any").strip()
    if scope not in {"", "any", "*"} and scope != session_id:
        log(f"skip other-session scope={scope} session={session_id}")
        return 0
    try:
        raw = meta.get("expires_at", "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        expires = datetime.fromisoformat(raw)
    except Exception:
        log(f"skip bad-expiry meta={meta.get('expires_at')!r}")
        return 0
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    now = datetime.now(CST)
    if now >= expires:
        log(f"skip expired at {expires.isoformat()} session={session_id}")
        return 0
    if not body:
        log(f"skip empty-body session={session_id}")
        return 0
    title = meta.get("title", "会话面板")
    stamp = expires.strftime("%m-%d %H:%M")
    block = f"[HUD·{title}] 生效至 {stamp}（{profile or 'default'}）\n{body}"
    if len(block) > MAX_CHARS:
        block = block[:MAX_CHARS].rstrip() + "\n…（面板过长已截断）"
    log(f"inject session={session_id} profile={profile} chars={len(block)}")
    print(json.dumps({"context": block}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
