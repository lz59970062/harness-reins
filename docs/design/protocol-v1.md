# Reins 回调协议 v1（设计草案）

状态：草案第 2 版，2026-10-13。已拍板的决定见第 9 节。评审通过后，改名提交（第 3 步）按本文落地。

命名说明（2026-10-25）：本协议在 0.8 版以 ATC（Agent Traffic Control）的名字落地，记录前缀是 `atc.`。0.9 版改名为 Harness Reins（命令 `reins`），前缀随之改为 `reins.`，结构不变；旧前缀的记录由 `reins migrate` 存档，不再读取。下文按新名字书写。

## 1. 为什么要在改名时定协议

Reins 对外的契约有四样东西：模型看到的回调信封、磁盘上的回调记录、任务结果、ACK 记录。
改名一定会碰到它们。如果这次只换名字，以后加 `--notify`、专家档案、汇合时再改结构，就要破坏两次兼容。
所以 v1 一次把格式定下来，后续功能需要的字段先预留，值可以为空，格式不再变。

Reins 的核心原则是**显式指定**：指定谁来做事（哪个 Harness、哪个模型、哪个专家），指定回调给谁（哪个会话）。
任何"猜一个目标"的行为都和这条原则冲突。

## 2. 现状和问题

| 问题 | 位置 | 后果 |
|---|---|---|
| `version` 整数被拿来做功能开关：路由语义变了就从 1 升到 2、3、4 | `cli.py` 的 `make_request` | 版本号既表示格式又表示功能，两个独立功能无法同时表达，只能继续往上加数字 |
| 结果只有退出码加一段自由文本 | `agent_result_path`，Codex 用 `-o` 写文件，Claude 和 Pi 截取 stdout | 接收方必须读日志或长文本才能接着干，跨 Agent 交接没有结构 |
| "进程结束"和"任务成功"混在 `outcome: completed` 里 | `managed_callback_request` | 退出码非零也是 `completed`；子代理说"做完了"和 Reins 观察到的结果没有区分 |
| 发起方和回调目标是同一个字段 | `target` | 以后 A 派任务、通知 B 时没有地方表达 |
| 子代理自己的会话 ID 没有记录 | Pi 子代理用 `--no-session`；Codex、Claude 的子代理有会话，但 Reins 没记下 ID | 子代理做完后没法再"追问"它，只能重新起一个 |
| 任务 ID 和回调 ID 互不相关；回调 ID 是 32 位十六进制 | `submit_managed_run`、`make_request` | 看回调 ID 不知道属于哪个任务；ACK 命令还带着完整的 `--queue-dir` 路径 |
| 时间戳是浮点秒数 | 各记录的 `created_at` 等 | Agent 读 JSON 时看不懂，排查要换算 |
| `--last` 目标 | `routing_text` | 可能把回调投给无关会话，违反"显式指定"原则 |

## 3. 主流 Agent 架构调研

协议要能覆盖主流架构，所以先看它们各自怎么启动、怎么恢复、怎么接收外部消息。

### 3.1 四类架构

| 类别 | 代表 | 会话怎么标识 | Reins 怎么接入 |
|---|---|---|---|
| **终端 Harness** | Codex、Claude Code、Pi、Gemini CLI、Qwen Code、OpenCode、Cursor CLI、Copilot CLI、Goose、Amp、Kimi CLI | 会话 ID（UUID、文件路径、`T-<uuid>`、名字等，各家不同） | 无头模式启动子代理；按 ID 恢复会话来投递回调；部分支持向运行中的会话注入消息 |
| **编辑器协议 ACP** | Zed 发起的 Agent Client Protocol；Gemini CLI、Kimi CLI、Goose、OpenCode 原生支持，Claude Code、Codex、Pi 有适配器 | `session/new` 返回的会话 ID | stdin/stdout 上的 JSON-RPC：`session/new`、`session/prompt`、`session/load`。Reins 作为客户端时，一个适配器就能驱动所有支持 ACP 的 Agent 当子代理 |
| **SDK 框架** | LangGraph、OpenAI Agents SDK、Claude Agent SDK、Google ADK | 应用自己的线程 ID，例如 LangGraph 的 `thread_id` | 没有终端会话可恢复。LangGraph 的做法是 `interrupt()` 暂停，之后用同一个 `thread_id` 加 `Command(resume=…)` 继续。Reins 只能通过"执行一条命令"或"调用一个 HTTP 地址"把结果交给应用 |
| **远程 Agent 协议 A2A** | Google 发起的 Agent2Agent | 任务 ID，状态为 submitted、working、input-required、completed、failed、canceled 等 | 长任务用 webhook 推送状态变化，但**规范不保证送达和重试**，客户端要自己轮询兜底 |

