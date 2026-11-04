# 重装系统后安装 Hermes v0.22.2（钉版本）

> 适用场景：全新 Windows，不携带旧会话/旧 `state.db`，从官方仓库安装 **v0.22.2** 并钉住。
> 本文所有命令都在**普通权限**的 PowerShell 里执行；不要用管理员窗口（会造出属主为 Administrators 的目录，之后普通终端读不了）。
> 最后核对时间：2026-11-05（源：`scripts/install.ps1` 1258–1301 行、`hermes_cli/update_downgrade_guard.py`、官方 updating/installation 文档）。

---

## 0. 为什么不用「先装最新再降级」

新版一旦运行，网关/桌面会往 `state.db` 写入运行时账本（`session_admissions` 等表）。官方 `hermes update` 有降级闸（`update_downgrade_guard.py`），会**直接拒绝**回到早于该账本的版本；手工 `git checkout` 虽能跑，但带运行时记录的会话在旧版里删不掉、`prune` 整批回滚。

全新系统直接钉 v0.22.2，`state.db` 是空的，不存在这个问题。

## 1. 前提

- 官方安装器带 `-Commit` 参数（当前版与 v0.22.2 版都有），且目标 commit 必须是 `origin/main` 的祖先——`v0.22.2` 的 commit `d923a5eb487a17c0a68801bba9ef0f56c81040a8` 满足。
- 需要能访问 GitHub 与 `hermes-agent.nousresearch.com`（国内直连不稳，先开代理）。
- 装完不要运行 `hermes update`（那是向前升级）。

## 2. 第一步：确认网络

```powershell
(Invoke-WebRequest https://github.com -Method Head -UseBasicParsing).StatusCode
(Invoke-WebRequest https://hermes-agent.nousresearch.com/install.ps1 -Method Head -UseBasicParsing).StatusCode
```

两条都返回 `200` 再往下走。

## 3. 第二步：把安装脚本下载成文件

```powershell
Invoke-WebRequest https://hermes-agent.nousresearch.com/install.ps1 -OutFile "$env:USERPROFILE\Downloads\install-hermes.ps1" -UseBasicParsing
```

- `Invoke-WebRequest` 就是"下载"；`-OutFile` 指定存到哪；`$env:USERPROFILE` 是你的用户目录，会自动展开。
- 预期：几秒到几十秒后回到提示符；`下载` 文件夹里出现 `install-hermes.ps1`（几百 KB）。
- 为什么不用 `irm ... | iex`：那种方式会在当前窗口直接执行，**传不了参数**，而且脚本里的 `exit` 会把窗口关掉。

可选的确认命令：

```powershell
Get-Item "$env:USERPROFILE\Downloads\install-hermes.ps1" | Select-Object Length
```

## 4. 第三步：钉版本安装

**一行命令，不要断行**：

```powershell
powershell -ExecutionPolicy Bypass -File "$env:USERPROFILE\Downloads\install-hermes.ps1" -Commit d923a5eb487a17c0a68801bba9ef0f56c81040a8 -IncludeDesktop
```

| 片段 | 含义 |
|---|---|
| `powershell` | 起一个子进程跑安装器；失败不会关掉你当前窗口 |
| `-ExecutionPolicy Bypass` | 只对这一次放行"下载来的脚本"（Windows 默认禁止执行） |
| `-File "…install-hermes.ps1"` | 要运行的脚本 |
| `-Commit d923a5eb…` | 要安装的版本（v0.22.2 的 commit） |
| `-IncludeDesktop` | 一并构建桌面端；不需要图形界面就删掉 |

- 预期耗时 10–30 分钟（blobless clone、PM 装 Python/Node/Chromium、桌面构建）。
- 成功标志：最后一行 `Hermes Agent install complete (pinned d923a5eb…)`。
- 中途会弹 `hermes setup` 向导（选模型/填 key）；想装完再配就加 `-NonInteractive`。

**备用路径（官网打不开时）**：

```powershell
git clone --filter=blob:none https://github.com/NousResearch/hermes-agent.git "$env:USERPROFILE\hermes-src"
cd "$env:USERPROFILE\hermes-src"
git checkout d923a5eb487a17c0a68801bba9ef0f56c81040a8
powershell -ExecutionPolicy Bypass -File .\scripts\install.ps1 -Commit d923a5eb487a17c0a68801bba9ef0f56c81040a8 -IncludeDesktop
```

## 5. 第四步：验收

```powershell
hermes --version       # 期望 Hermes Agent v0.22.2 … · local d923a5eb…
hermes doctor          # 期望无 FAIL
hermes pm status       # 期望依赖准备成功
hermes gateway status  # 期望能读到网关状态
Get-Content "$env:LOCALAPPDATA\hermes\hermes-agent\.hermes-bootstrap-complete"
                       # 期望含 "pinnedCommit": "d923a5eb…"
```

## 6. 第五步：关掉更新提示

```powershell
hermes config set updates.check false
```

否则每次启动都会提示升到最新版，误点就回到新版本。**不要执行 `hermes update`。**

## 7. 第六步：配置模型与密钥

```powershell
hermes setup
```

或手动：

```powershell
hermes config set model.provider custom:deepseek
hermes config set model.name deepseek-flash
```

API key 写入 `%LOCALAPPDATA%\hermes\.env` 的 `DEEPSEEK_API_KEY=`（或用 `hermes auth`）。

## 8. 常见失败

| 报错 | 原因 | 处置 |
|---|---|---|
| `commit … is not on branch main` | commit 写错，或误加了 `-Branch` | 用完整 40 位 sha，不加 `-Branch` |
| clone 超时 / `could not pin commit` | 代理没生效 | 回第 2 节验证两条 200 |
| 桌面构建失败（打印 `⚠` 但继续） | Electron 下载失败 | CLI 不受影响；之后 `hermes desktop --build-only --force-build` |
| `hermes` 不是内部或外部命令 | PATH 未刷新 | 重开终端，或执行安装器提示的 `$env:Path = …` 那行 |

## 9. 重装前检查清单

- [ ] 各项目仓的未推提交已推（`git -C <仓> status`、`git -C <仓> log @{u}..HEAD --oneline`）
- [ ] 工作树里未提交的改动已处理（`git status --porcelain`）
- [ ] `~/.ssh` 私钥、GitHub PAT、模型 API key 已另行备份
- [ ] 记住钉版本 commit：`d923a5eb487a17c0a68801bba9ef0f56c81040a8`
