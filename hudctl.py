#!/usr/bin/env python3
"""hudctl.py — 管理会话面板（HUD）的一个小命令行。

面板 = 本脚本同目录的 panel.md，头部四行元数据（session_id / expires_at / title / enabled），
其后是注入正文。pre_llm_call 钩子每轮读它；本脚本只负责**人来操作**：

    python hudctl.py status                  # 看当前面板：作用范围、到期时间、剩多久
    python hudctl.py check                   # 校验格式（缺元数据/到期写错/正文超长/未知键）
    python hudctl.py check --json            # 机器可读：errors（真格式错）与 notices（状态提示）
    python hudctl.py set --ttl 12h           # 换正文并续期（正文从 --body 文件或 stdin 读）
    python hudctl.py off / on                # 暂停 / 恢复注入（不删面板；on --ttl 8h 顺手续期）
    python hudctl.py snapshot                # 存一份快照（同日多份加时间戳，不互相覆盖）
    python hudctl.py restore latest --ttl 8h # 从快照恢复（恢复前自动拍 pre-restore 快照，拍不成拒绝恢复）
    python hudctl.py restore --file <名>     # 按文件名恢复（含 pre-restore-*.md；写入前先校验快照内容）
    python hudctl.py prune --keep 14         # 只列出将被删除的旧快照；加 --yes 才真删

设计边界：脚本不自动恢复、不自动续期——恢复必须是人手动执行（用户定的规矩）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import stat as _stat
import sys
import tempfile
import time
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import NoReturn

try:
    import hud_panel  # 同目录；元数据 + 条目作用域的唯一解析实现
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


META_KEYS = hud_panel.META_KEYS if hud_panel is not None else (
    "session_id", "expires_at", "title", "enabled")
MAX_PANEL_BYTES = 200_000      # 软上限：超过它 status/check 只报，不解析整份（注入端也跳过）
MAX_PANEL_READ_BYTES = 2_000_000   # 硬上限：整份读写的绝对上限（再大就不能当面板读）
HEAD_READ_BYTES = 64_000       # 「只读头部」的上限：元数据在前 24 行里，64KB 足够
MAX_BODY_CHARS = 1600          # 与 hud_inject.MAX_CHARS 同值：正文超限直接拒绝写入
FILE_ATTRIBUTE_REPARSE_POINT = 0x400   # Windows：目录联接/符号链接等重解析点


def _is_reparse_point(st) -> bool:
    """Windows 重解析点（junction / symlink 等）；POSIX 上没有该属性，恒 False。"""
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT)


def _unsafe_path(path: Path) -> str:
    """路径是符号链接 / Windows 重解析点 / 非普通文件时返回原因，否则空串。

    一律用 lstat：绝不跟随链接去判断「目标是不是普通文件」——那正是软链攻击的入口。
    不存在返回空串（交给后续 open() 报缺文件）。

    `lstat` 报 `FileNotFoundError` 时**直接返回空串**，不要用 `os.path.lexists` 去判
    「悬空软链」：并发进程完全可能在两次调用之间创建同一个普通锁文件，`lexists` 为真
    就会被误判成重解析点（CI windows/3.12 的并发 scope 用例即此竞态）。「是否存在 /
    是否普通文件」交给后续 `open()`/`fstat()` 判定；真正的软链由 `lstat` 成功时的
    `S_ISLNK` 分支和 `fstat` 兜住。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"无法 lstat（{type(exc).__name__}）"
    if _stat.S_ISLNK(st.st_mode) or _is_reparse_point(st):
        return "是符号链接/重解析点"
    if not _stat.S_ISREG(st.st_mode):
        return "不是普通文件"
    return ""


def _is_regular_file(path: Path) -> bool:
    """是普通文件、且不是符号链接/重解析点（快照过滤用）。"""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return _stat.S_ISREG(st.st_mode) and not _is_reparse_point(st)


