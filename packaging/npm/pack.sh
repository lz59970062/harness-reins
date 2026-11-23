#!/usr/bin/env bash
# Stage and pack the npm packages around a frozen build: the platform package and the
# launcher package, which is the same for every platform.
# Usage: pack.sh <npm-version> <platform>, e.g. pack.sh 0.7.1-alpha.2 linux-x64
# Linux runs it inside a Node image (/src, /out); build-native.sh runs it on the CI runner.
set -euo pipefail

version=$1
platform=$2
src=${REINS_SRC:-/src}
out=${REINS_OUT:-/out}
native() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }
stage=$(mktemp -d)
cp -r "$src/packaging/npm/reins" "$stage/reins"
mkdir -p "$stage/$platform/dist"
# npm pack leaves symlinks out, and PyInstaller links the macOS Python framework: copy their targets.
cp -RL "$out/dist/reins" "$stage/$platform/dist/reins"

node - "$(native "$stage")" "$version" "$platform" <<'EOF'
const fs = require("fs");
const path = require("path");
const [stage, version, platform] = process.argv.slice(2);
const [os, cpu] = platform.split("-");

const mainFile = path.join(stage, "reins", "package.json");
const main = JSON.parse(fs.readFileSync(mainFile, "utf8"));
main.version = version;
if (!(`@harness-reins/${platform}` in main.optionalDependencies)) throw new Error(`${platform} is not among the launcher's optionalDependencies`);
for (const name of Object.keys(main.optionalDependencies)) main.optionalDependencies[name] = version;
fs.writeFileSync(mainFile, JSON.stringify(main, null, 2) + "\n");

fs.writeFileSync(path.join(stage, platform, "package.json"), JSON.stringify({
  name: `@harness-reins/${platform}`,
  version,
  description: `Prebuilt Harness Reins binary for ${platform}`,
  license: "MIT",
  os: [os],
  cpu: [cpu],
  files: ["dist"],
  preferUnplugged: true,
}, null, 2) + "\n");
EOF

mkdir -p "$out/npm"
(cd "$stage/reins" && npm pack --silent --pack-destination "$(native "$out/npm")")
(cd "$stage/$platform" && npm pack --silent --pack-destination "$(native "$out/npm")")
