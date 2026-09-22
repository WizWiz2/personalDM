from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy import select

from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.tables import Turn
from app.models.scene_development import NpcSceneAction, SceneDevelopment, coerce_disposition_to_actions
from app.models.truth_engine import CanonicalEventCreate, TruthEventEvidenceCreate
from app.models.turn import ChatMessage
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProvider
from app.services.role_model_router import ModelRole
from app.services.scene_state_service import SceneStateService
from app.services.base_context_compiler import count_tokens
from app.services.truth_engine import CanonicalEventStore
from app.services.turn_planner import TurnPlanningError


DEVELOPMENT_PROMPT = """[SCENE DEVELOPMENT — WORLD AGENCY]
The human's actions have already been resolved. Decide what a present NPC now independently does
to pursue a motive or advance an established situation. You own NPC initiative, never the hero.
Return SceneDevelopment. This phase runs after every normal turn, including ordinary travel.

Choose one useful development, at most two connected NPC acts. An NPC can initiate a conversation,
make a concrete offer or request, take a conversational position, reveal a character claim, or perform
local observable behavior. The player need not ask for it first. Prefer an actionable opportunity
or advancement of an existing unresolved situation over another nod, gaze or atmospheric paragraph.
Do not repeat a just-resolved reply or an initiative already offered in recent_developments.

Use disposition=quiet with a concrete reason when the current action already makes enough progress,
the human is resting or awaiting a decision, or no grounded initiative is appropriate. Do not force
an interruption, threat or a question at the end of every turn. A quiet choice is not failure.

Each action needs an eligible actor_id and source_refs from the supplied agenda. purpose is private
planning rationale; action contains only the observable act, never secret motives presented as fact.
Goals/desires justify choosing an act, not knowing secrets. Private knowledge is actor-scoped;
another actor's knowledge and DM-only scene notes cannot become this NPC's knowledge. Claims,
opinions and lies remain attributed speech, not new objective world facts.

Actions take place AFTER the executed outcome in the target scene. Do not undo, obstruct or repeat
completed steps. Never complete skipped steps. Do not add people, items, routes, travel, time jumps,
item transfers, injuries or other state mutations requiring an executor. A proposed trip or bargain
is only an offer; never narrate acceptance or movement by the hero. Never decide the player's speech,
thoughts, feelings, consent or next voluntary act. player_opportunity describes an OPEN possibility.
Respect existing canon, pending choices and the complication policy: initiative is not permission
to invent an unseeded threat. Existing tension can develop without manufacturing a new complication.
If response_actor_id is set, only that actor can own a new act in this response.
Use the language of player_input for all prose fields. Return only the required JSON object.
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
                ).order_by(Turn.created_at.desc(), Turn.id.desc()).limit(4)
            )).scalars().all()
            for turn in reversed(turns):
                snapshot = json.loads(turn.context_snapshot or "{}")
                development = (snapshot.get("turn_authority") or {}).get("scene_development")
                if development:
                    # Prior actors' private rationales do not belong to the next actor's context.
                    recent.append({"disposition": development["disposition"], "actions": [
                        {"actor_id": action["actor_id"], "action": action["action"]}
                        for action in development.get("actions", [])
                        if authority.acting_character_id is None
                        or action["actor_id"] == str(authority.acting_character_id)
                    ]})
        resolved = authority.validator_payload()
        # The executed receipt already projects completed/blocked steps into consequences. Avoid
        # spending a second copy of the action sequence on route IDs and execution diagnostics.
        resolved.pop("action_sequence", None)
        resolved.pop("scene_development", None)
        return {
            "player_input": authority.player_input,
            "response_actor_id": str(authority.acting_character_id)
            if authority.acting_character_id else None,
            "resolved_turn": resolved,
            "actors": actors, "agenda": sources, "recent_developments": recent,
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
    def fit_context(context: dict, context_window: int) -> tuple[dict, dict]:
        """Reserve decision essentials first; compress before hard capacity failure.

        Prefer fitting by shrinking optional resolved_turn blocks and motive prose,
        then filling remaining in-play agenda before recent history. Closed-world
        authorization still uses the full agenda outside this fitted prompt slice.
        Hard error only if a minimal decision core still cannot fit.
        """
        system_tokens = count_tokens(DEVELOPMENT_PROMPT) + count_tokens(
            json.dumps(SceneDevelopment.model_json_schema(), ensure_ascii=False)
        )
        budget = context_window - 1100 - max(128, context_window // 10) - system_tokens
        agenda = context["agenda"]
        required_full = {
            ref: value for ref, value in agenda.items() if ref.startswith("actor:")
        }
        owners: set[str] = set()
        for ref, value in agenda.items():
            if ref.startswith("goal:") and value["owner_id"] not in owners:
                required_full[ref] = value
                owners.add(value["owner_id"])

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
        disposition, _ = coerce_disposition_to_actions(decision.disposition, decision.actions)
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
            }
            if disposition_coerced or result.disposition != original_disposition:
                payload["original_disposition"] = original_disposition
                payload["disposition_coerced"] = True
            return payload

        if not dropped:
            status = "disposition_coerced" if disposition_coerced else "unchanged"
            return decision, _audit(status, decision)
        if not kept:
            quiet = cls.quiet_without_acts(
                "NPC initiative omitted: scene development could not authorize cited acts."
            )
            return quiet, _audit("degraded_quiet", quiet)
        filtered = decision.model_copy(update={"actions": kept, "disposition": "act"})
        return filtered, _audit("actions_filtered", filtered)

    @classmethod
    def validate(cls, decision: SceneDevelopment, context: dict) -> SceneDevelopment:
        """Sanitize unauthorized acts; never abort the player turn for soft SD contract misses."""
        sanitized, _audit = cls.sanitize(decision, context)
        return sanitized

    async def plan(
        self,
        authority: TurnAuthority,
        router,
        *,
        disposition_bias: str | None = None,
    ) -> tuple[SceneDevelopment, dict]:
        context = await self.context(authority)
        full_agenda = dict(context["agenda"])
        if not context["actors"]:
            return SceneDevelopment(
                disposition="quiet", reason="No eligible present NPC in the resolved scene.",
                actions=[],
            ), {"status": "no_eligible_actor", "agenda": context["agenda"]}
        selection = await router.resolve(authority.campaign_id, ModelRole.PLANNER)
        if selection is None:
            raise TurnPlanningError("Scene development has no control model")
        context, budget_audit = self.fit_context(context, selection.config.context_window)
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
        data = await router.generate_json(
            LLMProvider(), selection,
            [ChatMessage(role="system", content=DEVELOPMENT_PROMPT + bias_note),
             ChatMessage(role="user", content=json.dumps(context, ensure_ascii=False))],
            max_tokens=1100, temperature=0.2, response_model=SceneDevelopment,
        )
        decision = SceneDevelopment.model_validate(data)
        # Authorize citations against the full in-play agenda even if budget omitted text;
        # unknown or foreign refs degrade to quiet/filtered acts instead of aborting the turn.
        authority_context = {**context, "agenda": full_agenda}
        decision, sanitize_audit = self.sanitize(decision, authority_context)
        status = "completed"
        if sanitize_audit["sanitize_status"] == "degraded_quiet":
            status = "degraded_quiet"
        elif sanitize_audit["sanitize_status"] == "actions_filtered":
            status = "actions_filtered"
        return decision, {
            "status": status, "model_name": selection.config.model_name,
            "agenda": context["agenda"], "actor_ids": [a["id"] for a in context["actors"]],
            "director_disposition_bias": disposition_bias,
            **budget_audit,
            **sanitize_audit,
        }

    async def publish(self, authority: TurnAuthority, assistant_turn_id: UUID) -> None:
        decision = authority.scene_development
        if decision is None:
            return
        location_id = (
            await SceneRepository(self._session).get_location_id(authority.target_scene_id)
            if authority.target_scene_id else None
        )
        store = CanonicalEventStore(self._session)
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
