#!/usr/bin/env python3
"""hud_report.py —— 按档统计 HUD 注入情况，回答「这轮到底有没有生效」。

读 hud.log（每轮一行），输出：
  · 每个 profile 注入次数、跳过次数（会话不匹配 / 过期 / 关闭）
  · 首次/最后一次注入时间，当前面板是否仍有效
  · 最近 5 条注入样本

用法：
  python hud_report.py                # 全部历史
  python hud_report.py --since 2026-10-22
"""
from __future__ import annotations

import argparse
import os
import re
import stat as _stat
import sys
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LOG = Path(os.environ.get("HUD_LOG") or (ROOT / "hud.log"))
PANEL = Path(os.environ.get("HUD_PANEL") or (ROOT / "panel.md"))

for _stream in (sys.stdout, sys.stderr):
    try:
        if not _stream.isatty():  # 管道/CI 下统一 UTF-8，避免 Windows 默认代码页报错
            _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

LINE = re.compile(r"^(?P<ts>[\d\-T:+]+)\s+(?P<kind>[A-Za-z_-]+)\s+(?P<rest>.*)$")

FILE_ATTRIBUTE_REPARSE_POINT = 0x400
# 预览只取前几行：面板被写坏成几百 KB 时也没必要整份读进来
PREVIEW_BYTES = 64 * 1024


def _unsafe_panel(path: Path) -> str:
    """返回拒绝预览的原因；安全则空串。

    只读报告也不能跟随软链：`panel.md -> ~/.ssh/id_rsa` 会把仓外文件的前几行打出来。
    与 hudctl 的 `_unsafe_path` 同口径：lstat 判软链/重解析点/非普通文件，不对目标做 stat。
    """
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"无法 lstat（{type(exc).__name__}）"
    if _stat.S_ISLNK(st.st_mode) or getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        return "是符号链接/重解析点"
    if not _stat.S_ISREG(st.st_mode):
        return "不是普通文件"
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--since", default="")
    args = ap.parse_args()
    since = args.since.strip()
    if since:
        try:
            datetime.strptime(since, "%Y-%m-%d")
        except ValueError:
            print(f"--since 需要合法的 YYYY-MM-DD：{since}")
            return 2

    if not LOG.exists():
        print(f"没有日志：{LOG}")
        return 1

    inj: dict[str, int] = defaultdict(int)
    skip: dict[str, Counter] = defaultdict(Counter)
    first: dict[str, str] = {}
    last: dict[str, str] = {}
    recent: list[str] = []
    total = 0
    unparsed = 0                 # 格式不认识的行（BOM/旧时间格式等）——不能静默吞掉
    other_kinds: dict[str, int] = defaultdict(int)   # 认识时间但类型不是 inject/skip（如旧的 guard）
    filtered_rounds = 0          # 有条目被作用域过滤掉的注入轮次
    filtered_by_prof: dict[str, int] = defaultdict(int)
    invalid_scope_hits = 0       # 非法 [scope:] 出现次数（含被整块跳过的轮次）

    sources = [p for p in (LOG.with_name(LOG.name + ".1"), LOG) if p.exists()]
    lines_all: list[str] = []
    for src in sources:
        lines_all.extend(src.read_text(encoding="utf-8", errors="replace").splitlines())
    for raw in lines_all:
        m = LINE.match(raw.strip())
        if not m:
            unparsed += 1
            continue
        ts, kind, rest = m.group("ts"), m.group("kind"), m.group("rest")
        if since and ts[:10] < since:
            continue
        total += 1
        # meta= 里可能是面板原文（如 meta='profile=ceo'），先剥掉再取字段，防被劫持
        clean = re.sub(r"meta=(?:'[^']*'|\"[^\"]*\"|\S+)", "", rest)
        mprof = re.search(r"\bprofile=(\S+)", clean)
        prof = mprof.group(1) if mprof else "-"
        bad = re.search(r"\binvalid_scope=(\d+)", clean)
        if bad:
            invalid_scope_hits += int(bad.group(1))
        if kind not in {"inject", "skip"}:
            other_kinds[kind] += 1
            continue
        if kind == "inject":
            inj[prof] += 1
            first.setdefault(prof, ts)
            last[prof] = ts
            recent.append(raw.strip())
            me = re.search(r"\bentries=(\d+)/(\d+)", clean)
            if me and int(me.group(1)) < int(me.group(2)):
                filtered_rounds += 1
                filtered_by_prof[prof] += 1
        else:
            reason = clean.split()[0] if clean else "?"
            skip[prof][reason] += 1
            if reason == "no-entry-for-session":
                # 整块面板都被作用域筛掉：也算一轮“被过滤”
                filtered_rounds += 1
                filtered_by_prof[prof] += 1

    print(f"日志：{LOG}")
    print(f"统计区间：{since or '全部'}　总行数 {total}\n")
    print(f"{'档':<14}{'注入':>6}{'跳过':>6}  首次注入             最后注入")
    for prof in sorted(inj, key=lambda p: -inj[p]):
        print(f"{prof:<14}{inj[prof]:>6}{sum(skip.get(prof, {}).values()):>6}  {first[prof][:19]:<20} {last[prof][:19]}")
    if filtered_rounds or invalid_scope_hits:
        print(f"\n作用域过滤：{filtered_rounds} 轮有条目被筛掉"
              + (f"（按档：{'，'.join(f'{k} {v}' for k, v in sorted(filtered_by_prof.items()))}）"
                 if filtered_by_prof else "")
              + (f"；非法 [scope:] 累计 {invalid_scope_hits} 处" if invalid_scope_hits else ""))
    if unparsed:
        print(f"\n⚠ {unparsed} 行格式无法识别（已跳过，未计入统计）")
    if other_kinds:
        detail = "，".join(f"{k}×{v}" for k, v in sorted(other_kinds.items()))
        print(f"ℹ {sum(other_kinds.values())} 行其它类型（{detail}；已跳过，未计入统计）")
    if skip:
        print("\n跳过原因：")
        for prof in sorted(skip):
            c = skip[prof]
            if not c:
                continue
            print(f"  {prof:<14}" + "  ".join(f"{k}×{v}" for k, v in c.items()))

    print("\n最近 5 条注入：")
    for line in recent[-5:]:
        print("  " + line)

    if PANEL.exists() or PANEL.is_symlink():
        unsafe = _unsafe_panel(PANEL)
        if unsafe:
            print(f"\n当前面板：{unsafe}，拒绝预览：{PANEL}"
                  "（换回普通文件再跑；绝不对链接目标读内容）")
            return 0
        try:
            with PANEL.open("r", encoding="utf-8", newline="") as fh:
                head = fh.read(PREVIEW_BYTES).splitlines()[:4]
        except UnicodeDecodeError:
            print("\n当前面板：不是合法 UTF-8，无法预览")
            return 0
        except OSError as exc:
            print(f"\n当前面板：读不到（{type(exc).__name__}）：{PANEL}")
            return 0
        print("\n当前面板：")
        for line in head:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
