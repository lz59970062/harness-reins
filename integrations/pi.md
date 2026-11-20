# Pi

Pi can receive results for its own tasks and work as a child Agent on independent assignments.

## Install the extension

Follow the [installation guide](../installation.md), then run:

```bash
reins setup --with-pi-extension --enable --now
```

Run `/reload` in an existing ordinary Pi session, or restart Pi. The extension and skill are installed in the Pi configuration directory, which defaults to `~/.pi/agent`.

## Submit from Pi

Ask Pi to run:

```bash
reins run --cwd "$PWD" --task "Prepare the data" -- python prepare_data.py
```

Reins binds the task to the session named by `PI_SESSION_FILE`. When the result arrives, the extension delivers the callback into that Pi process. An idle Pi starts handling it immediately; a busy Pi receives it after the current tool work finishes.

After reading the artifacts, ask Pi to execute the callback's ACK command.

## Launch Pi through Reins

To allow session recovery after Pi closes, start a managed session:

```bash
reins pi --cwd "$PWD"
```

To continue an existing session, provide its absolute path:

```bash
reins pi --session /absolute/path/to/session.jsonl
```

## Give work to Pi

Submit from the original Agent conversation:

```bash
reins agent pi --cwd "$PWD" --task "Review data preparation" -- "Read prepare_data.py. Check missing values, duplicate records, and output formats. Report specific issues."
```

The answer is saved in the Reins task directory. Its path appears in both the submission output and callback. See the [Pi implementation notes](https://github.com/lz59970062/harness-reins/blob/main/docs/pi.md) for model options, system prompts, and session recovery.
