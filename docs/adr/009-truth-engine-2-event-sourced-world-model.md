# ADR 009: Truth Engine 2 event-sourced world model

## Status

Proposed and under active experimentation on `feat/truth-engine-2-foundation`. Do not merge this architecture into `main` until deterministic testing and repeated live-model shadow evaluation prove the migration path.

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

A relation can be established or explicitly ended through the same semantic type and entity IDs. Relation termination therefore does not require action-specific rules such as `give -> clear debt`; a semantic observation that the relation is no longer present emits `remove_relation`, and the reducer closes its temporal validity interval.

### Semantic types

`semantic_types` is a dynamic campaign-local registry with stable IDs, descriptions, cardinality, optional value schema, and an optional `system_key` for engine-owned protocol slots.

There are no synonym lists, keyword dictionaries, or lexical matching rules in the registry.

Engine-owned keys such as `core.entity.location` and `core.item.position` are ABI identifiers between deterministic executors and the reducer. They are never used to recognize player language. Open-ended world semantics use candidate retrieval plus constrained semantic resolution.

### Entity identity and mentions

`entity_mentions` records textual mentions separately from entity identity. Several labels can resolve to one stable entity, so a role label and a later personal name do not require creating two NPCs and merging them afterward.

Identity is owned by UUID, not display name. The legacy `(campaign_id, entity_type, canonical_name)` uniqueness constraint has been removed from the ORM and the TE2 migration. Distinct entities may therefore share a human-facing label such as `guard` while retaining different UUIDs.

For an unresolved `NEW` entity:

1. the backend creates the UUID;
2. the Entity row is a durable registry shell with TE2 provenance;
3. a canonical `record_mention` event establishes its active linguistic/world support;
4. future candidate retrieval sees TE2-created registry shells only when they still have active structural support such as a mention, graph edge, scene membership or explicit context.

Undo does not need to destroy and later recreate Entity rows. Reverting the origin event removes the active mention/projection during rebuild, so an unsupported registry shell disappears from future semantic candidate sets without breaking historical foreign-key identity.

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

A domain must not retain a second writer once TE2 owns it.

Structured movement is now single-writer: the old post-turn movement proposal/event reconciliation path has been removed. Inventory physical fields remain temporarily as a compatibility read projection because the existing runtime still reads them, but structured inventory canon is written by TE2 from executor receipts. The existing Scribe item-transfer guard prevents same-turn prose extraction from overwriting a structured executor receipt.

Legacy compatibility projections may remain during migration, but they are not a second semantic authority.

### Undo and replay

Undo does not delete canonical history. Events sourced from the undone user turn are marked `reverted`, then `WorldReducer.rebuild()` reconstructs TE2 projections from active events.

`ActiveCanonReplay` must not delete TE2-owned event rows. Legacy physical-state compensation remains temporarily while old context/read paths still depend on legacy tables and fields. It can be removed domain-by-domain after context reads TE2 projections directly.

### Candidate retrieval and constrained semantic resolution

Deterministic receipts must never be re-decided by an LLM. Open-ended semantic observations are canonicalized through a bounded resolution layer:

1. `TruthCandidateRetriever` produces a small candidate set from machine-known structure only;
2. entity candidates are prioritized by explicit context, active scene membership, TE2 graph adjacency and prior active mentions;
3. semantic-type candidates are prioritized by currently active slots for the resolved subject;
4. engine-owned `system_key` slots are hidden from open-ended semantic resolution by default;
5. `ConstrainedSemanticResolver` lets the model choose only an exact supplied UUID or `NEW`;
6. backend validation rejects IDs outside that bounded set;
7. `SemanticObservationCompiler` turns the resolved observation into canonical TE2 events/effects;
8. `WorldReducer` owns temporal supersession and graph mutation.

No word dictionary, synonym map, regex semantic recognizer, edit-distance identity or token-overlap identity heuristic is used. Candidate retrieval currently uses structural evidence. A later embedding ranker may reduce large candidate sets, but it will plug into this retriever boundary and remain a rebuildable index rather than truth storage.

For a genuinely new semantic slot, the model supplies only a proposed label, semantic description, cardinality and optional value schema. The backend creates the stable semantic UUID and attaches the creating canonical event as provenance. The model cannot invent an existing UUID.

### Semantic residual contract

The old Scribe emits table-specific persistence proposals (`fact`, `relationship`, `assert/revise/retract`, free-text subject/predicate keys). TE2 introduces a narrower model contract: `SemanticResidualEnvelope`.

The residual extractor emits only an observation graph:

- local entity references with mention text and coarse entity type;
- fluent observations: one entity, a semantic description, and an observed value;
- relation observations: two entity references, a semantic description, and whether the relation is present or explicitly absent.

Local references are not database identity. `SemanticResidualCompiler` resolves them through the constrained entity resolver and then routes observations through the generic semantic compiler.

The extractor is explicitly forbidden from emitting:

- persistence-table choices or mutation operations;
- structured movement/inventory/time effects already covered by executor receipts;
- unconfirmed player intentions;
- character claims, rumours or beliefs as objective truth;
- scene theses, plot hooks, mood or presentation detail.

