# Independent live-agent regression — after PR #207

**Status:** prepared, not executed. Run only after review/CI; never treat scripted deterministic
simulation as evidence of live-play quality. Follow `docs/playtest-protocol.md` for causal trace.
Use the actual configured model/provider and production HTTP endpoints, with distinct fresh
campaigns. Do not patch the model's answers, inject mock decisions, or use a fixed transcript
of expected narrator prose.

## Prerequisites

- Record exact Git commit, model/provider by role, configuration, request IDs and timestamp.
- Keep PR unmerged. Record whether the test uses PR #207 directly or a later branch.
- Baseline from the **2026-10-12 Brynnholm report** (not reproduced here):
  74 published narrative turns, ~5 meta-turns; 9/47 safe_fallback (19%),
  mean ~75s and maximum 223s per game-master turn; 8 failed post-turn jobs.
  These are *historical reported observations*, not targets inferred from new CI.
- Log every attempt, including failed and compensated turns. Do not quietly retry them away.

## A. Semantic identity and same-name collision (30+ live turns)

- Introduce two different characters whose names share a stem: Макар and Макара.
  Establish separate voices, positions, roles and at least one distinct personal action.
  Verify separate entity IDs and independent location/presence after several turns.
- Use grammatical cases of an existing name: «запись Юнда», «говорю с Юндом».
  Verify no fresh character is minted from the inflection alone.
- Introduce a pier keeper, then a senior fisherman at the same place.
  A unique `slot_id` is **not** enough to merge them; compare role and independent evidence.
- Introduce a temporary-role NPC, then have them explicitly self-identify.
  Verify a single persistent ID and provenance of promotion; an uncertain identity must
  become a logged canon gap instead of silent loss.
- Report extra identity-arbitration model calls and timing separately. An extra model pass
  is acceptable only when an actual ambiguity is present.

## B. Physical causality and canon cardinality (30+ live turns)

- Put two different boats at different places; refer to each as «лодка» in some messages.
  Verify a physical ID and no spontaneous renaming, swapping or teleportation.
- Change one boat's location/state without changing the other's; inspect the active
  facts and `superseded_by` links. A text label is not physical identity.
- Use two puddles of blood in distinct positions and an inherently single-valued
  property (e.g. one named door open/closed). Both puddles must coexist; door revision
  must supersede only its own previous state.
- Move/remove a significant item (letter/key/boat contents) and verify ownership and
  location, including after a scene transition.
- Explicitly distinguish *what PR #207 currently protects* (typed fact identity)
  from *open functionality* (automatic first-class registration and placement of all
  consequential objects). A failure in the latter is expected to remain until shipped.

## C. Player actions, social movement and publication (40+ live turns)

- Issue ordered actions: «записываю X, выхожу к Y и говорю Z».
  Account for every committed world action, speech request and refused action in
  intent, action sequence and published narration. Don't treat reported 2/2
  contract-vs-step coverage as proof that the original 3/3 survived extraction.
- Ask an NPC to accompany the player, then try walking together. Check actual
  NPC consent and physical movement; no narration-only teleportation.
- Use /DM or /OOC and confirm it changes no physical world state.
- Exercise fallback boundaries; record RAW GOOD/PUBLISHED BAD, repairs and user-facing text.
- Run `/regenerate` for the newest narrative turn after materializing a movement or
  new entity. Verify historical pair is undone, only one replacement is active,
  and physical/canon effects exist once. Attempt to regenerate an older turn and
  verify a non-mutating error.
- Attempt overlapping turn publication and post-turn memory work; log both
  fingerprint conflicts and lock wait times, including >1 worker if supported.

## Evidence and gate

For every defect capture: input, expected vs actual typed delta, first failed pipeline
boundary, role/model, raw/repair/published prose, `/debugger/turns/{id}`, active
entity/fact/presence views, post-turn job errors, and timing. Include aborted turns
in denominators.

Hard safety gates: **zero** silent identity merges, unsupported physical teleportations,
unreported dropped voluntary actions, and unjustified deletion of current canon.
Quality measures: safe_fallback share and RAW GOOD/PUBLISHED BAD share should improve
against the reported 2026-10-12 baseline; median/p95/max turn latency and
additional arbitration calls must be reported without hiding outliers.

The test agent must remain an independent investigator: it should neither implement
fixes nor accept a code-level claim as evidence of successful gameplay. Report failures
even when fixed-branch unit tests pass.
