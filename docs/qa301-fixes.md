# QA-301 continuity fixes

QA-301 on `804749d` completed 100 turns, but published prose could diverge from the physical
receipts and conversation history. These changes address the demonstrated boundaries without
adding another model call to the interactive pipeline.

| Evidence | Change |
|---|---|
| F-114: an operator announces reading without giving any values | `item_read` preserves an addressed reading request as a service action; a successful read requires an item-bound content disclosure before execution. Rereads keep the first active disclosure. |
| F-115: a sheet is confused with the paper supply | Native inventory decoding explicitly selects the whole registered object. Producing a portion or written artifact is creation with its own reserved ID; `written_content` persists at that creation step. Later handoffs use the produced object's identity. |
| F-116: repairs alternately require opposite item positions | Each required item review and each claim are bound to one exact item ID. Position denials have an explicit polarity. Custody/placement corrections come from comparison with receipts, while semantic content/property/agency checks remain model-owned. |
| F-118: an invisible operator appears in a newly profiled room | `resident_role` alone no longer manufactures a keeper at the authority boundary. A concrete typed encounter is required. Offscreen introductions in production are limited to personal identities frozen from the input. |
| F-119: memory jobs fail during extraction/apply | Fact proposals require a registered subject in the native schema, not just a post-generation validator. Memory apply discards its old SQLite read snapshot after acquiring the campaign barrier. |
| F-120: a provider stall leaves a run running for nine hours | Structured narrator requests have their own wall-clock budget. The whole narrative saga has a deadline and uses its existing compensation path, marking timeout as failed rather than user cancellation. |
| F-121: a confirmation and promise disappear from usable memory | Exact frozen player speech and the certified response beat are committed with publication as sourced knowledge, using accepted proposals for undo/replay. Relevance recall retrieves a speaker's old lines independently of the listener's recent window. |
| Stale initiatives repeat fulfilled conditions | The existing outcome call reviews every candidate agenda against current evidence. Resolved/superseded agendas receive source-bound closures. Only active reviewed threads can drive independent moves; undoing a closure's source reopens it. |
| M-2: legacy descriptions disagree with return observations | `presence_check` observations use current structured occupant locations at the relevant hop, instead of accepting an obsolete scene description as physical evidence. |

## Compatibility and limits

- No destructive save migration or automatic merging of existing NPCs is included. Historical
  duplicates and previously missing documents are not silently repaired by these changes.
- Reading, artifact production and presence checks still require correct semantic extraction.
  Native contracts prevent malformed execution; live-model behavior needs another acceptance run.
- Published speech is evidence of what someone said, not proof that their statement is objectively
  true. Player speech in compound travel turns is not assigned to a destination cast without a
  typed speech timestamp.
- Default budgets are `NARRATOR_LLM_TIMEOUT_SECONDS=180` and `TURN_TIMEOUT_SECONDS=600`.
  Explicit cancellation remains cancellation. Provider failure and timeout publish no partial turn.
- QA-301 contained five failed memory jobs: one SQLite lock error and four missing-subject schema
  failures. Those are separate failure modes. The cause of the server process's disappearance was
  not established and is not claimed to be fixed here.

## Acceptance rerun

Use an isolated copy of each save and the same model/settings as QA-301. Check: actual values
when documents are read; separate supply/note identities and the note's literal text after a handoff;
two similar documents during repeated repairs; a named NPC and a new room with the same resident
role; an introduction/confirmation recalled after 30+ intervening turns; goals staying closed;
ordinary returns in the migrated campaign; provider timeout after a prepared transfer; and undo of
speech memory, transfers, and thread closures. Keep turn status, physical receipts, memory-job errors
and narration traces separate in the verdict.
