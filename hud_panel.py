#!/usr/bin/env python3
"""hud_panel.py — 面板解析的唯一实现（inject / ctl / selfcheck 共用，避免几套解析漂移）。

三件事：
  1. 头部元数据：`key: value`；空行即结束元数据块；支持行内注释（空白 + `#` 之后忽略）。
     头部出现**不认识的键**时不静默当正文——返回 unknown 列表，调用方 fail-closed。
  2. 条目作用域（写在条目开头，注入时会从正文里删掉）：

        9. [scope: all] 所有会话（含以后新建）
        10. [scope: session:20260101_000000_abcdef] 只对这一条会话
        11. [scope: session:id1,id2] 多条会话
        12. [scope: profile:default,ceo] 只对这几个档
        13. 不带标注 = 跟随面板 session_id（默认 all）

     `[scope: …]` 也可以写在该条目的**续行**行首（缩进不限），同样算这一条的标注。
     同一条目写了**两处及以上**（首行一处 + 续行，或续行里两处）→ 判冲突，整条不注入。
     解析口径：一个新条目 = 以 `1.` / `1)` / `1、` / `-` / `*` 开头的顶格行；
     缩进的续行归上一条；空行保留在原来位置；代码围栏里的内容不参与解析。

     换行统一按 `\n` 处理（`normalize_newlines`）：CRLF、裸 CR、旧版 `hudctl set` 在
     Windows 上叠出来的 `\r\r\n` 都只算一个换行，绝不因为行尾符多注出空行。

  3. 机制指纹：关键源码文件的**内容哈希**（不是 size+mtime），inject 与 selfcheck 同一口径。
     源码根从家目录向上找 `hermes-agent`（HERMES_HOME 指到 profiles/<名> 时也能找到），
     因此指纹只跟源码内容走，与当前档无关。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata

ENTRY_RE = re.compile(r"^(?:\d+[.)、]|[-*])\s+")
SCOPE_RE = re.compile(r"^\s*\[scope:\s*([^\]]*?)\s*\]\s*", re.IGNORECASE)
INLINE_SCOPE_RE = re.compile(
    r"[\[［【（〔「『｛＜(<{]\s*scop\w*\b\s*:?[^\]］】）〕」』｝＞)>}]*[\]］】）〕」』｝＞)>}]?",
    re.IGNORECASE,
)
BARE_SCOPE_RE = re.compile(r"(?m)^\W*scop\w*\s*[:：]", re.IGNORECASE)
FENCE_RE = re.compile(r"^\s*(```|~~~)")
META_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")
# 会话 id / 档名允许的字符：字母数字开头，其余可用 _ . -
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]{0,63}$")

META_KEYS = ("session_id", "expires_at", "title", "enabled")
META_SCAN_LINES = 24
KINDS = ("all", "session", "profile")

# enabled 的合法取值表（inject / ctl check / impact 共用一张表：非法值一律 fail-closed）
ENABLED_TRUE = frozenset({"true", "1", "yes", "on"})
ENABLED_FALSE = frozenset({"false", "0", "no", "off"})
ENABLED_VALUES = ENABLED_TRUE | ENABLED_FALSE


MECH_FILES = (
    "agent/turn_context.py",
    "hermes_cli/plugins_dispatch.py",
    "hermes_cli/hooks.py",
    "agent/shell_hooks.py",
)


# --------------------------------------------------------------------------- #
# 换行归一
# --------------------------------------------------------------------------- #
def normalize_newlines(text: str) -> str:
    r"""把 CRLF / 裸 CR / `\r\r\n` 统一成 `\n`（一串 CR 只算一个换行）。

    旧版 `hudctl set --body` 在 Windows 上按平台默认写 CRLF，而正文文件本身已是 CRLF，
    落盘就叠成 `\r\r\n`；`splitlines()` 会把它当两个换行，注进上下文就是多出来的空行。
    读的时候先归一，坏面板不必手改也能正常注入；写端同时按 `\n` 落盘（双保险）。
    """
    if not text or "\r" not in text:
        return text
    return re.sub(r"\r+\n|\r+", "\n", text)


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


def parse_enabled(value) -> bool | None:
    """enabled 取值 → True / False；不在白名单里返回 None。

    注入端拿到 None 必须 fail-closed（跳过），不能因为「不认识」就当成启用——
    那等于把 `enabled: maybe` 当默认开，白名单就白写了。
    """
    text = ("" if value is None else str(value)).strip().lower()
    if text in ENABLED_TRUE:
        return True
    if text in ENABLED_FALSE:
        return False
    return None


def split_meta(text: str) -> tuple[dict[str, str], str, list[str]]:
    """切成 (元数据, 正文, 未知键列表)。

    与旧实现同口径：前 24 行、连续的非空 `key: value` 行；空行或非元数据行即正文开始。
    唯一新增：头部出现 `somekey: value` 且 somekey 不在 META_KEYS 时记进 unknown
    （调用方必须 fail-closed，绝不能把元数据当正文注进上下文）。
    """
    meta: dict[str, str] = {}
    unknown: list[str] = []
    lines = normalize_newlines(text).splitlines()
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
    for line in normalize_newlines(body).splitlines():
        if FENCE_RE.match(line):
            fence = None if fence else True
    return fence is not None


def degenerate_reason(body: str) -> str:
    """正文处于「不能按条目安全解析」的状态时返回原因，否则空串。"""
    if unclosed_fence(body):
        return "unclosed-code-fence"
    return ""


_CONFUSABLES = str.maketrans({
    # Cyrillic / Greek 常见同形字（scope 用到的字母）
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "ѕ": "s",
    "у": "y", "х": "x", "і": "i", "ј": "j", "ԁ": "d", "һ": "h",
    "ӏ": "l", "ԛ": "q", "ԝ": "w",
    "α": "a", "ε": "e", "ι": "i", "κ": "k", "ν": "v", "ο": "o",
    "ρ": "p", "τ": "t", "υ": "u", "χ": "x",
    # 大写同形字 + lunate sigma / 大写 lunate sigma / 词尾 sigma
    "Ѕ": "s", "С": "c", "О": "o", "Р": "p", "Ο": "o", "Ρ": "p",
    "ϲ": "s", "Ϲ": "s", "ς": "s", "Σ": "s",
})

def _loose_scope_at(text: str) -> tuple[str, bool] | None:
    """行首 scope 标注探测（NFKC + 去格式字符 + 任意非单词前缀）。

    不枚举括号族：任何标点前缀（含全角/异体括号）、零宽字符都先归一，
    再看行首是否为 `scop... :`。写法不标准就返回 exact=False，交给调用方 fail-closed。
    """
    original = text.lstrip()
    if not original:
        return None
    # 行首是中文/其它非 ASCII 字母时，后面的 `[scope:]` 只是正文提及，不是标注。
    first = original[:1]
    if unicodedata.category(first)[0] == "L":
        folded_first = unicodedata.normalize("NFKC", first.translate(_CONFUSABLES))
        if not (folded_first.isascii() and folded_first.isalpha()):
            return None
    # 键帽（1️⃣）是 ASCII 数字 + VS16 + U+20E3：整组剥掉再扫。
    original = re.sub(r"[0-9](?:\ufe0f|\u20e3)+\s*", "", original)
    # 任何“括号开头 + 前 64 字内有冒号”的行都按标签处理：同形字/异体括号
    # 不再逐个枚举，避免“补一个漏一个”。标准 [scope: …] 走后面的精确解析。
    head = unicodedata.normalize("NFKC", original[:64])
    opener = head[:1]
    if opener and not opener.isalnum() and not opener.isspace() and opener not in "-–—":
        if (":" in head or "：" in head) and not re.match(r"^\[\s*scope\b", head, re.IGNORECASE):
            token = head[1:].split(":", 1)[0].split("：", 1)[0].strip(" \t[](){}<>〈〉")
            return (token or "?", False)
    # 去掉行首的非 ASCII 标记（①/℅/〈/全角括号/零宽…）再归一：
    # 这些字符 NFKD 后可能变成 ASCII（①→1、℅→c/o），留在扫描串里会挡住 `scope`。
    start = 0
    while start < len(original):
        ch = original[start]
        if ord(ch) < 128:
            break
        if unicodedata.category(ch)[0] in ("N", "S", "P", "Z", "C", "M"):
            start += 1
            continue
        break
    scan_src = original[start:]
    decomposed = unicodedata.normalize("NFKD", scan_src)
    folded = unicodedata.normalize("NFKC", decomposed)
    scan = "".join(ch for ch in decomposed.translate(_CONFUSABLES) if ord(ch) < 128)
    m = re.match(r"^(\W*)(scop\w*)\s*:?\s*(.*)$", scan, re.IGNORECASE)
    if not m:
        return None
    prefix, _word, body = m.group(1), m.group(2), m.group(3)
    colon = ":" in scan[: len(scan) - len(body)]
    # 裸词 `scope` 没有冒号、也没有标点前缀时是普通正文，不当作标注。
    if not prefix and not colon:
        return None
    token = re.split(r"[\]\)\>»】〉」』｝〗〙❩⟧⦆]", body, maxsplit=1)[0].strip()
    if not token:
        return None
    # 标准写法只认 ASCII `[scope: …]`：全角/异体括号一律按非法处理。
    tag = original.split("]", 1)[0] if original.startswith("[") else original
    exact = (
        original.startswith("[")
        and ":" in tag
        and not any(ord(ch) > 127 for ch in tag)
        and bool(SCOPE_RE.match(folded))
    )
    return token, exact


def entries(body: str) -> list[dict]:
    """切成条目：{start,end,scope_token,scope,scope_line,scope_lines,conflict,scope_labels,
    invalid_continuation}。

    scope=None 表示非法或未标注（未标注时由调用方补默认范围）；
    scope_line 是第一处续行标注的行号，scope_lines 是**全部**续行标注的行号
    （`hudctl scope` 修冲突时要全部删掉，只删第一处第二处还会让整条失效）；
    同一 entry 两处及以上标注 → conflict（整条不注入）。

    续行行首的 `[scope: …]` **只有解析得出来才算标注**：写法非法时按普通续行文本原样保留
    （列进 invalid_continuation 供 check 告警），绝不因为一句写坏的标注把整条丢掉。

    判冲突必须扫**整条**的所有续行：只看第一处会把第二条标注当普通正文留在条目里，
    结果注入端带着 `[scope: …]` 原文注进上下文、check 却还是绿的。
    """
    lines = normalize_newlines(body).splitlines()
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
        malformed_scope = False
        loose = _loose_scope_at(rest)
        if loose is not None:
            loose_token, exact = loose
            if token is None:
                token = loose_token
            if not exact:
                # 全角括号/缺右括号/[ scope] 等：按非法标注 fail-closed。
                malformed_scope = True
        labels: list[str] = [token] if token is not None else []
        scope_line = None
        scope_lines: list[int] = []
        conflict = False
        invalid_continuation: list[str] = []
        for i in range(start + 1, end):
            if mask[i]:
                continue
            mm = SCOPE_RE.match(lines[i])
            loose = _loose_scope_at(lines[i])
            if not mm and loose is None:
                continue
            if not mm and loose is not None:
                found, exact = loose
                if not exact or parse_scope(found) is None:
                    invalid_continuation.append(found)
                    malformed_scope = True
                    continue
                # 键帽/装饰前缀后的标准标注：当正常续行标注处理，
                # 不能因为 SCOPE_RE 没直接匹配就当作“没有标注”。
                labels.append(found)
                scope_lines.append(i)
                if scope_line is None:
                    scope_line = i
                continue
            found = mm.group(1)
            if parse_scope(found) is None:
                invalid_continuation.append(found)   # 非法：不当标注，正文原样留着
                malformed_scope = True
                continue
            labels.append(found)
            scope_lines.append(i)
            if scope_line is None:
                scope_line = i
        if len(labels) > 1:
            conflict = True                  # 两处及以上：整条失效（fail-closed）
            token = labels[0]
        elif labels:
            token = labels[0]
        scope = None if (conflict or malformed_scope) else (parse_scope(token) if token else None)
        out.append({"start": start, "end": end, "scope_token": token, "scope": scope,
                    "scope_line": scope_line, "scope_lines": scope_lines,
                    "conflict": conflict, "scope_labels": labels,
                    "malformed_scope": malformed_scope,
                    "invalid_continuation": invalid_continuation})
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


def strip_leading_scope(line: str) -> str:
    r"""去掉行首的 [scope: …]，**保留原缩进**；整行只有标注时返回空串。

    SCOPE_RE 的 `^\s*` 与 `]\s*` 会把缩进一起吃掉：续行 `   [scope: all] - x`
    去标签后顶格成 `- x`，下一次解析就成了一个新条目（内容被「提升」出来、
    作用域另算）。所以这里把原缩进补回去。
    """
    if not SCOPE_RE.match(line):
        # 键帽/装饰前缀后的合法标注：SCOPE_RE 不匹配，但标签仍要剥掉，
        # 否则接收方会看到 `1️⃣[scope: profile:ceo]` 原文。
        if _loose_scope_at(line) is not None:
            m = re.search(r"[\[［【（〔｛<〈(].*?[\]］】）〕｝>〉)]", line)
            if m:
                return (line[: m.start()] + line[m.end() :]).rstrip()
        return line.rstrip()
    stripped = SCOPE_RE.sub("", line, count=1).rstrip()
    if not stripped.strip():
        return ""
    indent = line[: len(line) - len(line.lstrip())]
    return indent + stripped


# 旧名保留：外部/测试可能按私有名引用
_strip_leading_scope = strip_leading_scope


def has_scope_like(body: str) -> bool:
    """正文任意位置（任意字符宽度/零宽）出现 `scop...:` 就当作 scope 字样。"""
    decomposed = unicodedata.normalize("NFKD", body)
    cleaned = "".join(ch for ch in decomposed if unicodedata.category(ch) not in ("Cf", "Mn"))
    scan = "".join(ch for ch in decomposed.translate(_CONFUSABLES) if ord(ch) < 128)
    return bool(re.search(r"scop\w*\s*[:：]", scan, re.IGNORECASE))


def has_label_like(body: str) -> bool:
    """行首任意非字母数字 + 前 64 字内有冒号：按标签行处理（含符号类前缀）。"""
    for line in normalize_newlines(body).splitlines():
        stripped = line.lstrip()
        if not stripped or stripped[0].isalnum() or stripped[0].isspace():
            continue
        if stripped[0] in "-–—":
            continue
        head = unicodedata.normalize("NFKC", stripped[:64])
        if ":" in head or "：" in head:
            return True
    return False


def filter_body(body: str, session_id: str, profile: str,
                default_scope=None) -> tuple[str, int, int]:
    """按作用域过滤正文，并删掉 [scope: …] 标注。返回 (新正文, 保留条数, 总条数)。

    零条目时：不带 `[scope:]` 的自由正文按一整块放行（旧行为）；带标注却解析不出条目
    → 返回空正文 + 0 条，调用方必须跳过——绝不把带标签的原文原样广播给所有会话。
    """
    lines = normalize_newlines(body).splitlines()
    items = entries(body)
    if not items:
        if (INLINE_SCOPE_RE.search(body) or BARE_SCOPE_RE.search(body)
                or has_scope_like(body) or has_label_like(body)):
            return "", 0, 0
        return body, 1, 1
    preamble = []
    for line in lines[: items[0]["start"]]:
        if _loose_scope_at(line) is not None or has_scope_like(line) or has_label_like(line):
            # 前言里写标注/标签：该行不能原样广播（否则“只给 ceo”会进所有人上下文）。
            continue
        preamble.append(line)
    out: list[str] = preamble  # 保留第一个条目之前的标题/前言
    kept = 0
    for item in items:
        if item["scope_token"] is not None or item.get("malformed_scope"):
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
                line = strip_leading_scope(line)
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
    lines = normalize_newlines(body).splitlines()
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
                line = strip_leading_scope(line)
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
    """返回非法标注列表（写了但解析不了、同一 entry 写了两处，或续行写坏）。"""
    out: list[str] = []
    for item in entries(body):
        if item["scope_token"] is not None and item["scope"] is None:
            out.append(item["scope_token"])
        out.extend(item.get("invalid_continuation") or [])
    return out


def validate(body: str) -> list[str]:
    """返回作用域相关的告警（非法 / 两处及以上冲突 / 不在行首 / 带标注却没有条目）。"""
    warns: list[str] = []
    items = entries(body)
    for item in items:
        if item["scope_token"] is None:
            continue
        labels = item.get("scope_labels") or [item["scope_token"]]
        if item["conflict"]:
            shown = "、".join(f"[scope: {label}]" for label in labels)
            phrase = "写了两处标注" if len(labels) == 2 else f"写了 {len(labels)} 处标注"
            warns.append(f"{shown} 同一 entry {phrase}（只许一处，注入时整条失效）")
        elif item["scope"] is None:
            warns.append(f"[scope: {item['scope_token']}] 非法（只认 all / session:id / profile:name）")
    for item in items:
        for token in item.get("invalid_continuation") or []:
            warns.append(f"[scope: {token}] 续行标注非法（只认 all / session:id / profile:name）："
                         "整条不注入（fail-closed）")
    if not items and (INLINE_SCOPE_RE.search(body) or has_scope_like(body) or has_label_like(body)):
        warns.append("正文里写了 [scope: …] 却没有任何可识别条目：注入会整块跳过"
                     "（条目必须以 1. / 1) / - / * 顶格开头）")
    lines = normalize_newlines(body).splitlines()
    mask = _fence_mask(lines)
    for item in items:
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
# 钩子声明 / 同意（config.yaml 与 allowlist 的唯一解析；hudctl 与 selfcheck 共用）
# --------------------------------------------------------------------------- #
HOOK_EVENT = "pre_llm_call"
_HOOK_CMD_RE = re.compile(r"command\s*:\s*[\"']?([^\"',}\]]+)")


_HUD_INJECT_RE = re.compile(r"(?<![\w.~-])hud_inject\.py(?![.\w~-])", re.IGNORECASE)


def _strip_yaml_comment_preserve(value: str) -> str:
    """去掉 YAML 行内注释但保留缩进；引号内的 # 保留。"""
    out: list[str] = []
    quote: str | None = None
    escaped = False
    for ch in value:
        if quote:
            out.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\" and quote == '"':
                escaped = True
            elif ch == quote:
                quote = None
            continue
        if ch in ("'", '"'):
            quote = ch
            out.append(ch)
            continue
        if ch == "#" and (not out or out[-1].isspace()):
            break
        out.append(ch)
    return "".join(out).rstrip()


