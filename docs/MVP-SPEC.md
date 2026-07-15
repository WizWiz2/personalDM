# MVP Specification
## Первый проверяемый вертикальный срез

**Статус:** канонический для MVP  
**Версия:** 0.1  
**Дата:** 15 июля 2026

## Цель

Проверить, что Campaign Truth Engine делает длинную AI-кампанию устойчивее обычного чата.

MVP доказывает три вещи:

1. кампания переживает restart;
2. пользователь управляет тезисами и каноном;
3. NPC не получает недоступные ему сведения.

## Основной сценарий

Пользователь создаёт кампанию, подключает LLM, создаёт сцену и персонажей, добавляет тезисы, играет, просматривает proposed changes, принимает или исправляет их, перезапускает приложение, продолжает игру, проверяет источник знания NPC, регенерирует плохой ответ, вручную рисует сцену и запускает локальный музыкальный фон.

## Scope

### Campaign и provider

Campaign CRUD, OpenAI-compatible base URL, model name, optional API key, connection test, streaming, manual context length fallback.

### Chat

User turn, DM response, streaming, stop, regenerate, undo last pair, raw history.

### Scene и thesis

Title, location text, participants, mood, tension, active theses. Thesis supports create/edit/lock/resolve, visibility и source turn.

### Character, Fact, Belief, Goal, Relationship Assertion

Manual creation plus provenance. Belief is character-scoped. Relationship has narrative type/description/reason and optional intensity.

### Memory Inspector

List facts and beliefs, show source turn, edit/supersede, show active scene theses.

### Assisted Canon

After each DM response produce at most five proposals: fact, event, relationship assertion, scene thesis or movement. Every proposal supports accept, reject, edit. No auto-approve.

### Image slice

ComfyUI endpoint, one approved reference per character, manual «Generate scene», save prompt/workflow/seed/output path. No automatic generation.

### Music slice

Local folder, mood tags, current scene selection, recent-track cooldown, manual next/stop.

## Turn pipeline

```text
persist user input
→ compile common scene context
→ build actor-scoped packet
→ stream narrative response
→ persist response
→ extract proposed changes
→ deterministic validation
→ user review
→ commit accepted changes
→ queue summary/index work
```

Actor packet contains only public scene state, visible theses, the actor profile, goals, beliefs, relationships to present entities and memories known by the actor.

## Streaming semantics

Narrative text is visible before semantic continuity analysis. Late semantic checks produce warnings. State changes remain proposed until validated. Typed tools may reject impossible actions before commit.

## Minimal data model

campaigns, provider_configs, turns, scenes, scene_participants, scene_theses, entities, characters, character_goals, facts, beliefs, relationship_assertions, events, proposed_changes, media_assets, tracks, playback_history.

A turn has `parent_turn_id` and status `active | alternative | undone`. This enables regenerate/undo without a full branch system.

## Deterministic validation

MVP validates explicit data only: entity existence, active status, scene membership for state-changing actions, exclusive item placement, correct belief owner, valid source turn and thesis visibility.

It does not claim SQL can validate arbitrary prose.

## Context order

1. system and campaign instructions;
2. current scene;
3. active scene theses;
4. actor packet;
5. recent turns;
6. accepted facts/events;
7. retrieved older memory;
8. reserved output budget.

Compiler metadata records included/excluded blocks, token estimate, source IDs and actor.

## Storage

Only SQLite is implemented. Use WAL, Alembic, backup before migration, relative media paths. FTS5 comes after scene summaries; sqlite-vec is optional. Raw archive in SQLite is canonical; JSONL/Markdown are exports.

## Acceptance tests

- 50 turns survive backend restart.
- Given Safira knows the king is alive and Liara believes he is dead, Liara's actor packet does not include the true fact.
- A belief links to its source turn.
- Superseded facts stop entering future packets.
- Regenerate preserves old response as alternative.
- Only accepted proposals enter future context.
- Scene image stores workflow metadata.
- Music avoids the previous three tracks when alternatives exist.

## Non-goals

Full event-sourced replay, named branch trees, branch merge, PostgreSQL, server mode, multiplayer, D&D automation, automatic image generation, perfect identity consistency and external streaming music integration.

## Definition of done

The MVP is pleasant enough to run one real existing campaign for several sessions and demonstrates at least one prevented knowledge leak and one repaired memory error.
