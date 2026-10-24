#!/usr/bin/env python3
"""hud_panel.py — 面板解析的唯一实现（inject / ctl / selfcheck 共用，避免几套解析漂移）。

三件事：
  1. 头部元数据：`key: value`；空行即结束元数据块；支持行内注释（空白 + `#` 之后忽略）。
     头部出现**不认识的键**时不静默当正文——返回 unknown 列表，调用方 fail-closed。
  2. 条目作用域（写在条目开头，注入时会从正文里删掉）：

        9. [scope: all] 所有会话（含以后新建）
        10. [scope: session:20261024_120358_06278b] 只对这一条会话
        11. [scope: session:id1,id2] 多条会话
        12. [scope: profile:default,ceo] 只对这几个档
        13. 不带标注 = 跟随面板 session_id（默认 all）

     `[scope: …]` 也可以写在该条目的**续行**行首（缩进不限），同样算这一条的标注。
     解析口径：一个新条目 = 以 `1.` / `1)` / `1、` / `-` / `*` 开头的顶格行；
     缩进的续行归上一条；空行保留在原来位置；代码围栏里的内容不参与解析。

  3. 机制指纹：关键源码文件的**内容哈希**（不是 size+mtime），inject 与 selfcheck 同一口径，
     家目录口径也由 hermes_home() 统一。
"""
from __future__ import annotations

import hashlib
import os
import re

