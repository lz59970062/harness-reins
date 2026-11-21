# 任务的主人，以及有意地停掉任务

状态：第 14 版（2026-11-21）。`reins owner` 和 `reins stop` 都已实现。

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

## 3. 第二阶段：`reins stop`（已实现）

```bash
reins stop <task-id> [-m "不需要这个服务了"] [--yes] [--grace 10]
```

**怎么找到任务的进程。** worker 启动命令前在环境里加 `REINS_TASK_ID=<任务ID>`，任务启动的每个进程都继承它，`setsid`、`nohup`、两次 fork 挂到 init 下的也一样。`reins stop` 扫 `/proc/<pid>/environ` 找带这个标记的进程。任务里再嵌套启动的 Reins 任务有自己的 `REINS_TASK_ID`，不会被一起停掉。加这个标记之前启动的任务，退回用 screen 的 `STY=<pid>.reins-<任务ID>` 或 systemd 单元的 `INVOCATION_ID`。发信号前再核对一次标记；Python 支持 pidfd 时通过 pidfd 发信号，PID 被复用也不会停错进程，不支持时（例如本机的 conda Python）核对后立刻发信号，PID 恰好在这几微秒里被复用的情况忽略不计。`reins stop` 不会停掉自己和自己的上级进程。

**流程：**

1. 显示任务、命令、发起它的会话，问 `Stop it? [y/N]`；`--yes` 跳过，没有终端又没有 `--yes` 时拒绝。
2. 任务还没结束：写 `tasks/<id>/stop.json`（时间、原因）。
3. systemd 任务：`systemctl --user stop <unit>`，单元已经不在也算停了。
4. 所有任务：给带标记的进程发 SIGTERM，`--grace` 秒后还在的发 SIGKILL，再扫一遍。
5. 能拿到任务锁、且执行单元已不在时，当场把任务记为 `interrupted`（`stopped_by_user`）并排队回调；否则守护进程的恢复流程看到 `stop.json` 后记录。
6. 还有进程没停掉或单元没停掉：退出码 2，再运行一次即可。

| 任务状态 | systemd | screen（包括 Docker） |
|---|---|---|
| 排队中、等待重试 | 不再启动，回调说用户停的 | 同左 |
| 正在启动 | worker 看到 `stop.json` 不启动命令 | 同左 |
| 运行中 | 停单元，再按标记清一遍 | 按标记停掉所有进程，screen 随之结束 |
| 已结束、留下了进程 | 不会发生（`KillMode=control-group`） | 按标记停掉留下的进程，不写 `stop.json` |
| 已结束、没有留下进程 | 说明没有可停的 | 同左 |

**回调。** 有 `stop.json` 的任务，不管结果是 interrupted 还是带退出码的 completed，回调短文都是「Stopped: the user stopped this task on purpose with `reins stop`. Do not rerun, restart or replace it; if you still need it, ask the user first.」，详情里写停止时间和原因，去掉「重新创建任务」的建议。

**范围之外：** 清空了环境的进程（`env -i`）、换了用户的进程（`sudo`）、任务用 `docker run` 拉起的别的容器、任务自己用 `systemd-run --user` 拉起的单元——这些在操作系统层面已经不属于这个任务。macOS（launchd）和 Windows 任务明确拒绝，等有机器验证再做。

**SKILL.md：** 回调说用户有意停止时不要重新运行或替代；只有用户要求时才用 `reins stop`，不停别的会话的任务，想要它占着的资源先问它的会话。

## 4. 验证（2026-11-21）

- 单元测试：排队任务不启动、恢复流程记为 `stopped_by_user`、两种回调文字、worker 不启动已停任务并加上标记、`setsid`/两次 fork/忽略 SIGTERM 的进程都被停掉、命令的各种状态和退出码。
- 主机实测（临时 `REINS_HOME`）：运行中的 systemd 任务和 screen 任务，含 `setsid nohup`、两次 fork、`nohup &` 子进程，全部停掉；已结束的 screen 任务留下的进程被停掉；已结束的 systemd 任务确认没有残留。
- Docker 实测（`reins:ci-local`，screen 后端，不带 `--init`）：上面三种情况都通过；去掉 `REINS_TASK_ID` 后靠 `STY` 也能找到全部进程。
- 一个临时 Claude Code 会话读到这条回调后表示不会重启服务，会先问用户。
