#!/bin/sh
# Stark Pocket acceptance in the iOS simulator against the real runtime on this Mac.
#
#   ios/scripts/pocket-demo.sh                 handoff and faults
#   ios/scripts/pocket-demo.sh --skip-faults   handoff only
#   ios/scripts/pocket-demo.sh --simulator "iPhone 18 Pro"
#
# Builds StarkMac.app and StarkPocket for testing, then runs
# prototype/tools/pocket_acceptance.py. Uses ~/.gameshark/demo, never the everyday
# database. Needs full Xcode with an iOS simulator runtime.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
app="$("$here/../../mac/scripts/build-app.sh" | tail -1)"
mac="$here/../../mac/scripts"
"$mac/swiftpm.sh" build -c release --product starkctl >/dev/null
ctl="$(cd "$mac/../StarkMac" && "$mac/swiftpm.sh" build -c release --show-bin-path)/starkctl"
project="$here/../StarkPocket/StarkPocket.xcodeproj"
sim="iPhone 18 Pro"
prev=""
for a in "$@"; do
  [ "$prev" = --simulator ] && sim="$a"
  prev="$a"
done
xcodebuild -project "$project" -scheme StarkPocket \
  -destination "platform=iOS Simulator,name=$sim" \
  -derivedDataPath "$here/../StarkPocket/build" build-for-testing -quiet
cd "$here/../../prototype"
exec uv run python tools/pocket_acceptance.py --app "$app" --project "$project" --ctl "$ctl" "$@"
