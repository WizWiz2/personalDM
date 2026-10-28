# QA-300: identity, physical state and bounded context

Baseline: `cc4133b9eab8bd50a3acfa896710b3795c0e50c2`. Validation date: 2026-10-28.

QA-300 exposed two competing descriptions of physical state: executor-owned item/character
positions and prose-derived facts or historical scene membership. This patch removes several
ways that stale memory could override the current world, and fixes document references across
turns and within multi-artifact creation actions.

## Changes

- Production Scribe facts select `subject_entity_id` from registered identities. They cannot
  write parallel `holder` or `location` facts; physical positions remain executor-owned.
- Migration `f4a5b6c7d8e9` binds exact, unambiguous legacy names/aliases. Unbound current facts
  and parallel position facts become historical. Binding aliases also reconciles single slots.
  **All source fact rows are retained.** On the QA-300 FINAL copy, 101 current facts became 17;
  unbound current facts and physical-position facts both became zero. This deliberately stops
  ungrounded legacy statements from acting as current canon. Downgrade does not reactivate them.
- Active-scene character cards use the same physical-location constraint as scene state.
  Historical participation remains available for replay and undo.
- New NPC descriptions can select an existing cast ID, including when the input uses a title
  or an inflected name. Reusing an identity does not authorize arrival from another location.
  Existing duplicate character records are not automatically merged.
- Context windows bound local facts to 24, other facts to 12, and beliefs to 8 per character.
  Beliefs remain stored; this is prompt selection, not deletion or automatic truth revision.
- Produced-item references must point to an earlier creation action in the same input.
  An explicit zero-based ordinal distinguishes multiple artifacts from one action. Previous-turn
  artifacts use their durable registered item IDs. Failed creation still blocks dependent acts.
- Inventory auditors select their evidence from exact candidate fragments, preventing invented
  quotations from terminating otherwise valid turns. Custody checks remain unchanged.
- Failed/cancelled generations close unfinished validation audits. Startup also closes audits
  left by terminal generations. SQLite busy timeout is 30 seconds.
- `scripts/soak_metrics.py` reports snapshot metrics using read-only SQLite access. Its blocks
  include attempts whose start times fall between consecutive 50-GM publication boundaries.
- The deterministic simulation fixture now provides complete inventory reviews for its controlled
  item-free prose, grounded Scribe identities, and distinct speech on successive turns. It refuses
  to certify positional prose it cannot interpret.

## Validation

- Full backend suite: **1244 passed, 1 xfailed, 1 non-strict xpassed** (436.92 seconds).
  After making artifact ordinal selection mandatory, the affected wire/interpreter/regression
  suite passed **78 tests**. CI must validate the final commit on Python 3.12.
- Deterministic quality simulation: **60/60 completed**, 12 scenes across 3 acts, zero failed
  generation runs, zero unbound current facts. Mock usage is not evidence of real token costs.
- Real local API on an isolated QA-300 database copy, ChatGPT provider: **6/6 published**:
  create and place a note; take it and travel to Vera; hand it over on the next turn;
  have Vera read it verbatim; leave; return and ask about its custody. The final item owner
  remained Vera, and the quoted note content was shown. No new unbound current facts appeared.
- The live check first reproduced `Item position evidence is not in the candidate`; the
  extractive evidence schema was added before the successful six-turn sequence.
- Migration was exercised on a synthetic legacy database and a copy of QA-300 FINAL.
  Original campaign files and `C:\Pet\personalDM` were not changed.
- Local interpreter/site-packages: the existing main-checkout venv, Python 3.14.2. `PYTHONPATH`
  explicitly selected this checkout. Windows pytest temporary-directory permissions required
  an external runner using inherited directory ACLs; this workaround is not application code.

Example metrics command, from repository root:

```powershell
python scripts/soak_metrics.py C:\path\campaign.db.FINAL --campaign CAMPAIGN_UUID
```

## Findings that must not be overstated

The FINAL snapshot has 98 scenes, 71/101 previously unbound current facts, 63 current beliefs
with no supersession, and several suspicious NPC identity variants. However:

- A fresh scene on an actual return visit is intentional: `_scene_at` documents QA-209 F-16,
  where reusing old scenes resurrected departed occupants and objects. This patch preserves
  that lifecycle and its undo semantics; 98 scenes alone does not prove 98 duplicate visits.
- Untyped fact subjects do have text-key supersession. Their problem is fragmented identity,
  not a total absence of supersession.
- Beliefs can be independent claims, mistakes or private knowledge. They do not all require a
  fact ID, and learning that a fact changed must not grant an absent NPC new knowledge.
- Missing shutdown/traceback messages do not establish why Windows terminated a process.
  Detached generation must survive browser navigation; cancelling every disconnect is unsafe.

## Remaining acceptance work

This is a verified repair batch, not a claim that all QA-300 acceptance criteria now pass.
A 200–300-turn real-provider soak is still required to measure input growth against 15%,
repair/refusal frequency, identity stability and process survival. The complete authority item
snapshot can still grow with the world's inventory. NPC-announced route creation (F-108),
general scene-visit inflation, clock progression and closing-beat repetition need separate
evidence and changes. No speculative duplicate-person merge or scene-history rewrite is included.
