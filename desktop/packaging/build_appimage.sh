#!/usr/bin/env bash
# Wrap the Linux one-folder build (build_linux.sh) into one AppImage.
#
#   DLNE_BUILD_DIR=/path/used/for/build_linux.sh  DLNE_APPIMAGETOOL=/path/to/appimagetool-x86_64.AppImage \
#   DLNE_APPIMAGETOOL_SHA256=<hash>  desktop/packaging/build_appimage.sh
#
# appimagetool is downloaded by hand from https://github.com/AppImage/appimagetool/releases (the release page lists each
# file's SHA-256); the script refuses to run it when DLNE_APPIMAGETOOL_SHA256 is set and does not match. It runs with
# --appimage-extract-and-run, so it needs no FUSE.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DESKTOP="$(dirname "$HERE")"
REPO="$(dirname "$DESKTOP")"
OUT="${DLNE_BUILD_DIR:?set DLNE_BUILD_DIR to the folder build_linux.sh used}"
TOOL="${DLNE_APPIMAGETOOL:?set DLNE_APPIMAGETOOL to appimagetool-x86_64.AppImage}"
APP="$OUT/dist/DLNE-Donate"
[ -x "$APP/DLNE-Donate" ] || { echo "run build_linux.sh first: $APP/DLNE-Donate is missing" >&2; exit 2; }
if [ -n "${DLNE_APPIMAGETOOL_SHA256:-}" ]; then
  echo "${DLNE_APPIMAGETOOL_SHA256}  $TOOL" | sha256sum -c - || { echo "appimagetool does not match the published hash" >&2; exit 3; }
fi

VERSION="$(sed -n 's/^__version__ = "\(.*\)"/\1/p' "$DESKTOP/dlne_donate/__init__.py")"
DIR="$OUT/AppDir"
rm -rf "$DIR"
mkdir -p "$DIR/usr/bin" "$DIR/usr/share/icons/hicolor/256x256/apps"
cp -a "$APP" "$DIR/usr/bin/DLNE-Donate"
python3 - "$REPO/android/play/icon-512.png" "$DIR/dlne-donate.png" <<'PY' 2>/dev/null || cp "$REPO/android/play/icon-512.png" "$DIR/dlne-donate.png"
import sys
from PIL import Image
Image.open(sys.argv[1]).convert("RGBA").resize((256, 256), Image.LANCZOS).save(sys.argv[2])
PY
cp "$DIR/dlne-donate.png" "$DIR/usr/share/icons/hicolor/256x256/apps/dlne-donate.png"
cat > "$DIR/dlne-donate.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=DLNE Donate
Comment=Lend this computer's GPU or CPU to the DLNE swarm
Exec=DLNE-Donate
Icon=dlne-donate
Categories=Network;Utility;
Terminal=false
EOF
cat > "$DIR/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/bin/DLNE-Donate/DLNE-Donate" "$@"
EOF
chmod +x "$DIR/AppRun"

TARGET="$OUT/dist/DLNE-Donate-$VERSION-x86_64.AppImage"
ARCH=x86_64 "$TOOL" --appimage-extract-and-run --no-appstream "$DIR" "$TARGET"
chmod +x "$TARGET"
( cd "$OUT/dist" && sha256sum "$(basename "$TARGET")" | tee "$(basename "$TARGET").sha256" )
ls -la "$TARGET"
