#!/usr/bin/env python3
"""hud_inject.py — 每轮注入一段「会话面板」到模型上下文（Hermes pre_llm_call 钩子）。

机制：Hermes 在每次模型调用前触发 `pre_llm_call`，把本轮信息以 JSON 从 stdin 交进来；
本脚本读面板文件，满足「未过期 + 会话匹配 + 能安全解析」就把面板作为
`{"context": "..."}` 打到 stdout，Hermes 会把它追加到本轮用户消息后面。
不满足就一声不响退出。

面板结构（同目录 panel.md）：
    session_id: any            # any = 所有会话（含以后新建的）；或填具体会话 id
    expires_at: 2026-12-31T07:00:00+08:00
    title: 显示名
    enabled: true

其后是正文（注入内容本身）。

fail-closed（宁可整块不注入，也不注入半成品/元数据）：
  · payload 没有 session_id：跳过（空值不等于 any）
  · 面板头部有未知键、session_id 为空、代码围栏未闭合、缺 hud_panel.py：跳过并 log

退出码永远 0：钩子坏了不该拦住正常对话（fail-open）。
环境变量：HUD_PANEL / HUD_LOG / HUD_STATE 可覆盖默认路径。
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.abspath(__file__))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
try:
    import hud_panel  # 同目录；元数据 + 条目作用域的唯一解析实现
except Exception:  # pragma: no cover - 缺模块时整块跳过（fail-closed），绝不全员原样注入
    hud_panel = None

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
MAX_PANEL_BYTES = 200_000  # 面板不该有这么大；上限防误放巨型文件
HERMES_HOME = (hud_panel.hermes_home() if hud_panel is not None
               else (os.environ.get("HERMES_HOME") or os.path.join(
                   os.environ.get("LOCALAPPDATA") or os.path.expanduser("~/AppData/Local"), "hermes")))
STATE_FILE = os.path.join(os.environ.get("HUD_STATE") or os.path.join(ROOT, "state"), "selfcheck.json")

META_KEYS = {"session_id", "expires_at", "title", "enabled"}


def _mech_fingerprint() -> str:
    """与 selfcheck.py 同一实现（hud_panel.mech_fingerprint）：内容哈希 + 同一家目录口径。"""
    if hud_panel is None:
        return ""
    return hud_panel.mech_fingerprint(HERMES_HOME)


def _selfcheck_note() -> str:
    """更新后未自检/自检失败时给一行提醒；正常时为空。"""
    try:
        with open(STATE_FILE, encoding="utf-8") as fh:
            state = json.load(fh)
    except Exception:
        return "⚠ 机制未自检：先跑 python selfcheck.py"
    if not isinstance(state, dict):
        return "⚠ 机制未自检：先跑 python selfcheck.py"
    if state.get("status") == "error" or not state.get("ok"):
        return "⚠ 机制自检失败：看 state/selfcheck.json 并适配新版"
    if state.get("fingerprint") != _mech_fingerprint():
        return "⚠ Hermes 已更新、机制未复检：先跑 python selfcheck.py，失败则按提示适配新版"
    return ""


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


def _strip_comment(value: str) -> str:
    """元数据行里的行内注释（空白 + #）去掉；正文不做处理。"""
    if hud_panel is not None:
        return hud_panel.strip_comment(value)
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
    """(元数据, 正文)。与 hudctl 共用 hud_panel.split_meta，避免两套解析漂移。"""
    if hud_panel is not None:
        meta, body, _unknown = hud_panel.split_meta(text)
        return meta, body
    # 兜底（缺 hud_panel 时只用于兼容调用；注入主流程已在缺模块时整块跳过）
    meta, lines, body_start = {}, text.splitlines(), 0
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
    return meta, "\n".join(lines[body_start:]).strip()


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        log(f"skip bad-payload type={type(payload).__name__}")
        return 0
    session_id = str(payload.get("session_id") or "").strip()
    profile = str(payload.get("profile") or "").strip()
    if not session_id:
        # 「没写」不等于 any：没有会话标识就无法判断该不该注入，跳过而不是全员注入
        log(f"skip no-session profile={profile}")
        return 0
    if hud_panel is None:
        log(f"skip degenerate-panel reason=no-hud-panel session={session_id} profile={profile}")
        return 0
    unsafe = _reject_unsafe(PANEL)
    if unsafe:
        log(f"skip unsafe-panel reason={unsafe} session={session_id}")
        return 0
    try:
        if os.path.getsize(PANEL) > MAX_PANEL_BYTES:
            log(f"skip oversized-panel session={session_id} profile={profile}")
            return 0
    except OSError:
        pass
    try:
        with open(PANEL, encoding="utf-8") as fh:
            text = fh.read(MAX_PANEL_BYTES)
    except FileNotFoundError:
        log(f"skip no-panel session={session_id} profile={profile}")
        return 0
    except (OSError, UnicodeDecodeError) as exc:
        log(f"skip unreadable-panel session={session_id} profile={profile} err={type(exc).__name__}")
        return 0
    text = text.lstrip("\ufeff")
    meta, body, unknown = hud_panel.split_meta(text)
    if unknown:
        # 头部有不认识的键：不把元数据当正文注入，整块跳过（hudctl check 会报错提示）
        log(f"skip unknown-meta keys={','.join(unknown)} session={session_id} profile={profile}")
        return 0
    if meta.get("enabled", "true").lower() in {"0", "false", "no", "off"}:
        log(f"skip disabled session={session_id} profile={profile}")
        return 0
    scope = (meta.get("session_id") or "").strip()
    if not scope:
        log(f"skip empty-scope session={session_id} profile={profile}")
        return 0
    if scope.lower() not in {"any", "*"} and scope != session_id:
        log(f"skip other-session scope={scope} session={session_id} profile={profile}")
        return 0
    try:
        raw = (meta.get("expires_at") or "").strip()
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
    degenerate = hud_panel.degenerate_reason(body)
    if degenerate:
        # 代码围栏没闭合 → 条目边界不可信，注解可能原样漏进上下文；整块跳过
        log(f"skip degenerate-panel reason={degenerate} session={session_id} profile={profile}")
        return 0
    bad_scopes = len(hud_panel.invalid_scopes(body))
    body, kept, total = hud_panel.filter_body(body, session_id, profile)
    if kept == 0:
        log(f"skip no-entry-for-session session={session_id} profile={profile} "
            f"entries={total} invalid_scope={bad_scopes}")
        return 0
    title = meta.get("title", "会话面板")
    stamp = expires.astimezone(CST).strftime("%m-%d %H:%M")
    who = profile or "default"
    hint = ("default bot：优先用 Hermes 官方机制（delegate_task / cron / kanban）"
            if who == "default" else
            f"{who} bot：优先用 OTUI（build/open-tui-orchestrator）")
    block = f"[HUD·{title}] 生效至 {stamp}（{who}）\n{body}\n\n执行机制建议：{hint}"
    note = _selfcheck_note()
    if note:
        block += "\n" + note
    if len(block) > MAX_CHARS:
        block = block[:MAX_CHARS].rstrip() + "\n…（面板过长已截断）"
    log(f"inject session={session_id} profile={profile} chars={len(block)} "
        f"entries={kept}/{total}" + (f" invalid_scope={bad_scopes}" if bad_scopes else ""))
    print(json.dumps({"context": block}, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    try:
        code = main()
    except SystemExit:
        raise
    except BaseException as exc:  # noqa: BLE001 - 钩子绝不因内部异常拦住对话
        log(f"skip internal-error {type(exc).__name__}")
        code = 0
    sys.exit(code)