### 3.2 终端 Harness 的能力

Reins 需要的六项能力：

- **绑定**：shell 工具里能读到当前会话 ID，用来记录"谁发起的"
- **启动**：无头模式运行一个子代理
- **结构化输出**：强制最终输出符合给定的 JSON Schema
- **冷投递**：会话没在运行时，按 ID 恢复它，送入一条消息
- **热投递**：会话正在运行时，把消息注入进去
- **预设 ID**：启动子代理时指定它的会话 ID，以后好找回

| Harness | 绑定 | 启动 | 结构化输出 | 冷投递 | 热投递 | 预设 ID |
|---|---|---|---|---|---|---|
| Codex 0.162.1 ✅ | `CODEX_THREAD_ID` | `codex exec` | `--output-schema` | `codex exec resume <id>` | app-server `thread/resume` + `turn/start`（Reins 已用于 Desktop） | 无；从 `--json` 事件里读 |
| Claude Code 2.1.296 ✅ | `CLAUDE_CODE_SESSION_ID` | `claude -p` | `--json-schema` | `claude -p --resume <id>` | Channels（MCP 推送，Reins 已用）；后台 `ltc wait` | `--session-id <uuid>` |
| Pi 0.87.1 ✅ | `PI_SESSION_FILE` / `PI_SESSION_ID` | `pi -p --mode text\|json\|rpc` | 无 | `pi -p --session <文件\|id>` | 扩展 steer（Reins 已用） | `--session-id <id>` |
| Gemini CLI 📄 | `GEMINI_SESSION_ID` 只确认在 hooks 环境里有，shell 工具里有没有待核实 | `gemini -p` | `--output-format json`，无 Schema | `--resume <id>`（第三方资料） | 未发现 | 待核实 |
| Qwen Code 📄 | 待核实 | `qwen -p` | 同 Gemini（它是 Gemini CLI 的分支） | `qwen --resume <id> -p` | 未发现 | `--session-id`（第三方资料） |
| OpenCode 📄 | 插件的 `shell.env` 钩子可以注入会话 ID（第三方资料） | `opencode run` | `--format json` | `opencode run -s <id>` | `opencode serve` 提供 HTTP API | 待核实 |
| Cursor CLI 📄 | 待核实 | `agent -p` | `--output-format json` | `agent -p --resume=<id>` | 未发现 | `agent create-chat` 先拿到 ID |
| Copilot CLI 📄 | 待核实 | `copilot -p` | 待核实 | 文档只确认非交互模式支持 `--continue` | SDK 走 JSON-RPC | 待核实 |
| Goose 📄 | 待核实 | `goose run -t` | 待核实 | `goose run --resume --session-id <id>` | 未发现 | `--session-id` / `--name` |
| Amp 📄 | 待核实 | `amp -x --stream-json` | 待核实 | `amp threads continue <T-id>` | 未发现 | 无；从首个事件读 |
| Kimi CLI 📄 | 待核实 | `kimi -p` | `--output-format` | `--session <id>`，能否与 `-p` 同用待核实 | ACP `session/load` | 待核实 |

✅ 本机 `--help` 核实过；📄 来自文档或第三方资料，接入前要逐项核实。

### 3.3 对协议的影响

