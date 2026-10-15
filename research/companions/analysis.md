# Floating companions and cross device continuity: evidence ledger

Read only static inspection of locally installed, authorized apps on 2026-10-14, using REA
(plist and signature) plus bounded `strings` and `grep` over shipped artifacts. No app was
launched, instrumented, or modified. No proprietary code is reproduced here.

| # | Claim | Evidence | Confidence |
|---|---|---|---|
| C1 | Claude.app renders quick entry and voice windows as transparent Electron `type:"panel"` windows: `focusable:false`, `hiddenInMissionControl`, always on top at `pop-up-menu` or `screen-saver` level, visible on all workspaces. | `Contents/Resources/app.asar` constructor context; `setIgnoreMouseEvents` appears 17 times | Observed, static |
| C2 | Grok Bot.app anchors a frameless transparent `skipTaskbar` panel at the bottom right of the work area, `setAlwaysOnTop(true,"floating")`, dragged by `-webkit-app-region:drag`, with a sandboxed preload. | `app.asar` window factory | Observed, static |
| C3 | Grok Bot handoff identifiers (`EndGrokBotBoxHandoffRequest`, `box_handoff_request_id`) describe a server side agent box handoff, not a device handoff. The only socket endpoint string is `wss://api.x.ai/`. | `app.asar` identifiers | Observed, static. Device continuity there is cloud mediated (inference). |
| C4 | Raycast (native Swift) references `setFloatingPanel:`, `setHidesOnDeactivate:`, `setBecomesKeyOnlyIfNeeded:`, `setCollectionBehavior:`, `setMovableByWindowBackground:`, `setIgnoresMouseEvents:`, `setLevel:`. | selector strings in the Mach-O | Observed selectors; flag values inferred (no decompile) |
| C5 | ChatGPT.app (Electron, bundle `com.openai.codex`) ships `aps-environment: production`, app group `2DC432GLL2.com.openai.codex.notifications`, keychain group `2DC432GLL2.com.openai.shared`, and declares only `NSUserActivityTypeBrowsingWeb`. | REA `ev_0f62f6574f99…`, `ev_a995acbbfed8…` | Observed. Continuity is account based; app Handoff limited to web. |
| C6 | iPhone Mirroring holds private entitlements including `com.apple.RemoteDisplay`, `com.apple.private.sharing.unlock-manager`, `com.apple.wifip2pd`, `com.apple.PairingManager.Read`, `com.apple.private.screencapturekit.noprompt`. | codesign entitlements | Observed. Unavailable to third party apps. |

## Conclusions

- Every inspected third party companion is a local floating panel plus cloud state. None
  pairs devices peer to peer. Stark's local authoritative runtime with paired thin clients
  has no direct precedent here, yet needs only public API.
- Apple's own continuity rides private entitlements (C6). The public surface is
  NSUserActivity Handoff, Network.framework (Bonjour, AWDL peer to peer), and
  MultipeerConnectivity.

## Gaps

- Hopper bridge timed out (likely a license or loader dialog), so no pseudocode backs C4
  flag values. REA reported `/tmp/rea-SXIwXu` left behind.
- Static evidence only. No runtime observation confirmed window levels or behavior.
