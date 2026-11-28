# Reins Link 协议

状态：第 3 版，2026-11-28。单机部分（第 12 节的 PR 0、1、2a、2b、2c）已在 0.11.0 发布。多机部分在第 3 版改为配对邮箱（第 1.1、9 节），取代原来的 Link 长连接服务和 TLS 端口。第 13 节的决定已定。

安全边界见 [link-threat-model.md](link-threat-model.md)。记录格式、ID 和能力清单的通用规则沿用 [protocol-v1.md](protocol-v1.md)，本文只写新增和不同的部分。

## 1. 目标

让运行中的 Codex、Claude Code、Pi 会话互相发消息，不论它们在同一台机器还是不同机器。

**包含：**

- 用名字寻址任意已注册的会话
- 持久化的消息，不因断线、重启或会话关闭而丢失
- 跨机器传输：共享目录和 SSH，两者是同一个邮箱抽象的两种实现
- 防止 Agent 之间无限对话

**不包含：**

- 文件和产物同步。消息只传文字和引用（第 6.3 节）
- 流式输出、token 级实时共享
- 多用户组织、中心账号系统
- 跨机器的共享任务调度。`--host` 只负责在指定机器上启动（第 11 节）

### 1.1 多机方向（已定，第 3 版）

这几条是多机工作的基调。要改其中任何一条，先改本文。

1. **存储转发，不建长连接服务。** 两个节点配对后共用一个邮箱，消息和回执都是邮箱里的文件。没有常驻的 Link 服务，没有帧协议、重连和 TLS 端口。
2. **一个邮箱抽象，两种实现。** 共享目录（NFS、JuiceFS、共享云盘）和 SSH 实现同一组操作（第 9.1 节），握手、签名、去重、回执、重试只写一份。
3. **谁能连，谁搬运。** SSH 时邮箱放在被连的那台机器上，被连方只读写本地文件，连接方负责两个方向的搬运。只要有一个方向能连通，消息就双向可达。
4. **配对靠握手商定，不手写配置。** 对端的节点 ID、别名、协议版本、能力和配对密钥由 `reins link connect` 的握手决定，Reins 自己保存结果。
5. **塔台不碰网络。** 搬运由每个对端一个的 `_link-sync` 子进程完成，它只把文件搬进本地 `inbound/`。塔台调用的准入逻辑和 `reins send` 是同一份，限额也是同一份。投递引擎不知道消息从哪来。
6. **限额和权限由接收方执行。** 发送方的声明一律不作为依据。跨机器的 `wake` 和本机一样（第 6.2 节），会话不在就按它的 Harness 规则拉起，这样才能无人值守；接收方可以对某个对端关掉（`--wake never`）。
7. **绝不猜。** `reviewer@gpu1` 里的 `gpu1` 没配对就是 `unknown_peer`，不会退回到本机同名会话。

TLS 端口、配对码之外的证书体系和远程启动（第 11 节）等到真有需要再做。

## 2. 五条不变量

所有实现和评审都以这五条为准。任何 PR 违反其中一条，都要先改本文，不能在代码里破例。

1. **传输与消息分离。** 邮箱和搬运不知道消息怎么投递，投递引擎不知道消息来自共享目录还是 SSH。
2. **写入者明确。** 每类文件的写入者和锁见第 8 节。`_link-sync` 不写 `queue/**`。邮箱里每个文件只有一个写入者。
3. **身份来自配对，不来自内容。** 对端是谁由配对时商定的密钥决定：邮箱里每个文件都带这个密钥的 HMAC，验不过的文件不导入。文件里自称的节点 ID 不能作为依据。
4. **绝不猜测目标。** 名字解析失败就拒绝，不退回“最近的会话”。沿用 protocol-v1 的显式指定原则。
5. **未确认的结果不等于成功。** 每个状态只表示它定义的事实（第 7 节），不能把“已发出”说成“已读”。

## 3. 概念

| 名词 | 含义 |
|---|---|
| 节点（node） | 一台机器上一个 OS 账号的一份 Reins，由 `~/.reins` 确定 |
| 对端（peer） | 和本节点配对过的另一个节点，用别名称呼，例如 `gpu1` |
| 会话名（session name） | 注册到本节点的逻辑名字，对应一个具体的 Harness 会话 |
| 消息（message） | 一条从会话发往会话的持久记录，一个新的根 |
| 对话（thread） | 一串有回复关系的消息 |
| 邮箱（mailbox） | 一对节点共用的目录，放在共享盘上或 SSH 被连方的磁盘上（第 9 节） |
| 搬运进程 | `_link-sync`：塔台为每个对端启动一个，负责本地发件箱、收件箱和邮箱之间的搬运 |

