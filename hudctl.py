#!/usr/bin/env python3
"""hudctl.py — 管理会话面板（HUD）的一个小命令行。

面板 = 本脚本同目录的 panel.md，头部四行元数据（session_id / expires_at / title / enabled），
其后是注入正文。pre_llm_call 钩子每轮读它；本脚本只负责**人来操作**：

    python hudctl.py status                  # 看当前面板：作用范围、到期时间、剩多久
    python hudctl.py check                   # 校验格式（缺元数据/到期写错/正文超长/已过期）
    python hudctl.py set --ttl 12h           # 换正文并续期（正文从 --body 文件或 stdin 读）
    python hudctl.py off / on                # 暂停 / 恢复注入（不删面板）
    python hudctl.py snapshot                # 存一份快照（同日多份加时间戳，不互相覆盖）
    python hudctl.py restore latest --ttl 8h # 从快照恢复（指定日期取当天最新；只能人手动跑）
    python hudctl.py prune --keep 14         # 只留最近 N 份快照

设计边界：脚本不自动恢复、不自动续期——恢复必须是人手动执行（用户定的规矩）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import hud_panel  # 同目录；条目作用域解析
except Exception:  # pragma: no cover
    hud_panel = None

ROOT = Path(__file__).resolve().parent
PANEL = Path(os.environ.get("HUD_PANEL") or (ROOT / "panel.md"))
SNAPS = Path(os.environ.get("HUD_SNAPS") or (ROOT / "snapshots"))
CST = timezone(timedelta(hours=8))
for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():  # 管道/CI 下统一 UTF-8，避免 Windows 默认代码页报错
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

TTL_RE = re.compile(r"^(\d+(?:\.\d+)?)([mhd])$")


MAX_TTL_DAYS = 3650  # 10 年，防溢出


def parse_ttl(text: str) -> timedelta:
    m = TTL_RE.match(text.strip().lower())
    if not m:
        raise SystemExit(f"TTL 写法不认：{text}（例：30m / 12h / 2d）")
    value, unit = float(m.group(1)), m.group(2)
    if value <= 0:
        raise SystemExit(f"TTL 必须大于 0：{text}")
    limit = {"m": MAX_TTL_DAYS * 1440, "h": MAX_TTL_DAYS * 24, "d": MAX_TTL_DAYS}[unit]
    if value > limit:
        raise SystemExit(f"TTL 过大（上限 {MAX_TTL_DAYS} 天）：{text}")
    return (timedelta(minutes=value) if unit == "m"
            else timedelta(hours=value) if unit == "h"
            else timedelta(days=value))


def parse_expiry(raw: str):
    """接受 ISO8601；容忍结尾 Z。解析失败返回 None。"""
    raw = (raw or "").strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        return datetime.fromisoformat(raw)
    except ValueError:
        return None


META_KEYS = ("session_id", "expires_at", "title", "enabled")


def _strip_comment(value: str) -> str:
    for sep in (" #", "\t#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    return value.strip()


def split_panel(text: str) -> tuple[dict[str, str], str]:
    """与 hud_inject.parse_panel 同源：前 24 行连续元数据，空行/非元数据行结束元数据块。"""
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
    return meta, "\n".join(lines[body_start:]).strip()


def read_panel(create: bool = True) -> tuple[dict[str, str], str]:
    if not PANEL.exists() and not create:
        raise SystemExit(f"面板不存在：{PANEL}")
    if not PANEL.exists():
        sample = ROOT / "panel.example.md"
        if sample.exists():
            PANEL.write_text(sample.read_text(encoding="utf-8").lstrip("\ufeff"), encoding="utf-8")
            print(f"（面板不存在，已从 panel.example.md 复制：{PANEL}）")
        else:
            raise SystemExit(f"面板不存在：{PANEL}（先 cp panel.example.md panel.md）")
    try:
        raw = PANEL.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise SystemExit(f"面板不是合法 UTF-8：{PANEL}")
    return split_panel(raw.lstrip("\ufeff"))


def write_panel(meta: dict[str, str], body: str) -> None:
    meta = dict(meta)
    meta.setdefault("session_id", "any")
    meta.setdefault("title", "会话面板")
    meta.setdefault("enabled", "true")
    if "expires_at" not in meta:
        raise SystemExit("面板缺少 expires_at，无法写入（先补一行到期时间）")
    for key in META_KEYS:
        if "\n" in meta[key] or "\r" in meta[key]:
            raise SystemExit(f"元数据 {key} 不能包含换行")
    head = "\n".join(f"{k}: {meta[k]}" for k in META_KEYS)
    if os.path.islink(PANEL):
        raise SystemExit(f"面板是符号链接，拒绝写入：{PANEL}")
    data = f"{head}\n\n{body}\n"
    tmp = PANEL.with_name(f"{PANEL.name}.{os.getpid()}.tmp")
    tmp.write_text(data, encoding="utf-8")
    os.replace(tmp, PANEL)  # 原子替换：注入端不会读到空/半截文件


def _hermes_home() -> Path:
    """HERMES_HOME → %LOCALAPPDATA%/hermes → ~/.local/share/hermes（跨平台兜底）。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return Path(env)
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "hermes"
    return Path.home() / ".local" / "share" / "hermes"


