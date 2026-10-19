#!/usr/bin/env bash
# Runs inside the Ubuntu 22.04 container started by build-appimage.sh.
# /src is the Babel source (read-only); the AppImage is written to /out.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive
APPIMAGETOOL=https://github.com/AppImage/appimagetool/releases/download/1.9.0/appimagetool-x86_64.AppImage

apt-get update -qq
# Python, plus the system libraries Qt's X11 and Wayland plugins load (PyInstaller bundles them)
apt-get install -y -qq --no-install-recommends python3-venv libpython3.10 binutils file wget ca-certificates desktop-file-utils \
    libgl1 libegl1 libfontconfig1 libfreetype6 libdbus-1-3 libglib2.0-0 \
    libxkbcommon0 libxkbcommon-x11-0 libwayland-client0 libwayland-cursor0 libwayland-egl1 \
    libxcb-cursor0 libxcb-icccm4 libxcb-image0 libxcb-keysyms1 libxcb-randr0 libxcb-render-util0 \
    libxcb-shape0 libxcb-xinerama0 libxcb-xkb1 libxcb-xfixes0 libx11-xcb1 >/dev/null

python3 -m venv /venv
/venv/bin/pip install -q --upgrade pip
/venv/bin/pip install -q -r /src/requirements.txt pyinstaller

cp -r /src /work
cd /work
args=()
for f in babel.svg chevron-down.svg chevron-down-accent.svg window-close.svg open-menu.svg dice.svg refresh.svg; do
    args+=(--add-data "/work/$f:.")
done
/venv/bin/python -m PyInstaller --noconfirm --clean --onedir --name babel "${args[@]}" \
    --hidden-import PyQt6.QtSvg --exclude-module tkinter --exclude-module PyQt6.QtWebEngineCore \
    --exclude-module PyQt6.QtMultimedia --exclude-module PyQt6.Qt3DCore \
    --distpath /work/dist --workpath /work/build babel.py

# Use the computer's own font, X11 and C++ libraries rather than Ubuntu 22.04's: newer systems'
# font settings confuse the old fontconfig, and graphics drivers need the system's newer libstdc++
for lib in libfontconfig.so.1 libfreetype.so.6 libX11.so.6 libX11-xcb.so.1 libstdc++.so.6 libgcc_s.so.1; do
    rm -f "/work/dist/babel/_internal/$lib"
done

# AppDir: the program, a launcher entry and the icon
A=/work/Babel.AppDir
mkdir -p "$A/usr/lib"
cp -r /work/dist/babel "$A/usr/lib/babel"
cat > "$A/AppRun" <<'EOF'
#!/bin/sh
HERE="$(dirname "$(readlink -f "$0")")"
exec "$HERE/usr/lib/babel/babel" "$@"
EOF
chmod +x "$A/AppRun"
cat > "$A/babel.desktop" <<'EOF'
[Desktop Entry]
Type=Application
Name=Babel
GenericName=Library
Comment=Browse and search your book collection
Exec=babel
Icon=babel
Terminal=false
Categories=Office;Viewer;Education;Literature;
Keywords=books;library;ebook;epub;pdf;reading;kobo;
StartupWMClass=babel
EOF
cp babel.svg "$A/babel.svg"
cp babel.svg "$A/.DirIcon"

wget -q -O /appimagetool "$APPIMAGETOOL"
chmod +x /appimagetool
ARCH=x86_64 /appimagetool --appimage-extract-and-run -v --no-appstream "$A" "/out/Babel-$VERSION-x86_64.AppImage"
