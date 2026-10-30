# 钩子配置迁移：裸 `python` → 绝对解释器；`temp/hud/` → 仓库路径

> **状态：本机 2026-10-31 已执行（单档 default）。**
> 实际落地形态：`<专用 venv>/Scripts/python.exe <仓库>/hud_inject.py`（本机具体路径见 `hermes config get hooks`）。
> 改动量：`config set` 1 次、`--accept-hooks` 1 次；验收 `hermes hooks doctor` 四项全绿、`selfcheck.py` 7/7、注入日志已落仓库 `hud.log`、`hudctl impact` 显示该档全部会话生效。
> 本机当前**只有 default 一个 profile**（多 bot 档未恢复）；下方正文保留多档通用流程，将来恢复多档时按第五节逐档执行。

## 一、为什么迁

| 现状 | 问题 |
|---|---|
| `command: python <旧目录>/hud_inject.py` | 解释器由 PATH 决定：PATH 里任一可写目录放个 `python.exe` 就顶替了**每轮以本用户身份执行**的代码；切虚拟环境/改 PATH 顺序还会让钩子悄悄换解释器，换失败时是 fail-open（不拦对话），更难发现 |
| 脚本跑在仓库外的副本 `<旧目录>/` | 仓库修完 ≠ 线上生效，两份会漂；测试与 `git grep` 都覆盖不到线上那份 |

目标命令形状（**一个字符串**，Windows 路径用 `/`，整条命令不要含空格）：

```
<解释器绝对路径> <仓库绝对路径>/hud_inject.py
```

例：`C:/path/to/venv/Scripts/python.exe C:/path/to/hermes-hook-prompt/hud_inject.py`

要求：

- **两个都是绝对路径**（解释器 + 脚本）；
- **都不含空格**——Hermes 用 `split_command_line()` 拆命令，带空格的路径要加引号，而命令串本身在 JSON 里，多一层转义极易写错；
- 解释器版本 ≥ 3.10（CI 覆盖 3.10 / 3.12）。

## 二、0. 前置：备份 + 记下原始命令串

Hermes 家目录解析顺序（与 `hud_panel.py` 同口径）：`HERMES_HOME` → `%LOCALAPPDATA%/hermes` → `~/AppData/Local/hermes`（POSIX：`~/.local/share/hermes`）。

```bash
H="${HERMES_HOME:-$LOCALAPPDATA/hermes}"        # 按上表确认实际值
cp "$H/config.yaml"                  "$H/config.yaml.pre-hook-migration"
cp "$H/shell-hooks-allowlist.json"   "$H/shell-hooks-allowlist.json.pre-hook-migration"

# 其余 16 档各自的配置与 allowlist 也要备份：config.yaml 是**按档分开存**的，
# 只备 default 那份，回滚时第 2–17 档就回不去（迁移改了 17 档，回滚也得覆盖 17 档）
for p in $(ls "$H/profiles"); do
  d="$H/profiles/$p"
  [ -f "$d/config.yaml" ]                && cp "$d/config.yaml"                "$d/config.yaml.pre-hook-migration"
  [ -f "$d/shell-hooks-allowlist.json" ] && cp "$d/shell-hooks-allowlist.json" "$d/shell-hooks-allowlist.json.pre-hook-migration"
done
ls "$H"/profiles/*/*.pre-hook-migration | wc -l   # 期望 = 有配置的档数 × 2

# 17 档现状全量存档（回滚要用原始串，allowlist 是精确比对的）
hermes config get hooks > hook-config-before.txt
for p in $(ls "$H/profiles"); do
  printf '\n== %s ==\n' "$p" >> hook-config-before.txt
  hermes -p "$p" config get hooks >> hook-config-before.txt
done
```

同时把五个脚本与面板复制一份留档（**迁移期间 `temp/hud/` 原样保留，它就是回滚目标**）：

```bash
cp -a "<旧目录>" "<旧目录>.bak-$(date +%Y%m%d-%H%M%S)"
```

## 三、1. 选解释器并钉死绝对路径

```bash
python -c "import sys; print(sys.executable)"     # 取输出，不要手抄
```