def _state_dbs() -> list[tuple[str, Path]]:
    """default 档 + 每个 profile 自己的 state.db（各档会话库是分开的）。"""
    home = _hermes_home()
    found: list[tuple[str, Path]] = []
    root = home / "state.db"
    if root.exists():
        found.append(("default", root))
    profiles = home / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if not entry.is_dir() or entry.name == ".deleted":
                continue
            db = entry / "state.db"
            if db.exists():
                found.append((entry.name, db))
    return found


def cmd_sessions(args) -> int:
    """列出所有档最近的会话（id / 档 / 最后活动 / 标题）。"""
    import sqlite3
    dbs = _state_dbs()
    if not dbs:
        raise SystemExit(f"找不到任何会话库（HERMES_HOME={_hermes_home()}）")
    rows = []
    for profile, db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error as exc:
            print(f"（跳过 {profile}：{exc}）")
            continue
        try:
            part = con.execute(
                "select id, title, last_activity_at, message_count from sessions "
                "where coalesce(archived,0)=0 and coalesce(hidden,0)=0 and id not like 'cron_%' "
                "order by last_activity_at desc limit ?", (args.limit,)
            ).fetchall()
        except sqlite3.Error:
            part = con.execute(
                "select id, title, last_activity_at, 0 from sessions "
                "order by last_activity_at desc limit ?", (args.limit,)
            ).fetchall()
        finally:
            con.close()
        rows.extend((sid, title, profile, ts, n) for sid, title, ts, n in part)
    rows.sort(key=lambda r: r[3] or 0, reverse=True)
    print(f"{'会话 id':<26} {'档':<12} {'消息':>4}  最后活动     标题")
    for sid, title, profile, ts, n in rows[: args.limit]:
        when = datetime.fromtimestamp(ts, CST).strftime("%m-%d %H:%M") if ts else "-"
        print(f"{sid:<26} {profile:<12} {n or 0:>4}  {when}  {(title or '')[:28]}")
    return 0




def _profile_cfg(profile: str) -> Path:
    home = _hermes_home()
    return home / "config.yaml" if profile == "default" else home / "profiles" / profile / "config.yaml"


def _profile_allow(profile: str) -> Path:
    home = _hermes_home()
    return (home / "shell-hooks-allowlist.json" if profile == "default"
            else home / "profiles" / profile / "shell-hooks-allowlist.json")


