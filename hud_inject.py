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
        parent = os.path.dirname(LOG)
        if parent:
            os.makedirs(parent, exist_ok=True)
        try:
            if os.path.getsize(LOG) > MAX_LOG_BYTES:
                os.replace(LOG, LOG + ".1")
        except OSError:
            pass
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(CST).isoformat(timespec='seconds')} {line}\n")
    except Exception:
        pass


META_KEYS = {"session_id", "expires_at", "title", "enabled"}


def _strip_comment(value: str) -> str:
    """元数据行里的行内注释（空白 + #）去掉；正文不做处理。"""
    for sep in (" #", "\t#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    return value.strip()


def _reject_unsafe(path: str) -> str:
    """返回拒绝原因；安全则返回空串。防软链/重解析点/非普通文件被当面板读。"""
    if not os.path.exists(path):
        return ""  # 交给 open() 报缺文件，日志记 skip no-panel
    try:
        st = os.lstat(path)
    except OSError as exc:
        return f"lstat:{type(exc).__name__}"
    if os.path.islink(path) or getattr(st, "st_file_attributes", 0) & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
        return "symlink-or-reparse"
    import stat as _stat
    if not _stat.S_ISREG(st.st_mode):
        return "not-regular-file"
    return ""


def parse_panel(text: str):
    """前 24 行内、连续的非空元数据行；遇到空行或非元数据行即视为正文开始。"""
    meta, lines = {}, text.splitlines()
    body_start = 0
    for i, line in enumerate(lines[:24]):
        stripped = line.strip()
        if not stripped:
            if meta:
                break
            body_start = i + 1
            continue
        if ":" in line:
            key, value = line.split(":", 1)
            name = key.strip().lower()
            if name in META_KEYS:
                meta[name] = _strip_comment(value)
                body_start = i + 1
                continue
        break
    body = "\n".join(lines[body_start:]).strip()
    return meta, body


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        log(f"skip bad-payload type={type(payload).__name__}")
        return 0
    session_id = str(payload.get("session_id") or "")
    profile = str(payload.get("profile") or "")
    unsafe = _reject_unsafe(PANEL)
    if unsafe:
        log(f"skip unsafe-panel reason={unsafe} session={session_id}")
        return 0
    try:
        with open(PANEL, encoding="utf-8") as fh:
            text = fh.read(200_000)  # 面板不该有这么大；上限防误放巨型文件
    except FileNotFoundError:
        log(f"skip no-panel session={session_id} profile={profile}")
        return 0
    except (OSError, UnicodeDecodeError) as exc:
        log(f"skip unreadable-panel session={session_id} profile={profile} err={type(exc).__name__}")
        return 0
    text = text.lstrip("\ufeff")
    meta, body = parse_panel(text)
    if meta.get("enabled", "true").lower() in {"0", "false", "no", "off"}:
        log(f"skip disabled session={session_id} profile={profile}")
        return 0
    scope = meta.get("session_id", "any").strip()
    if scope not in {"", "any", "*"} and scope != session_id:
        log(f"skip other-session scope={scope} session={session_id} profile={profile}")
        return 0
    try:
        raw = meta.get("expires_at", "").strip()
        if raw.endswith("Z"):
            raw = raw[:-1] + "+00:00"
        expires = datetime.fromisoformat(raw)
    except Exception:
        log(f"skip bad-expiry meta={meta.get('expires_at')!r} session={session_id} profile={profile}")
        return 0
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    now = datetime.now(CST)
    if now >= expires:
        log(f"skip expired at {expires.isoformat()} session={session_id} profile={profile}")
        return 0
    if not body:
        log(f"skip empty-body session={session_id} profile={profile}")
        return 0
    title = meta.get("title", "会话面板")
    stamp = expires.astimezone(CST).strftime("%m-%d %H:%M")
    block = f"[HUD·{title}] 生效至 {stamp}（{profile or 'default'}）\n{body}"
    if len(block) > MAX_CHARS:
        block = block[:MAX_CHARS].rstrip() + "\n…（面板过长已截断）"
    log(f"inject session={session_id} profile={profile} chars={len(block)}")
    print(json.dumps({"context": block}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
