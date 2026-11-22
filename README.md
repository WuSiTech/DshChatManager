# DSH 对话管理器

一个只用 Python 标准库写的本地小工具，用来**查看并删除 DeepSeek Harness（DSH）的工作区与对话**。

- 网页界面：按工作区分组展示对话名称、ID、大小、更新时间，支持多选和逐项删除。
- 删除工作区 = **删除工作区记录 + 它名下的所有对话**（磁盘上的项目文件夹保留）。
- 检测到 DSH 正在运行时自动**锁定删除**，并提示先退出 DSH。
- 每次改 `workspace.json` 前自动备份，写入采用原子替换并回读校验。

## 快速开始

1. **完全退出 DSH**：关闭所有窗口，并右键托盘图标选择「退出」。
   如果还有残留进程，在任务管理器里结束 `DeepSeek Harness.exe`。
2. 双击 `run.cmd`（或在本目录执行 `python dsh_chat_manager.py`）。
3. 浏览器会自动打开 `http://127.0.0.1:8765/`（端口被占用时会自动换一个）。
4. 勾选要删除的对话/工作区 → 点「删除所选」→ 在确认框里点「永久删除」。

不想用网页时，也可以在终端里先看看有哪些内容：

```powershell
python dsh_chat_manager.py --list
python dsh_chat_manager.py --check
```

## 删除到底删了什么

DSH 的数据都在 `DSH_HOME`（默认 `%USERPROFILE%\.dsh`）里：

| 操作 | 会删除 | 不会删除 |
| --- | --- | --- |
| 删除对话 | `sessions/<项目目录>/<session-id>/` 下的会话日志<br>`storages/session_projcache/sessions/<session-id>.json`<br>`workspace.json` 中该对话的归属、归档、置顶记录 | 工作区本身；其他对话 |
| 删除工作区 | 上表列出的**全部对话**<br>`workspace.json` 里的工作区记录和排序项<br>该工作区的归档/置顶会话记录 | **磁盘上的项目文件夹**<br>`attachments/` 附件仓库（内容寻址、可能被别的对话共享） |

> 本工具的行为和 DSH 自带的「删除工作区」不同：DSH 只取消分组、保留会话记录；本工具按你的要求把会话也删掉。

删除前的 `workspace.json` 会备份到 `C:\Users\<你>\.dsh-chat-manager\backups\`（保留最近 60 份）。
**注意：备份只包含工作区登记信息，不包含会话日志。** 会话日志删除后无法恢复。

## 为什么必须先退出 DSH

DSH 运行时会把内存里的工作区列表写回 `workspace.json`。如果一边运行 DSH、一边删除：

- 你的删除可能被 DSH 的写回覆盖，出现「删了又回来」；
- 也可能出现登记信息与会话文件不一致。

所以工具会检测 DSH 进程，发现就禁用删除，并显示进程名和数量。退出 DSH 后点「重新检测」即可解锁。
检测方式：Windows 进程快照（`DeepSeek Harness.exe`、`dsh.exe`），以及通过 PowerShell 检查 `node.exe` 的命令行是否在跑 DSH CLI。

确实需要在 DSH 运行时删除时，可以用强制模式（**风险自负**，不建议）：

```powershell
python dsh_chat_manager.py --force
```

## 安全机制

- 只监听 `127.0.0.1`，不对外网开放；请求会校验 `Host`、`Origin`，删除接口还要求自定义请求头（防跨站伪造）。
- 删除前重新读取并校验 `workspace.json`；只接受界面当前展示过的 ID，未知 ID 会被忽略并在结果里报告。
- 写入前备份，写入时用「临时文件 + `os.replace`」原子替换，写入后回读解析；失败会自动用备份还原。
- 如果 `workspace.json` 在读取后被别的程序改动，会取消本次操作并提示刷新。
- 不删除 `attachments/`（附件可能被多个对话共享），也不删除任何未登记在 `workspace.json` 里的会话文件。

## 命令行参数

| 参数 | 说明 |
| --- | --- |
| `--home PATH` | 指定 `DSH_HOME`（默认取环境变量 `DSH_HOME`，否则 `~/.dsh`） |
| `--port N` | 网页端口，默认 `8765`；`0` 表示随机端口 |
| `--backup-dir PATH` | `workspace.json` 备份目录 |
| `--no-browser` | 启动后不自动打开浏览器 |
| `--force` | DSH 运行时也允许删除（危险） |
| `--list` | 在终端列出工作区与对话后退出 |
| `--json` | 配合 `--list` 输出 JSON（便于脚本处理） |
| `--check` | 只检查数据目录和 DSH 运行状态 |

示例：

```powershell
# 管理另一份 DSH 数据（例如从别的机器拷过来的）
python dsh_chat_manager.py --home D:\backup\dsh-home

