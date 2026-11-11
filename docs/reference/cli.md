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

## 提交工作

| 命令 | 用途 |
| --- | --- |
| `reins run --task <名称> -- <命令>` | 提交一个命令 |
| `reins agent codex -- <任务>` | 启动 Codex 子 Agent |
| `reins agent claude -- <任务>` | 启动 Claude 子 Agent |
| `reins done --task <名称> --exit-code <退出码>` | 报告外部任务结果 |
| `reins log <任务ID>` | 打印任务输出的最后 10 行并持续跟踪，任务结束后退出，相当于 `tail -f`；ID 写前几位即可 |
| `reins log <任务ID> -n 100 --no-follow` | 只打印最后 100 行 |
| `reins log <任务ID> --all` | 从第一行开始打印完整日志，再继续跟踪 |

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
