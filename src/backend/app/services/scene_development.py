from __future__ import annotations

import json
import asyncio
from time import perf_counter
from uuid import UUID, uuid4

from pydantic import ValidationError
from sqlalchemy import select

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.models.scene_development import SceneDevelopment, coerce_disposition_to_actions, development_wire
from app.models.truth_engine import CanonicalEventCreate, TruthEventEvidenceCreate
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import ModelRole
from app.services.scene_state_service import SceneStateService
from app.services.base_context_compiler import count_tokens
from app.services.truth_engine import CanonicalEventStore
from app.services.turn_planner import TurnPlanningError
from app.services.director_contract import DIRECTOR_EFFECT_CRITERIA, execution_limitations, obligations


DEVELOPMENT_PROMPT = DIRECTOR_EFFECT_CRITERIA + """
[SCENE DEVELOPMENT — WORLD AGENCY]
Player acts are executed. Realize director_requirements in director_policy style.
world_development cites exact non-motive agenda keys (scene/thesis/public conditions, including
DM secrets). Author a grounded event and OPEN usable player_opportunity, not endless hints.
An earlier allow_new_complication=false does not prohibit this authorized grounded beat.
NPC acts require eligible actor_id and owned source_refs. Motives authorize acts, not knowledge;
foreign/private sources cannot become NPC knowledge. Claims are attributed; purpose is private.
If response_actor_id is set, only that actor acts; world_development=null.
No new people/items/routes, transfers, movement, injury or time jumps without an executor.
Never redo completed or execute skipped steps. Hero speech, thoughts, feelings, consent and
next action remain the player's choice. Revelations grant no items/travel.
resolved_progress must be a NEW discovery/objective/obstacle/option with evidence_quote copied
exactly from resolved_turn.observable_consequences; attempts and repeated facts do not qualify.
state_updates are PUBLIC persistent conditions, not inventory/location/health. Quote development
exactly and reuse existing published_world_state state_id; null only for a new subject. Existing
slots may also change from quoted completed_world_outcomes, never claims or unchosen options.
Provide narration_draft: executed results FIRST, then approved acts, without contradiction.
The independently checked draft is NOT evidence. No private rationale, IDs, hero choices or
third-person hero restaging; use second-person results.
Quote approved addressed_response words verbatim, attributed to their speaker, before later acts.
Give an answer or closure, not another hint. Keep reason/progress_reason to one sentence,
development under 400 characters and narration_draft under 800. Use player_input language.
Return only SceneDevelopment JSON.
"""