# 只看列表，不启动网页
python dsh_chat_manager.py --list

# 导出当前状态为 JSON
python dsh_chat_manager.py --list --json > dsh-state.json
```

## 界面说明

- **顶部状态**：`DSH 未运行` 时才能删除；`DSH 正在运行` 时会显示红色告警和进程名。
- **工作区卡片**：显示名称、路径、工作区 ID、创建/更新时间、对话数量。勾选卡片左侧复选框 = 删除整个工作区（其下对话会一起删除，并自动锁定子项）。
- **对话行**：显示标题、对话 ID、日志大小、最后更新时间；`已归档`、`日志缺失` 会单独标记。点 ID 可复制。
- **搜索**：匹配工作区名称/路径/ID，以及对话标题/ID。
- **批量操作**：`全选对话` 只选对话、不选工作区；`清空` 取消全部选择；`展开/折叠` 控制列表。
- **确认框**：列出将删除的工作区、对话和合计数量，确认后才会真正执行。

### “日志缺失”是什么意思？

`workspace.json` 里仍然登记着这个对话，但 `sessions/` 下已经找不到它的日志文件（常见于手动清理过、迁移过数据，或旧版本遗留）。
它仍然会占一条列表记录，删除它只是清掉登记信息和投影缓存。

## 恢复与排错

- **想撤销工作区登记**：先退出 DSH，把 `.dsh-chat-manager\backups\workspace-*.json` 复制回
  `<DSH_HOME>\storages\workspace.json`。已删除的会话日志不在备份里，无法通过这一步恢复。
- **端口被占用**：程序会自动改用随机端口，浏览器地址以终端输出为准；也可以 `--port 0`。
- **提示“找不到界面文件”**：`web/` 目录必须和 `dsh_chat_manager.py` 放在同一级。
- **双击 `run.cmd` 一闪而过**：在 PowerShell 里执行 `python dsh_chat_manager.py` 查看具体报错。
- **没装 Python**：安装 Python 3.9+ 即可，不需要任何第三方包。

## 兼容性

- Windows 10/11（主要测试环境）；macOS/Linux 也能运行，进程检测改为扫描 `/proc`，网页功能一致。
- Python 3.9+。Python 3.14+ 自带 `compression.zstd`，可以在投影缓存缺失时从会话日志里兜底读取标题；
  更低版本没有该模块时会显示「无标题」，不影响删除功能。若安装了 `zstandard` 包也会自动使用。
- 只依赖标准库：`http.server`、`json`、`pathlib`、`ctypes` 等。

## 开发与测试

```powershell
# 单元测试（全部在临时目录里构造假 DSH 数据，不会碰真实 ~/.dsh）
python -m unittest discover -s tests -v
```

代码结构：

```
dsh_chat_manager.py   数据读取、DSH 进程检测、删除逻辑、HTTP 服务、CLI
web/index.html        页面骨架
web/styles.css        样式（自动适配深色模式）
web/app.js            列表渲染、多选、搜索、确认框、删除请求
web/favicon.svg       图标
tests/test_core.py    单元测试与 HTTP 接口测试
run.cmd               Windows 双击启动脚本
```

## 许可

仅供个人本地使用，请自行承担删除数据的风险。
