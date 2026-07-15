# Documentation map

This repository contains canonical specifications and exploratory research. They are not equally authoritative.

## Precedence

1. Accepted ADR
2. `MVP-SPEC.md`
3. `product-foundation.md`
4. Proposed ADR
5. Amendments and research notes

## Canonical documents

- `product-foundation.md` — product vision, priorities and scope.
- `MVP-SPEC.md` — implementable MVP contract.
- Accepted ADRs — decisions that override general documents.

## Current priorities

- **P0:** local runtime, provider connection, immutable turn log, restart and continue.
- **P1:** scenes, scene theses, characters, facts, beliefs, goals, relationships, inspectable memory.
- **P2:** assisted canon plus manual scene image generation and simple local music.
- **P3:** retrieval, provenance tooling and continuity warnings.
- **P4:** advanced image consistency, music direction, rulesets and multiplayer.

## Boundaries

- SQLite is the only MVP storage backend.
- PostgreSQL is future server mode, not an early adapter.
- The LLM never receives all NPC secrets in one shared context.
- Relationships are assertions with reasons and provenance; numeric axes are optional views.
- Full branching is outside MVP. MVP supports undo and regenerate.