def _atomic_write_text(path: Path, data: str) -> Path:
    """同目录临时文件 + os.replace 原子替换。

    临时文件名由 `tempfile.mkstemp`（O_CREAT|O_EXCL）生成，**不会**落到可预测的
    `panel.md.<pid>.tmp` 上：被人预算好名字放一个软链，旧写法会把数据写进受害文件。

    固定 `newline="\n"`：文本模式的默认换行转换会在 Windows 上把每个 `\\n` 写成 CRLF，
    正文里本来就有 CRLF 时就叠成双 CR 行尾——注入端 `splitlines()` 会把它算成两个换行。
    面板一律按 LF 落盘，跨平台字节一致。
    """
    fd, tmp_name = tempfile.mkstemp(prefix=f"{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(data)
        os.replace(tmp, path)
    finally:
        if tmp.exists() or tmp.is_symlink():
            try:
                tmp.unlink()
            except OSError:
                pass
    return path


ANSI_RE = re.compile(
    r"\x1b\[[0-?]*[ -/]*[@-~]"                 # CSI（颜色、光标移动…）
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?"     # OSC（标题、超链接…）
    r"|\x1b[@-Z\\-_]"                          # 双字符 ESC 序列
    r"|\x1b"                                   # 落单的 ESC
)
C0_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


def _safe_text(value) -> str:
    """终端输出前的清洗：先剥 ESC 序列（否则后面只剩 `[31m` 残字），再剥 C0 控制符。

    会话标题来自 state.db（另一进程/另一档写入的数据），直接 print 等于让标题里的
    ANSI 序列改终端颜色、清屏、伪造输出。换行也压成空格：一行一条记录不能被撑断。
    """
    text = ANSI_RE.sub("", "" if value is None else str(value))
    text = C0_RE.sub("", text)
    return re.sub(r"[\r\n\t]+", " ", text)


def _panel_size() -> int:
    """面板字节数；读不到就当 0（后续 open/stat 会给出具体错误）。"""
    try:
        return PANEL.stat().st_size
    except OSError:
        return 0


def _normalize_newlines(text: str) -> str:
    r"""CRLF / 裸 CR / 旧版叠出来的 `\r\r\n` 统一成 `\n`（与 hud_panel 同一实现）。

    写入前在**唯一的入口**归一：正文来自 `--body` 文件、stdin、快照恢复时行尾各不相同，
    不归一就会把双 CR 写进面板，注入端再按行拆就多出空行。
    """
    if hud_panel is not None:
        return hud_panel.normalize_newlines(text)
    return re.sub(r"\r+\n|\r+", "\n", text) if "\r" in text else text


def _read_text_exact(path: Path) -> str:
    r"""按原样读文本：`newline=""` 保留原始行尾，交给 _normalize_newlines 统一。

    不能用 `Path.read_text()`：它默认做通用换行转换，会把旧版叠出来的 `\r\r\n`
    先拆成两个 `\n`——之后再归一已经无从区分，头部因此多出一个空行
    （check 假红、restore 拒收合法快照）。读面板与读快照都必须走这里。
    """
    with path.open("r", encoding="utf-8", newline="") as fh:
        return fh.read()


def _strip_comment(value: str) -> str:
    if hud_panel is not None:
        return hud_panel.strip_comment(value)
    for sep in (" #", "\t#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    return value.strip()


def _split_panel_ex(text: str) -> tuple[dict[str, str], str, list[str]]:
    """(元数据, 正文, 未知键)。与 hud_inject 共用 hud_panel.split_meta，绝不各写一份。"""
    if hud_panel is not None:
        return hud_panel.split_meta(text)
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
    return meta, "\n".join(lines[body_start:]).strip(), []


def split_panel(text: str) -> tuple[dict[str, str], str]:
    """与 hud_inject.parse_panel 同源：前 24 行连续元数据，空行/非元数据行结束元数据块。"""
    meta, body, _unknown = _split_panel_ex(text)
    return meta, body


def read_panel_ex(create: bool = True, head_only: bool = False) -> tuple[dict[str, str], str, list[str]]:
    """(元数据, 正文, 未知键)。头部未知键必须能报出来：注入端会因此整块跳过。

    head_only=True 只读文件开头（最多 64KB）：元数据在前 24 行，够用了。
    status/check/set 走这条——面板被写坏成几百 KB 时也能读出头部并整份覆盖，
    不会因为「读不了整份」而连自救的 set 都执行不了。
    """
    unsafe = _unsafe_path(PANEL)
    if unsafe:
        # 管理端读路径同样不能跟随软链：status/impact/check 读到的可能是别人的私钥
        raise SystemExit(f"面板{unsafe}，拒绝读取：{PANEL}（换成普通文件后再操作）")
    if not PANEL.exists() and not create:
        raise SystemExit(f"面板不存在：{PANEL}")
    if not PANEL.exists():
        sample = ROOT / "panel.example.md"
        if sample.exists():
            _atomic_write_text(PANEL, sample.read_text(encoding="utf-8").lstrip("\ufeff"))
            print(f"（面板不存在，已从 panel.example.md 复制：{PANEL}）")
        else:
            raise SystemExit(f"面板不存在：{PANEL}（先 cp panel.example.md panel.md）")
    try:
        size = PANEL.stat().st_size
    except OSError as exc:
        raise SystemExit(f"读不到面板：{PANEL}（{type(exc).__name__}）")
    if head_only:
        try:
            with PANEL.open("rb") as fh:
                chunk = fh.read(HEAD_READ_BYTES)
        except OSError as exc:
            raise SystemExit(f"读不到面板：{PANEL}（{type(exc).__name__}）")
        raw = chunk.decode("utf-8", errors="replace")
    else:
        if size > MAX_PANEL_READ_BYTES:
            raise SystemExit(
                f"面板过大（{size} 字节 > {MAX_PANEL_READ_BYTES}）：拒绝整份读取 {PANEL}；"
                "要换掉它用 set --body <文件>（set 只读头部，可整份覆盖）")
        try:
            # 读原样行尾（一串 CR 只算一个换行）：归一交给 normalize_newlines
            raw = _read_text_exact(PANEL)
        except UnicodeDecodeError:
            raise SystemExit(f"面板不是合法 UTF-8：{PANEL}")
    return _split_panel_ex(raw.lstrip("\ufeff"))


def read_panel_head() -> tuple[dict[str, str], list[str]]:
    """只读头部：给 status / set 用（超大面板也能读、能覆盖）。"""
    meta, _body, unknown = read_panel_ex(create=True, head_only=True)
    return meta, unknown


def read_panel(create: bool = True) -> tuple[dict[str, str], str]:
    meta, body, _unknown = read_panel_ex(create)
    return meta, body


def render_panel(meta: dict[str, str], body: str) -> str:
    """把元数据 + 正文拼成面板文本（write_panel 与 restore 预校验共用同一份拼法）。"""
    meta = dict(meta)
    meta.setdefault("session_id", "any")
    meta.setdefault("title", "会话面板")
    meta.setdefault("enabled", "true")
    if "expires_at" not in meta:
        raise SystemExit("面板缺少 expires_at，无法写入（先补一行到期时间）")
    for key in META_KEYS:
        if "\n" in meta.get(key, "") or "\r" in meta.get(key, ""):
            raise SystemExit(f"元数据 {key} 不能包含换行")
    head = "\n".join(f"{k}: {meta.get(k, '')}" for k in META_KEYS)
    return f"{head}\n\n{_normalize_newlines(body)}\n"


def write_panel(meta: dict[str, str], body: str) -> None:
    unsafe = _unsafe_path(PANEL)
    if unsafe:
        raise SystemExit(f"面板{unsafe}，拒绝写入：{PANEL}（软链/重解析点会把内容写进别人指定的文件）")
    data = render_panel(meta, body)
    _atomic_write_text(PANEL, data)


def _open_lock(lock_path: Path):
    """打开面板锁：软链 / 重解析点 / 非普通文件一律拒绝。

    三道检查：打开前 lstat（拒软链）、POSIX 上再加 O_NOFOLLOW、打开后 fstat 确认句柄
    是普通文件并与路径 lstat 的 inode 对得上——防「检查之后被换成软链」的竞态。
    旧写法 `open(path, "a+b")` 会老老实实跟随软链，把锁文件指向谁就锁谁、还能写。
    """
    unsafe = _unsafe_path(lock_path)
    if unsafe:
        raise OSError(f"面板锁{unsafe}")
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(lock_path, flags, 0o600)
    try:
        st = os.fstat(fd)
        if not _stat.S_ISREG(st.st_mode) or _is_reparse_point(st):
            raise OSError("面板锁不是普通文件")
        try:
            lst = os.lstat(lock_path)
        except OSError:
            lst = None
        if lst is not None:
            if _stat.S_ISLNK(lst.st_mode) or _is_reparse_point(lst):
                raise OSError("面板锁是符号链接/重解析点")
            if lst.st_ino and st.st_ino and (lst.st_dev, lst.st_ino) != (st.st_dev, st.st_ino):
                raise OSError("面板锁在打开过程中被替换了")
        return os.fdopen(fd, "r+b")
    except BaseException:
        os.close(fd)
        raise


@contextmanager
def panel_lock(timeout: float = 10.0):
    """面板读-改-写用的**同一把**文件锁（跨进程）。

    set / off / on / scope / use / restore 都走它：没有锁时两个并发命令各自读到旧内容、
    后写的那次会把前一次的更新整块覆盖掉（丢更新）。
    """
    lock_path = PANEL.with_name(PANEL.name + ".lock")
    fh = None
    try:
        try:
            fh = _open_lock(lock_path)
        except OSError as exc:
            _die(f"打不开面板锁 {lock_path}（{type(exc).__name__}: {exc}）："
                 "它应该是普通文件；若是目录/别的文件，先把它移走再试")
        try:
            fh.seek(0, os.SEEK_END)
            if fh.tell() == 0:
                fh.write(b"\0")
                fh.flush()
        except OSError:
            pass
        deadline = time.monotonic() + timeout
        while True:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise SystemExit(f"面板被另一个进程锁住（{lock_path}），稍后重试")
                time.sleep(0.05)
        yield
    finally:
        if fh is not None:
            try:
                fh.seek(0)
                if os.name == "nt":
                    import msvcrt
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
            except OSError:
                pass
            try:
                fh.close()
            except OSError:
                pass


def _hermes_home() -> Path:
    """与 hud_panel.hermes_home() 同一口径（HERMES_HOME → LOCALAPPDATA → XDG）。"""
    if hud_panel is not None:
        return Path(hud_panel.hermes_home())
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
            if not entry.is_dir() or entry.name.startswith("."):
                continue        # 与 _known_profiles 一致：点目录不是可选档（否则计入却无法按档筛选）
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
        print(f"{_safe_text(sid):<26} {profile:<12} {n or 0:>4}  {when}  {_safe_text(title)[:28]}")
    return 0




def _profile_cfg(profile: str) -> Path:
    home = _hermes_home()
    return home / "config.yaml" if profile == "default" else home / "profiles" / profile / "config.yaml"


def _profile_allow(profile: str) -> Path:
    home = _hermes_home()
    return (home / "shell-hooks-allowlist.json" if profile == "default"
            else home / "profiles" / profile / "shell-hooks-allowlist.json")


def _hook_commands(profile: str) -> list[str]:
    r"""config 里 **pre_llm_call 事件下**、未注释的、command 指向 hud_inject 的命令。

    解析只在 hud_panel 里实现一份（hudctl 与 selfcheck 共用）：只声明在
    pre_tool_call 下的命令不算数（否则陈旧配置会假装生效），行内写法同样认。
    缺 hud_panel 时不猜：返回空（宁可报「没声明」，也不把别的钩子当成本钩子）。
    """
    try:
        text = _profile_cfg(profile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    if hud_panel is None:
        return []
    return hud_panel.hook_commands(text)


def _allowed_commands(profile: str) -> set[str]:
    """allowlist 里 pre_llm_call 且指向 hud_inject 的命令（精确字符串，与 Hermes 同口径）。"""
    try:
        text = _profile_allow(profile).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return set()
    if hud_panel is None:
        return set()
    return hud_panel.approved_commands(text)


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


def _enabled_value(meta: dict):
    """enabled → True / False / None（非法）。与注入端共用 hud_panel.parse_enabled。"""
    raw = meta.get("enabled", "true")
    if hud_panel is not None:
        return hud_panel.parse_enabled(raw)
    text = str(raw).strip().lower()
    if text in {"true", "1", "yes", "on"}:
        return True
    if text in {"false", "0", "no", "off"}:
        return False
    return None


def _panel_state(meta: dict) -> tuple[bool, bool, str, bool]:
    """(启用, 已过期, 范围, 到期时间是否非法)。到期非法按“全部不生效”算，与注入一致。

    范围为空/缺省**不再**当成 any：注入端把「没写」视为无效并跳过，这边口径必须一致。
    enabled 用与注入端同一张白名单：非法值 → 不启用（fail-closed），不猜默认开。
    """
    enabled_value = _enabled_value(meta)
    enabled = bool(enabled_value)

    expires = parse_expiry(meta.get("expires_at", ""))
    invalid_expiry = expires is None
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    expired = invalid_expiry or bool(expires and expires <= datetime.now(CST))
    scope = (meta.get("session_id") or "").strip()
    return enabled, expired, scope, invalid_expiry


def _enabled_illegal(meta: dict) -> bool:
    return _enabled_value(meta) is None


def _coverage(body: str, meta: dict, unknown=()):
    """对一份正文算覆盖：每个档的统计 + 每条会话的生效与否。

    unknown 是头部未知键（注入端会因此整块跳过）；body 的围栏未闭合同样整块跳过——
    这两条都得在盘点里体现，否则 impact 会报「生效」而实际一个字都注不进去。
    """
    enabled, expired, panel_scope, invalid_expiry = _panel_state(meta)
    bad_enabled = _enabled_illegal(meta)
    unknown = list(unknown or [])
    degenerate = hud_panel.degenerate_reason(body) if hud_panel is not None else "no-hud-panel"
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
            if bad_enabled:
                reason = "enabled 取值不合法（注入会整块跳过；hudctl check 会报错）"
            elif not enabled:
                reason = "面板已暂停"
            elif unknown:
                reason = f"头部有未知键（{', '.join(unknown[:3])}），注入会整块跳过"
            elif invalid_expiry:
                reason = "到期时间无法解析（注入会整块跳过）"
            elif expired:
                reason = "面板已过期"
            elif not panel_scope:
                reason = "面板 session_id 为空（注入会跳过）"
            elif not declared:
                reason = "该档没声明 pre_llm_call 钩子"
            elif not allowed:
                reason = "该档没同意该钩子命令"
            elif panel_scope not in {"any", "*"} and panel_scope != sid:
                reason = "面板只指向别的会话"
            elif degenerate:
                reason = f"正文无法安全解析（{degenerate}），注入会整块跳过"
            kept, total = 1, 1
            if not reason and hud_panel is not None:
                _, kept, total = hud_panel.filter_body(body, sid, profile)
                if kept == 0:
                    reason = ("正文写了 [scope: …] 却没有可识别条目（注入会整块跳过）"
                              if total == 0 else "所有条目都不作用于这条会话")
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


def _impact_filters(profiles: list[str], kinds: list[str]) -> dict:
    """impact 的筛选回显；missing_profiles 只在非空时出现（保持旧 JSON 形状）。"""
    out: dict = {"profiles": profiles, "kinds": kinds}
    have = {n for n, _ in _state_dbs()}
    missing = [p for p in _known_profiles() if p not in have]
    if missing:
        out["missing_profiles"] = missing
    return out


def cmd_impact(args) -> int:
    """盘点对话：哪些会生效、哪些不会，以及原因。改面板前必须先跑这个。

    --profile / --kinds 只看子集（表格与 JSON 同口径）：totals 只算筛后结果，
    totals_all 保留全量，方便对照“筛掉多少”。
    """
    want_profiles = parse_profile_filter(getattr(args, "profile_filter", None))
    want_kinds = parse_kind_filter(getattr(args, "kind_filter", None))
    filtered = bool(want_profiles or want_kinds)
    meta, body, unknown = read_panel_ex(create=False)
    enabled, expired, panel_scope, invalid_expiry = _panel_state(meta)
    profiles_all, sessions_all = _coverage(body, meta, unknown)
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
                  "invalid_expiry": invalid_expiry, "unknown_meta": unknown,
                  "expires_at": meta.get("expires_at", ""),
                  "entries": len(hud_panel.entries(body)) if hud_panel else 0,
                  "filters": _impact_filters(want_profiles, want_kinds)},
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
        return 0 if not failed and not failed_all else 1
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
    if failed_all:
        note += f"；⚠ 全量不完整：{len(failed_all)} 个档读库失败（{', '.join(failed_all[:5])}）"
    if failed:
        print(f"\n⚠ 无法完整盘点：{len(failed)} 个档读库失败（{', '.join(failed[:5])}）")
        print(f"已读到 {total} 条会话：生效 {total_eff}，不生效 {total - total_eff}；"
              f"合计数不可当结论{note}")
    else:
        print(f"\n合计 {total} 条会话：生效 {total_eff}，不生效 {total - total_eff}{note}")
    missing = [p for p in _known_profiles() if p not in {n for n, _ in _state_dbs()}]
    if missing:
        print(f"\nℹ {len(missing)} 个档还没有 state.db（未使用过，不计入全量）：{', '.join(missing)}")
    if args.verbose:
        print("\n逐条：")
        for s in sessions:
            mark = "生效" if s["effective"] else "不生效"
            print(f"  [{mark}] {_safe_text(s['id']):<26} {s['profile']:<12} {_safe_text(s['title'])}"
                  + ("" if s["effective"] else f"  ← {s['reason']}"))
    if unknown:
        print(f"\n⚠ 头部有未知键：{', '.join(unknown)}（注入端会整块跳过；改掉或删掉这些行）")
    if not panel_scope:
        print("\n⚠ 面板 session_id 为空：注入端视为无效并跳过（写 any 才作用于全部会话）")
    elif panel_scope in {"any", "*"}:
        print("默认范围=any：包含以后新建的会话。要限定范围，给条目加 [scope: session:<id>] 或用 use/ set --session。")
    return 0 if not failed and not failed_all else 1


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
    new_body = _read_body_file(target)   # 与 set 同一读取口径：UTF-8 校验 + 换行归一
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
            print(f"  [+生效] {_safe_text(s['id']):<26} {s['profile']:<12} {_safe_text(s['title'])}")
        for s in lost:
            print(f"  [-生效] {_safe_text(s['id']):<26} {s['profile']:<12} {_safe_text(s['title'])}  ← {s['reason']}")
    print(f"\n确认后执行：python hudctl.py set --body \"{target}\"")
    return 0


def cmd_scope(args) -> int:
    """改某一条的作用域：scope <条目号> all|session:id[,id]|profile:p[,p]|none"""
    if hud_panel is None:
        raise SystemExit("缺 hud_panel.py，无法解析条目")
    with panel_lock():
        meta, body = read_panel()
        items = hud_panel.entries(body)
        if not 1 <= args.entry <= len(items):
            raise SystemExit(f"条目号超出范围：1..{len(items)}")
        value = args.value.strip()
        if "\n" in value or "\r" in value:
            raise SystemExit("作用域值不能包含换行")
        if value.lower() != "none" and hud_panel.parse_scope(value) is None:
            raise SystemExit(f"作用域写法不认：{value}（all / session:id[,id] / profile:p[,p] / none）")
        lines = body.splitlines()
        item = items[args.entry - 1]
        marker_m = hud_panel.ENTRY_RE.match(lines[item["start"]])
        marker = marker_m.group(0) if marker_m else ""
        # 只删行首那个标注：正文里恰好写着 `[scope: …]` 字样（示例/说明）不能一起删掉
        rest = hud_panel.SCOPE_RE.sub("", lines[item["start"]][len(marker):], count=1).lstrip()
        tag = "" if value.lower() == "none" else f"[scope: {value}] "
        block = [marker + tag + rest]
        drop = set(item.get("scope_lines") or ([item["scope_line"]] if item["scope_line"] is not None else []))
        for idx in range(item["start"] + 1, item["end"]):
            line = lines[idx]
            if idx in drop:
                # 续行上被认作标注的**每一处**都清掉：只清第一处，第二处仍会让整条失效
                # 用 hud_panel 的同一实现：保留原缩进，避免 `- 续行` 顶格后变成新条目
                line = hud_panel.strip_leading_scope(line)
                if not line:
                    continue
            block.append(line)
        lines[item["start"]:item["end"]] = block
        write_panel(meta, "\n".join(lines))
    print(f"第 {args.entry} 条作用域 → {value}")
    return 0


def _session_index(dbs) -> list[tuple[str, str, str]]:
    """所有档的全部会话 (id, 标题, 档)。**不截断**：use 的精确匹配不能被 limit 挡住。"""
    rows: list[tuple[str, str, str]] = []
    for profile, db in dbs:
        try:
            con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        except sqlite3.Error:
            continue
        try:
            part = con.execute("select id, title from sessions").fetchall()
        except sqlite3.Error:
            continue
        finally:
            con.close()
        rows.extend((sid, title or "", profile) for sid, title in part)
    return rows


def cmd_use(args) -> int:
    """把面板指向另一个会话：先按 id 精确匹配**全部**会话，再退化为标题子串。"""
    dbs = _state_dbs()
    if not dbs:
        raise SystemExit(f"找不到任何会话库（HERMES_HOME={_hermes_home()}）")
    target = args.target.strip()
    if not target:
        raise SystemExit("目标不能为空：给会话 id 或标题关键字")
    rows = _session_index(dbs)
    hits = [r for r in rows if r[0] == target] or [r for r in rows if target.lower() in r[1].lower()]
    if not hits:
        raise SystemExit(f"没找到会话：{target}（先跑 `python hudctl.py sessions`）")
    if len(hits) > 1:
        listing = "\n".join(f"  {_safe_text(sid)}  [{prof}] {_safe_text(title)}"
                            for sid, title, prof in hits[:8])
        raise SystemExit(f"匹配到多个会话，请给完整 id：\n{listing}")
    sid, title, _prof = hits[0]
    with panel_lock():
        meta, body = read_panel()
        meta["session_id"] = sid
        meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
        meta["enabled"] = "true"
        write_panel(meta, body)
    print(f"已挂到：{_safe_text(sid)}（{_safe_text(title)}） 到期 {meta['expires_at']}")
    return 0


def cmd_status(_args) -> int:
    size = _panel_size()
    oversized = size > MAX_PANEL_BYTES
    if oversized:
        meta, _unknown = read_panel_head()     # 只读头部：几百 KB 也不整份读
        body = ""
    else:
        meta, body = read_panel()
    expires = parse_expiry(meta.get("expires_at", ""))
    if expires and expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    now = datetime.now(CST)
    left = (expires - now) if expires else None
    flag = _enabled_value(meta)
    state = ("启用" if flag else "暂停") if flag is not None else f"非法（enabled={meta.get('enabled')!r}，注入会跳过）"
    print(f"面板：{PANEL}")
    print(f"标题：{meta.get('title', '-')}")
    print(f"范围：{meta.get('session_id', 'any')}")
    print(f"状态：{state}")
    print(f"到期：{expires.isoformat(timespec='minutes') if expires else '解析失败'}"
          + (f"（剩 {left.days}天{left.seconds//3600}小时{(left.seconds%3600)//60}分）" if left and left.total_seconds() > 0 else "（已过期）" if left else ""))
    if oversized:
        print(f"⚠ 面板过大（{size} 字节 > {MAX_PANEL_BYTES}）：正文不读取；"
              f"可用 set --body <文件> 整份覆盖（set 只读头部）")
        return 2
    print(f"正文：{len(body)} 字")
    return 0


def _die(message: str, code: int = 2) -> NoReturn:
    """命令行拒绝：打到 stderr 并用指定退出码（默认 2）退出。"""
    print(f"✗ {message}", file=sys.stderr)
    raise SystemExit(code)


def _read_body_file(path: Path) -> str:
    if not path.is_file():
        _die(f"正文文件不存在：{path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        _die(f"读不到正文文件：{path}（{type(exc).__name__}）")
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        _die(f"正文不是合法 UTF-8：{path}（另存为 UTF-8 再试）")
    return _normalize_newlines(text.lstrip("\ufeff")).strip()


def cmd_set(args) -> int:
    warning = ""
    with panel_lock():
        # 只读头部：面板被写坏成几百 KB 时也必须能整份覆盖（否则 set 救不回来）
        meta, _unknown = read_panel_head()
        if args.session:
            if any(ch.isspace() for ch in args.session):
                _die(f"session_id 不能含空白：{args.session!r}（写 any 或具体会话 id）")
            meta["session_id"] = args.session
        if args.title:
            meta["title"] = args.title
        # 元数据先行校验：坏元数据必须立刻拒绝，不能等写完正文才发现
        for key in META_KEYS:
            if "\n" in meta.get(key, "") or "\r" in meta.get(key, ""):
                _die(f"元数据 {key} 不能包含换行")
        if args.body:
            body = _read_body_file(Path(args.body))
        elif not sys.stdin.isatty():
            # 按字节读再自己解码：文本模式的通用换行会先把 CRLF 折成 LF、把双 CR 折成两个
            # 换行，后面的归一就再也分不清原文到底几个换行了。
            try:
                raw = sys.stdin.buffer.read()
            except AttributeError:      # 被替换成纯文本流的调用方（测试/嵌入）
                raw = sys.stdin.read().encode("utf-8", "replace")
            try:
                incoming = raw.decode("utf-8")
            except UnicodeDecodeError:
                _die("stdin 不是合法 UTF-8（用 --body 指定 UTF-8 文件）")
            body = _normalize_newlines(incoming.lstrip("﻿")).strip()
        else:
            # TTY 且没给正文：不阻塞等输入，直接提示怎么用
            _die("没有正文：用 --body <文件>，或从管道喂入"
                 "（例：cat body.md | python hudctl.py set --ttl 8h）")
        if not body:
            _die("正文为空：注入端会整块跳过，拒绝写入")
        if len(body) > MAX_BODY_CHARS:
            _die(f"正文 {len(body)} 字，超过上限 {MAX_BODY_CHARS} 字（注入端会截断）")
        expires = datetime.now(CST) + parse_ttl(args.ttl)
        estimate = estimate_block(meta, body, expires)
        if estimate > MAX_BLOCK_CHARS:
            # 只警告不拒写：正文没超 1600 就是合法面板，注入端截断时也会保住绝大部分。
            # 旧实现拿整块估长拒写，正文 1403 字 + 28 字 session id 都存不进去。
            warning = (f"⚠ 注入块约 {estimate} 字（含标题行/尾行/余量），超过 {MAX_BLOCK_CHARS}："
                       f"注入时会被截断，建议再减 {estimate - MAX_BLOCK_CHARS} 字")
        meta["expires_at"] = expires.isoformat(timespec="seconds")
        meta["enabled"] = "true"
        write_panel(meta, body)
    print(f"已更新：范围={meta['session_id']} 到期={meta['expires_at']} 正文={len(body)} 字")
    if warning:
        print(warning)
    return 0


# 与 hud_inject.py 的 MAX_CHARS 同值；两个脚本要能各自独立部署，所以不互相 import
MAX_BLOCK_CHARS = 1600
# hud_inject 还可能追加一行 selfcheck 提醒（约 40 字）。这里只留 20 字余量：
# 旧值 80 会把「正文 1403 字 + 28 字 session id」这种完全能注进去的保存判成超限。
BLOCK_MARGIN = 20
HINT_DEFAULT = "default bot：优先用 Hermes 官方机制（delegate_task / cron / kanban）"
HINT_OTHER = "{who} bot：优先用 OTUI（build/open-tui-orchestrator）"


def estimate_block(meta: dict[str, str], body: str, expires) -> int:
    """按注入端真实拼法估整块字数：头行 + 正文 + 尾行 + 余量。

    头行用真标题、真到期时间；括号里注入的是**档名**（payload.profile），所以估长也只取档名。
    旧实现拿档名与面板 session_id 里较长的一个，40 字的 session id 会把头行凭空放大，
    害得正文没超限也保存不了。尾行是 hud_inject 每次都会追加的「执行机制建议」。
    """
    title = meta.get("title") or "会话面板"
    stamp = expires.astimezone(CST).strftime("%m-%d %H:%M") if expires else "12-31 07:00"
    profile = (os.environ.get("HERMES_PROFILE") or os.environ.get("HERMES_SESSION_PROFILE")
               or os.environ.get("HUD_PROFILE") or "default").strip() or "default"
    who = profile                      # 只取档名：session_id 不进头行
    hint = HINT_DEFAULT if profile == "default" else HINT_OTHER.format(who=profile)
    header = f"[HUD·{title}] 生效至 {stamp}（{who}）"
    tail = f"执行机制建议：{hint}"
    return len(header) + len(body) + len(tail) + 2 + BLOCK_MARGIN


def _inspect_panel(raw: str) -> dict:
    """对一份面板文本做完整格式校验（check 与 restore 写入前共用同一套判定）。

    返回 {errors, notices, meta, body, scope, estimate}。只有 **_格式错** 进 errors：
    过期/暂停/范围不匹配是状态提示（正常状态，不影响机制判定）。
    """
    errors: list[str] = []
    notices: list[str] = []
    if raw.startswith("\ufeff"):
        notices.append("文件带 BOM（注入端能容忍，建议去掉）")
        raw = raw.lstrip("\ufeff")
    raw = _normalize_newlines(raw)

    seen: set[str] = set()
    for line in raw.splitlines()[:24]:
        if not line.strip():
            break
        key, sep, value = line.partition(":")
        name = key.strip().lower()
        if not sep or name not in META_KEYS:
            break
        if name in seen:
            errors.append(f"重复元数据：{name}")
        seen.add(name)
        if any(ord(ch) < 32 for ch in value):
            errors.append(f"元数据 {name} 含控制字符")

    meta, body, unknown = _split_panel_ex(raw)
    if unknown:
        errors.append(f"头部未知键：{', '.join(unknown)}"
                      "（未知键会让整块失效：注入端直接跳过，绝不把元数据当正文注入）")
    for key in META_KEYS:
        if key not in meta:
            errors.append(f"缺少元数据：{key}")
    scope = (meta.get("session_id") or "").strip()
    if "session_id" in meta and not scope:
        errors.append("session_id 为空（空值=无效，注入端会跳过；作用于全部要写 any）")
    elif scope and scope.lower() not in {"any", "*"} and len(scope) < 8:
        notices.append(f"session_id 看起来不像会话 id：{scope!r}")
    enabled = _enabled_value(meta)
    if enabled is None:
        errors.append(f"enabled 取值不合法：{meta.get('enabled')!r}"
                      "（只认 true/false/1/0/yes/no/on/off；非法值注入端会整块跳过）")
    elif not enabled:
        notices.append("面板已暂停（enabled=false）：注入会被跳过（hudctl on 恢复）")
    expires = parse_expiry(meta.get("expires_at", ""))
    if expires is None:
        errors.append(f"expires_at 无法解析：{meta.get('expires_at')!r}")
    else:
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=CST)
        if expires <= datetime.now(CST):
            notices.append(f"面板已过期：{meta.get('expires_at')}（注入会被跳过；on --ttl 8h 可续期）")
    if scope and scope.lower() not in {"any", "*"}:
        notices.append(f"面板只作用于会话 {scope}：其它会话不会注入")
    if not body:
        errors.append("正文为空：注入端会跳过")
    if hud_panel is None:
        errors.append("缺 hud_panel.py：无法解析条目（注入端也会整块跳过）")
    else:
        degenerate = hud_panel.degenerate_reason(body)
        if degenerate:
            errors.append(f"代码围栏未闭合（{degenerate}）：条目边界不可信，注入端会整块跳过")
        errors.extend(hud_panel.validate(body))
    estimate = estimate_block(meta, body, expires)
    if estimate > MAX_BLOCK_CHARS:
        # 只是「整块会被截断」的提醒：正文上限由 set 单独拦（>1600 才拒），这里不判红，
        # 否则正文 1590 字这种完全可用（截断也保住大部分）的面板会被 check 卡死。
        notices.append(f"注入块约 {estimate} 字（标题行 + 正文 + 注入尾行，另含 "
                       f"{BLOCK_MARGIN} 字提醒余量），超过 {MAX_BLOCK_CHARS} 上限（注入时会被截断）")
    return {"errors": errors, "notices": notices, "meta": meta, "body": body,
            "scope": scope, "estimate": estimate}


def cmd_check(args) -> int:
    """校验面板格式：**只有真格式错**让 exit code 变 1；过期/暂停/范围/整块超长是「状态提示」。

    状态提示不算失败：面板过期是正常状态（注入会跳过），把它当机制坏了的判据就是假红。
    超大面板只读头部/只报大小，不把几百 KB 读进来再解析。
    """
    if not PANEL.exists():
        print(f"✗ 面板不存在：{PANEL}")
        return 1
    unsafe = _unsafe_path(PANEL)
    if unsafe:
        print(f"✗ 面板{unsafe}，拒绝读取：{PANEL}（检查/修复前先换成普通文件）")
        return 1
    size = _panel_size()
    if size > MAX_PANEL_BYTES:
        print(f"✗ 面板过大（{size} 字节 > {MAX_PANEL_BYTES}）：{PANEL}"
              f"（可用 set --body <文件> 整份覆盖；本命令只读头部）")
        return 1
    try:
        raw = _read_text_exact(PANEL)
    except UnicodeDecodeError:
        print(f"✗ 面板不是合法 UTF-8：{PANEL}")
        return 1
    except OSError as exc:
        print(f"✗ 读不到面板：{PANEL}（{type(exc).__name__}）")
        return 1
    data = _inspect_panel(raw)
    errors, notices = data["errors"], data["notices"]
    meta, body, scope = data["meta"], data["body"], data["scope"]
    estimate = data["estimate"]

    if getattr(args, "json", False):
        print(json.dumps({"ok": not errors, "errors": errors, "notices": notices,
                          "scope": scope, "expires_at": meta.get("expires_at", ""),
                          "body_chars": len(body), "estimate": estimate},
                         ensure_ascii=False, indent=2))
    else:
        if errors:
            print("✗ 面板格式有问题：")
            for item in errors:
                print("  - " + item)
        for item in notices:
            print("  · 状态提示：" + item)
        if not errors:
            print(f"✓ 面板格式正常：范围={scope or 'any'} 到期={meta.get('expires_at')} "
                  f"正文={len(body)} 字（整块约 {estimate} 字 / 上限 {MAX_BLOCK_CHARS}）")
    strict_fail = bool(notices) and getattr(args, "strict", False)
    return 1 if errors or strict_fail else 0


def _is_expired(meta: dict) -> bool:
    """到期时间不可解析也算「过期」（注入端会跳过）。"""
    expires = parse_expiry(meta.get("expires_at", ""))
    if expires is None:
        return True
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=CST)
    return expires <= datetime.now(CST)


def cmd_toggle(flag: str):
    def run(args) -> int:
        with panel_lock():
            meta, body = read_panel()
            if flag == "true":
                ttl = getattr(args, "ttl", None)
                if ttl:
                    meta["expires_at"] = (datetime.now(CST) + parse_ttl(ttl)).isoformat(timespec="seconds")
                elif _is_expired(meta):
                    _die(f"面板已过期（{meta.get('expires_at', '?')}）：直接 on 只是打开一个永不生效的面板。"
                         "顺手续期用 hudctl on --ttl 8h（或 set 换正文+续期）")
                if not (body or "").strip():
                    _die("正文为空：on 之后注入端仍会跳过；先用 set --body <文件> 写入正文")
                if not (meta.get("session_id") or "").strip():
                    _die("session_id 为空：on 之后注入端仍会跳过；用 set --session any 或具体会话 id 补上")
            meta["enabled"] = flag
            write_panel(meta, body)
        print(f"enabled={flag}" + (f" 到期={meta['expires_at']}" if flag == "true" else ""))
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


def _ensure_snaps() -> None:
    """快照目录必须是目录且可建；被放成同名文件时友好退出（别抛 traceback）。"""
    if SNAPS.exists() and not SNAPS.is_dir():
        _die(f"快照目录 {SNAPS} 是个文件，不是目录：先把它移走再跑 snapshot/restore/prune")
    try:
        SNAPS.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        _die(f"建不了快照目录：{SNAPS}（{type(exc).__name__}: {exc}）")


def _snapshots() -> list[Path]:
    """快照目录里的合法快照，按时间（取自文件名）从旧到新；杂项 .md / 同名目录 / 软链都不算。"""
    found = [(key, path.name, path) for path in SNAPS.glob("*.md")
             if _is_regular_file(path) and (key := _snapshot_key(path)) is not None]
    return [path for _, _, path in sorted(found)]


def _snapshot_named(name: str) -> Path:
    """--file / 位置参数里的快照文件名：只认基名，不许带路径，必须是普通文件（非软链）。"""
    clean = (name or "").strip()
    if not clean:
        raise SystemExit("快照文件名不能为空：给 snapshots/ 下的名字（如 pre-restore-2026-10-22-120000.md）")
    if Path(clean).name != clean:
        raise SystemExit(f"--file 只认文件名，不能带路径：{clean}")
    target = SNAPS / clean
    unsafe = _unsafe_path(target)
    if unsafe:
        raise SystemExit(f"快照{unsafe}，拒绝使用：{target}（还原前先换成普通文件）")
    if not target.is_file():
        raise SystemExit(f"快照不存在：{target}")
    return target


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


def _snapshot_to(target: Path) -> Path:
    """原子地把当前面板复制成 target；失败时抛 OSError 并清掉临时文件。

    临时文件用 mkstemp（O_EXCL，名字不可预算）+ 写入前 lstat 拒绝软链目标：
    软链面板不拍（会把链接指向的文件内容当面板存下来），软链目标不覆盖。
    """
    unsafe_panel = _unsafe_path(PANEL)
    if unsafe_panel:
        raise OSError(f"面板{unsafe_panel}，拒绝快照")
    unsafe_target = _unsafe_path(target)
    if unsafe_target:
        raise OSError(f"目标快照{unsafe_target}，拒绝覆盖")
    fd, tmp_name = tempfile.mkstemp(prefix=f"{target.name}.", suffix=".tmp", dir=str(target.parent))
    os.close(fd)
    tmp = Path(tmp_name)
    try:
        shutil.copy2(PANEL, tmp)
        os.replace(tmp, target)  # 原子落盘：不留半份快照，也不覆盖已有快照
    finally:
        if tmp.exists() or tmp.is_symlink():
            try:
                tmp.unlink()
            except OSError:
                pass
    return target


def _pre_restore_target(now: datetime) -> Path:
    """恢复前的「现场」快照：名字带 pre-restore 前缀 + 时间戳。

    刻意**不**用 `YYYY-MM-DD.md` 命名：它不参与 `restore latest`、也不被 prune 当快照处理，
    这样「恢复前的现场」不会顶掉用户真正的快照序列，两者在目录里一眼可分。
    """
    stamp = f"pre-restore-{now.strftime('%Y-%m-%d-%H%M%S')}"
    candidate = SNAPS / f"{stamp}.md"
    seq = 1
    while candidate.exists() or candidate.is_symlink():
        seq += 1
        candidate = SNAPS / f"{stamp}-{seq}.md"
    return candidate


def cmd_snapshot(_args) -> int:
    _ensure_snaps()
    with panel_lock():          # 持锁复制：并发 set 不能让我们拍到半份/新旧交界的文件
        unsafe = _unsafe_path(PANEL)
        if unsafe:
            _die(f"面板{unsafe}，拒绝快照：{PANEL}（快照只拍普通文件）")
        if not PANEL.exists():
            read_panel(create=True)     # 保持旧行为：面板不存在时先从 panel.example.md 复制
        size = _panel_size()
        target = _snapshot_target(datetime.now(CST))
        try:
            _snapshot_to(target)
        except OSError as exc:
            raise SystemExit(f"快照失败（{type(exc).__name__}: {exc}）：{SNAPS} 不可写？")
        if size <= MAX_PANEL_BYTES:
            _meta, body = read_panel(create=False)
            detail = f"{len(body)} 字"
        else:
            detail = f"{size} 字节（过大，已原样快照）"
    print(f"快照：{target}（{detail}）")
    return 0


def _resolve_restore_target(args) -> Path:
    """把参数解析成一份快照文件：latest / YYYY-MM-DD / --file <名> / pre-restore-*.md。"""
    wanted_file = getattr(args, "file", None)
    positional = getattr(args, "date", None)
    if wanted_file:
        return _snapshot_named(wanted_file)
    if not positional:
        raise SystemExit("给日期（YYYY-MM-DD / latest）或用 --file <快照文件名>")
    if positional.lower().endswith(".md"):
        # 允许直接写 pre-restore-2026-10-22-120000.md 这类文件名
        return _snapshot_named(positional)
    if positional != "latest" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", positional):
        raise SystemExit(f"日期格式应为 YYYY-MM-DD 或 latest：{positional}")
    snaps = _snapshots()        # pre-restore-*.md 不匹配快照命名，永远不参与 latest
    if not snaps:
        raise SystemExit("没有快照可恢复")
    if positional == "latest":
        return snaps[-1]
    wanted = tuple(int(part) for part in positional.split("-"))
    same_day = []
    for path in snaps:
        key = _snapshot_key(path)
        if key is not None and key[:3] == wanted:
            same_day.append(path)
    if not same_day:
        raise SystemExit(f"没有这份快照：{positional}（{SNAPS} 下没有该日快照）")
    return same_day[-1]          # 同日多份 → 取最新一份


def cmd_restore(args) -> int:
    _ensure_snaps()
    target = _resolve_restore_target(args)
    unsafe = _unsafe_path(target)          # 解析与实际读取之间再过一遍（解析可能来自 latest）
    if unsafe:
        _die(f"快照{unsafe}，拒绝恢复：{target}")
    try:
        text = _read_text_exact(target)
    except UnicodeDecodeError:
        raise SystemExit(f"快照不是合法 UTF-8：{target}")
    except OSError as exc:
        raise SystemExit(f"读不到快照：{target}（{type(exc).__name__}）")
    # 先像 check 一样验一遍**快照本身**：坏快照写进去等于把现网面板一起弄坏，宁可不恢复
    raw_report = _inspect_panel(text)
    if raw_report["errors"]:
        _die(f"快照 {target.name} 内容不合法，拒绝覆盖现网面板："
             + "；".join(raw_report["errors"][:4]))
    meta, body, unknown = _split_panel_ex(text.lstrip("\ufeff"))
    if unknown:
        _die(f"快照 {target.name} 头部有未知键（{', '.join(unknown)}），拒绝恢复")
    if args.session:
        meta["session_id"] = args.session
    meta["expires_at"] = (datetime.now(CST) + parse_ttl(args.ttl)).isoformat(timespec="seconds")
    meta["enabled"] = "true"
    candidate = render_panel(meta, body)
    final_report = _inspect_panel(candidate)   # 改写元数据之后再验一遍（要写进去的就是它）
    if final_report["errors"]:
        _die(f"快照 {target.name} 恢复后仍不合法，拒绝写入："
             + "；".join(final_report["errors"][:4]))
    with panel_lock():
        pre = None
        if PANEL.exists():
            try:
                pre = _snapshot_to(_pre_restore_target(datetime.now(CST)))
            except OSError as exc:
                # 拍不成现场快照就不恢复：恢复是不可逆覆盖，没有退路的事不做
                _die(f"恢复前快照写不进去（{type(exc).__name__}: {exc}），拒绝恢复。"
                     f"先确认 {SNAPS} 可写")
        write_panel(meta, body)
    said = f"（恢复前现场已存：{pre.name}）" if pre else "（当前没有面板，无需 pre-restore 快照）"
    print(f"已从 {target.name} 恢复：范围={meta['session_id']} 到期={meta['expires_at']} "
          f"正文={len(body)} 字 {said}")
    return 0


def cmd_prune(args) -> int:
    """默认只列清单；加 --yes 才真删。只动本工具命名的快照，手工 .md 一律不碰。"""
    if args.keep < 1:
        raise SystemExit(f"--keep 必须 >= 1：{args.keep}")
    if SNAPS.exists() and not SNAPS.is_dir():
        raise SystemExit(f"快照目录 {SNAPS} 是个文件，不是目录：先把它移走再跑 prune")
    snaps = _snapshots()  # 新旧两种命名混排，按文件名里的时间从旧到新
    drop = snaps[: max(0, len(snaps) - args.keep)]
    strays = []
    if SNAPS.is_dir():
        strays = sorted(p.name for p in SNAPS.glob("*.md")
                        if p.is_file() and _snapshot_key(p) is None)
    if not args.yes:
        print(f"将保留最近 {min(args.keep, len(snaps))} 份，待删除 {len(drop)} 份（未删除）")
        for path in drop:
            print(f"  - {path.name}")
        if drop:
            print("加 --yes 才真删：python hudctl.py prune --keep "
                  f"{args.keep} --yes")
        else:
            print("没有需要删除的快照。")
    else:
        deleted = 0
        for path in drop:
            if _snapshot_key(path) is None:   # 双保险：只删本工具命名的快照
                continue
            try:
                path.unlink()
                deleted += 1
            except OSError as exc:
                raise SystemExit(f"删不掉 {path.name}：{type(exc).__name__}")
        print(f"保留 {min(args.keep, len(snaps))} 份，删除 {deleted} 份")
    restored = [name for name in strays if name.startswith("pre-restore-")]
    manual = [name for name in strays if not name.startswith("pre-restore-")]
    if restored:
        print(f"（恢复前现场快照 {len(restored)} 份：不参与 latest / 不计入 prune；"
              f"确要清理请手动删除 {SNAPS}{os.sep}pre-restore-*.md，或用 "
              f"restore --file <名> 先取回再删）")
    if manual:
        print(f"（另有 {len(manual)} 个非本工具命名的 .md，是手工文件：不计入快照、也不删除："
              f"{', '.join(manual[:5])}{' …' if len(manual) > 5 else ''}）")
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
    p_check = sub.add_parser("check")
    p_check.add_argument("--json", action="store_true", help="机器可读：errors / notices 分开")
    p_check.add_argument("--strict", action="store_true",
                         help="状态提示（过期/暂停/范围不匹配）也算失败")
    p_check.set_defaults(func=cmd_check)
    sub.add_parser("off").set_defaults(func=cmd_toggle("false"))
    p_on = sub.add_parser("on")
    p_on.add_argument("--ttl", default=None, help="顺手续期（例：8h）；不给则沿用原到期时间")
    p_on.set_defaults(func=cmd_toggle("true"))
    sub.add_parser("snapshot").set_defaults(func=cmd_snapshot)
    p_restore = sub.add_parser("restore")
    p_restore.add_argument("date", nargs="?", default=None,
                           help="YYYY-MM-DD 或 latest（用 --file 时可省略；也可直接给快照文件名）")
    p_restore.add_argument("--file", help="按文件名恢复（含 pre-restore-*.md）；与日期二选一")
    p_restore.add_argument("--ttl", default="12h")
    p_restore.add_argument("--session")
    p_restore.set_defaults(func=cmd_restore)
    p_prune = sub.add_parser("prune")
    p_prune.add_argument("--keep", type=int, default=14)
    p_prune.add_argument("--yes", action="store_true", help="真的删除（不加只列清单）")
    p_prune.set_defaults(func=cmd_prune)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
