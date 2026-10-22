#!/usr/bin/env python3
"""hudctl.py — 管理会话面板（HUD）的一个小命令行。

面板 = 本脚本同目录的 panel.md，头部四行元数据（session_id / expires_at / title / enabled），
其后是注入正文。pre_llm_call 钩子每轮读它；本脚本只负责**人来操作**：

    python hudctl.py status                  # 看当前面板：作用范围、到期时间、剩多久
    python hudctl.py check                   # 校验格式（缺元数据/到期写错/正文超长/已过期）
    python hudctl.py set --ttl 12h           # 换正文并续期（正文从 --body 文件或 stdin 读）
    python hudctl.py off / on                # 暂停 / 恢复注入（不删面板）
    python hudctl.py snapshot                # 存一份当天的快照到 snapshots/
    python hudctl.py restore latest --ttl 8h # 从快照恢复（只能人手动跑）
    python hudctl.py prune --keep 14         # 只留最近 N 份快照

设计边界：脚本不自动恢复、不自动续期——恢复必须是人手动执行（用户定的规矩）。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
PANEL = ROOT / "panel.md"
SNAPS = ROOT / "snapshots"
CST = timezone(timedelta(hours=8))
try:
    if not sys.stdout.isatty():  # 管道/CI 下统一 UTF-8，避免 Windows 默认代码页报错
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

TTL_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")


def parse_ttl(text: str) -> timedelta:
    m = TTL_RE.match(text.strip().lower())
    if not m:
        raise SystemExit(f"TTL 写法不认：{text}（例：30m / 12h / 2d）")
    value, unit = float(m.group(1)), m.group(2)
    return timedelta(minutes=value) if unit == "m" else timedelta(hours=value) if unit == "h" else timedelta(days=value)


def parse_expiry(raw: str):
    """接受 ISO8601；容忍结尾 Z。解析失败返回 None。"""
    raw = (raw or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


def split_panel(text: str) -> tuple[dict[str, str], str]:
    meta, body_lines, in_meta = {}, [], True
    for line in text.splitlines():
        if in_meta:
            key, sep, value = line.partition(":")
            if sep and key.strip().lower() in {"session_id", "expires_at", "title", "enabled"}:
                meta[key.strip().lower()] = value.strip()
                continue
            if line.strip() == "":
                continue
            in_meta = False
        body_lines.append(line)
    return meta, "\n".join(body_lines).strip()


def read_panel() -> tuple[dict[str, str], str]:
    if not PANEL.exists():
        sample = ROOT / "panel.example.md"
        if sample.exists():
            PANEL.write_text(sample.read_text(encoding="utf-8").lstrip("\ufeff"), encoding="utf-8")
            print(f"（面板不存在，已从 panel.example.md 复制：{PANEL}）")
        else:
            raise SystemExit(f"面板不存在：{PANEL}（先 cp panel.example.md panel.md）")
    return split_panel(PANEL.read_text(encoding="utf-8").lstrip("\ufeff"))


def write_panel(meta: dict[str, str], body: str) -> None:
    meta = dict(meta)
    meta.setdefault("session_id", "any")
    meta.setdefault("title", "会话面板")
    meta.setdefault("enabled", "true")
    if "expires_at" not in meta:
        raise SystemExit("面板缺少 expires_at，无法写入（先补一行到期时间）")
    head = "\n".join(f"{k}: {meta[k]}" for k in ("session_id", "expires_at", "title", "enabled"))
    PANEL.write_text(f"{head}\n\n{body}\n", encoding="utf-8")


def _hermes_home() -> Path:
    """HERMES_HOME → %LOCALAPPDATA%/hermes → ~/.local/share/hermes（跨平台兜底）。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".local" / "share" / "hermes"


def _state_db() -> Path:
    return _hermes_home() / "state.db"


