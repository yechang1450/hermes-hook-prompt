#!/usr/bin/env python3
"""hudctl.py — 管理会话面板（HUD）的一个小命令行。

面板 = 本脚本同目录的 panel.md，头部四行元数据（session_id / expires_at / title / enabled），
其后是注入正文。pre_llm_call 钩子每轮读它；本脚本只负责**人来操作**：

    python hudctl.py status                  # 看当前面板：作用范围、到期时间、剩多久
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
TTL_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")


def parse_ttl(text: str) -> timedelta:
    m = TTL_RE.match(text.strip().lower())
    if not m:
        raise SystemExit(f"TTL 写法不认：{text}（例：30m / 12h / 2d）")
    value, unit = float(m.group(1)), m.group(2)
    return timedelta(minutes=value) if unit == "m" else timedelta(hours=value) if unit == "h" else timedelta(days=value)


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
        raise SystemExit(f"面板不存在：{PANEL}")
    return split_panel(PANEL.read_text(encoding="utf-8"))


def write_panel(meta: dict[str, str], body: str) -> None:
    head = "\n".join(f"{k}: {meta[k]}" for k in ("session_id", "expires_at", "title", "enabled") if k in meta)
    PANEL.write_text(f"{head}\n\n{body}\n", encoding="utf-8")


def _state_db() -> Path:
    home = Path(os.environ.get("HERMES_HOME") or (Path(os.environ["LOCALAPPDATA"]) / "hermes"))
    return home / "state.db"


def cmd_sessions(args) -> int:
    """列出最近的会话（id / 标题 / 档 / 最后活动），方便挑一个挂面板。"""
    import sqlite3
    db = _state_db()
    if not db.exists():
        raise SystemExit(f"找不到会话库：{db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "select id, title, profile_name, last_activity_at, message_count from sessions "
        "where coalesce(archived,0)=0 and coalesce(hidden,0)=0 and id not like 'cron_%' "
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
    try:
        expires = datetime.fromisoformat(meta.get("expires_at", ""))
    except ValueError:
        expires = None
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