## 4. 寻址与注册

### 4.1 节点身份

节点第一次注册会话或配对时生成 `node_id`，格式 `n-` + 16 位十六进制，此后不变。人记的是对端别名，例如 `gpu1`。别名只在本节点有效，不通过网络传播。

### 4.2 地址

```text
reviewer           本节点的 reviewer
reviewer@gpu1      对端 gpu1 上的 reviewer
```

名字和别名都符合 `[a-z0-9][a-z0-9_-]{0,31}`。

发送方不需要知道对方的 Harness、会话 ID 或工作目录。这些只保存在目标节点上。

记录里的 target 使用 protocol-v1 预留的 `session`（不同于 origin）语义，并新增两种写法：

```json
{"kind": "session_name", "name": "reviewer"}
{"kind": "session_name", "name": "reviewer", "node": "n-3f9a1c0d7b2e4a65"}
```

### 4.3 注册

```bash
reins session register reviewer          # 绑定当前会话
reins session list                       # 本节点的会话，以及已配对节点上的会话（名字带 @别名）
reins session unregister reviewer
```

- 绑定沿用现有的环境变量（`CODEX_THREAD_ID`、`CLAUDE_CODE_SESSION_ID`、`PI_SESSION_FILE`），识别不到就拒绝。
- 名字已被其他会话占用时拒绝，除非显式加 `--replace`。一个会话只有一个名字，`--replace` 也用于改名。
- `shell` 是保留字：不在任何会话里发出的消息，寄件方显示为 `shell`，不能回复。在会话里发消息必须先注册，否则对方没法回。
- 注册时冻结投递所需的路由和环境（Codex 可执行文件和 profile、Pi 频道目录），因为这些只有接收方自己的环境才知道。
- 配对就是信任：对端能看到本节点所有已注册的名字和 Harness 类型，不包括工作目录、会话 ID、环境。没有 `--share` 这种逐个公开的开关。
- “在线”只是提示，不能作为投递依据。

## 5. 消息记录 `reins.message/1`

```json
{
  "schema": "reins.message/1",
  "id": "m-9c41e07a2b6d13f8",
  "thread": "t-5d20ab7c4e1f9036",
  "in_reply_to": null,
  "hop": 0,
  "created_at": "2026-11-02T08:30:12.114Z",
  "expires_at": "2026-11-09T08:30:12.114Z",
  "from": {"node": "n-1a2b3c4d5e6f7081", "name": "planner", "harness": "codex"},
  "to": {"kind": "session_name", "name": "reviewer", "node": "n-3f9a1c0d7b2e4a65"},
  "subject": "Review the queue change",
  "body": "Please check branch feature/queue.",
  "refs": [{"kind": "git", "repo": "origin", "ref": "feature/queue"}],
  "wake": "never",
  "requires": []
}
```

- **ID：** `m-` 加 16 位十六进制，前 12 位是发送时间（毫秒），后 4 位随机，这样同一接收方的待投递消息按发送顺序处理。`id` 由发送方生成，只有 `(from.node, id)` 才唯一。
- **本地根：** 本节点发出的消息直接用 `id` 作根。对端发来的消息不能这样，不同对端可能用同一个 `id`，甚至故意撞车，所以本地根是 `m-` + `sha256(from.node + ":" + id)` 的前 16 位十六进制。回调 ID 沿用根 + 后缀规则，例如 `m-4be17a90c2d3f615-c1`，ACK 命令是 `reins ack m-4be17a90c2d3f615-c1`。
- **`local`：** 落盘的记录还有一个 `local` 字段，保存接收会话、路由和回调 ID。它只属于本节点，绝不发到网络上。
- **`from.node`：** 接收节点用配对记录里的节点 ID 核对这个字段。不一致时拒绝，错误码 `identity_mismatch`。
- **`hop`：** 新对话为 0，回复时由 Reins 设为父消息的 `hop + 1`，调用方不能指定。跨机器的回复，`in_reply_to` 必须是接收节点自己发给这个对端的消息，`hop` 必须等于它的 `hop + 1`，否则拒绝（`invalid_record`）。所以对端改不小 `hop`。
- **`expires_at`：** 默认 7 天。依赖发送方时钟，只用于清理和提示，不用于安全判断。
- **未知字段：** 原样保留，必需字段缺失则拒绝。`requires` 有不认识的能力时拒绝并报告，不会投递一半。
- **大小限制：** `subject` 最多 200 字符且不能含换行、控制字符或 `[reins`、`ACK after` 这类信封标记；`body` 最多 64 KiB；`refs` 最多 20 项。没给 `subject` 时取正文第一行，那一行不合格就用 `Message from <名字>`。

