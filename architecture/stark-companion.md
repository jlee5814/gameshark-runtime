# Stark companion: presence, Jack In, and thin surfaces

Evidence behind these choices: [research/companions/analysis.md](../research/companions/analysis.md).

## Model

The runtime owns identity, duties, and conversations. A surface (the Mac panel, a paired
iPhone) is a view. Presence names the one surface acting as Stark's body, under a
monotonic generation. Jack In and Jack Out move presence; the agent never moves, and the
runtime stays on the Mac because stock iOS suspends background apps and runs no daemons.

## Invariants

- P1 single active presence: the `presence` row names exactly one surface; at most one
  handoff is open.
- P2 stale surfaces act on nothing: every surface write carries its presence generation and
  `guard_surface_in_txn` rejects a stale one inside the same transaction as the owner lease
  guard (I2 applied to surfaces).
- P3 every handoff resolves to one committed generation: deadlines persist as absolute
  times, so a runtime killed mid jack resolves identically after restart through `tick()`.
- Surface writes dedup by `client_msg_id` (I4); a dedup hit wins over the presence check,
  so a message that landed before a jack stays landed. Reconnect resync replays rows after
  `last_seq` (I5).

## Jack state machine (`gameshark/presence.py`)

```
preparing --ready_out | prepare deadline (3 s)--> committed  gen+1, surface = target
preparing --target lost--------------------------> aborted    presence unchanged
committed --ack_in-------------------------------> acked
committed --ack deadline (10 s) | target lost----> reverted   gen+1, surface = home,
                                                              or source when home failed
```

A silent source commits without its draft rather than stranding a live target. Crash
points `P1_after_prepare` and `P2_after_commit` are proven by real SIGKILL in
`tests/test_presence.py`.

## Surface writes (`gameshark/conversation.py`)

User messages land in the inference journal's `messages` table, so they are ordinary
context for the next model call; assistant rows still commit only through the journal.

## Not yet built

Gateway (Unix socket and pinned TLS), device pairing, Bonjour, StarkMac, StarkPhone,
animations, voice, notifications. See the phased plan in the PR description.
