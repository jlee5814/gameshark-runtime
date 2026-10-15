#!/bin/sh
# Run `swift build` or `swift test` for StarkMac.
#
# Some Command Line Tools installs keep a stale Swift 5.10 PackageDescription private
# interface beside a newer library, so every manifest fails to link. When that file is
# present, a VFS overlay points the compiler at the matching public interface for manifest
# builds only. Nothing outside the package's .build directory changes.
set -eu
cd "$(dirname "$0")/../StarkMac"
api="$(dirname "$(dirname "$(xcrun --find swift)")")/lib/swift/pm/ManifestAPI/PackageDescription.swiftmodule"
if [ -f "$api/arm64-apple-macos.private.swiftinterface" ] &&
   ! grep -q swiftLanguageModes "$api/arm64-apple-macos.private.swiftinterface" &&
   grep -q swiftLanguageModes "$api/arm64-apple-macos.swiftinterface"; then
  mkdir -p .build
  overlay="$PWD/.build/manifest-overlay.yaml"
  cat > "$overlay" <<YAML
{"version": 0, "case-sensitive": "false", "roots": [
 {"type": "file", "name": "$api/arm64-apple-macos.private.swiftinterface", "external-contents": "$api/arm64-apple-macos.swiftinterface"},
 {"type": "file", "name": "$api/x86_64-apple-macos.private.swiftinterface", "external-contents": "$api/x86_64-apple-macos.swiftinterface"}
]}
YAML
  set -- "$@" -Xbuild-tools-swiftc -vfsoverlay -Xbuild-tools-swiftc "$overlay" \
    -Xbuild-tools-swiftc -module-cache-path -Xbuild-tools-swiftc "$PWD/.build/manifest-cache"
fi
# Command Line Tools keep Swift Testing outside the default search paths; Xcode does not.
dev="$(xcode-select -p)"
if [ "$1" = test ] && [ -d "$dev/Library/Developer/Frameworks/Testing.framework" ] &&
   [ ! -d "$dev/Platforms" ]; then
  fw="$dev/Library/Developer/Frameworks"
  lib="$dev/Library/Developer/usr/lib"
  set -- "$@" -Xswiftc -F -Xswiftc "$fw" -Xlinker -F -Xlinker "$fw" \
    -Xlinker -rpath -Xlinker "$fw" -Xlinker -rpath -Xlinker "$lib"
fi
exec swift "$@"