## 6. 投递语义

### 6.1 走现有投递链路

准入时写入一条 `reins.callback/1`，`root` 是消息 ID。重试、目标锁、ACK、回调提醒都复用现有逻辑，不写第二套。回调的 `requires` 由实际路由决定，例如 `deliver.codex-queue`。

准入只有一个函数：本机 `reins send` 在命令进程里调用它，多机后塔台对 `inbound/` 调用同一个函数。这样 `reins send` 能当场给出 `unknown_name`、`limit_exceeded`，塔台没运行时消息也不丢。

### 6.2 `wake`：是否唤醒已关闭的会话

本机消息和任务结果走同一条路，Claude Code 和 `reins pi` 打开的 Pi 默认 `resume`：没有进程接收时就是要拉起来处理，消息才算可用。Codex 默认 `never`，因为只有队列能进入开着的 Codex，而 Reins 分不清它开着还是已关闭；DSH 和普通 Pi 不能安全地无头恢复，也是 `never`，指定 `resume` 时降级并在正文前注明。跨机器消息的规则相同（维护者决定：无人值守时，另一台机器的消息也要能拉起会话）。接收方可以对某个对端用 `reins link connect 别名 --wake never` 关掉，这个对端的消息就只进收件箱。

| 取值 | 行为 |
|---|---|
| `never` | 只走不启动新进程的通道；不可用时等会话下次打开 |
| `resume` | 没有进程接收时，用无头方式恢复会话。回复链和频率仍受第 10 节限制 |

三种 Harness 在 `never` 下的行为（0.11.0 用真实 Harness 验证过，见第 12.2 节）。Claude Code 和托管 Pi 选 `resume` 时，关闭这一列变成恢复会话：


| Harness | 通道 | 会话忙碌时 | 会话关闭时 | 重新打开后 |
|---|---|---|---|---|
| Codex | `deliver.codex-queue` | Codex 排队，这一轮结束后取走 | 留在 Codex 队列，状态 `published` | 自动收到 |
| Claude Code | `reins wait`（后台）或 `reins claude` 频道；回调模式 `manual` | `reins wait` 退出时唤醒模型 | 留在收件箱，状态 `queued`，不启动进程 | 运行 `reins wait` 或 `reins inbox` |
| Pi | 扩展的 `deliver.pi-steer` | 当前工具批次结束后 steer 进去 | 留在 Reins 队列，状态 `queued`，不启动进程 | 塔台发现邮箱重新打开后投递 |

`reins wait` 每次只等一轮。信封里提醒 Claude 在 ACK 后重新启动它，所以持续接收靠模型重新启动等待，不需要常驻进程。

差异都落到第 7 节的同一套状态上。接收方式可以不同，状态含义不能不同。

### 6.3 消息的正文和引用

**正文对接收模型来说是不可信内容。** 这一点的处理规则见威胁模型的 T2。

- 信封第一行固定为 `[reins-msg] <回调ID>`，只有寄件方、主题、对话和读取方式，正文放进 `details` 文件。
- `details` 里的正文用每条消息随机生成的标记包住，并说明它由其他 Agent 或脚本写成，不是用户的指令。正文无法提前写出这个标记，所以关不上这个围栏。
- 寄件方和主题在写入信封前要做校验，不能出现换行或仿冒信封键（例如 `ACK after inspection:`）的内容。
- `ACK` 命令永远由接收节点生成，不接受消息里传来的任何命令文本。

`refs` 只是引用，Reins 不去获取。跨机器时路径通常无效，所以第一版只定义两种：

