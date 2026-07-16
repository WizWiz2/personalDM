import json
import re
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.models.turn import ChatMessage
from app.models.proposed_change import ProposedChangeCreate, ChangeType
from app.providers.llm_provider import LLMProvider

class MemoryScribe:
    """Uses LLM to extract structured canon deltas from narrative context (ADR-007 / MVP-SPEC)."""
    
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
        assistant_content: str
    ) -> list[ProposedChangeCreate]:
        """Analyzes the turn and returns up to 5 proposed structural changes to the canon."""
        config = await self._config_repo.get_by_campaign_id(campaign_id)
        if not config:
            return []
            
        api_key = await self._config_repo.get_decrypted_key(campaign_id)

        # 1. Fetch scene participants and location to build a mapping dictionary for LLM
        entity_mapping_prompt = ""
        known_entities = {}
        
        if scene_id:
            participants = await self._scene_repo.get_participants(scene_id)
            scene = await self._scene_repo.get_by_id(scene_id)
            
            entity_ids = list(participants)
            # Add current scene location if it exists
            # In MVP, location is just description text in Scene, but let's check entities
            # We can scan entities in campaign
            all_campaign_entities = await self._entity_repo.list_by_campaign(campaign_id)
            for ent in all_campaign_entities:
                known_entities[ent.canonical_name.lower()] = str(ent.id)
                for alias in ent.aliases:
                    known_entities[alias.lower()] = str(ent.id)
            
            if known_entities:
                entity_mapping_prompt = "\nUse the following UUID mappings for entities mentioned in the text:\n"
                for name, uuid_str in known_entities.items():
                    entity_mapping_prompt += f"- Name/Alias '{name}': UUID = {uuid_str}\n"

        # 2. Construct the prompt instructing the model to generate structured JSON delta
        system_prompt = f"""You are a structured data extractor for a tabletop RPG truth engine.
Analyze the latest turn (User turn and Assistant game master turn) and propose up to 5 changes to the world state.

Available Change Types:
1. "fact" - Objective fact. Payload format: {{"subject": "UUID or name", "predicate": "verb", "object_value": "UUID or text description"}}
2. "movement" - A character changes location. Payload format: {{"character_id": "UUID of character", "location_id": "UUID of location"}}
3. "relationship" - An update to character relationships. Payload format: {{"subject_id": "UUID of character", "object_id": "UUID of target", "relation_type": "trust/fear/loyalty/etc.", "description": "how it changed"}}
4. "scene_thesis" - narrative dynamic or secret in current scene. Payload format: {{"scene_id": "UUID of scene", "thesis_type": "canon/secret/tension/etc.", "text": "description of thesis"}}

Guidelines:
- Propose ONLY clear, concrete changes that actually happened in this turn.
- Limit to maximum 5 proposals.
- Return a JSON object with a single "proposals" array containing the items.
- If no changes occurred, return an empty list: {{"proposals": []}}
{entity_mapping_prompt}
RESPOND ONLY WITH VALID JSON. Do not include markdown wraps like ```json.
"""

        turn_content = f"User: {user_content}\nAssistant: {assistant_content}"
        messages = [
            ChatMessage(role="system", content=system_prompt),
            ChatMessage(role="user", content=turn_content)
        ]

        # 3. Request JSON completion from LLM
        response_text = ""
        async for token in self._llm_provider.generate_stream(messages, config, api_key):
            response_text += token

        # 4. Parse output
        return self._parse_response(response_text, scene_id, known_entities)

    def _parse_response(self, text: str, scene_id: UUID | None, known_entities: dict) -> list[ProposedChangeCreate]:
        # Clean response text in case model wrapped it in markdown code blocks
        clean_text = text.strip()
        if clean_text.startswith("```"):
            # Strip block markers
            lines = clean_text.splitlines()
            if lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].startswith("```"):
                lines = lines[:-1]
            clean_text = "\n".join(lines).strip()
            
        try:
            data = json.loads(clean_text)
            proposals = data.get("proposals", [])
            
            results = []
            for prop in proposals[:5]:  # Limit to 5
                ctype_str = prop.get("change_type")
                payload = prop.get("payload", {})
                
                if not ctype_str or not payload:
                    continue
                    
                # Map names to UUIDs in payload if LLM forgot to do it
                payload = self._resolve_payload_names(payload, known_entities, scene_id)
                
                try:
                    change_type = ChangeType(ctype_str)
                    results.append(ProposedChangeCreate(
                        change_type=change_type,
                        payload=payload
                    ))
                except ValueError:
                    # Invalid change type, skip
                    pass
            return results
            
        except Exception:
            # Fallback regex parsing if JSON format is broken
            return self._regex_fallback_parse(clean_text, scene_id, known_entities)

    def _resolve_payload_names(self, payload: dict, known_entities: dict, scene_id: UUID | None) -> dict:
        """Helper to replace entity names with UUIDs in payload fields if name is known."""
        resolved = payload.copy()
        
        # UUID mapping keys depending on change type
        keys_to_resolve = ["subject", "object_value", "character_id", "location_id", "subject_id", "object_id"]
        for key in keys_to_resolve:
            val = resolved.get(key)
            if val and isinstance(val, str) and not self._is_uuid(val):
                # Try to map name to UUID
                uuid_match = known_entities.get(val.lower())
                if uuid_match:
                    resolved[key] = uuid_match
                    
        # Auto-inject scene_id into thesis payload if missing
        if "scene_id" in resolved and not resolved["scene_id"] and scene_id:
            resolved["scene_id"] = str(scene_id)
            
        return resolved

    def _regex_fallback_parse(self, text: str, scene_id: UUID | None, known_entities: dict) -> list[ProposedChangeCreate]:
        """Simple fallback if the LLM output is not valid JSON but contains JSON-like structures."""
        results = []
        # Try to find all blocks matching {"change_type": "...", "payload": {...}}
        matches = re.findall(r'\{\s*"change_type"\s*:\s*"([^"]+)"\s*,\s*"payload"\s*:\s*(\{.*?\})\s*\}', text, re.DOTALL)
        for ctype_str, payload_str in matches[:5]:
            try:
                payload = json.loads(payload_str)
                payload = self._resolve_payload_names(payload, known_entities, scene_id)
                change_type = ChangeType(ctype_str)
                results.append(ProposedChangeCreate(
                    change_type=change_type,
                    payload=payload
                ))
            except Exception:
                pass
        return results

    def _is_uuid(self, val: str) -> bool:
        try:
            UUID(str(val))
            return True
        except ValueError:
            return False
