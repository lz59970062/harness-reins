# Reins Link 协议

状态：第 2 版，2026-11-02。单机部分（第 12 节的 PR 0、1、2a、2b、2c）已在 0.11.0 发布，多机部分按第 1.1 节的方向实现。第 13 节的决定已定。

安全边界见 [link-threat-model.md](link-threat-model.md)。记录格式、ID 和能力清单的通用规则沿用 [protocol-v1.md](protocol-v1.md)，本文只写新增和不同的部分。

## 1. 目标

让运行中的 Codex、Claude Code、Pi 会话互相发消息，不论它们在同一台机器还是不同机器。

**包含：**

- 用名字寻址任意已注册的会话
- 持久化的消息，不因断线、重启或会话关闭而丢失
- 跨机器传输，同一个服务支持本机 socket、SSH 和带鉴权的 TLS 端口
- 防止 Agent 之间无限对话

**不包含：**

- 文件和产物同步。消息只传文字和引用（第 6.3 节）
- 流式输出、token 级实时共享
- 多用户组织、中心账号系统
- 跨机器的共享任务调度。`--host` 只负责在指定机器上启动（第 11 节）

### 1.1 多机方向（已定）

这几条是多机工作的基调。要改其中任何一条，先改本文。

1. **长期形态是带鉴权的服务端口。** 每台机器一个 Link 服务，监听 mTLS 端口，用固定证书指纹识别对端。多台机器之间实时互通靠它。
2. **SSH 不是另一套方案，是同一协议的另一条传输。** 帧、去重、状态完全相同。SSH 不用开新端口，认证也现成，所以先落地，用来打通协议和测试；已有 SSH 的小规模场景也可以一直用它。
3. **实现顺序：** Link 服务和本机 socket（PR 3）→ SSH（PR 4）→ TLS 和配对（PR 5）→ 远程启动（PR 6）。前一步的 N 组验收没过，不合并下一步。
4. **塔台不碰网络。** 网络进来的消息落到 `inbound/`，塔台调用的准入逻辑和 `reins send` 是同一份，限额也是同一份。投递引擎不知道消息从哪来。
5. **限额和权限由接收方执行。** 发送方的声明一律不作为依据。
6. **默认最小权限。** Link 服务默认关闭，不监听任何地址；新对端只有 `message` 权限；`spawn` 默认没有；`wake` 默认 `never`；不提供明文 TCP，也不提供关闭证书校验的开关。
7. **多机之前绝不猜。** `reviewer@gpu1` 现在就是 `unknown_peer` 错误，不会退回到本机同名会话。

## 2. 五条不变量

所有实现和评审都以这五条为准。任何 PR 违反其中一条，都要先改本文，不能在代码里破例。

1. **传输与消息分离。** 帧协议不知道消息怎么投递，投递引擎不知道消息来自 socket、SSH 还是 TLS。
2. **写入者明确。** 每类文件的写入者和锁见第 8 节。Link 服务不写 `queue/**`。
3. **身份来自传输，不来自内容。** 对端是谁由 socket 凭证、受限 SSH 密钥或 TLS 证书决定。`hello` 和消息里自称的身份不能作为依据。
4. **绝不猜测目标。** 名字解析失败就拒绝，不退回“最近的会话”。沿用 protocol-v1 的显式指定原则。
5. **未确认的结果不等于成功。** 每个状态只表示它定义的事实（第 7 节），不能把“已发出”说成“已读”。

## 3. 概念

| 名词 | 含义 |
|---|---|
| 节点（node） | 一台机器上一个 OS 账号的一份 Reins，由 `~/.reins` 确定 |
| 对端（peer） | 本节点配置过的另一个节点 |
| 会话名（session name） | 注册到本节点的逻辑名字，对应一个具体的 Harness 会话 |
| 消息（message） | 一条从会话发往会话的持久记录，一个新的根 |
| 对话（thread） | 一串有回复关系的消息 |
| Link 服务 | 负责跨节点收发帧的独立进程，默认关闭 |

## 4. 寻址与注册

### 4.1 节点身份