| `kind` | 字段 | 说明 |
|---|---|---|
| `git` | `repo`、`ref` | 接收方自己 `git fetch` |
| `path` | `path` | 只对同一节点或共享文件系统有意义 |

## 7. 消息状态

本节点内的消息，发送方看到的状态由回调的状态推导，不另存一份，所以不会出现两边不一致。发往对端的消息，状态来自对端的回执，存在 `sent/` 里，只往前走（第 9.4 节）。终止状态标 ●。

| 状态 | 含义 | 由谁确认 |
|---|---|---|
| `queued` | 已持久化到本节点 | 本节点塔台 |
| `forwarded` | 目标节点已导入并通过准入，回了 `forwarded` 回执。本节点内的消息跳过这一步 | 对端塔台 |
| `published` | 已交给目标 Harness 的通道，例如 `codex queue` 已接受 | 目标节点塔台 |
| `acked` ● | 接收会话运行了 `reins ack`，表示收到并看过 | 接收会话 |
| `rejected` ● | 对端明确拒绝，错误码见第 9.4 节 | 对端塔台 |
| `expired` ● | 到期未送达 | 本节点塔台 |
| `failed` | 投递明确失败，且需要人处理。可用 `reins retry` 修复 | 目标节点塔台 |
| `canceled` ● | 被 `reins cancel` 撤销 | 本节点 |
| `unknown` | 结果不明，例如发布命令被杀。**不自动重发**，等人或 ACK 澄清 | 目标节点塔台 |

**可靠性保证：**

- 节点之间是至少一次传输加去重。正常情况下目标节点只产生一条回调。
- 交给 Harness 这一步没有恰好一次的保证。结果不明时不重发（沿用 0.10 的行为）；CLI 恢复通道可能重复，所以信封里的回调 ID 必须稳定，接收方据此去重。
- 对话内的顺序不保证。需要因果关系时看 `in_reply_to`。
- ACK 不表示同意，也不表示任务完成。

## 8. 文件布局与写入者

```text
~/.reins/link/                 目录权限 0700，文件权限 0600
  node.json                    节点 ID（0.11.0）
  config.yaml                  限额（0.11.0，可选）
  sessions/<名字>.json, .env   会话名和冻结的路由、环境（0.11.0）
  messages/<本地根>.json       本节点收到的消息，写入后不再改（0.11.0）
  threads/<对话>.json          对话计数（0.11.0）
  rates.json                   发送方和收发对的计数（0.11.0）
  alerts/<摘要>.json           给人看的限额和链路告警（0.11.0）
  peers/<别名>.json            配对记录：对端节点、传输、角色、配对密钥（握手产物，不手写）
  peers/<别名>.names.json      对端公开的会话名
  sent/<消息ID>.json           发往对端的消息和回执带来的状态
  outbox/<别名>/<文件>          等待搬进邮箱的消息和回执
  inbound/<别名>/<文件>         已从邮箱取回、等待塔台导入的文件
  mailboxes/<配对ID>/          本节点是 SSH 被连方时，这一对的邮箱
  quarantine/<别名>/<文件>      验签或格式不过的文件，附告警
```

| 文件 | 写入者 |
|---|---|
| `sessions/`、`messages/`、`threads/`、`rates.json`、`alerts/` | 做准入或注册的进程，持 `link` 锁：`reins session`、`reins send`，以及导入对端消息的塔台 |
| `queue/pending/<回调ID>.json` | 同上，和 `reins done` 一样持久化后交给塔台 |
| `queue/**` 的其余部分 | 塔台和 `reins ack` 等已有写入者（不变） |
| `peers/` | `reins link connect`、`reins link remove`、被连方的 `_link-box`（握手），持 `link` 锁 |
| `sent/`、`outbox/` | `reins send`（消息）和塔台（回执、状态），持 `link` 锁 |
| `inbound/`、`quarantine/` | `_link-sync`，每个对端一个，持这个对端的 `link-sync-<别名>` 锁 |
| `mailboxes/` | 被连方的 `_link-box` 和被连方自己的 `_link-sync`，遵守第 9.1 节的单写入者规则 |

准入先写消息记录，再写回调。进程在两步之间崩溃时，塔台发现消息没有回调，会按记录补上，回调 ID 是固定的，不会重复。

`_link-sync` 不读写 `queue/**`，不导入 `tower`。塔台离线时，`_link-sync` 仍可接收并落盘到 `inbound/`，塔台恢复后再处理。全部写入使用 `system/files.py` 的原子写入。

