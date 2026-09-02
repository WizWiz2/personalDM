# Campaign Truth Engine transition matrix

This matrix is the source of truth for local model acceptance. GitHub CI proves deterministic code;
`test-models.bat` proves that the configured real models can drive that code from an unambiguous human
input into a structurally correct truth-state transition.

The live oracle is deterministic. It reads the isolated SQLite truth store after each real model turn;
no second LLM grades the first one.

## State transitions

| Domain | Transition | Semantic owner | Deterministic owner | Contract |
| --- | --- | --- | --- | --- |
| Scene | current scene -> new scene at known location | Planner | SceneTransitionExecutor / lifecycle | `movement_known_location` |
| Scene | A -> B -> A | Planner | SceneTransitionExecutor / identity | `movement_round_trip_identity` |
| Scene | ordered A -> B -> C | Planner + reviewer | ActionSequenceExecutor | `compound_two_movements` |
| Scene | time boundary creates/advances scene state | Planner | SceneTransitionExecutor | `time_explicit_advance` |
| Scene | observation does not advance time | Planner | scene state invariant | `time_no_accidental_advance` |
| Location | discover a new destination | Planner | Location identity/materializer | `movement_new_location_profile` |
| Location | new durable destination has useful profile | Planner + reviewer | location profile guard | `movement_new_location_profile` |
| Location | revisit does not duplicate entity | Planner | location identity | `movement_round_trip_identity` |
| Character | unknown responder becomes typed NPC in the actual current scene | Planner | NpcIntroductionResolver/materializer | `new_npc_direct_contact` |
| Character | source-scene NPC does not follow implicitly | Planner + validator | presence service | `movement_known_location` |
| Character | dead identity may be mentioned but not materialized | Planner + narrator | presence invariant | `dead_character_mention` |
| Identity | synthetic/CJK temporary identity is role-grounded or rejected | Planner | NpcIntroductionResolver | deterministic CI + live contact |
| Identity | temporary role -> stable revealed name, one entity | Planner/Registrar | identity promotion | `npc_temporary_to_stable_identity` |
| Item | owner -> current location (`drop`) | Planner | ActionSequenceExecutor | `item_drop` |
| Item | current location -> player (`take`) | Planner | ActionSequenceExecutor | `item_take` |
| Item | player -> present NPC (`give`) | Planner | ActionSequenceExecutor | `item_give` |
| Item | owner -> current location (`place`) | Planner | ActionSequenceExecutor | `item_place` |
| Fact | new observable state becomes durable fact | Scribe/auditor | canon application | `fact_create_from_observable_change` |
| Fact | old state -> superseded new state | Scribe | FactRepository/canon reconciliation | `fact_state_supersede` |
| Knowledge | NPC statement -> recipient knowledge with source NPC | narrator memory auditor/Scribe | BeliefRepository | `npc_claim_epistemics` |
| Knowledge | claim is not promoted to objective fact | narrator memory auditor/Scribe | memory authority rules | `npc_claim_epistemics` |
| Relationship | explicit satisfied relationship state supersedes old assertion | Scribe | RelationshipRepository | `relationship_explicit_resolution` |
| Thesis | one thread resolves | Curator | thesis lifecycle | `thesis_resolve_exactly_one` |
| Thesis | independent same-type threads survive omission | Curator | thesis lifecycle | `thesis_resolve_exactly_one` + deterministic Round43 |
| Event | one executed outcome is not written twice | Scribe/executor | EventRepository | `event_single_write` |
| Turn | meaningful negative/quiet result remains concrete | Planner | publication guard | `negative_result_is_concrete` |
| Turn | empty control result cannot become successful fiction | Planner/reviewer | dead-turn/publication guards | deterministic product contract + `negative_result_is_concrete` |
| Canon | unsupported player claim cannot overwrite objective truth | Planner/Scribe | canon conflict rules | `player_claim_cannot_overwrite_canon` |
| Compound | blocker at step B prevents C | Planner + reviewer | ActionSequenceExecutor | `compound_blocker_stops_tail` |
| Undo | undo movement/scene transition | none | TurnUndoService/replay | `undo_movement` |
| Undo | undo item mutation | none | TurnUndoService/replay | `undo_item_drop` |
| Undo | undo NPC materialization | none | TurnUndoService/replay | `undo_npc_creation` |
| Undo | undo turn-created fact | none | TurnUndoService/replay | `undo_turn_created_fact` |
| Restart | persisted truth is identical after process restart | none | persistence/runtime bootstrap | deterministic process-restart contract still required |
| Status | active -> inactive | semantic source dependent | entity status application | explicit transition contract still required |
| Status | active -> dead/destroyed | semantic source dependent | entity status/presence invariant | dead non-resurrection is covered; explicit status-change contract still required |

The two remaining status rows are intentionally not filled with a vague combat prompt. A live contract
is only useful when the correct transition is objectively decidable. We should add them when the
runtime has an explicit authoritative status-change input or fixture boundary. Restart is deterministic
runtime behavior and belongs in CI rather than being graded through an LLM.

## Contract classes

`core` contains short high-signal transitions that should be run frequently. Every core contract has a
100% pass requirement by default.

`extended` contains model-sensitive memory/identity/lifecycle transitions. They are still objectively
validated from stored truth, but a temporary pass-rate threshold can expose stochastic weakness while
we tune prompts. A threshold below 100% is technical debt, not permission to call the behavior
reliable.

Run the fast layer:

```bat
test-models.bat
```

Run the complete current catalog twice:

```bat
test-models.bat --suite all --repeat 2
```

Run one transition while debugging:

```bat
test-models.bat --case npc_claim_epistemics --repeat 3
```

Artifacts contain BEFORE, AFTER, semantic delta, generation errors and per-turn latency under
`src/backend/data/live-model-contracts/<timestamp>/`.

## Deterministic coverage

GitHub Actions separately measures branch coverage for `src/backend/app`. That number answers only:
"how much deterministic Python was exercised?" It is not semantic model coverage and must never be
used as evidence that Qwen/Gemma interpreted a player turn correctly.

Coverage is first measured as a baseline. Once the real baseline is known, CI should ratchet it upward
rather than inventing a decorative target that encourages mock-heavy tests.

## Completion rule

Do not delete an older test merely because a row has a live-model contract. A historical test can be
removed only when its independent responsibility is covered by one of these layers:

1. a deterministic unit test for the deterministic service/invariant;
2. a deterministic integration/product test for cross-service persistence;
3. a real-model local contract for semantic model compliance when an LLM owns the decision.

A long simulation remains a soak/stability benchmark. It must not be used as evidence that individual
semantic state transitions are correct.
