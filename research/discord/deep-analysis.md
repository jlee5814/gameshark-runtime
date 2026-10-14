# Discord deep analysis for GameShark persistent agents

Read only static analysis of authorized local artifacts. No auth bypass, no anti tamper defeat, no secret extraction, no private message inspection, no modification of Discord.

## Evidence and confidence legend

- **Observed**: read directly from a shipped artifact on this machine. Anchor cited.
- **Inferred**: derived from observed code plus public Electron semantics. Not executed.
- **Assumed**: common pattern, stated to be explicit, no direct anchor.

Primary artifacts:

- `scratchpad/discord_asar_extract/bundle.js` (3.1 MB webpack, the desktop **Bootstrapper** main)
- `scratchpad/discord_asar_extract/splashScreenPreload.js`
- `scratchpad/discord_asar_extract/package.json`
- REA bundles: `rea-discord-survey-evidence.json`, `rea-vscode-verification-evidence.json`
- Bundle identity (Observed, `package.json`): name `discord`, description `Discord Client for Desktop - Bootstrapper`, `main` `bundle.js`, Electron 42.7.1, deps `yauzl`, `@sentry/electron`, `electron-log`, `request`.

### Central limitation, stated up front

Discord's `~/Library/Application Support/discord` on this machine holds only `Local Storage` and `Session Storage`. No `modules/`, no `installed.json`, no `settings.json`, no versioned client tree. **Every claim about module loading, gateway, presence, and client state is therefore static evidence from the bootstrapper code, not an observed running client.** The webpack renderer, the gateway socket client, and the presence system ship as downloaded modules absent here. I analyze the bootstrapper and tag the rest Inferred. I do not reconstruct the client.

## Phase 1: module host, bootstrapper, updater relationships

Discord splits into two layers:

1. **Signed shell** (Observed): the `.app` bundle, the `Discord` launcher (119 KB), the 373 MB Electron Framework, and the bootstrapper `app.asar`. Signed by Discord Inc. team `53Q6R32WPB`, hardened runtime. This layer is immutable between host updates.
2. **Module host** (Observed in bootstrapper code): a mutable, versioned module tree the bootstrapper downloads, stages, verifies, and loads at runtime. Anchors: `locallyInstalledModules`, `moduleInstallPath`, `installedModulesFilePath = join(moduleInstallPath, "installed.json")`, `moduleDownloadPath`, log lines `Module install path:` and `Module installed file path:`.

Updater relationships (Observed anchors, behavior Inferred):

- Three coordinated updaters: `hostUpdater` (the signed shell, via `autoUpdater` + `quitAndInstall`), `moduleUpdater` (the module tree), and `autoUpdater` (Electron Squirrel underneath the host path).
- A task model with named tasks `HostInstall` and `ModuleInstall`, driven through `checkForModuleUpdates` and `checkForUpdates`.
- `skipHostUpdate = settings?.get("SKIP_HOST_UPDATE")`, log `Host updates: disabled|enabled`. The host update can be suppressed independently of module updates.

Confidence: the relationships above are **Observed** at the code and identifier level. The precise runtime ordering is **Inferred** because I did not execute the updater.

## Phase 2: the five questions

### 1. Module lifecycle: discover, load, initialize, update, terminate

- **Discover** (Observed): the bootstrapper reads `installed.json` under `moduleInstallPath` to learn which modules and versions are locally present (`locallyInstalledModules`, `getModuleData`, `determineModuleData`).
- **Load** (Observed anchor, Inferred mechanism): a `Require` wrapper resolves modules against `${cwd()}/node_modules/${module}` and an `existingModule` path, so loading is ordinary CommonJS `require` against the staged tree rather than a bespoke loader.
- **Initialize** (Inferred): on first run the bootstrapper runs `firstRun.performFirstRunTask`, then writes a durable sentinel `firstRunCompletePath` (`existsSync` guard, then `writeFileSync(firstRunCompletePath, "true")`). This is a checkpoint: initialization runs once and records completion durably.
- **Update** (Observed): `checkForModuleUpdates` compares local `installed.json` against a remote manifest, downloads deltas to a staging area, unpacks zips with `yauzl`, and commits by rewriting `installed.json`. Retry uses `retryOptions` with `skip_host_download` / `skip_module_download` flags and exponential backoff `retryInSeconds = Math.min(...)`, surfaced to the splash as `splashState.seconds` via `setTimeout`.
- **Terminate** (Observed anchors, atomicity Inferred): host install completes through `quitAndInstall` and `app.relaunch`; the process exits and the newly staged tree loads on next start. Module swap is stage then relaunch, never hot patch of a running module. The anchors are observed; that the swap is atomic and corruption free is inference from `installed.json` plus `PENDING_UPDATES`, not observed, because the client module tree is absent on this machine.

Limitation: the loaded modules themselves are absent, so I observe the host side of the contract only. Module internal teardown is **not** evidenced.

### 2. Event propagation: how modules receive events, where routing lives

- **Main to renderer bridge** (Observed): `splashScreenPreload.js` exposes a fixed channel set over `ipcRenderer`: `DISCORD_SPLASH_SCREEN_READY`, `DISCORD_SPLASH_SCREEN_QUIT`, `ipcRenderer.on("DISCORD_SPLASH_UPDATE_STATE")`, `DISCORD_SPLASH_SCREEN_QUOTE`, and `DISCORD_*_BUILD_OVERRIDE`. The preload is the authorization boundary: only these channels cross into the splash renderer.
- **Routing location** (Observed): the main process owns routing. It emits updater progress as events (`checking-for-update`, `update-available`, `update-progress`, `update-downloaded`, `update-not-available`, `update-manually`, `update-error`, `host-updated`) and pushes state to the splash through `DISCORD_SPLASH_UPDATE_STATE`. Coordination is centralized in the main process, not distributed across modules.
- **System events** (Observed): `app.on` handlers for `ready`, `will-quit`, `suspend`, `render-process-gone`, `child-process-gone`, `uncaughtException`.