1. **会话 ID 必须是不透明字符串**，外加可选的定位信息。各家的格式差别很大：UUID、文件路径、`T-<uuid>`、名字都有。Claude Code 和 Qwen Code 的会话还和工作目录绑定。所以 `target` 里除了 `session`，还要带 `cwd` 和可选的 `locator`（比如 Pi 的会话文件路径）。
2. **适配器按能力声明，不按 Harness 写死。**上表的六项能力就是适配器的接口。回调记录的 `requires` 引用的就是这些能力名。
3. **SDK 框架需要一种不是"会话"的目标。**预留 `target.kind = "endpoint"`：执行一条命令，或 POST 到一个本机地址，把回调 JSON 交给应用。应用拿到后自己去 `Command(resume=…)`。
4. **ACP 是接入长尾 Harness 的捷径。**写一个 ACP 客户端适配器，Gemini、Kimi、Goose、OpenCode 都能当子代理用。但 ACP 的会话归启动它的客户端所有，Reins 没法向用户在编辑器里开的 ACP 会话投递，所以 ACP 只用于"启动子代理"，不用于回调。
5. **结构化报告优先用原生 Schema。**Codex 的 `--output-schema` 和 Claude 的 `--json-schema` 能直接约束最终输出；不支持的再退回"写文件"的方式。
6. **记录子代理自己的会话 ID。**有预设 ID 能力的 Harness 由 Reins 指定，否则从输出事件里读。这样以后可以继续追问同一个子代理，而不是重新起一个。
7. **A2A 不保证送达，Reins 保证。**Reins 的至少一次投递、稳定的回调 ID 和显式 ACK，正是 A2A 留给实现方自己解决的部分。状态取值和 A2A 对齐，方便以后桥接。

## 4. 设计原则

1. **文本给模型看，JSON 给程序读。**信封文本可以按需调整措辞；任何程序（Pi 扩展、Claude 频道、`reins wait`）都只读 JSON 记录，不解析信封文本。
2. **观察到的和声称的分开。**Reins 亲眼看到的（进程状态、退出码、时长）放在 `observed`；子代理自己报告的（做完没有、产物在哪）放在 `report`，标记为未经验证。延续现有原则："进程结束不等于任务成功"。
3. **用能力清单代替版本号升级。**格式用 `schema` 标识，功能用 `requires` 列出。daemon 遇到不认识的能力就不投递，并报告原因，绝不投一半。
4. **未知字段原样保留，必需字段缺失就拒绝。**新版本加的可选字段，旧版本读到时不报错，写回时也不丢弃。
5. **预留的字段 v1 只写空值，不实现语义。**
6. **显式指定，绝不猜测目标。**

## 5. ID：一个根，加后缀

每件工作有一个根 ID，下属的东西在根 ID 后面加后缀。从任何一个 ID 都能直接看出它属于哪件工作。

| 对象 | 格式 | 例子 |
|---|---|---|
| 任务（根） | 8 位十六进制 | `1a2b3c4d` |
| 任务的第 N 次启动 | 根 + `-a<N>` | `1a2b3c4d-a1` |
| 任务的第 N 条回调 | 根 + `-c<N>` | `1a2b3c4d-c1` |
| 目标（根） | `g-` + 8 位十六进制 | `g-77c0e1aa` |
| 目标的第 N 条提醒回调 | 目标根 + `-c<N>` | `g-77c0e1aa-c3` |
| 汇合组（根，预留） | `grp-` + 8 位十六进制 | `grp-5e6f7a8b` |

规则：

- **后缀表示"它是什么、第几个"，不表示状态。**`-c1` 是"第 1 条回调"，它从待投递到已 ACK，ID 始终不变。状态写在记录字段里。像 `-pending`、`-done` 这样的后缀不允许，否则状态一变 ID 就要跟着变。
- **投递重试不产生新回调。**同一条回调重试 10 次，ID 仍然是 `-c1`。只有出现新事件（比如恢复报告、再次通知）才会有 `-c2`。
- **每条回调都必须有根。**目前没有任务的回调有两种：目标提醒，以及 `ltc done` 报告的外部任务。前者挂在目标根下；后者改为让 `reins done` 也建一条轻量的任务记录，`kind` 为 `external`。
- 任务是最常见的根，所以不加前缀，最短；目标和汇合组是容器，比较少见，加前缀便于区分。
- 结果和 ACK 与任务、回调一一对应，不需要单独的 ID。
- 根 ID 创建时用原子方式占位，已存在就重新生成。8 位十六进制约 43 亿个，单机足够。
- 所有 ID 都符合现有的文件名安全规则 `[A-Za-z0-9_-]+`，不用改校验。
- 回调 ID 从 32 个字符缩短到 11 个，ACK 命令变成 `reins ack 1a2b3c4d-c1`。

