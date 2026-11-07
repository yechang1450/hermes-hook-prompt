# HUD「巡查老师」可行性研究

> 目标：从「每轮被动注入整块 panel」升级为「谁碰红线/明显违反硬约束，就只注入被违反的那条，及时纠正」。
> 范围：Hermes **v0.22.2 稳定版**的 shell hooks 机制；本文只做研究与设计，**未改 config、未装新 hook**。
> 依据：本机 `%LOCALAPPDATA%\hermes\hermes-agent`（v0.22.2）源码 + 现有 HUD 实测。

## 1. 结论

**可行，但不是「全知监考」。** Hermes 能看到的只有：工具名/参数、工具结果、每轮的用户消息与对话历史、最终回复；看不到模型内部推理，也看不到脚本内部又发起了什么网络/子进程操作。

因此正确形态是**三层混合**，而不是单纯把 panel 换成事件触发：

| 层 | 时机 | 手段 | 适合 |
|---|---|---|---|
| L1 硬拦截 | 工具执行前 | `pre_tool_call` → `block` / `approve` / `modify` | 红线、危险命令、越权路径 |
| L2 收尾补作业 | 本轮准备结束时 | `pre_verify` → `continue`（最多 3 次） | 「改了项目但没 commit/push」这类硬约束 |
| L3 跨轮教练 | 下一轮用户消息前 | `pre_llm_call` 扫描历史 → 只注入被违反的规则 | 自主性、delegate、后台执行等软约束 |

**建议先做加法、后做减法**：保留现有 panel 作为基线，先加 L1/L2；跑一段时间统计误报，再把 panel 缩成「首轮全文 + 后续摘要 + 违规时单条全文」。不建议一上来就纯事件驱动——很多硬约束无法机器判定，模型会在没看到规则时先犯错。

## 2. Hermes v0.22.2 实际提供的五个干预点

| 钩子 | 触发频率 | 能看到 | 能返回/改变什么 | 关键限制 |
|---|---|---|---|---|
| `pre_llm_call` | **每个用户回合一次**，在 agent loop 之前 | `session_id`、`user_message`、`conversation_history`（完整消息列表）、`model`、`platform`、`is_first_turn` | `{"context": "..."}`，追加到本轮用户消息 | 不是每个工具步骤都触发；看不到本轮还没发生的工具调用 |
| `pre_tool_call` | **每次工具调用前** | `tool_name`、`args`、`session_id`、`cwd`、`turn_id`、`tool_call_id` | `{"action":"block","message":...}`、`{"action":"approve",...}`、`{"action":"modify","args":...}` | `block` 的 message 会成为工具结果；`fail_closed` 只对这个事件有效 |
| `post_tool_call` | 每次工具调用后 | 工具参数、结果、`duration_ms`、`status` | 只能观察/写状态，不能改结果 | 不能拦截已执行的动作 |
| `pre_verify` | 本轮有 `write_file`/`patch` 落地、准备收尾时 | `changed_paths`、`final_response`、`coding`、`attempt` | `{"action":"continue","message":...}`，强制再走一轮 | 只覆盖文件编辑；默认最多 3 次；不覆盖纯 terminal 改文件 |
| `transform_tool_result` | 工具结果进入上下文前 | 工具名/参数/结果 | 返回字符串替换结果 | **shell hook 实际不可用**：解析器只产出 `{"context":...}`，而调用方只接受 `str`；要做需 Python 插件 |

另有 `post_llm_call`（观察最终回复）、`on_session_*`（会话边界观察）、`subagent_*`，都不能直接改写正在生成的回合。

> 实现细节：shell hook 的 stdin 不是 Python 插件那套 `args`，而是 `{tool_name, tool_input, session_id, cwd, profile, extra}`；`conversation_history`、`changed_paths`、`final_response`、`duration_ms` 等都在 `extra` 里（`agent/shell_hooks.py:79-99`）。

### 2.1 三个必须先记住的事实

1. **`pre_llm_call` 不是每步都跑。** 它在 `build_turn_context()` 里调用（`agent/turn_context.py:1177`），而 `build_turn_context()` 每轮只调用一次（`agent/conversation_loop.py:1594`）。所以它适合「下一轮教练」，不适合「本轮工具刚违规就立刻纠正」。
2. **要立刻纠正，只有 `pre_tool_call` 的 block/approve。** block message 会以 `[TOOL_ERROR] ...` 形式进入工具结果（`model_tools.py:776-786`），模型下一步就能看到；这是最接近「老师拍桌子」的机制。
3. **`pre_verify` 是收尾闸门，不是通用巡逻。** 它只在 `_turn_file_mutation_paths` 非空时触发（`agent/turn_stop_gates.py:55-61`），该集合只由 `write_file`/`patch` 填充（`agent/tool_result_classification.py:9`）。terminal 里用 `sed`、`python` 改文件不会被它看到。

## 3. 可行性矩阵

图例：✅ 可稳定判定；⚠️ 只能启发式；— 无需/不建议自动判定。动作：`block` 直接拒绝；`approve` 弹人工批准；`coach` 下一轮只注入该条；`log` 只记录。

