#!/usr/bin/env bash
# Freeze reins with PyInstaller. Runs inside a Python image and writes /out/dist/reins.
# The builder's glibc is the oldest glibc the binary will run on.
set -euo pipefail

work=$(mktemp -d)
cp -r /src/pyproject.toml /src/src "$work/"   # /src is mounted read-only
# A frozen build has no source files to digest, so it carries its build ID.
if [[ -n ${REINS_BUILD:-} ]]; then
  printf '%s\n' "$REINS_BUILD" > "$work/src/harness_reins/_build.txt"
fi
python -m venv "$work/venv"
. "$work/venv/bin/activate"
pip install --quiet --disable-pip-version-check pyinstaller==6.11.1 "$work[tui]"
pyinstaller --noconfirm --log-level WARN --distpath /out/dist --workpath "$work/build" \
  /src/packaging/pyinstaller/reins.spec

/out/dist/reins/reins --version
echo "built against $(ldd --version | head -1)"
du -sh /out/dist/reins
