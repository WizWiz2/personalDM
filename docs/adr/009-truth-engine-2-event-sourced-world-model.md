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

Engine-owned keys such as `core.entity.location` and `core.item.position` are ABI identifiers between deterministic executors and the reducer. They are never used to recognize player language. Open-ended world semantics will later use candidate retrieval plus constrained semantic resolution.

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

Deterministic receipts must never be re-decided by an LLM. The future Canon Compiler receives only the semantic residual that executors cannot know directly.

The intended semantic pipeline is:

1. retrieve a small candidate set of existing entities and semantic types;
2. let the model choose only an exact candidate ID or `NEW`;
3. validate the choice in code;
4. emit canonical events/effects;
5. let `WorldReducer` own temporal mutation.

The model cannot invent an existing ID and does not directly mutate persistence.

Candidate retrieval may later use embeddings/vector search, but vector storage is an index, not the source of truth.

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
5. Candidate retrieval and constrained Canon Compiler.
6. Generic facts -> semantic fluents.
7. Generic relationships -> temporal relations.
8. NPC identity -> entity mentions / semantic entity resolution.
9. Beliefs and narrative theses separated from objective truth.
10. Context compiler reads TE2 projections as authoritative state.
11. Remove superseded Scribe fixers and legacy compatibility projections.
12. Repeated full live-model suite before considering merge to `main`.

## Rejected approaches

### Synonym/keyword dictionaries

Rejected. Open-ended language produces an unbounded number of paraphrases and domain concepts. Maintaining lexical equivalence lists would move semantic reasoning into brittle hand-written data.

### Regex/string-shape canon identity

Rejected as an authoritative semantic mechanism. Structural parsing of machine-owned protocol fields is acceptable; recognizing world meaning from prose through string shape is not.

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
- semantic model failures are constrained to candidate decisions rather than arbitrary DB writes.

Costs:

- migration is incremental and temporarily maintains compatibility projections;
- semantic candidate retrieval and Canon Compiler still need implementation;
- current Fact/Relationship/Scribe infrastructure cannot be removed until TE2 context parity is proven;
- live contracts must ultimately validate world-model invariants rather than implementation-specific proposal shapes.
