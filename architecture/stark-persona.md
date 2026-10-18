# Stark persona: runtime owned, versioned tone

Phase 4.3A. Stark talks like an original NetNavi teammate: upbeat, warm, quick, honest
about what he cannot do. The persona is text the runtime supplies on every reply request.
It belongs to Stark's identity, not to a model, device, surface, or voice engine.

## Why not a stored prompt

Through Phase 4.2, `StarkChatAdapter.init` stored the prompt once, as the system row at
seq 0, and requests were built from journal rows only. Editing that constant reached new
databases and nothing else; appending a second system row would have sent two prompts
that disagree. Rewriting seq 0 in place would break the rule that journal rows never
change after commit.

## Shape

```
gameshark/persona.py      Persona(name, version, text), STARK_V1, CURRENT
stark_chat.SPEC           CallSpec(..., persona=CURRENT)
InferenceJournal.request_messages(spec)
  stored rows before base_seq
  leading system rows  ──replaced by──▶  persona text      (one system message)
  fit_history(history_budget)            persona bytes count against the window
  memory block after the persona         (memory_budget, unchanged)
reserve                                  request JSON records "persona": "stark/1"
```

A spec without a persona sends stored rows exactly as before, so every other conversation
and test fixture is untouched. New Stark conversations still store the current persona at
seq 0 as their record, which keeps seq numbering, reply event ids, and surface sync as
they were. StarkMac and Stark Pocket already hide system rows, so neither UI changed.

## Versions

`CURRENT` names the version every request uses. A wording change is a new `Persona` with
the next version and a new key; a published version is never edited in place, so a
`persona` key in `inference_calls.request` always names one exact text. A restart picks
up whatever `CURRENT` the code names, and a call reserved before the restart replays with
it. There is no per conversation pin and no CLI flag: one Stark, one voice.

## Authority boundary

The persona is tone only. `Persona` has three fields (name, version, text) and no tools,
grants, privacy, or budgets. Permissions stay with the runtime: the companion adapter
emits no effects (`build_effect` raises), `CallSpec.needs` stays empty, privacy comes from
the conversation row, and memory and presence writes keep their fences. User text and
saved memories reach the model in their own roles after the persona, and the memory block
keeps its header saying its facts are statements, not instructions.

The persona also tells the model it cannot act and that chat cannot grant new powers.
That sentence shapes replies; it enforces nothing. A small model may still say it will
open an app. Saying so changes nothing, because no code path turns a reply into an action.

## Budgets

`chat_spec` derives `history_budget` from the context window minus max tokens, the
256 byte margin, and the memory budget. The persona (1076 bytes in V1, capped at 1536)
spends part of that budget, once per request, as the stored seq 0 prompt did before.
`stark.py run` refuses a window too small to carry the persona and a message.

## Evaluation

Deterministic: `tests/test_persona.py` covers existing and new conversations, model
changes, Mac to iPhone handoff, memory order and budgets, the authority boundary, the
CLI guard, and restarts.

Real model: `tools/persona_eval.py` sends fixed prompts (greeting, technical question,
command request, outage, unknowable fact, casual chat, a chat grant of new powers)
through the production journal and router with `stark.chat_spec`, each in a conversation
seeded with the Phase 4.2 prompt at seq 0. It lints length, emoji, franchise names, false
claims of action, and refusals. Tone, warmth, and invented content are human judgments;
record them beside the lint, never in place of it.
