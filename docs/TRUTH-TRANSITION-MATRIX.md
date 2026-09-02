# Campaign Truth Engine transition matrix

This matrix is the source of truth for local model acceptance. GitHub CI proves deterministic code;
`test-models.bat` proves that the configured real models can drive that code from an unambiguous human
input into a structurally correct truth-state transition.

The live oracle is deterministic. It reads the isolated SQLite truth store after each real model turn;
no second LLM grades the first one.

## State transitions

| Domain | Transition | Semantic owner | Deterministic owner | Local contract |
| --- | --- | --- | --- | --- |
| Scene | current scene -> new scene at known location | Planner | SceneTransitionExecutor / lifecycle | `movement_known_location` |
| Scene | A -> B -> A | Planner | SceneTransitionExecutor / identity | `movement_round_trip_identity` |
| Scene | ordered A -> B -> C | Planner + reviewer | ActionSequenceExecutor | `compound_two_movements` |
| Scene | time boundary creates/advances scene state | Planner | SceneTransitionExecutor | `time_explicit_advance` |
| Scene | observation does not advance time | Planner | scene state invariant | `time_no_accidental_advance` |
| Location | discover a new destination | Planner | Location identity/materializer | `movement_new_location_profile` |
| Location | new durable destination has useful profile | Planner + reviewer | location profile guard | `movement_new_location_profile` |
| Location | revisit does not duplicate entity | Planner | location identity | `movement_round_trip_identity` |
| Character | unknown responder becomes typed NPC | Planner | NpcIntroductionResolver/materializer | `new_npc_direct_contact` |
| Character | source-scene NPC does not follow implicitly | Planner + validator | presence service | `movement_known_location` |
| Character | dead identity may be mentioned but not materialized | Planner + narrator | presence invariant | `dead_character_mention` |
| Identity | synthetic/CJK temporary identity is role-grounded or rejected | Planner | NpcIntroductionResolver | deterministic CI + extended live contact |
| Item | owner -> current location (`drop`) | Planner | ActionSequenceExecutor | `item_drop` |
| Item | current location -> player (`take`) | Planner | ActionSequenceExecutor | `item_take` |
| Item | player -> present NPC (`give`) | Planner | ActionSequenceExecutor | `item_give` |
| Item | owner -> current location (`place`) | Planner | ActionSequenceExecutor | `item_place` |
| Fact | new observable state becomes durable fact | Scribe/auditor | canon application | covered indirectly; dedicated fact-create case still required |
| Fact | old state -> superseded new state | Scribe | FactRepository/canon reconciliation | `fact_state_supersede` |
| Knowledge | NPC statement -> recipient knowledge with source NPC | narrator memory auditor/Scribe | BeliefRepository | `npc_claim_epistemics` |
| Knowledge | claim is not promoted to objective fact | narrator memory auditor/Scribe | memory authority rules | `npc_claim_epistemics` |
| Relationship | explicit satisfied relationship state supersedes old assertion | Scribe | RelationshipRepository | `relationship_explicit_resolution` |
| Thesis | one thread resolves | Curator | thesis lifecycle | `thesis_resolve_exactly_one` |
| Thesis | independent same-type threads survive omission | Curator | thesis lifecycle | `thesis_resolve_exactly_one` + deterministic Round43 |
| Event | executed world outcome records durable event once | Scribe/executor | EventRepository | movement/item cases expose events; dedicated no-duplicate case still required |
| Turn | meaningful negative/quiet result remains concrete | Planner | publication guard | `negative_result_is_concrete` |
| Turn | empty control result cannot become successful fiction | Planner/reviewer | dead-turn/publication guards | deterministic product contract + `negative_result_is_concrete` |
| Undo | undo movement/scene transition | none | TurnUndoService/replay | `undo_movement` |
| Restart | persisted truth is identical after process restart | none | persistence/runtime bootstrap | dedicated process-restart case still required |
| Status | active -> inactive | semantic source dependent | entity status application | dedicated case still required |
| Status | active -> dead/destroyed | semantic source dependent | entity status/presence invariant | dead non-resurrection covered; explicit status-change case still required |
| Canon | contradiction without authority must not overwrite current truth | Planner/Scribe | canon conflict rules | dedicated contradiction case still required |
| Compound | blocker at step B prevents C | Planner + reviewer | ActionSequenceExecutor | dedicated live blocker case still required |
| Undo | undo item/fact/NPC creation | none | TurnUndoService/replay | deterministic coverage exists; dedicated live cases still required |
| NPC identity | temporary role -> stable revealed name, one entity | Planner/Registrar | identity promotion | deterministic Round42; dedicated live reveal case still required |

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

## Completion rule

Do not delete an older test merely because a row has a live-model contract. A historical test can be
removed only when its independent responsibility is covered by one of these layers:

1. a deterministic unit test for the deterministic service/invariant;
2. a deterministic integration/product test for cross-service persistence;
3. a real-model local contract for semantic model compliance when an LLM owns the decision.

A long simulation remains a soak/stability benchmark. It must not be used as evidence that individual
semantic state transitions are correct.