ENTRY_RE = re.compile(r"^(?:\d+[.)、]|[-*])\s+")
SCOPE_RE = re.compile(r"^\s*\[scope:\s*([^\]]*?)\s*\]\s*", re.IGNORECASE)
INLINE_SCOPE_RE = re.compile(r"\[scope:[^\]]*\]", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
META_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
# 会话 id / 档名允许的字符：字母数字开头，其余可用 _ . -
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

META_KEYS = ("session_id", "expires_at", "title", "enabled")
META_SCAN_LINES = 24
KINDS = ("all", "session", "profile")

MECH_FILES = (
    "agent/turn_context.py",
    "hermes_cli/plugins_dispatch.py",
    "hermes_cli/hooks.py",
    "agent/shell_hooks.py",
)


# --------------------------------------------------------------------------- #
# 头部元数据
# --------------------------------------------------------------------------- #
def strip_comment(value: str) -> str:
    """元数据行里的行内注释（空白 + #）去掉；正文不做处理。"""
    for sep in (" #", "\t#"):
        idx = value.find(sep)
        if idx != -1:
            value = value[:idx]
    return value.strip()


def split_meta(text: str) -> tuple[dict[str, str], str, list[str]]:
    """切成 (元数据, 正文, 未知键列表)。

    与旧实现同口径：前 24 行、连续的非空 `key: value` 行；空行或非元数据行即正文开始。
    唯一新增：头部出现 `somekey: value` 且 somekey 不在 META_KEYS 时记进 unknown
    （调用方必须 fail-closed，绝不能把元数据当正文注进上下文）。
    """
    meta: dict[str, str] = {}
    unknown: list[str] = []
    lines = text.splitlines()
    body_start = 0
    for i, line in enumerate(lines[:META_SCAN_LINES]):
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
                meta[name] = strip_comment(value)
                body_start = i + 1
                continue
            if META_KEY_RE.match(key.strip()):
                unknown.append(name)
                body_start = i + 1
                continue
        break
    body = "\n".join(lines[body_start:]).strip()
    return meta, body, unknown


# --------------------------------------------------------------------------- #
# 作用域
# --------------------------------------------------------------------------- #
def parse_scope(token: str) -> tuple[str, set[str]] | None:
    """'session:a,b' → ('session', {'a','b'})；非法返回 None（fail-closed）。

    值按逗号/空白切分后逐个校验：出现半个 token（`session:A profile:p`）整条判非法，
    绝不把 `profile:p` 当成会话 id 的一部分。
    """
    raw = (token or "").strip()
    if not raw:
        return None
    if raw.lower() in {"all", "*", "any"}:
        return ("all", set())
    if ":" not in raw:
        return None
    kind, values = raw.split(":", 1)
    kind = kind.strip().lower()
    if kind not in ("session", "profile"):
        return None
    chunks = [chunk for chunk in re.split(r"[,\s]+", values.strip()) if chunk]
    if not chunks:
        return None
    if chunks == ["*"]:
        return ("all", set())
    items: set[str] = set()
    for chunk in chunks:
        if ":" in chunk:
            return None        # 半个 token：整条标注判非法
        if not ID_RE.match(chunk):
            return None
        items.add(chunk)
    return (kind, items) if items else None


def _fence_mask(lines: list[str]) -> list[bool]:
    """标出代码围栏内的行（含围栏行本身），这些行不参与条目/标注解析。"""
    mask = [False] * len(lines)
    fence: str | None = None
    opened = -1
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if m:
            if fence is None:
                fence, opened = m.group(1), i
            else:
                fence = None
                for j in range(opened, i + 1):
                    mask[j] = True
            continue
        if fence is not None:
            mask[i] = True
    if fence is not None:            # 围栏没闭合：从开栏到结尾都算围栏内
        for j in range(opened, len(lines)):
            mask[j] = True
    return mask


def unclosed_fence(body: str) -> bool:
    """代码围栏是否未闭合。未闭合时条目边界不可信 → 调用方必须整块跳过。"""
    fence = None
    for line in body.splitlines():
        if FENCE_RE.match(line):
            fence = None if fence else True
    return fence is not None


def degenerate_reason(body: str) -> str:
    """正文处于「不能按条目安全解析」的状态时返回原因，否则空串。"""
    if unclosed_fence(body):
        return "unclosed-code-fence"
    return ""


def entries(body: str) -> list[dict]:
    """切成条目：{start,end,scope_token,scope,scope_line,conflict}。

    scope=None 表示非法或未标注（未标注时由调用方补默认范围）；
    scope_line 是写在续行行首的那处标注的行号（删除标注时用）；同一 entry 两处标注 → conflict。
    """
    lines = body.splitlines()
    mask = _fence_mask(lines)
    starts = [i for i, line in enumerate(lines) if not mask[i] and ENTRY_RE.match(line)]
    out: list[dict] = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        first = lines[start]
        marker = ENTRY_RE.match(first)
        rest = first[len(marker.group(0)):] if marker else first
        m = SCOPE_RE.match(rest)
        token = m.group(1) if m else None
        scope_line = None
        conflict = False
        for i in range(start + 1, end):
            if mask[i]:
                continue
            mm = SCOPE_RE.match(lines[i])
            if not mm:
                continue
            if token is not None:
                conflict = True
                scope_line = i
            else:
                token, scope_line = mm.group(1), i
            break
        scope = None if conflict else (parse_scope(token) if token else None)
        out.append({"start": start, "end": end, "scope_token": token, "scope": scope,
                    "scope_line": scope_line, "conflict": conflict})
    return out


def scope_matches(scope, session_id: str, profile: str) -> bool:
    if scope is None:
        return False  # 非法/缺失一律不匹配；默认继承由调用方先填好
    kind, items = scope
    if kind == "all":
        return True
    if kind == "session":
        return session_id in items
    if kind == "profile":
        return (profile or "default") in items
    return False


def _strip_leading_scope(line: str) -> str:
    """去掉行首的 [scope: …]；整行只有标注时返回空串。"""
    return SCOPE_RE.sub("", line, count=1).rstrip()


def filter_body(body: str, session_id: str, profile: str,
                default_scope=None) -> tuple[str, int, int]:
    """按作用域过滤正文，并删掉 [scope: …] 标注。返回 (新正文, 保留条数, 总条数)。"""
    lines = body.splitlines()
    items = entries(body)
    if not items:
        return body, 1, 1
    out: list[str] = lines[: items[0]["start"]]  # 保留第一个条目之前的标题/前言
    kept = 0
    for item in items:
        if item["scope_token"] is not None:
            scope = item["scope"]  # 非法 / 两处冲突 → None → 不匹配（fail-closed）
        else:
            scope = default_scope if default_scope is not None else ("all", set())
        if not scope_matches(scope, session_id, profile):
            continue
        block = lines[item["start"]:item["end"]]
        marker_m = ENTRY_RE.match(block[0])
        marker = marker_m.group(0) if marker_m else ""
        rest = SCOPE_RE.sub("", block[0][len(marker):], count=1)
        cleaned = [(marker + rest).rstrip()]
        for idx in range(item["start"] + 1, item["end"]):
            line = lines[idx]
            if idx == item["scope_line"]:
                line = _strip_leading_scope(line)
                if not line.strip():
                    continue   # 整行只有标注 → 丢掉，不留空行
            cleaned.append(line)
        out.extend(cleaned)
        kept += 1
    return "\n".join(out).strip(), kept, len(items)


def entry_texts(body: str) -> list[str]:
    """给 ctl 用：每条条目的**完整正文**（去掉列表标记与 [scope: …] 标注，含续行）。

    preview 的条目增删比较必须用它，而不是截断后的标题：长条目前缀相同、条目重复，
    都会让「截断后做集合比较」得出错误的增删结论。
    """
    lines = body.splitlines()
    result: list[str] = []
    for item in entries(body):
        block = lines[item["start"]:item["end"]]
        first = block[0]
        marker = ENTRY_RE.match(first)
        rest = first[len(marker.group(0)):] if marker else first
        rest = SCOPE_RE.sub("", rest, count=1).strip()
        tail: list[str] = []
        for idx in range(item["start"] + 1, item["end"]):
            line = block[idx - item["start"]]
            if idx == item["scope_line"]:
                line = _strip_leading_scope(line)
                if not line.strip():
                    continue
            tail.append(line.rstrip())
        result.append("\n".join([rest] + tail).strip())
    return result


def entry_scopes(body: str) -> list[tuple[str, str | None]]:
    """给 ctl 显示用：[(条目首行（截到 60 字）, scope_token 或 None)]。

    比较增删请用 entry_texts()；这里截断只为显示。
    """
    return [((text.splitlines()[0] if text else "")[:60], item["scope_token"])
            for text, item in zip(entry_texts(body), entries(body))]


def invalid_scopes(body: str) -> list[str]:
    """返回非法标注列表（写了但解析不了，或同一 entry 写了两处）。"""
    return [item["scope_token"] for item in entries(body)
            if item["scope_token"] is not None and item["scope"] is None]


def validate(body: str) -> list[str]:
    """返回作用域相关的告警（非法 / 两处冲突 / 不在行首）。"""
    warns: list[str] = []
    for item in entries(body):
        if item["scope_token"] is None:
            continue
        if item["conflict"]:
            warns.append(f"[scope: {item['scope_token']}] 同一 entry 写了两处标注（注入时整条失效）")
        elif item["scope"] is None:
            warns.append(f"[scope: {item['scope_token']}] 非法（只认 all / session:id / profile:name）")
    lines = body.splitlines()
    mask = _fence_mask(lines)
    for item in entries(body):
        for idx in range(item["start"], item["end"]):
            if mask[idx]:
                continue
            line = lines[idx]
            clean = re.sub(r"`[^`]*`", "", line)   # 文档里的示例不算写错位置
            hits = INLINE_SCOPE_RE.findall(clean)
            if not hits:
                continue
            if idx == item["start"]:
                marker = ENTRY_RE.match(line)
                head = line[len(marker.group(0)):] if marker else line
                if SCOPE_RE.match(head):
                    hits = hits[1:]      # 行首那个已被当标注
            elif SCOPE_RE.match(line):
                hits = hits[1:]          # 续行行首那个也已被当标注
            if hits:
                warns.append(f"条目「{line.strip()[:20]}…」的标注不在行首，注入时不会生效也不会被删掉")
    return warns


# --------------------------------------------------------------------------- #
# 机制指纹 / 家目录口径（inject 与 selfcheck 必须一致）
# --------------------------------------------------------------------------- #
def hermes_home() -> str:
    """HERMES_HOME → %LOCALAPPDATA%/hermes → Windows 默认 ~/AppData/Local/hermes → POSIX XDG。"""
    env = os.environ.get("HERMES_HOME")
    if env:
        return env
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return os.path.join(local, "hermes")
    home = os.path.expanduser("~")
    if os.name == "nt":
        return os.path.join(home, "AppData", "Local", "hermes")
    return os.path.join(home, ".local", "share", "hermes")


def mech_fingerprint(home: str | None = None) -> str:
    """关键源码文件的**内容** sha256。

    不再用 size+mtime：同样的文件被 checkout 一遍 mtime 就变（假红），
    而改了内容但大小/时间没变则会漏报。inject 与 selfcheck 都调这一份，
    保证「同一口径」——否则一端算出来的指纹永远对不上另一端写的值。
    """
    source = os.path.join(home or hermes_home(), "hermes-agent")
    digest = hashlib.sha256()
    for rel in MECH_FILES:
        digest.update(rel.encode())
        digest.update(b"\0")
        try:
            with open(os.path.join(source, rel), "rb") as fh:
                digest.update(hashlib.sha256(fh.read()).digest())
        except OSError:
            digest.update(b"missing")
        digest.update(b"\n")
    return digest.hexdigest()[:16]