## 6. 四类记录

所有 JSON 记录都有 `schema` 字段，格式为 `reins.<类型>/<主版本>`。只有破坏兼容时才升主版本；新增可选字段不升。
所有时间字段用 RFC 3339 UTC 字符串，精确到毫秒，例如 `"2026-10-13T11:39:22.184Z"`；时长用 `duration_seconds` 数字。

| schema | 文件 | 写入方 |
|---|---|---|
| `reins.task/1` | `tasks/<根ID>/task.json` | daemon |
| `reins.result/1` | `tasks/<根ID>/result.json` | daemon（`observed`）+ 子代理（`report`，经 daemon 校验后合并） |
| `reins.callback/1` | `queue/<状态>/<回调ID>.json` | daemon |
| `reins.ack/1` | `queue/acks/<回调ID>.json` | `reins ack` 经 daemon 写入 |

"daemon 写入"是第 5 步"只有 daemon 写状态"的前提。v1 的格式不依赖它，但字段是按这个原则设计的。

### 6.1 地址：origin 和 target

```json
"origin": {"harness": "codex", "session": "019a…", "cwd": "/work/repo", "binding": "CODEX_THREAD_ID"},
"target": {"kind": "session", "session": "019a…"}
```

- `origin`：谁提交的任务。只记录，不参与投递。
- `target`：回调投给谁。`session` 是不透明字符串（Pi 的会话直接用会话文件的绝对路径）。Harness 和工作目录就是记录里的 `agent` 和 `cwd`，所以 v1 的 target 不重复存放；以后 `--notify` 把回调发给另一个 Harness 时，再在 target 里加 `harness`、`cwd` 和 `locator`。

| `target.kind` | v1 | 含义 |
|---|---|---|
| `session` | 实现，且必须等于 `origin` | 投给某个具体会话 |
| `session`（不同于 origin） | 预留 | 以后的 `--notify <会话>` |
| `profile` | 预留 | 以后的专家档案：把结果交给一个按档案新启动的 Agent，也就是任务链 |
| `endpoint` | 预留 | 给 SDK 框架用：执行一条命令或 POST 到本机地址，把回调 JSON 交给应用 |
| `none` | 实现 | 现在的 `manual` 模式：只保存，不投递，等人来看 |

`--last` 已删除。

### 6.2 `reins.callback/1`

```json
{
  "schema": "reins.callback/1",
  "id": "1a2b3c4d-c1",
  "root": "1a2b3c4d",
  "created_at": "2026-10-13T11:39:22.184Z",
  "origin": {…},
  "target": {…},
  "requires": ["deliver.codex-queue"],
  "route": {"mode": "codex-queue", "codex_bin": "…", "codex_home": "…"},
  "group": null,
  "goal_id": null,
  "envelope": {"format": "reins-compact/1", "text": "…", "details_path": "…"},
  "delivery": {"attempts": 0, "next_attempt_at": null, "reminders": {…}}
}
```

