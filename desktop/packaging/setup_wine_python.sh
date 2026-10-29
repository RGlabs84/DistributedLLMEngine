#!/usr/bin/env bash
# One-time: a dedicated Wine prefix with the official Windows Python 3.12 in it, for build_windows.sh.
#
#   DLNE_WINE_PREFIX=/path/to/new/prefix  desktop/packaging/setup_wine_python.sh  [python-3.12.10-amd64.exe]
#
# NEVER point DLNE_WINE_PREFIX at ~/.wine. The installer is downloaded from python.org if you do not pass it, and checked
# against its published GPG signature (Steve Dower's release-signing key, fingerprint 7ED1 0B65 31D7 C8E1 BC29  6021 FC62
# 4643 4870 34E5) before it is run; without gpg or the key the script stops rather than run an unchecked installer.
set -euo pipefail

PREFIX="${DLNE_WINE_PREFIX:?set DLNE_WINE_PREFIX to a NEW folder (not ~/.wine)}"
case "$PREFIX" in "$HOME/.wine"|"$HOME/.wine/"*) echo "refusing to touch ~/.wine" >&2; exit 2;; esac
VERSION=3.12.10
INSTALLER="${1:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -z "$INSTALLER" ]; then
  INSTALLER="$WORK/python-$VERSION-amd64.exe"
  curl -fsSL -o "$INSTALLER" "https://www.python.org/ftp/python/$VERSION/python-$VERSION-amd64.exe"
  curl -fsSL -o "$INSTALLER.asc" "https://www.python.org/ftp/python/$VERSION/python-$VERSION-amd64.exe.asc"
fi
if [ ! -f "$INSTALLER.asc" ]; then echo "need $INSTALLER.asc to verify the installer" >&2; exit 2; fi
export GNUPGHOME="$WORK/gnupg"; mkdir -m 700 "$GNUPGHOME"
curl -fsSL "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x7ED10B6531D7C8E1BC296021FC624643487034E5" | gpg --import
gpg --status-fd 1 --verify "$INSTALLER.asc" "$INSTALLER" | grep -q "VALIDSIG 7ED10B6531D7C8E1BC296021FC624643487034E5" \
  || { echo "the installer's signature does not verify; not running it" >&2; exit 3; }
sha256sum "$INSTALLER"

export WINEPREFIX="$PREFIX" WINEARCH=win64 WINEDEBUG=-all WINEDLLOVERRIDES="mscoree,mshtml="
unset WAYLAND_DISPLAY
xvfb-run -a bash -c '
  wineboot -i && wineserver -w
  wine "'"$INSTALLER"'" /quiet InstallAllUsers=0 PrependPath=0 Include_test=0 Include_launcher=0 Include_doc=0 Include_tcltk=1 Include_pip=1 TargetDir="C:\\Python312"
  wineserver -w
  wine "C:\\Python312\\python.exe" -c "import sys, tkinter; print(sys.version); print(\"Tk\", tkinter.TkVersion)"
  wineserver -k
'
