# 终端界面：reins tui

一个终端、认证一次，就能看到这台机器上所有的 Claude Code、Codex 和 Pi 会话，以及它们派出的任务、Goal 和消息；会话就在旁边的窗格里打开，用的是各自 Harness 的原生界面。同一个会话永远只有一个进程，不会分叉。

```bash
reins tui
```

SSH 断开后布局保留。重新连上，再运行一次 `reins tui`，所有窗格都还在。

## 界面

```text
┌─ Reins ──────────────────────────────┬─ claude 08e8651e 运动图 ───────────┐
│ Sessions  Tasks  Goals  Messages     │                                    │
│ ●  here        working  运动图       │  Claude Code 原生界面（attach）    │
│ ○  Desktop     idle     进展 PPT     │                                    │
│ ◌  closed               旧的回调     ├─ log cf408bf5 ─────────────────────┤
│ opened 运动图                        │  任务输出，持续跟踪                │
└──────────────────────────────────────┴────────────────────────────────────┘
```

左边是 Reins 面板，四个标签页：

- **Sessions**：每个会话的标题、在哪里运行、忙闲、名下正在跑的任务数、待处理的回调数和 Goal。最近派过任务、现在已关闭的会话也列在这里，方便找回。
- **Tasks**：每个任务都标出是哪个会话派的，以及回调有没有被确认。
- **Goals**：计划和进度。
- **Messages**：会话之间的消息。

