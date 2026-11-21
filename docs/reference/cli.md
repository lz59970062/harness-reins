# 命令速查

查看完整参数：

```bash
reins --help
reins run --help
reins agent --help
```

## 安装和检查

| 命令 | 用途 |
| --- | --- |
| `reins --version` | 查看当前版本 |
| `reins setup --enable --now` | 安装技能并启动协调器 |
| `reins setup --keep-skill --force --enable --now` | 更新协调器，保留技能内容 |
| `reins doctor --agent codex --session <id>` | 检查指定会话的运行配置 |

## 终端界面

| 命令 | 用途 |
| --- | --- |
| `reins tui` | 打开终端界面：所有会话、任务、Goal 和消息，会话在旁边的窗格里打开。见[终端界面](../guides/tui.md) |
| `reins tui <ID>` | 直接打开某个会话；会话 ID、任务 ID 或回调 ID 都可以 |

## 提交工作

| 命令 | 用途 |
| --- | --- |
| `reins run --task <名称> -- <命令>` | 提交一个命令 |
| `reins agent codex -- <任务>` | 启动 Codex 子 Agent |
| `reins agent claude -- <任务>` | 启动 Claude 子 Agent |
| `reins done --task <名称> --exit-code <退出码>` | 报告外部任务结果 |
| `reins log <任务ID>` | 打印任务输出的最后 10 行；在终端里会持续跟踪到任务结束，相当于 `tail -f`。输出到管道或文件时只打印不跟踪。ID 写前几位即可 |
| `reins log <任务ID> -n 100 --no-follow` | 只打印最后 100 行 |
| `reins log <任务ID> --all` | 从第一行开始打印完整日志；在终端里再继续跟踪 |
| `reins owner` | 列出还没结束的任务，以及发起每个任务的会话在 Reins 通讯录里的名字；日志最久没动的排在最前 |
| `reins owner <任务ID>` | 这个任务是哪个会话发起的（名字、Harness、会话 ID），运行了多久，日志最后什么时候写入。加 `--json` 输出机器可读的结果 |
| `reins stop <任务ID> -m "原因"` | 有意停掉一个任务：结束任务的进程，包括用 `setsid`、`nohup` 脱离出去的；还没开始的任务不再启动；发起它的会话收到回调，说明是用户停的、不要重新运行。已经结束的任务会清掉它留下的进程。没有终端时加 `--yes`。支持 Linux 上 systemd 和 screen 运行的任务（包括 Docker） |

提交参数写在 `--` 前面：

| 参数 | 用途 |
| --- | --- |
| `--cwd <目录>` | 工作目录 |
| `--task <名称>` | 回调中显示的任务名称 |
| `--agent <类型>` | 接收回调的 Agent 类型 |
| `--session <会话>` | 接收回调的会话 |
| `--queue-dir <目录>` | 回调队列位置 |
| `--backend auto` | 自动选择任务执行后端 |
| `--goal-id <目标>` | 关联多阶段目标 |
| `--callback-format full` | 使用完整回调文本 |

## 处理回调

| 命令 | 用途 |
| --- | --- |
| `reins status` | 查看待处理、进行中和投递失败的回调 |
| `reins status --state failed` | 查看投递失败的回调 |
| `reins ack <回调ID>` | 确认已经读取结果 |
| `reins cancel --id <回调ID>` | 撤销这条回调的投递 |
| `reins prompt-policy` | 查看提醒间隔 |

## 会话之间发消息

| 命令 | 用途 |
| --- | --- |
| `reins session register <名字>` | 给当前会话起名 |
| `reins install-claude-hook [--remove]` | 添加或移除 Claude Code 钩子，消息和任务结果直接进开着的窗口 |
| `reins session list` | 查看有哪些会话，以及消息现在怎么到达 |
| `reins session unregister <名字>` | 删除名字 |
| `reins send <名字> --subject <主题> -m <正文>` | 发消息，开始新对话 |
| `reins reply <消息ID> -m <正文>` | 回复，同时确认收到 |
| `reins inbox` | 查看未读；`--all` 含已读，`--sent` 看自己发的，`--thread <ID>` 看整个对话 |
| `reins message show <消息ID>` | 读一条消息和它的状态 |
| `reins message alerts` | 查看被限额拦下的对话；`--clear` 清除 |

没有监听的 Claude Code 会话和已关闭的 `reins pi` 会话默认会被无头恢复；Codex 默认进自己的队列，要恢复得加 `--wake resume`。`--wake never` 不启动任何进程。详见[会话之间发消息](../guides/messages.md)。

## 预览版命令

以下命令使用 `0.7.1` 预览版：

| 命令 | 用途 |
| --- | --- |
| `reins wait --task <任务ID>` | 在 Claude Code 中等待并输出回调 |
| `reins retry --id <回调ID>` | 重新投递保存的回调 |
| `reins agent pi -- <任务>` | 启动 Pi 子 Agent |
| `reins pi` | 打开 Reins 托管的 Pi 会话 |
| `reins template list` | 查看已注册模板 |

多阶段目标的命令顺序见[目标指南](../guides/goals.md)。
