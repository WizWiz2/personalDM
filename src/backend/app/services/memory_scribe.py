import json
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.services.canon_semantics import CanonAudit, CanonEnvelope, proposals_from_envelope


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
    """Extract evidence-backed durable canon candidates from one authoritative turn."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entity_repo = EntityRepository(session)
        self._scene_repo = SceneRepository(session)
        self._fact_repo = FactRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()
        self.last_audit: dict = CanonAudit().model_dump()

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
        current_facts = await self._fact_repo.list_active(campaign_id)
        fact_lines = [
            f"- {fact.subject} | {fact.predicate} | {fact.object_value or 'null'} "
            f"[{fact.truth_status}]"
            for fact in current_facts[-40:]
        ]

        system_prompt = f"""Ты Memory Scribe русскоязычной настольной RPG.
Сначала выдели подтверждённые последствия одного завершённого хода, затем предложи структурированные изменения канона.
Верни только один JSON-объект. Все тексты пиши на русском.

КТО ГОВОРИЛ В ОТВЕТЕ: {actor_name}
ПЕРСОНАЖ ИГРОКА: {player_name}
ПРИСУТСТВУЮТ В СЦЕНЕ: {', '.join(participant_names) or 'неизвестно'}

ИЗВЕСТНЫЕ СУЩНОСТИ:
{chr(10).join(entity_lines) or '- нет'}

ТЕКУЩИЕ ОБЪЕКТИВНЫЕ FACTS:
{chr(10).join(fact_lines) or '- нет'}

КРИТИЧЕСКИЕ ПРАВИЛА:
- Сообщение игрока является попыткой, вопросом или гипотезой, но не доказательством результата.
- Авторитетным источником результата является только ответ ДМа.
- Реплика NPC является character_claim: она создаёт knowledge слушателя, но не объективный fact.
- Публично описанное ДМом наблюдение является public_observation.
- Прямо подтверждённое ДМом изменение мира является dm_confirmed.
- Не сохраняй атмосферу, намерения, планы и повтор уже известного.
- Для evidence скопируй короткий точный фрагмент из ответа ДМа.
- Используй точные ИМЕНА сущностей, не UUID и не SELF/USER/all/N/A.
- Scene Thesis обслуживается отдельным Curator и запрещён.

ФОРМАТ:
{{
  "outcomes": [
    {{
      "id": "o1",
      "kind": "world_state|event|knowledge_transfer|relationship_change|movement|item_transfer",
      "description": "что устойчиво изменилось",
      "evidence": "точная цитата из ответа ДМа",
      "authority": "dm_confirmed|public_observation|character_claim|player_intent",
      "durable": true
    }}
  ],
  "proposals": [
    {{
      "outcome_id": "o1",
      "change_type": "fact|event|relationship|movement|knowledge|item_transfer",
      "operation": "assert|revise|retract|contradict",
      "cardinality": "single|multi",
      "payload": {{}}
    }}
  ]
}}

PAYLOAD:
- fact: {{"subject":"устойчивый субъект","predicate":"стабильная связь","object_value":"значение или null","truth_status":"true|false|disputed","visibility":"dm|public"}}
- event: {{"event_type":"тип","description":"что произошло","location_id":"имя локации или null","participant_ids":["имена"]}}
- movement: {{"character_id":"имя","location_id":"имя локации","description":"что переместилось"}}
- relationship: {{"subject_id":"имя","object_id":"имя","relation_type":"стабильный тип","description":"новое состояние","reason":"подтверждённая причина","intensity":0.0}}
- knowledge: {{"recipient_id":"имя слушателя","proposition":"что он узнал или услышал","source_character_id":"имя говорящего или null","confidence":0.8,"status":"known|believed|doubted","previous_proposition":"что исправляется или null"}}
- item_transfer: {{"item_id":"точное имя предмета","owner_id":"имя владельца или null","location_id":"имя локации или null","description":"передача"}}

FACT SEMANTICS:
- assert: нового текущего значения ещё нет;
- revise: прежнее текущее значение уточнено или заменено;
- contradict: ДМ прямо опроверг прежнее текущее значение;
- retract: прежнее значение больше не считается текущим;
- cardinality=single, если одновременно допустимо только одно значение; multi, если значений может быть несколько.