右边的窗格由面板打开：会话窗格运行 `claude attach`、`codex resume --remote` 或 `reins pi`（见[Codex、Pi 和 DeepSeek Harness](#codex-pi-和-deepseek-harness)），日志窗格运行 `reins log`。会话窗格的标题是 `<Harness> <ID> @地址 会话标题`，例如 `claude 08e8651e @lab 运动图`，和面板里的名字对得上；Harness 自己设置的终端标题不会盖掉它。你手动改的窗格名会在 3 秒内被改回来，因为面板靠这个名字找到会话所在的窗格。面板宽度固定；第一个窗格占据右边的提示区，之后的会话、日志和终端开在右边最新那个窗格的下方或右边（按 `d` 切换），不会把面板越挤越窄。

## 按键

| 键 | 作用 |
| --- | --- |
| `Enter` 或双击 | 打开选中的会话；它已经在某个窗格里，就跳过去。在 Tasks 页，打开派这个任务的会话。单击只是选中 |
| `n` | 在指定目录新建 Claude Code、Codex 或 Pi 会话；装了不止一个时会先问用哪个 |
| `l` | 跟踪任务输出 |
| `t` | 在选中会话的目录里开一个终端 |
| `d` | 切换新窗格开在下方还是右边；底栏显示当前选择 |
| `m` | 给选中的会话发消息 |
| `s` | 停止 Claude Code 会话（会先确认）。对话保留，之后 `Enter` 还能打开 |
| `c` | 取消任务的回调（会先确认） |
| `o` 或右键 | 打开选中会话的菜单（见下） |
| `a` | 显示 / 隐藏已归档的会话 |
| `1`–`4` | 切换标签页 |

面板每 3 秒自动刷新。最上面一行是 `reins doctor` 的检查结果：没问题显示 “Reins is ready”，有问题会逐条列出该怎么处理。

## 会话菜单

在会话上**右键**，或者选中后按 `o`，会弹出它的菜单：

| 菜单项 | 作用 |
| --- | --- |
| Open below / Open to the right | 在下方或右边打开，不用先按 `d` 切换 |
| Fork into a new session | 带着这段对话另开一个新会话（新 ID），原会话不受影响。在 Desktop 里开着的会话也能分叉 |
| Terminal in its directory | 在会话目录里开终端，同 `t` |
| Send a message... | 给它发消息，同 `m` |
| Pin to the top / Unpin | 置顶。只影响 Reins 面板，不改 Claude 自己的列表 |
| Copy address / Name it... / Rename address... | 复制、登记或修改它在 Reins 通讯录里的地址（见下） |
| Copy Claude session id / Codex thread id / Pi session file | 复制 Harness 自己认的 ID：`claude --resume`、`codex resume`、`pi --session` 用的那个 |
| Stop... | 停止会话（会先确认），同 `s` |
| Archive / Stop and archive... / Unarchive | 归档：从列表里收起；还在运行的会先停止（会先确认）。对话保留。它再运行、派出新任务或有回调待处理时，会自动回到列表；按 `a` 能看到已归档的会话 |

置顶和归档只记在 Reins 自己的文件里（`~/.reins/tui/sessions.json`），不会删除任何对话。Claude Code 外部改不了会话标题，也没有删除单个对话的命令，所以菜单里没有这两项；标题可以在会话里用 `/rename` 改。菜单只列出会话所在 Harness 支持的操作：停止只对 Claude Code，分叉对 Claude Code 和 Codex，DeepSeek Harness 会话没有“打开”。

### 复制会话地址

地址是会话在 Reins 通讯录里的名字。复制后，到别的对话里告诉 agent “发给 @这个地址”，它就会用 `reins send` 送过去。已经有名字的会话，标题前面会显示 `@名字`，置顶的会话前面有 `★`。

- 有名字：选 “Copy address”；要换名字选 “Rename address...”，旧名字随之作废。别的会话已经在用的名字不会被抢走。
- 还没有名字：选 “Name it and copy its address...”，面板会建议一个名字，确认后登记进通讯录并复制。

复制会先通过终端的剪贴板协议（OSC 52）直接写进你本机的剪贴板，同时弹出一个小窗，把复制的内容单独放一行。能粘贴就按 `Enter` 关掉；粘贴出来是空的，说明终端不允许程序写剪贴板（macOS 自带的“终端”和 Claude Code Desktop 里的终端都是这样），就在小窗里手动复制：

- macOS 自带的“终端”：按 ⌘R 关掉鼠标报告，拖选那一行，⌘C，再按 ⌘R 恢复。
- iTerm2：按住 ⌥ 拖选，⌘C。

想让复制直接生效，可以用 iTerm2（在 Settings → General → Selection 打开 “Applications in terminal may access clipboard”）或 Ghostty。

## 窗格操作

窗格只用几个 `Alt` 键，按一下就生效，没有要记的模式；`Ctrl` 键全部留给 Claude Code 和终端。鼠标点击切换窗格、滚轮翻看历史也可以。

| 按键 | 作用 | 会话怎样 |
| --- | --- | --- |
| `Alt+←/→/↑/↓` | 在窗格之间移动 | |
| `Alt+x` | 关掉当前窗格 | Claude Code 和 Codex 照常在后台运行；Pi 会话随之结束 |
| `Alt+f` | 当前窗格全屏 / 还原 | |
| `Alt+=` / `Alt+-` | 调大 / 调小当前窗格 | |
| `Alt+d` | 暂时离开，以后运行 `reins tui` 回来 | 布局和窗格都保留 |
| `Alt+q` | 先问一句，默认选中“暂时离开”（Detach）；选 Quit 才结束整个界面 | 结束时 Claude Code 和 Codex 照常在后台运行；界面里的 Pi 会话和你开的终端随之结束 |

在面板里按 `Ctrl+Q` 也能离开：可以选暂时离开（同 `Alt+d`），或结束整个界面（同 `Alt+q`）。

**Mac 上 Alt 就是 Option 键**，但终端默认不把它当 Alt 发出去，`Alt` 键会全部失灵：

- macOS 自带的“终端”：设置 → 描述文件 → 键盘，勾选“将 Option 键用作 Meta 键”。
- iTerm2：Settings → Profiles → Keys，把 Left Option key 设成 `Esc+`。

没改之前，用面板里的 `Ctrl+Q` 离开或结束。

要停止某个会话，在面板里选中它按 `s`：进程停止，对话保留。

面板窗格被关掉了也没关系：再运行 `reins tui`，它会问你是否重开界面；Claude Code 和 Codex 会话照常在后台运行，重开后在面板里双击就能接回。在界面里面的终端里运行 `reins tui`，它会告诉你已经在界面里了。

## Codex、Pi 和 DeepSeek Harness

每个 Harness 都用自己的原生界面打开，Reins 对每个都守同一条承诺：一个会话只有一个进程。

| Harness | 会话列表来自 | 会话窗格运行 | 关掉窗格后 | 打开期间的回调 |
| --- | --- | --- | --- | --- |
| Claude Code | `claude agents` | `claude attach`，后台会话 | 照常运行 | 会话里的 Reins hook |
| Codex | Codex 的 app-server 守护进程 | `codex resume --remote`，守护进程上的线程 | 留在守护进程上，空闲后由它卸载 | 守护进程上这个线程的新一轮 |
| Pi | Pi 的会话文件 | `reins pi`，带 Reins 扩展和写入租约的 Pi | Pi 随窗格结束 | Reins 的 Pi 扩展 |
| DeepSeek Harness | Reins 通讯录和任务记录 | 不打开：它没有终端界面 | | 它自己运行的 `reins wait` |

- **Codex** 会话通过 Codex 共享的 app-server 守护进程（`codex app-server daemon`）打开，所以 Reins 窗格、通过 SSH 连接的 Codex Desktop 和 `codex resume --remote` 看到的是同一个线程。守护进程没在运行时，Reins 会先启动它。不带 `--remote` 打开的 Codex 窗口自己写线程：面板显示它开在别的终端里，不会再打开一份。新建的 Codex 会话发出第一条消息后才会出现在列表里。
- Codex 收到回调时保留线程自己的设置，只把 Reins 的队列和通讯录目录加进可写目录，所以 agent 不用申请提权就能 ACK；线程留在守护进程上期间一直有这项权限。
- **Pi** 没有守护进程，会话就活在它的窗格里。在别处带着 Reins 扩展打开的 Pi 会话显示为开在别的终端；不带扩展打开的看不到。
- **DeepSeek Harness**：有 Reins 名字或派过任务的会话会列出来，可以发消息、复制地址、看任务日志。打开请用 DeepSeek Harness 的网页版或桌面版。

## 打开会话时会发生什么

你只需要按 `Enter` 或双击，Reins 按会话现在的状态选择做法。以 Claude Code 会话为例：

| 会话状态 | Reins 的做法 |
| --- | --- |
| 已经在某个窗格里 | 跳到那个窗格 |
| 在后台运行，或已停止 | 新开窗格，`claude attach` 接上；已停止的会话会被重新拉起 |
| 已关闭 | 先用 `claude --bg --resume` 把它转成后台会话，再接上。ID 和对话都不变 |
| 在 Desktop 或别的终端里开着 | 不打开，提示它在哪里；照样可以发消息、看任务日志。那边关掉后就能打开 |

直接打开某个会话，可以写会话 ID，也可以写任务 ID 或回调 ID：

```bash
reins tui cf408bf5
```

## 和 Desktop 一起用

Claude Code Desktop 打不开正在后台运行的会话，会提示 “That session is running in the background”。在面板里选中它按 `s` 停止，或者运行 `claude stop <id>`，Desktop 就能打开。

## 安装

- **npm 安装**：已经带上 Zellij 和面板，不用另装。
- **pip 安装**：`pip install 'harness-reins[tui]'`，再从系统的包管理器安装 [Zellij](https://zellij.dev)，或者用 `REINS_ZELLIJ` 指定它的路径。
- **Windows**：Zellij 不支持 Windows，请用 `reins status` 和 `reins log`。

Reins 用自己的 Zellij 配置（`~/.reins/zellij`），不影响你本机的 Zellij。它的会话索引放在本机临时目录（`/tmp/reins-zellij-<uid>`），所以 `~/.cache` 挂在 JuiceFS、NFS 这类网络盘上时也不会卡住；窗格里的程序仍用你自己的 `XDG_CACHE_HOME`。`reins tui` 只给人用；在 agent 会话里运行会直接报错。
