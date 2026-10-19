# GameShark Runtime

A clean-room research prototype for persistent agents: an agent that holds durable
responsibilities, survives process death, and resumes cross-environment work without
losing commitments or duplicating externally observable effects.

Deterministic by design. No LLM, no real game client, and all external integrations are
read only. Stark can hold a real GitHub pull request monitoring responsibility across
process restarts without a second prompt.

## Layout

- `prototype/` — the runnable implementation (Python 3.12+, SQLite, pytest) plus its own
  [README](prototype/README.md) with diagrams, schema, the invariant-to-test matrix, and
  the proven-vs-unproven guarantees. Start here.
- `architecture/` — the design: supervisor, agent lifecycle, continuity invariants, and
  the Discord-to-GameShark and [Bionic to GameShark](architecture/bionic-to-gameshark.md)
  translations, and the v0.3 [inference router](architecture/inference-router.md) design.
- `research/` — the read-only reverse-engineering analysis that motivated the design,
  with evidence and confidence levels: [Discord](research/discord/deep-analysis.md) and
  [Bionic (LM Studio)](research/bionic/deep-analysis.md).

## Versions

- **v0.1** — durable identity, fenced ownership leases, a checkpoint-before-effect journal
  for exactly-once consequence under documented destination assumptions, a durable event
  inbox, and crash recovery proven by real subprocess kills.
- **v0.2** — persistent real world responsibility: a `stark` daemon and CLI monitor real
  GitHub pull requests read only (reviews, change requests, comments, CI check runs),
  classify activity deterministically, deliver each meaningful change once to a local
  companion feed, and survive SIGKILL, offline periods, rate limits, and token revocation.
  Design: [architecture/github-monitor.md](architecture/github-monitor.md).
- **v0.3 (design only, not implemented)** — a model agnostic inference router: stateless
  providers behind one OpenAI compatible contract (SGLang, MLX, LM Studio/Bionic, cloud),
  capability and budget admission, a fenced inference journal, and a drain then fence
  handoff, so Stark switches local and cloud models without losing identity or duties.
  Design: [architecture/inference-router.md](architecture/inference-router.md).
- **v0.4 (in progress)** — Stark companion: a fenced presence lease and two phase Jack In
  handoff so a floating Mac avatar and a paired iPhone act as thin views over one runtime.
  Runtime core landed; StarkMac (floating panel over an authenticated Unix socket) landed;
  Stark Pocket (iPhone) landed. Phase 4.1: replies come from a real OpenAI compatible
  model through the v0.3 router, with visible waiting, failure, and retry states,
  accepted by hand on a physical iPhone for the recorded scenarios
  ([ios/StarkPocket/README.md](ios/StarkPocket/README.md)). Phase 4.2: facts the user
  explicitly saves persist in the runtime, reach every reply through their own budget,
  and can be corrected or forgotten, also accepted by hand on a physical iPhone for the
  recorded scenarios ([architecture/stark-memory.md](architecture/stark-memory.md)).
  Phase 4.3A: Stark speaks with a runtime owned, versioned persona on every reply, accepted
  by hand on StarkMac and a physical iPhone for the recorded scenarios
  ([architecture/stark-persona.md](architecture/stark-persona.md)).
  Phase 4.3B: Stark reads his new replies aloud on the surface he is present on, accepted
  by hand on StarkMac and a physical iPhone for the recorded scenarios, with one silent
  relaunch observation open
  ([architecture/stark-voice-output.md](architecture/stark-voice-output.md#phase-43b-acceptance-physical-iphone-and-starkmac-2026-10-18)).
  Phase 4.3C: the user holds a microphone, speaks, reviews the on device transcript, and
  chooses Send or Insert into Composer, accepted by hand on StarkMac and a physical iPhone
  for the recorded scenarios, with several steps not verified and two observations open
  ([architecture/stark-voice-input.md](architecture/stark-voice-input.md#phase-43c-acceptance-physical-iphone-and-starkmac-2026-10-19)).
  Design: [architecture/stark-companion.md](architecture/stark-companion.md),
  app: [mac/StarkMac/README.md](mac/StarkMac/README.md).

## Run

```sh
cd prototype
uv run pytest -v        # full suite
uv run python demo.py   # v0.1 durable-core demo (real SIGKILL)
uv run python demo_v2.py  # v0.2 PR-monitoring demo across a restart and a revoke

uv run python stark.py watch owner/repo#123   # assign once (token: GITHUB_TOKEN or gh)
uv run python stark.py run --notify --inference-url http://127.0.0.1:30000/v1 \
    --model <name>                            # serve across restarts; --fake-model for a demo
uv run python stark.py feed                   # unread updates

../mac/scripts/build-app.sh                   # StarkMac.app; open it while `run` serves
../mac/scripts/demo.sh                        # the seven StarkMac acceptance checks
```

## Scope and ethics

The reverse-engineering work was read only against locally installed, authorized software.
No proprietary third-party code is included in this repository. All runtime code is
original.
