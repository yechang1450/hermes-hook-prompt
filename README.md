# hermes-hook-prompt

给 [Hermes Agent](https://hermes-agent.nousresearch.com/docs) 装一块**每轮都摆在模型眼前**的提示面板：一段文字，每轮自动注入，带时效、可限会话、随时可改。

全部用 Hermes **官方机制**实现——没有改源码、没有打补丁、没有包装外层。仓库里只有三个 Python 脚本（纯标准库）和一份面板样例。

> 它要解决的问题：`.hermes.md` / `AGENTS.md` / SOUL / 长期记忆都是**静态**的——写进去就一直占着上下文，改一次要等新会话，还很难「只在接下来 12 小时里生效」。而很多临时提醒（这周别动 X、今天先做 Y、按某个口径回答）恰恰只需要活一段时间。

---

## 一、它用的是哪些官方机制，各自到底干什么

Hermes 的 `config.yaml` 里有一个官方的 `hooks:` 配置块，用来声明 **shell hook**：在 Agent 生命周期的固定节点上，调用一个本地命令，把该节点的上下文以 JSON 从 stdin 交给它，再读它的 stdout 决定下一步。主要事件（v0.22.2）：

| 事件 | 触发时机 | 能做什么 |
|---|---|---|
| `pre_llm_call` | **每轮**发给模型之前 | 返回 `{"context": "…"}` → 追加到本轮用户消息 |
| `post_llm_call` | 每轮模型返回之后 | 观察 |
| `pre_tool_call` | 每次工具调用之前 | **能拦**：`{"decision":"block","reason":"…"}` 或 exit 2 |
| `post_tool_call` | 工具返回之后 | 观察 |
| `pre_verify` | 交付/验证阶段（带 final_response、changed_paths、attempt） | 观察 / 干预 |
| `on_session_start` / `on_session_end` / `on_session_finalize` / `on_session_reset` | 会话生命周期 | 观察 / 初始化 |
| `pre_api_request` 等 | 每次 API 请求前后 | 观察 |

**本项目只用 `pre_llm_call` 这一个事件**，理由很简单：它的语义就是「模型开口之前」，天然适合放一段提示。

### 注入是怎么落地的（源码口径）

1. Hermes 每轮组装上下文时调用 `_collect_pre_llm_call_context()`（`agent/turn_context.py:798`），把所有 `pre_llm_call` 钩子的 `context` 收集起来；
2. 收集结果存进 `plugin_user_context` 字段——源码注释写得很直白：**`pre_llm_call` context (appended to user message)**（同文件 `:503`）；
3. 发给模型时，它被**追加到本轮用户消息后面**，和你自己打的那句话同一通道。

所以：

- **每轮一次**，不是每次工具调用；
- **一次性整块注入**：不管面板里排了几条，都是一个字符串，不是分多次；
- 模型看到的就是「你的消息 + 面板」，面板里写了什么它就得先读过去。

### 分档与同意（官方设计，不是本项目的发明）

- **一个 profile 一份配置**：default 在 `HERMES_HOME/config.yaml`，其他档在 `profiles/<名>/config.yaml`——所以要逐档声明；
- **首次运行要同意**：钩子命令会记进该档的 `shell-hooks-allowlist.json`；交互式会弹提示，非交互用 `--accept-hooks`；
- **fail-open**：钩子超时或崩溃不会拦住对话（`fail_closed` 只对 `pre_tool_call` 有意义）。

---

## 二、安装

要求：Python 3.9+（只用标准库），Hermes 能正常启动。仓库＝三个脚本 + 一份面板样例。
实测环境：Windows 11、Hermes v0.22.2、Python 3.14.7。

```bash
git clone <this-repo> hermes-hook-prompt
cd hermes-hook-prompt
cp panel.example.md panel.md      # 然后按需改正文与 expires_at
```

声明钩子（**每个 profile 一次**；Windows 路径注意用 `/`）：

```bash
# default 档
hermes config set --force hooks.pre_llm_call \
  '[{"command":"python C:/path/to/hermes-hook-prompt/hud_inject.py","timeout":10}]'

# 其他档
hermes -p <profile> config set --force hooks.pre_llm_call \
  '[{"command":"python C:/path/to/hermes-hook-prompt/hud_inject.py","timeout":10}]'
```

记录同意（非交互；不加这条钩子会被跳过；default 档去掉 `-p <profile>`）：

```bash
hermes -p <profile> --accept-hooks -z "ok"
```

核对：

```bash
hermes -p <profile> config get hooks
```

> 想作用到**所有会话（包括以后新建的）**：面板写 `session_id: any`。它不是「当前这批会话」的快照，而是对所有 session 一律放行。

---

## 三、面板文件

```yaml
session_id: any                                # any / * / 具体会话 id
expires_at: 2026-12-31T07:00:00+08:00          # 到点自动失效
title: 全员自主推进
enabled: true                                  # off 即暂停，不用删配置

（空行之后是正文，原样注入）
```

正文建议控制在 **1000 字以内**（脚本硬上限 1600 字，超出截断）。按 ¥2/百万 token 粗算，1000 字约 600–700 token ≈ **¥0.0012–0.0014/轮**；另外每轮多一次 Python 启动，本机实测约 60ms。

---

## 四、日常操作（`hudctl.py`）

```bash
python hudctl.py status                     # 范围、到期、剩多久、正文字数
python hudctl.py set --ttl 12h --body b.md  # 换正文并续期（不给 --body 就读 stdin）
python hudctl.py off / on                   # 暂停 / 恢复
python hudctl.py snapshot                   # 存当天快照到 snapshots/
python hudctl.py restore latest --ttl 8h    # 从快照恢复（设计上只允许人手动跑）
python hudctl.py prune --keep 14            # 只留最近 14 份快照
python hudctl.py sessions                   # 列出最近会话（读 state.db，只读）
python hudctl.py use "<id|标题关键字>" --ttl 8h   # 把面板挂到某个会话
```

`hud_report.py` 读 `hud.log`，按档/按天统计注入与跳过（过期、会话不匹配、关闭）：

```bash
python hud_report.py --since 2026-10-22
```

---

## 五、怎么验证「真的被听见」

只看注入日志不算。两条断言都要过：

**断言 1｜能读出**——在挂载的档里问：

> 把你上下文末尾 HUD 段的第一行原样抄出来，只抄这一行。

能逐字复述 → 说明它确实在模型输入里，没被截断或丢弃。

**断言 2｜有影响**——临时在面板里加一条：

> 回答的最后必须单独一行写 `HUD-OK`。

开 / 关（`hudctl.py on` / `off`）各跑一次同一个问题：输出里出现 / 消失 → 说明注入真的改变生成结果。

两条都过，才算「听见了」。

---

## 六、边界（先说清楚）

- **它是提醒，不是约束**：模型看到不等于一定照做；长上下文、指令冲突时会漂。要「不能装作没看见」得上 `pre_tool_call` 硬拦——但那条要求规则可判定，概念型提示（「自行分析、自行抉择」）翻译不成「命中什么就拦」，硬写只会变成实现者自己编的标准。
- **每轮一次**：不是每次工具调用；工具层面的拦截是另一个事件。
- **一个面板文件同一时间只服务一个范围**：`any` 或某一个具体会话。
- **不自动续期、不自动恢复**：到点就停，恢复快照要人手动执行。
- **不碰 Hermes 源码**：只有 `config.yaml` 里一行官方声明。

---

## 七、文件

| 文件 | 作用 |
|---|---|
| `hud_inject.py` | `pre_llm_call` 钩子本体：读面板 → 判会话/时效 → 输出 `{"context": …}` |
| `hudctl.py` | 人来操作面板：status / set / off / on / snapshot / restore / sessions / use / prune |
| `hud_report.py` | 读 `hud.log`，按档统计注入与跳过 |
| `panel.example.md` | 面板样例（复制成 `panel.md` 使用） |
| `docs/i-robot-hud.svg` | 设计灵感示意（原创绘制） |

---

## 八、灵感：I, Robot 里那个「眼前全是要点」的机器人

《我，机器人》（2004）里，威尔·史密斯进博士家时，拆除机器人（NS-5）的第一视角不是只有画面——四周一圈叠着状态、流程、约束。它不需要「回忆」注意事项，因为注意事项一直摆在眼前。

这套东西就是那个 HUD 的极简版：**不靠记忆，不靠自觉回忆，每轮重新摆在模型眼前**。

![HUD 示意](docs/i-robot-hud.svg)

> 电影原帧版权属片方，仓库里没有放截图；上图是按同一概念原创绘制的示意图。想换成原帧的话，自行放 `docs/i-robot-scene.png` 并改这一行引用即可。

---

## 九、许可

**本仓库不附 LICENSE**：这里没有原创的机制——`hooks` 是 Hermes 官方能力，注入点、事件语义、allowlist 都由 Hermes 实现；本仓库只是把「面板文件 + 一段接线脚本」拼起来。引用 Hermes 的文档与源码时，请遵循其自身的许可条款。
