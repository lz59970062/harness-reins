#!/usr/bin/env bash
# Install the packed npm packages through a throwaway local registry and run a
# real callback cycle. Runs as root inside a disposable Node image that has no
# Python; packaging/test-linux.sh starts it.
set -euo pipefail

step() { printf '\n=== %s\n' "$*"; }
upstream=${NPM_UPSTREAM:-https://registry.npmjs.org}
registry=http://127.0.0.1:4873

step "no Python on this machine"
for name in python python3; do
  if command -v "$name" >/dev/null; then echo "unexpected $name: $(command -v "$name")"; exit 1; fi
done
echo "ok: neither python nor python3 is on PATH"

step "screen for the workload backend"
if [[ -n ${APT_MIRROR:-} ]]; then sed -i "s|deb.debian.org|$APT_MIRROR|g" /etc/apt/sources.list.d/debian.sources; fi
apt-get update -qq && apt-get install -y -qq --no-install-recommends screen procps >/dev/null
screen --version

step "local npm registry (verdaccio) holding only the reins packages"
npm install -g --silent --registry "$upstream" verdaccio@5.31.1
mkdir -p /tmp/verdaccio
cat >/tmp/verdaccio/config.yaml <<'EOF'
storage: /tmp/verdaccio/storage
# Publishing sends the tarball base64-encoded; the binary package exceeds the 10mb default.
max_body_size: 100mb
auth:
  htpasswd:
    file: /tmp/verdaccio/htpasswd
packages:
  # No uplink: the real registry's harness-reins (a placeholder until release) must not shadow the local packages.
  'harness-reins':
    access: $all
    publish: $authenticated
  '@harness-reins/*':
    access: $all
    publish: $authenticated
log: { type: stdout, format: pretty, level: error }
EOF
verdaccio --config /tmp/verdaccio/config.yaml --listen 127.0.0.1:4873 >/tmp/verdaccio/log 2>&1 &
for _ in $(seq 50); do node -e "fetch('$registry/-/ping').then(()=>process.exit(0),()=>process.exit(1))" && break; sleep 0.2; done
token=$(node -e "
fetch('$registry/-/user/org.couchdb.user:ci', {method: 'PUT', headers: {'content-type': 'application/json'},
  body: JSON.stringify({name: 'ci', password: 'ci-password'})}).then(r => r.json()).then(j => console.log(j.token))")
npm config set "//127.0.0.1:4873/:_authToken" "$token"
for tarball in /out/npm/*.tgz; do npm publish --registry "$registry" "$tarball"; done
npm view --registry "$registry" harness-reins name version optionalDependencies

step "npm install -g harness-reins (npm picks the platform binary)"
npm install -g --registry "$registry" harness-reins
npm ls -g --depth 1 | grep "@harness-reins/linux-x64"
command -v reins
reins --version

step "bundled resources load from the frozen binary"
reins install-skill --target claude
ls ~/.claude/skills/*/SKILL.md
reins template list >/dev/null && echo "ok: templates readable"

step "end-to-end: daemon -> screen task -> callback delivered to a fake claude"
export CLAUDE_CONFIG_DIR=/tmp/claude CODEX_HOME=/tmp/codex REINS_DESKTOP_APP_SERVER=0
export PATH=/opt/fake:$PATH REINS_CLAUDE_BIN=/opt/fake/claude
mkdir -p /opt/fake
cat >/opt/fake/claude <<'EOF'
#!/bin/sh
# Stands in for `claude -p --resume ... --input-format stream-json`: record what reins delivered.
case "$1" in auth) echo '{"loggedIn": true}'; exit 0;; esac
IFS= read -r line
prompt=$(printf '%s' "$line" | node -e 'let s="";process.stdin.on("data",d=>s+=d).on("end",()=>console.log(JSON.parse(s).message.content))')
{ echo "argv: $*"; echo "--- stdin ---"; echo "$prompt"; echo "--- end ---"; } >>/tmp/fake-claude.log
# Like a real agent: inspect, then run the ACK command the callback names.
ack=$(printf '%s\n' "$prompt" | sed -n 's/^ACK after inspection: //p')
sh -c "$ack" >>/tmp/fake-claude.log 2>&1
echo '{"type":"system","subtype":"init"}'
echo '{"type":"result","subtype":"success","result":"fake claude received the callback"}'
cat >/dev/null  # Claude stays until its stdin closes.
EOF
chmod +x /opt/fake/claude
reins setup --backend screen --service standalone --skill-target claude --claude-bin /opt/fake/claude --now
reins run --agent claude --session e2e-session --task npm-e2e --backend screen --callback-mode cli \
  -- sh -c 'sleep 2; echo payload-from-task'

# Done once the callback arrived and nothing is left awaiting ACK.
# (status lists callbacks only, so it is also empty while the task runs.)
for _ in $(seq 120); do [[ -s /tmp/fake-claude.log && -z $(reins status --quiet-empty) ]] && break; sleep 0.5; done
if [[ ! -s /tmp/fake-claude.log ]]; then
  echo "FAIL: no callback reached the fake claude"; reins status || true; exit 1
fi
echo "--- fake claude log"; cat /tmp/fake-claude.log
if [[ -n $(reins status --quiet-empty) ]]; then echo "FAIL: callback was not acknowledged"; reins status; exit 1; fi
if ! ls /root/.reins/queue/acks/*-c1.json >/dev/null 2>&1; then echo "FAIL: no ACK record"; exit 1; fi
deliveries=$(grep -c '^--- stdin ---' /tmp/fake-claude.log)
if [[ $deliveries -ne 1 ]]; then echo "FAIL: callback delivered $deliveries times, expected once"; exit 1; fi
echo "ok: callback delivered and acknowledged"

step "who owns the long-lived processes"
ps -eo pid,args | grep -E '[l]tc (daemon|_)' || true
for pid in $(pgrep -f 'dist/reins/reins daemon'); do echo "daemon $pid exe -> $(readlink /proc/$pid/exe)"; done

step "reins status / doctor"
reins status || true
reins doctor --agent claude --session e2e-session | head -c 1500; echo
