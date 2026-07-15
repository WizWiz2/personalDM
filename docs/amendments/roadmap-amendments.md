# Roadmap
## Canonical correction to the exploratory roadmap

**Status:** canonical roadmap amendment  
**Version:** 0.2  
**Date:** 15 July 2026

Calendar estimates are removed because they were generated without implementation evidence.

## Stage 0: Playable local chat

Campaign CRUD, provider config, streaming, stop, raw persistence, restart and continue, undo/regenerate, export and error handling.

Exit: one campaign runs for 50 turns and survives restart.

## Stage 1: Manual canon and scene theses

Scenes, participants, Scene Thesis UI, characters, manual facts/beliefs/goals/relationships, Memory Inspector and actor-scoped context.

Exit: a controlled scenario proves an NPC does not receive a fact they do not know.

## Stage 1.5: Assisted Canon

At most five proposed changes after a response; accept/reject/edit; proposals persist across restart; accepted records enter future context. No auto-approve.

## Stage 2: Thin atmosphere slice

Manual ComfyUI scene generation with one approved reference per character, saved workflow metadata, local music library, scene mood signature, weighted selection and cooldown.

## Stage 3: Long-campaign retrieval

Scene summaries, FTS5, entity-aware filtering, optional sqlite-vec, context provenance and rebuild.

## Stage 4: Continuity assistance

Start with invalid entity/status references, item placement conflicts, actor knowledge boundary checks, locked-thesis conflicts and soft warnings. Do not begin with several semantic LLM judges on every turn.

## Stage 5: Advanced atmosphere

Multiple references, IP-Adapter/InstantID, pose/composition controls, multi-character scenes, automatic visual triggers and adaptive music.

## Stage 6: Optional mechanics

Dice tools and one concrete ruleset or systemless mechanics. No generic plugin framework before a second ruleset exists.

## Phase 2: Separate decision

Multiplayer, server mode, PostgreSQL, auth/roles, private player knowledge and concurrent state need their own product foundation.

## Removed from early scope

Full branch tree, branch merge, relationship graph, PostgreSQL adapter, semantic validation of every response and unsupported calendar estimates.
