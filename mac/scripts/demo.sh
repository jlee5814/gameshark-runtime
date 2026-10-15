#!/bin/sh
# Repeatable StarkMac acceptance demonstration.
#
#   mac/scripts/demo.sh                 automated run, fresh demo database
#   mac/scripts/demo.sh --interactive   also pause for a real mouse drag and a look
#   mac/scripts/demo.sh --inference-url http://127.0.0.1:30000/v1 --model <name>
#
# Builds StarkMac.app and starkctl, then runs prototype/tools/acceptance.py. Uses
# ~/.gameshark/demo, never the everyday ~/.gameshark/runtime.db.
set -eu
here="$(cd "$(dirname "$0")" && pwd)"
app="$("$here/build-app.sh" | tail -1)"
"$here/swiftpm.sh" build -c release --product starkctl >/dev/null
ctl="$(cd "$here/../StarkMac" && "$here/swiftpm.sh" build -c release --show-bin-path)/starkctl"
cd "$here/../../prototype"
exec uv run python tools/acceptance.py --app "$app" --ctl "$ctl" --fresh "$@"
