# Reins 架构设计（草案）

状态：已评审，2026-10-13。协议细节见 [protocol-v1.md](protocol-v1.md)。0.9 版由 ATC 改名为 Harness Reins，结构不变。

## 1. 故事决定架构

Reins 的故事是一句话：**显式指定谁来做事、回调给谁；任务交给操作系统独立运行；结果可靠地回到指定的会话。**

一件工作的生命周期因此固定为六步，架构的每一层都对应其中一段：

```text
派发 Dispatch → 托管 Own → 观察 Observe → 报告 Report → 投递 Deliver → 确认 Acknowledge
 谁来做、回给谁   OS 托管进程    Reins 记下事实    子代理声称结果   送回指定会话     会话确认收到
```

核心名词也由故事决定，代码和文档统一使用：

| 名词 | 含义 | 空管比喻（只用于文档） |
|---|---|---|
| Work（工作） | 一个根：任务、目标或汇合组 | 航班 |
| Worker（执行者） | 谁来做：一条命令，或一个 Agent（Harness + 模型），或一个专家 Agent | 机组 |
| Expert Agent（专家 Agent） | 预先注册的"提示词 + Harness + 模型 + 推理强度"组合，例如写作专家、编码专家 | 专业机组 |
| Target（目标） | 回调给谁：会话、专家、端点或不投递 | 登机口 |
| Owner（托管者） | 谁托着进程：systemd、launchd、Task Scheduler、screen | 跑道和机库 |
| Callback（回调） | 一次投递 | 进港通知 |
| Result（结果） | 观察到的事实 + 子代理的报告 | 飞行记录 |
| Ack（确认） | 目标会话确认收到 | 落地确认 |
| Tower（塔台） | daemon：唯一能改共享状态的进程 | 塔台 |

代码里的包名用平实的英文名，比喻只出现在文档和 README 里。这样新贡献者不需要先学一套隐喻才能读代码。

## 2. 现状

| 部分 | 现状 | 评价 |
|---|---|---|
| `agents/` | `AgentAdapter` 协议 + 注册表，Codex、Claude、Pi 三个适配器 | 好的抽象，保留；按能力拆细 |
| `platforms/` | `ExecutionBackend` 协议，systemd、launchd、Windows、screen | 好的抽象，保留；改名为 owners |
| `storage.py` | 原子写入、私有权限 | 保留 |
| `callbacks.py` | 信封渲染，无 I/O | 保留，按 protocol v1 重写 |
| `cli.py` 5890 行 | 命令解析、队列、投递引擎、Codex Desktop 的 app-server 客户端、托管任务与恢复、目标、服务安装、代理配置、daemon 主循环全在一起 | **主要问题** |
| `claude_code.py`、`claude_channel.py`、`pi_callback.py`、`desktop_*.py` | 各 Harness 的投递通道，散落在顶层 | 归到各自的 Harness 目录下 |
| 测试 13649 行 | 大量直接 mock `cli.<函数>` | 拆分时要同步迁移，是重构的主要工作量 |

## 3. 目标结构

