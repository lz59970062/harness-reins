# Antigravity 接入

在 Google Antigravity CLI（`agy`）的会话里交出一个长任务，结束这一轮，任务跑完时结果回到**同一个对话**：窗口开着，就在窗口里把 Agent 唤醒；窗口关了，Reins 在后台把对话接上，Agent 照样检查结果、ACK、继续下一步。

## 安装

```bash
reins setup --with-agy --force --enable --now
```

`--with-agy` 把专用技能装到 `~/.gemini/config/skills/harness-reins`，Antigravity 的每个新对话都能看到。守护进程在自己的 `PATH` 和 `~/.local/bin/agy` 里找 `agy`；装在别处时设置 `REINS_AGY_BIN`。检查状态：

```bash
reins doctor --agent agy --session "$ANTIGRAVITY_CONVERSATION_ID"
```

## 回调怎样回到对话

Antigravity 给 Agent 执行的每条命令都设置 `ANTIGRAVITY_CONVERSATION_ID`，`reins run` 据此自动绑定当前对话：

```bash
reins run --cwd "$PWD" --task "train model" -- python train.py
```

stderr 会给出等待命令：

```text
reins: Antigravity live callback: run as a background command (do not wait for it): reins wait --queue-dir … --task <id>
```

| 对话的状态 | 回调怎样到达 |
| --- | --- |
| 窗口开着 | Agent 把 `reins wait` 放到后台（`run_command` 的 `WaitMsBeforeAsync` 设为 500）后结束这一轮。回调一入队，`reins wait` 就退出，Antigravity 随即唤醒同一个窗口里的 Agent，空闲时也一样 |
| 没有进程打开它 | 守护进程运行 `agy --resume <id> -p`，把回调作为一条 stream-json 消息交给它，追加一轮。队列目录作为工作区加入，Agent 可以直接 ACK |

守护进程**不会在对话被打开时恢复它**。窗口和 `agy -p` 都会对 `~/.gemini/antigravity-cli/presence/<对话 ID>.lock` 持有排他的 `flock`，进程退出时由操作系统释放；守护进程只在这个锁空闲时才恢复对话，所以不会与窗口同时写同一个对话。

headless 恢复中的 `reins wait` 会立即返回：`agy -p` 要等后台命令结束才退出，下一个回调会在这一轮结束后再次恢复对话。

## 消息

`reins session register NAME` 之后，其他会话可以用 `reins send NAME` 找到这个对话。窗口开着时，消息经后台 `reins wait` 送达；没有进程打开对话时，消息会在后台恢复它（`--wake never` 则只放进收件箱）。

## 沙盒

Antigravity 的 Auto 模式在终端沙盒里执行命令，沙盒看不到主目录，而 Reins 和它的队列都在主目录下。技能会让 Agent 从第一次起就在沙盒外运行 Reins 命令。

## 验证情况与限制

- 在 Linux 上用 `agy` 1.5.18 实测：交互窗口里提交任务、结束这一轮、空闲时被唤醒并 ACK（15 分钟的任务期间没有多余的唤醒）；`agy -p` 会话里后台等待和 ACK；窗口关闭后由守护进程 headless 恢复并 ACK；两个对话并行恢复、同一对话串行；消息的 headless 回复；`reins tui` 里打开、跳转和新建窗格。
- macOS 使用同样的 `flock` 判断，尚未实测。
- Windows 上看不到 presence 锁，所以只通过 `reins wait` 投递，不做 headless 恢复。
- `reins tui` 面板会列出 Antigravity 对话，可以在窗格里打开、新建、发消息、开协作会话。
- headless 恢复里 `agy` 无法弹出审批，Reins 会为这一轮放行 ACK、回复、发消息、查目标这几条 Reins 命令（保留你自己的权限规则）；`reins run` 这类启动新任务的命令不放行，后续任务等你打开窗口再继续。
- 对话被删除后，Reins 不会再恢复它：`agy --resume` 遇到不认识的 ID 会新建一个对话，所以回调直接标记为 blocked。
- 暂不支持 Antigravity 作为子 Agent（`reins agent agy`）。