节点首次启用 Link 时生成 `node_id`，格式 `n-` + 16 位十六进制，此后不变。人记的是对端别名，例如 `gpu1`。别名只在本节点有效，不通过网络传播。

### 4.2 地址

```text
reviewer           本节点的 reviewer
reviewer@gpu1      对端 gpu1 上的 reviewer
```

名字符合 `[a-z0-9][a-z0-9_-]{0,31}`。

发送方不需要知道对方的 Harness、会话 ID 或工作目录。这些只保存在目标节点上。

记录里的 target 使用 protocol-v1 预留的 `session`（不同于 origin）语义，并新增两种写法：

```json
{"kind": "session_name", "name": "reviewer"}
{"kind": "session_name", "name": "reviewer", "node": "n-3f9a1c0d7b2e4a65"}
```

### 4.3 注册

```bash
reins session register reviewer          # 绑定当前会话，只有本节点可见
reins session register reviewer --share  # 同时对已配置的对端可见（多机后）
reins session list [--peer gpu1]         # --peer 在多机后提供
reins session unregister reviewer
```

- 绑定沿用现有的环境变量（`CODEX_THREAD_ID`、`CLAUDE_CODE_SESSION_ID`、`PI_SESSION_FILE`），识别不到就拒绝。
- 名字已被其他会话占用时拒绝，除非显式加 `--replace`。一个会话只有一个名字，`--replace` 也用于改名。
- `shell` 是保留字：不在任何会话里发出的消息，寄件方显示为 `shell`，不能回复。在会话里发消息必须先注册，否则对方没法回。
- 注册时冻结投递所需的路由和环境（Codex 可执行文件和 profile、Pi 频道目录），因为这些只有接收方自己的环境才知道。
- 对端能看到的只有：名字、Harness 类型、是否在线，不包括工作目录、会话 ID、环境。
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
- **`from.node`：** 接收节点用连接的已验证身份覆盖这个字段。不一致时拒绝，错误码 `identity_mismatch`。
- **`hop`：** 新对话为 0，回复时由 Reins 设为父消息的 `hop + 1`，调用方不能指定。
- **`expires_at`：** 默认 7 天。依赖发送方时钟，只用于清理和提示，不用于安全判断。
- **未知字段：** 原样保留，必需字段缺失则拒绝。`requires` 有不认识的能力时拒绝并报告，不会投递一半。
- **大小限制：** `subject` 最多 200 字符且不能含换行、控制字符或 `[reins`、`ACK after` 这类信封标记；`body` 最多 64 KiB；`refs` 最多 20 项。没给 `subject` 时取正文第一行，那一行不合格就用 `Message from <名字>`。

## 6. 投递语义

### 6.1 走现有投递链路

准入时写入一条 `reins.callback/1`，`root` 是消息 ID。重试、目标锁、ACK、回调提醒都复用现有逻辑，不写第二套。回调的 `requires` 由实际路由决定，例如 `deliver.codex-queue`。

准入只有一个函数：本机 `reins send` 在命令进程里调用它，多机后塔台对 `inbound/` 调用同一个函数。这样 `reins send` 能当场给出 `unknown_name`、`limit_exceeded`，塔台没运行时消息也不丢。

### 6.2 `wake`：是否唤醒已关闭的会话

`wake` 默认 `never`。把两个关闭的会话互相唤醒会持续消耗额度，所以这必须是显式选择。

| 取值 | 行为 |
|---|---|
| `never`（默认） | 只走不启动新进程的通道；不可用时等会话下次打开 |
| `resume` | 允许用无头方式恢复已关闭的会话，受第 10 节的预算限制，超出后降级为 `never` 并在正文前注明 |

三种 Harness 在 `never` 下的行为（0.11.0 用真实 Harness 验证过，见第 12.2 节）：

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

发送方看到的状态由回调的状态推导，不另存一份，所以不会出现两边不一致。终止状态标 ●。

