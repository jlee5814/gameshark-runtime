#!/bin/sh
# Build StarkMac.app from the SwiftPM package: release binary, sprite atlas, Info.plist,
# ad hoc signature.
# Output: mac/build/StarkMac.app
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
"$here/swiftpm.sh" build -c release --product StarkMac
bin="$(cd "$here/../StarkMac" && "$here/swiftpm.sh" build -c release --show-bin-path)/StarkMac"
app="$here/../build/StarkMac.app"
rm -rf "$app"
mkdir -p "$app/Contents/MacOS" "$app/Contents/Resources/stark"
cp "$bin" "$app/Contents/MacOS/StarkMac"
# Stark's body: the extracted atlas and its manifest (mac/tools/extract_sprites.py).
cp "$here/../StarkMac/Assets/stark/manifest.json" "$here/../StarkMac/Assets/stark/stark-atlas.png" \
  "$app/Contents/Resources/stark/"
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
  <key>NSMicrophoneUsageDescription</key><string>Hold the microphone button to talk to Stark. Audio is recognized on this Mac and never saved.</string>
  <key>NSSpeechRecognitionUsageDescription</key><string>Stark turns what you say into text on this Mac so you can review it before sending. Nothing is sent to a speech server.</string>
</dict>
</plist>
PLIST
codesign --force --sign - "$app"
echo "$app"