- `root`：所属的任务或目标。任务的细节（命令、退出码、日志）**不复制**进回调记录，统一从根找 `result.json`，避免两份数据不一致。
- `requires`：投递需要的能力，由冻结的路由推出（`core/capabilities.py`）：`deliver.cli-resume`（通过 Harness 命令行恢复会话）、`deliver.codex-desktop`（向已加载的 Codex Desktop 线程提交）、`deliver.pi-steer` 和 `deliver.pi-follow-up`（Pi 扩展的两种投递方式）。`manual` 模式不需要任何能力。塔台维护一份已知能力表，遇到不认识的能力就不选取这条回调，原样留在队列里，升级后照常投递。这一项替代原来的 `version` 2、3、4。
- `route`：各个投递方式自己的参数，替代现在平铺在顶层的 `callback_mode`、`pi_profile_dir` 等字段。
- 会话环境不放进回调记录：提交时另存一份私有副本 `<queue>/environments/<回调ID>.json`（权限 600）。无头恢复以协调进程的环境为底，叠加这份副本，再去掉会话标记（`CLAUDECODE`、`CLAUDE_CODE_SESSION_ID`、`CODEX_THREAD_ID` 和 Pi 的会话变量等），`REINS_HOME` 与锁目录以协调进程为准。这样恢复会话和执行任务看到同一套环境，而不是 systemd/launchd 服务启动时定下的那套。回调 ACK、完成或取消后删除副本；失败的回调保留副本，以便 `reins retry`。没有副本的回调照旧用协调进程的环境。
- `group`：预留给汇合，以后的形状是 `{"id": "grp-…", "expect": 3, "policy": "all"}`。v1 恒为 `null`。

### 6.3 `reins.result/1`

```json
{
  "schema": "reins.result/1",
  "root": "1a2b3c4d",
  "kind": "agent",
  "observed": {
    "state": "exited",
    "exit_code": 0,
    "attempt": "1a2b3c4d-a1",
    "started_at": "…",
    "ended_at": "…",
    "duration_seconds": 7412.3,
    "backend": "systemd",
    "host": "…",
    "recovery_reason": null,
    "log": "attempt-1.log"
  },
  "worker": {"harness": "claude", "model": "…", "profile": null, "session": "5b1e…", "checkpoint": null},
  "lineage": null,
  "text_result": "agent-result.md",
  "report": {
    "status": "completed",
    "summary": "…",
    "artifacts": [{"path": "docs/guide.md", "description": "…"}],
    "questions": [],
    "next_steps": []
  },
  "report_source": "native-schema",
  "report_error": null
}
```

`kind` 的取值：`command`（`reins run`）、`agent`（`reins agent`）、`external`（`reins done`，由外部调度器托管，`observed` 里只有对方报告的退出码）。

**`observed.state`**（Reins 观察到的，可信）：

| 值 | 含义 |
|---|---|
| `exited` | 进程正常退出，看 `exit_code` |
| `interrupted` | 运行中失去所有者（重启、注销、跨主机），看 `recovery_reason` |
| `launch_failed` | 没能启动 |
| `unknown` | 无法确定是否启动过；不会自动重跑 |

这一项把现在的 `outcome: completed / unknown` 和任务状态 `interrupted / launch_failed / foreign_host` 合并成一套词。是否成功由读者结合 `exit_code` 判断，Reins 不替它下结论。

**`worker.session`**：子代理自己的会话 ID。Claude Code、Pi 这类能预设 ID 的由 Reins 指定；Codex、Amp 这类从输出事件里读；拿不到就为 `null`。

**`worker.checkpoint` 与 `lineage`（预留，v1 恒为 `null`）**：让做完一件事的专家带着积累的上下文去做下一件事。

- 任务完成的那一刻，Reins 用 Harness 的分叉能力（`codex exec fork`、`claude --resume --fork-session`、`pi --fork`）复制出一个**检查点**会话，ID 写进 `worker.checkpoint`。检查点永远不直接拿来干活，因此始终停在"刚做完"的状态。必须在完成那一刻冻结，因为 Harness 分叉复制的是会话当前状态，事后回不到那个时间点。
- 新任务用 `reins agent --from <根ID>` 从检查点分叉出新会话，多个分叉可以同时运行；`--continue <根ID>` 在原会话上原地继续，需要独占租约。
- 新任务记录 `lineage: {"from": "<根ID>", "mode": "fork" | "continue"}`，`reins tree <根ID>` 展示上下文树。血缘写在字段里，不编进 ID，否则 ID 会随分叉层数无限变长。
- 会话不能跨 Harness 分叉；跨 Harness 的交接只靠 `report` 和 `text_result`。

