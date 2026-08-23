from __future__ import annotations

import json
from uuid import uuid4

from sqlalchemy import select

from app.config import settings
from app.db.tables import Turn
from app.models.turn import ChatMessage, TurnCreate
from app.models.turn_authority import TurnAuthority
from app.providers.llm_provider import LLMProviderError
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.role_model_router import ModelRole
from app.services.turn_authority_validator import TurnAuthorityValidator

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
    location = world.starting_location_name or "Стартовая локация"
    setting = world.world_summary or world.setting_name or world.genre or "Мир кампании"
    situation = world.starting_situation or "Здесь уже назревает событие, которое требует внимания."
    present = [
        npc.name or npc.role
        for npc in world.starter_npcs
        if npc.present_at_start and (npc.name or npc.role)
    ]
    paragraphs = [
        f"{location}. {setting}",
        situation,
    ]
    if present:
        paragraphs.append("Поблизости находятся: " + ", ".join(present) + ".")
    else:
        paragraphs.append("В непосредственной близости не видно других людей или существ.")
    paragraphs.append(
        "Обстановка уже сложилась; следующий заметный сдвиг зависит от того, что произойдёт здесь."
    )
    return "\n\n".join(paragraphs)


def _opening_authority(campaign_id, state, completion) -> TurnAuthority:
    world = state.draft.world
    character = state.draft.character
    hero_name = completion.setup.player_character_name or character.name
    location = completion.setup.starting_location_name or world.starting_location_name
    present = [hero_name] if hero_name else []
    for npc in world.starter_npcs:
        if not npc.present_at_start:
            continue
        name = npc.name or npc.role
        if name and name not in present:
            present.append(name)
    situation = world.starting_situation
    return TurnAuthority(
        campaign_id=campaign_id,
        # Opening has no user trigger turn. This synthetic UUID is validation-only and is never
        # persisted as a real turn relation.
        trigger_turn_id=uuid4(),
        player_character_id=completion.setup.player_character_id,
        player_character_name=hero_name,
        player_input="",
        source_scene_id=completion.scene.id,
        target_scene_id=completion.scene.id,
        scene_disposition="stay",
        transition_type="none",
        source_location_path=[location] if location else [],
        target_location_path=[location] if location else [],
        present_character_names=present,
        resolution="opening_scene",
        observable_consequences=[situation] if situation else [],
        canon_constraints=[
            "Это opening до первой заявки игрока: герой не совершает новых добровольных действий.",
            "Физически присутствуют только player character и подтверждённые starter NPC.",
            "Нельзя добавлять новую угрозу, фигуру, существо, маршрут или значимый объект как факт.",
        ],
        narration_guidance=[
            "Описывать внешний мир, обстановку и подтверждённую starting situation.",
            "Не приписывать герою мысли, эмоции, решения, телесные реакции или обязанности.",
            "Hook должен вытекать из starting situation/starter NPC, а не из нового существа или события.",
        ],
        allow_new_complication=False,
    )


def _validation_payload(result) -> dict:
    return result.model_dump(mode="json")


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


async def _stream_opening(self, messages, selection, *, temperature: float) -> tuple[str, dict]:
    chunks: list[str] = []
    async for chunk in self._provider.generate_stream(
        messages,
        selection.config,
        selection.api_key,
        max_tokens=_OPENING_MAX_TOKENS,
        temperature=temperature,
    ):
        chunks.append(chunk)
    return "".join(chunks).strip(), dict(self._provider.last_telemetry or {})


