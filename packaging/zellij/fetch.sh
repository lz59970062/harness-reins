#!/usr/bin/env bash
# Download the pinned Zellij for one npm platform and check it against the release's SHA-256.
# Usage: fetch.sh <platform> <output-dir>, e.g. fetch.sh linux-x64 packaging/out/zellij
# `reins tui` runs this copy (design 7.4); Zellij is MIT-licensed, LICENSE.md ships beside it.
set -euo pipefail

version=0.46.0
platform=$1
out=$2
case $platform in
  linux-x64)    asset=zellij-no-web-x86_64-unknown-linux-musl.tar.gz
                sha=6ddb3296479e8832c2c7a43252e404877b6507c833719663dd10b3b2182185fa ;;
  linux-arm64)  asset=zellij-no-web-aarch64-unknown-linux-musl.tar.gz
                sha=f43fbb9fa1bfd945c70a932c2d8974d23c60dae118d13915b762d944774f601d ;;
  darwin-arm64) asset=zellij-no-web-aarch64-apple-darwin.tar.gz
                sha=5abf6676840a5e120edf0d341e1454e4251abb1b826ca524bbc9f30bb400e6f5 ;;
  darwin-x64)   asset=zellij-no-web-x86_64-apple-darwin.tar.gz
                sha=089aff98277aba45bda4386d4aa5efe99d2524c4c0e3b33f2c715b1c048c5033 ;;
  *) echo "no Zellij for $platform; reins tui is not available there" >&2; exit 2 ;;
esac

mkdir -p "$out"
archive=$(mktemp)
trap 'rm -f "$archive"' EXIT
url=${REINS_ZELLIJ_MIRROR:-https://github.com/zellij-org/zellij/releases/download}/v$version/$asset
curl -fsSL --retry 3 -o "$archive" "$url"
if command -v sha256sum >/dev/null; then got=$(sha256sum "$archive" | cut -d' ' -f1); else got=$(shasum -a 256 "$archive" | cut -d' ' -f1); fi
if [[ $got != "$sha" ]]; then
  echo "Zellij $asset: SHA-256 $got, expected $sha" >&2
  exit 1
fi
tar -xzf "$archive" -C "$out" zellij
chmod 755 "$out/zellij"
cp "$(dirname "${BASH_SOURCE[0]}")/LICENSE.md" "$out/ZELLIJ-LICENSE.md"
echo "Zellij $version for $platform in $out"
