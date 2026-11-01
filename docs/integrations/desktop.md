# 接入 Codex Desktop

## 默认原生队列回调

普通启动 Desktop 即可。`auto` 检测到 Codex Desktop 时选择 `codex-queue`，任务结束后只调用一次官方 `codex queue --thread UUID --message TEXT`。这条路径不需要 Reins 启动器、私有 MCP 管道或额外模型，也不调用 `--resume` 抢占聊天。

```bash
reins run --callback-mode codex-queue --task "一分钟回调" -- python -c "import time; time.sleep(60); print('ready')"
```

接受入队后等待接收聊天执行 ACK；未 ACK 不能记为成功。发送意图和原生消息 ID 持久保存，超时或断线不自动重发。聊天忙时由 Codex 原生队列安排后续处理；保持接收聊天在应用中打开。Reins 不轮询聊天状态或让模型定时领取结果。

这项接口属于通用 Codex CLI，非 macOS 专属。本机 macOS Desktop 已通过独立进程延迟 60 秒投递并自动启动新轮次的验证；Linux/Windows 未做真实桌面验收。服务器 CLI 的 `auto` 仍选择原来的 `cli`，无需迁移。

macOS 默认优先使用应用内置 Codex；可通过 `REINS_CODEX_BIN` 明确指定支持 `queue` 的版本。可执行文件和 `CODEX_HOME` 在提交时固定，避免后台服务切换目标配置。

升级时须同时更新 Reins 协调进程；旧进程不认识 `deliver.codex-queue` 能力，会保留记录而不投递。

## 原生 App Server 工具结果回调

`--callback-mode app-server` 是独立的显式路由，不使用 Desktop 启动器或私有 MCP 管道。
它要求调用方提供**承载原聊天的现有 App Server** 的 Unix socket：

```bash
reins doctor --agent codex --session THREAD_ID --callback-mode app-server \
  --app-server-socket /absolute/path/to/owner.sock
reins run --agent codex --session THREAD_ID --callback-mode app-server \
  --app-server-socket /absolute/path/to/owner.sock --task "native callback" -- python job.py
```

提交时固定 socket 的实际路径；投递前验证同一用户的 socket 对端和已加载聊天。
找不到原聊天就拒绝投递，不会从磁盘在另一个 Core 恢复聊天，也不会退回 CLI。
任务结果通过官方 `turn/start` 的 `toolOutput` 字段进入模型上下文；空闲时启动处理，
活跃时由 Core 排入当前轮次。接收方沿用原聊天的权限与配置，必须有执行回调中 ACK 命令的权限。
这条路由不覆盖原聊天的 sandbox 或审批策略。

投递客户端等待服务推送的 `turn/completed`，不反复查询聊天状态或等待聊天空闲。
ACK 和取消在进入等待及收到完成/断线/超时后检查；没有模型 ACK 就不算成功。
发送前保存投递意图；丢失响应时保留状态，禁止自动改走 CLI 或重发不确定的请求。
明确不支持 `toolOutput` 的旧 Core 会报告阻塞，需要修复版本后重试同一回调。

| 环境 | 范围 |
| --- | --- |
| 服务器或本机 Codex CLI | 继续使用原有 `cli` / `codex exec resume`；不要求这项接入 |
| macOS、Linux 上可访问的原聊天 App Server | 此版本实现同用户 Unix socket 的原生路由 |
| Windows | RPC 本身跨平台；此版本未实现该原生模式的 Windows 传输与身份验证 |
| 普通启动的 Desktop | 只有正式可访问、承载目标聊天的入口才可使用；不自动探测或接管私有连接 |

2026-11-01 的 macOS 实测：应用内 `send_message_to_thread` 可投递；普通外部 MCP
客户端被应用以 `missing-code-signing-identity` 拒绝。默认 daemon 与 Desktop 的
stdio Core 是两个服务。**本功能不解决这条 Desktop 授权边界，也未证明普通 Desktop 的完整回调。**

隔离集成测试使用真实 Codex 0.166.0、临时配置及本地模拟模型，验证空闲/活跃轮次的工具结果
输入和完成事件，不访问真实账号或云模型：

```bash
PYTHONPATH=src:tests REINS_TEST_NATIVE_CODEX_BIN=/absolute/path/to/codex \
  python -m unittest test_native_core_integration -v
```

协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。

## 可选共享 Core 启动器

Codex Desktop 接入使用 `0.7.1` 预览版的共享 Core 启动器。Desktop 和 Reins 通过同一个 Core 访问会话，任务结束后把回调送回桌面应用。

## 生成启动器

在 macOS 或 Windows 上完成[预览版安装](../installation.md#安装预览版)，然后运行：

```bash
reins setup --force --enable --now
reins desktop status
```

macOS 会生成 `Start Reins Desktop.command`；Windows 会生成 `Start Reins Desktop.cmd` 和对应的 PowerShell 脚本。默认位置是用户 Codex 配置目录下的 `harness-reins` 文件夹。

## 启动 Desktop

保存当前工作并退出 Desktop，再双击生成的启动器，或运行：

```bash
reins desktop launch
```

之后从这个 Desktop 会话提交一个小任务：

```bash
reins run --callback-mode desktop --cwd "$PWD" --task "Desktop 回调示例" -- python -c "print('Desktop callback ready')"
```

让 Agent 读取结果并执行回调中的 ACK，完成一次接入检查。PowerShell 工具环境中将 `--cwd "$PWD"` 换成 `--cwd "$($PWD.Path)"`。

## 调整应用路径

macOS 同时安装多个应用时，可以明确选择：

```bash
reins desktop prepare --app /Applications/Codex.app --force
```

Windows 默认使用直接启动方式。Store 包上下文启动是实验选项，配置命令是：

```powershell
reins setup --desktop-launch-mode package-context --force --enable --now
```

各平台的启动步骤、运行条件和验证记录见 [macOS Desktop](https://github.com/lz59970062/harness-reins/blob/main/docs/macos-desktop-bridge.md)与 [Windows Desktop](https://github.com/lz59970062/harness-reins/blob/main/docs/windows-desktop-bridge.md)。应用升级后，再用上面的示例检查回调。
