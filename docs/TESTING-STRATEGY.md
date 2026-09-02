# PersonalDM testing strategy

The test suite exists to protect playable truth-engine behavior, not the current implementation shape.
A green build is useful only when a real player-visible regression would make at least one relevant
contract red.

## Test layers

### 1. Unit contracts

Use direct functions/services for deterministic mechanics with a narrow responsibility: identity
normalization, schema validation, projection filtering, lifecycle math, repository transforms, etc.

Unit tests may freely mock collaborators. They should assert the service's own contract, not claim to
prove that a complete turn works.

### 2. Integration contracts

Exercise several real services against the test database and assert durable state across their
boundary. Examples: Thesis Curator + lifecycle storage + ContextCompiler; action sequence + scene
transition + undo; Scribe + continuity + canon application.

Mock only external nondeterminism or the layer intentionally outside the scenario. Prefer asserting
stored truth and downstream readable state over private call counts.

### 3. Product contracts (`@pytest.mark.product_contract`)

Exercise a player-visible invariant through the application/API boundary. A product contract should
normally have the shape:

1. establish world state;
2. submit a player action through the public turn path;
3. inspect the published surface;
4. inspect durable world state through repositories/API/debugger;
5. assert both the desired result and forbidden corruption states.

Product contracts do **not** receive the suite-wide generic `TurnAuthorityPlanner.plan` happy-path
mock. If a scenario needs deterministic planning, it must explicitly provide that scenario's plan or
script model transport. This makes the test's seam visible instead of silently replacing gameplay
semantics for every endpoint test.

A product contract may mock LLM/model transport because model stochasticity is a separate acceptance
layer. It must not mock the persistence/materialization components whose result it claims to prove.

Good assertions:

- a newly discovered location exists once, has a useful durable description, is the active physical
  location, and survives a later read;
- an invalid new location transition creates no Location and no assistant canon turn;
- a dead character is absent from structured presence after narration tries to reintroduce them;
- one explicitly resolved thesis closes while independent same-type threads remain active;
- a compound player instruction executes every authorized step in order;
- `/undo` removes the turn's durable effects and restores the previous projection;
- rejected narration never becomes canonical surface;
- a generic no-change fallback is impossible when no typed outcome exists.

Bad assertions:

- exact equality of long prose when punctuation/wording is not the product contract;
- passing an object to a helper and only asserting that the helper returns the same object;
- mocking a repository/materializer and then claiming the test proves persistence;
- using one global generic successful plan for a scenario whose purpose is to test planning;
- asserting a historical fallback string simply because the implementation used to return it.

Exact text equality is appropriate only when the text itself is a protocol/token/API contract.
Otherwise assert semantic anchors, typed fields, durable identity and forbidden states.

### 4. Inter-agent semantic contracts

Use `@pytest.mark.interagent_contract_enforced` when the Planner/Validator/Scribe decision boundary is
the subject. Script `RoleModelRouter.generate_json` or equivalent model transport, then run the real
agent implementation, schema parsing, semantic reviewer and repair path.

This layer answers questions such as “does the reviewer repair a dropped compound movement?” It does
not need to repeat every database assertion from product contracts.

### 5. Live model acceptance

Deterministic CI cannot prove that the configured local/cloud models reliably obey semantic prompts.
Before declaring a large playability round complete, run a short adversarial live playtest against the
actual runtime. Use scenarios derived from previous production failures, then run a longer soak only
after the short acceptance passes.

Live acceptance is evidence in addition to CI, never a replacement for deterministic contracts.

## Forbidden-regression tests

Some bugs deserve explicit negative invariants because their presence is always wrong. Examples:

- no durable dead/destroyed character in live scene presence without an authoritative status change;
- no synthetic placeholder NPC identity becoming canon;
- no Session Zero placeholder Location becoming canon;
- no newly materialized gameplay Location with only a destination label and no durable public
  profile;
- no empty successful turn rebranded as generic “nothing changes” fiction;
- no technical UUID/status/authority diagnostics on the player-facing narrative surface.

These tests should fail closed. They must not replace semantic model judgment with growing keyword
heuristics; deterministic checks are appropriate only for machine-provable structural corruption or
known forbidden technical surfaces.

## CI layout

`Product contract acceptance` is the product-level gate. During migration it runs two groups:

- tests already marked `product_contract`;
- older strong cross-system scenarios selected explicitly from Round/stabilization files.

Migrate old scenarios incrementally. Do not mass-mark entire files when they mix unit and product
checks. Once all required scenarios carry the marker, the explicit legacy list can be removed and the
gate can become `pytest -m product_contract` only.

Subsystem workflows remain valuable for fast ownership and diagnostics, but a subsystem-green result
must never be interpreted as proof that the whole playable turn is healthy.

## Rule for every regression fix

When a live bug is fixed, ask four questions:

1. What product invariant was violated?
2. At which lowest authoritative boundary should the fix live?
3. Which deterministic test proves that boundary?
4. Which cross-system/product scenario would have caught the original player-visible bug?

If the new test only describes the new implementation and would stay green while the original bug is
reintroduced through another path, the regression is not covered yet.
