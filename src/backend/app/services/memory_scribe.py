import json
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError


PLACEHOLDER_SELF = {"self", "speaker", "acting_character", "acting_character_id"}
PLACEHOLDER_PLAYER = {
    "player",
    "user",
    "hero",
    "head_character_uuid",
    "user_character_id",
    "player_character_id",
}
PLACEHOLDER_ALL = {"all", "everyone", "witnesses", "party", "group"}
UUID_PATTERN = re.compile(
    r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-5][0-9a-fA-F]{3}-"
    r"[89abAB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}"
)
CJK_PATTERN = re.compile(r"[\u3400-\u9fff]")
HTML_PATTERN = re.compile(r"<[^>]+>")


class MemoryScribe:
    """Extract durable canon candidates from one completed authoritative turn."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entity_repo = EntityRepository(session)
        self._scene_repo = SceneRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    async def extract_proposals(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        user_content: str,
        assistant_content: str,
        acting_character_id: UUID | None = None,
        player_character_id: UUID | None = None,
    ) -> list[ProposedChangeCreate]:
        if not assistant_content or not assistant_content.strip():
            return []
        if assistant_content.lstrip().startswith("[Generation failed"):
            return []
        if "[generation interrupted]" in assistant_content:
            return []

        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return []
        api_key = await self._config_repo.get_decrypted_key(campaign_id)

        known_entities: dict[str, str] = {}
        display_by_id: dict[str, str] = {}
        type_by_id: dict[str, str] = {}
        for entity in await self._entity_repo.list_by_campaign(campaign_id):
            entity_id = str(entity.id)
            display_by_id[entity_id] = entity.canonical_name
            type_by_id[entity_id] = entity.entity_type
            known_entities[entity.canonical_name.casefold()] = entity_id
            for alias in entity.aliases:
                known_entities[alias.casefold()] = entity_id

        scene_participant_ids: list[str] = []
        if scene_id:
            scene = await self._scene_repo.get_by_id(scene_id)
            if scene:
                scene_participant_ids = [str(value) for value in scene.participants]

        actor_name = display_by_id.get(str(acting_character_id), "narrator")
        player_name = display_by_id.get(str(player_character_id), "player")
        entity_lines = [
            f"- {display_by_id[entity_id]} [{type_by_id[entity_id]}]"
            for entity_id in sorted(display_by_id, key=lambda value: display_by_id[value].casefold())
        ]
        participant_names = [
            display_by_id.get(entity_id, entity_id) for entity_id in scene_participant_ids
        ]

        system_prompt = f"""Ты Memory Scribe русскоязычной настольной RPG.
Проанализируй один завершённый ход и предложи не более восьми устойчивых изменений.
Верни только JSON. Все proposition, description, predicate и object_value пиши на русском.

КТО ГОВОРИЛ В ОТВЕТЕ: {actor_name}
ПЕРСОНАЖ ИГРОКА: {player_name}
ПРИСУТСТВУЮТ В СЦЕНЕ: {', '.join(participant_names) or 'неизвестно'}

КРИТИЧЕСКОЕ ПРАВИЛО ССЫЛОК:
- Используй ТОЧНЫЕ ИМЕНА из списка ниже, а не UUID.
- Никогда не пиши SELF, USER, PLAYER, all, N/A, null строкой или выдуманные ID.
- Backend сам преобразует имена в UUID.

ИЗВЕСТНЫЕ СУЩНОСТИ:
{chr(10).join(entity_lines) or '- нет'}

ПРАВИЛА АВТОРИТЕТА:
- Сообщение игрока — намерение, вопрос, заявление или гипотеза.
- Ответ ДМа — авторитетный результат.
- Не превращай неподтверждённое намерение игрока в факт.
- Диалог персонажа создаёт knowledge у слушателя, а не knowledge у самого говорящего.
- Публичное наблюдение без конкретного рассказчика обычно является fact или event.
- Временная атмосфера и повторение уже известного не являются долговременным каноном.
- Scene Thesis обслуживается Thesis Curator и здесь запрещён.
- Предпочитай пустой список слабой или дублирующей записи.

