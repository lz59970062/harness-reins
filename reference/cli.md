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
| `reins status` | 查看待处理回调 |
| `reins status --state failed` | 查看投递失败的回调 |
| `reins ack <回调ID>` | 确认已经读取结果 |
| `reins cancel --id <回调ID>` | 撤销这条回调的投递 |
| `reins prompt-policy` | 查看提醒间隔 |

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
