#!/usr/bin/env node
// npm launcher: find the frozen reins for this platform and run it.
//
// The binary is installed by one of the optionalDependencies below; npm picks
// the one whose "os"/"cpu" match. Long-lived owners (systemd, launchd, Task
// Scheduler) are registered against the binary itself, never this script, so
// they do not depend on Node.
"use strict";

const { spawnSync } = require("child_process");
const path = require("path");

const PLATFORM_PACKAGES = {
  "linux-x64": "@harness-reins/linux-x64",
  "linux-arm64": "@harness-reins/linux-arm64",
  "darwin-x64": "@harness-reins/darwin-x64",
  "darwin-arm64": "@harness-reins/darwin-arm64",
  "win32-x64": "@harness-reins/win32-x64",
};

function fail(message) {
  process.stderr.write(`reins: ${message}\n`);
  process.exit(1);
}

const PIP_INSTALL = 'python3 -m pip install "git+https://github.com/lz59970062/harness-reins.git"';

// The Linux binaries link against glibc; musl systems such as Alpine cannot load them.
function isMusl() {
  if (process.platform !== "linux") return false;
  try {
    const report = process.report && process.report.getReport();
    return !(report && report.header && report.header.glibcVersionRuntime);
  } catch {
    return false;
  }
}

function binaryPath() {
  if (process.env.REINS_BINARY) return process.env.REINS_BINARY;
  if (isMusl()) fail(`no prebuilt binary for musl-based Linux (such as Alpine); install from source: ${PIP_INSTALL}`);
  const key = `${process.platform}-${process.arch}`;
  const name = PLATFORM_PACKAGES[key];
  if (!name) fail(`no prebuilt binary for ${key}; install from source: ${PIP_INSTALL}`);
  let root;
  try {
    root = path.dirname(require.resolve(`${name}/package.json`));
  } catch {
    // A platform this release has no build for is not among its optionalDependencies.
    const own = require(path.join(__dirname, "..", "package.json"));
    if (!(own.optionalDependencies && name in own.optionalDependencies)) {
      fail(`this release has no prebuilt binary for ${key}; install from source: ${PIP_INSTALL}`);
    }
    fail(`${name} is not installed; reinstall without --omit=optional / --no-optional`);
  }
  return path.join(root, "dist", "reins", process.platform === "win32" ? "reins.exe" : "reins");
}

const result = spawnSync(binaryPath(), process.argv.slice(2), { stdio: "inherit" });
if (result.error) fail(result.error.message);
if (result.signal) process.kill(process.pid, result.signal);
process.exit(result.status === null ? 1 : result.status);
