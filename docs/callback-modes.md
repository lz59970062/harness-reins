# Callback modes

Task execution and callback delivery are separate capabilities. Native task
owners still run commands independently on Linux, macOS and Windows. Returning
to a conversation also requires a connection to its current owner. A session ID,
an installed CLI, or an idle Agent turn does not prove that connection exists.

## Supported routes and limits

| Selection | Behavior | Acceptance requirement |
| --- | --- | --- |
| `--callback-mode auto` (default) | Recognizes the current `Codex Desktop` origin marker and selects `codex-queue`; otherwise selects `cli` | If an integration omits the marker, select the mode explicitly. Historical session `source` is not current ownership. |
| `--callback-mode cli` | Uses the existing local App Server when available, otherwise the bound Agent's CLI resume | Real original-session receipt/ACK must be verified for that CLI configuration; an unrelated Core may still own the session. |
| `--callback-mode codex-queue` | Publishes once through the public `codex queue` command; the running owner picks it up. See [Codex Desktop](integrations/desktop.md) | The receiving chat must ACK under its own permissions. Queued is not received. |
| `--callback-mode desktop` | Retired; new work cannot select it. Saved records can move with `reins retry --id ID --callback-mode codex-queue` | See [Moving saved Desktop callbacks](#moving-saved-desktop-callbacks). |
| `--callback-mode manual` | Runs the workload and saves a callback for later inspection; never automatically starts an Agent | User or original Agent reads results and ACKs receipt. A file by itself does not wake the Agent. |

PI Agent callbacks always use CLI resume: delivery runs
`pi --print --session <session-file>`, and the Codex-only modes are rejected
before admission.

For a new `run` or `agent`, a blocked preflight refuses admission before creating
the task. The user can repair the route or explicitly choose `manual`. `done`
reports work that already happened, so it preserves the callback even if the
connection is currently unavailable. Do not silently choose manual or force CLI
mode to conceal a failure.

The mode is frozen with the task. Each delivery checks the live peer, so another
profile or an unavailable socket cannot authorize sending a prompt elsewhere.

## Moving saved Desktop callbacks

A normally started Codex Desktop needs no launcher: `auto` selects `codex-queue`,
and `reins setup` no longer generates `Start Reins Desktop`. A callback saved with
`--callback-mode desktop` keeps its result. On macOS and Windows it stops in
`failed/` with `delivery_state: blocked`, and `last_error` names the command:

```bash
reins retry --id <callback-id> --callback-mode codex-queue
```

The workload does not run again. Linux records keep delivering through the local
App Server socket, and the same command moves them if that socket is unavailable.
See [Codex Desktop](integrations/desktop.md).

## Readiness and recovery

`reins doctor` reports `checks.callback` and a separate `callback` object:

- `owner_reachable`: a read-only query observed the bound thread in this Core.
- `unverified`: CLI delivery may be available, but no original-session delivery
  was proved. An absent loaded-thread entry does not mean the thread is free.
- `blocked`: the connection or its ownership was not verified.
- `manual`: automatic delivery was explicitly disabled.

The probe only initializes a client and reads `thread/loaded/list`; it never
resumes a thread or starts a model turn. `end_to_end_verified` remains false:
only actual receipt and ACK establish a successful callback. A tool sandbox may
deny socket/process access; retry the check through an authorized ordinary-user
execution context rather than weakening peer checks.

A definite App Server writer conflict or explicit bridge failure is retained in
`failed/` with `delivery_state: blocked`. It does not spend retries attempting a
second writer. Other transient failures retain the existing retry policy.
Unknown submission outcomes retain their cross-queue lease and cannot be retried
through the ordinary recovery command.
When the owning Core reports an active turn, delivery waits in the pending queue
without consuming its retry budget; it does not interrupt that turn.

After inspecting and repairing the connection, requeue the same callback:

```bash
reins retry --id <callback-id>
# Or move it to the native Codex queue:
reins retry --id <callback-id> --callback-mode codex-queue
# Or explicitly put a failed result into the manual inbox:
reins retry --id <callback-id> --callback-mode manual
reins status --state pending --state failed
```

`retry` preserves the ID, original session, prompt and workload artifacts. It
records previous attempts/errors and never executes the business command again.
Acknowledged, canceled, actively delivering and ambiguous callbacks are refused.
Manual receipt uses the emitted ACK after inspecting the result in its bound
session. This confirms receipt, not autonomous transport acceptance or goal completion.

## Upgrade and acceptance

New tasks use record version **3**, and callbacks with explicit modes use version
**2**. Upgrade the coordinator before submitting them. Older coordinators reject
these versions instead of silently ignoring their callback intent. Old task and
callback versions remain readable. Do not downgrade a queue containing new work.

Acceptance is by OS **and connection**, not OS alone: CLI, the native queue,
restart, owner change, lost response, and duplicate receipt.

See [Codex Desktop](integrations/desktop.md) for the default Desktop route.
