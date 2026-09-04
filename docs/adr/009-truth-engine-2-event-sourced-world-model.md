# ADR 009: Truth Engine 2 event-sourced world model

## Status

Proposed and under active experimentation on `feat/truth-engine-2-foundation`. Do not merge this architecture into `main` until deterministic and live-model testing prove the migration path.

## Context

The current canon pipeline asks language models to do too many jobs at once: extract outcomes, resolve entity identity, invent free-text fact keys, choose mutation semantics, and emit persistence payloads. This creates semantic drift and makes lexical normalization, synonym lists, regex rules, and case-specific guards tempting. Those techniques do not scale to an open-ended RPG world.

Truth Engine 2 changes the ownership boundary. Machine-confirmed events and their effects are authoritative. Current world state is a temporal projection of those events. Language models may help resolve genuinely semantic ambiguity, but they do not own identity, event ordering, temporal supersession, provenance, undo, or persistence invariants.

## Decision

### Canonical history

Reuse the existing `events` table instead of creating a second competing event store. A row in `truth_event_records` marks an event as TE2-canonical and adds:

- per-campaign sequence ordering;
- stable idempotency key;
- source kind and source turn;
- structured payload;
- active/reverted inclusion state.

Canonical event effects are immutable. The effect protocol is deliberately small:

- `set_fluent`;
- `add_relation`;
- `remove_relation`;
- `record_mention`.

These are engine operations, not a vocabulary of game-world concepts.

### Temporal world projection

`fluent_assertions` stores state-like claims with stable semantic type IDs and event validity boundaries. `world_relation_assertions` stores graph-shaped entity-to-entity relations with the same temporal semantics. `assertion_support` records event provenance.

For a single-cardinality fluent, applying a different value closes the previous assertion and opens the new one. Applying the same value adds support rather than duplicating state.

### Semantic types

`semantic_types` is a dynamic campaign-local registry with stable IDs, descriptions, cardinality, optional value schema, and an optional `system_key` for engine-owned protocol slots.

There are no synonym lists, keyword dictionaries, or lexical matching rules in the registry.

Engine-owned keys such as `core.entity.location` and `core.item.position` are ABI identifiers between deterministic executors and the reducer. They are never used to recognize player language. Open-ended world semantics use candidate retrieval plus constrained semantic resolution.

### Entity mentions

`entity_mentions` records textual mentions separately from entity identity. Several labels can resolve to one stable entity, so a role label and a later personal name do not require creating two NPCs and merging them afterward.

### Structured executor receipts

`StructuredReceiptEventCompiler` consumes only machine-resolved structured executor state. It does not inspect Narrator prose.

Publication happens only after a prepared structured transition/action sequence becomes `applied`. A failed or rolled-back prepared action therefore never enters canonical history.

Current deterministic coverage includes:

- movement -> `core.entity.location`;
- inventory take/drop/place/give -> `core.item.position`;
- time/focus/blocked structured steps as canonical historical events;
- blocked actions without world-state effects.

Before the first TE2 mutation of a migrated slot, the compiler imports the previous machine-resolved state as a `legacy_baseline` event. This makes event-sourced undo possible even when the campaign existed before TE2.

### Single-writer migration

A domain must not retain a second post-turn writer once deterministic receipt compilation owns it.

Structured movement is now single-writer: the old post-turn movement proposal/event reconciliation path has been removed. Inventory physical fields remain temporarily as a compatibility read projection because the existing runtime still reads them, but structured inventory canon is written by TE2 from executor receipts. The existing Scribe item-transfer guard prevents same-turn prose extraction from overwriting a structured executor receipt.

Legacy compatibility projections may remain during migration, but they are not a second semantic authority.

### Undo and replay

Undo does not delete canonical history. Events sourced from the undone user turn are marked `reverted`, then `WorldReducer.rebuild()` reconstructs TE2 projections from active events.

`ActiveCanonReplay` must not delete TE2-owned event rows. Legacy physical-state compensation remains temporarily while old context/read paths still depend on legacy tables and fields. It can be removed domain-by-domain after context reads TE2 projections directly.

### Semantic residuals

Deterministic receipts must never be re-decided by an LLM. The Canon Compiler receives only semantic residuals that executors cannot know directly.

The semantic pipeline is now implemented as an initial vertical slice:

1. `TruthCandidateRetriever` produces bounded candidates from machine-known structure only;
2. entity candidates are prioritized by explicit context, scene membership, graph adjacency and prior TE2 mentions;
3. semantic-type candidates are prioritized by currently active slots for the resolved subject;
4. engine-owned `system_key` slots are hidden from open-ended semantic resolution by default;
5. `ConstrainedSemanticResolver` lets the model choose only an exact supplied UUID or `NEW`;
6. backend validation rejects candidate IDs outside that bounded set;
7. `SemanticObservationCompiler` turns resolved fluent/relation observations into canonical TE2 events/effects;
8. `WorldReducer` owns temporal supersession and graph mutation.

