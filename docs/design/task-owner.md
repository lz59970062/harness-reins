# 任务的主人，以及有意地停掉任务

状态：第 13 版（2026-11-20）。第一阶段（`reins owner`）已实现，第二阶段（`reins stop`）待实现。

前 12 版一度扩展到按进程、端口查找（环境变量标记、cgroup、`/proc` 扫描、macOS 读取等），经多轮审查越改越复杂。维护者明确了需求：**只管 Reins 发起的可回调任务**，给每个任务一个能查询的字段，映射到通讯录里发起它的会话。进程和端口不碰。

相关文档：会话的名字（通讯录）见 [link-protocol.md](link-protocol.md)。

## 1. 为什么要做

1. **资源被占着，不知道是谁的任务。** 想知道某个 Reins 任务是哪个会话发起的，好去问一声。
2. **服务杀不掉。** 会话用 `reins run` 拉起的服务，直接 kill 后回调报一个退出码，会话以为崩了又拉起来；用户也不记得是哪个会话拉的。

## 2. 第一阶段：`reins owner`（已实现）

```bash
reins owner                 # 还没结束的任务和发起它们的会话，日志最久没动的在前
reins owner <task-id>       # 任务 ID、唯一前缀或回调 ID
reins owner ... --json      # schema reins.owner/1
```

每个任务的 `task.json` 本来就记着发起它的会话（`origin.harness`、`origin.session`；没有时用回调目标 `target.session`）。`reins owner` 查询时用 `messages.name_of` 在通讯录里找这个会话的名字，所以会话后来改名也能显示当前的名字。没有名字时显示 `(no name)`。

输出：任务名、命令、目录、运行时长、日志最后写入时间；发起的会话（名字、harness、短 ID）；下一步（有名字时 `reins send <name>`，以及 `reins tui <id>`）。

不做：按进程或端口反查任务。需要时用户自己从 `nvidia-smi`、`ss -ltnp` 认出是哪个任务，再查任务 ID。

## 3. 第二阶段：`reins stop`

```bash
reins stop <task-id> [--message "不需要这个服务了"] [--quiet] [--yes]
```

**只支持用 systemd 运行的任务**（Linux 的默认方式）：systemd 单元的 cgroup 把任务的所有进程，包括 `setsid`、`nohup` 出去的，都关在一起，停止时不按 PID 发信号。screen 方式的任务拒绝并说明。

1. **确认。** 终端里列出任务、命令、发起的会话，问 `Stop it? [y/N]`；`--yes` 跳过；没有终端又没有 `--yes` 时拒绝。任务已经结束：说明，什么都不做。
2. **写 `tasks/<id>/stop.json`**：`requested_at`、发起者、`message`、`quiet`。临时文件加 `rename`，一次写完；再次运行直接覆盖。
3. **`systemctl --user stop <unit>`**：systemd 向单元里所有进程发 SIGTERM，超时后 SIGKILL，等单元结束才返回。
4. **报告**：单元已不在运行，退出码 0；否则退出码 2，可以再运行一次。

**取舍：被停掉的任务不记录退出码。** worker 和工作负载在同一个单元里一起被结束，来不及写结果；守护进程的恢复流程发现单元已不在，按现有规则把任务标为 interrupted 并排队回调。

**回调正文**（`managed_task_prompt`）：有 `stop.json` 时写「用户在某时用 `reins stop` 有意停止了这个任务，原因：……。不要重新运行、重启或换个方式替代它；确实需要时先问用户」，去掉「重新创建任务」的建议。`--quiet` 时 c1 直接写进已有的 `canceled` 状态，不投递，也不写 ACK。worker 启动工作负载前看到 `stop.json`（任务还在排队时就被停止），就不启动。

不另设新状态；出了问题再运行一次 `reins stop`。

**SKILL.md 新增：** 回调说任务是用户用 `reins stop` 停的，不要重新运行或替代它，确实需要先问用户；只有用户要求时才停别的会话发起的任务，想要它占着的资源先用 `reins owner` 找到发起的会话去问。

## 4. 待验证（第二阶段）

- systemd 任务里 `setsid nohup` 出去的孙进程被 `systemctl --user stop` 结束。
- 停止后恢复流程排队的回调带着「用户有意停止」的正文；真实 Claude Code 会话收到后不重启服务。
- 排队中的任务被停止后不会启动；screen 任务被拒绝且不受影响。
