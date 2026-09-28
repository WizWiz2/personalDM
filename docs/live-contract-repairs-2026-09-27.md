# Live model contract repairs, 2026-09-27

The failing suite was `test-models.bat`, which uses real Ollama models and isolated SQLite databases.
The initial run `20260927T051301Z` failed 8 of 26 contracts with `gemma4:e4b` and `qwen2.5:7b`.
A preceding run with the same models, `20260926T042359Z`, passed 26/26.

## Runtime changes

- Compile item and recipient references into request-local schema enums from the engine's catalogs.
  Generated UUID typos and selection of the controlled character as an inventory recipient cannot
  pass this boundary. This is a catalog of actual game state, not a dictionary of natural-language
  expressions.
- Independently classify the actor and durable effect of each extracted action. Inventory, travel,
  temporal actions, local acts, and speech have different native decoding shapes. Registered item
  placement cannot remain generic prose without an executable ownership/location operation.
- Preserve action text and order. A disagreement that would erase an action or its typed durable
  effect receives a bounded semantic reconsideration with the original input and complete candidate.
- Classify destination references semantically as explicit or contextual. Candidate retrieval uses
  general grammatical/identity anchors. An explicit endpoint with no registered identity candidates
  remains new; contextual references can use the full scene catalog. A second model pass cannot
  replace a concrete new destination with an unrelated known location or reopen it as ambiguous.
- A movement's spatial field represents the intended effect and endpoint, including unsuccessful
  attempts. The outcome/compiler owns success, obstacles, and route topology. The schema excludes
  movement combined with a null endpoint or an absence of intended travel.
- Strip engine-generated UUID decorations when reading physical cast identities. Plain and decorated
  references to the same person no longer count as different cast members.
- Exclude the controlled protagonist as their own addressee. An unregistered interlocutor retains
  the designation supplied by the human and can be materialized by a typed NPC introduction.
- Bound response/introduction prerequisites to existing frozen action indices; an empty action list
  exposes only null prerequisites. Existing addressees without a name-revelation request expose
  null revelation fields rather than allowing an unrelated personal-name rewrite. An existing
  addressee also fixes response ownership in native decoding: the newly revealed personal name
  cannot replace the current entity designation and detach the approved answer from that NPC.
- Bind name-revelation evidence to the authoritative indexed answer rather than a separately
  generated paraphrase. Names absent from the spoken answer still fail validation; long replies
  yield an exact bounded quote.
- Reject an echoed question as an answer instead of manufacturing an ignorance claim.
- Respect `safe_mundane` when granting route discovery to action-sequence steps. An accepted compiled
  destination profile can materialize a new location through the normal transition transaction.

No lexical semantic lists, synonym tables, or sentence-classification regexes were added.

## Test contract maintenance

The live surface oracle rejects an entire engine fallback, rather than arbitrary occurrences of
its words within a substantive response. It still rejects empty publication and the complete stub.
Durable state and ordered-action assertions remain in place.

Older Python fixtures now represent the current boundaries: semantic silence selection, typed
`start_game` handoff, explicit starting location, current migration head, exact stored location name,
and diagnostic errors in the debugger rather than leaked implementation details in player prose.
Ambiguous navigation is supplied to the executor as unresolved, rather than as an already selected
concrete location from a mocked planner.

Regression tests cover UUID corruption, self-recipient/self-addressee errors, decorated cast
deduplication, nonexistent prerequisites, disputed action deletion, durable effects recovered from
generic extraction, attempted-movement consistency in both Python and native JSON schema, and the
surface-oracle false positive, authoritative name evidence, and binding the response owner
to an existing NPC.

## Validation

- `test-models.bat`: **26/26 passed** with `gemma4:e4b` and `qwen2.5:7b`; no repair cascade
  in 33 turns. Local report: `src/backend/data/live-model-contracts/20260927T204651Z/report.md`.
- Full deterministic backend suite: **1074 passed** (373.60 seconds).
- Coverage: combined **75.64%**, statements **79.73%**, branches **62.42%**; above the CI
  thresholds (72%, 76%, 57%). Ruff on changed Python files and `git diff --check` passed.
