# Pi Agent integration

[← Back to README](../README.md)

Reins works with the Pi coding agent (`pi`) in both directions: Pi can
run as a **child agent** (`reins agent pi`), and a Pi session can be the **callback parent** that Reins
wakes when a task finishes. Verified with Pi 0.87.1 on Linux; native Windows/macOS Pi is untested.

## Set up

```bash
reins setup --with-pi-extension     # daemon + skills for Codex, Claude Code and Pi + Pi extension
# or, for Pi only:
reins install-pi-extension          # extension + Pi skill in ${PI_CODING_AGENT_DIR:-~/.pi/agent}
```

Then `/reload` or restart any running Pi. The Pi skill (`skills/harness-reins/`) is a focused
`SKILL.md` about steered callbacks plus the shared reference as `REFERENCE.md`; Codex and Claude
Code skills do not carry Pi-session instructions. If you previously linked the Codex skill into
`~/.agents/skills/`, remove that link so Pi does not see two `harness-reins` skills.

## Pi as the callback parent

Inside Pi's shell, `reins run` binds the callback to the absolute `$PI_SESSION_FILE` (partial IDs
are rejected):

```bash
reins run --cwd "$PWD" --task "train model" -- python train.py
```

When the task finishes, the daemon publishes the callback to the session's private mailbox and the
extension hands it to Pi with `pi.sendUserMessage(..., {deliverAs: "steer"})`:

- **Busy Pi** gets it after the current tool-call batch, before the next model request — the run
  does not have to end. Steering never aborts a running tool, so keep foreground commands short.
- **Idle Pi** starts a normal turn.
- It continues the current branch in the same process; no second writer opens the JSONL.

### Delivery does not block the daemon

Publishing to the live mailbox hands the callback to Pi. The daemon returns immediately (worker
result 122, "published; awaiting ACK") and keeps launching tasks and delivering other sessions'
callbacks. Until the ACK, the session's retained target lease keeps that session's later
callbacks in order. `reins ack` finalizes it directly.

Each daemon loop also checks every published, unacknowledged callback. It is moved to `failed`
for manual recovery — never replayed automatically — when:

| Condition | Meaning |
| --- | --- |
| the receiving Pi exited or closed its mailbox | it can no longer ACK |
| the session was reopened by another process | the published envelope belongs to the old owner |
| `--resume-timeout`, or 1 h if none is set, passed since publication | no ACK in time |

If the extension had already seen the callback in the session (`session_observed`), order is kept
and nothing needs replaying, so later callbacks for that session continue; each names the
un-ACKed one until it is ACKed. Otherwise the session's later callbacks wait behind it. The same
applies to a managed offline resume that ends without an ACK: when the callback is in the session
file, later callbacks continue; when it is not, the session waits for manual recovery.

`last_error` records whether Pi had already admitted the message. Inspect the session, then
`reins ack <id>` if it was handled, or `reins retry --id <id>` to deliver it again.

### Ordinary and managed Pi

- **Ordinary Pi** (`pi` with the extension installed) receives callbacks only while it runs.
  Callbacks for a closed session are blocked until it is reopened; then `reins retry --id <id>`.
- **Managed Pi** adds offline recovery:

  ```bash
  reins pi --cwd "$PWD"                                   # new persistent managed session
  reins pi --session /absolute/session.jsonl -- --model provider/model
  ```

  The launcher locks the session file before Pi opens it and holds the lock for Pi's lifetime.
  Only after the verified owner has fully exited may the delivery worker take the same lock and
  run `pi --print --session <file>`. Managed sessions are pinned: restart `reins pi` to switch, fork
  or reload resources (`/reload` exits managed Pi rather than risk losing the pin). The advisory
  lock coordinates Reins writers; independently launched Pi processes do not honor it.

Callbacks freeze their Pi profile (`PI_CODING_AGENT_DIR`) and channel root
(`REINS_PI_CHANNEL_ROOT`). Check readiness with:

```bash
reins doctor --agent pi --session "$PI_SESSION_FILE"
```

It reports mailbox registration or recovery eligibility, not verified receipt. Mailbox
publication, admission and session observation never replace the ACK.

## Pi as a child agent

```bash
reins agent pi --cwd "$PWD" --task "review parser edge cases" \
  -- "Inspect parser.py and its tests. Report concrete defects and suggested fixes."
reins doctor --operation agent --agent-worker pi
```

Reins runs `pi --print --mode text --no-session` with the prompt on stdin and saves stdout to the
managed private result file; no Pi session is persisted. The child inherits the submission-time Pi
profile, authentication, API, proxy and extension configuration; Reins removes Codex/Claude parent
markers plus `PI_SESSION_ID`, `PI_SESSION_FILE` and `PI_CODING_AGENT`. Tool permissions and project
extension trust follow Pi's own non-interactive configuration; Reins does not add `--approve`.

| Option | Pi behavior |
| --- | --- |
| `--model provider/model` | passed to Pi |
| `--reasoning-effort` | mapped to `--thinking`: `off`, `minimal`, `low`, `medium`, `high`, `xhigh`, `max` |
| `--system-prompt-file FILE` | Pi only; snapshotted privately at submission and passed to `--system-prompt` |
| `REINS_PI_BIN` / `setup --pi-bin` | select the Pi executable |

A task template can set Pi defaults:

```yaml
pi:
  model: provider/model
  reasoning_effort: high
  system_prompt_file: prompts/system.md   # relative to the YAML file
```

See [template registration](template-registration.md) to make a template discoverable as an
`reins-NAME` skill, and [examples/prompts/pi-system.md](../examples/prompts/pi-system.md) for a
sample system prompt.

## Tests

`tests/test_pi_live_integration.py` drives real Pi against an isolated loopback model fixture (no
paid model calls). Run it with `REINS_TEST_REAL_PI=1` and `pi` on PATH.
