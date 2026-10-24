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

LINE = re.compile(r"^(?P<ts>[\d\-T:+]+)\s+(?P<kind>inject|skip)\s+(?P<rest>.*)$")


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
            continue
        ts, kind, rest = m.group("ts"), m.group("kind"), m.group("rest")
        if since and ts[:10] < since:
            continue
        total += 1
        mprof = re.search(r"profile=(\S+)", rest)
        prof = mprof.group(1) if mprof else "-"
        bad = re.search(r"invalid_scope=(\d+)", rest)
        if bad:
            invalid_scope_hits += int(bad.group(1))
        if kind == "inject":
            inj[prof] += 1
            first.setdefault(prof, ts)
            last[prof] = ts
            recent.append(raw.strip())
            me = re.search(r"entries=(\d+)/(\d+)", rest)
            if me and int(me.group(1)) < int(me.group(2)):
                filtered_rounds += 1
                filtered_by_prof[prof] += 1
        else:
            reason = rest.split()[0] if rest else "?"
            skip[prof][reason] += 1

    print(f"日志：{LOG}")
    print(f"统计区间：{since or '全部'}　总行数 {total}\n")
    print(f"{'档':<14}{'注入':>6}{'跳过':>6}  首次注入             最后注入")
    for prof in sorted(inj, key=lambda p: -inj[p]):
        print(f"{prof:<14}{inj[prof]:>6}{sum(skip[prof].values()):>6}  {first[prof][:19]:<20} {last[prof][:19]}")
    if inj:
        print(f"\n作用域过滤：{filtered_rounds} 轮有条目被筛掉"
              + (f"（按档：{'，'.join(f'{k} {v}' for k, v in sorted(filtered_by_prof.items()))}）"
                 if filtered_by_prof else "")
              + (f"；非法 [scope:] 累计 {invalid_scope_hits} 处" if invalid_scope_hits else ""))
    if skip:
        print("\n跳过原因：")
        for prof, c in sorted(skip.items()):
            if not c:
                continue
            print(f"  {prof:<14}" + "  ".join(f"{k}×{v}" for k, v in c.items()))

    print("\n最近 5 条注入：")
    for line in recent[-5:]:
        print("  " + line)

    if PANEL.exists():
        try:
            head = PANEL.read_text(encoding="utf-8").splitlines()[:4]
        except UnicodeDecodeError:
            print("\n当前面板：不是合法 UTF-8，无法预览")
            return 0
        print("\n当前面板：")
        for line in head:
            print("  " + line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
