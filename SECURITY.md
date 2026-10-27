# 安全说明

## 这个项目能碰到什么

| 组件 | 读 | 写 | 网络 |
|---|---|---|---|
| `hud_inject.py`（每轮由 Hermes 调起） | `panel.md`、`state/selfcheck.json`（只读机制指纹） | `hud.log`（只记 session_id / profile / 字数 / 跳过原因） | 无 |
| `hudctl.py`（人来跑） | `panel.md`、`snapshots/`、`state.db`（**只读模式** `mode=ro`）、各档 `config.yaml` 与 `shell-hooks-allowlist.json`（`impact` 只读） | `panel.md`、`snapshots/` | 无 |
| `hud_report.py`（人来跑） | `hud.log`、`panel.md` | 无 | 无 |
| `hud_panel.py`（被上面几个 import 的解析件） | 无（只解析传入的正文） | 无 | 无 |
| `selfcheck.py`（人来跑） | `panel.md`、Hermes 源码（`agent/`、`hermes_cli/`）、本档 `config.yaml`、`shell-hooks-allowlist.json` | `state/selfcheck.json` | 无 |

没有第三方依赖，没有遥测，不会把任何内容发到外部；唯一起子进程的是 `selfcheck.py`（调 `hud_inject.py` 与 `hudctl.py check` 做端到端自检），两个子进程都是本仓库脚本。

## 信任边界（重要）

**`panel.md` 就是模型每轮都会读到的输入。** 能改这个文件的人，就能往模型上下文里塞任意文字（提示词注入）。所以：

- 把它放在**你自己账户**拥有的目录里（默认就是仓库目录），不要放 `/tmp`、共享盘、或别人可写的路径；
- 多用户机器上注意目录权限；Windows 下可用 `icacls <dir> /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"` 收紧；
- 面板正文不要粘贴不可信来源的整段文本（比如网页抓取内容）。

## 文件安全

- 面板是**符号链接 / Windows 重解析点**时，`hud_inject.py` 拒绝读取（日志记 `skip unsafe-panel`）——防止把 `~/.ssh/id_rsa` 之类的文件当面板送进模型；
- 面板必须是普通文件，FIFO/设备文件一律拒绝；`hudctl.py` 的所有读路径（`status`/`check`/`impact`/`preview`/`snapshot`/`restore`…）同样先 `lstat`，软链/重解析点直接拒绝（exit 2），不跟随；`hud_report.py` 的「当前面板」预览同口径——面板是软链/重解析点/目录时只打印拒绝原因，绝不把仓外文件的前几行打出来；
- `hudctl.py` 写面板用「同目录临时文件 + `os.replace`」原子替换，临时文件名由 `tempfile.mkstemp`（`O_CREAT|O_EXCL`）随机生成——不再使用可预测的 `panel.md.<pid>.tmp`，预置软链无法被跟随写入；`selfcheck.py` 写 `state/selfcheck.json` 同理；
- 读改写走**同一把文件锁**（`panel.md.lock`），并发的 `set/off/on/scope/use/restore` 不会互相覆盖丢更新；锁文件打开前 `lstat` 拒绝软链/重解析点，打开后 `fstat` 再验普通文件（POSIX 另加 `O_NOFOLLOW`）；整份读取的绝对上限是 2 MB，超过 200 KB 时 `status`/`set` 只读头部（`set` 因此能整份覆盖被写坏的超大面板）、`check` 只报大小，不给自己留死角；
- `restore` 覆盖前会先把当前面板存成 `snapshots/pre-restore-<时间戳>.md`；写不进快照就**拒绝恢复**；快照内容先按 `hudctl check` 的同一套规则校验，非法快照直接拒绝（exit 2），绝不把现网面板一起写坏；快照本身是软链/重解析点时同样拒绝使用；
- `prune` 默认只列清单，加 `--yes` 才删，且只删本工具命名的快照（手工 `.md` 一律不碰）。

## 日志

`hud.log` 会记录 **会话 id** 与 **profile 名**（不含对话正文）。默认记完整会话 id，方便和 `hudctl sessions` 对账；不想让日志里出现可读的会话标识时，给钩子进程设 `HUD_LOG_SESSIONS=0`，日志只记 `sha256(session_id)` 前 8 位（形如 `session=#a1b2c3d4`，仍能区分不同会话，但不能直接读出 id）。分享日志排障前先想一下这些标识是否敏感；脚本在日志超过 1 MB 时自动轮转到 `hud.log.1`。

日志文件默认就在仓库目录里，继承该目录的 ACL。多用户机器上建议把仓库目录（含 `hud.log`）收紧到本人：Windows 用 `icacls <dir> /inheritance:r /grant:r "%USERNAME%:(OI)(CI)F"`，POSIX 用 `chmod 700 <dir>` / `chmod 600 hud.log*`。

## 已知的非安全问题

- 钩子脚本每轮启动一次 Python（本机 5 次实测 0.15–0.6 s，中位约 0.2 s），属预期成本；
- 钩子失败时静默放行（fail-open），不会拦住对话——这是 Hermes 对 `pre_llm_call` 的默认语义；
- 面板里写的规则只是**提醒**，不是强制；要强制得用 `pre_tool_call`（本项目不提供，因为概念型规则无法可靠判定）。

## 报告问题

请使用本仓库 GitHub “Security” 页中的 **Report a vulnerability** 私密报告入口。不要在公开 issue 里贴真实的 `panel.md` 内容、会话 id、日志原文或可直接利用的复现细节；需要公开讨论时，请使用脱敏后的最小复现。