判据：

- 输出是**真实解释器**，不是 `.../WindowsApps/python3.exe` 这类 Store 别名，也不是 pyenv / conda / 版本管理器生成的 shim——shim 会随环境变量改指向，等于把「PATH 劫持」换成「shim 劫持」；
- 用绝对路径冒烟一次（exit 0 即可，注不注入取决于面板状态）：

```bash
echo '{"session_id": "smoke", "profile": "default"}' | "<绝对解释器>" "<仓库>/hud_inject.py"; echo "rc=$?"
```

建议专用 venv（`python -m venv C:/path/to/venv`），里面**不装第三方包**——本项目纯标准库，venv 只是给解释器一个稳定、不随 PATH 变的位置。

## 四、2. 脚本落到仓库路径

目标目录就是仓库根，五个脚本 + `panel.md` 同目录：

```
<仓库>/{hud_inject.py,hudctl.py,hud_panel.py,hud_report.py,selfcheck.py,panel.md}
```

- `hud_inject.py` 默认读**自己所在目录**的 `panel.md`；也可用 `HUD_PANEL` 覆盖（必须给 Windows 原生路径，`D:/...`，不要 `/d/...`——原生 Python 会把 `/d` 解析成 `\d`）；
- `panel.md` 不入库（`.gitignore` 已忽略），从旧目录**复制**过去：`cp "<旧目录>/panel.md" "<仓库>/panel.md"`；
- 复制完先跑一次 `python hudctl.py status` 确认读到的是同一份面板（范围、到期、字数与迁移前一致）。

## 五、3. 逐档改 `hooks.pre_llm_call`（17 档）

先用 **default 试点**，观察一轮真实对话的 `hud.log`，再铺开其余 16 档。

```bash
# default
hermes config set --force hooks.pre_llm_call \
  '[{"command":"<绝对解释器> <仓库>/hud_inject.py","timeout":10}]'
```

```bash
# 其余 16 档：档名从 profiles 目录取，别凭记忆敲
for p in $(ls "$H/profiles"); do
  hermes -p "$p" config set --force hooks.pre_llm_call \
    '[{"command":"<绝对解释器> <仓库>/hud_inject.py","timeout":10}]'
done
```

逐档清单（勾一个填一个；`<p01>`…`<p16>` = `profiles/` 下的档名）：

| # | 档 | 配置已改 | 已重新同意 | `config get hooks` 复核 | `impact` 该档「钩子/同意」 | `hud.log` 见 `inject` |
|---|---|---|---|---|---|---|
| 1 | default | ☐ | ☐ | ☐ | ☐ | ☐ |
| 2 | `<p01>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 3 | `<p02>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 4 | `<p03>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 5 | `<p04>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 6 | `<p05>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 7 | `<p06>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 8 | `<p07>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 9 | `<p08>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 10 | `<p09>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 11 | `<p10>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 12 | `<p11>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 13 | `<p12>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 14 | `<p13>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 15 | `<p14>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 16 | `<p15>` | ☐ | ☐ | ☐ | ☐ | ☐ |
| 17 | `<p16>` | ☐ | ☐ | ☐ | ☐ | ☐ |

**注意**：

- `config set --force` 会**整块替换**该键的数组。如果某档 `pre_llm_call` 下还挂着别的钩子，先把旧的整段抄下来，再把新命令**合并**进数组，别覆盖掉；
- 命令串里的引号：外层单引号包 JSON，JSON 里 `"command"` 的值用双引号——路径里**不要**出现空格就不必再嵌引号；
- 只改 `pre_llm_call`。`pre_tool_call` 的守卫已 unset 归档，不要顺手恢复。

## 六、4. 重新同意（必做，否则静默跳过）

allowlist 按 `(event, command)` **精确字符串**记录（`e.get("command") == command`）。命令串一改，旧记录就不再匹配，钩子被跳过，日志只有一行：

```
shell hook for pre_llm_call (...) not allowlisted — skipped. Use --accept-hooks / ...
```

```bash
hermes --accept-hooks -z "ok"                                   # default
for p in $(ls "$H/profiles"); do hermes -p "$p" --accept-hooks -z "ok"; done
```

