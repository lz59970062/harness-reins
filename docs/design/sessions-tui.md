# 会话托管与 Reins TUI

状态：草案，2026-11-16。使用流程已经和维护者对齐，第 13 节的三项决定已定；第 11 节的 9 项验证已全部完成。目标版本：Claude、Codex、Pi 的会话管理和 DSH 的列表进 0.14（2026-11-17 维护者要求一起适配），可接入的子 agent 进 0.15。

相关文档：会话之间发消息见 [link-protocol.md](link-protocol.md)，现有分层见 [architecture.md](architecture.md)。

## 1. 为什么要做

服务器上只有 CLI 时，用户要看一个会话的进度，现在唯一的办法是 `claude --resume <id>`。这会出问题：

- 窗口关着时，Reins 会无头恢复这个会话，处理回调和 goal 提醒，并接着干活。
- 用户这时 `claude --resume` 进去看，窗口只加载了那一刻的 transcript，之后无头进程和窗口各写各的，会话分成两条分支。窗口里的 Claude 不记得无头那边派过的任务，也不认它改过的文件。
- 无头进程运行期间不检查会话有没有被窗口打开，照样接新回调，分叉越来越大（`harness/claude/stream.py` 的 `supervise` 循环）。

Desktop 用户没有这个问题，因为 Desktop 本来就能看各个会话的状态。本设计要在终端里给出同样的能力：用一个终端、认证一次，就能看、打开、管理所有会话，并且同一个会话永远只有一个进程。

**包含：**

- 基于 Zellij 的 TUI：左侧 Reins 面板，右侧按需分屏的会话窗格和日志窗格。用户管理会话只通过它，覆盖所有 Harness，操作一致
- Claude 会话改由官方后台会话托管，Reins 的恢复不再起无头进程
- Codex 会话由它自己的 app-server 守护进程托管，Pi 会话在 Reins 的 Zellij 窗格里运行，DSH 只列出（0.14，见第 6 节）

**不包含：**

- 自己实现聊天客户端。会话交互一律用各 Harness 的原生界面
- 解析或展示模型的思考内容
- 跨机器查看会话。多机走 Link 协议，以后再做
- Windows 上的分屏。Zellij 不支持 Windows，Windows 第一版只有命令行

## 2. 五条不变量

1. **一个会话同一时间只有一个进程。** 打开、恢复、投递之前都先查会话状态，绝不另起第二个进程。
2. **状态现问源头。** Reins 不另外记一份 Harness 会话的状态。Claude 问 `claude agents --json --all`，Zellij 托管的会话问 Zellij，任务、回调、goal 看 Reins 自己的记录。用户用原生命令做的任何改动，下次读状态时都能看到。面板唯一自己记的是用户的偏好：哪些会话置顶、哪些归档（`~/.reins/tui/sessions.json`），它们只决定列表怎么排、显不显示，不代表会话状态。
3. **只用官方的公开接口。** 只调用用户安装的原版 Harness 程序和它公开的命令。不直接请求模型 API，不读、不存、不转发登录凭据，不碰思考内容。
4. **两组命令分开。** 给 agent 用的命令不交互、不阻塞，只能操作自己名下的东西；给用户用的命令可以交互，能看全部。用户命令不写进技能文档。
5. **回调送到会话的托管者那里。** 会话由谁托管，回调就交给谁，不为投递单独起进程。

## 3. 两组命令

### 3.1 给 Agent 用的命令

基本就是现有命令，本设计不改它们的行为：

| 用途 | 命令 |
|---|---|
| 派活 | `reins run`、`reins agent`（`continue`、`fork`、`expert`）、`reins done` |
| 收结果 | `reins ack`、`reins wait`、处理 `[reins-status]` 修复块 |
| 多步骤工作 | `reins goal start / check / set-plan / ack` |
| 会话间通信 | `reins session register / list / unregister`、`reins send / reply / inbox` |
| 看任务输出 | `reins log <id> --no-follow` |

规则：

- 不需要 TTY，输出方便程序读取，绝不无限期阻塞。
- 只操作自己名下的任务、子 agent、goal 和消息。
- `reins ack` 只该由收到回调的 agent 执行。用户想让一条回调不再投递，用 `reins cancel`。

### 3.2 给用户用的入口

会话管理（列出、新建、打开、停止、删除、看最近输出）只在 TUI 里提供，不单独做一组命令行子命令（2026-11-15 决定）。概念最少，也不会和 agent 用的 `reins session` 混淆。