Типы и payload, ссылки задаются ИМЕНАМИ:
1. fact: {{"subject":"имя или устойчивый объект","predicate":"краткая связь","object_value":"имя или текст"}}
2. event: {{"event_type":"тип","description":"подтверждённое событие","location_id":"имя локации или null","participant_ids":["имена"]}}
3. movement: {{"character_id":"имя","location_id":"имя локации","description":"необязательно"}}
4. relationship: {{"subject_id":"имя","object_id":"имя","relation_type":"тип","description":"изменение","reason":"причина"}}
5. knowledge: {{"recipient_id":"имя слушателя","fact_id":null,"proposition":"что именно стало известно","source_character_id":"имя говорящего или null","confidence":0.8}}
6. item_transfer: {{"item_id":"точное имя предмета","owner_id":"имя владельца или null","location_id":"имя локации или null","description":"передача"}}

Верни {{"proposals":[]}}, если ничего устойчивого не подтверждено.
"""

        response_text = ""
        try:
            async for token in self._llm_provider.generate_stream(
                [
                    ChatMessage(role="system", content=system_prompt),
                    ChatMessage(
                        role="user",
                        content=(
                            "ПОПЫТКА ИГРОКА:\n"
                            f"{user_content}\n\n"
                            "АВТОРИТЕТНЫЙ РЕЗУЛЬТАТ ДМА:\n"
                            f"{assistant_content}"
                        ),
                    ),
                ],
                config,
                api_key,
                max_tokens=900,
                temperature=0.1,
            ):
                response_text += token
        except LLMProviderError:
            return []

        return self._parse_response(
            response_text,
            known_entities,
            set(display_by_id),
            acting_character_id,
            player_character_id,
            scene_participant_ids,
        )

    def _parse_response(
        self,
        text: str,
        known_entities: dict[str, str],
        known_ids: set[str],
        acting_character_id: UUID | None,
        player_character_id: UUID | None,
        scene_participant_ids: list[str],
    ) -> list[ProposedChangeCreate]:
        clean_text = text.strip()
        if clean_text.startswith("```"):
            lines = clean_text.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_text = "\n".join(lines).strip()

        try:
            data = json.loads(clean_text)
        except Exception:
            return []

        results: list[ProposedChangeCreate] = []
        for proposal in data.get("proposals", [])[:8]:
            value = proposal.get("change_type")
            payload = proposal.get("payload", {})
            if not value or not isinstance(payload, dict) or not payload:
                continue
            try:
                change_type = ChangeType(value)
            except ValueError:
                continue
            if change_type == ChangeType.SCENE_THESIS:
                continue

            normalized = self._normalize_payload(
                change_type,
                payload,
                known_entities,
                known_ids,
                acting_character_id,
                player_character_id,
                scene_participant_ids,
            )
            if normalized:
                results.append(
                    ProposedChangeCreate(
                        change_type=change_type,
                        payload=normalized,
                    )
                )
        return results

    @staticmethod
    def _clean_scalar(value):
        if not isinstance(value, str):
            return value
        value = HTML_PATTERN.sub("", value).strip()
        if value.casefold() in {"", "null", "none", "n/a", "unknown"}:
            return None
        return value

    def _resolve_reference(
        self,
        value,
        known_entities: dict[str, str],
        known_ids: set[str],
        acting_character_id: UUID | None,
        player_character_id: UUID | None,
    ) -> str | None:
        value = self._clean_scalar(value)
        if value is None:
            return None
        if isinstance(value, UUID):
            candidate = str(value)
            return candidate if candidate in known_ids else None
        if not isinstance(value, str):
            return None

        folded = value.casefold().strip()
        if folded in PLACEHOLDER_SELF:
            return str(acting_character_id) if acting_character_id else None
        if folded in PLACEHOLDER_PLAYER:
            return str(player_character_id) if player_character_id else None

        direct = known_entities.get(folded)
        if direct:
            return direct

        match = UUID_PATTERN.search(value)
        if match and match.group(0) in known_ids:
            return match.group(0)

        for alias, entity_id in sorted(
            known_entities.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            if alias and alias in folded:
                return entity_id
        return None

    @staticmethod
    def _clean_text(value) -> str | None:
        value = MemoryScribe._clean_scalar(value)
        if not isinstance(value, str):
            return None
        value = " ".join(value.split())
        if CJK_PATTERN.search(value):
            return None
        return value[:2000] or None

    def _resolve_list(
        self,
        values,
        known_entities: dict[str, str],
        known_ids: set[str],
        acting_character_id: UUID | None,
        player_character_id: UUID | None,
        scene_participant_ids: list[str],
    ) -> list[str]:
        if not isinstance(values, list):
            return []
        resolved: list[str] = []
        for value in values:
            if isinstance(value, str) and value.casefold().strip() in PLACEHOLDER_ALL:
                resolved.extend(scene_participant_ids)
                continue
            entity_id = self._resolve_reference(
                value,
                known_entities,
                known_ids,
                acting_character_id,
                player_character_id,
            )
            if entity_id:
                resolved.append(entity_id)
        return list(dict.fromkeys(resolved))

    def _normalize_payload(
        self,
        change_type: ChangeType,
        payload: dict,
        known_entities: dict[str, str],
        known_ids: set[str],
        acting_character_id: UUID | None,
        player_character_id: UUID | None,
        scene_participant_ids: list[str],
    ) -> dict | None:
        resolved = dict(payload)

        reference_keys = (
            "character_id",
            "location_id",
            "subject_id",
            "object_id",
            "recipient_id",
            "source_character_id",
            "item_id",
            "owner_id",
        )
        for key in reference_keys:
            if key in resolved:
                resolved[key] = self._resolve_reference(
                    resolved.get(key),
                    known_entities,
                    known_ids,
                    acting_character_id,
                    player_character_id,
                )

        if "participant_ids" in resolved:
            resolved["participant_ids"] = self._resolve_list(
                resolved.get("participant_ids"),
                known_entities,
                known_ids,
                acting_character_id,
                player_character_id,
                scene_participant_ids,
            )

        for key in (
            "subject",
            "predicate",
            "object_value",
            "event_type",
            "description",
            "relation_type",
            "reason",
            "proposition",
        ):
            if key in resolved:
                resolved[key] = self._clean_text(resolved.get(key))

        if change_type == ChangeType.KNOWLEDGE:
            actor_id = str(acting_character_id) if acting_character_id else None
            player_id = str(player_character_id) if player_character_id else None
            source_id = resolved.get("source_character_id") or actor_id
            recipient_id = resolved.get("recipient_id")
            if not recipient_id and source_id and player_id and source_id != player_id:
                recipient_id = player_id
            if recipient_id == source_id and player_id and source_id != player_id:
                recipient_id = player_id
            proposition = resolved.get("proposition")
            if not recipient_id or not proposition:
                return None
            confidence = resolved.get("confidence", 0.8)
            try:
                confidence = float(confidence)
            except (TypeError, ValueError):
                confidence = 0.8
            resolved.update(
                {
                    "recipient_id": recipient_id,
                    "source_character_id": source_id,
                    "confidence": min(1.0, max(0.2, confidence)),
                }
            )
            if not resolved.get("fact_id"):
                resolved["fact_id"] = None

        elif change_type == ChangeType.MOVEMENT:
            if not resolved.get("character_id") or not resolved.get("location_id"):
                return None
        elif change_type == ChangeType.RELATIONSHIP:
            if not resolved.get("subject_id") or not resolved.get("object_id"):
                return None
            if resolved["subject_id"] == resolved["object_id"]:
                return None
        elif change_type == ChangeType.ITEM_TRANSFER:
            if not resolved.get("item_id"):
                return None
            owner_id = resolved.get("owner_id")
            location_id = resolved.get("location_id")
            if bool(owner_id) == bool(location_id):
                return None
        elif change_type == ChangeType.EVENT:
            if not resolved.get("event_type") or not resolved.get("description"):
                return None
            resolved.setdefault("participant_ids", [])
        elif change_type == ChangeType.FACT:
            if not resolved.get("subject") or not resolved.get("predicate"):
                return None

        return {key: value for key, value in resolved.items() if value is not None}