| 状态 | 含义 | 由谁确认 |
|---|---|---|
| `queued` | 已持久化到本节点 | 本节点塔台 |
| `forwarded` | 目标节点已落盘并回了 `accepted`。本节点内的消息跳过这一步 | 对端 Link |
| `published` | 已交给目标 Harness 的通道，例如 `codex queue` 已接受 | 目标节点塔台 |
| `acked` ● | 接收会话运行了 `reins ack`，表示收到并看过 | 接收会话 |
| `rejected` ● | 对端明确拒绝，错误码见第 9.4 节 | 对端 |
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
  messages/<本地根>.json       消息记录，写入后不再改（0.11.0）
  threads/<对话>.json          对话计数（0.11.0）
  rates.json                   发送方和收发对的计数（0.11.0）
  alerts/<摘要>.json           给人看的限额告警（0.11.0）
  identity/                    节点私钥（多机）
  peers.yaml                   对端、权限、证书指纹（多机）
  link.sock                    本机接入点（多机）
  outbox/<消息ID>.json         等待发往对端（多机）
  sent/<消息ID>.json           对端的回执（多机）
  inbound/<节点ID>/<消息ID>.json  对端发来且已落盘的消息（多机）
  seen/<节点ID>/<消息ID>       去重标记（多机）
```

| 文件 | 写入者 |
|---|---|
| `sessions/`、`messages/`、`threads/`、`rates.json`、`alerts/` | 做准入或注册的进程，持 `link` 锁：`reins session`、`reins send`，以及多机后的塔台 |
| `queue/pending/<回调ID>.json` | 同上，和 `reins done` 一样持久化后交给塔台 |
| `queue/**` 的其余部分 | 塔台和 `reins ack` 等已有写入者（不变） |
| `outbox/` | 塔台（多机） |
| `sent/`、`inbound/`、`seen/` | Link 服务（多机） |

准入先写消息记录，再写回调。进程在两步之间崩溃时，塔台发现消息没有回调，会按记录补上，回调 ID 是固定的，不会重复。

Link 服务不读写 `queue/**`，只读 `messages/` 来发送状态帧，不导入 `engine` 或 `tower`。塔台离线时，Link 仍可接收并落盘，塔台恢复后再处理。全部写入使用 `system/files.py` 的原子写入。

**分层：** 新增 `harness_reins/link/`，依赖限于 `core`、`system`、`store`。`tests/test_architecture.py` 的 `ALLOWED` 要同步加上，不能进入 `KNOWN_VIOLATIONS`。

## 9. Link 帧协议 `reins.link/1`

### 9.1 传输

帧协议只要求一条可靠的双向字节流：

| 接入方式 | 身份来源 | 平台 |
|---|---|---|
| 本机 socket：`link.sock` | 操作系统提供的对端 uid，必须等于本账号 | Linux、macOS |
| SSH：`ssh host reins _link-stdio`，由它桥接到 `link.sock` | 受限密钥（推荐）：`restrict,command="reins _link-stdio --peer NAME"` 把身份固定在服务端。未受限的密钥已有完整 shell，等同本账号，可声明 `peers.yaml` 里的任一对端 | Linux、macOS |
| TLS：`reins link serve --listen ADDR` | mTLS 客户端证书，匹配 `peers.yaml` 里固定的指纹 | Linux、macOS；Windows 待定 |

Windows 没有稳定可用的 Python Unix socket，本机接入点单独设计（第 13 节）。

### 9.2 帧

每帧是 4 字节大端长度加 UTF-8 JSON，单帧上限 1 MiB。超限或解析失败时返回错误并关闭连接。

```json
{"schema": "reins.link/1", "type": "send", "seq": 12, "body": {}}
```

| `type` | 方向 | 作用 |
|---|---|---|
| `hello` | 双向 | 版本范围、能力清单、Reins 版本 |
| `send` | 发送方 → 接收方 | 携带一条 `reins.message/1` |
| `accepted` | 接收方 → 发送方 | 消息已落盘，或此前已收到（重复） |
| `rejected` | 接收方 → 发送方 | 明确拒绝，发送方不再重试 |
| `busy` | 接收方 → 发送方 | 暂时不收，带 `retry_after_seconds` |
| `status` | 接收方 → 发送方 | `published`、`acked`、`failed`、`unknown` 等状态变化 |
| `directory` | 双向 | 请求或返回 `--share` 的会话列表 |
| `ping`/`pong` | 双向 | 保活 |
| `spawn` | 发送方 → 接收方 | 仅第 11 节，默认不可用 |

`seq` 只用于匹配请求和回复，不表示消息顺序。

### 9.3 握手与版本

1. 传输层先完成身份验证。验证失败的连接读不到任何帧。
2. 双方发送 `hello`：`{"versions": [1], "capabilities": ["message"], "reins_version": "0.11.0"}`。
3. 取共同的最高版本。没有共同版本时返回 `unsupported_version` 并关闭，消息留在发件箱，`reins link status` 显示原因和两边的版本。

能力清单是双方都支持、且这个连接被授权使用的交集。`hello` 不能扩大已配置的权限。

### 9.4 发送流程

```text
A CLI        A 塔台        A Link        B Link        B 塔台       B 会话
  │ submit/ ───▶│             │             │             │            │
  │             │ outbox/ ───▶│── send ────▶│             │            │
  │             │             │             │ seen+inbound│            │
  │             │◀── sent/ ───│◀─ accepted ─│────────────▶│            │
  │             │             │             │             │ callback ─▶│
  │             │◀── sent/ ───│◀─ status ───│◀────────────│◀─── ack ───│
```

接收方的步骤必须按这个顺序：

1. 校验身份、权限、限额、记录格式。
2. 按 `(节点, ID)` 串行处理。查 `seen/<节点>/<ID>`：已存在且摘要相同就回 `accepted`，摘要不同就回 `rejected: id_conflict`。
3. 原子写入 `inbound/<节点>/<ID>.json`。
4. 原子写入 `seen/<节点>/<ID>`，内容是消息摘要。
5. 回 `accepted`。

第 3、4 步之间崩溃时，`seen` 还不存在，发送方重发后会再写一次 `inbound/`。塔台读到 `messages/<本地根>.json` 已存在时直接丢弃，所以最终只有一条回调。

发送方的重试：指数退避，初始 2 秒、上限 5 分钟，直到 `accepted`、`rejected` 或过期。

`status` 帧也可能丢失或重复。接收节点在重连后重发尚未确认的状态变化，发送方只接受能让状态前进的更新。

`seen` 标记至少保留到消息过期后 7 天。

**错误码：** `identity_mismatch`、`permission_denied`、`unknown_name`、`id_conflict`、`invalid_record`、`unsupported_version`、`unsupported_capability`、`limit_exceeded`、`quota_exceeded`、`expired`。

### 9.5 配对（TLS）

配对码只用一次，由人通过可信渠道传递，例如已有的 SSH 或密码管理器。

```bash
reins link invite --peer gpu1 --address 100.64.0.2:7443   # 在 A 上运行，打印配对码
reins link join CODE --peer hub                           # 在 B 上运行
```

1. 配对码包含 A 的地址、A 的证书指纹和一个至少 128 位的随机秘密。
2. B 连接 A，并验证 A 的证书指纹与配对码一致。
3. B 发送 `HMAC(秘密, B 的证书指纹)` 和 B 的证书指纹。
4. A 校验后固定 B 的指纹，默认只授予 `message` 权限，配对码立即作废。

证书自签，靠固定指纹识别，不需要 CA。吊销就是从 `peers.yaml` 删除指纹。

其他管理命令：`reins link serve`、`reins link status`、`reins link peer list|remove`、`reins link rotate-identity`。

## 10. 防止无限对话

限额由接收节点执行，因为发送方可能被攻破或者有 bug。

| 限制（`config.yaml` 里的键） | 默认值 | 超出时 |
|---|---|---|
| 连续回复层数（`hop`） | 8 | `limit_exceeded` |
| 单个对话的消息数（`thread_messages`） | 100 | 同上 |
| 单个对话每分钟消息数（`thread_per_minute`） | 20 | 同上 |
| 同一发送方每分钟消息数（`sender_per_minute`） | 120 | 同上；多机时对应对端，回 `busy` |
| 同一收发对每小时消息数（`pair_per_hour`） | 60 | 同上。防止 Agent 不回复、每次开新对话来绕过 `hop` |
| 待投递消息总数（`pending_messages`） | 1000 | 同上；多机时回 `busy` |
| 单个对话的 `wake: resume` 次数（`thread_resumes`） | 3 | 降级为 `never` 并在正文前注明 |
| 消息保留天数（`ttl_days`） | 7 | 未读的变成 `expired` |
| 已结束消息的清理天数（`retention_days`） | 30 | 删除消息记录和正文 |

拒绝时发送方的命令以退出码 3 结束，并让 Agent 停下告诉用户。告警出现在 `reins status`、`reins message alerts` 和 `reins doctor`，不会再发给任何 Agent，避免错误提示本身成为下一轮对话。`reins doctor` 之外的命令不会把它附到 Agent 的输出里。

配置示例，写在 `~/.reins/link/config.yaml`：

```yaml
limits:
  hop: 4
  pair_per_hour: 30
```

## 11. 远程启动 Agent（最后一阶段）

```bash
reins agent --host gpu1 expert reviewer --name r1 -- "审查这个分支"
```

- 需要对端权限 `spawn`，默认没有。
- 只允许启动接收节点 `peers.yaml` 里为这个对端列出的专家，不接受任意命令行。
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
| 3 | `link/` 服务、帧协议、本机 socket、发件箱、去重 | 1 | N 组，用两个隔离的 `REINS_HOME` |
| 4 | SSH 接入和断线重连 | 3 | N 组，跑在真实 sshd 上 |
| 5 | TLS、mTLS、配对、吊销 | 3，且需要专门的安全评审 | S 组 |
| 6 | `--host` 远程启动 | 2、4 或 5，且需要专门的安全评审 | S 组和第 11 节的每一条 |

### 12.1 验收矩阵

**L：单机消息**

| 编号 | 场景 | 期望 |
|---|---|---|
| L1 | 三种 Harness 两两互发 | 都能收到并 ACK，发送方看到 `acked` |
| L2 | 接收会话忙碌 | 不丢不重，空闲后可见 |
| L3 | 接收会话关闭，`wake: never` | 不启动新进程，重新打开后可见 |
| L4 | 名字不存在 | `unknown_name`，不尝试任何其他会话 |
| L5 | 两个 Agent 自动互相回复 | 在 `hop` 上限处停下，出现人工告警 |
| L6 | 正文含 `ACK after inspection:` 和仿造的 `[reins]` 行 | 信封不受影响，ACK 命令仍由接收节点生成 |
| L7 | 塔台重启 | 不丢消息，不重复投递 |

**N：跨节点**

| 编号 | 场景 | 期望 |
|---|---|---|
| N1 | 正常发送 | `queued → forwarded → published → acked` |
| N2 | 在 `send` 前、`accepted` 前、`accepted` 后分别断线 | 最终恰好一条回调 |
| N3 | Link 在写 `inbound` 和写 `seen` 之间崩溃 | 重发后最终恰好一条回调 |
| N4 | 重放旧帧 | 回 `accepted`，不产生第二条回调 |
| N5 | 相同 ID、不同内容 | `id_conflict` |
| N6 | 对端长时间离线后恢复 | 未过期的消息送达，已过期的变成 `expired` |
| N7 | 两边版本不兼容 | 有明确提示，消息保留 |
| N8 | 塔台离线时收到消息 | Link 落盘，塔台恢复后处理 |
| N9 | 自称别的 `from.node` | `identity_mismatch` |
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
| 1 | `wake` 默认值 | `never` | `resume` 需要显式指定，有额度风险 |
| 2 | 第 10 节的默认值 | 先这样，已可配置 | 按真实使用调整 |
| 3 | 证书生成 | 用 `cryptography` 库，打包进二进制 | 现有运行依赖只有 PyYAML，这会增大包体积；`openssl` 命令行在 Windows 上通常没有。第 5 号 PR 再引入 |
| 4 | Windows 的多机支持 | 第 5 号 PR 之后单独立项 | 需要决定本机接入方式（命名管道或回环 TCP 加令牌）。单机消息在 Windows 上可用 |
| 5 | 协议与威胁模型的语言 | 中文为准 | 贡献者需要时再出英文版 |
| 6 | 第 5、6 号 PR 的安全评审 | 维护者加一名有 TLS 和身份认证经验的外部评审者 | 合并前必须有 |