```text
harness_reins/
  core/            纯逻辑，不做任何 I/O
    ids.py           根 ID 与后缀（1a2b3c4d-c1）
    records.py       四类记录的数据类、严格校验、未知字段保留
    envelope.py      reins-compact/1 信封渲染
    states.py        observed.state、report.status 等取值和状态机
    capabilities.py  能力名常量（deliver.resume.claude 等）

  store/           持久化，只关心"怎么可靠地存"
    layout.py        ~/.reins 目录布局，唯一的路径来源
    files.py         原子写入（原 storage.py）
    locks.py         文件锁、租约
    inbox.py         CLI 提交请求的收件箱（见第 5 节）
    repo.py          按 schema 读写 Task、Callback、Result、Ack

  harness/         谁来做事、怎么回到会话；每个 Harness 一个目录
    base.py          能力协议：Binder、Spawner、Resumer、Injector、SchemaOutput
    registry.py
    codex/           adapter.py、app_server.py（从 cli.py 拆出）
    claude/          adapter.py、channel.py、wait.py
    pi/              adapter.py、channel.py、extension/（JS）
    （预留）acp/     通用 ACP 客户端，只用于启动子代理
    （预留）endpoint/ 给 SDK 框架用的命令或 HTTP 投递

  owners/          谁托着进程（原 platforms/ 中的后端）
    base.py          ExecutionBackend、OwnerState
    linux.py（systemd）  macos.py（launchd）  windows.py（Task Scheduler）  screen.py

  system/          操作系统原语，只依赖标准库
    posix.py  windows_io.py  windows_process.py

  engine/          生命周期六步的业务逻辑
    dispatch.py      派发：校验 Worker 与 Target，建任务记录
    worker.py        任务进程：运行、观察、收集报告、写本次启动的结果
    recovery.py      重启后核对所有者，绝不盲目重跑
    delivery.py      按 requires 和能力选通道，重试、退避、租约
    reminders.py     回调提醒节奏
    goals.py         多阶段目标
    experts.py       专家 Agent（现 templates）

  tower/           daemon：唯一改共享状态的进程
    main.py          主循环：处理收件箱、推进状态、调度投递、核对恢复
    api.py           （第 5 步）本机 socket 接口与版本握手

  cli/             薄命令层：解析参数，提交请求，打印结果
    main.py
    commands/        run、agent、done、ack、status、cancel、retry、goal、expert、doctor、setup、migrate、wait

  install/         安装与环境
    canonical.py     ~/.reins/bin 与 versions（第 5 步）
    services.py      systemd unit、launchd plist、Windows 计划任务、supervisor
    skills.py        各 Harness 的 skill 安装
    migrate.py       从 ATC 或更早的 LTC 一次性迁移
    diagnostics.py   诊断

  assets/          随包分发的非 Python 文件：skill、JS 扩展、PowerShell 脚本
```

## 4. 依赖方向

```text
cli ──→ tower ──→ engine ──→ harness ──→ core
                     │   └──→ owners  ──→ core
                     └──────→ store   ──→ core
install ──→ owners、harness、store
（system 是最底层，store、owners、harness 都可以依赖它）
```

规则：

1. `core` 不依赖任何包，也不做 I/O，所以协议逻辑可以单独测试。
2. `harness` 和 `owners` 只依赖 `core` 和 `system`，**不碰 `store`**。适配器只负责"构造命令、建立连接、汇报能力"，不拥有任何状态。这条延续现在 `agents/base.py` 里写明的原则。
3. `engine` 是唯一把 Harness、托管者和存储组合起来的地方。
4. `cli` 不直接改共享状态，只往收件箱提交请求、读取记录。
5. 加一个测试扫描 import 语句，违反方向就失败，防止架构慢慢退化。

## 5. 单一写入者，落到文件级别

原先的说法是"只有 daemon 写状态"，但任务进程必须能在 daemon 停止时照样写下结果。所以精确的规则是：**每个文件只有一个写入者。**

| 文件 | 唯一写入者 |
|---|---|
| `tasks/<根>/attempt-<N>/`（退出码、日志、子代理报告原件） | 那次启动的任务进程 |
| `tasks/<根>/task.json`、`result.json` | 塔台 |
| `queue/**`、`goals/**` | 塔台 |
| `inbox/<唯一文件名>.json` | CLI，每个请求一个新文件，写完不再改 |

流程：

- 任务进程只往自己那次启动的目录里写，写完就退出，不依赖塔台在线。
- 塔台读取启动目录，校验子代理报告，合并成 `result.json`，再生成回调。
- `reins ack`、`reins cancel` 这类命令往 `inbox/` 放一个请求文件，由塔台处理。塔台暂时不在线时，请求也不会丢。
- 第 5 步的 socket 接口只是让 CLI 能立刻拿到处理结果；收件箱始终是兜底。

这样，多个环境里装了不同版本的 CLI，最多只能往收件箱投请求；真正改状态的永远是同一个塔台，而塔台会拒绝它不认识的 schema。

## 6. 关键接口

**Harness 适配器按能力组合**，不要求每个 Harness 实现全部能力：

```python
class Binder(Protocol):        # 从 shell 环境识别当前会话
    def bind(self, env) -> Origin | None: ...

class Spawner(Protocol):       # 启动子代理
    def spawn(self, spec: WorkerSpec) -> SpawnPlan: ...      # 命令、预设会话 ID、报告方式

class Resumer(Protocol):       # 冷投递：按 ID 恢复会话
    def resume(self, target: Target, text: str) -> Command: ...

class Injector(Protocol):      # 热投递：注入运行中的会话
    def inject(self, target: Target, callback: Callback) -> DeliveryOutcome: ...
```

