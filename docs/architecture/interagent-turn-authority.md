# Inter-agent Turn Authority

**Status:** implementation contract  
**Date:** 8 August 2026

## Problem

Live playtests repeatedly exposed the same class of failures even after local prompt fixes:

- Planner allows or implies an outcome that Narration Validator later rejects;
- Narrator invents movement or protagonist actions that were never committed structurally;
- a new NPC is only registered after narration, while the pre-publication validator requires every speaker to already be a scene participant;
- Memory Scribe re-infers movement and other state from prose after the structured executor has already committed authoritative state;
- normal tests mock Planner and Validator globally, so a green suite does not prove that their hand-off is coherent;
- post-turn LLM jobs execute synchronously in the interactive path and multiply latency.

The root cause is not one bad prompt. Each agent currently reconstructs authority from a long mixed prompt instead of consuming one typed hand-off.

## Decision

A narrative turn has exactly one typed `TurnAuthority` object.

```text
user input
  -> Context Compiler
  -> Turn Authority Planner (structured LLM)
  -> deterministic scene/action execution
  -> TurnAuthority build
  -> Narrator renders approved authority
  -> Authority Validator validates against the same object
  -> deterministic outcome materializer
  -> save/commit
  -> enqueue post-turn memory jobs
  -> return control to player
          |
          +-> background Registrar/Scribe/Curator
```

### Authority ownership

| Concern | Owner | May another agent override it? |
|---|---|---|
| player input / voluntary protagonist action | human player | no |
| turn resolution and intended observable consequences | Turn Authority Planner | narrator may only render |
| scene/location transition | deterministic executor | no |
| current scene participant set | deterministic scene state | no |
| explicitly planned new NPC introduction | Turn Authority Planner + deterministic materializer | narrator may only render |
| prose/style | Narrator | yes, as long as authority is preserved |
| continuity verdict | Authority Validator | may request prose repair, never rewrite state |
| durable memory not already owned by structured state | Memory Scribe | only after deterministic validation |

## New NPC protocol

The old protocol was circular:

1. Narrator could invent a new NPC.
2. Validator rejected the NPC because it was not in `scene_participants`.
3. EntityRegistrar would only create the NPC after validation.

The new protocol removes the circle:

1. Planner emits `npc_introductions[]` in structured output.
2. `TurnAuthority` marks exactly those names as allowed introductions.
3. Validator treats only those names as legal new characters. Known absent characters remain illegal.
4. After accepted narration, the deterministic materializer creates the planned NPC and adds it to the authoritative scene.
5. EntityRegistrar remains a legacy/fallback extractor, not the primary source of an NPC that the turn depends on.

## Player agency protocol

The protagonist is never represented as an NPC in actor-scoped context. The authority object carries the player character identity and the exact latest human input. The Validator treats invented voluntary protagonist dialogue/actions as a hard error.

## Scene protocol

The planner must make an explicit scene-disposition decision for every narrator turn. A structured location/time/focus boundary must match the corresponding scene transition. Ordered movement steps that auto-succeed must carry their own structured transition.

If planning fails, the runtime uses a conservative typed fallback instead of silently running an unconstrained narrator.

## Validation protocol

Validator input is the compact typed authority plus candidate prose, not the entire mixed narrator prompt. This removes duplicated and contradictory instructions and reduces control-model context cost.

The validator may request one targeted prose repair. It cannot change scene state, introduce a different NPC, or reinterpret player intent.

## Provider completion protocol

An explicit provider `done_reason=stop` is authoritative completion. Punctuation is only a fallback heuristic when the provider did not report a finish reason. A normal response without terminal punctuation must not trigger a continuation/retry.

## Post-turn protocol

Interactive latency ends after the accepted turn is persisted and post-turn jobs are durably queued. Registrar, Scribe and Curator run outside the critical path. Their failure may affect memory freshness but must not delay or invalidate the already accepted narrative turn.

## Testing

The suite has three distinct layers:

1. deterministic unit tests may mock individual model roles;
2. inter-agent contract tests use deterministic fake outputs but execute the real Planner -> Authority -> Validator/materializer hand-off;
3. live-model acceptance remains a separate local playtest/benchmark and is never confused with deterministic CI.

A prompt-string assertion is not sufficient evidence that two agents agree on authority.
