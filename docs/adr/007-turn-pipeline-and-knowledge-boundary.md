# ADR-007: Turn pipeline and actor-scoped knowledge

**Status:** Accepted  
**Date:** 15 July 2026

## Context

Earlier documents described conflicting orders for Narrative LLM, Memory Scribe, Continuity Checker and state commit. They also proposed passing knowledge for all present NPCs in one shared context.

## Decision

Use this pipeline:

```text
persist input
→ compile context
→ stream narrative response
→ persist response
→ extract proposed changes
→ deterministic validation
→ human review / commit
→ async summaries and semantic warnings
```

For any significant NPC action or reply, compile an actor-scoped packet. Facts unavailable to the actor are excluded rather than merely labelled secret.

## Consequences

- Memory Scribe produces proposals before commit.
- Deterministic validators operate on typed proposals and tool calls.
- Semantic continuity checks are warnings, not retroactive blockers.
- Common scene context contains only observable state.
- Private knowledge is supplied only in the relevant actor packet.
- Context snapshots record actor and source IDs.

## Rejected alternatives

- **All NPC knowledge in one prompt:** rejected because instructions are weaker than information boundaries.
- **Validate before streaming:** rejected for MVP because it requires buffering the response.
- **Memory Scribe after commit:** rejected because Scribe proposes the changes that commit consumes.
