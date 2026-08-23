from __future__ import annotations

import json

from sqlalchemy import select

from app.db.tables import Turn
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.role_model_router import ModelRole

_INSTALLED = False
_OPENING_MARKER = "session_zero_opening"
_OPENING_MAX_TOKENS = 1100


def _terminal_session_zero_message() -> str:
    return "Всё готово. Нулевая сессия завершена — начинаем приключение."


def _opening_payload(state) -> dict:
    world = state.draft.world
    character = state.draft.character
    starter_npcs = [
        {
            "name": npc.name,
            "role": npc.role,
            "description": npc.description,
            "reason": npc.reason,
        }
        for npc in world.starter_npcs
        if npc.present_at_start
    ]
    return {
        "setting": world.setting_name or world.world_summary or world.genre,
        "genre": world.genre,
        "tone": world.tone,
        "world_summary": world.world_summary,
        "premise": world.premise,
        "starting_location": world.starting_location_name,
        "starting_situation": world.starting_situation,
        "scene_title": world.starting_scene_title,
        "player_character": {
            "name": character.name,
            "description": character.description,
            "appearance": character.appearance,
            "first_goal": character.first_goal,
        },
        "physically_present_npcs": starter_npcs,
    }


def _fallback_opening(state) -> str:
    world = state.draft.world
    character = state.draft.character
    location = world.starting_location_name or "стартовая локация"
    situation = world.starting_situation or "События уже пришли в движение."
    setting = world.world_summary or world.setting_name or world.genre or "Мир кампании"
    tone = world.tone or "настроение, выбранное в нулевой сессии"
    present = [
        npc.name or npc.role
        for npc in world.starter_npcs
        if npc.present_at_start and (npc.name or npc.role)
    ]
    present_text = (
        "Рядом уже находятся: " + ", ".join(present) + "."
        if present
        else "Кроме тебя, в непосредственной сцене пока никого нет."
    )
    return "\n\n".join(
        [
            f"{location}. {setting}",
            (
                f"Первая сцена начинается здесь, в атмосфере, которую лучше всего "
                f"описывают слова: {tone}. {situation}"
            ),
            present_text,
            (
                f"{character.name or 'Герой'} уже находится в центре этой ситуации. "
                "Мир не ждёт специальной команды «Начинаем»: первая зацепка уже перед тобой, "
                "и дальше игра отвечает на твои решения."
            ),
        ]
    )