Compilation is retry-idempotent at the semantic boundary. Before resolving an already-delivered fluent or relation again, the residual compiler checks its stable canonical event key. If the event already exists, it re-applies/rechecks the reducer instead of calling semantic resolution again. This prevents retries from creating orphan `NEW` semantic-type rows before event-store idempotency is reached.

### Read-only semantic shadow

The legacy Scribe is still the runtime semantic writer for generic facts and relationships. TE2 must not become a hidden second writer merely for evaluation.

`SemanticResidualShadowService` therefore runs only as an opt-in, read-only post-turn experiment:

1. legacy post-turn processing commits first;
2. shadow reads the active user/assistant pair;
3. it retrieves only active TE2 events whose `source_kind=executor_receipt` and `source_turn_id` is that user turn;
4. those receipts are supplied to the residual extractor so deterministic effects are excluded;
5. after the potentially long model read, the transaction is rolled back and turn activity is re-checked so a concurrent undo wins;
6. the residual envelope is written only to `assistant.context_snapshot["te2_semantic_shadow"]` as diagnostic metadata.

Shadow mode does not create Entities, SemanticTypes, FluentAssertions, WorldRelationAssertions or semantic canonical events.

It is disabled by default and enabled with `PDM_TE2_SEMANTIC_SHADOW_ENABLED=true`.

`test-models-shadow.bat` enables this mode for the isolated real-model contract suite and generates `te2-shadow-report.md` / `te2-shadow-report.json`. The report places TE2 residual observations beside the legacy Scribe proposals from the same assistant turn. It deliberately does not attempt to declare semantic equivalence through string matching.

## Layer separation

TE2 keeps different epistemic classes separate:

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
5. Candidate retrieval and constrained Canon Compiler. **Initial structural retriever/resolver/compiler slice implemented and deterministic tests added.**
6. Generic facts -> semantic fluents. **Generic compiler and residual contract implemented; legacy Scribe FACT writer still owns runtime.**
7. Generic relationships -> temporal relations. **Generic add/remove lifecycle implemented; legacy relationship writer still owns runtime.**
8. NPC identity -> entity mentions / semantic entity resolution. **Name uniqueness removed; `NEW` identity, duplicate display labels and undo-safe active support implemented.**
9. Read-only residual shadow against real model turns. **Runtime wiring and comparison-report tooling implemented; repeated live-model evaluation still required.**
10. Promote proven semantic domains from shadow to TE2 single-writer ownership, one domain at a time.
11. Separate beliefs and narrative theses from objective truth.
12. Context compiler reads TE2 projections as authoritative state.
13. Remove superseded Scribe fixers and legacy compatibility projections.
14. Repeated full live-model suite before considering merge to `main`.

## Rejected approaches

### Synonym/keyword dictionaries

Rejected. Open-ended language produces an unbounded number of paraphrases and domain concepts. Maintaining lexical equivalence lists would move semantic reasoning into brittle hand-written data.

### Regex/string-shape canon identity

Rejected as an authoritative semantic mechanism. Structural parsing of machine-owned protocol fields is acceptable; recognizing world meaning from prose through string shape is not.

### String-similarity retrieval as semantic identity

Rejected. Edit distance or token overlap can be useful for UI search but cannot decide whether two observations describe the same entity or semantic slot. Structural retrieval plus a bounded semantic judge is the minimum acceptable path; embeddings may later improve candidate ranking without becoming authority.

### A guard per failing live test

Rejected. Guard accumulation hides architectural ownership problems and creates whack-a-mole behavior.

### Shadow compilation into canonical state

Rejected. Running both legacy Scribe writes and TE2 semantic writes during comparison would create two competing authorities and contaminate the experiment. Shadow evaluation must remain diagnostic-only until a domain is deliberately transferred to TE2 single-writer ownership.

### Artificial unique-name suffixes

Rejected. Names are labels, not identity. Encoding identity as `Guard #2` would preserve the old lexical schema bug rather than fixing it.

### Separate authoritative graph database

Rejected for now. It adds deployment and transactional complexity without solving the semantic identity problem. SQLite is sufficient for the current local single-user workload.

## Consequences

Positive:

- deterministic state changes have one owner;
- undo/replay becomes event inclusion + rebuild;
- semantic identity stops depending on free-text fact keys or unique display names;
- provenance is explicit;
- historical state remains queryable;
- aliases and mentions do not require duplicate entities;
- semantic model failures are constrained to candidate decisions rather than arbitrary DB writes;
- generic fluents and relations share one compiler path instead of table-specific reconciliation rules;
- relation termination is a temporal graph operation rather than an action-specific fixer;
- semantic residual extraction can be evaluated against the existing runtime without becoming a second writer.

Costs:

- migration is incremental and temporarily maintains compatibility projections;
- structural candidate retrieval will need an embedding/ranking index before very large campaigns;
- current Fact/Relationship/Scribe infrastructure cannot be removed until TE2 runtime/context parity is proven;
- residual extraction adds an extra control-model call when shadow mode is enabled;
- live shadow output requires qualitative/contract-level evaluation before writer ownership can move;
- live contracts must ultimately validate world-model invariants rather than implementation-specific proposal shapes.
