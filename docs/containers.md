# Linux containers and AutoDL

Reins 0.7.0 uses GNU screen when a systemd user manager is unavailable. A container
does not need systemd, privileged mode, a host service socket or the Docker socket
to run this backend. Keep Reins, the selected Agent CLI, its session files and the
workload in the **same running container**, under the same user account.

There are two independent choices:

| Setting | What it controls | Container choice |
| --- | --- | --- |
| `--backend` on `run`, `agent` or `setup` | The owner of each workload | `screen` |
| `--service` on `setup` | How the callback coordinator starts | `standalone`, an explicitly selected existing `supervisor`, or run `reins daemon` in the foreground yourself |

`setup --service auto` uses an available systemd user manager, otherwise standalone
mode. It does not automatically modify a platform-managed Supervisor installation.
The selected workload backend is saved in each task; recovery keeps that owner.

For unattended use, configure both startup and recovery rather than relying on a
detached shell. Choose the route that owns your container:

| Environment | Start Reins when the container starts | Restart an exited coordinator |
| --- | --- | --- |
| Existing AutoDL/development instance | Its verified boot hook runs standalone setup | Use an existing Supervisor if this is required |
| Container with an administered Supervisor | A Reins program with `autostart=true` | `autorestart=true`, while Supervisor remains running |
| Docker host you control | Foreground `reins daemon` with `--restart unless-stopped` | Docker restarts the whole container |

These restore the coordinator, not an interrupted training process. Keep the
existing Agent profile, queue, installation and workspace at stable paths.

## Docker image

From the `v0.7.0` release checkout:

```bash
docker build -f examples/docker/Dockerfile -t reins:0.7.0 .
docker run --rm reins:0.7.0 reins --version
docker run --rm reins:0.7.0 screen --version
```

The [example Dockerfile](../examples/docker/Dockerfile) installs Python-based Reins
and screen. It contains **no Agent CLI, model runtime, GPU stack or workload
dependencies**. Extend this image with the Codex or Claude Code CLI and the
dependencies required by your commands before expecting a real callback. The
following example uses `reins-agent:0.7.0` as the name of that prepared image.
Its selected Agent executable must be on the `reins` user's `PATH`.

An existing Debian/Ubuntu Python image can supply the base without changing the
Dockerfile:

```bash
docker build -f examples/docker/Dockerfile \
  --build-arg BASE_IMAGE=your-local-python-image:tag \
  -t reins:0.7.0 .
```

The override requires Python 3.9+ and `apt-get`; Alpine images use different
package tooling and cannot be substituted into this Dockerfile unchanged.
The build context allowlist excludes local profiles, task records, Git history
and committee discussions. Do not add credentials through `COPY`, build arguments
or image environment values.

### Start the coordinator and agent

Choose an existing absolute workspace path on the Docker host. The image uses UID
1000; that user must be able to write the mounted workspace. Adjust the image user
or workspace permissions for your deployment if the host uses a different UID.

```bash
export REINS_WORKSPACE=/absolute/path/to/your/project
docker volume create reins-state
docker run -d --init --name reins-service \
  --mount type=volume,src=reins-state,dst=/state \
  --mount "type=bind,src=$REINS_WORKSPACE,dst=/workspace" \
  reins-agent:0.7.0
docker exec reins-service reins install-skill --target both
docker logs reins-service
```

