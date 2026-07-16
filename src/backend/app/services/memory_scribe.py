import json
import re
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.proposed_change import ChangeType, ProposedChangeCreate
from app.models.turn import ChatMessage
from app.providers.llm_provider import LLMProvider


class MemoryScribe:
    """Extract structured canon candidates from a completed narrative turn."""

    def __init__(self, session: AsyncSession):
        self._session = session
        self._scene_repo = SceneRepository(session)
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
        """Return no more than five canon candidates.

        The user message is an attempted action, statement or hypothesis. Only a
        result explicitly confirmed by the completed DM response may become an
        objective fact, event, movement or scene state.
        """
        if not assistant_content or not assistant_content.strip():
            return []
        if assistant_content.lstrip().startswith("[Generation failed"):
            return []

        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return []
        api_key = await self._config_repo.get_decrypted_key(campaign_id)

        known_entities: dict[str, str] = {}
        all_campaign_entities = await self._entity_repo.list_by_campaign(campaign_id)
        for entity in all_campaign_entities:
            known_entities[entity.canonical_name.lower()] = str(entity.id)
            for alias in entity.aliases:
                known_entities[alias.lower()] = str(entity.id)

        mapping_lines = ""
        if known_entities:
            mapping_lines = "\nKnown entity UUID mappings:\n" + "\n".join(
                f"- {name}: {entity_id}"
                for name, entity_id in sorted(known_entities.items())
            )

        scene_instruction = (
            f"\nThe trusted current scene UUID is {scene_id}. "
            "Use exactly this value for scene_thesis proposals."
            if scene_id
            else "\nThere is no trusted current scene UUID; do not propose scene_thesis."
        )

        system_prompt = f"""You are the Memory Scribe for a tabletop RPG truth engine.
Analyze one completed turn and propose at most five durable structured changes.

AUTHORITY RULES:
- The USER message is an intention, claim, question, perception or hypothesis.
- The ASSISTANT/DM message is the authoritative outcome of the turn.
- Never turn a user-only claim into objective canon.
- A fact, event or movement is allowed only when the DM explicitly confirms it happened.
- Dialogue, plans, guesses, attempted actions and atmospheric repetition are not facts.
- Prefer no proposal over a weak or redundant proposal.
- Temporary sensory details usually belong in a scene_thesis, not a durable fact.

Available change types and payloads:
1. fact: {{"subject": "UUID or stable name", "predicate": "short relation", "object_value": "UUID or text"}}
2. event: {{"event_type": "short type", "description": "confirmed event", "location_id": "UUID or null", "participant_ids": ["UUID"]}}
3. movement: {{"character_id": "UUID", "location_id": "UUID"}}
4. relationship: {{"subject_id": "UUID", "object_id": "UUID", "relation_type": "type", "description": "confirmed change", "reason": "cause"}}
5. scene_thesis: {{"scene_id": "UUID", "thesis_type": "canon|secret|tension|unresolved_beat|visual_state|music_mood", "text": "active scene dynamic", "visibility": "dm|public|character_only"}}

Return exactly one JSON object with a "proposals" array. Each item must have
"change_type" and "payload". Return {{"proposals": []}} when nothing durable
was confirmed. Do not use markdown fences.{scene_instruction}{mapping_lines}
"""

        messages = [
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
        ]

        response_text = ""
        async for token in self._llm_provider.generate_stream(
            messages,
            config,
            api_key,
        ):
            response_text += token

        return self._parse_response(response_text, scene_id, known_entities)

    def _parse_response(
        self,
        text: str,
        scene_id: UUID | None,
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
            proposals = data.get("proposals", [])
            results = []
            for proposal in proposals[:5]:
                change_type_value = proposal.get("change_type")
                payload = proposal.get("payload", {})
                if not change_type_value or not isinstance(payload, dict) or not payload:
                    continue

                try:
                    change_type = ChangeType(change_type_value)
                except ValueError:
                    continue

                payload = self._resolve_payload_names(
                    payload,
                    change_type,
                    known_entities,
                    scene_id,
                )
                if change_type == ChangeType.SCENE_THESIS and not scene_id:
                    continue

                results.append(
                    ProposedChangeCreate(
                        change_type=change_type,
                        payload=payload,
                    )
                )
            return results
        except Exception:
            return self._regex_fallback_parse(
                clean_text,
                scene_id,
                known_entities,
            )

    def _resolve_payload_names(
        self,
        payload: dict,
        change_type: ChangeType,
        known_entities: dict[str, str],
        scene_id: UUID | None,
    ) -> dict:
        resolved = payload.copy()
        scalar_keys = [
            "subject",
            "object_value",
            "character_id",
            "location_id",
            "subject_id",
            "object_id",
        ]
        for key in scalar_keys:
            value = resolved.get(key)
            if value and isinstance(value, str) and not self._is_uuid(value):
                entity_id = known_entities.get(value.lower())
                if entity_id:
                    resolved[key] = entity_id

        participant_ids = []
        for value in resolved.get("participant_ids", []):
            if isinstance(value, str) and not self._is_uuid(value):
                participant_ids.append(known_entities.get(value.lower(), value))
            else:
                participant_ids.append(value)
        if "participant_ids" in resolved:
            resolved["participant_ids"] = participant_ids

        if change_type == ChangeType.SCENE_THESIS and scene_id:
            resolved["scene_id"] = str(scene_id)

        return resolved

    def _regex_fallback_parse(
        self,
        text: str,
        scene_id: UUID | None,
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
                payload = json.loads(payload_text)
                payload = self._resolve_payload_names(
                    payload,
                    change_type,
                    known_entities,
                    scene_id,
                )
                if change_type == ChangeType.SCENE_THESIS and not scene_id:
                    continue
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
