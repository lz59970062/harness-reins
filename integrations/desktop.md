# 接入 Codex Desktop

## 默认原生队列回调

普通启动 Desktop 即可。`auto` 检测到 Codex Desktop 时选择 `codex-queue`，任务结束后只调用一次官方 `codex queue --thread UUID --message TEXT`。这条路径不需要 Reins 启动器、私有 MCP 管道或额外模型，也不调用 `--resume` 抢占聊天。

```bash
reins run --callback-mode codex-queue --task "一分钟回调" -- python -c "import time; time.sleep(60); print('ready')"
```

接受入队后等待接收聊天执行 ACK；未 ACK 不能记为成功，也不会自动重发。聊天忙时由 Codex 原生队列排到当前轮次之后；保持接收聊天在应用中打开。Reins 不轮询聊天状态或让模型定时领取结果。某个回调迟迟没有 ACK，也不会挡住同一聊天后面的回调；`reins status` 会列出它。

ACK 在原聊天的权限下执行。如果聊天的沙箱不能写 Reins 队列目录（默认 `~/.reins`），需要批准这条命令或把该目录加入可写范围，否则 ACK 会失败。

已在 macOS Desktop 和 Linux 上的真实 Codex 验证。Windows Desktop 尚未验证。服务器 CLI 的 `auto` 仍选择原来的 `cli`，无需迁移。

macOS 默认优先使用应用内置 Codex；可通过 `REINS_CODEX_BIN` 明确指定支持 `queue` 的版本。可执行文件和 `CODEX_HOME` 在提交时固定，避免后台服务切换目标配置。

`codex queue` 明确报错（例如找不到聊天）时回调进入 failed，修好后用 `reins retry --id ID` 重发。命令被杀或输出无法确认时结果未知，不允许重发；检查原聊天后用 `reins ack` 或 `reins cancel` 收尾。

升级时须同时更新 Reins 协调进程；旧进程不认识 `deliver.codex-queue` 能力，会保留记录而不投递。

## 原生 App Server 工具结果回调

`--callback-mode app-server` 是独立的显式路由，不启动 Desktop，也不使用私有 MCP 管道。
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

普通启动的 Desktop 不走这条路由，请用上面的原生队列回调。

隔离集成测试使用真实 Codex 0.166.0、临时配置及本地模拟模型，验证空闲/活跃轮次的工具结果
输入和完成事件，不访问真实账号或云模型：

```bash
PYTHONPATH=src:tests REINS_TEST_NATIVE_CODEX_BIN=/absolute/path/to/codex \
  python -m unittest test_native_core_integration -v
```

协议参考：[Codex App Server](https://learn.chatgpt.com/docs/app-server)。

## 从旧启动器升级

退出 Desktop，再用普通方式打开。`reins setup` 不再生成 `Start Reins Desktop`；旧文件只会打印这段提示，可以删除。回调不需要其他配置。

用 `--callback-mode desktop` 保存的回调不会丢失结果。在 macOS 和 Windows 上，它会停在 failed，并给出迁移命令：

```bash
reins retry --id ID --callback-mode codex-queue
```

任务不会重新运行，同一份结果回到同一个聊天。请在能找到你所用 Codex 的 shell 中执行，可执行文件和配置目录在这一步固定。如果旧投递可能已经发出，重试会被拒绝；先检查原聊天，再用 `reins ack` 或 `reins cancel` 收尾。在 Linux 上，这些记录继续通过本机 App Server socket 投递；socket 不可用时同样可以用这条命令迁移。

新任务不能再选择 `--callback-mode desktop`，报错会指向 `codex-queue`。
