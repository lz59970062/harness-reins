# Codex Desktop

## Default native queue callbacks

Start Desktop normally. `auto` selects `codex-queue` for Codex Desktop and invokes the public `codex queue --thread UUID --message TEXT` once after task completion. It needs no custom launcher, private MCP access, extra model, or `--resume` against another owner.

```bash
reins run --callback-mode codex-queue --task "one minute callback" -- python -c "import time; time.sleep(60); print('ready')"
```

A successful command means queued, not received. The receiving conversation must ACK. Durable intent and the native message ID prevent automatic re-publication after a timeout or lost response. Codex manages delivery to busy/idle conversations; keep the receiving chat open. Reins does not poll conversation state or schedule model wakeups to collect results.

The CLI interface is platform independent. A real macOS Desktop received a 60-second delayed message from an independent process and started a new turn; Linux/Windows Desktop receipt has not been live-tested. Server CLI auto-routing still uses the existing CLI resume transport. macOS prefers the bundled Codex, while an explicit `REINS_CODEX_BIN` wins. The binary and profile paths are frozen at submission. Update the coordinator with Reins; older coordinators retain records requiring the new `deliver.codex-queue` capability instead of misrouting them.

## Native App Server tool-output callbacks

The opt-in `app-server` route uses an existing server that already owns the bound
thread. It does not launch Desktop or access the application's private MCP pipe.

```bash
reins doctor --agent codex --session THREAD_ID --callback-mode app-server \
  --app-server-socket /absolute/path/to/owner.sock
reins run --agent codex --session THREAD_ID --callback-mode app-server \
  --app-server-socket /absolute/path/to/owner.sock --task "native callback" -- python job.py
```

Submission freezes the resolved socket path. Delivery verifies the same-user
Unix peer and checks that the target thread is loaded before subscribing to it.
It never resumes an absent thread on another Core and never falls back to CLI.
Callbacks use `turn/start.toolOutput`, preserving the existing thread's permissions
and configuration. Idle threads start processing; active threads receive queued
tool output without waiting for idle. The original thread needs permission to run
the callback's ACK command; this route does not override its sandbox or approvals.

The client waits for pushed `turn/completed` events without polling thread state.
ACK/cancel files are checked before waiting and after completion, disconnection or
timeout. Completion alone is not success: the model must ACK. Submission intent is
persisted before sending, and an uncertain outcome prevents automatic replay or
transport fallback. An older Core rejecting `toolOutput` blocks the callback until
repair; it is not silently sent as user input.

The RPC is platform independent. This implementation supports same-user Unix
sockets on macOS/Linux; the native Windows transport is not implemented. Existing
server and local CLI workflows keep `codex exec resume` and need none of this setup.

The macOS Desktop probe on 2026-11-01 received an in-application message, but its
external MCP client was rejected with `missing-code-signing-identity`. The default
daemon did not own the Desktop thread. This route does not bypass that boundary or
claim standard-launch Desktop end-to-end delivery.

An opt-in test uses real Codex 0.166.0 with a temporary profile and local stub model
(no account or remote model) to verify idle/active tool-output delivery and events:

```bash
PYTHONPATH=src:tests REINS_TEST_NATIVE_CODEX_BIN=/absolute/path/to/codex \
  python -m unittest test_native_core_integration -v
```

Protocol: [Codex App Server](https://learn.chatgpt.com/docs/app-server).

## Optional shared Core launcher

Codex Desktop integration uses the shared Core launcher in the `0.7.1` preview. Desktop and Reins access sessions through the same Core, which delivers callbacks into the desktop app when tasks finish.

## Generate a launcher

Install the [preview](../installation.md#install-the-preview) on macOS or Windows, then run:

```bash
reins setup --force --enable --now
reins desktop status
```

macOS generates `Start Reins Desktop.command`. Windows generates `Start Reins Desktop.cmd` and a PowerShell script. The default location is the `harness-reins` folder inside your Codex configuration directory.

## Start Desktop

Save your work and quit Desktop. Double-click the generated launcher, or run:

```bash
reins desktop launch
```

Submit a small task from that Desktop session:

```bash
reins run --callback-mode desktop --cwd "$PWD" --task "Desktop callback example" -- python -c "print('Desktop callback ready')"
```

Ask the Agent to read the result and run the included ACK command to check the integration. In a PowerShell tool environment, use `--cwd "$($PWD.Path)"` in place of `--cwd "$PWD"`.

## Choose an app path

On macOS with multiple app installations, select one explicitly:

```bash
reins desktop prepare --app /Applications/Codex.app --force
```

Windows uses direct launch by default. The experimental Store package-context option is configured with:

```powershell
reins setup --desktop-launch-mode package-context --force --enable --now
```

See the [macOS Desktop](https://github.com/lz59970062/harness-reins/blob/main/docs/macos-desktop-bridge.md) and [Windows Desktop](https://github.com/lz59970062/harness-reins/blob/main/docs/windows-desktop-bridge.md) notes for platform setup and validation records. After an app upgrade, run the small example again to check callbacks.