**分层：** 不新增包。记录、签名和地址在 `core/linkwire.py`，文件在 `store/link.py`，邮箱、握手、搬运和导入在 `engine/`，命令在 `commands/link.py`。

## 9. 配对邮箱 `reins.link/1`

### 9.1 邮箱

每一对节点共用一个邮箱：

```text
<邮箱根>/
  hello/            握手文件（只用于共享目录配对）
  to-<A节点>/        发给 A 的文件，B 写，A 取走后删除
  to-<B节点>/        发给 B 的文件
```

邮箱根放在双方都能到达的地方：

| 传输 | 邮箱根 | 本节点怎么访问 |
|---|---|---|
| 共享目录 | `<共享目录>/<配对ID>/` | 双方都直接读写本地路径（`DirMailbox`） |
| SSH，本节点是连接方 | 被连方的 `~/.reins/link/mailboxes/<配对ID>/` | 通过 `ssh 主机 reins _link-box`（`SshMailbox`） |
| SSH，本节点是被连方 | 自己的 `~/.reins/link/mailboxes/<配对ID>/` | 直接读写本地路径（`DirMailbox`） |

两种实现提供同一组操作，`batch` 让 SSH 一次往返完成整轮：

| 操作 | 作用 |
|---|---|
| `put(格子, 名字, 内容)` | 原子写入：先写 `.tmp-` 开头的临时名，再改名；同名同内容视为已存在 |
| `list(格子)` | 列出文件，忽略 `.tmp-` 开头的 |
| `get(格子, 名字)` | 读一个文件 |
| `remove(格子, 名字列表)` | 删除已取走的文件；不存在不算错 |
| `wait(格子, 超时)` | 格子里有文件就返回，最多等到超时；在邮箱所在的机器上轮询 |
| `batch(操作列表)` | 按顺序执行多步，一次返回全部结果 |

共享目录上的规则，都是 JuiceFS 上吃过的亏：

- 只依赖“同一目录内的改名是原子的”。不用跨机器的锁，不用 SQLite。
- 不用 inotify（看不到别的机器写入），在邮箱所在的机器上轮询。
- 不用文件的 mtime 判断任何事。过期从接收方看到文件时算起。
- 每个文件只有一个写入者：写入方只新建，读取方只删除。

### 9.2 邮箱里的文件

```json
{"schema": "reins.link/1", "kind": "message", "pair": "p-…", "from": "n-…", "to": "n-…",
 "body": {}, "mac": "…"}
```

| `kind` | 文件名 | `body` |
|---|---|---|
| `message` | `m-<消息ID>.json` | 一条 `reins.message/1`，不含 `local` |
| `receipt` | `r-<消息ID>-<状态>.json` | `{"id", "state", "code", "reason"}`：`forwarded`、`rejected`、`published`、`acked`、`failed`、`unknown`、`expired`、`canceled` |
| `names` | `n-<序号>.json` | 公开的会话名和 Harness 类型；新的取代旧的 |
| `bye` | `b-<序号>.json` | 解除配对 |

`mac` 是配对密钥对其余字段的规范 JSON（键排序、无空白）的 HMAC-SHA256。`pair`、`from`、`to` 和配对记录不符，或者验签失败，文件移到 `quarantine/` 并告警，不导入。单个文件上限 1 MiB。

### 9.3 握手：`reins link connect`

**SSH：** `reins link connect hub`（`hub` 是 `~/.ssh/config` 里的别名或 `user@host`）。

1. 连接方运行 `ssh hub sh -c '<找到 reins> _link-box --hello'`，在 stdin 送 `hello`：协议版本、节点 ID、建议的别名（主机名）、能力清单。远端优先用 `~/.reins/bin/reins`，没有再用 `PATH` 上的 `reins`。
2. 被连方生成配对 ID 和 32 字节配对密钥，建好 `mailboxes/<配对ID>/`，保存配对记录，回 `welcome`：自己的节点 ID、建议的别名、商定的版本和能力、配对密钥。SSH 本身已经证明连接方有这个账号，所以不另要配对码。
3. 连接方保存配对记录，角色为搬运方。

**共享目录：** 任何能写这个目录的人都能放文件，所以要一次性配对码证明双方身份。