| 用途 | 入口 |
|---|---|
| 主界面 | `reins tui` |
| 直接打开某个会话 | `reins tui <会话ID或任务ID>`：进入 TUI 并按第 5 节打开它 |
| 发消息 | TUI 里按 `m`，或 `reins send <会话> …`（和 agent 用的同一条） |
| 看任务 | TUI 里按 `l`，或 `reins log <任务ID>`、`reins status` |
| 手动清理 | TUI 里按 `c`，或 `reins cancel`、`reins retry` |
| 安装维护 | `reins setup`、`reins doctor` |

规则：

- `reins tui <id>` 接受会话 ID，也接受任务 ID 或回调 ID，并从任务记录找到派它的会话。用户通常记得任务，不记得会话。
- 在会话里（检测到 Reins 的 agent 环境）调用 `reins tui` 时直接报错，说明这是给用户用的入口。
- 代价：会话的新建、停止、删除没法写进脚本。真有需要时，可以直接用各 Harness 的原生命令（如 `claude --bg`、`claude stop`），Reins 读状态时照样能看到（不变量 2）。
- `reins log` 默认持续跟踪，是给人用的。stdout 不是 TTY 时自动不跟踪，防止 agent 被卡住。

## 4. 会话状态

### 4.1 统一的状态

| 状态 | 含义 |
|---|---|
| `attached` | 由 Reins 侧托管，正在某个 TUI 窗格里接着 |
| `running` | 由 Reins 侧托管，没有窗格接着 |
| `foreign` | 在 Desktop 或 Reins 之外的终端窗口里开着 |
| `stopped` | 托管者还记得它，但进程已经停了 |
| `closed` | 没有任何进程，只有 transcript |

另外标注忙闲（`busy` / `idle`），以及名下正在跑的任务数、未处理的回调数和 goal。

### 4.2 Claude 的状态从哪来

以 `claude agents --json --all` 为准。2.1.325 上实测的字段（2026-11-16，V8）：

| 字段 | 交互会话 | 后台会话 |
|---|---|---|
| `kind` | `interactive` | `background` |
| `id`（短 ID，attach 用） | 无 | 有，即会话 ID 的前 8 位 |
| `pid` | 有 | 运行中有；进程已退出为空 |
| `status` | `idle` 等 | 运行中有；进程已退出为空 |
| `state` | 无 | `working`、`done`、`blocked`（实测见过这三种；`blocked` 出现在一次工具调用被拒之后） |
| `sessionId`、`name`、`cwd`、`startedAt` | 有 | 有 |

映射：

- `kind` 为 `background` 且有 `pid` → `running`；被某个 Reins 窗格接着时为 `attached`。`claude agents` 不报告有没有客户端接着，`attached` 只看 Reins 自己的窗格
- `kind` 为 `interactive` → `foreign`
- `kind` 为 `background` 但 `pid` 为空 → `stopped`。后台会话空闲一段时间后进程会自己退出（实测隔夜后已退出），`claude attach` 时自动重新拉起（V1）
- 不在列表里、但有 transcript 的 → `closed`
- 忙闲看 `state`（后台会话）或 `status`（交互会话）

后台会话在 `~/.claude/sessions/<pid>.json` 里也有记录：`kind` 为 `bg`，`entrypoint` 为 `cli`。现有的 `open_window` 判断会把它当成开着的窗口，所以现有投递逻辑不会对后台会话起无头进程。`claude agents` 不可用（旧 CLI）时，退回 `~/.claude/sessions/<pid>.json`。

会话名直接用 Claude 自动生成的标题，Reins 不另起名字。

## 5. 打开会话

用户只有一个“打开”动作，由 Reins 按状态选命令：

| 状态 | 动作 |
|---|---|
| `attached` | 跳到那个窗格，不重复打开 |
| `running` 或 `stopped` | 新开窗格，运行 `claude attach <id>` |
| `closed` | 先 `claude --bg --resume <id>` 转成后台会话，再新开窗格 attach |
| `foreign` | 不打开，提示会话在哪里开着；可以发消息、看任务日志。原来的窗口关掉后，状态变成 `closed`，就能打开 |
| 有 Reins 旧方式的无头进程 | 等它做完当前这一轮退出，再按 `closed` 处理 |

依据：