**`report`**（子代理声称的，未经验证）：

- `reins agent` **默认开启**，`--no-report` 关闭。
- 获取方式按 Harness 能力选择，记在 `report_source`：
  - `native-schema`：用 Codex 的 `--output-schema` 或 Claude 的 `--json-schema` 约束最终输出
  - `report-file`：不支持 Schema 的 Harness，Reins 传环境变量 `REINS_REPORT_FILE`，并在提示词末尾加一段简短说明，让子代理结束前写好 JSON
- `status` 取 `completed`、`partial`、`input_required`、`failed`。其中 `completed`、`input_required`、`failed` 有意和 A2A 的任务状态对齐；`partial`（只完成了一部分）是 Reins 自己加的。
- `summary` 上限 2000 字符，`artifacts` 上限 50 项，超出部分截断并在 `report_error` 里说明。
- 报告缺失、不是合法 JSON 或字段类型不对时，`report` 为 `null`，原因写进 `report_error`。**不会因为报告有问题就判定任务失败**，也不会因为报告说"完成"就认为任务成功。
- `text_result` 保留现在的自由文本结果，作为报告缺失时的兜底。

### 6.4 `reins.ack/1`

```json
{
  "schema": "reins.ack/1",
  "callback_id": "1a2b3c4d-c1",
  "acked_at": "…",
  "by": {"harness": "codex", "session": "019a…"},
  "note": null
}
```

语义不变：ACK 表示"收到并看过了"，不表示任务完成，也不表示目标达成。`by` 从运行 `reins ack` 的环境里自动识别，识别不到就为 `null`。

## 7. 信封文本 `reins-compact/1`

命令任务：

```text
[reins] 1a2b3c4d-c1
Task: train model
Result: exited 0 · 2h 3m 32s
From: run on systemd
Files: ~/.reins/tasks/1a2b3c4d/ (attempt-1.log, result.json)
Details: ~/.reins/queue/details/1a2b3c4d-c1.md (as needed)
Session: 019a… (only)
ACK after inspection: reins ack 1a2b3c4d-c1
```

子代理任务，多一行 `Report`：

```text
[reins] 8e4f0c12-c1
Task: write the migration guide
Result: exited 0 · 14m 8s
From: agent claude, model …, session 5b1e…
Report: completed (claimed, unverified) · 2 artifacts
Files: ~/.reins/tasks/8e4f0c12/ (attempt-1.log, agent-result.md, result.json)
Details: ~/.reins/queue/details/8e4f0c12-c1.md (as needed)
Session: 019a… (only)
ACK after inspection: reins ack 8e4f0c12-c1
```

规则：

- 第一行固定为 `[reins] <回调ID>`，方便检索。信封里不写协议版本，因为程序不读这段文本。
- 每行的键和顺序固定；没有内容的行省略。
- `Report` 行必须带 `claimed, unverified`，提醒模型这是子代理自己说的。
- 中断、启动失败、未知状态，在 `Result` 后追加 `Recovery: <原因>; not restarted. Inspect before relaunching.`，沿用现有措辞。
- 有自定义消息、模板交接、恢复说明或目标提醒时，`Details` 行写 `(read before acting)`，沿用现有规则。
- ACK 命令只写 `reins ack <id>`；只有使用非默认队列目录时才带 `--queue-dir`，否则会话在别的环境里执行 ACK 可能找不到队列。固定安装位置落地后，非默认队列会更少见。
- 现有的长度测试改成针对新格式，长度不应超过现在的 compact-v1。

## 8. 和旧 LTC 的衔接

按已经商定的"一次性迁移"：

- `reins migrate` 发现旧 LTC 还有待投递或运行中的回调、仍在运行的任务时，**拒绝迁移**，列出它们；加 `--wait` 则等旧 daemon 处理完再继续。
- 处理完后：把 `~/.ltc` 改名为 `~/.ltc.migrated-<日期>` 留作只读存档，卸载旧服务（`codex-long-task-wakeup.*`），删除三处旧 skill，安装新 skill。
- **不转换历史记录。**旧记录只在存档里，Reins 不读。
- Reins 读到没有 `schema` 字段的记录时，一律当作无法识别，拒绝处理并提示运行 `reins migrate`。