Takeaway: events flow from a central main process bus outward to constrained renderer channels. Modules consume, the host coordinates.

### 3. State ownership: local, remote, and what survives termination

- **Local, durable** (Observed): `installed.json` (module versions), `settings.json` (for example `SKIP_HOST_UPDATE`), the `firstRunCompletePath` sentinel, and staging dirs `PENDING_UPDATES`, `pending_requests`, `pending_minidump`. Paths resolve from `app.getPath` for `userData`, `appData`, `crashDumps`, `logs`.
- **Local, per viewer** (Observed on disk): `Local Storage` and `Session Storage` under the user data dir. These are the only runtime state present on this machine.
- **Remote** (Inferred, client absent): account identity, message history, and presence live server side behind the gateway. The bootstrapper holds none of it. Identity material binds to the keychain group `com.hnc.agekeys` (Observed in entitlements), not to a local file.
- **Survives termination** (Observed): the module tree plus `installed.json`, settings, and the first run sentinel. In flight update tasks survive as staged content in `PENDING_UPDATES`, which is the key continuity primitive.

### 4. Recovery: module failure, restart, network interruption, update failure

- **Update failure and network interruption** (Observed): the updater retries with backoff (`retryInSeconds = Math.min(...)`), toggling `skip_host_download` / `skip_module_download` so a partial success is not redone. Staged downloads in `PENDING_UPDATES` let a resumed attempt continue rather than restart.
- **Renderer or child crash** (Observed): `app.on("render-process-gone", (_, contents, details) => { let { reason } = details ... })` and a sibling handler that calls `addMessage` with a failure marker. The host observes the crash reason and reports it; relaunch is available through `app.relaunch`.
- **Application restart** (Observed): on start the bootstrapper rereads `installed.json`, so a restart deterministically reloads the last committed module set. A failed host install plausibly leaves the running set intact because the swap is stage then relaunch (Inferred, not observed; the client modules are absent).
- **Fatal path** (Observed): `fatal(err)` on first run task failure.

### 5. Continuity: reestablishing application state and presence after interruption

- **Code continuity** (Observed): deterministic. Restart reloads the committed module tree from `installed.json`. No code state needs reconstruction because code is durable on disk.
- **Session and presence continuity** (Inferred, client absent): the client reestablishes the gateway socket and resumes presence server side. The bootstrapper does not contain this; a count of `gateway|presence|heartbeat|websocket` tokens in `bundle.js` returns matches, but those belong to Sentry transport and Electron internals, not the Discord account gateway. I mark presence continuity **Inferred** and out of evidenced scope.

## Observed versus inferred summary

| Claim | Confidence | Anchor |
|---|---|---|
| Two layer split: signed shell + module host | Observed | `installed.json`, `moduleInstallPath`, signature |
| Stage then relaunch module swap (mechanism) | Observed | `quitAndInstall`, `app.relaunch`, `PENDING_UPDATES` |
| Swap is atomic and corruption free | Inferred | not observed; client module tree absent |
| First run checkpoint sentinel | Observed | `firstRunCompletePath` + `writeFileSync` |
| Update retry with backoff and skip flags | Observed | `retryInSeconds=Math.min`, `skip_host_download` |
| Central main process event bus to fixed renderer channels | Observed | `splashScreenPreload.js` channel set |
| Crash observation and relaunch | Observed | `render-process-gone`, `app.relaunch` |
| Gateway resume and presence | Inferred | client modules absent on disk |
| Identity bound to keychain not local file | Observed (entitlement) / Inferred (use) | `com.hnc.agekeys` |

## What did Discord teach us about persistent agents that we did not already know?

Three concrete lessons, each grounded:

1. **Separate the durable owner from the swappable worker, and treat the swap as atomic through relaunch rather than hot patch.** Discord never mutates a running module (Observed): it stages the next version, rewrites a single commit record (`installed.json`), and relaunches. Whether the swap is strictly atomic is inference, not observed. The signed shell is the durable owner; modules are disposable workers. For GameShark this says the supervisor identity and its adapters must be different lifetimes: the supervisor persists, adapters are staged and replaced, and the swap point is an explicit restart, not an in place mutation. GameShark makes the atomic commit a design requirement rather than assuming Discord proves it.

2. **A single durable commit record is the continuity anchor, not the process.** Discord's restart recovery is trivial because `installed.json` plus staged `PENDING_UPDATES` fully determine what loads next. The process is disposable; the on disk record is authoritative. GameShark should treat the process as cattle and put all resume truth in one durable, atomically rewritten record per responsibility.

3. **The first run sentinel is the smallest useful idempotency primitive, and it already lives before the work, not after.** `existsSync(firstRunCompletePath)` guards a once only task, and the sentinel is written only on success. That is check then act then mark, the exact ordering a persistent agent needs so a crash between act and mark is recoverable. What we did not have before as a concrete pattern: Discord shows the sentinel must record the **outcome** of a consequential action, so replay after a crash can distinguish done from not done. GameShark generalizes this one boolean sentinel into a per effect idempotency key journaled before the external call and marked complete after, which is the core of exactly once consequential behavior. To be precise about attribution: Discord is not observed to provide exactly once external operations. Exactly once consequence is a GameShark design goal that holds only under destination side assumptions (idempotency by operation id or a reliable probe); see `../../prototype/README.md` proven vs unproven.

The non obvious part: persistence in Discord is not process persistence at all. It is durable records plus deterministic reload. That reframes GameShark's target from keep the agent alive to make the agent's resume a pure function of durable state.
