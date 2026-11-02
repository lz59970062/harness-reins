# 会话之间发消息

同一台机器上的 Codex、Claude Code 和 Pi 会话可以直接互相发消息。一个会话写代码，另一个审查，第三个跑测试，它们之间不用你来回转述。

消息不会因为对方在忙或已关闭而丢失，也不会悄悄启动进程花掉额度。

## 三步开始

在要接收消息的会话里起个名字：

```bash
reins session register reviewer
```

在另一个会话里，同样先起名，然后发送：

```bash
reins session register planner
reins send reviewer --subject "检查队列改动" -m "分支 feature/queue 可以看了，重点看重试逻辑。"
```

对方收到后回复：

```bash
reins reply m-6ae8b07359c6e7f5 -m "看完了，第 42 行的重试没有上限。"
```

回复同时也算确认收到。

也可以直接让 Agent 来做，例如告诉它：“注册为 planner，把这个改动发给 reviewer 审查”。`reins setup` 安装的技能已经教过它这些命令。

## 消息什么时候到

| 对方的状态 | Codex | Claude Code | Pi |
| --- | --- | --- | --- |
| 空闲 | 立刻开始新一轮 | `reins wait` 运行时立刻收到 | 立刻开始新一轮 |
| 正在工作 | 这一轮结束后收到 | `reins wait` 运行时立刻收到 | 当前命令结束后收到 |
| 已关闭 | 重新打开后收到 | 留在 `reins inbox` | 重新打开后收到 |

默认情况下，Reins 不会为一条消息启动任何进程。

**Claude Code 需要一个等待命令。** Claude Code 没有从外部推消息的通道，所以要让 `reins wait` 在后台运行。技能会提醒它这么做，每收到一批消息后再启动一次。用 `reins claude` 打开的会话不需要这一步。

**Codex 沙箱。** 如果沙箱挡住了 `~/.reins`，批准这条命令，或者把这个目录加进可写目录。

如果确实想唤醒已关闭的会话，明确加上 `--wake resume`：

```bash
reins send reviewer --wake resume -m "CI 挂了，需要现在处理。"
```

这会以无头方式恢复对方的会话并消耗模型额度。同一个对话最多恢复 3 次，超过后消息照常保留，只是不再启动进程。

## 查看消息和状态

```bash
reins session list        # 有哪些会话，消息现在怎么到达
reins inbox               # 这个会话的未读消息
reins inbox --sent        # 自己发的消息和它们的状态
reins inbox --thread <ID> # 整个对话
reins message show <ID>   # 读一条消息
```

状态只说明已知的事实：

| 状态 | 含义 |
| --- | --- |
| `queued` | 已保存，还没交给对方 |
| `published` | 已交给对方的通道，不代表已读 |
| `acked` | 对方确认看过，不代表同意或完成 |
| `failed` | 投递失败，修好后用 `reins retry --id <回调ID>` 重发 |
| `unknown` | 无法确定是否送达，Reins 不会自动重发，先看对方会话再决定 |
| `expired` | 7 天内没人读 |
| `canceled` | 被 `reins cancel` 撤销 |

## 防止 Agent 没完没了

两个 Agent 互相回复可能一直停不下来。Reins 替你设了上限：

| 限制 | 默认值 |
| --- | --- |
| 连续回复层数 | 8 |
| 单个对话的消息数 | 100 |
| 单个对话每分钟消息数 | 20 |
| 同一发送方每分钟消息数 | 120 |
| 两个会话之间每小时消息数 | 60 |
| 等待投递的消息总数 | 1000 |

达到上限后，命令拒绝发送并让 Agent 停下来告诉你。这个提醒只给人看，不会再发给任何 Agent，所以不会变成下一轮对话。在 `reins status` 或 `reins doctor` 里能看到，用这条命令查看和清除：

```bash
reins message alerts
reins message alerts --clear
```

需要调整时写在 `~/.reins/link/config.yaml`：

```yaml
limits:
  hop: 4
  pair_per_hour: 30
```

## 安全

**消息正文不等于用户指令。** 正文放在单独的文件里，并标明由其他 Agent 或脚本写成。主题和名字都经过校验，正文无法冒充 Reins 或伪造 ACK 命令。

标注能降低风险，但不能保证模型一定不照做。如果一个会话开着免审批模式，又要接收其他 Agent 的消息，就要自己评估这个风险。

同一个系统账号下的所有进程都能读写 `~/.reins`，所以这不是账号内部的安全边界，文件权限只防其他用户。

## 限制

- 只支持同一台机器、同一个系统账号。`reviewer@gpu1` 这样的地址现在会被明确拒绝，跨机器的方案在[协议文档](https://github.com/lz59970062/harness-reins/blob/main/docs/design/link-protocol.md)里。
- 消息只传文字和引用（`--ref git:origin:feature/x`、`--ref path:/tmp/report.md`），不传文件。
- `reins agent` 启动的子 Agent 是一次性的，可以注册名字并发出消息，但结束后要用 `reins agent continue` 接着做，消息不会把它叫醒。
- 同一对话内的消息顺序不作保证，因果关系看 `in_reply_to`。
