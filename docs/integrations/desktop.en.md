# Codex Desktop

## Default native queue callbacks

Start Desktop normally. `auto` selects `codex-queue` for Codex Desktop and invokes the public `codex queue --thread UUID --message TEXT` once after task completion. It needs no custom launcher, private MCP access, extra model, or `--resume` against another owner.

```bash
reins run --callback-mode codex-queue --task "one minute callback" -- python -c "import time; time.sleep(60); print('ready')"
```

A successful command means queued, not received. The receiving conversation must ACK, and nothing is sent twice automatically. Codex holds the message until a busy conversation finishes its turn; keep the receiving chat open. Reins does not poll conversation state or schedule model wakeups to collect results. A callback still waiting for its ACK does not hold back later callbacks for the same chat; `reins status` lists it.

The ACK runs under the chat's own permissions. If its sandbox cannot write the Reins queue directory (`~/.reins` by default), approve the command or add that directory to the writable roots, otherwise the ACK fails.

If `codex queue` reports an error (no such thread, for example), the callback moves to failed; fix the cause and run `reins retry --id ID`. If the command was killed or its output cannot be confirmed, the outcome is unknown and retry is refused; inspect the chat, then `reins ack` or `reins cancel`.

Verified with macOS Desktop and with real Codex on Linux. Windows Desktop has not been verified. Server CLI auto-routing still uses the existing CLI resume transport. macOS prefers the bundled Codex, while an explicit `REINS_CODEX_BIN` wins. The binary and profile paths are frozen at submission. Update the coordinator with Reins; older coordinators retain records requiring the new `deliver.codex-queue` capability instead of misrouting them.

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

A normally started Desktop does not use this route; use the native queue callbacks above.

An opt-in test uses real Codex 0.166.0 with a temporary profile and local stub model
(no account or remote model) to verify idle/active tool-output delivery and events:

```bash
PYTHONPATH=src:tests REINS_TEST_NATIVE_CODEX_BIN=/absolute/path/to/codex \
  python -m unittest test_native_core_integration -v
```

Protocol: [Codex App Server](https://learn.chatgpt.com/docs/app-server).

## Upgrading from the old launcher

Quit Desktop and open it the normal way. `reins setup` no longer writes `Start Reins Desktop`, and an old copy only prints this advice, so you can delete it. Callbacks need nothing else.

A callback saved with `--callback-mode desktop` keeps its result. On macOS and Windows it stops in failed with the exact command to move it:

```bash
reins retry --id ID --callback-mode codex-queue
```

The task does not run again; the same result goes to the same chat. Run it from a shell that can reach the Codex you use, because the executable and profile are frozen at that point. If the old delivery may have been sent, retry is refused: inspect the chat, then `reins ack` or `reins cancel`. On Linux these records keep delivering through the local App Server socket, and the same command moves them if that socket is gone.

`--callback-mode desktop` cannot be selected for new work; the error points to `codex-queue`.
