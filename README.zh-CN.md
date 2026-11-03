<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lz59970062/harness-reins/main/assets/readme/hero-dark.svg">
  <img src="https://raw.githubusercontent.com/lz59970062/harness-reins/main/assets/readme/hero-light.svg" width="100%" alt="Harness Reins">
</picture>
</p>

# Reins（Harness Reins）

**给 harness 配的 harness：把工作派给任何一个 Agent，结果回到发起它的那个会话。**

[English](README.md) · [文档站](https://lz59970062.github.io/harness-reins/) · [快速开始](https://lz59970062.github.io/harness-reins/quickstart/) · [基准测试](https://lz59970062.github.io/harness-reins/benchmark/) · [更新记录](CHANGELOG.md)

## 要解决的问题

你让 Codex 或 Claude Code 训练一个模型、跑一组基准测试，或者把一次代码审查交给另一个 Agent。
这件事要花两个小时，然后呢？

- Agent 用 `sleep && tail` **轮询**，把回合、token 和上下文浪费在等待上。
- 或者任务**挂在 Agent 的 shell 下面**，回合一结束、超时或者应用重启，任务就跟着没了。
- 或者另一个 Agent 的结论落在别的窗口里，**要你手工搬回**原来的对话。

## Reins 怎么做

Reins 把工作（一条命令，或任何 Harness 里的一个全新 Agent）交给操作系统原生的进程托管者独立运行。
完成后，Reins **回到原来那个会话**，送上一条简短的结果：观察到了什么、子代理报告了什么、文件在哪里，
以及确认收到的命令。

```text
[reins] <task-id>-c1
Task: train model
Result: exited 0 · 2h 3m 32s
From: run on systemd-user
Files: ~/.reins/tasks/<task-id>/ (attempt-1.log, result.json)
Details: .../details/<task-id>-c1.md (as needed)
Session: <session-id> (only)
ACK after inspection: reins ack <task-id>-c1
```

等待期间不消耗任何模型调用。

## 实测：等待要花多少

**省 8 倍的钱，少 10 倍的模型调用，Agent 进程被杀，任务照样跑完。**
同一个 Agent（Codex + Kimi K3）、同一个任务，分别不用和使用 Reins。
→ [完整的基准测试](https://lz59970062.github.io/harness-reins/benchmark/)

<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lz59970062/harness-reins/main/assets/readme/benchmark-longrun-zh-dark.svg">
  <img src="https://raw.githubusercontent.com/lz59970062/harness-reins/main/assets/readme/benchmark-longrun-zh-light.svg" width="100%" alt="60 小时内，不用 Reins 的 Agent 发出 911 次模型请求、花费 287 元，之后会话被杀、任务失败；用 Reins 只发出 90 次请求、花费 35 元，并跑完全部 72 小时">
</picture>
</p>

不用 Reins，Agent 只能自己守着：一个会话轮询了 60 小时，发出 911 次模型请求、花掉 ¥287，进程一被杀，任务也跟着没了。
用 Reins，Agent 每做完一步就结束回合，有事时才被叫回：同样 60 小时只花 ¥35，第 60 小时无进程可杀，任务一直跑到结束。

<p align="center">
<picture>
  <source media="(prefers-color-scheme: dark)" srcset="https://raw.githubusercontent.com/lz59970062/harness-reins/main/assets/readme/benchmark-time-zh-dark.svg">
  <img src="https://raw.githubusercontent.com/lz59970062/harness-reins/main/assets/readme/benchmark-time-zh-light.svg" width="100%" alt="墙钟时间占比：不用 Reins 时大部分时间 Agent 活着干等；用 Reins 时大部分时间没有任何 Agent 进程">
</picture>
</p>

| | 不用 Reins | 用 Reins |
| --- | ---: | ---: |
| 3 天任务，前 60 小时花费 | ¥286.78 | **¥35.17** |
| 这 60 小时的模型请求 | 911 | **90** |
| 第 60 小时杀掉所有 Agent 进程 | 任务失败 | **跑完 72 小时** |
| 结果回到原会话（中位延迟） | – | **2 秒** |
| 4.8 小时任务花费 | ¥30.59 | **¥30.11** |

Reins 改变的是等待的成本和可靠性，不改变模型解题的能力。花费按标价计算（输入 ¥20、缓存命中 ¥2、输出 ¥100 / 百万 token）。

## 为什么要同时支持多个 Agent 框架

订阅是使用前沿模型最划算的方式，但订阅和 Harness 是绑在一起的：Claude 的订阅让你在 Claude Code
里用 Claude 模型，ChatGPT 的订阅让你在 Codex 里用 OpenAI 模型。这份额度一般不能拿到别的工具里用，
这些 Harness 也都围绕自家模型设计。

Pi 这类开源 Harness 正好相反，给它什么模型它就跑什么模型：任意厂商的 API key、自己 GPU 上的开源权重模型，
或者专门处理杂活的便宜快速模型。

真实的工作很少只适合一个模型。写作、编码、审查、长时间实验，各有做得最好或最省钱的模型，而且模型还在越来越专精。
想让每件事都交给合适的模型，就得让几个 Harness 并行工作，有闭源的，也有开源的。可它们彼此并不知道对方存在：
Codex 会话没法把任务交给 Claude Code 再把结果拿回来，Pi 里的 Agent 也没法向它们汇报。

本项目要补的就是这块空白。在 Codex、Claude Code 或 Pi 的任一会话里，`reins agent codex|claude|pi`
都能把一个全新的子代理作为持久任务启动，结果回到发起任务的那个会话。一套横跨闭源与开源 Harness 的调度层，
Codex、Claude Code、Pi 都是 harness，Reins 是握住它们的那副缰绳，这也是 Long Task Callback 先演进为 ATC、再改名为 **Harness Reins** 的原因。

## 快速开始

```bash
# 1. 安装（目前提供 Linux x64 预编译包；其他平台见下文）
npm install -g harness-reins

# 2. 设为本机唯一的 Reins，安装 Codex 和 Claude Code 的 skill，启动协调服务
reins setup --force --enable --now
#    同时使用 Pi？加上 --with-pi-extension

# 3. 检查
reins --version
reins doctor --agent codex --session <会话ID>
```

没有 Node，或者在 macOS、Windows 的预编译包发布之前，可以从源码安装，再运行同样的 `reins setup`：

```bash
python3 -m pip install "git+https://github.com/lz59970062/harness-reins.git"
```

`reins setup` 会把当前这份安装登记为唯一的 `~/.reins/bin/reins`。其他环境里装的 `reins` 会把命令转发给它，
不同版本不会混用。

### 从 ATC（`atc`）或 Long Task Callback（`ltc`）迁移

Reins 不读取这两代的记录，需要一次显式迁移：

```bash
reins migrate            # 只显示会改什么，不做任何改动
reins migrate --apply    # 存档 ~/.atc 和 ~/.ltc，卸载旧服务、skill 和 hook
reins setup --force --enable --now
```

两代各自迁移：哪一代还有待投递的回调或正在运行的任务，就先保持原样并报告出来，不会因为 LTC 忙着就拖住空闲的 ATC。
加 `--wait` 等旧 daemon 处理完，或用 `--only atc|ltc` 只迁其中一代。旧状态目录只会改名存档，不会删除。
ATC 的 systemd drop-in 和 `experts.yaml` 会带到 Reins。

## 常用命令

| 命令 | 作用 |
| --- | --- |
| `reins run -- <命令>` | 把一条命令交给系统托管，完成后回调 |
| `reins agent codex\|claude\|pi -- <任务>` | 启动一个全新的子代理；它会写一份结构化报告，信封里标注为"声称，未经验证" |
| `reins done --exit-code <N>` | 任务由 Slurm、tmux 等外部调度器托管时，只负责报告结束 |
| `reins session register <名字>` | 给当前会话起名，让其他会话能找到它 |
| `reins send <名字> -m <正文>` | 给另一个 Codex、Claude Code、Pi 或 DeepSeek Harness 会话发消息，详见[指南](docs/guides/messages.md) |
| `reins inbox` | 查看这个会话的未读消息 |
| `reins ack <回调ID>` | 确认已经收到并看过结果（不等于任务完成） |
| `reins status` | 查看还需要处理的回调 |
| `reins doctor` | 检查环境，给出可以交给 Agent 执行的修复计划 |
| `reins migrate` | 从 LTC 迁移 |

## 文档

| 主题 | 链接 |
| --- | --- |
| 概览与安装 | [概览](docs/overview.md) · [安装](docs/installation.md) · [快速开始](docs/quickstart.md) |
| 用法指南 | [运行任务](docs/guides/run.md) · [子代理](docs/guides/agents.md) · [回调](docs/guides/callbacks.md) · [目标](docs/guides/goals.md) · [外部任务](docs/guides/external-jobs.md) |
| Harness | [Claude Code](docs/claude-code.md) · [Pi](docs/pi.md) |
| 平台 | [macOS](docs/macos.md) · [Windows](docs/windows.md) · [容器](docs/containers.md) |
| 恢复与排错 | [恢复](docs/recovery.md) · [排错](docs/troubleshooting.md) |
| 设计 | [协议 v1](docs/design/protocol-v1.md) · [包架构](docs/design/architecture.md) |

## 许可证

[MIT](LICENSE) · Reins 的前身是 Agent Traffic Control（`atc`），更早是 Long Task Callback（`ltc`）。
