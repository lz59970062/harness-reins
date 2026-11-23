#!/usr/bin/env bash
# Smoke-test the packages from build-native.sh on a disposable macOS or Windows CI runner:
# the launcher finds the binary, bundled resources load, and a task owned by launchd or
# Task Scheduler sends its callback to a fake claude, which acknowledges it.
# It runs `reins setup` against the real home directory, so it refuses to run outside CI.
#
#   packaging/npm/smoke-native.sh darwin-arm64
set -euo pipefail

if [[ ${CI:-} != true ]]; then echo "smoke-native.sh changes the home directory; it runs only on CI runners" >&2; exit 2; fi
platform=$1
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
out=$root/packaging/out
native() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
step() { printf '\n=== %s\n' "$*"; }
python_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml")
npm_version=$(sed -E 's/a([0-9]+)$/-alpha.\1/; s/b([0-9]+)$/-beta.\1/; s/rc([0-9]+)$/-rc.\1/' <<<"$python_version")
build=$(git -C "$root" rev-parse --short=12 HEAD)

step "the launcher finds the platform package"
# The layout npm creates; a registry install is tested on Linux (smoke-test.sh).
prefix=$(mktemp -d)
mkdir -p "$prefix/node_modules/harness-reins" "$prefix/node_modules/@harness-reins/$platform"
tar -xzf "$out/npm/harness-reins-$npm_version.tgz" -C "$prefix/node_modules/harness-reins" --strip-components 1
tar -xzf "$out/npm/harness-reins-$platform-$npm_version.tgz" -C "$prefix/node_modules/@harness-reins/$platform" --strip-components 1
launcher=$(native "$prefix/node_modules/harness-reins/bin/reins.js")
reins() { node "$launcher" "$@"; }
version=$(reins --version)
echo "$version"
[[ $version == *"$python_version"* && $version == *"$build"* ]] || { echo "FAIL: expected $python_version build $build"; exit 1; }

step "bundled resources load from the frozen binary"
export CLAUDE_CONFIG_DIR=$(native "$(mktemp -d)") CODEX_HOME=$(native "$(mktemp -d)") REINS_DESKTOP_APP_SERVER=0
reins install-skill --target claude
ls "$CLAUDE_CONFIG_DIR"/skills/*/SKILL.md
reins template list >/dev/null && echo "ok: templates readable"

case $platform in
  darwin-*)
    step "reins tui ships its Zellij and Textual"
    zellij=$prefix/node_modules/@harness-reins/$platform/dist/reins/zellij
    "$zellij" --version
    test -f "$(dirname "$zellij")/ZELLIJ-LICENSE.md"
    reins tui --help >/dev/null
    reins _tui-panel --help >/dev/null
    echo "ok: $("$zellij" --version) and the panel entry point are in the package" ;;
esac

step "end-to-end: OS task owner -> callback delivered to a fake claude"
fake=$(mktemp -d)
# Stands in for `claude -p --resume ... --input-format stream-json`: record what reins delivered,
# then, like a real agent, run the ACK command the callback names.
cat >"$fake/fake-claude.js" <<'JS'
const fs = require("fs");
const { execSync } = require("child_process");
const log = require("path").join(__dirname, "fake-claude.log");
const args = process.argv.slice(2);
if (args[0] === "auth") { console.log('{"loggedIn": true}'); process.exit(0); }
// --help without --bg: an older Claude, so Reins resumes with claude -p as recorded here.
if (args[0] === "--help") { console.log("Usage: claude [options]"); process.exit(0); }
let input = "", handled = false;
process.stdin.on("data", (chunk) => {
  input += chunk;
  const end = input.indexOf("\n");
  if (handled || end < 0) return;
  handled = true;
  const prompt = JSON.parse(input.slice(0, end)).message.content;
  fs.appendFileSync(log, "argv: " + args.join(" ") + "\n--- stdin ---\n" + prompt + "\n--- end ---\n");
  const ack = /^ACK after inspection: (.*)$/m.exec(prompt);
  // Windows callbacks write the ACK command for PowerShell.
  const shell = process.platform === "win32" ? "powershell.exe" : "/bin/sh";
  try { fs.appendFileSync(log, execSync(ack[1], { encoding: "utf8", shell })); }
  catch (error) { fs.appendFileSync(log, "ACK failed: " + error.message + "\n"); }
  console.log('{"type":"system","subtype":"init"}');
  console.log('{"type":"result","subtype":"success","result":"fake claude received the callback"}');
});
process.stdin.on("end", () => process.exit(0));  // Claude stays until its stdin closes.
JS
node_bin=$(native "$(command -v node)")
if [[ $platform == win32-* ]]; then
  # A plain forwarding shim, which Reins resolves to its target; it refuses arbitrary batch files.
  printf '@echo off\r\n"%s.exe" "%s" %%*\r\n' "${node_bin%.exe}" "$(native "$fake/fake-claude.js")" >"$fake/claude.cmd"
  claude_bin=$(native "$fake/claude.cmd")
else
  printf '#!/bin/sh\nexec "%s" "%s" "$@"\n' "$node_bin" "$fake/fake-claude.js" >"$fake/claude"
  chmod +x "$fake/claude"
  claude_bin=$fake/claude
fi
reins setup --force --skill-target claude --claude-bin "$claude_bin" --now
reins run --agent claude --session e2e-session --task npm-e2e --callback-mode cli \
  -- "$node_bin" -e "setTimeout(() => console.log('payload-from-task'), 2000)"

fail() {
  echo "FAIL: $*"
  reins status || true
  echo "--- daemon log"; tail -n 80 "$HOME/.reins/daemon.log" || true
  echo "--- callback records"; find "$HOME/.reins/queue" -name '*-c1*' -type f | while read -r f; do echo "# $f"; head -c 3000 "$f"; echo; done
  echo "--- task logs"; tail -n 20 "$HOME"/.reins/tasks/*/attempt-1.log || true
  [[ -s $fake/fake-claude.log ]] && { echo "--- fake claude log"; cat "$fake/fake-claude.log"; }
  exit 1
}
for _ in $(seq 240); do [[ -s $fake/fake-claude.log && -z $(reins status --quiet-empty) ]] && break; sleep 0.5; done
[[ -s $fake/fake-claude.log ]] || fail "no callback reached the fake claude"
echo "--- fake claude log"; cat "$fake/fake-claude.log"
[[ -z $(reins status --quiet-empty) ]] || fail "callback was not acknowledged"
deliveries=$(grep -c '^--- stdin ---' "$fake/fake-claude.log")
[[ $deliveries -eq 1 ]] || fail "callback delivered $deliveries times, expected once"
grep -q 'payload-from-task' "$HOME"/.reins/tasks/*/attempt-1.log || fail "the task's output is not in its log"
echo "ok: task ran under the OS owner, callback delivered and acknowledged"

step "reins status / doctor"
reins status || true
# Informational: without a real, signed-in Claude, doctor reports needs_configuration.
reins doctor --agent claude --session e2e-session | head -c 1500 || true; echo