Каждый durable outcome должен иметь хотя бы один proposal. Если устойчивых изменений нет, верни пустые outcomes и proposals.
"""

        try:
            data = await self._llm_provider.generate_json(
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
                max_tokens=1400,
                temperature=0.0,
                response_model=CanonEnvelope,
            )
        except LLMProviderError as exc:
            self.last_audit = CanonAudit(
                envelope_valid=False,
                error=str(exc),
            ).model_dump()
            raise

        return self._parse_data(
            data,
            authoritative_text=assistant_content,
            known_entities=known_entities,
            known_ids=set(display_by_id),
            acting_character_id=acting_character_id,
            player_character_id=player_character_id,
            scene_participant_ids=scene_participant_ids,
        )

    def _parse_response(
        self,
        text: str,
        known_entities: dict[str, str],
        known_ids: set[str],
        acting_character_id: UUID | None,
        player_character_id: UUID | None,
        scene_participant_ids: list[str],
        authoritative_text: str = "",
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
            self.last_audit = CanonAudit(
                envelope_valid=False,
                error="Scribe returned invalid JSON",
            ).model_dump()
            return []
        return self._parse_data(
            data,
            authoritative_text,
            known_entities,
            known_ids,
            acting_character_id,
            player_character_id,
            scene_participant_ids,
        )

    def _parse_data(
        self,
        data: dict,
        authoritative_text: str,
        known_entities: dict[str, str],
        known_ids: set[str],
        acting_character_id: UUID | None,
        player_character_id: UUID | None,
        scene_participant_ids: list[str],
    ) -> list[ProposedChangeCreate]:
        extracted, audit = proposals_from_envelope(data, authoritative_text)
        results: list[ProposedChangeCreate] = []
        for proposal in extracted:
            if proposal.change_type == ChangeType.CANON_GAP:
                results.append(proposal)
                continue
            if proposal.change_type == ChangeType.SCENE_THESIS:
                continue
            normalized = self._normalize_payload(
                proposal.change_type,
                proposal.payload,
                known_entities,
                known_ids,
                acting_character_id,
                player_character_id,
                scene_participant_ids,
            )
            if normalized:
                results.append(
                    ProposedChangeCreate(
                        change_type=proposal.change_type,
                        payload=normalized,
                    )
                )
        audit.proposal_count = len(results)
        self.last_audit = audit.model_dump()
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
            known_entities.items(), key=lambda item: len(item[0]), reverse=True
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
        canon_meta = resolved.get("_canon") if isinstance(resolved.get("_canon"), dict) else {}

        for key in (
            "character_id",
            "location_id",
            "subject_id",
            "object_id",
            "recipient_id",
            "source_character_id",
            "item_id",
            "owner_id",
        ):
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
            "previous_proposition",
        ):
            if key in resolved:
                resolved[key] = self._clean_text(resolved.get(key))

        if change_type == ChangeType.KNOWLEDGE:
            actor_id = str(acting_character_id) if acting_character_id else None
            source_id = resolved.get("source_character_id") or actor_id
            recipient_id = resolved.get("recipient_id")
            proposition = resolved.get("proposition")
            if not recipient_id or recipient_id == source_id or not proposition:
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
                    "status": resolved.get("status") or "known",
                }
            )
            resolved.setdefault("fact_id", None)
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
            if bool(resolved.get("owner_id")) == bool(resolved.get("location_id")):
                return None
        elif change_type == ChangeType.EVENT:
            if not resolved.get("event_type") or not resolved.get("description"):
                return None
            resolved.setdefault("participant_ids", [])
        elif change_type == ChangeType.FACT:
            if not resolved.get("subject") or not resolved.get("predicate"):
                return None
            operation = str(resolved.get("operation") or canon_meta.get("operation") or "assert")
            cardinality = str(resolved.get("cardinality") or canon_meta.get("cardinality") or "single")
            if operation not in {"assert", "revise", "retract", "contradict"}:
                operation = "assert"
            if cardinality not in {"single", "multi"}:
                cardinality = "single"
            resolved["operation"] = operation
            resolved["cardinality"] = cardinality

        if canon_meta:
            resolved["_canon"] = canon_meta
        return {key: value for key, value in resolved.items() if value is not None}