这样代码里不需要保留任何旧格式的读取逻辑。

## 9. 已拍板

| 问题 | 决定 |
|---|---|
| `--last` | 删除。违反"显式指定谁来做、回调给谁"的原则 |
| 子代理报告 | `reins agent` 默认开启，`--no-report` 关闭 |
| ID | 一个根 ID 加后缀，见第 5 节 |
| 回调记录是否复制任务细节 | 不复制，只引用根 ID |

## 10. v1 不做的事

- 远程投递、跨机器、多用户鉴权
- 任务运行中的进度推送
- `--notify`、专家 Agent、汇合、`endpoint` 目标、检查点与分叉的具体语义（只预留字段）
- ACP 客户端适配器、新增 Harness（协议已为它们留好位置，实现放到 v0.8 之后）
- 和 A2A 的实际桥接（只对齐状态取值）
- CLI 和 daemon 之间的 socket 协议（第 5 步单独设计，届时增加 `reins.daemon/1`）

## 11. 上下文、节点与专家（v0.9 设计，2026-10-15 拍板）

### 11.1 概念

| 概念 | 含义 | 怎么寻址 |
|---|---|---|
| 节点（node） | 一次 `reins agent` 任务及它结束时子代理会话的状态 | 任务 ID（`1a2b3c4d`）、该任务的回调 ID（`1a2b3c4d-c1`）或路径名（`research/review`）；命令里优先用 ID，名字给人看 |
| 上下文（context） | 一条线性的节点链，链上每一步都基于上一步的会话 | 链上第一个节点的任务 ID |
| 专家（expert） | 预先配置的"Harness + 模型 + 推理强度 + 系统提示词"组合 | 专家名（`writer`） |

**每个节点都有自己的 Harness 会话。** 续写和分叉都是"从某个节点的会话分叉出新会话，再执行新提示词"：Claude 用 `--resume <会话> --fork-session --session-id <新 UUID>`，Codex 用 `codex exec fork <会话> -` 并从 `--json` 事件读出新会话 ID，Pi 用 `--fork <会话文件> --session-dir <节点目录>`。任务结束后，它的会话不再被任何人写入，天然就是那个时刻的检查点，所以可以从任意历史节点精确地分叉，不需要单独冻结检查点。代价是每个节点各存一份会话历史。

会话之外的共享状态不在检查点里。Claude Code 的自动记忆（`<配置目录>/projects/<项目>/memory/`）由同一项目的所有会话共用，子任务里"记住某事"会写进去，之后从更早节点分叉出的会话也会读到（真机实测：从 research 分叉出的 branch 说出了只在 research/recall 里出现过的词，记忆文件里正好有它；设置 `CLAUDE_CODE_DISABLE_AUTO_MEMORY=1` 后分叉只记得 research 的内容）。Reins 不替用户关掉记忆；需要严格隔离的节点在子进程环境里设置这个变量。

### 11.2 统一入口

```bash
reins agent codex --name research -- "调研 X"            # 新上下文
reins agent continue research --name review -- "写评审"    # 接在链末尾：research/review
reins agent fork research --name plan -- "做规划"          # 从 research 节点分出新上下文：research/plan
reins agent expert writer --name draft -- "写初稿"         # 按专家配置新建上下文
```

- 写成子命令，读起来像一句话（`reins agent continue research`）。`continue` 和 `fork` 沿用原节点的 Harness、模型、专家和工作目录，不必重复写。
- `continue` 只能接在链的末尾；从中间节点接着干必须用 `fork`，并起新名字。
- 节点的任务还没结束时，不能从它续写或分叉。
- 只有子代理任务产生节点；`reins run` 和 `reins done` 不产生。

### 11.3 名字是必填字段