`-z` 是一次性会话，只为了让该档的钩子被注册并写进 allowlist；它本身不需要面板生效。

## 七、5. 验收（同一张清单，17/17 全绿才算完成）

1. `hermes -p <档> config get hooks` → 命令 = `<绝对解释器> <仓库>/hud_inject.py`，`timeout: 10`；
2. `python hudctl.py impact --profile <档>` → 该档「钩子 / 同意」列已声明/已同意；
3. 该档开一个新会话问一句 → `hud.log` 出现 `inject ... entries=k/n`；
   看到 `skip` 要读 reason：`expired` / `off` / `scope` 属**面板状态**，`unsafe-panel` / `degenerate-panel` / `empty-scope` 才是故障；
4. `python selfcheck.py` → `{"ok": true, "failed": [], "notices": []}`（rc=0）；
5. 运行副本 = 仓库那份，`sha256` 自然全等：`sha256sum <仓库>/{hud_inject,hudctl,hud_panel,hud_report,selfcheck}.py`；
6. `hermes hooks doctor` 若报 mtime drift，属正常（脚本内容变过，allowlist 记的 mtime 只供 drift 提示，不参与运行时放行判断）。

## 八、6. 回滚

按代价从小到大，任选其一。**三种都要覆盖 17 档（default + 16 个 profile）**：迁移是逐档改的，
只回 default 会让其余 16 档继续跑新命令。

1. **只回命令串**（脚本先不动）：

```bash
# default
hermes config set --force hooks.pre_llm_call \
  '[{"command":"python <旧目录>/hud_inject.py","timeout":10}]'

# 其余 16 档：档名从 profiles 目录取，与第五节清单逐一核对
for p in $(ls "$H/profiles"); do
  hermes -p "$p" config set --force hooks.pre_llm_call \
    '[{"command":"python <旧目录>/hud_inject.py","timeout":10}]'
done
```

旧 `(event, command)` 在 allowlist 里还在 → **不用**重新同意。回滚完照第七节第 3 步**逐档**看
`hud.log` 恢复 `inject`（default 绿、16 档没绿就是漏回了）。

2. **整份还原配置与 allowlist**（用第 0 节的逐档备份）：

```bash
# default
cp "$H/config.yaml.pre-hook-migration" "$H/config.yaml"
cp "$H/shell-hooks-allowlist.json.pre-hook-migration" "$H/shell-hooks-allowlist.json"

# 其余 16 档（某档第 0 节没备份到的，别硬还原，改回第 1 种方式逐档恢复）
for p in $(ls "$H/profiles"); do
  d="$H/profiles/$p"
  [ -f "$d/config.yaml.pre-hook-migration" ]                && cp "$d/config.yaml.pre-hook-migration"                "$d/config.yaml"
  [ -f "$d/shell-hooks-allowlist.json.pre-hook-migration" ] && cp "$d/shell-hooks-allowlist.json.pre-hook-migration" "$d/shell-hooks-allowlist.json"
done

hermes config get hooks                        # default 复核
for p in $(ls "$H/profiles"); do               # 16 档逐一复核命令串 + timeout
  printf '\n== %s ==\n' "$p"
  hermes -p "$p" config get hooks
done
```

3. **连脚本一起回**：`cp -a "<旧目录>.bak-<时间>"/* "<旧目录>/"`（迁移期间没删过旧目录，通常用不上）。

回滚验收与第七节同一张清单（**17/17**，含 16 个 profile 的 `config get hooks` 与 `hud.log`）。
三种回滚都只覆盖「命令串 + 配置 + allowlist」，不碰面板与快照；确认新路径跑满一周再归档旧目录，别提前删。

## 九、本次（W4）没做的事

- 没有执行任何 `hermes config set` / `--accept-hooks`；
- 没有移动、删除、改写 `temp/hud/` 里的脚本或面板；
- 本机 17 档仍在跑旧的裸 `python` 命令——**这是已知问题 P2 的第 1 条**（见 [KNOWN-ISSUES.md](KNOWN-ISSUES.md)）。
