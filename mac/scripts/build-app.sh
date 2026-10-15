#!/bin/sh
# Build StarkMac.app from the SwiftPM package: release binary, Info.plist, ad hoc signature.
# Output: mac/build/StarkMac.app
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
"$here/swiftpm.sh" build -c release --product StarkMac
bin="$(cd "$here/../StarkMac" && "$here/swiftpm.sh" build -c release --show-bin-path)/StarkMac"
app="$here/../build/StarkMac.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS"
cp "$bin" "$app/Contents/MacOS/StarkMac"
cat > "$app/Contents/Info.plist" <<'PLIST'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleIdentifier</key><string>dev.gameshark.starkmac</string>
  <key>CFBundleName</key><string>StarkMac</string>
  <key>CFBundleExecutable</key><string>StarkMac</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleShortVersionString</key><string>0.4.0</string>
  <key>CFBundleVersion</key><string>1</string>
  <key>LSMinimumSystemVersion</key><string>14.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST
codesign --force --sign - "$app"
echo "$app"