- `claude attach`：“Open the background session in this terminal… The session keeps running either way.”（2.1.321 `--help`）
- `claude stop`：“Its conversation is kept; resume it later with `claude attach <id>`.”
- `--bg --resume <session-id>`：“continues that session in the background under the same ID, or starts a copy and says so when the session is already running.” Reins 必须先确认会话不在运行，才能用它。已停止的后台会话只能不带别的参数唤醒，否则也会起副本（V12）。
- 官方文档：对正在后台运行的会话执行 `claude --resume`，会接到这个会话上，而不是重新加载 transcript。所以用户在别处手动 resume 也不会分叉。

新建会话一律用 `claude --bg` 启动，再 attach。

实测（2.1.325，V1、V2、V3、V5）：

- 进程已退出的后台会话，`claude attach` 会重新拉起它，显示原来的对话，会话 ID 和 transcript 文件不变，没有新分支。
- 对交互会话执行 `claude attach`，返回 `No job matching '<id>'`，退出码 1，不另起副本。所以 `foreign` 会话不会被 attach 意外抢走。
- 已关闭的普通会话用 `claude --bg --resume <id> [提示]` 拉起后，变成后台会话，ID 和 transcript 文件不变，新回复接在原分支后面。
- 两个终端可以同时 attach 同一个后台会话，画面同步，两边都能输入，transcript 仍是一条分支。TUI 默认跳到已经打开的窗格，不重复打开；用户想在两个窗格里看同一个会话也可以。
- 所有客户端都断开后，后台会话继续运行。

## 6. 托管与投递

| Harness | 托管者 | 回调投递 |
|---|---|---|
| Claude | 官方后台会话（`--bg`、`attach`、`stop`、`rm`） | 现有的 Stop / SessionStart hook（`asyncRewake`） |
| Codex | Codex 自己的 app-server 守护进程；Reins 窗格运行 `codex resume --remote`（V16） | 线程在守护进程上时在守护进程上开新一轮，并把队列加进可写目录（V22；只读线程走 `codex queue`），否则沿用 `codex exec resume` |
| Pi | Reins 的 Zellij 窗格运行 `reins pi`，Pi 随窗格结束 | Pi extension 的信箱 |
| DSH | 不托管：没有终端界面，用它自己的网页版或桌面版 | 现有的 live 通道 |

Claude 不放进 Zellij 托管：Desktop 和其他终端不知道 Zellij 里有这个会话，`claude --resume` 会另起进程，又回到分叉问题。官方后台会话自带防分叉保证，比统一实现更重要。

### 6.1 Reins 的恢复方式改变

现在：会话关闭时，有回调或 goal 提醒，daemon 用 `claude -p --resume <id>` 起无头进程。

改为：daemon 用 `claude --bg --resume <id>` 把会话拉成后台会话，回调通过 hook 送进去。用户随时可以 attach 进去看，不会分叉。

- 旧版 CLI 没有 `--bg` 时，退回现在的无头 stream 方式。
- 无头 stream 方式本身也要补交接（0.14 的 PR 0，不依赖本设计的其他部分）：`supervise` 循环发现这个会话有窗口打开后，不再接新回调，并关闭 stdin，让它做完当前这一轮就退出；同时把这一轮的最终输出作为一条消息送进窗口，因为窗口打开时这一轮还没写进 transcript。

### 6.2 后台会话的回收

后台会话长时间空闲时，Reins 不主动停止它，由 Claude 自己管理（实测空闲后进程会自己退出）。用户可以在 TUI 里按 `s` 或用 `claude stop` 停掉，下次打开或有回调时再拉起来。资源占用和额度消耗跟普通会话一样，这一点在用户文档里写明。

进程已退出的后台会话收到回调时怎么拉起，要在 PR 2 里验证：先试 hook 能不能在 `claude attach` 之外触发重新拉起；不能的话，daemon 用 `claude --bg --resume <id>`。

### 6.3 后台会话的托管进程

实测（V7）：`claude --bg` 把会话交给 Claude 自己的守护进程（`claude daemon`，下面每个会话一个 `bg-pty-host`）。启动它的进程退出后，会话照常运行：从一个运行完就退出的 systemd 临时单元里起的后台会话，单元结束后仍在运行。

但守护进程留在第一个启动它的进程所在的 cgroup 里。本机它在一个已经断开的 SSH 登录会话（`session-17.scope`，状态 `closing`）里，因为 `KillUserProcesses=no` 且开了 linger，进程没有被杀掉。在 `KillUserProcesses=yes` 的机器上，SSH 一断，所有后台会话都会被杀掉。

