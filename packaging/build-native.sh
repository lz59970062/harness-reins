#!/usr/bin/env bash
# Build reins for this machine's platform and pack it as npm packages, without Docker.
# The macOS and Windows jobs of .github/workflows/binaries.yml run it; Linux uses
# build-linux.sh, whose old-glibc builder lets one binary run on many distributions.
#
#   packaging/build-native.sh darwin-arm64   -> packaging/out/{dist,npm}
set -euo pipefail

platform=$1
case $platform in
  darwin-arm64|darwin-x64|win32-x64) ;;
  *) echo "build-native.sh builds darwin-arm64, darwin-x64 or win32-x64; for Linux use build-linux.sh" >&2; exit 2 ;;
esac
here=$(node -p 'process.platform + "-" + process.arch')
if [[ $here != "$platform" ]]; then echo "this machine is $here, not $platform" >&2; exit 2; fi

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
out=$root/packaging/out
python_version=$(sed -n 's/^version = "\(.*\)"/\1/p' "$root/pyproject.toml")
npm_version=$(sed -E 's/a([0-9]+)$/-alpha.\1/; s/b([0-9]+)$/-beta.\1/; s/rc([0-9]+)$/-rc.\1/' <<<"$python_version")
build=$(git -C "$root" rev-parse --short=12 HEAD)
git -C "$root" diff --quiet HEAD -- src pyproject.toml || build="$build-dirty"
echo "building $python_version (npm $npm_version) build $build for $platform"

rm -rf "$out"
mkdir -p "$out"
export REINS_SRC=$root REINS_OUT=$out
REINS_BUILD=$build bash "$root/packaging/freeze.sh"
# `reins tui` runs the Zellij shipped beside reins (design 7.4); it does not run on Windows.
case $platform in darwin-*) bash "$root/packaging/zellij/fetch.sh" "$platform" "$out/dist/reins" ;; esac
bash "$root/packaging/npm/pack.sh" "$npm_version" "$platform"
ls -l "$out/npm"