```bash
reins link connect --dir /mnt/shared/reins-link              # A：打印配对码，进入等待
reins link connect --dir /mnt/shared/reins-link --code <码>  # B
```

1. A 生成配对 ID 和配对密钥，在 `<目录>/<配对ID>/hello/offer.json` 写下签名的 `hello`，打印配对码（配对 ID 加密钥）。配对码由人通过可信渠道传给 B。
2. B 用配对码里的密钥验 `offer.json`，写签名的 `hello/accept.json`，保存配对记录。
3. A 的 `_link-sync` 验过 `accept.json` 后把配对记录从等待改为可用，删掉 `hello/`。

**商定的内容：** 双方节点 ID、各自给对方的别名（默认取对方主机名，`--name` 可改）、共同的最高协议版本、能力的交集（第 3 版只有 `message`、`receipt`、`names`）、配对密钥。`wake` 不商定，它只是接收方自己的设置。

**重新握手：** 已经配对的机器不能直接再 `connect`，要先 `reins link remove`，再 `connect`，换新密钥。对方那边如果还留着旧配对（比如解除时没能通知到它），新的握手会替换它，规则和解除配对一样：在路上的消息不投递，没送出的标成 `failed`。协议版本不兼容时，`_link-sync` 停止搬运，`reins link list` 显示两边的版本，消息留在发件箱。

**解除配对：** `reins link remove hub` 先删本地配对记录（之后本节点不再发任何文件，发件箱里没送出的消息记为 `failed`），再通知对方。共享目录和 SSH 搬运方往邮箱放一个签名的 `bye`，对方收到后也删除它的配对记录。SSH 被连方直接删掉邮箱：搬运方下次连上时得到 `unknown_peer`，就把自己这边的配对标成 `gone` 并留下告警，所以没有会丢的 `bye`。不管哪种，塔台都先把已经取回的文件导入完，再删除配对记录。已收到的消息都不受影响；解除配对那一刻还在路上的消息不投递，发送方那边记为 `failed`。

SSH 加固（可选）：在被连方的 `authorized_keys` 里写 `restrict,command="~/.reins/bin/reins _link-box --pair p-…"`，这把钥匙就只能操作这一个邮箱，也不能重新握手。不受限的钥匙等于完整 shell。

### 9.4 搬运与导入

```text
A reins send ─▶ sent/ + outbox/hub/ ─▶ A _link-sync ─▶ 邮箱 to-<B>/ ─▶ B _link-sync ─▶ B inbound/a/
                                                                                       │ B 塔台：准入
A sent/ 状态 ◀── A 塔台 ◀── A inbound/hub/ ◀── 邮箱 to-<A>/ ◀── B outbox/a/ ◀── 回执 ──┘
```

- **`_link-sync`：** 塔台在每轮循环里确认每个对端都有一个 `_link-sync` 在跑，由 `link-sync-<别名>` 锁保证只有一个。它做两件事：
  - 发：发件箱有文件就 `put` 进对方的格子，成功后删本地文件。每秒看一次本地发件箱。
  - 收：对自己的格子 `wait`（最多 50 秒），有文件就 `get`，验签后写进 `inbound/<别名>/`，下一轮再从邮箱 `remove`。先落盘再删，崩溃只会导致重复，不会丢。
  - 失败时指数退避，初始 2 秒，上限 5 分钟。错误写进配对记录的 `last_error`，`reins link list` 能看到。
- **导入：** 塔台对 `inbound/` 里的消息调用和 `reins send` 同一个准入函数，限额按 `名字@别名` 计。通过就写消息记录和回调，回一个 `forwarded` 回执；不通过回 `rejected` 并附错误码。处理完删掉 `inbound/` 里的文件。
- **去重：** 本地根是 `m-` + `sha256(from.node + ":" + id)` 的前 16 位。本地根已存在就是重复：再回一次 `forwarded`，不产生第二条回调。
- **回执：** 塔台发现对端消息的状态变了（`published`、`acked`、`failed`……），就往发件箱放一个回执。发送方只接受让状态前进的回执：`queued < forwarded < published < acked`，终止状态之后不再变。
- **过期：** 发送方的消息过期时还在发件箱里，就删掉它并标成 `expired`。

**错误码：** `identity_mismatch`、`unknown_peer`、`unknown_name`、`id_conflict`、`invalid_record`、`unsupported_version`、`limit_exceeded`、`expired`。