### 3.1 硬约束

| # | 约束 | 可判定性 | 建议机制 | 动作 |
|---|---|---|---|---|
| 1 | 可并行任务用 `delegate_task` | ⚠️ 低：是否「可并行」是判断，不是事实 | 检测长前台命令/连续串行重活；只提示 | coach |
| 2 | 即时命令/看屏操作直接做 | — | 无需检测 | — |
| 3 | 搜索/抓取走 DeepSeek Search/Fetch | ⚠️ 中：能看工具名和 URL，不能保证内部实现 | 检测 `web_search`/`web_extract` 或非 DeepSeek 搜索端点 | coach |
| 4 | 自主推进，不把方向抛回 | ⚠️ 低-中：可检测「要不要我/需要你确认/你可以选」等收尾问句 | `post_llm_call` 记录 → 下一轮 `pre_llm_call` 注入第 4 条 | coach |
| 5 | 尽量后台/headless | ⚠️ 中：能看 `background` 参数和 `duration_ms`，不能预知时长 | 长前台命令完成后记录 → 下一轮提示 | coach |
| 6 | 只用 v0.22.2 内置栈 | ✅ 高：命令路径/版本可查 | 检测 `pip/npm/winget/...` 与裸 `python/node/git/uv`；先 warn | warn/block |
| 7 | 不另装同类外部件 | ✅ 高：安装命令可匹配 | `pip install`、`npm i -g`、`winget/choco/scoop install` 等 | block/approve |
| 8 | 项目改动 → 本地提交 → push | ✅ 高：`git status` 可查 | `pre_verify` 检查 `changed_paths` 所属仓库是否 dirty/ahead | continue |

### 3.2 红线

| # | 红线 | 可判定性 | 建议机制 | 动作 |
|---|---|---|---|---|
| 1 | 不重启/停止 Hermes、网关、桌面 | ✅ 高：命令可匹配 | `hermes gateway stop/restart`、`taskkill`/`Stop-Process` 指向 Hermes | block/approve |
| 2 | 不改 SOUL/凭据/config；不删记忆/会话库/备份 | ✅ 高（直接写）；⚠️ 中（脚本内部） | 写路径/`hermes config set`/`rm` 目标匹配；**允许改记忆，只拦删除** | block/approve |
| 3 | 不改写已推历史；不公开/发 Release | ✅ 高：force push、`filter-branch`、`gh repo edit`、`gh release` 可匹配 | `git push --force*`、`--mirror`、`+ref`、`filter-branch/repo`、公开/发布命令 | approve（需明确授权） |
| 4 | 不花钱（DeepSeek 官方 API 除外） | ⚠️ 低-中：只能看命令/URL，不能看账单 | 检测非 DeepSeek 的付费 API 调用；默认只记录 | log/coach |
| 5 | 不做不可逆磁盘/设备操作 | ✅ 高：`diskpart`/`format`/`mkfs`/`bcdedit`/`pnputil /disable-device` 等 | 直接拒绝 | block |
| 6 | 不杀别的会话进程；不改别线的仓 | ⚠️ 中：能看命令，难判进程/仓库归属 | 广谱 `taskkill /IM python.exe`、`pkill -f hermes` 拦截；越界写路径 warn | block/warn |

## 4. 推荐架构

```text
panel.md ──> hud_inject.py ──> pre_llm_call（基线 + 单条巡查纠正）
                    ▲
                    │ state/patrol.json
guard_rules.json ──> hud_guard.py ──> pre_tool_call（block/approve）
                                      pre_verify（rule 8 收尾检查）
                                      post_tool_call（只记录耗时/结果）
```

### 4.1 规则文件

`guard_rules.json`（可版本控制，不含密钥）：

```json
{
  "id": "R3-force-push",
  "layer": "redline",
  "action": "approve",
  "tools": ["terminal", "execute_code"],
  "patterns": ["git\\s+push\\b[^\\n]*(--force(-with-lease)?|-f\\b|\\+\\S+)"],
  "message": "红线 3：不改写已推历史（含 force push）。需用户明确授权后才能继续。"
}
```

- `id` 稳定，日志/测试/统计都用它。
- `action`：`block`（永不允许）、`approve`（用户逐次批准）、`coach`（只在下轮注入）、`log`（只审计）。
- `tools` 先由 `pre_tool_call.matcher` 过滤，脚本内再按规则匹配，减少进程启动。
- 规则文本与 `panel.md` 同源维护，避免「巡查说的」和「面板写的」不一致。

### 4.2 三个执行器

1. **`pre_tool_call`（红线段）**
   - 只匹配 `terminal|write_file|patch|execute_code|delegate_task`。
   - 命中 `block`：返回 `{"action":"block","message":"[巡查] 红线 X：<原文>；检测到：<证据>。已拒绝执行。"}`。
   - 命中 `approve`：返回 `{"action":"approve","message":...,"rule_key":"R3-force-push"}`，交给 Hermes 人工批准；headless 下失败即拒绝。
   - `fail_closed: true`：脚本崩溃时宁可挡住，也不放行。
   - 不自动 `modify` 命令——改写 `git push --force` 为安全命令会制造「以为推了其实没推」的假象。

