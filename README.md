# GameShark Runtime

A clean-room research prototype for persistent agents: an agent that holds durable
responsibilities, survives process death, and resumes cross-environment work without
losing commitments or duplicating externally observable effects.

Deterministic by design. No LLM, no real game client, and all external integrations are
read only.

## Layout

- `prototype/` — the runnable implementation (Python 3.12+, SQLite, pytest) plus its own
  [README](prototype/README.md) with diagrams, schema, the invariant-to-test matrix, and
  the proven-vs-unproven guarantees. Start here.
- `architecture/` — the design: supervisor, agent lifecycle, continuity invariants, and
  the Discord-to-GameShark translation.
- `research/` — the read-only reverse-engineering analysis that motivated the design,
  with evidence and confidence levels.

## Versions

- **v0.1** — durable identity, fenced ownership leases, a checkpoint-before-effect journal
  for exactly-once consequence under documented destination assumptions, a durable event
  inbox, and crash recovery proven by real subprocess kills.
- **v0.2** — the first real responsibility: read-only GitHub pull request monitoring with
  deterministic relevance classification, deduplicated delivery to a local companion feed,
  durable cursors, and safe stop on authorization revocation.

## Run

```sh
cd prototype
uv run pytest -v        # full suite
uv run python demo.py   # v0.1 durable-core demo (real SIGKILL)
uv run python demo_v2.py  # v0.2 PR-monitoring demo across a restart and a revoke
```

## Scope and ethics

The reverse-engineering work was read only against locally installed, authorized software.
No proprietary third-party code is included in this repository. All runtime code is
original.