所以 Reins 拉起后台会话时，如果 Claude 守护进程还没运行，就先在 `systemd-run --user --scope` 里启动，让它不依赖任何登录会话。macOS 和 Windows 的做法在实现时再定。

### 6.4 权限模式与 Reins 命令

实测（V6）：2.1.325 的后台会话默认开着 auto mode。回调通过 hook 送进了没有客户端接着的后台会话，但会话里的 Claude 执行 `/home/lizhi_2024/.reins/bin/reins ack <id>` 时，被 auto mode 的分类器以 “Destructive or Unwanted Actions” 拒绝，回调一直停在 pending。

V9 实测（2026-11-16，三个后台会话，各投递一次回调）：

| 配置 | 结果 |
|---|---|
| 项目 `.claude/settings.json` 的 `permissions.allow`，旧写法 `Bash(reins ack:*)` 和完整路径的同类写法 | ACK 仍被拒 |
| `claude --bg --settings '<JSON>'` 传入 `permissions.allow`，新写法 `Bash(/home/…/.reins/bin/reins ack *)` 和 `Bash(reins ack *)` | ACK 通过 |
| `claude --bg --settings '<JSON>'` 传入 `autoMode.allow`（`"$defaults"` 加一句说明 `reins ack` 是预期操作） | ACK 通过 |