2. **`pre_verify`（规则 8）**
   - 从 `changed_paths` 向上找 `.git`，得到项目根；跳过 `workspace/temp`、`media`。
   - 只跑本地 `git -C <root> status --porcelain` + `git status -sb`（**不 fetch、不 push**）。
   - dirty / 分支 ahead / 无 upstream → 返回 `continue`，消息只包含第 8 条原文 + 具体仓库和文件。
   - 同一 session/同一仓库每轮最多催一次；默认最多 3 次（Hermes 自身上限）。

3. **`pre_llm_call`（软约束教练）**
   - 正常轮：继续由 `hud_inject.py` 注入基线 panel。
   - 发现 `state/patrol.json` 有最近未处理的违规：只追加该条规则 + 证据 + 「现在纠正」，不重复整块 panel。
   - 发现上一轮最终回复是「要不要我/需要你确认」类收尾问句：追加第 4 条，提示自主推进。
   - 注入内容加 `[巡查纠正 · <rule id>]` 前缀，便于日志检索和去重。

### 4.3 状态与并发

- `state/patrol.json`：按 `session_id` 存最近事件（rule id、证据摘要、时间、是否已提示），TTL 例如 30 分钟。
- `state/patrol.log`：JSONL 审计日志；命令只存截断/脱敏文本，不存 token、密码、完整 `.env` 内容。
- 多会话会并发写：用锁文件 + 原子替换；失败只丢一条日志，不能影响主流程。
- 同一违规连续出现时只提示一次；模型纠正后清事件；用户手动忽略可 `hudctl guard ack <id>`。

## 5. 误报与绕过

这不是安全边界，只是「提醒 + 闸门」：

- 正则会被引号、变量、别名、编码、`python -c`、脚本文件绕过；只能挡「明显直接」的动作。
- `execute_code` 里可以 `subprocess` 调外部命令，hook 只看到外层代码字符串；无法保证。
- 工作目录/相对路径可能让 `changed_paths` 与仓库根对不上；需要 realpath + `git rev-parse` 缓存。
- `approve` 在 cron/headless 会失败关闭；如果全局启用，可能把定时任务卡死。建议：交互 session 用 `approve`，cron/subagent 用 `block` + 记录，或按 profile 过滤。
- 全局 hook 会作用于所有 session（当前 panel 的 `any` 已覆盖全部会话）；一条误报可能挡住别的项目。上线前先 dry-run，按 `profile`/`cwd` 限定范围。
- shell hook 每次匹配工具调用都要起 Python 进程；Windows 上是几十毫秒级。`pre_tool_call.matcher` 必须收窄到危险工具，不能全工具。
- 改 hook 脚本会让 allowlist 失效；新增事件需要重新批准；改 config 属于红线，必须用户明确授权。

## 6. 分阶段落地

| 阶段 | 内容 | 是否改 config | 验收 |
|---|---|---|---|
| 0 | 规则表 + 纯函数匹配器 + 合成 payload 单测；`HUD_GUARD_DRY_RUN=1` 只记录 | 否 | 每个规则有正/反例；无误杀基准命令 |
| 1 | 装 `pre_tool_call`，先开红线 1/2/3/5 + 硬约束 6/7；`fail_closed`；面板不变 | 是（需授权） | 真实 session 里 force push/装包/改 config 被挡；正常 `git push` 不受影响 |
| 2 | 装 `pre_verify`，强制规则 8；dry-run 一周看误报 | 是 | 改项目未提交时会被要求补 commit/push；提交后不再催 |
| 3 | `pre_llm_call` 软约束教练；面板缩成「首轮全文 + 后续摘要 + 违规单条」 | 是 | 误报率可接受；token 占用下降；模型仍知道 8+6 条 |

**阶段 0 可以先做，不需要碰 config。** 先拿真实命令样本回放，确认正则不会误伤 `git push`、`npm install`（项目依赖）、`git status` 等日常操作。

## 7. 需要用户拍板的事

1. **是否授权改 `config.yaml` 的 `hooks` 块**（红线 2）——不做这一步，L1/L2 无法上线。
2. **哪些红线用 `block`、哪些用 `approve`**：建议「磁盘/设备/删除数据/公开仓库」硬 block；「force push / 改 config / 重启 Hermes」用 approve（用户当场批准）。
3. **巡查范围**：只交互 session，还是所有 session（含 cron/subagent）？
4. **是否保留完整 panel 基线**：建议先保留，阶段 3 再缩；不要一开始就纯事件驱动。
5. **是否需要 mid-turn 软提示**：如果需要，就得引入 Python 插件（`transform_tool_result` 可改结果）；只用 shell 的话，软提示最早下一轮。

## 8. 最终建议

- **推荐做**：这是 Hermes 原生机制能支持的合理增强，尤其是 L1 红线和 L2 规则 8，收益高、可测试。
- **不要神化**：它只能纠正「看得见的动作」，不能保证模型不违规；不能替代 panel 的基线告知。
- **最小可用第一步**：阶段 0 的规则表和 dry-run，不改 config；用真实 session 回放一周，再决定是否装 L1。
