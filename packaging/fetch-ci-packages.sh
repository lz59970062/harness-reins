#!/usr/bin/env bash
# Download the macOS and Windows npm packages that CI built for one commit into
# packaging/out/npm, beside the Linux build from build-linux.sh.
#
#   packaging/fetch-ci-packages.sh v0.15.0     (a tag, branch or commit; default HEAD)
set -euo pipefail

root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
commit=$(git -C "$root" rev-parse "${1:-HEAD}^{commit}")
run=$(gh run list --workflow binaries.yml --commit "$commit" --status success --limit 1 --json databaseId -q '.[0].databaseId')
if [[ -z $run ]]; then echo "no successful binaries.yml run for $commit yet (gh run list --workflow binaries.yml)" >&2; exit 1; fi
download=$(mktemp -d)
trap 'rm -rf "$download"' EXIT
gh run download "$run" --dir "$download"
mkdir -p "$root/packaging/out/npm"
for platform in darwin-arm64 darwin-x64 win32-x64; do
  tarball=$(find "$download/npm-$platform" -name "harness-reins-$platform-*.tgz" -print -quit)
  [[ -n $tarball ]] || { echo "run $run has no package for $platform" >&2; exit 1; }
  cp "$tarball" "$root/packaging/out/npm/"
done
echo "run $run ($commit):"
ls -l "$root/packaging/out/npm"
