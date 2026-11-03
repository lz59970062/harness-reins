# DeepSeek Harness integration

[← Back to README](../README.md)

Reins works with DeepSeek Harness (`dsh`, the desktop app or the `dsh` CLI) in both directions: DSH
can run as a **child agent** (`reins agent dsh`), and a live DSH session can be the **callback
parent** that Reins wakes when a task finishes.

## Set up

```bash
reins setup --with-dsh --force --enable --now
```

`--with-dsh` installs the focused skill under `${DSH_HOME:-~/.dsh}/skills/harness-reins` and records
the `dsh` launcher for the coordinator. If `dsh` is not on `PATH`, pass it explicitly. On macOS the
desktop app keeps it here:

```bash
reins setup --with-dsh --dsh-bin "/Applications/DeepSeek Harness.app/Contents/Resources/runtime/cli/bin/dsh" \
  --force --enable --now
```

DSH watches `~/.dsh/skills` (and the project's `.dsh/skills`), so a new session sees the skill
without a restart. Check readiness with:

```bash
reins doctor --agent dsh --session "$DSH_SESSION_ID"
```

## DSH as the callback parent

Inside DSH's Bash tool, `reins run` binds the callback to `DSH_SESSION_ID` automatically:

```bash
reins run --cwd "$PWD" --task "train model" -- python train.py
```

stderr prints the exact wait command:

```text
reins: DeepSeek Harness live callback: run as a background job (run_in_background: true): reins wait --queue-dir … --task <id>
```

Run that command with the `bash` tool's `run_in_background: true`. DSH keeps the job alive across
turns and notifies this conversation when it exits. `reins wait` prints the callback as soon as it is
queued; read the finished job's output with `job_output`, inspect the result, and ACK.

### Why there is no headless fallback

DSH's one-shot runner (`dsh --profile headless --session-id …`) refuses a session created under the
desktop agent preset:

```text
dsh: session "session-…" runs under agent preset "standard", which the one-shot runner does not compose
```

A second writer would also race the live DSH writer lock. Reins therefore **never resumes a DSH
session headlessly**:

- While a `reins wait` is alive, the daemon leaves the session's callbacks to it.
- With no waiter, a DSH callback stays queued until the bound session runs `reins wait` again.
  `reins status` still lists it.
- Wait exit code 4 (callback diverted to a headless resume) never occurs for DSH; exit code 3 means
  the wait timed out and the callback is still queued.

This is the same guarantee as an ordinary Pi session: delivery happens only while the original
process is alive, and Reins never guesses a replacement owner. A one-shot
`dsh --profile headless` run exits after its turn, so do not submit Reins work from it; use a live
desktop or interactive CLI session.

## DSH as a child agent

```bash
reins agent dsh --cwd "$PWD" --name parser-review -- "Inspect parser.py and report concrete defects."
```

Reins runs `dsh --profile headless` with the prompt on stdin and captures the final answer from
stdout. The headless profile does not use the desktop app's saved sign-in: set `DEEPSEEK_API_KEY`
(or restore stored credentials in the headless profile, as documented by DSH) or the child reports
an authentication error.

The child runs in DSH's default `workspace-write` sandbox with no approval channel, so it can write
only inside `--cwd`. Its result is its final answer (`agent-result.txt`); DSH children write no
`report.json`.

To drive DSH with your own endpoint, declare the route in `~/.dsh/cordis.patch.yml`. It applies to
every profile; `acp` selects its model separately:

```yaml
- id: llm-pi-ai
  config:
    providers:
      my-gateway:
        apiKeyEnv: MY_GATEWAY_API_KEY
        api: openai-completions
        baseURL: https://gateway.example/v1
        models:
          - id: my-model
- id: agent-default-model
  config: {provider: my-gateway, model: my-model}
- id: acp
  config: {provider: my-gateway, model: my-model}
```

| Option | DSH behavior |
| --- | --- |
| `--model` | unsupported: the compose profile selects the model |
| `--reasoning-effort` | unsupported |
| `--system-prompt-file` | unsupported |
| `REINS_DSH_BIN` / `setup --dsh-bin` | select the `dsh` launcher |

Child commands strip `DSH_SESSION_ID`, `DSH_SHELL` and `DSH_WEB_URL`, so the child cannot bind to the
parent session. They keep `DEEPSEEK_API_KEY`, `DSH_HOME` and `DSH_PROFILE`, which select the child's
own configuration and credentials.

## Sandbox

In DSH's default `workspace-write` mode `~/.reins` is read-only to the session, so `reins run`, `wait`,
`ack`, `session register`, `send` and `reply` are denied until the model retries them with
`sandbox_permissions`. The skill tells it to, and each retry asks for approval once. Task processes
themselves are not confined, because the OS task owner starts them.

## Platform status

| Platform | Real DSH verified |
| --- | --- |
| macOS | Desktop app: submit, job notification, ACK |
| Linux | `dsh` 0.2.1-rc.5, ACP and web UI: submit, job notification, ACK; ACP also: child agent, messaging between two sessions |
| Windows | Not yet: unit tests only |

On Windows, DSH's shell tool is `pwsh`: use `run_in_background` on that tool and `$env:DSH_SESSION_ID`
in place of `$DSH_SESSION_ID`.

## Verification

The adapter and live-wait paths have unit coverage in `tests/test_dsh.py`:

```bash
python -m pytest tests/test_dsh.py
```

The end-to-end path is: submit a managed task from a DSH session, start the printed `reins wait`
as a background job, let the task finish, call `job_output`, then ACK. Do not substitute a headless
resume for that test.