No word dictionary, synonym map, regex semantic recognizer or string-similarity heuristic is used for identity. Candidate retrieval currently uses structural evidence only. A later embedding ranker may reduce large candidate sets, but it will plug into the same retriever boundary and remain a rebuildable index rather than truth storage.

For a genuinely new semantic slot, the model supplies only the proposed label, semantic description, cardinality and optional value schema. The backend creates the stable semantic UUID and attaches the creating canonical event as provenance. A model cannot invent an existing UUID.

The first generic compiler slice supports:

- arbitrary fluent observations -> `set_fluent`;
- arbitrary entity-to-entity relation observations -> `add_relation`;
- different natural-language phrasings resolving to the same semantic UUID and therefore the same temporal slot;
- `NEW` semantic types becoming campaign-local schema elements with event provenance.

The old Scribe is not yet wired through this path. This is intentional: the new semantic contract is being proven independently before legacy FACT/RELATIONSHIP proposal writers are removed.

### Entity identity migration constraint

The existing `entities` schema still treats `(campaign_id, entity_type, canonical_name)` as unique. That is incompatible with TE2 identity: two distinct guards, servants or unnamed strangers must be allowed to share the same human-facing label while retaining different UUIDs.

Full `NEW entity` materialization is therefore blocked until that legacy uniqueness constraint is removed safely. TE2 will not work around it by encoding artificial identity into names. Stable UUIDs own identity; labels and mentions are presentation/linguistic data.

## Layer separation

TE2 will keep different epistemic classes separate:

- objective world truth: canonical events, fluents, relations;
- entity identity: entities and mentions;
- beliefs/claims: what a specific character believes or reports;
- narrative layer: theses, goals, unresolved threads;
- presentation: transient narrative detail.

A thesis is not a world fact, and an NPC belief is not objective truth.

## SQLite and graph representation

SQLite remains the authoritative database. Graph relationships are represented relationally by stable subject/type/object IDs and can be traversed with SQL/recursive CTEs. A separate graph database is not required for this architecture.

If future workloads justify a graph server, it should be a rebuildable read index, not authoritative storage.

## Migration order

1. Event/effect/projection foundation. **Implemented.**
2. Deterministic movement publication and undo. **Implemented; old movement writer retired.**
3. Deterministic inventory publication and undo. **Implemented; dedicated end-to-end tests added.**
4. Complete deterministic time/focus projection decisions where current-state materialization is useful.
5. Candidate retrieval and constrained Canon Compiler. **Initial structural retriever/resolver/compiler slice implemented and deterministic tests green.**
6. Generic facts -> semantic fluents. **Compiler path implemented; legacy Scribe FACT writer not migrated yet.**
7. Generic relationships -> temporal relations. **Compiler path implemented; legacy relationship writer not migrated yet.**
8. Remove legacy entity-name uniqueness and migrate NPC identity -> entity mentions / semantic entity resolution.
9. Beliefs and narrative theses separated from objective truth.
10. Context compiler reads TE2 projections as authoritative state.
11. Remove superseded Scribe fixers and legacy compatibility projections.
12. Repeated full live-model suite before considering merge to `main`.

## Rejected approaches

### Synonym/keyword dictionaries

Rejected. Open-ended language produces an unbounded number of paraphrases and domain concepts. Maintaining lexical equivalence lists would move semantic reasoning into brittle hand-written data.

### Regex/string-shape canon identity

Rejected as an authoritative semantic mechanism. Structural parsing of machine-owned protocol fields is acceptable; recognizing world meaning from prose through string shape is not.

### String-similarity retrieval as semantic identity

Rejected. Edit distance or token overlap can be useful for UI search but cannot decide whether two observations describe the same entity or semantic slot. Structural retrieval plus a bounded semantic judge is the minimum acceptable path; embeddings may later improve candidate ranking without becoming authority.

### A guard per failing live test

Rejected. Guard accumulation hides architectural ownership problems and creates whack-a-mole behavior.

### Separate authoritative graph database

Rejected for now. It adds deployment and transactional complexity without solving the semantic identity problem. SQLite is sufficient for the current local single-user workload.

## Consequences

Positive:

- deterministic state changes have one owner;
- undo/replay becomes event inclusion + rebuild;
- semantic identity stops depending on free-text fact keys;
- provenance is explicit;
- historical state remains queryable;
- entity aliases/mentions do not require duplicate entities;
- semantic model failures are constrained to candidate decisions rather than arbitrary DB writes;
- generic fluents and relations now have a common compiler path instead of table-specific reconciliation rules.

Costs:

- migration is incremental and temporarily maintains compatibility projections;
- structural candidate retrieval will need an embedding/ranking index before very large campaigns;
- current Fact/Relationship/Scribe infrastructure cannot be removed until TE2 runtime/context parity is proven;
- duplicate human-facing entity names require a legacy schema migration before `NEW entity` can be fully enabled;
- live contracts must ultimately validate world-model invariants rather than implementation-specific proposal shapes.
