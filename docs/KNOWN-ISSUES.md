# 已知问题

> 只记**当前仍然存在**的问题与设计边界；修掉的进最下面的「已修复」，避免同一现象被反复报告。
> 严重度按下面这张表判，别按「谁先遇到」判。

## 判级口径

| 级别 | 定义 | 处置 |
|---|---|---|
| **P0** | 数据丢失 / 安全漏洞 / 主路径不可用，**有已知触发路径且无缓解** | 停手先修 |
| **P1** | 同上但已有可行缓解；或影响所有用户的主路径缺陷 | 下个提交修 |
| **P2** | 需要特定条件（攻击者已具备等价权限、特定平台、并发窗口），且有 workaround | 排期修 |
| **P3** | 体验 / 成本 / 隐私卫生 / 文档 | 顺手改 |

**当前 P0 = 0，P1 = 0，P2 = 0。** 盘点时间 2026-10-31（本机单档 default：237/237 本地通过、`selfcheck` 7/7、`hooks doctor` 四项绿）。
CI 配置见 `.github/workflows/tests.yml`（ubuntu/windows × Python 3.10/3.12）。本机 2026-10-31 **无法查询远端运行结果**——无 `gh`、SSH 未配置、https 直连被重置；本地全量测试已通过，故这里不写「CI 绿」。

---

## P2

（当前无。原第 1 条「钩子命令用裸 `python`」已于 2026-10-31 修复，见文末。）

---

## P3

### 2. `hud.log` 默认记完整会话 id

- 日志每行含 `session=<完整 id>` + profile 名（**不含对话正文**），1 MB 轮转到 `hud.log.1`。
- 分享日志排障会连带暴露会话标识；设 `HUD_LOG_SESSIONS=0` 后只记 `sha256(session_id)` 前 8 位（`session=#a1b2c3d4`，仍可区分会话）。
- 日志落在仓库目录，继承该目录 ACL——多用户机器上要把目录收紧到本人（见 [SECURITY.md](../SECURITY.md)）。

### 3. 注入块 1600 字硬截断

- `hud_inject.py` 对整块（标题行 + 正文 + 尾行）超过 `MAX_CHARS=1600` 时**只截正文**，追加「…（面板过长已截断）」；尾行的「执行机制建议 + 自检警告」先留预算、永远保留（旧实现从尾部整块切，面板一长，最该看见的提醒最先消失）。
- 截断**不会**静默丢：`hudctl check` 报「整块约 N 字」的状态提示，`set` 直接拒绝正文 >1600 字。
- 面板正常用法（一两百字）碰不到这条。

### 4. `impact` / `sessions` 在库被写时可能「读不到」

- 这两条命令用 `sqlite3 ... ?mode=ro` 只读打开各档 `state.db`；Hermes 正在写、或库被独占时该档会记一条读库失败。
- 行为是**显式失败**：`impact` 打印「⚠ 无法完整盘点：N 个档读库失败」并 exit 1，JSON 里 `complete: false` + `failed_profiles`——不会把「合计 0 条」当成结论。重跑一次通常即可。
- 从未用过的档（有目录无 `state.db`）不算失败，另列为 `filters.missing_profiles`。

### 5. CI 只覆盖 Python 3.10 / 3.12

- 本机开发用的是 3.14.7，CI 不含 3.13/3.14。纯标准库、无版本分支，风险低；新增语法特性前先确认 3.10 兼容（CI 会挡）。

### 6. 面板到期不自动续期、不自动恢复

- 到 `expires_at` 就停止注入；`on` 对已过期面板必须显式 `--ttl`。快照只能人手 `restore`。这是刻意设计（提醒工具不该自作主张延长自己的寿命）。

### 7. 提醒层不保证被遵守

- `pre_llm_call` 注入的只是上下文，模型可以读到再按自己的做法走；长上下文/指令冲突时命中率会掉。
- 要「不能装作没看见」得上 `pre_tool_call` 硬拦，但那条要求规则可判定——概念型提示（「自行分析、自行抉择」）翻译不成「命中什么就拦」，本项目不做这层。**这是边界，不是缺陷。**

### 8. 每轮一次解释器启动

- 钩子每轮起一个 Python（本机 5 次实测 0.15–0.6 s，中位约 0.2 s；慢盘/杀软下更久）。属预期成本，不是泄漏或挂起。

### 9. 写操作等锁 10 秒后放弃

- `set/off/on/scope/use/restore/snapshot` 共用 `panel.md.lock`；锁被别的进程长期占用时，`hudctl` 打印「面板被另一个进程锁住」并 exit 2。
- 放弃的是**这次写入**，面板保持原样（先写临时文件再 `os.replace`），不会留半份内容。

---

## 已修复（留档对账）

- **钩子命令用裸 `python`、脚本跑在仓库外的副本上**（原 P2 #1）：解释器由 PATH 决定（任一可写目录放个 `python.exe` 就顶替了每轮以本用户身份执行的代码，切 venv / 改 PATH 顺序也会让钩子悄悄换解释器或 fail-open 失败），运行副本与仓库两份会漂（改完仓库 ≠ 线上生效）。
  修法（2026-10-31，单档 default 已执行）：解释器改为专用 venv 的绝对路径、脚本改为仓库路径，`config set` + `--accept-hooks` 各一次。验收：`hermes hooks doctor` 四项全绿、`selfcheck.py` 7/7、注入日志从旧目录切到仓库 `hud.log`、`hudctl impact` 该档全部会话生效。流程与回滚见 [HOOK-CONFIG-MIGRATION.md](HOOK-CONFIG-MIGRATION.md)。