def _strip_yaml_comment(value: str) -> str:
    """去掉 YAML 行内注释；引号内的 # 保留（返回去首尾空白）。"""
    return _strip_yaml_comment_preserve(value).strip()


def _is_hud_inject_command(cmd: str) -> bool:
    """命令里必须出现**完整的** `hud_inject.py`；`.bak`/`.py.old` 等变体不算。"""
    return bool(_HUD_INJECT_RE.search(cmd))


def _hook_commands_in(text: str) -> list[str]:
    """从一段 YAML/行内文本里取 `command: …`；只认指向 hud_inject.py 的条目。"""
    if "hud_inject" not in text.lower():
        return []
    cleaned = _strip_yaml_comment_preserve(text)
    return [
        cmd.strip()
        for cmd in _HOOK_CMD_RE.findall(cleaned)
        if _is_hud_inject_command(cmd.strip())
    ]


def hook_commands(config_text: str, event: str = HOOK_EVENT) -> list[str]:
    r"""config.yaml 里 **event 事件下**、未注释、command 指向 hud_inject 的命令。

    必须跟着事件走：只声明在 `pre_tool_call` 下的同一条命令不算数（旧实现只做
    「文件里同时出现 pre_llm_call 与 hud_inject.py」的子串判断，陈旧配置会假装生效）。
    支持块写法（`pre_llm_call:` 下面若干 `- command: …`）与行内写法
    （`pre_llm_call: [{command: …}]`）；hooks 子键的缩进按实际值判定，不硬编码两空格。
    """
    out: list[str] = []
    in_hooks = False
    base: int | None = None
    current: str | None = None
    for raw in normalize_newlines(config_text).splitlines():
        line = _strip_yaml_comment_preserve(raw)
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        if not in_hooks:
            if re.match(r"^hooks:\s*(?:#.*)?$", line):
                in_hooks, base, current = True, None, None
            elif re.match(r"^hooks:\s*\{.*\}\s*(?:#.*)?$", line):
                # 整段行内：hooks: {pre_llm_call: [{command: …}]}
                body = line.split(":", 1)[1].strip().strip("{}")
                m = re.match(rf"{re.escape(event)}\s*:\s*(.*)$", body)
                if m:
                    out.extend(_hook_commands_in(m.group(1)))
            continue
        if indent == 0:                 # 回到顶层键 → hooks 段结束
            break
        if base is None:
            base = indent
        if indent < base:
            break
        if indent == base and not stripped.startswith("-"):
            m = re.match(r"([A-Za-z_][\w]*):\s*(.*)$", stripped)
            current = m.group(1) if m else None
            if current == event and m and m.group(2).strip():
                out.extend(_hook_commands_in(m.group(2)))
            continue
        if current == event:
            out.extend(_hook_commands_in(stripped))
    return out


