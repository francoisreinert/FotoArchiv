#!/bin/bash
# FotoArchiv starten (Mac). Beim ersten Start auf einem Mac wird die Laufzeitumgebung
# einmalig nach ~/Library/Application Support/FotoArchiv entpackt (ca. 1 Minute).
HERE="$(cd "$(dirname "$0")" && pwd)"
RTVER="$(tr -d '\r\n' < "$HERE/runtime/VERSION")"
case "$(uname -m)" in
  arm64) ARCH=mac-arm64 ;;
  *) ARCH=mac-x64 ;;
esac
RT="$HOME/Library/Application Support/FotoArchiv/runtime-$RTVER-$ARCH"
PY="$RT/python/bin/python3"
if [ ! -f "$RT/ok" ]; then
  echo "FotoArchiv wird auf diesem Mac eingerichtet, bitte warten ..."
  rm -rf "$RT" && mkdir -p "$RT"
  tar -xzf "$HERE/runtime/python-$ARCH.tar.gz" -C "$RT" || { echo "Einrichtung fehlgeschlagen"; read -r; exit 1; }
  xattr -dr com.apple.quarantine "$RT" 2>/dev/null
  "$PY" -m pip install --no-index --disable-pip-version-check -q --find-links "$HERE/runtime/wheels-$ARCH" \
    pillow pillow-heif numpy opencv-python-headless rawpy av icloudpd samsungtvws segno || { echo "Einrichtung fehlgeschlagen"; read -r; exit 1; }
  touch "$RT/ok"
fi
"$PY" "$HERE/app/server.py" "$@"
