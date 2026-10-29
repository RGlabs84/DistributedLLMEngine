#!/usr/bin/env bash
# Build DLNE Donate for Linux x86-64: a PyInstaller one-folder app, packed as a tar.gz.
#
#   DLNE_BUILD_PYTHON=/path/to/python3.12/bin/python3 DLNE_BUILD_DIR=/path/to/scratch desktop/packaging/build_linux.sh
#
# The Python must have Tk (many system Pythons do not): python-build-standalone 3.12 "install_only" does, and its
# SHA-256 is published beside each release at https://github.com/astral-sh/python-build-standalone/releases.
# Everything lands in $DLNE_BUILD_DIR (a virtualenv, PyInstaller's work folder, dist/); nothing is written to the repository.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
PY="${DLNE_BUILD_PYTHON:?set DLNE_BUILD_PYTHON to a Python 3.12 that has Tk}"
OUT="${DLNE_BUILD_DIR:?set DLNE_BUILD_DIR to an empty or scratch folder}"

"$PY" -c 'import sys, tkinter; assert sys.version_info[:2] == (3, 12), sys.version; print("python", sys.version.split()[0], "Tk", tkinter.TkVersion)'

mkdir -p "$OUT"
if [ ! -x "$OUT/venv/bin/python" ]; then
  "$PY" -m venv "$OUT/venv"
fi
"$OUT/venv/bin/python" -m pip install --quiet --upgrade pip
"$OUT/venv/bin/python" -m pip install --quiet --no-input -r "$DESKTOP/requirements-build.txt"

rm -rf "$OUT/dist" "$OUT/work"
"$OUT/venv/bin/python" -m PyInstaller --noconfirm --clean --distpath "$OUT/dist" --workpath "$OUT/work" "$HERE/dlne-donate.spec"

VERSION="$("$OUT/venv/bin/python" -c "import sys; sys.path.insert(0, '$DESKTOP'); import dlne_donate; print(dlne_donate.__version__)")"
ARCHIVE="$OUT/dist/dlne-donate-$VERSION-linux-x86_64.tar.gz"
tar --owner=0 --group=0 -czf "$ARCHIVE" -C "$OUT/dist" DLNE-Donate
( cd "$OUT/dist" && sha256sum "$(basename "$ARCHIVE")" | tee "$(basename "$ARCHIVE").sha256" )
du -sh "$OUT/dist/DLNE-Donate" "$ARCHIVE"
echo "built: $OUT/dist/DLNE-Donate/DLNE-Donate"