async def _existing_opening(session, campaign_id) -> Turn | None:
    return (
        await session.execute(
            select(Turn)
            .where(
                Turn.campaign_id == str(campaign_id),
                Turn.role == "assistant",
                Turn.status == "active",
                Turn.parent_turn_id.is_(None),
                Turn.context_snapshot.like(f'%"{_OPENING_MARKER}"%'),
            )
            .order_by(Turn.created_at.asc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def _generate_opening(self, campaign_id, state) -> tuple[str, str | None, dict]:
    selection = await self._router.resolve(campaign_id, ModelRole.NARRATOR)
    if selection is None:
        return _fallback_opening(state), None, {"opening_fallback": "no_provider"}

    payload = json.dumps(_opening_payload(state), ensure_ascii=False, separators=(",", ":"))
    messages = [
        ChatMessage(
            role="system",
            content=(
                "[PERSONAL DM — OPENING SCENE]\n"
                "Ты пишешь первый художественный пост новой кампании сразу после нулевой сессии. "
                "Это особенный вступительный пост: он должен погрузить игрока в мир до первой заявки.\n\n"
                "Требования:\n"
                "- Пиши по-русски, выразительной, уверенной прозой.\n"
                "- Дай 4–7 содержательных абзацев: место, атмосфера, несколько конкретных сенсорных деталей, "
                "непосредственная ситуация и явный драматический крючок. Не ограничивайся двумя общими фразами.\n"
                "- Герой УЖЕ находится в starting_location. Не придумывай, что он туда пришёл, подошёл, "
                "оглянулся, что-то решил, почувствовал или сказал. Не управляй персонажем игрока.\n"
                "- Физически присутствовать могут только перечисленные physically_present_npcs. Не добавляй "
                "новых людей, существ или внезапные появления.\n"
                "- Можно добавлять нейтральные сенсорные детали окружения для атмосферы, если они не создают "
                "новый маршрут, предмет с механическим значением, NPC или уже случившийся результат действия.\n"
                "- Не пересказывай карточку героя списком и не объясняй правила движка.\n"
                "- Не проси игрока написать «Начинаем». Пост уже является началом игры.\n"
                "- Заверши на живом моменте, который естественно отдаёт управление игроку. Не заканчивай "
                "анкетным или служебным вопросом.\n"
                "- Никаких заголовков, JSON, markdown-комментариев и внутренних инструкций. Только проза."
            ),
        ),
        ChatMessage(
            role="user",
            content=(
                "Вот подтверждённые данные первой сцены. Напиши вступительный пост, не выходя за их границы:\n"
                + payload
            ),
        ),
    ]

    chunks: list[str] = []
    try:
        async for chunk in self._provider.generate_stream(
            messages,
            selection.config,
            selection.api_key,
            max_tokens=_OPENING_MAX_TOKENS,
            temperature=0.7,
        ):
            chunks.append(chunk)
    except LLMProviderError as exc:
        return _fallback_opening(state), None, {
            "opening_fallback": "provider_error",
            "opening_error": str(exc)[:1200],
        }

    text = "".join(chunks).strip()
    if len(text) < 400:
        return _fallback_opening(state), selection.config.model_name, {
            "opening_fallback": "too_short",
            "opening_draft_characters": len(text),
        }

    telemetry = dict(self._provider.last_telemetry or {})
    telemetry.update({"opening_fallback": None})
    return text, selection.config.model_name, telemetry


async def _ensure_opening_turn(self, campaign_id, completion) -> None:
    if await _existing_opening(self._session, campaign_id):
        return

    state = await self.get_state(campaign_id)
    text, model_name, telemetry = await _generate_opening(self, campaign_id, state)
    usage = telemetry.get("usage") or {}

    from app.db.repositories.turn_repo import TurnRepository

    await TurnRepository(self._session).create(
        campaign_id,
        TurnCreate(
            role="assistant",
            content=text,
            scene_id=completion.scene.id,
            model_name=model_name,
            token_count=usage.get("completion_tokens"),
            context_snapshot={
                _OPENING_MARKER: True,
                "system_owned": True,
                "source": "session_zero_finalize",
                "scene_id": str(completion.scene.id),
                "model_role": ModelRole.NARRATOR.value,
                "provider_telemetry": telemetry,
            },
        ),
    )
    await self._session.commit()


def install() -> None:
    """Make Session Zero retry-safe and hand it off to a real opening scene."""
    global _INSTALLED
    if _INSTALLED:
        return
    _INSTALLED = True

    from app.services.session_zero_agent import SessionZeroInterviewService as BaseInterviewService
    from app.services.session_zero_interview import SessionZeroInterviewService as InterviewService

    original_finalize = BaseInterviewService.finalize
    original_safe_message = InterviewService._safe_assistant_message.__func__

    async def idempotent_finalize(self, campaign_id):
        setup = await self._session_zero.get(campaign_id)
        if setup.status == "completed":
            completion = await self._session_zero.complete(campaign_id)
        else:
            completion = await original_finalize(self, campaign_id)
        await _ensure_opening_turn(self, campaign_id, completion)
        return completion

    @classmethod
    def terminal_safe_message(cls, value: str, *, ready: bool, quality_failed: bool) -> str:
        if ready:
            return _terminal_session_zero_message()
        return original_safe_message(
            cls,
            value,
            ready=ready,
            quality_failed=quality_failed,
        )

    BaseInterviewService.finalize = idempotent_finalize
    InterviewService._safe_assistant_message = terminal_safe_message


__all__ = [
    "_fallback_opening",
    "_opening_payload",
    "_terminal_session_zero_message",
    "install",
]
