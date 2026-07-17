import json
from dataclasses import dataclass
from uuid import UUID

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.scene_thesis import SceneThesisCreate, SceneThesisUpdate, ThesisType
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider


class DesiredThesis(BaseModel):
    thesis_type: ThesisType
    text: str = Field(min_length=3, max_length=800)
    priority: int = Field(default=0, ge=-10, le=10)
    visibility: str = "dm"
    related_entity_ids: list[UUID] = Field(default_factory=list)


class CuratorResponse(BaseModel):
    desired_active: list[DesiredThesis] = Field(default_factory=list, max_length=12)


@dataclass
class ThesisReconcileResult:
    created: int = 0
    superseded: int = 0
    resolved: int = 0
    kept: int = 0
    pinned_conflicts: int = 0
    duplicate_scopes: int = 0


class ThesisCurator:
    """Maintain one coherent, current working-memory snapshot for a scene.

    Scene theses are operational memory, not immutable canon. After every usable
    DM turn the curator proposes the complete desired set of *unpinned* active
    theses. The reconciler keeps unchanged theses, supersedes changed theses and
    resolves obsolete ones. User-pinned theses are authoritative and immutable.

    A scope is `(thesis_type, sorted related_entity_ids)`. Only one active thesis
    may occupy a scope. This gives each scene one global tension, one global
    visual state, one current intention per character, and so on.
    """

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)
        self._entity_repo = EntityRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    @staticmethod
    def scope_key(thesis_type: str, related_entity_ids: list[UUID]) -> str:
        entity_scope = ",".join(sorted(str(value) for value in related_entity_ids))
        return f"{thesis_type}:{entity_scope or 'scene'}"

    async def curate_after_turn(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        source_turn_id: UUID,
        user_content: str,
        assistant_content: str,
    ) -> ThesisReconcileResult | None:
        if not scene_id or not assistant_content.strip():
            return None

        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return None

        scene = await self._scene_repo.get_by_id(scene_id)
        if not scene:
            return None

        active = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
        entity_names: dict[str, str] = {}
        for entity_id in scene.participants:
            entity = await self._entity_repo.get_by_id(entity_id)
            if entity:
                entity_names[str(entity.id)] = entity.canonical_name

        current_lines = []
        for thesis in active:
            names = [entity_names.get(str(value), str(value)) for value in thesis.related_entity_ids]
            current_lines.append(
                json.dumps(
                    {
                        "id": str(thesis.id),
                        "scope": self.scope_key(thesis.thesis_type, thesis.related_entity_ids),
                        "type": thesis.thesis_type,
                        "text": thesis.text,
                        "priority": thesis.priority,
                        "visibility": thesis.visibility,
                        "related_entities": names,
                        "related_entity_ids": [str(value) for value in thesis.related_entity_ids],
                        "pinned": thesis.pinned,
                    },
                    ensure_ascii=False,
                )
            )

        entity_lines = [f"- {name}: {entity_id}" for entity_id, name in entity_names.items()]
        prompt = f"""You are the Scene Thesis Curator for a long-running RPG.
Return the COMPLETE desired set of current, unpinned scene theses after this turn.

A thesis is short-lived working memory that the DM must actively remember now.
It is not biography, inventory, permanent canon or a summary of old history.

RULES:
- Return 5-12 theses when the scene is active enough to justify them; fewer is fine for a quiet scene.
- The returned set must be internally consistent. Never keep mutually exclusive states.
- Use exactly one thesis per scope: thesis_type + the same related_entity_ids.
- Replace an old thesis by returning a new text in the same scope.
- Omit obsolete or completed theses; the engine will resolve them.
- Do not repeat pinned theses. They are authoritative and remain active automatically.
- Do not contradict a pinned thesis. When a pinned thesis conflicts with the turn, keep the pinned truth and omit the conflicting candidate.
- Prefer active intentions, tension, secrets affecting current choices, unresolved beats,
  relationship dynamics, visible state and music mood.
- Do not turn a player's attempted result into truth unless the DM confirmed it.
- Use only listed entity UUIDs.

Allowed thesis_type values:
canon, intention, relationship_dynamic, secret, tension, unresolved_beat,
visual_state, music_mood

Scene:
{scene.title}
{scene.location_description or ''}
Mood: {scene.mood or ''}; tension: {scene.tension or ''}

Known participants:
{chr(10).join(entity_lines) or '- none'}

Current active theses:
{chr(10).join(current_lines) or '- none'}

Completed turn:
PLAYER ATTEMPT: {user_content}
DM RESULT: {assistant_content}

Return JSON only:
{{"desired_active":[{{"thesis_type":"tension","text":"...","priority":5,"visibility":"dm","related_entity_ids":[]}}]}}
"""

        api_key = await self._config_repo.get_decrypted_key(campaign_id)
        response_text = ""
        async for token in self._llm_provider.generate_stream(
            [ChatMessage(role="system", content=prompt)],
            config,
            api_key,
        ):
            response_text += token

        desired = self._parse_response(response_text, set(entity_names))
        if desired is None:
            return None
        return await self.reconcile(scene_id, source_turn_id, desired)

    @staticmethod
    def _parse_response(text: str, allowed_entity_ids: set[str]) -> list[DesiredThesis] | None:
        clean = text.strip()
        if clean.startswith("```"):
            lines = clean.splitlines()
            clean = "\n".join(lines[1:-1]).strip()
        try:
            parsed = CuratorResponse.model_validate(json.loads(clean))
        except (json.JSONDecodeError, ValidationError, TypeError):
            return None

        result = []
        for thesis in parsed.desired_active:
            if thesis.visibility not in {"dm", "public", "character_only"}:
                continue
            if any(str(entity_id) not in allowed_entity_ids for entity_id in thesis.related_entity_ids):
                continue
            result.append(thesis)
        return result

    async def reconcile(
        self,
        scene_id: UUID,
        source_turn_id: UUID,
        desired: list[DesiredThesis],
    ) -> ThesisReconcileResult:
        result = ThesisReconcileResult()
        active = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
        pinned_by_scope = {
            self.scope_key(item.thesis_type, item.related_entity_ids): item
            for item in active
            if item.pinned
        }
        mutable_by_scope = {
            self.scope_key(item.thesis_type, item.related_entity_ids): item
            for item in active
            if not item.pinned
        }

        desired_by_scope: dict[str, DesiredThesis] = {}
        for item in sorted(desired, key=lambda value: value.priority, reverse=True):
            scope = self.scope_key(item.thesis_type.value, item.related_entity_ids)
            if scope in desired_by_scope:
                result.duplicate_scopes += 1
                continue
            if scope in pinned_by_scope:
                pinned = pinned_by_scope[scope]
                if pinned.text.strip() != item.text.strip():
                    result.pinned_conflicts += 1
                else:
                    result.kept += 1
                continue
            desired_by_scope[scope] = item

        for scope, old in mutable_by_scope.items():
            new = desired_by_scope.get(scope)
            if new is None:
                await self._scene_repo.update_thesis(
                    old.id,
                    SceneThesisUpdate(status="resolved"),
                )
                result.resolved += 1
                continue

            unchanged = (
                old.text.strip() == new.text.strip()
                and old.priority == new.priority
                and old.visibility == new.visibility
            )
            if unchanged:
                result.kept += 1
                desired_by_scope.pop(scope)
                continue

            await self._scene_repo.update_thesis(
                old.id,
                SceneThesisUpdate(status="superseded"),
            )
            await self._scene_repo.create_thesis(
                scene_id,
                SceneThesisCreate(
                    thesis_type=new.thesis_type,
                    text=new.text,
                    priority=new.priority,
                    visibility=new.visibility,
                    related_entity_ids=new.related_entity_ids,
                ),
                source_turn_id=source_turn_id,
            )
            desired_by_scope.pop(scope)
            result.superseded += 1

        for new in desired_by_scope.values():
            await self._scene_repo.create_thesis(
                scene_id,
                SceneThesisCreate(
                    thesis_type=new.thesis_type,
                    text=new.text,
                    priority=new.priority,
                    visibility=new.visibility,
                    related_entity_ids=new.related_entity_ids,
                ),
                source_turn_id=source_turn_id,
            )
            result.created += 1

        await self._session.flush()
        return result