def cmd_sessions(args) -> int:
    """列出最近的会话（id / 标题 / 档 / 最后活动），方便挑一个挂面板。"""
    import sqlite3
    db = _state_db()
    if not db.exists():
        raise SystemExit(f"找不到会话库：{db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        rows = con.execute(
            "select id, title, profile_name, last_activity_at, message_count from sessions "
            "where coalesce(archived,0)=0 and coalesce(hidden,0)=0 and id not like 'cron_%' "
            "order by last_activity_at desc limit ?", (args.limit,)
        ).fetchall()
    except sqlite3.Error:
        # 老版本/改过 schema 的库：退回最小列集
        rows = con.execute(
            "select id, title, null, last_activity_at, 0 from sessions "
            "order by last_activity_at desc limit ?", (args.limit,)
        ).fetchall()
    print(f"{'会话 id':<26} {'档':<12} {'消息':>4}  最后活动     标题")
    for sid, title, prof, ts, n in rows:
        when = datetime.fromtimestamp(ts, CST).strftime("%m-%d %H:%M") if ts else "-"
        print(f"{sid:<26} {(prof or 'default'):<12} {n or 0:>4}  {when}  {(title or '')[:28]}")
    return 0


def cmd_use(args) -> int:
    """把面板指向另一个会话（按 id 或标题子串匹配）。"""
    import sqlite3
    db = _state_db()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select id, title from sessions where coalesce(archived,0)=0 "
        "order by last_activity_at desc limit 300"
    ).fetchall()
    target = args.target
    hits = [r for r in rows if r[0] == target] or [r for r in rows if target.lower() in (r[1] or "").lower()]
    if not hits:
        raise SystemExit(f"没找到会话：{target}（先跑 `python hudctl.py sessions`）")
    if len(hits) > 1:
        listing = "\n".join(f"  {sid}  {title}" for sid, title in hits[:8])
        raise SystemExit(f"匹配到多个会话，请给完整 id：\n{listing}")
    sid, title = hits[0]
    meta, body = read_panel()
    meta["session_id"] = sid
    meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
    meta["enabled"] = "true"
    write_panel(meta, body)
    print(f"已挂到：{sid}（{title}） 到期 {meta['expires_at']}")
    return 0


def cmd_status(_args) -> int:
    meta, body = read_panel()
    expires = parse_expiry(meta.get("expires_at", ""))
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    now = datetime.now(CST)
    left = (expires - now) if expires else None
    print(f"面板：{PANEL}")
    print(f"标题：{meta.get('title', '-')}")
    print(f"范围：{meta.get('session_id', 'any')}")
    print(f"状态：{'启用' if meta.get('enabled', 'true').lower() not in {'false','0','no','off'} else '暂停'}")
    print(f"到期：{expires.isoformat(timespec='minutes') if expires else '解析失败'}"
          + (f"（剩 {left.days}天{left.seconds//3600}小时{(left.seconds%3600)//60}分）" if left and left.total_seconds() > 0 else "（已过期）" if left else ""))
    print(f"正文：{len(body)} 字")
    return 0


def cmd_set(args) -> int:
    meta, body = read_panel()
    if args.session:
        meta["session_id"] = args.session
    if args.title:
        meta["title"] = args.title
    if args.body:
        body = Path(args.body).read_text(encoding="utf-8").strip()
    elif not sys.stdin.isatty():
        incoming = sys.stdin.read().strip()
        if incoming:
            body = incoming
    meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
    meta["enabled"] = "true"
    write_panel(meta, body)
    print(f"已更新：范围={meta['session_id']} 到期={meta['expires_at']} 正文={len(body)} 字")
    return 0


META_KEYS = ("session_id", "expires_at", "title", "enabled")


