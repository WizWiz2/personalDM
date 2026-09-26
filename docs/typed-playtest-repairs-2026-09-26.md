# Repairs after the manual playtest

## Algorithmic boundaries

Meaning is resolved by the existing semantic agents. Deterministic code validates IDs, domain
membership, index coverage, scene scope, lifecycle state and retention, not vocabulary.

- Information requests are an ordered part of frozen player intent. Outcome resolution must cover
  every question index exactly once with an answer, explicit ignorance, refusal or deliberate
  deflection. `AddressedResponse` survives outcome → compiler → authority → narrator/validator.
  Spoken words remain attributed claims, not automatically objective canon. Safe publication can
  render the approved answers rather than fall back to a generic atmospheric beat.
  The existing narration reviewer supplies exact answer evidence for every question. Runtime and
  publication independently verify indexed coverage and quote existence; a bare model `pass` is
  insufficient. Semantic relevance still belongs to the reviewer, not a lexical matching rule.
- Response ownership binds to a present entity ID. A typed current addressee outranks stale `/talk`
  selection. Newly authorized speakers receive their ID when materialized. Name revelation does not
  authorize inventing a personal name, merging identities or creating a second entity.
- `/talk` uses the same planning and execution path as narrator-addressed input. Selecting a listener
  is an ownership hint, not permission to bypass player-intent extraction.
- Each approved movement carries an explicit companion roster. The compiler passes it to the
  existing scene-transition executor. Requested companions bypass ordinary solo-travel shortcuts;
  requesting company does not itself establish NPC consent. Native outcome schemas partition
  moving/stationary indices, so stationary actions cannot carry participants across locations.
- Location candidate retrieval includes grammatical lemmas from the existing language parser,
  with per-request caching. This allows inflected designations to reach the narrow semantic
  identity binder without automatically merging similarly named places or inventing aliases.
- Explicit negative player boundaries are retained as protected decisions. Semantic rendering and
  review contracts forbid converting sensory description into an unrequested voluntary action.
- Memory taxonomy no longer contains lexical classifiers or sentence-based texture extraction.
  Scribe supplies persistence classes and evidence-backed non-durable texture in its existing call.
  Runtime enforces scope, identity, retention and non-durable-over-durable precedence. Ambiguous
  aliases do not silently pick an entity. Legacy proposals retain explicit scope.
- Player memory excludes private facts and old scene-local state. Beliefs expose their source ID and
  turn provenance, with attribution rather than an unconditional “knows” label. Contradictory NPC
  claims remain possible; a lie must not be mistaken for objective truth.
- Established state is projected from active fact slots. Replaying a whole old receipt when one of
  its facts survives could resurrect superseded state of another subject; that replay is removed.
- Director pressure no longer manufactures a generic cliffhanger when no concrete hook exists.
  Rendering must complete the exchange before a grounded open choice or NPC opportunity.
- CLI does not wait for global background-memory idleness after each answer. Input yields through
  a thread so memory can progress while the player thinks. Cancelled jobs return to durable pending
  state; startup reschedules pending work, with per-turn dispatcher deduplication.
- Turn snapshots contain context/planning/preparation/presentation timings. Failed turns retain
  timing diagnostics too. Tied generation timestamps use insertion order, preventing undo from
  choosing an earlier completed run instead of the latest failed orphan.

## Verification and limits

The final full-suite run passed: **1029 tests**, with 313 warnings. The final small
guards for nonblank answer evidence and public established-state projection were also
checked by targeted runs (11 and 18 passing tests respectively). Changed Python files
passed Ruff checks; `git diff --check` passed.

Regression tests cover question coverage, native domain constraints, stable speaker identity,
safe answer publication, explicit companion compilation, memory scope/privacy/provenance,
semantic memory labels, cancellation recovery and non-blocking CLI input. The golden playthrough
now supplies its texture at the semantic Scribe boundary instead of expecting lexical extraction.

One real Qwen/Gemma CLI check initially exposed an invalid companion field on an observation; native
domain constraints were tightened. A repeated check completed with both requested answers and no
unrequested touch. That turn took approximately 29 seconds to plan and 38 seconds to prepare/render
and validate presentation. It also showed excess rhetorical closing prose, motivating removal of
the director's generic hook injection.

A later return trip exposed another old defect: a literal candidate gate treated `стеллажи` and
`стеллажам` as unrelated and created a new location without the NPC waiting at the original one.
Grammatical candidate retrieval now has a regression for that case. The extra trip also showed that
the model can still produce overblown prose and protagonist restaging despite prompt constraints;
this must remain part of the next sustained live quality audit, not be declared solved by unit tests.

This is not a new 50-turn quality benchmark. Green deterministic tests do not prove literary
quality, perfect model grounding or absence of local-model timeouts. Existing campaign history is
not rewritten or heuristically “cleaned”; stored old claims retain their original provenance.
