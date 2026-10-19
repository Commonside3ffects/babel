#!/usr/bin/env bash
# Build Babel as an AppImage (dist/Babel-<version>-x86_64.AppImage).
# It is built inside Ubuntu 22.04 so it also runs on older Linux systems
# (anything with glibc 2.35 or newer). Needs podman.
set -euo pipefail
cd "$(dirname "$0")"
VERSION=$(sed -n 's/^VERSION = "\(.*\)"/\1/p' babel.py)
mkdir -p dist
podman run --rm -v "$PWD:/src:ro,Z" -v "$PWD/dist:/out:Z" -e VERSION="$VERSION" \
    docker.io/library/ubuntu:22.04 bash /src/packaging/appimage-inside.sh
echo "Built: $PWD/dist/Babel-$VERSION-x86_64.AppImage"