### 9.5 安全

- 配对之后，没有配对密钥的人写进邮箱的文件验签失败，不会被导入。共享目录的弱点（同一个 uid 的所有进程都能写）只剩下拒绝服务：可以塞垃圾文件，但伪造不了消息。
- 配对密钥存在 `peers/<别名>.json`，权限 0600。它等于这条链路的身份，泄露了就重新配对。
- 配对码只用一次：A 收到 `accept.json` 后删掉 `hello/`。
- 跨机器的消息和本机消息一样可能拉起会话、消耗额度；`hop` 和频率上限照样止损，接收方也可以对某个对端设 `--wake never`。

## 10. 防止无限对话

限额由接收节点执行，因为发送方可能被攻破或者有 bug。

| 限制（`config.yaml` 里的键） | 默认值 | 超出时 |
|---|---|---|
| 连续回复层数（`hop`） | 30 | `limit_exceeded` |
| 单个对话的消息数（`thread_messages`） | 300 | 同上 |
| 单个对话每分钟消息数（`thread_per_minute`） | 20 | 同上 |
| 同一发送方每分钟消息数（`sender_per_minute`） | 120 | 同上；多机时对应对端，回 `busy` |
| 同一收发对每小时消息数（`pair_per_hour`） | 120 | 同上。防止 Agent 不回复、每次开新对话来绕过 `hop` |
| 待投递消息总数（`pending_messages`） | 1000 | 同上；多机时回 `busy` |
| 消息保留天数（`ttl_days`） | 7 | 未读的变成 `expired` |
| 已结束消息的清理天数（`retention_days`） | 30 | 删除消息记录和正文 |

拒绝时发送方的命令以退出码 3 结束，并让 Agent 停下告诉用户。告警出现在 `reins status`、`reins message alerts` 和 `reins doctor`，不会再发给任何 Agent，避免错误提示本身成为下一轮对话。`reins doctor` 之外的命令不会把它附到 Agent 的输出里。

配置示例，写在 `~/.reins/link/config.yaml`：

```yaml
limits:
  hop: 60
  pair_per_hour: 240
```

## 11. 远程启动 Agent（最后一阶段）

```bash
reins agent --host gpu1 expert reviewer --name r1 -- "审查这个分支"
```

- 需要对端权限 `spawn`，默认没有。
- 只允许启动接收节点 配对记录里为这个对端列出的专家，不接受任意命令行。
- 沙箱和权限模式由接收节点决定，请求最多只能收紧，不能放宽。
- 不转发环境变量、凭证和本机路径。
- 结果沿现有回调回到发起会话，`target` 的地址写法同第 4.2 节。

## 12. PR 拆分

每个 PR 必须能独立合并，且不影响未启用 Link 的用户。

| PR | 内容 | 依赖 | 验收 |
|---|---|---|---|
| 0 | 本文和威胁模型、贡献规则 | 无 | 已完成 |
| 1 | `core` 记录校验，`store` 布局，会话注册，`reins send`、`reins inbox`，限额（单机） | 0 | 已在 0.11.0 发布 |
| 2a/2b/2c | Codex、Claude、Pi 的接收 | 1 | 已在 0.11.0 发布，真实 Harness 结果见第 12.2 节 |
| 3 | 邮箱抽象、共享目录传输、握手、`_link-sync`、导入、回执、`reins link connect/list/remove`、`name@别名` | 1 | N 组，用两个隔离的 `REINS_HOME` 和一个共享目录 |
| 4 | SSH 传输（`_link-box`、`SshMailbox`） | 3 | N 组，跑在真实 sshd 上，包括只有单向能连 |
| 5 | `--host` 远程启动 | 4，且需要专门的安全评审 | S 组和第 11 节的每一条 |

### 12.1 验收矩阵

**L：单机消息**