- **`set --body` 把 CRLF 正文写成双 CR 行尾**：Windows 上文本模式默认换行转换把每个 `LF` 写成 `CRLF`，正文自带 CRLF 时就叠成 `CR CR LF`；`splitlines()` 把它当两个换行，注入到上下文里每行之间多出空行。
  修法：写入端 `--body`/stdin/`render_panel` 统一 `normalize_newlines`（CRLF、裸 CR、双 CR → `LF`），原子写固定 `newline=LF`；读取端 `hud_inject`/`hudctl` 以 `newline=""` 读原文再归一，所以旧版已经写坏的面板不用手改也能正常注入。回归：`tests/test_hud.py` 的四条行尾用例。
- **同一 entry 有多处 `[scope: …]` 只认第一处**：第二处标注被当普通正文注进上下文，`check` 还是绿的。
  修法：`hud_panel.entries()` 扫整条续行，两处及以上 → `conflict`（整条不注入），`validate()` 列出全部冲突标注与处数。回归：`tests/test_scope.py::ConflictCase`。
- **`hud_report.py` 预览跟随软链 / 目录抛 traceback**：`panel.md -> ~/.ssh/id_rsa` 时会把仓外文件前 4 行打出来。
  修法：与 `hudctl._unsafe_path` 同口径先 `lstat`，软链/重解析点/非普通文件只报拒绝原因（exit 0，不中断报告）。回归：`tests/test_fs_safety.py::ReportPreviewCase`。
- **`selfcheck.py` 成功时 detail 仍写失败文案**（`ok: true` 旁边写「缺 hud_panel.py」「没有批准记录」）。
  修法：`add()` 增加 `ok_detail`，成功与失败各写各的；失败文案保持原样。回归：`tests/test_selfcheck.py` 两条。
- **会话 id 守卫只扫仓库根**：`tests/**/*.py` 里的真实 id 扫不到。
  修法：守卫增加 tests 目录扫描，按 `TEST_RESIDUE` 白名单比对（历史残留只登记、不改写），未知 id 直接失败。
- **并发建锁被误判成「重解析点」**（CI `windows-latest/3.12` 的 `test_concurrent_scope_updates_are_not_lost` 变红，run `40183862352`）：
  `_unsafe_path()` 在 `os.lstat` 抛 `FileNotFoundError` 后用 `os.path.lexists` 判「悬空软链」，而并发进程可能刚把普通锁文件建出来，于是 `lexists=True` → 误报「是符号链接/重解析点（目标不可达）」→ `scope` exit 2。
  修法：该分支**直接返回空串**，是否存在/是否普通文件交给后续 `open()`/`fstat()`。
  回归：`tests/test_fs_safety.py::LockCreateRaceCase`（mock `lstat` 抛 `FileNotFoundError` + `lexists` 返回 True，断言返回空串且**根本不查** `lexists`）+ `tests/test_hardening.py::test_concurrent_scope_updates_under_double_contention`（12 进程抢新建锁，每条目两个写者写同值，终态断言不放松）。
- **真实会话 id 进了公开仓测试**：`tests/test_reverse_audit.py` 里 28 字真实 id 已换成同长度合成 id；`git grep -nE '[0-9]{8}_[0-9]{6}_[0-9a-f]{6}'` 只剩 `20260101*` / `20260102*` 合成值。
- **管理端读面板/快照没有归一 CRLF/双 CR**（第二轮复审 P2）：`check`/`restore` 用 `Path.read_text()`，通用换行转换把旧版叠出的 `CR CR LF` 先拆成两个换行，头部因此多出空行——`check` 假红「缺 expires_at/title/enabled」，`restore` 拒收合法快照。修法：统一走 `_read_text_exact()`（`newline=""` 保留原样）再 `_normalize_newlines`。回归：`tests/test_round2_residuals.py::DoubleCrHeaderCase`。
- **`selfcheck` 的 config/allowlist 是子串判定**（P2）：`hud_inject.py` 只挂在 `pre_tool_call`、`pre_llm_call` 挂别人时仍报 `ok: true`。修法：解析收进 `hud_panel.hook_commands()` / `approved_commands()`（按 `pre_llm_call` 事件域 + 精确命令），hudctl 与 selfcheck 共用一份；支持块写法、行内写法与任意合法缩进。回归：`tests/test_round2_residuals.py::SelfcheckEventDomainCase` / `HookParserCase`。
- **`hud.log` 可被 payload 换行注入伪造行**（P3）：`session_id` / `profile` 原样落盘，塞 `\n` 就能伪造一条 `inject … entries=9/9`。修法：`log()` 唯一写入口把控制符/换行折叠成空格（含 ESC），一条日志永远一行。回归：`LogSanitizeCase`。
- **`hudctl scope` 改写双标注条目会吃掉续行缩进**（P3）：`   [scope: all] - 续行` 去标签后顶格成 `- 续行`，下一次解析变成新条目（内容被提升、作用域另算）。修法：`hud_panel.strip_leading_scope()` 保留原缩进，注入端/`entry_texts`/`scope` 共用。回归：`ScopeIndentCase`。
- **截断吃掉尾行的自检警告**（P3）：先拼整块再切前 1600 字，面板一长「⚠ 机制未自检 / 已更新」与「执行机制建议」正好被切掉。修法：`_compose_block()` 先给尾行留预算，只截正文并补截断标记。回归：`TruncationCase`。

## 历史残留（不阻塞）

- 早期提交的测试文件里出现过真实会话 id，**工作树已清零，git 历史里仍在**。重写历史要 force push，属需单独授权的动作，本轮未做。克隆历史的人仍能看到该串；它只是会话标识（不含对话内容），且该会话已归档。