def approved_commands(allowlist_text: str, event: str = HOOK_EVENT) -> set[str]:
    """allowlist JSON 里 **event 事件下**、指向 hud_inject 的批准命令（精确字符串）。

    只看事件域：`pre_tool_call` 下的批准不能让 `pre_llm_call` 报「已同意」——
    那样钩子实际会被宿主静默跳过，而自检却是绿灯。
    """
    try:
        data = json.loads(allowlist_text)
    except (TypeError, ValueError):
        return set()
    if not isinstance(data, dict):
        return set()
    out: set[str] = set()
    for entry in data.get("approvals") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("event") or "").strip() != event:
            continue
        command = str(entry.get("command") or "").strip()
        if "hud_inject" in command.lower() and _is_hud_inject_command(command):
            out.add(command)
    return out


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


def is_profile_home(home: str | None = None) -> bool:
    """HERMES_HOME 是否已经指到某个档的家目录（布局：<base>/profiles/<名>）。

    Hermes 用 profile 跑会话时 HERMES_HOME 可能直接是 profile 目录；此时 config.yaml /
    allowlist 就在它自己下面，不能再拼一层 `profiles/<名>`。
    """
    path = os.path.abspath(home or hermes_home())
    return os.path.basename(os.path.dirname(path)) == "profiles"


