#!/usr/bin/env bash
# Prepare Demo B: build {fmt} 6.2.1 and 7.0.0 from upstream tags into the fleet's
# isolated, offline Conan home, and fetch the upstream changelog. Needs git, conan 2,
# cmake >= 3.20 and a C++17 compiler. Nothing touches ~/.conan2.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
work="$here/.work"
mkdir -p "$work"

for v in 6.2.1 7.0.0; do
  if [ ! -d "$work/fmt-$v" ]; then
    git clone -q --depth 1 --branch "$v" https://github.com/fmtlib/fmt.git "$work/fmt-$v"
  fi
  sed "s/@VERSION@/$v/" "$here/recipe/conanfile.py.in" > "$work/fmt-$v/conanfile.py"
done

if [ ! -f "$work/ChangeLog-old.md" ]; then
  # The 7.0.0 tag ships its changelog as RST; the Markdown history lives on the default branch.
  git clone -q --depth 1 https://github.com/fmtlib/fmt.git "$work/fmt-main"
  cp "$work/fmt-main/doc/ChangeLog-old.md" "$work/ChangeLog-old.md"
fi

depbot demo setup --fleet "$here/fleet.yaml" --recipe "$work/fmt-6.2.1" --recipe "$work/fmt-7.0.0"
