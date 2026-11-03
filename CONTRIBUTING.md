# Contributing to Harness Reins

Reins delivers results back to live agent sessions on Linux, macOS and Windows.
Most serious bugs here only appear with a real harness, a real OS service or a
failure halfway through, so this guide asks for more evidence than a passing
unit test.

## Before you start

- **Small fix:** open a PR directly.
- **New behaviour, a new record field or a new dependency:** open an issue first
  so the design can be agreed before you write it.
- **Link work (peers, ports, remote start):** read
  [the Link protocol](docs/design/link-protocol.md), especially its direction in
  section 1.1, and [the threat model](docs/design/link-threat-model.md), then take
  a numbered PR from section 12. Messaging on one machine is already released, and
  multi-machine work builds on it. If the design is wrong, change the document in
  your PR rather than diverging from it in code.

## Set up and test

Reins supports Python 3.9 and later, so avoid syntax that needs a newer version.
PEP 701 f-strings are a common trap.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
```

Always run the tests against an isolated home:

```bash
env -u XDG_RUNTIME_DIR -u DBUS_SESSION_BUS_ADDRESS -u CODEX_THREAD_ID -u CODEX_HOME \
  HOME="$(mktemp -d)" PYTHONPATH=src python3 -m unittest discover -s tests -q
```

Without this, tests can see your installed Reins, report false failures and leave
test callbacks in your real queue at `~/.reins/queue`. The same applies when
you run them from inside an agent session.

Integration tests are opt-in with `REINS_TEST_SCREEN=1`, `REINS_TEST_SYSTEMD=1`,
`REINS_TEST_LAUNCHD=1` or `REINS_TEST_WINDOWS_SCHEDULER=1`:

```bash
python3 -m unittest discover -s tests -p 'test_*integration.py' -v
```

For documentation changes, also run:

```bash
python -m pip install -r docs/requirements.txt
python scripts/docs_sync.py check
mkdocs build --strict
```

## Rules the code must follow

**Layers.** Imports only point down, and `tests/test_architecture.py` enforces it:

```text
cli -> commands -> install -> tower -> engine -> harness, owners, store -> core, system
```

Restructure the code instead of adding an exception. `KNOWN_VIOLATIONS` may only
shrink.

**Know who writes what.** Section 8 of the Link protocol lists who writes each
file and under which lock. The Link service never writes `queue/**`.

**Never guess a target.** If a session, name or route cannot be resolved, refuse
with a clear error.

**State what is known.** "Published" is not "read", and an ACK means seen, not
done. An outcome that cannot be determined stays unknown and is not retried
automatically, because delivering twice can be worse than asking.

**Records.** Keep unknown fields when rewriting a record, reject one that lacks a
required field, and leave a record alone if it `requires` a capability this
version does not know.

**Text sent to a model.** Keep envelopes short, and never let data from a peer,
task or log change their structure or the ACK command.

**Dependencies.** Reins has one runtime dependency, PyYAML, and ships as a frozen
binary. A new runtime dependency needs a stated reason and an agreed issue.

**Style.** Match the surrounding code, including its comment density and naming.
Write user-facing docs in terms of what the reader gets. Keep debugging history
out of them.

## What a PR needs

1. One purpose. Refactors and behaviour changes go in separate PRs.
2. Tests that fail without the change.
3. Failure cases, not only the normal path. For delivery and Link changes, cover
   the relevant ones: a dropped connection, a repeated frame, a restart halfway
   through, a closed session and a peer on another version.
4. **Real harness evidence if you touch a delivery path.** Mocks alone are not
   enough. Say which harness and version you ran, what you did and what happened,
   and say plainly what you could not run.
5. Green CI on Linux, macOS and Windows with Python 3.9 and 3.12. Do not skip or
   weaken a test to get there. If you have to change a test's expectation,
   explain why.
6. An `Unreleased` entry in `CHANGELOG.md` for anything users will notice.
7. Docs in Chinese and English for pages in the public site. `docs_sync.py check`
   enforces this.

AI-assisted contributions are welcome. You are responsible for the code you
submit and must have run what you say you ran.

## Review

`.github/CODEOWNERS` lists the paths that need the maintainer's review. These
include delivery, the tower, storage, install and service code, packaging, CI and
all Link code.

Changes to authentication, pairing, peer permissions, network listeners or remote
start also need a security review against the threat model. Expect those to take
longer, and keep them small.

Only the maintainer tags releases and publishes packages. Please do not bump the
version in a PR.

## Security issues

Do not open a public issue for a vulnerability. See [SECURITY.md](SECURITY.md).

## License

By contributing, you agree that your work is licensed under the
[MIT License](LICENSE), the same as the project.
