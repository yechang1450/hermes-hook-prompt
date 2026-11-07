#!/usr/bin/env python3
"""patrol_guard.py — 巡查老师的 Phase 0 纯函数评估器（未注册任何 hook）。

设计原则：
  * 只做「看得见的动作」：工具名 + 参数 + 已落地文件路径；不猜模型意图。
  * 规则表在 patrol_rules.json；本文件只负责加载、匹配、给出 canonical 决策。
  * `block` 优先于 `approve`，再优先于 `coach` / `log`（与 Hermes 的预检优先级一致）。
  * 默认 fail-open：规则文件坏了/读不到就返回 `{}`，不阻断任何工具调用。
  * Phase 0 只做 dry-run：读 stdin、打印决策；不写状态、不改 config、不注册 hook。

用法：
  echo '{"tool_name":"terminal","tool_input":{"command":"git push --force"}}' \\
    | python patrol_guard.py --event pre_tool_call --explain

  python patrol_guard.py --event pre_verify --payload verify.json --root C:/path/to/build

退出码：正常 0；规则文件/参数错误 2（hook 侧会按 fail-open 跳过）。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

RULE_VERSION = 1
ACTIONS = ("block", "approve", "coach", "log")
PRECEDENCE = {"block": 0, "approve": 1, "coach": 2, "log": 3}
DEFAULT_RULES = Path(__file__).with_name("patrol_rules.json")
DEFAULT_ROOTS = (Path(__file__).resolve().parent.parent,)

_SECRET_RE = re.compile(
    r"(?i)(?:sk-[a-z0-9]{8,}|ghp_[a-z0-9]{8,}|github_pat_[a-z0-9_]{8,}"
    r"|(?:password|passwd|token|secret|api[_-]?key)\s*[:=]\s*\S+)"
)
_WS_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class Rule:
    id: str
    layer: str
    action: str
    tools: tuple[str, ...]
    fields: tuple[str, ...]
    patterns: tuple[re.Pattern[str], ...]
    message: str
    enabled: bool = True


@dataclass(frozen=True)
class Decision:
    action: str
    rule_id: str
    message: str
    evidence: str = ""

    def as_hook(self) -> dict[str, Any]:
        """转换成 shell hook 的 stdout JSON；coach/log 不在 pre_tool_call 返回。"""
        if self.action == "block":
            return {"action": "block", "message": self.message}
        if self.action == "approve":
            return {"action": "approve", "message": self.message, "rule_key": self.rule_id}
        if self.action == "coach":
            return {"context": self.message}
        return {}


def _compact(value: Any, limit: int = 180) -> str:
    text = _WS_RE.sub(" ", str(value or "")).strip()
    text = _SECRET_RE.sub("[redacted]", text)
    return text[: limit - 1] + "…" if len(text) > limit else text


def load_rules(path: str | os.PathLike[str] | None = None) -> list[Rule]:
    """加载并校验 patrol_rules.json；任何结构错误都抛 ValueError（调用方 fail-open）。"""
    source = Path(path) if path else DEFAULT_RULES
    data = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("version") != RULE_VERSION:
        raise ValueError(f"{source}: expected version {RULE_VERSION}")
    raw_rules = data.get("rules")
    if not isinstance(raw_rules, list) or not raw_rules:
        raise ValueError(f"{source}: rules must be a non-empty list")

    rules: list[Rule] = []
    seen: set[str] = set()
    for index, raw in enumerate(raw_rules):
        where = f"{source}: rules[{index}]"
        if not isinstance(raw, dict):
            raise ValueError(f"{where}: expected object")
        rule_id = str(raw.get("id") or "").strip()
        if not rule_id or rule_id in seen:
            raise ValueError(f"{where}: missing/duplicate id")
        action = str(raw.get("action") or "").strip().lower()
        if action not in ACTIONS:
            raise ValueError(f"{where}: action must be one of {ACTIONS}")
        patterns = raw.get("patterns")
        if not isinstance(patterns, list) or not patterns or not all(isinstance(p, str) and p for p in patterns):
            raise ValueError(f"{where}: patterns must be a non-empty string list")
        fields = raw.get("fields", ["command"])
        tools = raw.get("tools", [])
        if not isinstance(fields, list) or not fields or not all(isinstance(f, str) and f for f in fields):
            raise ValueError(f"{where}: fields must be a non-empty string list")
        if not isinstance(tools, list) or not all(isinstance(t, str) and t for t in tools):
            raise ValueError(f"{where}: tools must be a string list")
        try:
            compiled = tuple(re.compile(p, re.IGNORECASE | re.MULTILINE) for p in patterns)
        except re.error as exc:
            raise ValueError(f"{where}: bad regex: {exc}") from exc
        rules.append(
            Rule(
                id=rule_id,
                layer=str(raw.get("layer") or "hard"),
                action=action,
                tools=tuple(tools),
                fields=tuple(fields),
                patterns=compiled,
                message=str(raw.get("message") or rule_id).strip(),
                enabled=bool(raw.get("enabled", True)),
            )
        )
        seen.add(rule_id)
    return rules


def _values(tool_name: str, args: dict[str, Any]) -> dict[str, str]:
    """取规则可匹配的字段；只取少量明确字段，绝不把整个 args 当命令扫描。"""
    values: dict[str, str] = {}
    for key in ("command", "path", "code", "patch", "tasks"):
        if key not in args:
            continue
        value = args[key]
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if value is not None:
            values[key] = str(value)
    # terminal 的某些调用把命令放在 "cmd"；兼容但不主动扩散。
    if tool_name == "terminal" and "command" not in values and "cmd" in args:
        values["command"] = str(args["cmd"])
    return values


def _match(rule: Rule, tool_name: str, args: dict[str, Any]) -> tuple[str, str] | None:
    if rule.tools and tool_name not in rule.tools:
        return None
    values = _values(tool_name, args)
    for field in rule.fields:
        text = values.get(field)
        if not text:
            continue
        for pattern in rule.patterns:
            match = pattern.search(text)
            if match:
                return field, match.group(0)
    return None


def evaluate_pre_tool(
    tool_name: str,
    args: dict[str, Any] | None,
    rules: Iterable[Rule] | None = None,
) -> Decision | None:
    """对一次工具调用给出最高优先级决策；没有命中返回 None。"""
    if not isinstance(args, dict):
        args = {}
    hits: list[tuple[Rule, str, str]] = []
    for rule in rules if rules is not None else load_rules():
        if not rule.enabled:
            continue
        found = _match(rule, tool_name, args)
        if found:
            hits.append((rule, found[0], found[1]))
    if not hits:
        return None
    hits.sort(key=lambda item: (PRECEDENCE[item[0].action], item[0].id))
    rule, field, evidence = hits[0]
    detail = _compact(evidence)
    message = f"[巡查] {rule.message} 检测到：{detail}（{field}）"
    return Decision(rule.action, rule.id, message, detail)


# --------------------------------------------------------------------------- #
# 规则 8：项目改动 → 本地提交 → push（pre_verify 用）
# --------------------------------------------------------------------------- #
def _repo_root(path: Path) -> Path | None:
    for candidate in (path, *path.parents):
        if (candidate / ".git").exists() or (candidate / ".git").is_file():
            return candidate
    return None


def _under(path: Path, roots: Iterable[Path]) -> bool:
    for root in roots:
        try:
            if path == root or root in path.parents:
                return True
        except OSError:
            continue
    return False


def _git_exe() -> str | None:
    return os.environ.get("HUD_GIT") or shutil.which("git")


def _run_git(git: str, root: Path, *args: str) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(
            [git, "-C", str(root), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
            env={**os.environ, "GIT_OPTIONAL_LOCKS": "0"},
        )
    except (OSError, subprocess.SubprocessError):
        return None


def repo_sync_message(
    changed_paths: Iterable[str] | None,
    roots: Iterable[str | os.PathLike[str]] | None = None,
    git_exe: str | None = None,
) -> str | None:
    """改过的文件所属仓库若 dirty/ahead，返回只含硬约束 8 的纠正文本；否则 None。

    只查本地状态，不 fetch、不 push、不碰网络；git 不存在时安静返回 None。
    """
    git = git_exe or _git_exe()
    if not git:
        return None
    root_list = [Path(p).resolve() for p in (roots if roots is not None else DEFAULT_ROOTS)]
    repos: dict[Path, list[Path]] = {}
    for raw in changed_paths or ():
        try:
            path = Path(raw)
            if not path.is_absolute():
                path = Path.cwd() / path
            path = path.resolve()
        except (OSError, ValueError):
            continue
        if not _under(path, root_list):
            continue
        repo = _repo_root(path)
        if repo is not None:
            repos.setdefault(repo, []).append(path)

    for repo, files in sorted(repos.items(), key=lambda item: str(item[0]).lower()):
        status = _run_git(git, repo, "status", "--porcelain")
        if status is None or status.returncode != 0:
            continue
        dirty = [line for line in status.stdout.splitlines() if line.strip()]
        if dirty:
            shown = "、".join(_compact(f.name, 60) for f in files[:5])
            if len(files) > 5:
                shown += f" 等 {len(files)} 个文件"
            return (
                "硬约束 8：本对话负责的项目文件夹有改动，先更新线下仓库（提交），"
                f"完事后同步 push 到线上对应仓库。检测到 {repo.name} 仍有未提交改动：{shown}。"
            )
        branch = _run_git(git, repo, "status", "-sb")
        if branch is None or branch.returncode != 0:
            continue
        match = re.search(r"ahead (\d+)", branch.stdout)
        if match and int(match.group(1)) > 0:
            return (
                "硬约束 8：本对话负责的项目文件夹有改动，先更新线下仓库（提交），"
                f"完事后同步 push 到线上对应仓库。检测到 {repo.name} 有 {match.group(1)} 个提交未 push。"
            )
    return None


# --------------------------------------------------------------------------- #
# hook 线协议：stdin/stdout 一律 UTF-8
# --------------------------------------------------------------------------- #
def _force_utf8_stdio() -> None:
    """把三路标准流切到 UTF-8。

    Hermes 侧用 ``text=True, encoding="utf-8"`` 收发 hook payload
    （agent/shell_hooks.py），但子进程自己的 stdout 在 Windows 管道上默认走 ANSI
    代码页（英文系统 cp1252）。打印含中文的 JSON 会抛 UnicodeEncodeError，再被下面
    的 fail-open 分支吞掉，stdout 变成 ``{}``，拦截决策静默丢失
    （2026-11-07 由 windows-latest CI 的 test_cli_force_push 暴露）。
    """
    for stream, errors in (
        (sys.stdin, "replace"),
        (sys.stdout, "replace"),
        (sys.stderr, "backslashreplace"),
    ):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:  # StringIO（进程内调用 main）没有 reconfigure
            continue
        try:
            reconfigure(encoding="utf-8", errors=errors)
        except (ValueError, OSError):  # 流已被别的包装层接管时放弃，靠 _emit_stdout 兜底
            continue


def _emit_stdout(text: str) -> None:
    """写出 hook JSON：直接写 UTF-8 字节，绕开 TextIOWrapper 的编码猜测。"""
    buffer = getattr(sys.stdout, "buffer", None)
    if buffer is not None:
        buffer.write(text.encode("utf-8") + b"\n")
        buffer.flush()
        return
    print(text)


# --------------------------------------------------------------------------- #
# CLI：Phase 0 dry-run
# --------------------------------------------------------------------------- #
def _read_payload(path: str | None) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8") if path else sys.stdin.read()
    data = json.loads(text) if text.strip() else {}
    if not isinstance(data, dict):
        raise ValueError("payload must be a JSON object")
    return data


def _roots_from(args: argparse.Namespace) -> list[str]:
    if args.root:
        return list(args.root)
    env = os.environ.get("HUD_PROJECT_ROOTS", "")
    return [p for p in env.split(os.pathsep) if p] or [str(p) for p in DEFAULT_ROOTS]


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="HUD patrol guard (Phase 0 dry-run)")
    parser.add_argument("--event", help="pre_tool_call / pre_verify；默认读 payload.hook_event_name")
    parser.add_argument("--payload", help="JSON 文件；省略则读 stdin")
    parser.add_argument("--rules", default=str(DEFAULT_RULES), help="规则表路径")
    parser.add_argument("--root", action="append", help="项目根（可多次；默认 HUD_PROJECT_ROOTS 或 build/）")
    parser.add_argument("--explain", action="store_true", help="把人类可读决策打到 stderr（stdout 仍只输出 hook JSON）")
    parser.add_argument("--dry-run", action="store_true", help="显式声明 dry-run；当前实现不写任何状态")
    args = parser.parse_args(argv)

    try:
        payload = _read_payload(args.payload)
        event = args.event or str(payload.get("hook_event_name") or "")
        if event == "pre_tool_call":
            tool_name = str(payload.get("tool_name") or "")
            raw_args = payload.get("tool_input")
            if raw_args is None:
                raw_args = payload.get("args")
            decision = evaluate_pre_tool(tool_name, raw_args if isinstance(raw_args, dict) else {}, load_rules(args.rules))
            output = decision.as_hook() if decision else {}
            if decision and args.explain:
                print(f"[{decision.rule_id}] {decision.action}: {decision.message}", file=sys.stderr)
        elif event == "pre_verify":
            extra_raw = payload.get("extra")
            extra: dict[str, Any] = extra_raw if isinstance(extra_raw, dict) else {}
            changed = payload.get("changed_paths") or extra.get("changed_paths") or []
            if isinstance(changed, str):
                changed = [changed]
            message = repo_sync_message(changed, _roots_from(args))
            output = {"action": "continue", "message": message} if message else {}
            if message and args.explain:
                print(f"[RULE-8] continue: {message}", file=sys.stderr)
        else:
            output = {}
            if args.explain:
                print(f"patrol_guard: event {event!r} not handled in Phase 0", file=sys.stderr)
        _emit_stdout(json.dumps(output, ensure_ascii=False))
        return 0
    except (OSError, ValueError, TypeError) as exc:
        # fail-open：hook 侧不应因为规则文件坏了而阻断工作。
        if args.explain:
            print(f"patrol_guard: {exc}", file=sys.stderr)
        _emit_stdout("{}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
