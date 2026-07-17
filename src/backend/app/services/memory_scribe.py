import json
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider


class MemoryScribe:
    """Extract durable canon candidates from a completed narrative turn."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._entity_repo = EntityRepository(session)
        self._config_repo = ProviderConfigRepository(session)
        self._llm_provider = LLMProvider()

    async def extract_proposals(
        self,
        campaign_id: UUID,
        scene_id: UUID | None,
        user_content: str,
        assistant_content: str,
    ) -> list[ProposedChangeCreate]:
        if not assistant_content or not assistant_content.strip():
            return []
        if assistant_content.lstrip().startswith("[Generation failed"):
            return []

        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return []
        api_key = await self._config_repo.get_decrypted_key(campaign_id)

        known_entities: dict[str, str] = {}
        for entity in await self._entity_repo.list_by_campaign(campaign_id):
            known_entities[entity.canonical_name.lower()] = str(entity.id)
            for alias in entity.aliases:
                known_entities[alias.lower()] = str(entity.id)

        mapping_lines = ""
        if known_entities:
            mapping_lines = "\nKnown entity UUID mappings:\n" + "\n".join(
                f"- {name}: {entity_id}"
                for name, entity_id in sorted(known_entities.items())
            )

        system_prompt = f"""You are the Memory Scribe for a tabletop RPG truth engine.
Analyze one completed turn and propose at most five durable structured changes.

AUTHORITY RULES:
- The USER message is an intention, claim, question, perception or hypothesis.
- The ASSISTANT/DM message is the authoritative outcome of the turn.
- Never turn a user-only claim into objective canon.
- A fact, event, movement, knowledge transfer or item transfer is allowed only
  when the DM explicitly confirms it happened.
- Dialogue, plans, guesses, attempted actions and atmospheric repetition are not facts.
- Use knowledge when a specific character learned, heard, saw or was told something.
- Use item_transfer when possession or the physical location of an existing item changed.
- Scene theses are maintained by a separate Thesis Curator. Never propose scene_thesis.
- Prefer no proposal over a weak, duplicate or inferred proposal.

Available change types and payloads:
1. fact: {{"subject": "UUID or stable name", "predicate": "short relation", "object_value": "UUID or text"}}
2. event: {{"event_type": "short type", "description": "confirmed event", "location_id": "UUID or null", "participant_ids": ["UUID"]}}
3. movement: {{"character_id": "UUID", "location_id": "UUID", "description": "optional"}}
4. relationship: {{"subject_id": "UUID", "object_id": "UUID", "relation_type": "type", "description": "confirmed change", "reason": "cause"}}
5. knowledge: {{"recipient_id": "UUID", "fact_id": "UUID or null", "proposition": "what was learned", "source_character_id": "UUID or null", "confidence": 0.0}}
6. item_transfer: {{"item_id": "UUID", "owner_id": "UUID or null", "location_id": "UUID or null", "description": "confirmed transfer"}}

Return exactly one JSON object with a "proposals" array. Each item must have
"change_type" and "payload". Return {{"proposals": []}} when nothing durable
was confirmed. Do not use markdown fences.{mapping_lines}
"""

        response_text = ""
        async for token in self._llm_provider.generate_stream(
            [
                ChatMessage(role="system", content=system_prompt),
                ChatMessage(
                    role="user",
                    content=(
                        "USER ATTEMPT:\n"
                        f"{user_content}\n\n"
                        "AUTHORITATIVE DM RESULT:\n"
                        f"{assistant_content}"
                    ),
                ),
            ],
            config,
            api_key,
        ):
            response_text += token

        return self._parse_response(response_text, known_entities)

    def _parse_response(
        self,
        text: str,
        known_entities: dict[str, str],
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
            results = []
            for proposal in data.get("proposals", [])[:5]:
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
                results.append(
                    ProposedChangeCreate(
                        change_type=change_type,
                        payload=self._resolve_payload_names(
                            payload,
                            known_entities,
                        ),
                    )
                )
            return results
        except Exception:
            return self._regex_fallback_parse(clean_text, known_entities)

    def _resolve_payload_names(
        self,
        payload: dict,
        known_entities: dict[str, str],
    ) -> dict:
        resolved = payload.copy()
        scalar_keys = [
            "subject",
            "object_value",
            "character_id",
            "location_id",
            "subject_id",
            "object_id",
            "recipient_id",
            "source_character_id",
            "item_id",
            "owner_id",
        ]
        for key in scalar_keys:
            value = resolved.get(key)
            if value and isinstance(value, str) and not self._is_uuid(value):
                entity_id = known_entities.get(value.lower())
                if entity_id:
                    resolved[key] = entity_id

        for key in ("participant_ids",):
            converted = []
            for value in resolved.get(key, []):
                if isinstance(value, str) and not self._is_uuid(value):
                    converted.append(known_entities.get(value.lower(), value))
                else:
                    converted.append(value)
            if key in resolved:
                resolved[key] = converted
        return resolved

    def _regex_fallback_parse(
        self,
        text: str,
        known_entities: dict[str, str],
    ) -> list[ProposedChangeCreate]:
        results = []
        matches = re.findall(
            r'\{\s*"change_type"\s*:\s*"([^"]+)"\s*,\s*"payload"\s*:\s*(\{.*?\})\s*\}',
            text,
            re.DOTALL,
        )
        for change_type_value, payload_text in matches[:5]:
            try:
                change_type = ChangeType(change_type_value)
                if change_type == ChangeType.SCENE_THESIS:
                    continue
                payload = self._resolve_payload_names(
                    json.loads(payload_text),
                    known_entities,
                )
                results.append(
                    ProposedChangeCreate(
                        change_type=change_type,
                        payload=payload,
                    )
                )
            except Exception:
                continue
        return results

    @staticmethod
    def _is_uuid(value: str) -> bool:
        try:
            UUID(str(value))
            return True
        except (ValueError, TypeError, AttributeError):
            return False
