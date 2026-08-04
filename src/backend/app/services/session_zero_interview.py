from __future__ import annotations

import json
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.goal_repo import GoalRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.character import CharacterCreate
from app.models.goal import GoalCreate
from app.models.location import LocationCreate
from app.models.session_zero import SessionZeroUpdate
from app.models.session_zero_interview import (
    SessionZeroInterviewDecision,
    SessionZeroInterviewDraft,
    SessionZeroInterviewState,
)
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.role_model_router import ModelRole, RoleModelRouter
from app.services.session_zero_service import SessionZeroService


class SessionZeroInterviewIncompleteError(ValueError):
    def __init__(self, missing_fields: list[str]):
        self.missing_fields = missing_fields
        super().__init__(
            "Session-zero interview is incomplete: " + ", ".join(missing_fields)
        )


class SessionZeroInterviewService:
    """LLM-led conversation that feeds the authoritative session-zero service.

    This layer owns only dialogue and a structured draft. It never creates scenes,
    characters, locations, goals, or canon while the interview is in progress.
    Materialization happens once, in one transaction, immediately before the existing
    SessionZeroService.complete() lifecycle is invoked.
    """

    STATE_KEY = "session_zero_interview"
    MAX_HISTORY_MESSAGES = 40
    OPENING_MESSAGE = (
        "Во что тебе хочется сыграть именно сейчас? Можно начать с мира, жанра, "
        "героя или просто с ощущения, которое хочется получить от кампании."
    )
    SYSTEM_PROMPT = """[PERSONAL DM — SESSION ZERO]
You are conducting a natural session-zero conversation for one player.

Your task is not to run a questionnaire. Listen to the player's latest answer, update the full
structured draft, and ask the single most useful next question. Ask two tightly related questions
only when separating them would feel unnatural. Adapt to what the player actually says.

Important rules:
- Preserve every supported detail already present in CURRENT DRAFT unless the player corrects it.
- Never invent preferences, boundaries, fears, values, biography, game system, age rating, or
  character emotions merely to fill a field.
- Distinguish values from fears, personality from biography, voice from speech patterns, and
  capabilities from limitations.
- Do not force 18+ content or a freeform rules system. Record the player's actual choice.
- Explore what the player wants and does not want, expected player/NPC initiative, tone, pacing,
  agency boundaries, desired challenge, and the protagonist only as far as relevant.
- If the player names an established setting or rules system, clarify fidelity only when needed.
- The opening situation must leave the protagonist's first words, decision, and feelings to the
  player.
- Mark boundaries_confirmed true only after the player has explicitly stated boundaries or said
  there are none.
- ready_to_finalize may be true only when every REQUIRED FIELD below is supported by the dialogue.
- Return one complete snapshot, not a patch. Return JSON only.

REQUIRED WORLD FIELDS:
setting_name, genre, premise, tone, world_summary, play_style, starting_location_name,
starting_situation, boundaries_confirmed.

REQUIRED CHARACTER FIELDS:
name, description, appearance, personality, values, fears, desires, voice, speech_patterns,
biography, capabilities, limitations, first_goal.

Return exactly the response schema requested by the caller. assistant_message must sound like a
helpful personal game master speaking directly to the player, not like a form or data validator.
"""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._session_zero = SessionZeroService(session)
        self._entities = EntityRepository(session)
        self._locations = LocationRepository(session)
        self._goals = GoalRepository(session)
        self._provider = LLMProvider()
        self._router = RoleModelRouter(ProviderConfigRepository(session))

    async def get_state(self, campaign_id: UUID) -> SessionZeroInterviewState:
        setup = await self._session_zero.get(campaign_id)
        raw = setup.custom_fields.get(self.STATE_KEY)
        if not isinstance(raw, dict):
            return SessionZeroInterviewState()
        try:
            return SessionZeroInterviewState.model_validate(raw)
        except ValueError:
            return SessionZeroInterviewState()

    async def answer(
        self,
        campaign_id: UUID,
        user_message: str,
    ) -> SessionZeroInterviewDecision:
        clean = " ".join(user_message.split())
        if not clean:
            raise ValueError("Session-zero answer cannot be empty")
        state = await self.get_state(campaign_id)
        if state.pending_user_message:
            raise ValueError(
                "A previous answer is waiting for the model; retry it before adding another"
            )
        state.messages.append({"role": "user", "content": clean})
        state.pending_user_message = clean
        await self._save_state(campaign_id, state, commit=True)
        return await self._continue_pending(campaign_id, state)

    async def retry_pending(
        self,
        campaign_id: UUID,
    ) -> SessionZeroInterviewDecision | None:
        state = await self.get_state(campaign_id)
        if not state.pending_user_message:
            return None
        return await self._continue_pending(campaign_id, state)

    async def _continue_pending(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
    ) -> SessionZeroInterviewDecision:
        selection = await self._router.resolve(campaign_id, ModelRole.SESSION_ZERO)
        if selection is None:
            raise LLMProviderError(
                "No LLM provider is configured for this campaign"
            )
        current = json.dumps(
            state.draft.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
        )
        messages = [
            ChatMessage(
                role="system",
                content=f"{self.SYSTEM_PROMPT}\n\n[CURRENT DRAFT]\n{current}",
            ),
            *[
                ChatMessage(role=item["role"], content=item["content"])
                for item in state.messages[-self.MAX_HISTORY_MESSAGES :]
                if item.get("role") in {"user", "assistant"}
                and item.get("content")
            ],
        ]
        data = await self._router.generate_json(
            self._provider,
            selection,
            messages,
            max_tokens=2800,
            temperature=0.35,
            response_model=SessionZeroInterviewDecision,
        )
        decision = SessionZeroInterviewDecision.model_validate(data)
        missing = self.missing_fields(decision.draft)
        decision.missing_topics = missing
        decision.ready_to_finalize = decision.ready_to_finalize and not missing
        state.draft = decision.draft
        state.messages.append(
            {"role": "assistant", "content": decision.assistant_message}
        )
        state.pending_user_message = None
        state.last_summary = decision.summary or self.summary(decision.draft)
        await self._save_state(campaign_id, state, commit=True)
        return decision

    async def finalize(self, campaign_id: UUID):
        state = await self.get_state(campaign_id)
        if state.pending_user_message:
            raise ValueError("The last answer has not been processed yet")
        missing = self.missing_fields(state.draft)
        if missing:
            raise SessionZeroInterviewIncompleteError(missing)

        world = state.draft.world
        character = state.draft.character
        try:
            location = await self._locations.create(
                campaign_id,
                LocationCreate(
                    canonical_name=world.starting_location_name,
                    description=world.starting_situation,
                    atmosphere=world.tone,
                    custom_fields={"source": "session_zero_interview"},
                ),
            )
            hero = await self._entities.create_character(
                campaign_id,
                CharacterCreate(
                    canonical_name=character.name,
                    description=character.description,
                    appearance=character.appearance,
                    personality=character.personality,
                    values=character.values,
                    fears=character.fears,
                    desires=character.desires,
                    voice=character.voice,
                    speech_patterns=character.speech_patterns,
                    biography=character.biography,
                    current_location_id=location.id,
                    current_intentions=[character.first_goal],
                    custom_fields={
                        "capabilities": character.capabilities,
                        "limitations": character.limitations,
                        "source": "session_zero_interview",
                    },
                ),
            )
            await self._goals.create(
                hero.id,
                GoalCreate(description=character.first_goal, priority=100),
            )
            setup = await self._session_zero.get(campaign_id)
            custom = dict(setup.custom_fields)
            custom[self.STATE_KEY] = state.model_dump(mode="json")
            await self._session_zero.update(
                campaign_id,
                SessionZeroUpdate(
                    setting_name=world.setting_name,
                    genre=world.genre,
                    premise=world.premise,
                    tone=world.tone,
                    themes=world.themes,
                    boundaries=world.boundaries,
                    boundaries_confirmed=world.boundaries_confirmed,
                    rules_system=world.rules_system,
                    world_summary=world.world_summary,
                    starting_situation=world.starting_situation,
                    starting_location_id=location.id,
                    starting_scene_title=(
                        world.starting_scene_title
                        or f"Начало: {world.starting_location_name}"
                    ),
                    play_style=world.play_style,
                    content_rating=world.content_rating,
                    narrative_style=world.narrative_style,
                    player_character_id=hero.id,
                    custom_fields=custom,
                ),
            )
            completed = await self._session_zero.complete(campaign_id)
            await self._session.commit()
            return completed
        except Exception:
            await self._session.rollback()
            raise

    async def _save_state(
        self,
        campaign_id: UUID,
        state: SessionZeroInterviewState,
        *,
        commit: bool,
    ) -> None:
        setup = await self._session_zero.get(campaign_id)
        custom = dict(setup.custom_fields)
        custom[self.STATE_KEY] = state.model_dump(mode="json")
        await self._session_zero.update(
            campaign_id,
            SessionZeroUpdate(custom_fields=custom),
        )
        if commit:
            await self._session.commit()

    @classmethod
    def missing_fields(cls, draft: SessionZeroInterviewDraft) -> list[str]:
        world = draft.world
        character = draft.character
        checks = {
            "world.setting_name": cls._text(world.setting_name),
            "world.genre": cls._text(world.genre),
            "world.premise": cls._text(world.premise),
            "world.tone": cls._text(world.tone),
            "world.world_summary": cls._text(world.world_summary),
            "world.play_style": cls._text(world.play_style),
            "world.starting_location_name": cls._text(
                world.starting_location_name
            ),
            "world.starting_situation": cls._text(world.starting_situation),
            "world.boundaries_confirmed": world.boundaries_confirmed,
            "character.name": cls._text(character.name),
            "character.description": cls._text(character.description),
            "character.appearance": cls._text(character.appearance),
            "character.personality": cls._text(character.personality),
            "character.values": bool(character.values),
            "character.fears": bool(character.fears),
            "character.desires": bool(character.desires),
            "character.voice": cls._text(character.voice),
            "character.speech_patterns": cls._text(character.speech_patterns),
            "character.biography": cls._text(character.biography),
            "character.capabilities": bool(character.capabilities),
            "character.limitations": bool(character.limitations),
            "character.first_goal": cls._text(character.first_goal),
        }
        return [name for name, ready in checks.items() if not ready]

    @classmethod
    def summary(cls, draft: SessionZeroInterviewDraft) -> str:
        world = draft.world
        character = draft.character
        return "\n".join(
            [
                f"Мир: {world.world_summary or world.setting_name or '—'}",
                f"Жанр и тон: {world.genre or '—'}; {world.tone or '—'}",
                f"Игра: {world.premise or '—'}",
                f"Стиль: {world.play_style or '—'}",
                f"Границы: {'; '.join(world.boundaries) or 'дополнительных нет'}",
                f"Герой: {character.name or '—'} — {character.description or '—'}",
                f"Характер: {character.personality or '—'}",
                f"Ценности: {'; '.join(character.values) or '—'}",
                f"Страхи: {'; '.join(character.fears) or '—'}",
                f"Желания: {'; '.join(character.desires) or '—'}",
                f"Сильные стороны: {'; '.join(character.capabilities) or '—'}",
                f"Ограничения: {'; '.join(character.limitations) or '—'}",
                f"Первая цель: {character.first_goal or '—'}",
                f"Старт: {world.starting_location_name or '—'} — "
                f"{world.starting_situation or '—'}",
            ]
        )

    @staticmethod
    def _text(value: object) -> str:
        return " ".join(str(value or "").split())