class SceneDevelopmentService:
    """Post-execution decision context and publication receipts, with no prose-driven mutations.

    Existing goals and active scene theses are the agenda's durable source of truth. We do not
    duplicate them into a second process store. Published acts live in the canonical event journal
    and the turn authority; undo invalidates their source turn through the normal event machinery.
    """

    def __init__(self, session):
        self._session = session

    async def context(self, authority: TurnAuthority) -> dict:
        entities = EntityRepository(self._session)
        scenes = SceneRepository(self._session)
        participants = (
            await entities.get_characters_in_scene(authority.target_scene_id)
            if authority.target_scene_id else []
        )
        eligible = [
            character for character in participants
            if character.id != authority.player_character_id
            and (authority.acting_character_id is None
                 or character.id == authority.acting_character_id)
        ]
        # Compact decision records, independently of the narrator's all-or-nothing card budget.
        actors = []
        sources = {}
        for character in eligible:
            ref = f"actor:{character.id}"
            sources[ref] = {
                "owner_id": str(character.id),
                "kind": "motive",
                "text": {
                    "description": (character.description or "")[:240],
                    "personality": (character.personality or "")[:240],
                    "intentions": [value[:160] for value in character.current_intentions[:3]],
                    "desires": [value[:160] for value in character.desires[:3]],
                },
            }
            goals = await GoalRepository(self._session).get_for_character(
                character.id, active_only=True,
            )
            for goal in sorted(goals, key=lambda item: (-item.priority, str(item.id)))[:3]:
                sources[f"goal:{goal.id}"] = {
                    "owner_id": str(character.id), "kind": "motive",
                    "text": goal.description[:400], "private": goal.is_secret,
                }
            beliefs = await BeliefRepository(self._session).get_for_character(
                character.id, active_only=True,
            )
            actors.append({
                "id": str(character.id), "name": character.canonical_name,
                "knowledge": [belief.proposition[:240] for belief in beliefs[:3]],
            })
        if authority.target_scene_id:
            state = await SceneStateService(self._session).get(
                authority.campaign_id, authority.target_scene_id,
            )
            if authority.acting_character_id is None:
                sources[f"scene:{authority.target_scene_id}"] = {
                    "kind": "situation", "visibility": "dm",
                    "text": {"goal": state.scene_goal, "conflict": state.active_conflict},
                }
            theses = await scenes.list_theses_by_scene(authority.target_scene_id, active_only=True)
            for thesis in sorted(theses, key=lambda item: (-item.priority, str(item.id)))[:6]:
                # Explicit actor turns cannot receive another actor's or DM-only knowledge.
                if authority.acting_character_id and not (
                    thesis.visibility == "public" or
                    thesis.visibility == "character_only"
                    and authority.acting_character_id in thesis.related_entity_ids
                ):
                    continue
                sources[f"thesis:{thesis.id}"] = {
                    "kind": thesis.thesis_type, "text": thesis.text[:400],
                    "visibility": thesis.visibility,
                    "related_entity_ids": [str(value) for value in thesis.related_entity_ids],
                }
        recent = []
        if authority.target_scene_id:
            turns = (await self._session.execute(
                select(Turn).where(
                    Turn.campaign_id == str(authority.campaign_id),
                    Turn.scene_id == str(authority.target_scene_id),
                    Turn.role == "assistant", Turn.status == "active",
                ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(16)
            )).scalars().all()
            for turn in reversed(turns):
                snapshot = json.loads(turn.context_snapshot or "{}")
                development = (snapshot.get("turn_authority") or {}).get("scene_development")
                if development:
                    # Prior actors' private rationales do not belong to the next actor's context.
                    recent.append({"disposition": development["disposition"],
                        "player_input": (snapshot.get("turn_authority") or {}).get("player_input") if authority.acting_character_id is None else None,
                        "resolved_progress": development.get("resolved_progress") if authority.acting_character_id is None else None,
                        "world_development": {
                            key: (development.get("world_development") or {}).get(key)
                            for key in ("development", "player_opportunity")
                        } if authority.acting_character_id is None and development.get("world_development") else None, "actions": [
                        {"actor_id": action["actor_id"], "action": action["action"][:240]}
                        for action in development.get("actions", [])
                        if authority.acting_character_id is None
                        or action["actor_id"] == str(authority.acting_character_id)
                    ]})
        resolved = authority.validator_payload()
        for condition in authority.published_world_state.get("conditions", []):
            sources[f"state:{condition['state_id']}"] = {
                "kind": "public_condition", "visibility": "public",
                "text": {"subject": condition["subject"], "value": condition["value"]},
                "location_id": condition["location_id"],
            }
        for change in authority.published_world_state.get("legacy_changes", []):
            sources[f"event:{change['event_id']}"] = {
                "kind": "published_change", "visibility": "public", "text": change["change"],
                "location_id": change["location_id"],
            }
        # The executed receipt already projects completed/blocked steps into consequences. Avoid
        # spending a second copy of the action sequence on route IDs and execution diagnostics.
        resolved.pop("action_sequence", None)
        resolved.pop("scene_development", None)
        sources["resolved_turn"] = {
            "kind": "executed_outcome", "visibility": "public",
            "text": list(authority.observable_consequences),
        }
        return {
            "player_input": authority.player_input,
            "response_actor_id": str(authority.acting_character_id)
            if authority.acting_character_id else None,
            "resolved_turn": resolved,
            "actors": actors, "agenda": sources, "recent_developments": recent,
            "scene_progress": self.progress_frame(sources, recent, resolved)
                if authority.acting_character_id is None else {},
        }

    @staticmethod
    def progress_frame(agenda: dict, recent: list[dict], resolved: dict) -> dict:
        """Project published receipts, never guesses about player intent, into pacing context."""
        situation = next((value.get("text", {}) for ref, value in agenda.items()
                          if ref.startswith("scene:")), {})
        changes = []
        options = []
        for turn in recent:
            progress = turn.get("resolved_progress") or {}
            world = turn.get("world_development") or {}
            change = world.get("development") or progress.get("evidence_quote")
            if change and change not in changes:
                changes.append(change)
            option = world.get("player_opportunity")
            if option and option not in options:
                options.append(option)
        return {
            "objective": situation.get("goal"),
            "current_obstacle": situation.get("conflict"),
            "open_threads": [
                {"source_ref": ref, "text": value.get("text")}
                for ref, value in agenda.items()
                if value.get("kind") in {"unresolved_beat", "tension"}
            ][:4],
            "published_changes": [change[:240] for change in changes[-16:]],
            "previously_offered_options": [option[:180] for option in options[-16:]],
            "turns_in_window": len(recent),
            "current_result": resolved.get("observable_consequences", []),
            "pending_player_choice": resolved.get("pending_player_choice"),
        }

    @staticmethod
    def _clip_text(value: object, limit: int) -> str:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return text if len(text) <= limit else text[: max(0, limit - 1)].rstrip() + "…"

    @classmethod
    def _compact_resolved_turn(cls, resolved: dict, *, level: int) -> dict:
        """Shrink optional authority blocks while keeping decision-critical fields."""
        if level <= 0 or not isinstance(resolved, dict):
            return resolved
        keep = {
            "player_character", "acting_character", "player_input", "scene_disposition",
            "transition_type", "source_location", "target_location", "present_characters",
            "allowed_speakers", "resolution", "dramatic_mode", "observable_consequences",
            "pending_player_choice", "allow_new_complication", "actor_turn_contract",
            "identity_reveal_requested",
            "published_world_state",
        }
        optional_short = {
            "known_absent_characters", "objects_here", "established_subjects",
            "allowed_new_npcs", "allowed_existing_npc_arrivals", "canon_constraints",
            "established_state", "protected_player_decisions", "complication_source",
        }
        out: dict = {}
        for key, value in resolved.items():
            if key in keep:
                out[key] = value
            elif level == 1 and key in optional_short:
                if isinstance(value, list):
                    clipped = []
                    for item in value[:4]:
                        if isinstance(item, str):
                            clipped.append(cls._clip_text(item, 160))
                        elif isinstance(item, dict):
                            clipped.append({
                                k: cls._clip_text(v, 80) if isinstance(v, str) else v
                                for k, v in list(item.items())[:4]
                            })
                        else:
                            clipped.append(item)
                    if clipped:
                        out[key] = clipped
                elif isinstance(value, str) and value:
                    out[key] = cls._clip_text(value, 160)
                elif value not in (None, "", [], {}):
                    out[key] = value
        if level >= 2:
            consequences = out.get("observable_consequences")
            if isinstance(consequences, list):
                out["observable_consequences"] = [
                    cls._clip_text(item, 120) if isinstance(item, str) else item
                    for item in consequences[:3]
                ]
            for path_key in ("source_location", "target_location"):
                path = out.get(path_key)
                if isinstance(path, list) and len(path) > 1:
                    out[path_key] = path[-1:]
        return out

    @classmethod
    def _compact_agenda_value(cls, ref: str, value: dict, *, level: int) -> dict:
        """Compress motive / goal text; keep ownership and kind for closed-world cites."""
        if level <= 0 or not isinstance(value, dict):
            return value
        compact = {
            key: value[key]
            for key in ("owner_id", "kind", "private", "visibility")
            if key in value
        }
        text = value.get("text")
        if ref.startswith("goal:"):
            compact["text"] = cls._clip_text(text, 160 if level == 1 else 80)
            return compact
        if isinstance(text, dict):
            if compact.get("kind") != "motive":
                compact["text"] = {key: cls._clip_text(value, 100 if level >= 2 else 160)
                                   for key, value in text.items()}
                return compact
            if level == 1:
                compact["text"] = {
                    "description": cls._clip_text(text.get("description", ""), 120),
                    "personality": cls._clip_text(text.get("personality", ""), 80),
                    "intentions": [
                        cls._clip_text(item, 80)
                        for item in list(text.get("intentions") or [])[:1]
                    ],
                    "desires": [
                        cls._clip_text(item, 80)
                        for item in list(text.get("desires") or [])[:1]
                    ],
                }
            else:
                stub = text.get("description") or text.get("personality") or ""
                if not stub:
                    intentions = text.get("intentions") or []
                    stub = intentions[0] if intentions else ""
                compact["text"] = cls._clip_text(stub, 100)
            return compact
        compact["text"] = cls._clip_text(text, 100 if level >= 2 else 160)
        return compact

    @classmethod
    def _compact_actors(cls, actors: list, *, level: int) -> list:
        if level <= 0:
            return actors
        out = []
        for actor in actors:
            if not isinstance(actor, dict):
                out.append(actor)
                continue
            entry = {"id": actor.get("id"), "name": actor.get("name")}
            if level == 1:
                knowledge = actor.get("knowledge") or []
                entry["knowledge"] = [
                    cls._clip_text(item, 80) for item in list(knowledge)[:1]
                ]
            else:
                entry["knowledge"] = []
            out.append(entry)
        return out

    @staticmethod
    def fit_context(context: dict, context_window: int, *, response_model=None) -> tuple[dict, dict]:
        """Reserve decision essentials first; compress before hard capacity failure.

        Prefer fitting by shrinking optional resolved_turn blocks and motive prose,
        then filling remaining in-play agenda before recent history. Closed-world
        authorization still uses the full agenda outside this fitted prompt slice.
        Hard error only if a minimal decision core still cannot fit.
        """
        system_tokens = count_tokens(DEVELOPMENT_PROMPT) + count_tokens(
            json.dumps(LLMProvider._compact_schema((response_model or SceneDevelopment).model_json_schema()),
                       ensure_ascii=False, separators=(",", ":"))
        )
        output_budget = min(1600, max(512, context_window // 5))
        budget = context_window - output_budget - max(128, context_window // 10) - system_tokens
        agenda = context["agenda"]
        required_full = {
            ref: value for ref, value in agenda.items() if ref.startswith(("actor:", "scene:"))
        }
        owners: set[str] = set()
        grounding_reserved = False
        for ref, value in agenda.items():
            if ref.startswith("goal:") and value["owner_id"] not in owners:
                required_full[ref] = value
                owners.add(value["owner_id"])
            if ref.startswith("thesis:") and not grounding_reserved:
                required_full[ref] = value
                grounding_reserved = True

        resolved = context.get("resolved_turn") or {}
        actors = context.get("actors") or []
        compress_resolved = 0
        compress_motives = 0
        compress_actors = 0

        def compact_req(req: dict) -> dict:
            return {
                ref: SceneDevelopmentService._compact_agenda_value(
                    ref, value, level=compress_motives,
                )
                for ref, value in req.items()
            }

        def build(req: dict) -> dict:
            # `req` must already hold compacted agenda values for the active level.
            return {
                **context,
                "resolved_turn": SceneDevelopmentService._compact_resolved_turn(
                    resolved, level=compress_resolved,
                ),
                "actors": SceneDevelopmentService._compact_actors(
                    actors, level=compress_actors,
                ),
                "agenda": dict(req),
                "recent_developments": [],
                # Trajectory is decision-critical, not leftover space after agenda cards.
                "scene_progress": {
                    key: ([SceneDevelopmentService._clip_text(item, 180) for item in value]
                          if key in {"published_changes", "previously_offered_options"} else value)
                    for key, value in context.get("scene_progress", {}).items()
                    if key != "current_result" and value not in (None, [], {}, "")
                },
            }

        def measure(payload: dict) -> int:
            return count_tokens(json.dumps(payload, ensure_ascii=False))

        required = compact_req(required_full)
        result = build(required)
        for step in (
            ("resolved", 1), ("actors", 1), ("motives", 1),
            ("resolved", 2), ("actors", 2), ("motives", 2),
        ):
            if measure(result) <= budget:
                break
            kind, level = step
            if kind == "resolved":
                compress_resolved = max(compress_resolved, level)
            elif kind == "actors":
                compress_actors = max(compress_actors, level)
            else:
                compress_motives = max(compress_motives, level)
            required = compact_req(required_full)
            result = build(required)

        if measure(result) > budget:
            # Last resort: keep prioritized stubs (response actor first). Cast roster
            # stays in `actors` even when some motive refs are omitted from the prompt.
            response_actor = context.get("response_actor_id")
            actor_refs: list = []
            goal_refs: list = []
            compacted_full = compact_req(required_full)
            for ref, value in compacted_full.items():
                owner = value.get("owner_id") or ref.split(":", 1)[-1]
                bucket = actor_refs if ref.startswith("actor:") else goal_refs
                bucket.append((0 if owner == response_actor else 1, ref, value))
            actor_refs.sort(key=lambda item: item[0])
            goal_refs.sort(key=lambda item: item[0])
            kept: dict = {}
            fitted = None
            for _prio, ref, value in actor_refs + goal_refs:
                kept[ref] = value
                candidate = build(kept)
                if measure(candidate) <= budget:
                    fitted = candidate
                    required = dict(kept)
                else:
                    del kept[ref]
            if fitted is None:
                required = {}
                fitted = build(required)
            result = fitted

        if measure(result) > budget:
            raise TurnPlanningError(
                "Scene decision essentials exceed control context window; increase context capacity"
            )

        for ref, value in agenda.items():
            if ref in required:
                continue
            compacted = SceneDevelopmentService._compact_agenda_value(
                ref, value, level=compress_motives,
            )
            required[ref] = compacted
            result["agenda"] = required
            if measure(result) > budget:
                del required[ref]
                result["agenda"] = required
        for development in reversed(context["recent_developments"]):
            result["recent_developments"].insert(0, development)
            if measure(result) > budget:
                result["recent_developments"].pop(0)
                break
        return result, {
            "context_tokens": measure(result), "context_budget": budget,
            "scene_progress_omitted": bool(context.get("scene_progress")) and not bool(result["scene_progress"]),
            "omitted_source_refs": [ref for ref in agenda if ref not in required],
            "omitted_recent_developments": len(context["recent_developments"])
            - len(result["recent_developments"]),
            "compressed_resolved_turn_level": compress_resolved,
            "compressed_motive_level": compress_motives,
            "compressed_actors_level": compress_actors,
        }

    @staticmethod
    def quiet_without_acts(reason: str) -> SceneDevelopment:
        return SceneDevelopment(disposition="quiet", reason=reason, actions=[])

    @classmethod
    def normalize_disposition(cls, decision: SceneDevelopment) -> SceneDevelopment:
        """Re-derive disposition from actions (defense in depth after model_copy / construct)."""
        disposition, _ = coerce_disposition_to_actions(decision.disposition, decision.actions, decision.world_development)
        if decision.disposition == disposition:
            return decision
        return decision.model_copy(update={"disposition": disposition})

    @classmethod
    def sanitize(
        cls, decision: SceneDevelopment, context: dict,
    ) -> tuple[SceneDevelopment, dict]:
        """Drop unauthorized NPC acts instead of aborting the player turn saga.

        Closed-world agenda ownership still applies: unknown refs and foreign private motives
        cannot authorize an act. Ineligible actors likewise cannot own initiative. Those
        failures degrade to filtered acts or quiet rather than TurnPlanningError, so narration
        can still publish the already-resolved player outcome.

        Disposition↔actions is normalized here as well: after filtering, disposition follows
        whether any authorized acts remain (same rule as coerce_disposition_to_actions).
        """
        original_disposition = decision.disposition
        decision = cls.normalize_disposition(decision)
        disposition_coerced = decision.disposition != original_disposition

        world = decision.world_development
        progress = decision.resolved_progress
        previous_quotes = [
            (item.get("resolved_progress") or {}).get("evidence_quote", "")
            for item in context.get("recent_developments", [])
        ]
        if progress and (not any(
            progress.evidence_quote in str(outcome)
            for outcome in (context.get("resolved_turn") or {}).get("observable_consequences", [])
        ) or progress.evidence_quote in previous_quotes):
            decision = decision.model_copy(update={"resolved_progress": None})
        world_dropped = False
        world_invalid_refs = []
        if world:
            world_invalid_refs = [ref for ref in world.source_refs if ref not in context["agenda"]]
        if world and (
            context.get("response_actor_id")
            or any(ref not in context["agenda"] for ref in world.source_refs)
            or any(context["agenda"][ref].get("kind") == "motive" for ref in world.source_refs)
            or any(context["agenda"][ref].get("visibility") == "character_only" for ref in world.source_refs)
        ):
            world_dropped = True
            decision = cls.normalize_disposition(decision.model_copy(update={"world_development": None}))

        if decision.state_updates:
            state = (context.get("resolved_turn") or {}).get("published_world_state") or {}
            known = {item["state_id"]: item for item in state.get("conditions", [])}
            kept_updates = []
            seen = set()
            world_text = decision.world_development.development if decision.world_development else ""
            sequence = (context.get("resolved_turn") or {}).get("action_sequence") or {}
            # Context normally compacts away the sequence, so authorization receives it
            # separately below. Completed mechanical outcomes may update existing slots.
            executed = context.get("completed_world_outcomes") or [
                step.get("observable_outcome", "") for step in sequence.get("steps", [])
                if step.get("status") == "completed" and step.get("action_type") != "observation"
            ]
            for update in decision.state_updates:
                key = str(update.state_id) if update.state_id else None
                in_world = bool(world_text and update.evidence_quote in world_text)
                in_execution = bool(key and any(update.evidence_quote in text for text in executed))
                if (key and key not in known) or not (in_world or in_execution):
                    continue
                if key in seen and key is not None:
                    continue
                seen.add(key)
                # Existing identity also owns its subject; the model cannot rename a slot.
                kept_updates.append(update.model_copy(update={
                    "state_id": update.state_id,
                    "subject": known[key]["subject"] if key else update.subject,
                }))
            decision = decision.model_copy(update={"state_updates": kept_updates})

        actors = {actor["id"] for actor in context["actors"]}
        agenda = context["agenda"]
        kept = []
        dropped = []
        for action in decision.actions:
            if str(action.actor_id) not in actors:
                dropped.append({
                    "reason": "ineligible_actor", "actor_id": str(action.actor_id),
                })
                continue
            invalid = None
            for ref in action.source_refs:
                source = agenda.get(ref)
                if source is None:
                    invalid = {"reason": "unknown_agenda_source", "ref": ref}
                    break
                if source.get("owner_id") not in (None, str(action.actor_id)):
                    invalid = {"reason": "foreign_private_motive", "ref": ref}
                    break
            if invalid is not None:
                dropped.append({**invalid, "actor_id": str(action.actor_id)})
                continue
            kept.append(action)

        def _audit(status: str, result: SceneDevelopment) -> dict:
            payload = {
                "sanitize_status": status,
                "dropped_actions": dropped,
                "dropped_world_development": world_dropped,
                "invalid_world_source_refs": world_invalid_refs,
            }
            if disposition_coerced or result.disposition != original_disposition:
                payload["original_disposition"] = original_disposition
                payload["disposition_coerced"] = True
            return payload

        if not dropped:
            status = "disposition_coerced" if disposition_coerced else "unchanged"
            return decision, _audit(status, decision)
        if not kept and not decision.world_development:
            quiet = cls.quiet_without_acts(
                "NPC initiative omitted: scene development could not authorize cited acts."
            )
            quiet = quiet.model_copy(update={"resolved_progress": decision.resolved_progress})
            return quiet, _audit("degraded_quiet", quiet)
        filtered = decision.model_copy(update={"actions": kept, "disposition": "act"})
        return filtered, _audit("actions_filtered", filtered)

    @classmethod
    def validate(cls, decision: SceneDevelopment, context: dict) -> SceneDevelopment:
        """Sanitize unauthorized acts; never abort the player turn for soft SD contract misses."""
        sanitized, _audit = cls.sanitize(decision, context)
        return sanitized

    async def plan(
        self, authority: TurnAuthority, router, *,
        disposition_bias: str | None = None, director_policy: dict | None = None,
    ) -> tuple[SceneDevelopment, dict]:
        from app.services.interactive_budget import remaining, interactive_budget
        left = remaining()
        if left is None:
            return await self._plan(authority, router, disposition_bias=disposition_bias,
                                    director_policy=director_policy)
        # A shared renderer produces and checks the publication candidate here. Reserving
        # another rendering pass would starve agency and duplicate that same work.
        planner = await router.resolve(authority.campaign_id, ModelRole.PLANNER)
        narrator = await router.resolve(authority.campaign_id, ModelRole.NARRATOR)
        shared_renderer = (planner is not None and narrator is not None
            and planner.config.model_name == narrator.config.model_name
            and getattr(planner.config, 'base_url', None) == getattr(narrator.config, 'base_url', None))
        allowance = max(0.0, left - (0.0 if shared_renderer else 15.0))
        failure = "Insufficient remaining model allowance"
        if allowance > 0:
            try:
                with interactive_budget(allowance):
                    # Each request observes the scoped deadline. Do not cancel this whole
                    # method: it owns the already-sanitized draft and can retain it if an
                    # auxiliary review/repair runs out of time. Database reads stay outside
                    # request cancellation, and unknown review is recorded as unavailable.
                    return await self._plan(authority, router,
                        disposition_bias=disposition_bias, director_policy=director_policy)
            except (TimeoutError, LLMProviderError) as exc:
                failure = f"{type(exc).__name__}: {exc}"
        return self.quiet_without_acts("World agency unavailable within interactive deadline."), {
            "status": "deadline_exhausted", "director_contract_status": "unavailable",
            "model_allowance_seconds": allowance,
            "error": failure,
        }

    async def _plan(
        self,
        authority: TurnAuthority,
        router,
        *,
        disposition_bias: str | None = None,
        director_policy: dict | None = None,
    ) -> tuple[SceneDevelopment, dict]:
        if authority.clarification_required:
            return self.quiet_without_acts("Awaiting player clarification."), {"status": "awaiting_clarification"}
        context = await self.context(authority)
        context["completed_world_outcomes"] = [
            step.get("observable_outcome", "") for step in (authority.action_sequence or {}).get("steps", [])
            if step.get("status") == "completed" and step.get("action_type") != "observation"
        ]
        full_agenda = dict(context["agenda"])
        context["director_policy"] = dict(director_policy or {})
        # Scene trajectory is a universal requirement, independent of the selected style.
        # Compare every continuing scene with its published trajectory, without keywords
        # or setting-specific rules. A first beat has no earlier trajectory to compare.
        from app.services.director_contract import TRAJECTORY_REQUIREMENT
        trajectory_required = bool(context.get("scene_progress", {}).get("turns_in_window", 0))
        if trajectory_required:
            context["director_policy"]["obligations"] = [
                *(context["director_policy"].get("obligations") or []), TRAJECTORY_REQUIREMENT,
            ]
        context["director_requirements"] = obligations(context["director_policy"])
        context["execution_limitations"] = {
            key: value for key, value in execution_limitations(context).items()
            if key.startswith("execution_permissions.") or key == "eligible_actors"
        }
        if not context["actors"] and not context["agenda"]:
            return SceneDevelopment(
                disposition="quiet", reason="No eligible present NPC in the resolved scene.",
                actions=[],
            ), {"status": "no_eligible_actor", "agenda": context["agenda"]}
        selection = await router.resolve(authority.campaign_id, ModelRole.PLANNER)
        if selection is None:
            raise TurnPlanningError("Scene development has no control model")
        wire = development_wire(context)
        context, budget_audit = self.fit_context(context, selection.config.context_window, response_model=wire)
        # The reserved trajectory frame already contains earlier world beats/options.
        # Review still receives the entire trajectory plus NPC initiatives; do not send
        # a second full copy of every world receipt and its private rationale.
        context["recent_developments"] = [
            {key: value for key, value in turn.items() if key != "world_development"}
            for turn in context["recent_developments"]
        ]
        bias_note = ""
        if disposition_bias == "act":
            bias_note = (
                "\n[DIRECTOR BIAS]\nEligible NPCs are present. Prefer disposition=act with a "
                "grounded agenda-backed initiative unless a concrete quiet reason applies."
            )
            context = {**context, "director_disposition_bias": "act"}
        elif disposition_bias == "quiet":
            bias_note = (
                "\n[DIRECTOR BIAS]\nPrefer disposition=quiet unless an already-pressing "
                "situation clearly requires a brief NPC act."
            )
            context = {**context, "director_disposition_bias": "quiet"}
        generation_started = perf_counter()
        data = await router.generate_json(
            LLMProvider(), selection,
            [ChatMessage(role="system", content=DEVELOPMENT_PROMPT + bias_note),
             ChatMessage(role="user", content=json.dumps(context, ensure_ascii=False))],
            max_tokens=min(1600, max(512, selection.config.context_window // 5)),
            temperature=0.2, response_model=SceneDevelopment,
            response_wire=wire,
        )
        generation_seconds = perf_counter() - generation_started
        decision = SceneDevelopment.model_validate(data)
        # Authorize citations against the full in-play agenda even if budget omitted text;
        # unknown or foreign refs degrade to quiet/filtered acts instead of aborting the turn.
        authority_context = {**context, "agenda": full_agenda}
        decision, sanitize_audit = self.sanitize(decision, authority_context)
        from app.services.director_contract import review_contract

        contract_audit = []
        prepared_review = {}
        prepared_review_calls = 0
        # Pure model checks over the same immutable candidate can run together. Dedicated
        # narrator configurations still use their own renderer in the publication phase.
        narrator = await router.resolve(authority.campaign_id, ModelRole.NARRATOR)
        shared_renderer = (narrator is not None
            and narrator.config.model_name == selection.config.model_name
            and getattr(narrator.config, 'base_url', None) == getattr(selection.config, 'base_url', None))
        validation_selection = (await router.resolve(authority.campaign_id, ModelRole.NARRATION_VALIDATOR)
                                if shared_renderer and decision.narration_draft else None)
        if obligations(context.get("director_policy") or {}) or decision.state_updates:
            # Review typed effects before narrator authority is frozen. A narrator repair
            # cannot create a missing world event. Bound this phase to one replanning attempt.
            for attempt in range(2):
                review_started = perf_counter()
                try:
                    if shared_renderer and decision.narration_draft and validation_selection is None:
                        validation_selection = await router.resolve(authority.campaign_id,
                                                                    ModelRole.NARRATION_VALIDATOR)
                    if validation_selection is not None and decision.narration_draft:
                        from app.services.prepared_narration_review import review_prepared
                        checked, reviewed = await asyncio.gather(
                            review_contract(router, selection, context, decision),
                            review_prepared(router, validation_selection,
                                authority.model_copy(update={'scene_development': decision}),
                                decision.narration_draft), return_exceptions=True)
                        if isinstance(reviewed, dict):
                            prepared_review = reviewed
                            prepared_review_calls += reviewed.get('control_calls', 0)
                        if isinstance(checked, BaseException):
                            raise checked
                    else:
                        checked = await review_contract(router, selection, context, decision)
                except (LLMProviderError, ValidationError) as exc:
                    # An auxiliary check must not roll back a valid resolved player turn.
                    # Unknown proof is never reported as fulfilled or used for persistent slots.
                    contract_audit.append({"attempt": attempt, "error_type": type(exc).__name__,
                        "status": "unavailable", "items": []})
                    decision = decision.model_copy(update={"state_updates": []})
                    break
                contract_audit.append({"attempt": attempt, "items": checked})
                missing = [item for item in checked if item["status"] == "missing"]
                if not missing or attempt == 1:
                    break
                from app.services.interactive_budget import remaining
                left = remaining()
                review_seconds = perf_counter() - review_started
                if left is not None and left < generation_seconds + review_seconds:
                    # A replacement must fit generation AND proof. Keep this already
                    # reviewed decision instead of spending the publication allowance
                    # on a repair that cannot finish its checks.
                    contract_audit.append({"attempt": attempt + 1,
                        "status": "repair_deferred_budget", "items": checked,
                        "remaining_seconds": left,
                        "estimated_repair_seconds": generation_seconds + review_seconds})
                    break
                try:
                    data = await router.generate_json(
                        LLMProvider(), selection,
                        [ChatMessage(role="system", content=DEVELOPMENT_PROMPT + bias_note),
                         ChatMessage(role="user", content=json.dumps({
                             **context, "previous_decision": decision.model_dump(mode="json"),
                             "unmet_director_contract": missing,
                             "repair_instruction": "Realize feasible unmet obligations using existing sources. "
                             "Preserve the executed player outcome, agency and current public conditions.",
                         }, ensure_ascii=False))],
                        max_tokens=min(1600, max(512, selection.config.context_window // 5)),
                        temperature=0.2, response_model=SceneDevelopment,
                        response_wire=wire,
                    )
                    decision, sanitize_audit = self.sanitize(SceneDevelopment.model_validate(data), authority_context)
                except (LLMProviderError, ValidationError) as exc:
                    contract_audit.append({"attempt": attempt + 1, "error_type": type(exc).__name__,
                        "status": "unavailable", "items": missing})
                    decision = decision.model_copy(update={"state_updates": []})
                    break
            if any(item["obligation_id"].startswith("continuity:") and item["status"] == "missing"
                   for item in contract_audit[-1]["items"]):
                decision = self.quiet_without_acts("Unresolved continuity error: omit unproven development.").model_copy(
                    update={"resolved_progress": decision.resolved_progress})
        status = "completed"
        if sanitize_audit["sanitize_status"] == "degraded_quiet":
            status = "degraded_quiet"
        elif sanitize_audit["sanitize_status"] == "actions_filtered":
            status = "actions_filtered"
        # Assign new identities after review/repair, so a failed draft's temporary
        # identities cannot be mistaken for already-published conditions on retry.
        decision = decision.model_copy(update={"state_updates": [
            update.model_copy(update={"state_id": update.state_id or uuid4()})
            for update in decision.state_updates
        ]})
        return decision, {
            "status": status, "model_name": selection.config.model_name,
            "model_base_url": getattr(selection.config, "base_url", None),
            "prepared_narration_review": {**prepared_review, 'control_calls': prepared_review_calls},
            "trajectory_required": trajectory_required,
            "trajectory_status": next((item['status'] for item in (
                contract_audit[-1]['items'] if contract_audit else [])
                if item.get('requirement') == TRAJECTORY_REQUIREMENT), 'unavailable'),
            "agenda": context["agenda"], "actor_ids": [a["id"] for a in context["actors"]],
            "director_disposition_bias": disposition_bias,
            "director_contract_review": contract_audit,
            "director_contract_status": (
                "unavailable" if contract_audit and contract_audit[-1].get("status") == "unavailable"
                else "missing" if contract_audit and any(item["status"] == "missing" for item in contract_audit[-1]["items"])
                else "reviewed" if contract_audit else "not_requested"
            ),
            **budget_audit,
            **sanitize_audit,
        }

    async def publish(self, authority: TurnAuthority, assistant_turn_id: UUID) -> None:
        decision = authority.scene_development
        if decision is None:
            return
        if any(update.state_id is None for update in decision.state_updates):
            raise ValueError("World state identities must be assigned before publication")
        location_id = (
            await SceneRepository(self._session).get_location_id(authority.target_scene_id)
            if authority.target_scene_id else None
        )
        store = CanonicalEventStore(self._session)
        if decision.world_development:
            beat = decision.world_development
            await store.append(authority.campaign_id, CanonicalEventCreate(
                event_key=f"scene-development:{assistant_turn_id}:world",
                event_type="world_scene_development", source_kind="scene_development",
                source_turn_id=authority.trigger_turn_id, location_id=location_id,
                participant_ids=[authority.player_character_id] if authority.player_character_id else [],
                description=beat.development,
                payload={**beat.model_dump(mode="json"),
                         "state_updates": [update.model_dump(mode="json") for update in decision.state_updates]},
                evidence=[TruthEventEvidenceCreate(
                    evidence_type="typed_authority", source_turn_id=assistant_turn_id,
                    source_ref="turn_authority.scene_development.world_development",
                )],
            ))
        elif decision.state_updates:
            await store.append(authority.campaign_id, CanonicalEventCreate(
                event_key=f"scene-development:{assistant_turn_id}:conditions",
                event_type="world_condition_update", source_kind="scene_development",
                source_turn_id=authority.trigger_turn_id, location_id=location_id,
                participant_ids=[authority.player_character_id] if authority.player_character_id else [],
                description="\n".join(update.evidence_quote for update in decision.state_updates),
                payload={"state_updates": [update.model_dump(mode="json") for update in decision.state_updates]},
                evidence=[TruthEventEvidenceCreate(evidence_type="typed_authority", source_turn_id=assistant_turn_id,
                    source_ref="turn_authority.scene_development.state_updates")],
            ))
        for index, action in enumerate(decision.actions):
            await store.append(authority.campaign_id, CanonicalEventCreate(
                event_key=f"scene-development:{assistant_turn_id}:{index}",
                event_type="npc_scene_action", source_kind="scene_development",
                source_turn_id=authority.trigger_turn_id, location_id=location_id,
                participant_ids=[action.actor_id], description=action.action,
                payload=action.model_dump(mode="json"),
                evidence=[TruthEventEvidenceCreate(
                    evidence_type="typed_authority", source_turn_id=assistant_turn_id,
                    source_ref=f"turn_authority.scene_development.actions.{index}",
                )],
            ))
