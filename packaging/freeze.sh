#!/usr/bin/env bash
# Freeze reins with PyInstaller into $REINS_OUT/dist/reins.
# Linux: runs inside the builder image (/src, /out); the builder's glibc is the oldest glibc
# the binary will run on. macOS and Windows: build-native.sh runs it on the CI runner.
set -euo pipefail

src=${REINS_SRC:-/src}
out=${REINS_OUT:-/out}
# Git Bash on Windows: hand native tools C:/... paths.
native() { if command -v cygpath >/dev/null; then cygpath -m "$1"; else printf '%s' "$1"; fi; }

work=$(mktemp -d)
cp -r "$src/pyproject.toml" "$src/src" "$work/"   # /src is mounted read-only
# A frozen build has no source files to digest, so it carries its build ID.
if [[ -n ${REINS_BUILD:-} ]]; then
  printf '%s\n' "$REINS_BUILD" > "$work/src/harness_reins/_build.txt"
fi
python -m venv "$work/venv"
if [[ -f $work/venv/Scripts/activate ]]; then . "$work/venv/Scripts/activate"; else . "$work/venv/bin/activate"; fi
python -m pip install --quiet --disable-pip-version-check pyinstaller==6.11.1 "$(native "$work")[tui]"
pyinstaller --noconfirm --log-level WARN --distpath "$(native "$out/dist")" --workpath "$(native "$work/build")" \
  "$(native "$src/packaging/pyinstaller/reins.spec")"

exe=$out/dist/reins/reins
[[ -f $exe.exe ]] && exe=$exe.exe
"$exe" --version
if [[ $(uname -s) == Linux ]]; then echo "built against $(ldd --version | head -1)"; fi
du -sh "$out/dist/reins"