第一种失败的原因没有单独拆开：可能是旧的 `:*` 写法没匹配上，也可能是项目级设置的问题。官方文档说具体的 Bash allow 规则在分类器之前生效，宽泛的规则（如 `Bash(*)`）进入 auto mode 时会被丢弃；`autoMode` 只从用户设置、托管设置或 `--settings` 读取，不读项目设置（[auto-mode-config](https://code.claude.com/docs/en/auto-mode-config.md)）。

处理：

- Reins 不替用户改会话的权限模式，沿用用户的默认设置。
- **Reins 拉起的后台会话**：启动时用 `--settings` 传入具体的 allow 规则，只限第 3.1 节的 agent 命令（如 `reins ack *`、`reins goal check *`），不含 `cancel --all` 之类，也不用宽泛的通配。不改用户的设置文件。优先用 `permissions.allow`，它是确定的规则匹配；`autoMode.allow` 依赖分类器的理解，只作后备。
- **用户自己开的会话**（Desktop、终端）：`reins setup` 已经会改 `~/.claude/settings.json` 装 hook，同时加上同样的具体 allow 规则，并在输出里列出加了哪些。
- 规则用 `*` 的新写法，并在 PR 2 里补一次项目级和用户级设置的对照测试，弄清第一种失败的原因。
- 回调文本里仍要告诉 agent：ACK 被拦时，请用户批准，不要换别的办法绕过。

## 7. TUI

### 7.1 布局

```text
┌─ Reins 面板 ──────────────┬─ 会话：嵌入空间运动图 ─────────────┐
│ [会话] 任务  Goal  消息    │                                     │
│ ● 嵌入空间运动图   后台 2任务 │   （Claude 原生界面，claude attach）  │
│ ○ 两页进展 PPT   Desktop   │                                     │
│ ◌ LTC Callback   已关闭    ├─ 任务 cf408bf5 日志 ────────────────┤
│                           │   （reins log 持续跟踪）              │
└───────────────────────────┴─────────────────────────────────────┘
```

- `reins tui` 接上名为 `reins` 的 Zellij 会话，没有就按 Reins 的布局新建。SSH 断开后布局保留，重新运行 `reins tui` 就接回来。
- 左侧面板是用 Textual 写的终端程序（2026-11-15 决定），随冻结二进制一起打包，有四个标签页：
  - **会话**：会话名、agent 类型、目录、状态、名下任务数；
  - **任务**：每个任务都显示是哪个会话派的；
  - **Goal**：计划和进度；
  - **消息**：收件箱。
- 右侧窗格由面板通过 `zellij action` 打开：会话窗格运行 `claude attach`，任务窗格运行 `reins log`，临时查看用浮动窗格。
- 关掉窗格只是离开，会话照常在后台跑。

### 7.2 按键（初稿）

| 键 | 动作 | 命令行里的同等操作 |
|---|---|---|
| `Enter` | 打开会话；已经开着就跳过去 | `reins tui <id>` |
| `n` | 新建会话 | 无 |
| `l` | 看任务日志 | `reins log` |
| `m` | 发消息 | `reins send` |
| `s` | 停止会话 | 无（可用原生命令） |
| `c` | 取消回调 | `reins cancel` |
| 待定 | 在当前窗格里切换到另一个会话 | 无 |

发消息、看日志、取消回调和命令行共用同一份实现，行为一致。

### 7.3 滚动

Zellij 默认就能用鼠标滚轮滚动，每个窗格有自己的回滚历史。翻看 Claude 的长对话用 Claude 自带的 Ctrl+O。不选 tmux 是因为 tmux 默认不能用鼠标滚动，打开后也只能在复制模式里滚，而且 Claude 的重绘会把历史塞满重复画面。

### 7.4 Zellij 的分发

- 固定一个 Zellij 版本，放进 npm 平台包（`@harness-reins/linux-x64`，以后的 macOS 包）。服务器访问 GitHub 不稳定，npm 有国内镜像，随包走最可靠。
- Reins 只用自带的这份和自己的配置目录，不受用户本机的 Zellij 影响。
- Zellij 是 MIT 许可，随包附上许可证。
- pip 安装和 Windows 包不带 Zellij。pip 安装时，`reins tui` 找本机的 `zellij`，找不到就提示怎么装。

## 8. 典型流程

**新开会话派活，然后离开**

1. `reins tui`，按 `n`，选 Claude 和目录。Reins 用 `claude --bg` 起会话，新开窗格接上。
2. 用户布置工作；agent 用 `reins goal start` 开 goal，用 `reins run` 派出任务。
3. 用户关掉窗格或断开 SSH，会话照常在后台跑。
4. 任务结束，回调通过 hook 送进会话，agent 处理后 ACK，面板里任务变成已完成。

**回到之前派过任务的对话**

1. 在“会话”页找到它，或者在“任务”页选中任务，跳到派它的会话。
2. 按 `Enter`，Reins 按第 5 节的表处理。

**看子 agent**

1. agent 用 `reins agent` 起了子 agent，面板里它显示在父会话下面。
2. 用户打开它的窗格查看，可以直接插话，必要时停掉。结果照常回到父会话。

子 agent 默认作为可接入的会话运行（2026-11-15 决定）：Claude 子 agent 是后台会话，Codex、Pi、DSH 子 agent 跑在 Reins 的 Zellij 托管里。0.15 和 Codex、Pi 的托管一起实现；在那之前，子 agent 仍是无头运行，面板里只能看日志。

用户插话会改变子 agent 的走向，它最后交回父会话的结果要注明“途中有用户介入”，避免父会话误以为结果完全按它的指令产生。

**断线、换电脑**

重新 SSH 上来，运行 `reins tui`，回到同一个 Zellij 会话，布局和窗格都在。

## 9. 安全边界

依据官方文档（2026-11-15 查阅）：

- **登录方式。** 官方禁止第三方产品接入或代理 claude.ai 登录，也禁止收集、保存或转手登录凭据和会话令牌；但明确允许最终用户用自己的订阅登录原版 Claude Code（[legal-and-compliance](https://code.claude.com/docs/en/legal-and-compliance.md)）。Reins 只调用用户自己安装、自己登录的原版 `claude`，不接触凭据。
- **思考内容。** 官方说明原始思考过程本来就拿不到，只有摘要；新模型会拒绝提取推理过程的请求（[thinking](https://platform.claude.com/docs/en/build-with-claude/thinking)）。Reins 不解析、不展示、不保存任何思考内容，会话交互全用 Claude 原生界面。
- **transcript。** 官方明说格式随版本变，直接解析的脚本随时会坏（[sessions](https://code.claude.com/docs/en/sessions.md)）。Reins 的状态以 `claude agents --json` 为准，不依赖 transcript 的结构。
- **不用的功能：**
  - channels 支持双向收发和远程批准权限，但自定义 channel 要加 `--dangerously-load-development-channels`，在 `-p` 下还会被忽略（[channels-reference](https://code.claude.com/docs/en/channels-reference.md)）。
  - Remote Control 没有给第三方客户端的接口，第二个连接接入会把第一个顶掉（[remote-control](https://code.claude.com/docs/en/remote-control.md)）。

## 10. 和现有功能的关系

- **`reins session`**：保持现有含义（会话名注册，给 agent 用），不加生命周期命令。
- **Claude hook**：继续是投递回调到 Claude 会话的主通道，后台会话也用它（待验证 V6）。
- **无头 stream 恢复**：保留作为旧 CLI 的后备，并补上 6.1 的交接。
- **`claude_moves.py`**：继续跟踪会话 ID 变化，`claude agents --json` 可以作为它的额外数据来源。
- **`reins log`**：0.13.3 已发布，加上 3.2 的非 TTY 自动不跟踪。

## 11. 待验证

全部只用官方命令，在本机测。会新建几个后台会话，消耗少量额度。V4 需要维护者在 Desktop 里配合。

用维护者终端里实际使用的 `claude`（nvm 安装，2026-11-15 为 2.1.325，已登录）来测，不用 Desktop 自带的那份（2.1.321）。本文引用的 `--help` 原文来自 2.1.321，测试时先用 2.1.325 核对一遍。

2026-11-16 在本机用 2.1.325 测过（测试目录在临时目录里，模型用 Haiku）。transcript 是否分叉，按 `uuid` / `parentUuid` 数叶子节点判断。

| 编号 | 问题 | 结果 |
|---|---|---|
| V1 | 进程已退出、或被 `claude stop` 的后台会话再 `claude attach` | **通过。** 重新拉起，原对话还在，ID 和 transcript 文件不变，1 个叶子 |
| V2 | 对交互会话执行 `claude attach` | **通过。** `No job matching '<id>'`，退出码 1，不另起副本 |
| V3 | 对已关闭的普通会话执行 `claude --bg --resume` | **通过。** 变成后台会话，ID 不变，transcript 1 个叶子，新回复接在后面 |
| V4 | 会话作为后台会话在跑时，在 Desktop 里打开它 | **通过。** 见下 |
| V5 | 两个终端同时 attach 同一个后台会话 | **通过。** 两边同时可见、都能输入，transcript 1 个叶子 |
| V6 | Reins 的 hook 投递进没有客户端接着的后台会话 | **投递通过，ACK 不通过。** 回调经 SessionStart hook 送达，没有起无头进程；但 `reins ack` 被 auto mode 分类器拒绝（第 6.4 节） |
| V7 | 后台会话的托管者 | **通过，有条件。** 启动它的进程退出后仍在运行；但 Claude 守护进程挂在第一个启动它的登录会话的 cgroup 里（第 6.3 节） |
| V8 | `claude agents --json --all` 的状态取值 | **通过。** 字段和取值见第 4.2 节；不报告是否有客户端接着 |
| V9 | auto mode 是否尊重 `reins ack` 等命令的 allow 规则 | **通过，有条件。** 用 `--settings` 传入新写法的 `permissions.allow`，或 `autoMode.allow`，ACK 都通过；项目设置里的旧写法不行（第 6.4 节） |
| V10 | 会话开在交互窗口里时执行 `claude --bg --resume` | **不分叉，但会起副本。** 退出码 0，提示 “open in another Claude Code process, so this started a copy as <新ID>”，原 transcript 不变。Reins 先查状态；万一起了副本，按输出里的短 ID 认出来，立刻 `stop` 加 `rm` |
| V11 | 不带提示的 `claude --bg --resume <id>` | **通过。** 会话以原 ID 起来，空闲等待输入；hook 把回调送进去，会话里的 Claude 执行 `reins ack`（`--settings` 带了 allow 规则）成功，全程没有无头进程 |
| V12 | 对已停止的后台会话执行 `--bg --resume` | **不带别的参数才接原会话。** 带了 `--model` 等参数，会另起副本（“keeps its own saved options, so the flags you passed started a copy”）；不带参数时，以原 ID 和当初保存的参数（含 `--settings`）唤醒 |
| V14 | Zellij 0.46.0 里的 `reins tui` | **通过。** 在伪终端里对真实队列和 Claude 实测：打开已关闭的会话（先转后台再 attach）、拒绝打开 Desktop 里开着的会话、停止后窗格自动关闭、对已停止的会话按 Enter 重新拉起、新建会话、开日志窗格。注意：自定义配置目录下 Zellij 仍会弹出首次运行的配置向导（`configuration` 插件），Reins 建好会话后关掉它；`list-panes` 过一会儿就不再报告 `claude attach` 窗格的命令，所以 Reins 按自己起的窗格名（`claude <短ID> <标题>`）认窗格 |
| V15 | 分叉 | **通过（Claude 2.1.326）。** `claude --bg --resume <id> --fork-session --name N` 打印新的短 ID，新会话带着原来的对话，原会话不受影响；在 Desktop 里开着的会话也能这样分叉。面板菜单的 Fork 用的就是它 |
| V16 | Codex 0.166 的共享守护进程 | **通过。** `codex resume --remote unix://<socket> <id>` 把线程加载到用户的 app-server 守护进程上（`thread/loaded/list` 可见）；客户端退出后线程仍留在守护进程上，空闲几分钟后由守护进程卸载。不带 `--remote` 的 `codex resume` 在自己进程里加载，守护进程看不到 |
| V17 | Codex 会话列表 | **通过。** 守护进程的 `thread/list` 给出 id、name、preview、cwd、createdAt 和 status（notLoaded / idle / active + activeFlags / systemError），默认只含交互来源，不含 `exec` 和子 agent；带 `useStateDbOnly` 约 20 ms，扫描 rollout 要 1.5 到 2.7 s |
| V18 | 不在守护进程上的 Codex 写入者 | **通过（Linux）。** 不带 `--remote` 的 `codex resume` 和 `codex exec resume` 一直开着线程的 rollout 文件；守护进程也开着它已加载线程的 rollout。所以面板按 `/proc/<pid>/fd` 找出守护进程以外开着 rollout 的 codex 进程，标为别处打开或无头，不再打开 |
| V19 | 用协议新建 Codex 线程 | **不可用。** `thread/start` 建出的线程在第一轮之前 rollout 是空的，`codex resume` 报 “no rollout found”。所以新建会话由 `codex --remote` 自己的窗口创建，面板在线程出现在列表里后给窗格改名 |
| V20 | 用协议分叉 Codex 线程 | **通过。** `thread/fork` 返回新线程，带着原来的对话，加载在守护进程上，可以直接 `codex resume --remote` 打开 |
| V21 | 投递进守护进程上的线程 | **通过。** 线程已加载且空闲时，`codex queue --thread <id> --message …` 由守护进程自己开始新一轮；未加载时加 `--resume` 才会加载并运行。守护进程上线程的工具环境没有 Desktop 标记，所以这些线程派的任务原本会走 `codex exec resume`，等于第二个写入者；现在投递前查到线程在守护进程上，就改走 `codex queue`（端到端实测：没有起 `codex exec`，回调作为新一轮出现在 Reins 窗格里）。这一轮用线程自己的权限：`workspace-write` 下 `~/.reins` 只读，ACK 要提权，或者把 `~/.reins` 加进 Codex 配置的 `writable_roots`（`--remote` 客户端不接受这个覆盖） |
| V22 | 回调那一轮的沙箱 | **通过。** 守护进程上线程的 `turn/start` 接受 `sandboxPolicy`，`writableRoots` 里的目录这一轮就能写（对照：同样的 `workspace-write` 不加时被拒），并延续到之后的轮次；`thread/resume` 返回线程当前的 `sandbox`。所以 Reins 对已加载的线程直接开一轮，在线程原有策略上只追加队列和通讯录目录，不改 cwd 和审批设置；只读线程不放宽，改走 `codex queue`。端到端实测：队列放在工作区和 `/tmp` 之外，agent 的 `reins ack` 不用提权就写入，没有起 `codex exec` |
| V13 | 其他 | `--bg` 拒绝没信任过的目录（“Workspace not trusted”）；`claude rm` 只删后台任务，有对话的 transcript 保留；会话记录文件只有 `status`，没有 `state`；刚起来还没收到提示的后台会话，`claude agents` 报 `state: blocked`、`status: idle`；从 Claude 会话里启动 `claude` 会继承 `CLAUDE_CODE_CHILD_SESSION` 等变量，不保存 transcript，所以 Reins 启动 Claude 时清掉这些变量 |

**V4 的过程：**

1. 维护者在 Linux 服务器上运行 `claude --resume <id> --desktop`，CLI 回复 `--desktop isn't available on this platform. It works on macOS and Windows (x64).`
2. 在 Desktop 里打开同一目录，会话列表里也找不到这个 CLI 起的后台会话。
3. 更要紧的情况是 Desktop 自己建的会话：关掉后被 Reins 拉成后台会话，用户再在 Desktop 侧边栏点开它。Desktop 在服务器上打开会话的方式是 `ccd-cli --resume=<id> --input-format stream-json --output-format stream-json …`（从运行中的 Desktop 会话进程参数看到）。用 Desktop 自带的 ccd-cli 2.1.321 照这个方式去恢复正在后台运行的会话，CLI 直接拒绝，没有写 transcript：

   ```text
   Error: That session is running in the background (7f7775f4). Run `claude attach 7f7775f4` to open it, or `claude stop 7f7775f4` first to resume it here. Add --fork-session to branch off a copy instead
   ```

   transcript 仍是 1 个叶子，后台会话照常运行。

结论：Desktop 打不开正在后台运行的会话，所以不会分叉。代价是用户在 Desktop 里点开一个被 Reins 转成后台会话的会话时，会看到报错。TUI 和用户文档要说明：在 TUI 里停止这个会话后，Desktop 就能打开。同样的保护也说明，Reins 现有的 `claude -p --resume` 无头恢复碰到正在后台运行的会话会被拒绝，不会分叉。

另外，Zellij 里跑 `claude attach` 的滚动和按键体验，要维护者亲自试过才算数。

**顺带发现：** 维护者终端里的 `reins` 先找到 nvm 全局装的 npm 包 0.11.0（`~/.nvm/versions/node/v22.23.3/bin/reins`），再转给已安装的 0.13.2 执行，每次都会打印一行提示。功能不受影响，升级 npm 包后就会消失。

## 12. 实现顺序

不发 0.13.4，原定的修复全部并入 0.14 一起发（2026-11-16，维护者决定）。

1. **验证**：第 11 节已完成。
2. **0.14 PR 0**：不依赖本设计其他部分的修复，可以和 PR 1 并行，任何时候合并：
   - **registration 锁**：拿不到锁时最多等约 30 秒；拿到锁后在锁文件里写入进程号、主机和时间；超时后报出锁文件路径、持有者以及它是否还活着，提示用 `fuser -v <路径>` 查；`reins setup` 打印正常的错误，不再出现 traceback。不自动删锁。
   - **6.1 的无头交接**：`supervise` 发现会话有窗口打开后，不再接新回调并关闭 stdin，把这一轮的最终输出送进窗口。
   - **`reins log`**：stdout 不是 TTY 时自动不跟踪。
   - **普通会话里的 ACK**：Desktop 的会话同样以 `--permission-mode=auto` 运行（第 11 节 V4 看到的进程参数）。要在真实的 Desktop 会话和终端会话里确认，auto mode 会不会拦 hook 送来的回调的 `reins ack`。会拦的话，`reins setup` 写入的 allow 规则（第 6.4 节）提前到 PR 0 做。
3. **0.14 PR 1**：Claude 状态读取（`claude agents --json`）和统一状态模型，作为内部模块，带测试。
4. **0.14 PR 2**：
   - 会话的新建、打开、停止、删除（内部实现）；
   - daemon 的恢复改用 `--bg --resume`；
   - Claude 守护进程放进独立的 systemd scope（第 6.3 节）；
   - Reins 拉起的会话用 `--settings` 带上 allow 规则，`reins setup` 也写入同样的规则（第 6.4 节）；
   - 补一次 allow 规则在项目级和用户级设置下的对照测试。
5. **0.14 PR 3**：`reins tui`、Textual 面板、Zellij 布局；Textual 进冻结构建，Zellij 打进平台包。
6. **0.14 发版附带**：
   - macOS 平台包：等维护者在 Mac 上构建好 `@harness-reins/darwin-*`，再把它加进主包的 `optionalDependencies`；
   - npm 启动脚本：对没有预编译包的平台，直接提示用 pip 从源码安装；
   - 安装文档：去掉写死的 `v0.7.0`。
7. **0.15**：Codex、Pi、DSH 的 Zellij 托管，子 agent 默认作为可接入的会话。

每一步合并前都要 K3 审查，并在真实 Claude 会话上验证。

## 13. 决定

**已定（2026-11-15，维护者）：**

1. 不做单独的会话管理命令组，用户管理会话只通过 `reins tui`；另加 `reins tui <id>` 直接打开某个会话（第 3.2 节）。
2. 面板用 Textual。
3. 子 agent 默认作为可接入的会话运行，0.15 实现（第 8 节）。

**留到实现时：**

4. Zellij 版本：实现时取最新稳定版，先让维护者在本机试过滚动和按键再固定。macOS 包是否带它，随 macOS 包一起定。
5. Codex 的回调投递：0.15 再定。往 Zellij 窗格里模拟打字太粗糙，要先查 Codex 有没有正规的接口。
