#!/usr/bin/env python3
"""hud_panel.py — 面板正文的作用域解析（inject 与 ctl 共用，避免两套解析漂移）。

条目作用域写法（写在条目开头，注入时会从正文里删掉）：

    9. [scope: all] 所有会话（含以后新建）
    10. [scope: session:20261024_120358_06278b] 只对这一条会话
    11. [scope: session:id1,id2] 多条会话
    12. [scope: profile:default,ceo] 只对这几个档
    13. 不带标注 = 跟随面板 session_id（默认 all）

解析口径：一个新条目 = 以 `1.` / `1)` / `1、` / `-` / `*` 开头的行；
缩进的续行归上一条；空行保留在原来位置。
"""
from __future__ import annotations

import re

ENTRY_RE = re.compile(r"^(?:\d+[.)、]|[-*])\s+")
SCOPE_RE = re.compile(r"^\s*\[scope:\s*([^\]]*?)\s*\]\s*", re.IGNORECASE)
INLINE_SCOPE_RE = re.compile(r"\[scope:[^\]]*\]", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")

KINDS = ("all", "session", "profile")


def parse_scope(token: str) -> tuple[str, set[str]] | None:
    """'session:a,b' → ('session', {'a','b'})；非法返回 None。"""
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
    raw_items = values.replace(",", " ").split()
    if raw_items == ["*"]:
        return ("all", set())
    items = {v.strip() for v in raw_items if v.strip()}
    return (kind, items) if items else None


def _entry_starts(lines: list[str]) -> list[int]:
    """顶格条目行；代码围栏内的不算。"""
    starts: list[int] = []
    fence: str | None = None
    for i, line in enumerate(lines):
        m = FENCE_RE.match(line)
        if m:
            fence = None if fence else m.group(1)
            continue
        if fence is None and ENTRY_RE.match(line):
            starts.append(i)
    return starts


def entries(body: str) -> list[dict]:
    """切成条目：{start,end,scope_token,scope}；scope=None 表示跟随面板默认。"""
    lines = body.splitlines()
    starts = _entry_starts(lines)
    out = []
    for n, start in enumerate(starts):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        first = lines[start]
        marker = ENTRY_RE.match(first)
        rest = first[len(marker.group(0)):] if marker else first
        m = SCOPE_RE.match(rest)
        token = m.group(1) if m else None
        out.append({"start": start, "end": end, "scope_token": token,
                    "scope": parse_scope(token) if token else None})
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
            scope = item["scope"]  # 非法 → None → 不匹配（fail-closed）
        else:
            scope = default_scope if default_scope is not None else ("all", set())
        if not scope_matches(scope, session_id, profile):
            continue
        block = lines[item["start"]:item["end"]]
        first = block[0]
        m = ENTRY_RE.match(first)
        marker = m.group(0) if m else ""
        rest = SCOPE_RE.sub("", first[len(marker):], count=1)
        block = [(marker + rest).rstrip()] + block[1:]
        out.extend(block)
        kept += 1
    return "\n".join(out).strip(), kept, len(items)


def entry_scopes(body: str) -> list[tuple[str, str | None]]:
    """给 ctl 用：[(条目标题, scope_token 或 None)]。"""
    lines = body.splitlines()
    result = []
    for item in entries(body):
        text = lines[item["start"]]
        text = SCOPE_RE.sub("", ENTRY_RE.sub("", text, count=1)).strip()
        result.append((text[:60], item["scope_token"]))
    return result


def invalid_scopes(body: str) -> list[str]:
    """返回非法标注列表（条目开头写了但解析不了）。"""
    return [item["scope_token"] for item in entries(body)
            if item["scope_token"] is not None and item["scope"] is None]


def validate(body: str) -> list[str]:
    """返回作用域相关的告警（非法、写在非行首）。"""
    warns = [f"[scope: {token}] 非法（只认 all / session:id / profile:name）"
             for token in invalid_scopes(body)]
    lines = body.splitlines()
    for item in entries(body):
        first = lines[item["start"]]
        # 代码片段里的示例不算“写错位置”（如文档里用 `[scope: session:<id>]` 举例）
        clean = re.sub(r"`[^`]*`", "", first)
        inline = INLINE_SCOPE_RE.findall(clean)
        if item["scope_token"] is not None:
            inline = inline[1:]  # 行首那个已被当标注，同行再出现才算写错位置
        if inline:
            warns.append(f"条目「{first[:20]}…」的标注不在行首，注入时不会生效也不会被删掉")
    return warns