`--init` adds a small init process to handle child-process reaping. A full systemd
installation inside the container is unnecessary for this arrangement.
[Docker process guidance](https://docs.docker.com/engine/containers/multi-service_container/)

### Enable Docker startup and restart

On the **Docker host**, add `--restart unless-stopped` to the `docker run` above,
or update that existing container without recreating it:

```bash
docker update --restart unless-stopped reins-service
docker inspect --format '{{.HostConfig.RestartPolicy.Name}}' reins-service
```

For Compose, set these keys on the service using the prepared image and the same
state/workspace mounts:

```yaml
services:
  reins:
    # Keep the image, volumes and runtime environment from your deployment.
    init: true
    restart: unless-stopped
    command: ["reins", "daemon"]
```

The host must start Docker itself on boot. `unless-stopped` leaves a manually
stopped container stopped; use `docker start reins-service` to start it again.
To remove automatic restart, use `docker update --restart no reins-service`
(Compose: `restart: "no"`). This does not stop the current container.
[Docker restart policy reference](https://docs.docker.com/engine/containers/start-containers-automatically/)

This route restarts the entire container if its main coordinator exits, ending
any screen workloads. Use the Supervisor route below when only the coordinator
should restart inside a still-running container. A rental instance without access
to its Docker host should use its platform startup mechanism instead.

### Use the Agent inside the container

Authenticate your selected Agent interactively **inside this container**, using
its supported login method. Its configuration and session directories are runtime
volume paths: `/state/codex` for `CODEX_HOME`, `/state/claude` for
`CLAUDE_CONFIG_DIR`. Existing dedicated profiles can instead be mounted at those
locations when the Agent supports that arrangement. They need write access for
session updates. Supply API credentials at container startup through your runtime
credential mechanism if the Agent uses environment-based authentication; the
coordinator also needs access when it launches a callback. Credentials supplied
only to one `docker exec` shell do not become the coordinator's environment.

Open the installed CLI in the container, for example:

```bash
docker exec -it --workdir /workspace reins-service codex
# Or, when that is the CLI installed in your image:
# docker exec -it --workdir /workspace reins-service claude
```

From that Agent's shell/tool context, submit the actual workload:

```bash
reins run --backend screen --cwd /workspace \
  --task "train experiment" -- python /workspace/train.py
```

The launching Agent normally supplies its session identifier. An unrelated
`docker exec` shell does not inherit the Agent's session; explicitly pass
`--agent codex --session <actual-container-session-id>` when submitting there.
Do not substitute a host desktop session ID: the container CLI must be able to
resume the bound session itself. The example disables Codex Desktop socket
delivery and uses the CLI callback path within the container.

### What persists

| Runtime path | Content |
| --- | --- |
| `/state/reins/` | Queue, task metadata, logs, compact callback details, ACKs, goals and reminder settings |
| `/state/codex/` and `/state/claude/` | Agent profiles, credentials and session history stored by the installed CLI |
| `/workspace/` | Your code, outputs and checkpoints |

The state volume and workspace mount must remain available at the same container
paths while tasks are active. Keep only one coordinator per queue. A named volume
outlives a container unless you explicitly remove it; a bind mount exposes the
chosen host directory to the container.
[Docker volumes](https://docs.docker.com/engine/storage/volumes/),
[bind mounts](https://docs.docker.com/engine/storage/bind-mounts/)

The image runs `reins daemon` in the foreground. **If that main service exits, the
container stops and its running tasks stop too.** Screen cannot outlive its
container. If coordinator restarts must leave workloads running, use your existing
Supervisor/process manager as the container's main service and let it manage
`reins daemon` as a child. Restart only that coordinator child; stopping the whole
container still ends all workloads. Do not use `reins setup --service standalone
--now` as a container's sole `CMD`: setup returns after starting its background
process, which ends the container's main command.

### Claude sessions and `reins tui` in a container

A callback for a closed Claude Code session brings it up as a Claude Code background session, as on the host. No
systemd is needed: Claude's own background daemon detaches from the process that started it, so restarting the
coordinator does not stop it. Stopping the container does. Keep `--init` so finished background processes are reaped.

Claude Code starts a background session only in a directory it trusts. In a new container, open `claude` once in the
project directory and accept the trust prompt; until then Reins resumes that session headless, as before. The trust
record lives in `.claude.json`: `$CLAUDE_CONFIG_DIR/.claude.json` when that variable is set (as in the image above,
under the state volume), otherwise `~/.claude.json` in the home directory, outside `~/.claude`. Persist it with the
rest of the Claude profile, or every new container asks again.

Open the terminal UI with a TTY:

```bash
docker exec -it --workdir /workspace reins-service reins tui
```

`Alt+d` leaves it running; the next `docker exec -it ... reins tui` comes back to the same panes. The npm package
includes Zellij; with a pip install, put a `zellij` binary on `PATH` or point `REINS_ZELLIJ` at one. The panel's top
line shows what `reins doctor` finds wrong, such as a coordinator that is not running.

## AutoDL or an existing Linux development container

Use the environment already provided by the platform; a second Docker daemon is
not required. Install screen with the image's package manager and install Reins into
the Python environment you intend to keep available:

```bash
# Debian/Ubuntu-based image, as its administrator:
apt-get update
apt-get install -y screen

# In the selected Python environment:
python -m pip install "git+https://github.com/lz59970062/harness-reins.git@v0.7.0"
reins --version
screen --version
```

Select a directory on storage that your platform retains. Replace the example
path below with that actual path; Reins does not determine the retention policy of
an AutoDL disk or rental instance. Choose the Agent profile locations **before
opening the session**. If you already have persistent profiles, keep their current
paths instead; exporting a new path does not copy old sessions or authentication.

```bash
export REINS_PERSIST_ROOT=/absolute/path/to/persistent-disk/reins
mkdir -p "$REINS_PERSIST_ROOT"
export CODEX_HOME="$REINS_PERSIST_ROOT/codex"
export CLAUDE_CONFIG_DIR="$REINS_PERSIST_ROOT/claude"
export REINS_EXECUTION_BACKEND=screen
export REINS_DESKTOP_APP_SERVER=0

# Install the selected Agent CLI and make its authentication available first.
reins setup --backend screen --service standalone --now
reins status
```

Keep these environment settings consistent in the Agent and coordinator startup
environments. A detached standalone daemon continues after the launching shell
closes while the platform keeps the container alive. It is **not** a boot service
or a crash supervisor. Register one of the startup routes below to restore it
after the platform/container starts. Until then, restore the same environment and
run `reins setup --backend screen --service standalone --keep-skill --now` manually.
Its log is
`$REINS_HOME/daemon.log`.

Standalone coordinator PID/runtime files belong to the selected `REINS_HOME`.
Use a separate `REINS_HOME` for an isolated installation or another standalone
coordinator, and start its Agent with that same `REINS_HOME`. Merely changing
`--queue-dir` does not isolate those coordinator files.

### Give startup the same environment as the Agent

Startup hooks and Supervisor do not necessarily load `.bashrc`, activate Conda,
or load NVM. In the working Agent environment, locate the installed executables:

```bash
command -v reins
command -v codex
command -v node
command -v screen
```

For a root-owned AutoDL instance, save the following wrapper as
`/root/.local/bin/reins-container` (create its parent directory first). **Edit its
paths before using it**: the Python environment, Agent runtime directory and
workspace are examples. For another user, use that user's directories and account.
Keep the profile paths of your existing Agent sessions; do not migrate them just
to match the example. The Agent runtime directory must contain the selected CLI
and its runtime, such as Node for an npm-installed Codex.

```sh
#!/bin/sh
set -eu
export PATH="/root/miniconda3/bin:/absolute/path/to/agent-runtime/bin:/usr/local/bin:/usr/bin:/bin"
export CODEX_HOME="/root/.codex"
export CLAUDE_CONFIG_DIR="/root/.claude"
export REINS_HOME="/root/.reins"
export REINS_QUEUE_DIR="/root/.reins/queue"
export REINS_EXECUTION_BACKEND=screen
export REINS_DESKTOP_APP_SERVER=0
cd /root/harness-reins
exec /root/miniconda3/bin/reins "$@"
```

Set `REINS_QUEUE_DIR` to your actual existing queue, including
when it differs from the example. Use the same setting in the Agent environment;
the hook and Supervisor below both inherit it from this wrapper. Keep one queue
per profile.

Make it executable, check its syntax, and prepare Reins once without starting a
second coordinator:

```bash
chmod 700 /root/.local/bin/reins-container
sh -n /root/.local/bin/reins-container
/root/.local/bin/reins-container --version
/root/.local/bin/reins-container setup --backend screen --service standalone \
  --skill-target codex --keep-skill
```

For Claude, use its existing profile and `--skill-target claude` (or `both`).
If authentication needs environment variables, provide them through a private
runtime environment available to the startup process; shell-only exports will
not reach an already-running Supervisor. For Reins proxy settings, add
`--inherit-proxy` or `--proxy-env-file /absolute/path/to/private-proxy.env` to the
one-time setup. Reins loads its saved proxy file when the daemon starts. Keep
credentials out of images, shared startup scripts and committed configuration.

### AutoDL: register a verified boot hook

First identify what your instance actually executes on each boot: an image startup
script, a platform boot-hook setting, or an existing process manager. Some images
use `/root/autodl.sh`; inspect the image's startup configuration before using that
path. Creating the file alone does not register a boot hook, and `.bashrc` only
runs for certain shells rather than on container startup.

AutoDL private-cloud administrators can configure per-boot scripts in their
[instance customization settings](https://private.autodl.com/docs/practice_6/).
That is a private-cloud facility, not a promised setting for every public rental
instance. If the platform provides no editable boot hook, use the existing
Supervisor route with its administrator or retain manual startup.

In a confirmed **one-shot boot hook**, preserve its existing commands and add:

```bash
mkdir -p /root/.reins
/root/.local/bin/reins-container setup --backend screen --service standalone \
  --skill-target codex --keep-skill --now \
  >> /root/.reins/startup.log 2>&1
```

Use the actual profile path for the log directory. Place this where the hook runs
on every boot, before any `exit` or `exec` that would skip it. Back up the existing
hook before editing. The hook may return after setup because the platform keeps
this development container running. Do not register it simultaneously with a
Supervisor program for the same queue, or run it on every terminal login.

An AutoDL **elastic deployment startup command** instead controls the container's
lifetime. For that route, after the one-time setup use the foreground command:

```bash
/root/.local/bin/reins-container daemon
```

Use the platform's restart controls if available; a detached setup command is not
a suitable main command. This distinction follows
[AutoDL's deployment startup guidance](https://www.autodl.com/docs/elastic_deploy_practice/).

The one-shot hook provides startup only, not crash recovery. `--enable` does not
change that in standalone mode. Repeating setup preserves an existing skill with
`--keep-skill` and checks queue ownership before requesting a safe reload. A reload
keeps the existing process environment and arguments: changed PATH, profiles or
service options need a controlled coordinator restart after deliveries drain.
If setup reports a deferred reload during active delivery, run it again after
that delivery drains; the setup call does not queue a later reload request.

To disable this boot integration, remove only the Reins invocation from the hook;
preserve the platform's other startup commands. Removing it does not stop the
currently running daemon or delete its records.

### Existing Supervisor: startup and coordinator crash recovery

Use this route only for a Supervisor you administer which the platform already
starts on boot. Locate its actual configuration, include directory and control
socket. Use `supervisorctl -c /actual/path/supervisord.conf status` to confirm you
are talking to that instance; an installed `supervisorctl` alone proves nothing.

After the one-time setup above, add the following program to that instance's
include directory. This example uses the same root-owned wrapper; other users
must set their matching `user`, paths and directory. The log directory must exist
and be writable by that user.

```ini
[program:reins]
command=/root/.local/bin/reins-container daemon
directory=/root/harness-reins
user=root
autostart=true
autorestart=true
startsecs=2
startretries=3
stopsignal=TERM
stopasgroup=true
killasgroup=true
redirect_stderr=true
stdout_logfile=/root/.reins/supervisor.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
```

The wrapper pins profile and PATH values for the daemon; Supervisor's `user=`
changes the execution identity without loading that user's login environment.
Keep Reins in the foreground under Supervisor.
[Supervisor environment and process rules](https://supervisord.org/subprocess.html)

Configure one coordinator per queue. Before switching an existing standalone
installation, drain active callback deliveries and stop only its verified
coordinator, or schedule the switch for the next planned container start. Disable
the old boot invocation as part of the switch. Then load only the Reins program,
replacing the configuration path below with the one you inspected:

```bash
supervisorctl -c /actual/path/supervisord.conf reread
supervisorctl -c /actual/path/supervisord.conf update reins
supervisorctl -c /actual/path/supervisord.conf status reins
```

`update reins` applies changes to that program; `restart reins` alone does not reread
configuration. Avoid restarting all platform services.
[Supervisor control commands](https://supervisord.org/running.html)

If status becomes `BACKOFF` or `FATAL`, inspect the program log and correct the
path, environment or permissions before starting it again. `startretries` is
bounded; automatic restart is not a substitute for fixing startup errors.

Reins also provides `setup --service supervisor --now` for an administered default
Supervisor layout. In 0.7.0 it uses bare `supervisorctl`, can start `supervisord`
if connection fails, and runs an unscoped `update`. It does not expose a
`supervisorctl -c` option. `REINS_SUPERVISOR_CONF_DIR` changes only
where the program file is written. For a platform-managed/custom layout, use the
explicit configuration above instead. The generated program also does not pin
custom Agent profile variables; the wrapper supplies them explicitly.

To remove this integration after deliveries drain, stop only `reins` with the same
`supervisorctl -c ... stop reins`, remove its program file, and run `reread` and
`update reins`. Keep the queue and Agent profiles. Stopping the whole container
still terminates all screen workloads; Supervisor restart settings cannot prevent
that.

### Verify startup before relying on it

Check the selected hook/program and its logs. For the readiness check, use the
actual Agent session in that container. An independent shell or `docker exec`
does not inherit its session ID; set `REINS_SESSION_ID` to that real ID first:

```bash
# Replace this value with your actual container Codex session ID.
REINS_SESSION_ID='replace-with-your-actual-container-session-id'
/root/.local/bin/reins-container doctor --backend screen \
  --agent codex --session "$REINS_SESSION_ID"
/root/.local/bin/reins-container status
```

For Docker, pass the actual container session ID from the host shell:

```bash
docker exec reins-service reins doctor --backend screen \
  --agent codex --session "$REINS_SESSION_ID"
docker logs --tail 50 reins-service
```

For Claude, select `--agent claude` and its actual session ID. From an Agent's own
tool environment, the session can be detected automatically. A `session_unbound`
issue means the check lacks that binding, not that the daemon failed to start.
An idle daemon may have no screen session.
`doctor` checks local readiness; it does not prove model authentication or delivery.

During a planned idle restart, check that the coordinator starts **before** any
interactive login runs setup. Do not reboot a busy instance just to test this.
Then, from an actual Agent session in that container, submit a short smoke task:

```bash
reins run --backend screen --task "startup callback check" -- \
  python -c 'import time; time.sleep(5); print("startup callback check passed")'
```

Confirm one execution, the original session's receipt and inspection, its ACK, and
the callback's `done` record. Container recreation may change identity even with
the same mounts; inspect old records rather than treating autostart as permission
to replay interrupted work or redirect its callback.

## Stop, restart and callback boundaries

- Closing an interactive shell does not stop screen-owned tasks while their
  container and process manager stay alive.
- Stopping, replacing or deleting the container interrupts its processes. Saved
  files do not preserve a running process. Inspect the recorded result and
  checkpoints before submitting new work; Reins does not automatically rerun an
  interrupted business command.
- Recreating a container can change its host/container identity and the available
  Agent sessions, even with the same volume. Reins can classify its old task records
  as belonging to another host. Persistent files enable inspection, but do not
  establish cross-container or cross-host callback recovery.
- Keep the original bound Agent session and its files accessible. A new login or
  a new conversation is not permission to redirect an old callback.
- The Linux container path does not establish native macOS or Windows support.
  GPU access and checkpointing belong to your workload/container configuration.

The Dockerfile and these commands are deployment examples. A working image build
or CLI version check does not verify real Agent authentication and original-session
callback delivery; validate that complete path in the prepared container before
relying on it for unattended work.
