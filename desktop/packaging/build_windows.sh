#!/usr/bin/env bash
# Cross-build DLNE-Donate.exe on Linux: the real Windows Python 3.12 and the real Windows wheels, run under Wine.
#
#   DLNE_WINE_PREFIX=/path/to/prefix DLNE_BUILD_DIR=/path/to/scratch desktop/packaging/build_windows.sh
#
# The prefix is the one setup_wine_python.sh made (Python at C:\Python312). Output: $DLNE_BUILD_DIR/dist/DLNE-Donate.exe
# (one windowed file), plus its SHA-256. Set DLNE_DONATE_ONEDIR=1 for a folder build instead of one file.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
PREFIX="${DLNE_WINE_PREFIX:?set DLNE_WINE_PREFIX to the prefix made by setup_wine_python.sh}"
OUT="${DLNE_BUILD_DIR:?set DLNE_BUILD_DIR to an empty or scratch folder}"
case "$PREFIX" in "$HOME/.wine"|"$HOME/.wine/"*) echo "refusing to use ~/.wine" >&2; exit 2;; esac

export WINEPREFIX="$PREFIX" WINEARCH=win64 WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
unset WAYLAND_DISPLAY
PY='C:\Python312\python.exe'
win() { winepath -w "$1"; }          # a Linux path as Wine sees it (Z:\...)

mkdir -p "$OUT"
rm -rf "$OUT/dist" "$OUT/work"
REQ="$(win "$DESKTOP/requirements-build.txt")"
SPEC="$(win "$HERE/dlne-donate.spec")"
ICON_SRC="$(win "$(dirname "$DESKTOP")/android/play/icon-512.png")"
ICON_OUT="$(win "$OUT/DLNE-Donate.ico")"
DIST="$(win "$OUT/dist")"
WORK="$(win "$OUT/work")"

xvfb-run -a bash -c '
  set -euo pipefail
  wine "'"$PY"'" -m pip install --quiet --no-input -r "'"$REQ"'"
  wine "'"$PY"'" "'"$(win "$HERE/make_icon.py")"'" "'"$ICON_SRC"'" "'"$ICON_OUT"'"
  DLNE_DONATE_ICON="'"$ICON_OUT"'" wine "'"$PY"'" -m PyInstaller --noconfirm --clean --distpath "'"$DIST"'" --workpath "'"$WORK"'" "'"$SPEC"'"
  wineserver -k || true
'

ls -la "$OUT/dist"
( cd "$OUT/dist" && sha256sum DLNE-Donate.exe 2>/dev/null | tee DLNE-Donate.exe.sha256 || true )
