# 安装

准备一个已经完成登录的 Agent CLI。Reins 和 Agent 使用同一个用户账号运行。

## 用 npm 安装（Linux x64）

预编译包不需要 Python，并且带上了 [`reins tui`](guides/tui.md) 用的 Zellij 和面板：

```bash
npm install -g harness-reins
reins setup --enable --now
reins --version
```

## 从源码安装

其他平台（macOS、Windows、Linux arm64）从源码安装，需要 Python 3.9 或更新版本和 Git。虚拟环境用来保存 Reins 及其依赖，安装完成后保留这个目录。

### Linux / macOS

```bash
git clone --depth 1 https://github.com/lz59970062/harness-reins.git
cd harness-reins
python3 -m venv .venv
source .venv/bin/activate
python -m pip install '.[tui]'
reins setup --enable --now
reins --version
```

`[tui]` 装上 `reins tui` 的面板；另外从系统包管理器装 [Zellij](https://zellij.dev) 就能用它。

### Windows PowerShell

```powershell
git clone --depth 1 https://github.com/lz59970062/harness-reins.git
Set-Location harness-reins
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\reins.exe setup --service windows-task --enable --now
& .\.venv\Scripts\reins.exe --version
```

`setup` 安装 Agent 技能，并配置后台协调器。最后一条命令显示版本号。Windows 后续调用可以继续使用 `.venv\Scripts\reins.exe` 的完整路径。

## 选择运行环境

| 环境 | 任务由谁托管 | 使用方式 |
| --- | --- | --- |
| Linux 桌面或服务器 | systemd 用户服务 | 使用默认 `setup` |
| macOS 桌面登录会话 | launchd | 在当前用户的终端中运行 `setup` |
| Windows 10 及更新版本 | 计划任务与 Job Object | 保持该用户登录，使用本地 NTFS 目录 |
| Docker、AutoDL 或使用 screen 的 Linux | GNU screen | 按下面的容器步骤配置 |

在 Debian / Ubuntu 容器中，先安装 screen，再启动协调器：

```bash
sudo apt-get install screen
reins setup --backend screen --service standalone --now
```

容器内的 Reins、Agent、工作目录和会话文件放在同一个运行环境中。把 `~/.reins`、Agent 配置目录和项目目录放在持久卷里，可以在重启后继续读取任务记录。镜像和启动配置见[容器部署资料](https://github.com/lz59970062/harness-reins/blob/main/docs/containers.md)。

## 升级

先处理完队列中的任务和回调，再安装新版本（npm：`npm install -g harness-reins`；源码：在原目录 `git pull` 后重新 `pip install`），然后运行 `reins setup --keep-skill --force --enable --now`。这会更新协调器并保留已有技能内容。

安装完成后，进入[第一个任务](quickstart.md)。
