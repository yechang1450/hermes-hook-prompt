# 安全说明

## 这个项目能碰到什么

| 组件 | 读 | 写 | 网络 |
|---|---|---|---|
| `hud_inject.py`（每轮由 Hermes 调起） | `panel.md` | `hud.log`（只记 session_id / profile / 字数 / 跳过原因） | 无 |
| `hudctl.py`（人来跑） | `panel.md`、`snapshots/`、`state.db`（**只读模式** `mode=ro`） | `panel.md`、`snapshots/` | 无 |
| `hud_report.py`（人来跑） | `hud.log`、`panel.md` | 无 | 无 |

没有第三方依赖，没有子进程，没有遥测，不会把任何内容发到外部。

## 信任边界（重要）

**`panel.md` 就是模型每轮都会读到的输入。** 能改这个文件的人，就能往模型上下文里塞任意文字（提示词注入）。所以：

- 把它放在**你自己账户**拥有的目录里（默认就是仓库目录），不要放 `/tmp`、共享盘、或别人可写的路径；
- 多用户机器上注意目录权限；Windows 下可用 `icacls <dir> /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"` 收紧；
- 面板正文不要粘贴不可信来源的整段文本（比如网页抓取内容）。

## 日志

`hud.log` 会记录 **会话 id** 与 **profile 名**（不含对话正文）。分享日志排障前先想一下这些标识是否敏感；脚本在日志超过 1 MB 时自动轮转到 `hud.log.1`。

## 已知的非安全问题

- 钩子脚本每轮启动一次 Python（本机实测约 60 ms），属预期成本；
- 钩子失败时静默放行（fail-open），不会拦住对话——这是 Hermes 对 `pre_llm_call` 的默认语义；
- 面板里写的规则只是**提醒**，不是强制；要强制得用 `pre_tool_call`（本项目不提供，因为概念型规则无法可靠判定）。

## 报告问题

开 issue 即可，请不要在 issue 里贴真实的 `panel.md` 内容、会话 id 或日志原文；用脱敏后的最小复现。