async def _generate_opening(self, campaign_id, state, completion) -> tuple[str, str | None, dict]:
    selection = await self._router.resolve(campaign_id, ModelRole.NARRATOR)
    if selection is None:
        return _fallback_opening(state), None, {"opening_fallback": "no_provider"}

    payload = json.dumps(_opening_payload(state), ensure_ascii=False, separators=(",", ":"))
    messages = [
        ChatMessage(
            role="system",
            content=(
                "[PERSONAL DM — OPENING SCENE]\n"
                "Ты пишешь первый большой художественный пост новой кампании сразу после нулевой "
                "сессии. Это вступительная сцена до первой заявки игрока.\n\n"
                "Требования:\n"
                "- Пиши по-русски, уверенной естественной прозой, 4–7 содержательных абзацев.\n"
                "- Каждый абзац должен добавлять конкретную внешнюю деталь места, ситуацию или "
                "драматический смысл; избегай пустого atmospheric filler и нагромождения метафор.\n"
                "- Герой УЖЕ находится в starting_location и пока ничего не делает. Никаких его "
                "мыслей, эмоций, решений, намерений, телесных реакций, инстинктивных жестов или "
                "директив вроде «вы понимаете», «вы чувствуете», «вы должны».\n"
                "- Физически присутствовать могут только перечисленные physically_present_npcs.\n"
                "- Можно добавлять нейтральную сенсорную фактуру окружения (свет, материал, запах, "
                "погоду, обычный фон), но нельзя вводить новую фигуру, существо, угрозу, маршрут, "
                "улику или причинно значимый предмет как состоявшийся факт.\n"
                "- Драматический hook должен вытекать именно из starting_situation, premise или "
                "поведения уже присутствующего starter NPC; не выдумывай неизвестного преследователя "
                "или «что-то большое и тёмное» только ради эффекта.\n"
                "- Не пересказывай карточку героя и не объясняй правила движка.\n"
                "- Не проси написать «Начинаем» и не заканчивай механическим «Что вы делаете дальше?».\n"
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

    try:
        text, narrator_telemetry = await _stream_opening(
            self,
            messages,
            selection,
            temperature=0.65,
        )
    except LLMProviderError as exc:
        return _fallback_opening(state), None, {
            "opening_fallback": "provider_error",
            "opening_error": str(exc)[:1200],
        }

    if len(text) < 400:
        return _fallback_opening(state), selection.config.model_name, {
            **narrator_telemetry,
            "opening_fallback": "too_short",
            "opening_draft_characters": len(text),
            "opening_raw_draft": text,
        }

    authority = _opening_authority(campaign_id, state, completion)
    validation_selection = await self._router.resolve(
        campaign_id,
        ModelRole.NARRATION_VALIDATOR,
        selection.config,
    )
    if validation_selection is None:
        return _fallback_opening(state), selection.config.model_name, {
            **narrator_telemetry,
            "opening_fallback": "validator_unavailable",
            "opening_raw_draft": text,
        }

    validator = TurnAuthorityValidator(self._router)
    attempts: list[dict] = []
    try:
        initial = await validator.validate(validation_selection, authority, text)
        attempts.append(
            {
                "index": 0,
                "strategy": "raw",
                "candidate_text": text,
                "validation": _validation_payload(initial),
                "validator_telemetry": validator.telemetry,
            }
        )
        if initial.verdict == "pass":
            return text, selection.config.model_name, {
                **narrator_telemetry,
                "opening_fallback": None,
                "opening_raw_draft": text,
                "opening_validation": {"status": "passed", "attempts": attempts},
            }

        surgical, surgery = NarrationPublicationGuard.surgical_repair_candidate(text, initial)
        if surgical is not None:
            surgical_result = await validator.validate(
                validation_selection,
                authority,
                surgical,
            )
            attempts.append(
                {
                    "index": 1,
                    "strategy": "deterministic_span_removal",
                    "candidate_text": surgical,
                    "repair": surgery,
                    "validation": _validation_payload(surgical_result),
                    "validator_telemetry": validator.telemetry,
                }
            )
            if surgical_result.verdict == "pass" and len(surgical) >= 400:
                return surgical, selection.config.model_name, {
                    **narrator_telemetry,
                    "opening_fallback": None,
                    "opening_raw_draft": text,
                    "opening_validation": {
                        "status": "repaired",
                        "repair_strategy": "deterministic_span_removal",
                        "attempts": attempts,
                    },
                }

        repair_messages = [
            *messages,
            ChatMessage(
                role="user",
                content=validator.repair_prompt(authority, text, initial),
            ),
        ]
        repaired, repair_telemetry = await _stream_opening(
            self,
            repair_messages,
            selection,
            temperature=settings.NARRATION_REPAIR_TEMPERATURE,
        )
        repaired_result = await validator.validate(
            validation_selection,
            authority,
            repaired,
        )
        attempts.append(
            {
                "index": len(attempts),
                "strategy": "preserve_first_model_edit",
                "candidate_text": repaired,
                "validation": _validation_payload(repaired_result),
                "validator_telemetry": validator.telemetry,
                "narrator_telemetry": repair_telemetry,
            }
        )
        if repaired_result.verdict == "pass" and len(repaired) >= 400:
            return repaired, selection.config.model_name, {
                **narrator_telemetry,
                "opening_fallback": None,
                "opening_raw_draft": text,
                "opening_validation": {
                    "status": "repaired",
                    "repair_strategy": "preserve_first_model_edit",
                    "attempts": attempts,
                },
            }

        return _fallback_opening(state), selection.config.model_name, {
            **narrator_telemetry,
            "opening_fallback": "validation_exhausted",
            "opening_raw_draft": text,
            "opening_validation": {
                "status": "safe_fallback",
                "attempts": attempts,
            },
        }
    except (NarrationValidationError, LLMProviderError) as exc:
        return _fallback_opening(state), selection.config.model_name, {
            **narrator_telemetry,
            "opening_fallback": "validation_error",
            "opening_error": str(exc)[:1200],
            "opening_raw_draft": text,
            "opening_validation": {"status": "safe_fallback", "attempts": attempts},
        }


async def _ensure_opening_turn(self, campaign_id, completion) -> None:
    if await _existing_opening(self._session, campaign_id):
        return

    state = await self.get_state(campaign_id)
    text, model_name, telemetry = await _generate_opening(
        self,
        campaign_id,
        state,
        completion,
    )
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
    """Make Session Zero retry-safe and hand it off to a validated opening scene."""
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
    "_opening_authority",
    "_opening_payload",
    "_terminal_session_zero_message",
    "install",
]
