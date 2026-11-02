# Installation

Prepare Python 3.9 or later, Git, and an Agent CLI that you have signed into. Run Reins and the Agent under the same user account.

## Install the stable version

These commands install `0.7.0`. Keep the virtual environment after installation: it holds Reins and its dependencies.

### Linux / macOS

```bash
git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/harness-reins.git
cd harness-reins
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
reins setup --enable --now
reins --version
```

### Windows PowerShell

```powershell
git clone --branch v0.7.0 --depth 1 https://github.com/lz59970062/harness-reins.git
Set-Location harness-reins
py -3 -m venv .venv
& .\.venv\Scripts\python.exe -m pip install .
& .\.venv\Scripts\reins.exe setup --service windows-task --enable --now
& .\.venv\Scripts\reins.exe --version
```

`setup` installs the Agent skills and configures the background coordinator. The last command prints the installed version. On Windows, you can continue using the full path to `.venv\Scripts\reins.exe`.

## Choose your runtime

| Environment | Task owner | Setup |
| --- | --- | --- |
| Linux desktop or server | systemd user service | Use the default `setup` |
| macOS desktop login session | launchd | Run `setup` in the current user's terminal |
| Windows 10 or later | Task Scheduler and Job Object | Keep the user logged in and use a local NTFS directory |
| Docker, AutoDL, or Linux with screen | GNU screen | Follow the container steps below |

In a Debian or Ubuntu container, install screen and start the coordinator:

```bash
sudo apt-get install screen
reins setup --backend screen --service standalone --now
```

Place Reins, the Agent, the working directory, and the session files in the same runtime environment. Mount `~/.reins`, the Agent configuration directory, and the project directory on persistent volumes to retain task records across restarts. See the [container deployment notes](https://github.com/lz59970062/harness-reins/blob/main/docs/containers.md) for images and startup configuration.

## Install the preview

The `0.7.1` preview includes Claude live callbacks and Pi integration. Install the current `main` branch in a new directory:

```bash
git clone https://github.com/lz59970062/harness-reins.git reins-preview
cd reins-preview
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
reins setup --force --enable --now
reins --version
```

On Windows, use the PowerShell steps above and remove `--branch v0.7.0 --depth 1` from the clone command.

When upgrading an existing installation, finish the queued tasks and callbacks first. Install the new version, then run `reins setup --keep-skill --force --enable --now` to update the coordinator and retain your existing skill content.

Continue with [Your first task](quickstart.md).