def profile_home(home: str | None = None, profile: str = "default") -> str:
    """某档的家目录：default → 家目录；其它档 → <home>/profiles/<名>（除非 home 本身就是它）。"""
    base = os.path.abspath(home or hermes_home())
    if is_profile_home(base):
        return base
    if (profile or "default") == "default":
        return base
    return os.path.join(base, "profiles", profile)


def source_root(home: str | None = None) -> str | None:
    """Hermes 源码根：从家目录向上最多三层找 `hermes-agent`。

    profile 家目录（.../profiles/x）里没有源码，但它的父目录、祖父目录里有；不向上找就会
    把「在 profile 里跑自检」误判成「机制没了」，写一份 error 状态让所有档每轮告警。
    找不到返回 None（调用方降级为 notice，不判红）。
    """
    current = os.path.abspath(home or hermes_home())
    for _ in range(3):                     # home / home.parent / home.parent.parent
        candidate = os.path.join(current, "hermes-agent")
        if os.path.isdir(candidate):
            return candidate
        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent
    return None


def mech_fingerprint(home: str | None = None) -> str:
    """关键源码文件的**内容** sha256；只在解析出的源码根上算。

    不再用 size+mtime：同样的文件被 checkout 一遍 mtime 就变（假红），
    而改了内容但大小/时间没变则会漏报。inject 与 selfcheck 都调这一份，
    且源码根由 source_root() 向上解析——profile 与 default 算出来必然同一个值。
    找不到源码返回空串（没有内容可哈希，绝不用占位串伪造一个「变了」的指纹）。
    """
    source = source_root(home)
    if not source:
        return ""
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
