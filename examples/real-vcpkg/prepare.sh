#!/usr/bin/env bash
# Fetch and bootstrap vcpkg into examples/real-vcpkg/.work/vcpkg (never ~/vcpkg). Needs git, curl,
# zip/unzip/tar, cmake and ninja. On ARM Linux vcpkg needs VCPKG_FORCE_SYSTEM_BINARIES=1.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$here/.work"
if [ ! -x "$here/.work/vcpkg/vcpkg" ]; then
  [ -d "$here/.work/vcpkg" ] || git clone -q --depth 1 https://github.com/microsoft/vcpkg.git "$here/.work/vcpkg"
  "$here/.work/vcpkg/bootstrap-vcpkg.sh" -disableMetrics
fi
echo "export VCPKG_ROOT=$here/.work/vcpkg"
