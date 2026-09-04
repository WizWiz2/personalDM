# ADR-009: Truth Engine 2 — event-sourced temporal world model

Status: Proposed

## Context

The current canon pipeline asks Memory Scribe to perform several jobs at once: extract outcomes from
narration, identify entities, invent free-text fact keys, decide assert/revise/retract semantics, and
emit persistence-specific payloads. Small differences in wording can therefore split one world state
into several facts or entities. Additional lexical normalization and case-specific guards do not
scale to an open-ended RPG world.

The existing system already has useful foundations: stable Entity IDs, an Event table, temporal
relationship assertions, typed executor receipts, accepted proposal replay, and provenance fields.
Truth Engine 2 should evolve those foundations instead of replacing the whole application at once.

## Decision

Truth Engine 2 uses an event-sourced temporal world model.

### 1. Canonical events are primary history

A durable world change is represented by an immutable canonical event. The existing `events` table
remains the shared event row. `truth_event_records` marks TE2 events and adds campaign-local sequence,
idempotency key, source kind, source turn, payload, and active/reverted status.

Each canonical event owns zero or more immutable normalized effects. Effects are a small engine
protocol (`set_fluent`, `add_relation`, `remove_relation`, `record_mention`), not a vocabulary of game
concepts.

### 2. Current world state is a projection

`fluent_assertions` and `world_relation_assertions` are temporal projections derived from canonical
event effects. They record the event that opened the assertion and the event that ended it.

For a single-cardinality semantic type, opening a different value/target closes the previous current
assertion for the same stable semantic slot. Multi-cardinality types keep independent values/edges.

Replaying active canonical events in sequence must reconstruct the same current TE2 projection.
This is the basis for future undo integration.

### 3. Semantic identity uses stable IDs, not word lists

`semantic_types` is a dynamic campaign-local registry. A semantic type has a stable UUID, kind,
description, cardinality, and optional value schema.

There is deliberately no synonyms/keywords table and no domain vocabulary hardcoded in the reducer.
A later Candidate Retriever may use embeddings and context to retrieve likely semantic/entity IDs;
a constrained semantic model may select one candidate or `NEW`. Persistence always uses stable IDs.

### 4. Mentions do not define entity identity

`entity_mentions` records how text referred to an entity at a specific point in history. Several
mentions can point to one Entity ID. A role label, description, alias, and later revealed personal
name therefore do not require creating and merging duplicate entities.

Entity resolution is intentionally outside the foundation reducer. It will be introduced as
candidate retrieval plus constrained semantic selection, not string dictionaries.

### 5. Provenance is first-class

Canonical events can carry normalized evidence. Derived assertions link back to supporting events.
The engine can therefore answer not only "what is true now?" but also "which event supports this?".

### 6. LLMs do not own deterministic mechanics

Typed executor receipts for deterministic domains (movement, inventory, time, scene participation)
will be compiled into canonical events/effects by code in the next migration phase. A model must not
re-decide a physical change that the executor already confirmed.

A later Canon Compiler handles only semantic residuals that genuinely require interpretation. It
selects from machine-provided candidate IDs or proposes `NEW`; it does not write arbitrary database
mutations.

## Storage choice

SQLite remains the sole authoritative database for the local product. Graph-shaped relations are
stored relationally and can be traversed with SQL. A vector index/embedding model may be added later
for candidate retrieval, but TE2 correctness must not depend on a vector or graph extension.

A dedicated graph database may be introduced only as a rebuildable read index if real workloads show
that SQLite graph traversal is insufficient. It must not become a second source of truth.

## Relationship to ADR-008

ADR-008 remains valid for legacy domains while they are not migrated. Its mutable current fields and
accepted-proposal replay are compatibility projections during the transition.

For a domain migrated to TE2, this ADR supersedes the "mutable current field is source of truth"
part: canonical events become history, and current fields/cards become projections that must be
reconstructible from active TE2 events.

## Migration plan

1. Foundation: storage, canonical event store, temporal reducer, replay tests. No gameplay wiring.
2. Deterministic domains: movement, inventory, time, and scene participation emit TE2 events/effects.
3. Semantic compiler: entity candidate retrieval, semantic-type retrieval, observations and relations.
4. Migrate generic facts, NPC identity, and interpersonal relationships to TE2 projections.
5. Separate beliefs/claims and narrative theses from objective world truth.
6. Make context building read TE2 projections and provenance.
7. Remove superseded Scribe fixers, lexical normalization, and case-specific post-turn guards.
8. Rewrite live contracts around world invariants and replay equivalence rather than exact model payloads.

## Explicit non-goals of the foundation PR

- No synonym dictionaries or keyword maps.
- No graph database service.
- No vector dependency yet.
- No new LLM/agent calls.
- No change to production turn behavior yet.
- No deletion of legacy tables/guards until migrated domains are proven equivalent.