| 编号 | 场景 | 期望 |
|---|---|---|
| L1 | 三种 Harness 两两互发 | 都能收到并 ACK，发送方看到 `acked` |
| L2 | 接收会话忙碌 | 不丢不重，空闲后可见 |
| L3 | 接收会话关闭，`wake: never` | 不启动新进程，重新打开后可见 |
| L3a | Claude Code 或托管 Pi 会话关闭，默认 `wake` | 恢复会话并投递一次 |
| L3b | Claude Code 窗口开着 | 不恢复，经钩子、频道或 `reins wait` 投递；窗口关闭后才恢复 |
| L3c | Claude Code 无头恢复运行中 | 不开第二个进程，经流式输入送进这个进程；进程退出后未确认的立即重试 |
| L4 | 名字不存在 | `unknown_name`，不尝试任何其他会话 |
| L5 | 两个 Agent 自动互相回复 | 在 `hop` 上限处停下，出现人工告警 |
| L6 | 正文含 `ACK after inspection:` 和仿造的 `[reins]` 行 | 信封不受影响，ACK 命令仍由接收节点生成 |
| L7 | 塔台重启 | 不丢消息，不重复投递 |

**N：跨节点**

| 编号 | 场景 | 期望 |
|---|---|---|
| N1 | 正常发送 | `queued → forwarded → published → acked` |
| N2 | 搬运在 `put` 前、`put` 后删发件箱前、取回后 `remove` 前分别中断 | 最终恰好一条回调 |
| N3 | 塔台在导入时崩溃 | 重启后恰好一条回调 |
| N4 | 重放旧文件 | 回 `forwarded`，不产生第二条回调 |
| N5 | 相同 ID、不同内容 | `id_conflict` |
| N6 | 对端长时间离线后恢复 | 未过期的消息送达，已过期的变成 `expired` |
| N7 | 两边版本不兼容 | 有明确提示，消息保留 |
| N8 | 塔台离线时收到消息 | `_link-sync` 落盘，塔台恢复后处理 |
| N9 | 自称别的 `from.node`，或没有配对密钥的文件 | 进 `quarantine/`，有告警，不导入 |
| N11 | SSH 只有单向能连 | 两个方向都送达 |
| N12 | 回复的 `hop` 被改小 | `invalid_record` |
| N10 | 两个对端使用相同的 `id` | 成为两条互不影响的消息 |

**S：安全**

见 [link-threat-model.md](link-threat-model.md) 的第 6 节。

### 12.2 单机验收结果（0.11.0）

Linux，Codex CLI 0.166.0 和 Pi 0.87.1（模型 Kimi K3），Claude Code 2.1.314。Codex 运行在 `workspace-write` 沙箱里。自动化测试在 `tests/test_messages.py`。

| 编号 | 结果 | 怎么验证的 |
|---|---|---|
| L1 | 通过 | 三种 Harness 两两真实互发并回复 |
| L2 | 通过 | 接收方正在跑 `sleep 40` 时发送，Pi 和 Codex 各收到一次 |
| L3 | 通过 | 关闭 Codex 和 Pi 后发送，没有新进程；重新打开后收到并回复。真实 Claude 会话关闭后消息留在收件箱，没有启动进程 |
| L4 | 通过 | 自动化测试 |
| L5 | 通过 | Codex 和 Pi 真实互相回复，`hop` 设为 3，第 4 跳被拒，出现告警 |
| L6 | 通过 | 自动化测试 |
| L7 | 通过 | 自动化测试，包括两步写入之间崩溃 |

`--wake resume` 在真实 Codex 和 Claude 的已关闭会话上验证过。没有验证：macOS 和 Windows 上的真实 Harness（只跑 CI 的自动化测试）、Codex Desktop、`reins claude` 频道收消息（有自动化测试，没有真实会话）。

## 13. 已定事项

| # | 问题 | 决定 | 备注 |
|---|---|---|---|
| 1 | `wake` 默认值 | Claude Code 和托管 Pi 用 `resume`，其余 `never`，本机和跨机器相同 | 维护者决定：消息优先可用，和任务结果一致；跨机器也要能无人值守。接收方可对某个对端关掉 |
| 2 | 第 10 节的默认值 | 先这样，已可配置 | 按真实使用调整 |
| 3 | 链路身份 | 配对密钥加 HMAC，标准库就够 | 第 3 版取消 TLS 证书，不需要 `cryptography` |
| 4 | Windows 的多机支持 | 共享目录可用；SSH 需要 Windows 上的 OpenSSH 客户端，被连方暂不支持 | 不再需要本机 socket |
| 5 | 协议与威胁模型的语言 | 中文为准 | 贡献者需要时再出英文版 |
| 6 | 第 5、6 号 PR 的安全评审 | 维护者加一名有 TLS 和身份认证经验的外部评审者 | 合并前必须有 |
