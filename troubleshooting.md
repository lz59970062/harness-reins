# 常见问题

## 终端找不到 reins

激活安装 Reins 的虚拟环境，再检查命令：

```bash
source /安装目录/harness-reins/.venv/bin/activate
reins --version
```

Windows 可以直接调用安装目录下的 `.venv\Scripts\reins.exe`。后台协调器也使用安装时的 Python 路径，移动虚拟环境后重新运行 `reins setup`。

## 任务提交时提示会话信息缺失

让 Agent 在当前会话的工具环境中执行 `reins run`。从独立终端或作业脚本提交时，传入 `--agent` 和 `--session`：

```bash
reins run --agent codex --session "填入原会话ID" --task "构建" -- bash scripts/build.sh
```

会话绑定方式见 [Codex CLI](integrations/codex.md)和[外部任务](guides/external-jobs.md)。

## 任务结束后还没看到回调

先查看回调队列，再检查当前会话配置：

```bash
reins status --state pending --state running --state failed
reins doctor --agent codex --session <原会话ID>
```

按报告中的具体项目处理：

| 情况 | 下一步 |
| --- | --- |
| 协调器尚未启动 | 按报告给出的 `setup` 命令启动它 |
| Claude Code 会话正在打开 | 让 Claude 将提交输出中的 `reins wait` 命令作为后台 Bash 任务运行 |
| Codex Desktop 没有收到回调 | 确认聊天仍然打开，再用 `reins status` 查看；保存的旧回调见 [Codex Desktop](integrations/desktop.md#从旧启动器升级) |
| 回调进入 `failed` | 读取错误记录，修复后在预览版中使用 `reins retry --id <回调ID>` |

业务任务的日志路径来自最初的提交输出。先打开日志和产物，可以了解任务本身的运行结果。

## Linux 上的无头回调全部失败，doctor 报告 agent_cli_blocked_by_service_sandbox

systemd 用户服务带有 `NoNewPrivileges=yes`。如果单元记录的 Agent CLI（如 `REINS_CLAUDE_BIN=~/.local/bin/claude`）其实是管理员提供的启动脚本，内部用 `sudo` 或其他 setuid 程序重新执行，它在登录终端里能运行，在服务里会失败，例如：

```text
sudo: effective uid is not 0, is /usr/bin/sudo on a file system with the 'nosuid' option set ...
```

此时每次 `claude -p --resume ...` 回调都以退出码 1 结束，重试 3 次后进入 `failed`。`reins setup` 和 `reins doctor` 会在同样的属性下用 `--version` 试运行单元记录的每个 CLI，失败时报告 `agent_cli_blocked_by_service_sandbox`，并给出二进制路径和 stderr 中的那一行。可以手动复现：

```bash
systemd-run --user --wait --pipe -p NoNewPrivileges=yes ~/.local/bin/claude --version
```

处理方式：

1. 优先让服务使用不需要提权的 CLI，例如启动脚本最终调用的真实程序：`reins setup --force --now --claude-bin /真实路径/claude`（Codex 和 Pi 分别用 `--codex-bin`、`--pi-bin`）。
2. 必须使用这个启动脚本时，运行 `reins setup --force --now --allow-privilege-escalation`。生成的单元不含 `NoNewPrivileges=yes`，服务启动的 Agent CLI 可以通过 sudo 等程序提权，请确认这是你接受的配置。

修复后对失败的回调执行 `reins retry --id <回调ID>`，不需要重新运行业务任务。

`NoNewPrivileges` 是服务进程启动时就定下的内核标志，热重载（re-exec）改不掉。`reins setup` 发现运行中的守护进程和单元不一致（`NoNewPrivileges`，或 `Environment=` 里的 Agent CLI 路径、PATH）时会改为重启服务；正好有回调在投递时会暂缓并提示命令。`reins doctor` 对这种情况报告 `coordinator_restart_required`，处理方式是等 `reins status` 没有进行中的回调后执行 `systemctl --user restart harness-reins.service`。

## Codex 被恢复后执行不了命令，ACK 失败

无头恢复时，Reins 用提交任务时记录的权限启动 `codex exec resume`：默认 `sandbox_mode=workspace-write`、`approval_policy=on-request`、`approvals_reviewer=auto_review`。如果最初的会话是在不支持 Codex 沙箱的地方用 `--dangerously-bypass-approvals-and-sandbox` 运行的（例如没有 bubblewrap 的容器），恢复后的 turn 会遇到：

```text
bubblewrap is unavailable: no system bwrap was found on PATH
Automatic approval review failed: ... Unsupported model: 'codex-auto-review'
```

命令都跑不了，ACK 也写不进去，回调会一直重试。让恢复后的 turn 和最初一样运行：在会话运行 `reins run` 的环境里设置

```bash
export REINS_SANDBOX_MODE=danger-full-access REINS_APPROVAL_POLICY=never
```

或者在 `reins run` 上传 `--sandbox-mode danger-full-access --approval-policy never`。这只适用于本来就不用沙箱运行 Codex 的环境。

## 同一条回调重复出现

读取结果后，执行回调中的完整 ACK 命令。核对回调 ID 和 `--queue-dir`，确认写入的是同一个队列。

## 重启后怎样继续训练

找到任务日志、结果文件和最近的 checkpoint。让 Agent 检查已有产物，再使用训练程序自身的恢复参数提交后续任务，例如：

```bash
reins run --cwd "$PWD" --task "继续训练" -- python train.py --resume outputs/checkpoint.pt
```

这里的 `--resume` 属于示例训练程序。换成你所用框架的恢复参数和实际 checkpoint 路径。

## 怎样反馈问题

在 [GitHub Issues](https://github.com/lz59970062/harness-reins/issues)中附上操作系统、Reins 版本、提交命令、错误文本和相关日志片段。提供一个小型复现命令，便于定位发生问题的步骤。