def _hook_commands(profile: str) -> list[str]:
    """config 里 **pre_llm_call 事件下**、未注释的、command 指向 hud_inject 的命令。

    要跟事件走：只声明在 pre_tool_call 下的命令不算数（否则陈旧配置会假装生效）。
    同时支持块写法与 `pre_llm_call: [{command: …}]` 行内写法。
    """
    try:
        text = _profile_cfg(profile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[str] = []
    event: str | None = None
    in_hooks = False
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if re.match(r"^hooks:\s*$", line):
            in_hooks, event = True, None
            continue
        if in_hooks and not line[:1].isspace():
            in_hooks, event = False, None  # 回到顶层键，hooks 段结束
        if not in_hooks:
            continue
        m_event = re.match(r"^\s{2}([A-Za-z_][\w]*):", line)
        if m_event:
            event = m_event.group(1)
            if event == "pre_llm_call" and "hud_inject" in stripped:
                # 行内写法：pre_llm_call: [{command: python …}]
                for cmd in re.findall(r"command\s*:\s*[\"']?([^\"',}\]]+)", stripped):
                    out.append(cmd.strip())
            continue
        if event != "pre_llm_call" or "hud_inject" not in stripped:
            continue
        m = re.search(r"command:\s*[\"']?(.+?)[\"']?\s*$", stripped)
        if m:
            out.append(m.group(1).strip())
    return out


def _allowed_commands(profile: str) -> set[str]:
    """allowlist 里 pre_llm_call 且指向 hud_inject 的命令（精确字符串，与 Hermes 同口径）。"""
    try:
        data = json.loads(_profile_allow(profile).read_text(encoding="utf-8", errors="replace"))
    except (OSError, ValueError):
        return set()
    out = set()
    for entry in (data.get("approvals") or []) if isinstance(data, dict) else []:
        if not isinstance(entry, dict):
            continue
        if entry.get("event") != "pre_llm_call":
            continue
        command = str(entry.get("command") or "").strip()
        if "hud_inject" in command:
            out.add(command)
    return out


def _hook_state(profile: str) -> tuple[bool, bool]:
    """(config 里声明了 hud_inject, allowlist 里对该命令有同意)"""
    cmds = _hook_commands(profile)
    allowed = _allowed_commands(profile)
    return bool(cmds), any(c in allowed for c in cmds)


def _all_sessions(db: Path) -> list[tuple[str, str, float, str]]:
    """(id, 标题, 最后活动, source)；排除 archived/hidden，但包含 cron（它们同样被注入）。"""
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        cols = {row[1] for row in con.execute("pragma table_info(sessions)")}
        source = "source" if "source" in cols else "''"
        where = []
        if "archived" in cols:
            where.append("coalesce(archived,0)=0")
        if "hidden" in cols:
            where.append("coalesce(hidden,0)=0")
        sql = f"select id, title, last_activity_at, {source} from sessions"
        if where:
            sql += " where " + " and ".join(where)
        rows = con.execute(sql).fetchall()
    finally:
        con.close()
    return [(sid, title or "", ts or 0, str(src or "")) for sid, title, ts, src in rows]


def _session_kind(sid: str, source: str) -> str:
    if source == "cron" or sid.startswith("cron_"):
        return "cron"
    if source == "kanban":
        return "kanban"
    if source == "oneshot":
        return "oneshot"
    if source == "subagent":
        return "subagent"
    return "chat"


KIND_CHOICES = ("chat", "cron", "kanban", "oneshot", "subagent")


def _known_profiles() -> list[str]:
    """default + profiles/ 下的档目录；用来校验 --profile 的取值（与 Hermes 的档名一致）。"""
    names = ["default"]
    profiles = _hermes_home() / "profiles"
    if profiles.is_dir():
        for entry in sorted(profiles.iterdir()):
            if entry.is_dir() and not entry.name.startswith("."):
                names.append(entry.name)
    return names


def _split_csv(raw: str) -> list[str]:
    return [part.strip() for part in (raw or "").split(",") if part.strip()]


def parse_profile_filter(raw: str | None) -> list[str]:
    """`--profile a,b` → 档名列表（去重保序）；不认识的档名报错，大小写按原样匹配。"""
    if raw is None:
        return []
    wanted = _split_csv(raw)
    if not wanted:
        raise SystemExit("--profile 不能为空（例：--profile default,ceo）")
    known = _known_profiles()
    bad = [name for name in wanted if name not in known]
    if bad:
        raise SystemExit(f"不认识的档名：{', '.join(bad)}"
                         f"（现有：{', '.join(known) or 'default'}；档名大小写敏感）")
    return list(dict.fromkeys(wanted))


def parse_kind_filter(raw: str | None) -> list[str]:
    """`--kinds chat,cron` → 分类列表（小写归一、去重保序）；非法分类报错。"""
    if raw is None:
        return []
    wanted = _split_csv(raw)
    if not wanted:
        raise SystemExit(f"--kinds 不能为空（可选：{','.join(KIND_CHOICES)}）")
    bad = [name for name in wanted if name.lower() not in KIND_CHOICES]
    if bad:
        raise SystemExit(f"不认识的分类：{', '.join(bad)}（可选：{','.join(KIND_CHOICES)}）")
    return list(dict.fromkeys(name.lower() for name in wanted))


def _filter_coverage(profiles: list[dict], sessions: list[dict],
                     want_profiles: list[str], want_kinds: list[str]):
    """按档/分类收窄盘点结果；每档计数按剩下的会话重算，避免与 totals 打架。"""
    if want_profiles:
        keep = set(want_profiles)
        profiles = [p for p in profiles if p["profile"] in keep]
        sessions = [s for s in sessions if s["profile"] in keep]
    if want_kinds:
        keep_kinds = set(want_kinds)
        sessions = [s for s in sessions if s.get("kind", "chat") in keep_kinds]
    counts: dict[str, list[int]] = {}
    for s in sessions:
        slot = counts.setdefault(s["profile"], [0, 0])
        slot[0] += 1
        slot[1] += 1 if s["effective"] else 0
    for p in profiles:
        total, eff = counts.get(p["profile"], (0, 0))
        p["sessions"], p["effective"], p["skipped"] = total, eff, total - eff
    return profiles, sessions


def _panel_state(meta: dict) -> tuple[bool, bool, str, bool]:
    """(启用, 已过期, 范围, 到期时间是否非法)。到期非法按“全部不生效”算，与注入一致。"""
    enabled = meta.get("enabled", "true").lower() not in {"false", "0", "no", "off"}
    expires = parse_expiry(meta.get("expires_at", ""))
    invalid_expiry = expires is None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    expired = invalid_expiry or bool(expires and expires <= datetime.now(CST))
    scope = (meta.get("session_id") or "any").strip()
    return enabled, expired, scope, invalid_expiry


def _coverage(body: str, meta: dict):
    """对一份正文算覆盖：每个档的统计 + 每条会话的生效与否。"""
    enabled, expired, panel_scope, invalid_expiry = _panel_state(meta)
    dbs = _state_dbs()
    if not dbs:
        raise SystemExit(f"找不到任何会话库（HERMES_HOME={_hermes_home()}）")
    profiles, sessions = [], []
    for profile, db in dbs:
        declared, allowed = _hook_state(profile)
        try:
            sess = _all_sessions(db)
        except sqlite3.Error as exc:
            profiles.append({"profile": profile, "sessions": 0, "declared": declared,
                             "allowed": allowed, "effective": 0, "skipped": 0,
                             "error": str(exc)})
            continue
        eff = skip = 0
        for sid, title, ts, source in sess:
            reason = ""
            if not enabled:
                reason = "面板已暂停"
            elif invalid_expiry:
                reason = "到期时间无法解析（注入会整块跳过）"
            elif expired:
                reason = "面板已过期"
            elif not declared:
                reason = "该档没声明 pre_llm_call 钩子"
            elif not allowed:
                reason = "该档没同意该钩子命令"
            elif panel_scope not in {"", "any", "*"} and panel_scope != sid:
                reason = "面板只指向别的会话"
            kept, total = 1, 1
            if not reason and hud_panel is not None:
                _, kept, total = hud_panel.filter_body(body, sid, profile)
                if kept == 0:
                    reason = "所有条目都不作用于这条会话"
            sessions.append({"id": sid, "profile": profile, "title": (title or "")[:40],
                             "kind": _session_kind(sid, source),
                             "effective": not reason, "reason": reason,
                             "kept": kept, "total": total})
            if reason:
                skip += 1
            else:
                eff += 1
        profiles.append({"profile": profile, "sessions": len(sess), "declared": declared,
                         "allowed": allowed, "effective": eff, "skipped": skip})
    return profiles, sessions


def cmd_impact(args) -> int:
    """盘点对话：哪些会生效、哪些不会，以及原因。改面板前必须先跑这个。

    --profile / --kinds 只看子集（表格与 JSON 同口径）：totals 只算筛后结果，
    totals_all 保留全量，方便对照“筛掉多少”。
    """
    want_profiles = parse_profile_filter(getattr(args, "profile_filter", None))
    want_kinds = parse_kind_filter(getattr(args, "kind_filter", None))
    filtered = bool(want_profiles or want_kinds)
    meta, body = read_panel(create=False)
    enabled, expired, panel_scope, invalid_expiry = _panel_state(meta)
    profiles_all, sessions_all = _coverage(body, meta)
    failed_all = [p["profile"] for p in profiles_all if p.get("error")]
    total_all = sum(p["sessions"] for p in profiles_all)
    eff_all = sum(p["effective"] for p in profiles_all)
    profiles, sessions = _filter_coverage(profiles_all, sessions_all, want_profiles, want_kinds)
    failed = [p["profile"] for p in profiles if p.get("error")]
    total = sum(p["sessions"] for p in profiles)
    total_eff = sum(p["effective"] for p in profiles)
    kinds: dict[str, int] = {}
    for s in sessions:
        kinds[s.get("kind", "chat")] = kinds.get(s.get("kind", "chat"), 0) + 1
    payload = {
        "panel": {"scope": panel_scope, "enabled": enabled, "expired": expired,
                  "invalid_expiry": invalid_expiry,
                  "expires_at": meta.get("expires_at", ""),
                  "entries": len(hud_panel.entries(body)) if hud_panel else 0,
                  "filters": {"profiles": want_profiles, "kinds": want_kinds}},
        "profiles": profiles, "sessions": sessions,
        "kinds": kinds,
        "totals": {"sessions": total, "effective": total_eff, "skipped": total - total_eff,
                   "complete": not failed, "failed_profiles": failed},
        "totals_all": {"sessions": total_all, "effective": eff_all,
                       "skipped": total_all - eff_all,
                       "complete": not failed_all, "failed_profiles": failed_all},
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if not failed else 1
    print(f"面板：范围={panel_scope} 状态={'暂停' if not enabled else '过期' if expired else '启用'} "
          f"到期={meta.get('expires_at', '?')} 条目={payload['panel']['entries']}")
    if filtered:
        print(f"筛选：档={'/'.join(want_profiles) or '全部'} 分类={'/'.join(want_kinds) or '全部'}")
    print(f"{'档':<14}{'会话':>5}  {'钩子':<4}{'同意':<4}{'生效':>5}{'不生效':>7}")
    for p in profiles:
        if p.get("error"):
            print(f"{p['profile']:<14}{0:>5}  {'?' if not p['declared'] else '✓':<4}{'?' if not p['allowed'] else '✓':<4}  （读库失败：{p['error'][:30]}）")
            continue
        print(f"{p['profile']:<14}{p['sessions']:>5}  {'✓' if p['declared'] else '✗':<4}"
              f"{'✓' if p['allowed'] else '✗':<4}{p['effective']:>5}{p['skipped']:>7}")
    if want_profiles:
        present = {p["profile"] for p in profiles}
        missing = [name for name in want_profiles if name not in present]
        if missing:
            print(f"（这些档没有会话库，未计入：{', '.join(missing)}）")
    if kinds:
        print("分类：" + "，".join(f"{k} {v}" for k, v in sorted(kinds.items())))
    note = f"（筛选后；全量 {total_all} 条，生效 {eff_all}）" if filtered else ""
    if failed:
        print(f"\n⚠ 无法完整盘点：{len(failed)} 个档读库失败（{', '.join(failed[:5])}）")
        print(f"已读到 {total} 条会话：生效 {total_eff}，不生效 {total - total_eff}；"
              f"合计数不可当结论{note}")
    else:
        print(f"\n合计 {total} 条会话：生效 {total_eff}，不生效 {total - total_eff}{note}")
    if args.verbose:
        print("\n逐条：")
        for s in sessions:
            mark = "生效" if s["effective"] else "不生效"
            print(f"  [{mark}] {s['id']:<26} {s['profile']:<12} {s['title']}"
                  + ("" if s["effective"] else f"  ← {s['reason']}"))
    if panel_scope in {"", "any", "*"}:
        print("默认范围=any：包含以后新建的会话。要限定范围，给条目加 [scope: session:<id>] 或用 use/ set --session。")
    return 0 if not failed else 1


def _entry_texts(body: str) -> list[str]:
    """每条条目的完整正文（已去掉 [scope: …] 标注）；缺 hud_panel 时退化为空。"""
    if hud_panel is None:
        return []
    return hud_panel.entry_texts(body)


def _diff_entries(old: list[str], new: list[str]) -> tuple[list[str], list[str]]:
    """按多重集算条目增删：重复条目按次数计，长条目按完整正文比较（不做截断）。"""

    def only_in(before: list[str], after: list[str]) -> list[str]:
        pool = Counter(before)
        out = []
        for text in after:  # 保留原顺序；同文按出现次数扣减
            if pool[text] > 0:
                pool[text] -= 1
            else:
                out.append(text)
        return out

    return only_in(old, new), only_in(new, old)


def _brief(text: str, width: int = 50) -> str:
    """预览输出用：只显示首行，过长/多行截断（JSON 里保留全文）。"""
    head = text.splitlines()[0] if text else ""
    return head if len(head) <= width and "\n" not in text else head[:width] + "…"


def _by_profile(rows: list[dict]) -> str:
    counts: dict[str, int] = {}
    for row in rows:
        counts[row["profile"]] = counts.get(row["profile"], 0) + 1
    return "，".join(f"{k} {v}" for k, v in sorted(counts.items())) or "无"


def cmd_preview(args) -> int:
    """预览一次面板改动的覆盖变化（不写入）。"""
    meta, body = read_panel(create=False)
    target = Path(args.body)
    if not target.is_file():
        raise SystemExit(f"正文文件不存在：{target}")
    new_body = target.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    # 预览按“确认后会执行 set”的状态算：启用 + 重新续期
    sim = dict(meta)
    sim["enabled"] = "true"
    sim["expires_at"] = (datetime.now(CST) + timedelta(days=MAX_TTL_DAYS)).isoformat(timespec="seconds")
    _, old_sessions = _coverage(body, sim)
    _, new_sessions = _coverage(new_body, sim)
    old = {s["id"]: s for s in old_sessions}
    new = {s["id"]: s for s in new_sessions}
    gained = [new[k] for k in new if new[k]["effective"] and not old.get(k, {}).get("effective")]
    lost = [old[k] for k in old if old[k]["effective"] and not new.get(k, {}).get("effective")]
    old_entries, new_entries = _entry_texts(body), _entry_texts(new_body)
    added, removed = _diff_entries(old_entries, new_entries)
    payload = {
        "entries": {"before": len(old_entries), "after": len(new_entries),
                    "added": added, "removed": removed},
        "effective": {"before": sum(s["effective"] for s in old_sessions),
                      "after": sum(s["effective"] for s in new_sessions)},
        "gained": gained, "lost": lost,
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    print(f"条目：{len(old_entries)} → {len(new_entries)}"
          f"（新增 {len(added)}、删除 {len(removed)}）")
    print(f"生效会话：{payload['effective']['before']} → {payload['effective']['after']}")
    print(f"新增生效：{len(gained)} 条（{_by_profile(gained)}）")
    print(f"失去生效：{len(lost)} 条（{_by_profile(lost)}）")
    for text in added:
        print(f"  + {_brief(text)}")
    for text in removed:
        print(f"  - {_brief(text)}")
    if args.verbose:
        for s in gained:
            print(f"  [+生效] {s['id']:<26} {s['profile']:<12} {s['title']}")
        for s in lost:
            print(f"  [-生效] {s['id']:<26} {s['profile']:<12} {s['title']}  ← {s['reason']}")
    print(f"\n确认后执行：python hudctl.py set --body \"{target}\"")
    return 0


def cmd_scope(args) -> int:
    """改某一条的作用域：scope <条目号> all|session:id[,id]|profile:p[,p]|none"""
    if hud_panel is None:
        raise SystemExit("缺 hud_panel.py，无法解析条目")
    meta, body = read_panel()
    items = hud_panel.entries(body)
    if not 1 <= args.entry <= len(items):
        raise SystemExit(f"条目号超出范围：1..{len(items)}")
    value = args.value.strip()
    if "\n" in value or "\r" in value:
        raise SystemExit("作用域值不能包含换行")
    if value.lower() not in {"none", "all"}:
        if hud_panel.parse_scope(value) is None:
            raise SystemExit(f"作用域写法不认：{value}（all / session:id[,id] / profile:p[,p] / none）")
    lines = body.splitlines()
    item = items[args.entry - 1]
    first = lines[item["start"]]
    m = hud_panel.ENTRY_RE.match(first)
    marker = m.group(0) if m else ""
    rest = re.sub(r"\[scope:[^\]]*\]", "", first[len(marker):], flags=re.IGNORECASE).lstrip()
    tag = "" if value.lower() == "none" else f"[scope: {value}] "
    lines[item["start"]] = marker + tag + rest
    write_panel(meta, "\n".join(lines))
    print(f"第 {args.entry} 条作用域 → {value}")
    return 0


def cmd_use(args) -> int:
    """把面板指向另一个会话（按 id 或标题子串匹配）。"""
    import sqlite3
    dbs = _state_dbs()
    if not dbs:
        raise SystemExit(f"找不到任何会话库（HERMES_HOME={_hermes_home()}）")
    rows = []
    for profile, db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            part = con.execute(
                "select id, title from sessions where coalesce(archived,0)=0 "
                "order by last_activity_at desc limit 300"
            ).fetchall()
        except sqlite3.Error:
            continue
        finally:
            con.close()
        rows.extend((sid, title, profile) for sid, title in part)
    target = args.target
    hits = [r for r in rows if r[0] == target] or [r for r in rows if target.lower() in (r[1] or "").lower()]
    if not hits:
        raise SystemExit(f"没找到会话：{target}（先跑 `python hudctl.py sessions`）")
    if len(hits) > 1:
        listing = "\n".join(f"  {sid}  [{prof}] {title}" for sid, title, prof in hits[:8])
        raise SystemExit(f"匹配到多个会话，请给完整 id：\n{listing}")
    sid, title, _prof = hits[0]
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
        body_path = Path(args.body)
        if not body_path.is_file():
            raise SystemExit(f"正文文件不存在：{body_path}")
        body = body_path.read_text(encoding="utf-8").lstrip("\ufeff").strip()
    elif not sys.stdin.isatty():
        incoming = sys.stdin.read().strip()
        if incoming:
            body = incoming
    meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
    meta["enabled"] = "true"
    write_panel(meta, body)
    print(f"已更新：范围={meta['session_id']} 到期={meta['expires_at']} 正文={len(body)} 字")
    return 0


# 与 hud_inject.py 的 MAX_CHARS 同值；两个脚本要能各自独立部署，所以不互相 import
MAX_BLOCK_CHARS = 1600
BLOCK_MARGIN = 80  # hud_inject 还可能追加一行 selfcheck 提醒（最长约 40 字），估长留够
HINT_DEFAULT = "default bot：优先用 Hermes 官方机制（delegate_task / cron / kanban）"
HINT_OTHER = "{who} bot：优先用 OTUI（build/open-tui-orchestrator）"


def estimate_block(meta: dict[str, str], body: str, expires) -> int:
    """按注入端真实拼法估整块字数：头行 + 正文 + 尾行 + 余量。

    头行用真标题、真到期时间；括号里注入的是档名，取档名与面板 scope 中较长的一个，
    宁可估长也不低估。尾行是 hud_inject 每次都会追加的「执行机制建议」。
    """
    title = meta.get("title") or "会话面板"
    stamp = expires.astimezone(CST).strftime("%m-%d %H:%M") if expires else "12-31 07:00"
    profile = (os.environ.get("HERMES_PROFILE") or "default").strip() or "default"
    who = max((profile, "default", (meta.get("session_id") or "any").strip()), key=len)
    hint = HINT_DEFAULT if profile == "default" else HINT_OTHER.format(who=profile)
    header = f"[HUD·{title}] 生效至 {stamp}（{who}）"
    tail = f"执行机制建议：{hint}"
    return len(header) + len(body) + len(tail) + 2 + BLOCK_MARGIN


def cmd_check(_args) -> int:
    """校验面板格式；有问题返回 1，方便放进门禁/CI。"""
    problems = []
    if not PANEL.exists():
        print(f"✗ 面板不存在：{PANEL}")
        return 1
    try:
        raw = PANEL.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        print(f"✗ 面板不是合法 UTF-8：{PANEL}")
        return 1
    if raw.startswith("\ufeff"):
        problems.append("文件带 BOM（注入端能容忍，但建议去掉）")
    raw = raw.lstrip("\ufeff")
    seen: set[str] = set()
    for line in raw.splitlines()[:24]:
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        name = key.strip().lower()
        if not sep or name not in META_KEYS:
            break
        if name in seen:
            problems.append(f"重复元数据：{name}")
        seen.add(name)
        if any(ord(ch) < 32 for ch in value):
            problems.append(f"元数据 {name} 含控制字符")
    meta, body = split_panel(raw)
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
    if hud_panel is not None:
        problems.extend(hud_panel.validate(body))
    estimate = estimate_block(meta, body, expires)
    if estimate > MAX_BLOCK_CHARS:
        problems.append(f"注入块约 {estimate} 字（标题行 + 正文 + 注入尾行，另含 "
                        f"{BLOCK_MARGIN} 字提醒余量），超过 {MAX_BLOCK_CHARS} 上限（会被截断）")
    if problems:
        print("✗ 面板有问题：")
        for p in problems:
            print("  - " + p)
        return 1
    print(f"✓ 面板正常：范围={scope or 'any'} 到期={meta['expires_at']} 正文={len(body)} 字"
          f"（整块约 {estimate} 字 / 上限 {MAX_BLOCK_CHARS}）")
    return 0


def cmd_toggle(flag: str):
    def run(_args) -> int:
        meta, body = read_panel()
        meta["enabled"] = flag
        write_panel(meta, body)
        print(f"enabled={flag}")
        return 0
    return run


SNAP_NAME_RE = re.compile(
    r"^(?P<date>\d{4}-\d{2}-\d{2})(?:-(?P<clock>\d{6})(?:-(?P<seq>\d+))?)?\.md$"
)


def _snapshot_key(path: Path):
    """快照文件名 → (年,月,日,时,分,秒,序号)；不是快照命名则返回 None。

    `YYYY-MM-DD.md` 是当天第一份，按 00:00:00 计；同日后续快照是
    `YYYY-MM-DD-HHMMSS.md`，同一秒里再撞名才加 `-N` 序号。
    """
    m = SNAP_NAME_RE.fullmatch(path.name)
    if not m:
        return None
    try:
        day = datetime.strptime(m.group("date"), "%Y-%m-%d")
    except ValueError:
        return None
    clock = m.group("clock") or "000000"
    hh, mm, ss = int(clock[:2]), int(clock[2:4]), int(clock[4:])
    if hh > 23 or mm > 59 or ss > 59:
        return None
    return (day.year, day.month, day.day, hh, mm, ss, int(m.group("seq") or 0))


def _snapshots() -> list[Path]:
    """快照目录里的合法快照，按时间（取自文件名）从旧到新；杂项 .md 不算。"""
    found = [(key, path.name, path) for path in SNAPS.glob("*.md")
             if (key := _snapshot_key(path)) is not None]
    return [path for _, _, path in sorted(found)]


def _snapshot_target(now: datetime) -> Path:
    """当天第一份用 `YYYY-MM-DD.md`；已存在就换 `YYYY-MM-DD-HHMMSS.md`，再撞名加序号。"""
    plain = SNAPS / f"{now.strftime('%Y-%m-%d')}.md"
    if not (plain.exists() or plain.is_symlink()):
        return plain
    stamp = now.strftime("%Y-%m-%d-%H%M%S")
    candidate = SNAPS / f"{stamp}.md"
    seq = 1
    while candidate.exists() or candidate.is_symlink():
        seq += 1
        candidate = SNAPS / f"{stamp}-{seq}.md"
    return candidate


def cmd_snapshot(_args) -> int:
    SNAPS.mkdir(exist_ok=True)
    meta, body = read_panel()
    target = _snapshot_target(datetime.now(CST))
    tmp = target.with_name(f"{target.name}.{os.getpid()}.tmp")
    try:
        shutil.copy2(PANEL, tmp)
        os.replace(tmp, target)  # 原子落盘：不留半份快照，也不覆盖已有快照
    finally:
        if tmp.exists():
            tmp.unlink()
    print(f"快照：{target}（{len(body)} 字）")
    return 0


def cmd_restore(args) -> int:
    SNAPS.mkdir(exist_ok=True)
    if args.date != "latest" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", args.date):
        raise SystemExit(f"日期格式应为 YYYY-MM-DD 或 latest：{args.date}")
    snaps = _snapshots()
    if not snaps:
        raise SystemExit("没有快照可恢复")
    if args.date == "latest":
        target = snaps[-1]
    else:
        wanted = tuple(int(part) for part in args.date.split("-"))
        same_day = []
        for path in snaps:
            key = _snapshot_key(path)
            if key is not None and key[:3] == wanted:
                same_day.append(path)
        if not same_day:
            raise SystemExit(f"没有这份快照：{args.date}（{SNAPS} 下没有该日快照）")
        target = same_day[-1]  # 同日多份 → 取最新一份
    meta, body = split_panel(target.read_text(encoding="utf-8"))
    if args.session:
        meta["session_id"] = args.session
    meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
    meta["enabled"] = "true"
    write_panel(meta, body)
    print(f"已从 {target.name} 恢复：范围={meta['session_id']} 到期={meta['expires_at']} 正文={len(body)} 字")
    return 0


def cmd_prune(args) -> int:
    if args.keep < 1:
        raise SystemExit(f"--keep 必须 >= 1：{args.keep}")
    snaps = _snapshots()  # 新旧两种命名混排，按文件名里的时间从旧到新
    drop = snaps[: max(0, len(snaps) - args.keep)]
    for path in drop:
        path.unlink()
    print(f"保留 {min(args.keep, len(snaps))} 份，删除 {len(drop)} 份")
    strays = sum(1 for _ in SNAPS.glob("*.md")) - len(snaps) if SNAPS.is_dir() else 0
    if strays > 0:
        print(f"（另有 {strays} 个非快照命名的 .md：不计入、未删除）")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="会话面板（HUD）管理")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(func=cmd_status)
    p_sessions = sub.add_parser("sessions")
    p_sessions.add_argument("--limit", type=int, default=15)
    p_sessions.set_defaults(func=cmd_sessions)
    p_impact = sub.add_parser("impact")
    p_impact.add_argument("--json", action="store_true")
    p_impact.add_argument("--verbose", action="store_true")
    p_impact.add_argument("--profile", dest="profile_filter", metavar="名[,名]",
                          help="只看这些档（逗号分隔；默认全部）")
    p_impact.add_argument("--kinds", dest="kind_filter", metavar="k1,k2",
                          help="只看这些分类：" + ",".join(KIND_CHOICES))
    p_impact.set_defaults(func=cmd_impact)
    p_preview = sub.add_parser("preview")
    p_preview.add_argument("--body", required=True, help="候选正文文件")
    p_preview.add_argument("--json", action="store_true")
    p_preview.add_argument("--verbose", action="store_true")
    p_preview.set_defaults(func=cmd_preview)
    p_scope = sub.add_parser("scope")
    p_scope.add_argument("entry", type=int)
    p_scope.add_argument("value")
    p_scope.set_defaults(func=cmd_scope)
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