def cmd_check(_args) -> int:
    """校验面板格式；有问题返回 1，方便放进门禁/CI。"""
    problems = []
    if not PANEL.exists():
        print(f"✗ 面板不存在：{PANEL}")
        return 1
    raw = PANEL.read_text(encoding="utf-8")
    if raw.startswith("\ufeff"):
        problems.append("文件带 BOM（注入端能容忍，但建议去掉）")
    meta, body = split_panel(raw.lstrip("\ufeff"))
    for key in META_KEYS:
        if key not in meta:
            problems.append(f"缺少元数据：{key}")
    scope = meta.get("session_id", "")
    if scope and scope not in {"any", "*"} and len(scope) < 8:
        problems.append(f"session_id 看起来不像会话 id：{scope!r}")
    enabled = meta.get("enabled", "true").lower()
    if enabled not in {"true", "false", "1", "0", "yes", "no", "on", "off"}:
        problems.append(f"enabled 取值不合法：{meta.get('enabled')!r}")
    expires = parse_expiry(meta.get("expires_at", ""))
    if expires is None:
        problems.append(f"expires_at 无法解析：{meta.get('expires_at')!r}")
    else:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=CST)
        if expires <= datetime.now(CST):
            problems.append(f"面板已过期：{meta.get('expires_at')}（注入会被跳过）")
    if not body:
        problems.append("正文为空")
    if len(body) > 1600:
        problems.append(f"正文 {len(body)} 字，超过注入上限 1600（会被截断）")
    if problems:
        print("✗ 面板有问题：")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"✓ 面板正常：范围={scope or 'any'} 到期={meta['expires_at']} 正文={len(body)} 字")
    return 0


def cmd_toggle(flag: str):
    def run(_args) -> int:
        meta, body = read_panel()
        meta["enabled"] = flag
        write_panel(meta, body)
        print(f"enabled={flag}")
        return 0
    return run


def cmd_snapshot(_args) -> int:
    SNAPS.mkdir(exist_ok=True)
    meta, body = read_panel()
    target = SNAPS / f"{datetime.now(CST).strftime('%Y-%m-%d')}.md"
    shutil.copy2(PANEL, target)
    print(f"快照：{target}（{len(body)} 字）")
    return 0


def cmd_restore(args) -> int:
    SNAPS.mkdir(exist_ok=True)
    if args.date != "latest" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        raise SystemExit(f"日期格式应为 YYYY-MM-DD 或 latest：{args.date}")
    snaps = sorted(SNAPS.glob("*.md"))
    if not snaps:
        raise SystemExit("没有快照可恢复")
    if args.date == "latest":
        target = snaps[-1]
    else:
        target = SNAPS / f"{args.date}.md"
        if not target.exists():
            raise SystemExit(f"没有这份快照：{target}")
    meta, body = split_panel(target.read_text(encoding="utf-8"))
    if args.session:
        meta["session_id"] = args.session
    meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
    meta["enabled"] = "true"
    write_panel(meta, body)
    print(f"已从 {target.name} 恢复：范围={meta['session_id']} 到期={meta['expires_at']} 正文={len(body)} 字")
    return 0


def cmd_prune(args) -> int:
    snaps = sorted(SNAPS.glob("*.md"))
    drop = snaps[: max(0, len(snaps) - args.keep)]
    for path in drop:
        path.unlink()
    print(f"保留 {min(args.keep, len(snaps))} 份，删除 {len(drop)} 份")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="会话面板（HUD）管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p_sessions = sub.add_parser("sessions")
    p_sessions.add_argument("--limit", type=int, default=15)
    p_sessions.set_defaults(func=cmd_sessions)
    p_use = sub.add_parser("use")
    p_use.add_argument("target", help="会话 id 或标题里的关键字")
    p_use.add_argument("--ttl", default="12h")
    p_use.set_defaults(func=cmd_use)
    p_set = sub.add_parser("set")
    p_set.add_argument("--ttl", default="12h")
    p_set.add_argument("--session", help="any / 会话 id；不给则沿用当前面板")
    p_set.add_argument("--title")
    p_set.add_argument("--body", help="正文文件路径；不给则读 stdin")
    p_set.set_defaults(func=cmd_set)
    sub.add_parser("check").set_defaults(func=cmd_check)
    sub.add_parser("off").set_defaults(func=cmd_toggle("false"))
    sub.add_parser("on").set_defaults(func=cmd_toggle("true"))
    sub.add_parser("snapshot").set_defaults(func=cmd_snapshot)
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("date", help="YYYY-MM-DD 或 latest")
    p_restore.add_argument("--ttl", default="12h")
    p_restore.add_argument("--session")
    p_restore.set_defaults(func=cmd_restore)
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--keep", type=int, default=14)
    p_prune.set_defaults(func=cmd_prune)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