每个适配器声明自己提供哪些能力（对应 protocol v1 第 3.2 节的六项）。投递引擎按回调记录的 `requires` 和目标 Harness 的能力挑选通道：能热投递就热投递，否则冷投递，都不行就标记为阻塞并说明原因。

**托管者**沿用现有的 `ExecutionBackend`：启动、查询状态、收尾。

**记录**统一用 `core/records.py` 的数据类：读取时严格校验 `schema` 和必需字段，未知字段放进 `extra` 原样写回。

## 7. 命令层和故事对齐

每次派发都显式回答两个问题：谁来做，回给谁。

| 命令 | 谁来做 | 回给谁 |
|---|---|---|
| `reins run -- <命令>` | 这条命令 | 默认是发起的会话 |
| `reins agent claude --model …` | Claude Code + 指定模型 | 默认是发起的会话 |
| `reins agent --expert writer` | 写作专家 Agent（Harness 和模型由专家定义决定） | 默认是发起的会话 |
| `--notify <会话>`（预留） | 不变 | 指定的其他会话 |
| `reins done` | 外部调度器 | 发起的会话 |

"默认是发起的会话"是显式规则，而不是猜测：发起会话的 ID 来自 Harness 注入的环境变量，识别不到就拒绝提交，不会退回"最近的会话"。

## 8. 实施顺序

架构拆分和协议改动有先后依赖。如果先在 5890 行的 `cli.py` 里实现 protocol v1，再拆文件，同一段代码要改两遍。所以建议把原计划的第 6 步"拆分 cli.py"提前：

| 顺序 | 内容 | 性质 |
|---|---|---|
| 1 | 机械改名：包名、命令名、文案 | 纯改名，测试全绿 |
| 2 | 搬家：`agents/`→`harness/`，`platforms/`→`owners/`，`storage.py`→`store/files.py`，各投递通道归位 | 纯移动，测试全绿 |
| 3 | 拆 `cli.py`：投递、Codex app-server、托管任务与恢复、目标、安装、命令，逐块搬出 | 纯移动，每块一个提交，测试全绿 |
| 4 | 加依赖方向测试 | 防退化 |
| 5 | 在 `core/` 实现 protocol v1，替换旧记录格式 | 行为改变，有新测试 |
| 6 | 收件箱与文件级单一写入者，`reins migrate` | 行为改变 |
| 7 | 固定安装位置、socket 握手、npm 打通，发布 v0.8 | 行为改变 |
| 最后 | 跨 Harness 演示场景和对照实验 | 证据 |

前 4 步只移动代码、不改行为，审查时只需确认"测试没改断言"；后 3 步才改行为，每步都有新的测试。

搬家时改了文件名的模块，会用旧名字作为别名导入（例如 `from .harness.pi import channel as pi_callback`），这样函数体一行不用改。这些别名在第 3 步拆分 `cli.py` 时逐步清理。

## 8.1 顺序调整（2026-10-15）

第 6 步原计划是"收件箱 + 文件级单一写入者"。实际评估下来，它要重写恢复逻辑里最微妙的部分（重启不重跑、结果优先于失联所有者），还要改 Pi 扩展和 Claude `reins wait` 的 ACK 写法，回归风险集中在并发和崩溃路径上。而它要解决的问题——多个环境里不同版本各写各的——可以由第 7 步从源头解决：所有入口转发到 `~/.reins/bin/reins` 这一份程序，版本不一致的命令行被握手拒绝。

因此先做第 7 步。第 6 步缩小为：`reins migrate`（已完成）、塔台拒绝不认识的 schema（第 5 步已完成）、中断任务的 `result.json` 由塔台补写。完整收件箱留作以后的加固项，出现实际问题时再做。

## 9. 已拍板

| 问题 | 决定 |
|---|---|
| 实施顺序 | 按第 8 节，拆分提前到协议改动之前 |
| 模板改名 | 故事里叫**专家 Agent**；命令为 `reins expert register writer`，派发用 `reins agent --expert writer`；代码为 `engine/experts.py` |
| 代码命名 | 包名用平实英文，比喻只放在文档里；唯一例外是 daemon 叫 `tower` |
