# vcpkg demo — manifest mode, registry baseline never moved

`app/` depends on `toydep` with `"version>=": "1.0.0"` through a filesystem registry kept in the
repo (`app/registry`, baseline `default` = 1.0.0, versions 1.0.0 and 2.0.0). DepBot raises the
constraint to `2.0.0`, leaves `vcpkg-configuration.json` and the baseline untouched, migrates
`old_sum` → `sum`, and verifies with vcpkg install + CMake + CTest.

```bash
eval "$(examples/real-vcpkg/prepare.sh)"     # clones + bootstraps vcpkg, prints VCPKG_ROOT
# ARM Linux only: export VCPKG_FORCE_SYSTEM_BINARIES=1  (needs cmake and ninja on PATH)
depbot run --fleet examples/real-vcpkg/fleet.yaml --event examples/real-vcpkg/events/toydep-v2.yaml --approve-major
```