- 每个节点都有一段名字：`--name` 给出；没给时取 `--task` 的前几个词；两者都没有时拒绝提交。一段名字不含 `/` 和空白，最长 32 个字符，可以是中文。
- **节点的完整名字是血缘路径**：续写和分叉都在父节点的完整名字后面加一段，例如 `research`、`research/review`、`research/plan`。不看 ID，只看名字，就知道这个子代理之前做了什么、后来做了什么。
- 完整名字全局唯一，同一个父节点下不允许重名。

### 11.4 读摘要，不读原始会话

`reins context list` 和 `reins context show` 只显示每个节点的名字、Harness、模型、状态、观察到的结果和子代理报告的摘要。原始会话文件只给出路径，Reins 从不把它的内容打印出来：父 Agent 读子代理的完整会话会占满自己的上下文，`result.json` 里的报告摘要才是给它看的。

### 11.5 记录

- 任务记录新增 `node`：`{"name": "research/review", "context": "1a2b3c4d", "lineage": {"from": "<节点ID>", "mode": "new" | "continue" | "fork"}}`。
- `result.json` 的 `worker.session` 写入这个节点自己的 Harness 会话（Pi 为会话文件路径），`lineage` 与任务记录一致。
- 上下文记录 `reins.context/1` 存在 `~/.reins/contexts/<上下文ID>.json`：`{"schema", "id", "harness", "cwd", "model", "expert", "nodes": [节点ID...], "parent": {"context", "node"} | null, "archived": false}`。

### 11.6 专家配置文件

- 位置：`~/.reins/experts.yaml`，schema 为 `reins.experts/1`；可以用 `--experts <文件>` 临时指定别的文件。
- `reins expert export > experts.yaml` 把系统提示词文件内联进去，生成一个自包含的文件，拷到别的机器上用 `reins expert import experts.yaml` 导入。
- 每个专家：`description`、`harness`、`model`、`reasoning`、`instructions`（或相对于配置文件的 `instructions_file`）、`report`（默认 true）。
- `instructions` 加在任务提示词前面，而不是作为各 Harness 的系统提示词传入：三个 Harness 设置系统提示词的方式各不相同，统一放进提示词，同一份配置在任何 Harness 上表现一致。续写和分叉不会重复添加，它们已经在会话里了。
- 现有的 `reins template` 由专家取代。

### 11.7 管理命令

| 命令 | 作用 |
|---|---|
| `reins context list` | 以树状列出上下文和节点（名字、Harness、模型、状态、报告状态） |
| `reins context show <节点>` | 一个上下文的完整链：每个节点的结果和报告摘要 |
| `reins context archive <节点>` | 归档整个上下文，不再出现在列表里 |
| `reins expert list` / `show` / `import` / `export` | 管理专家配置 |

## 参考资料

- Codex App Server：https://developers.openai.com/codex/app-server
- Gemini CLI 无头模式：https://geminicli.com/docs/cli/headless.md ；钩子环境变量：https://geminicli.com/docs/hooks/reference.md
- Qwen Code 无头模式：https://qwenlm.github.io/qwen-code-docs/de/users/features/headless/
- OpenCode 插件：https://opencode.ai/docs/plugins/ ；CLI：https://opencode.ai/docs/zh-cn/cli
- Cursor CLI：https://cursor.com/docs/cli/overview
- Copilot CLI 命令参考：https://docs.github.com/en/copilot/reference/cli-command-reference
- Goose 无头模式：https://goose-docs.ai/docs/tutorials/headless-goose
- Amp（第三方资料）：https://littlebearapps.com/help/untether/amp-runner/
- Kimi CLI：https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-command ；ACP：https://www.kimi.com/code/docs/en/kimi-code-cli/reference/kimi-acp
- ACP 概览：https://www.philschmid.de/acp-overview
- A2A 推送通知：https://dev.to/nehigna/a2a-push-notifications-what-the-spec-guarantees-and-what-it-leaves-to-you-55i9
- LangGraph 中断与持久执行：https://docs.langchain.com/oss/python/langgraph/durable-execution
- Claude Code Channels（第三方资料）：https://www.atcyrus.com/stories/what-are-claude-code-channels
