from pathlib import Path


def replace(path: str, old: str, new: str) -> None:
    file_path = Path(path)
    text = file_path.read_text(encoding="utf-8")
    if old not in text:
        raise RuntimeError(f"Expected source fragment was not found in {path}")
    file_path.write_text(text.replace(old, new), encoding="utf-8")


context_path = "src/backend/app/services/context_compiler.py"
replace(
    context_path,
    '''        current_budget_used = count_tokens(system_msg.content)\n\n        included_layers = ["layer_0_system"]''',
    '''        current_budget_used = count_tokens(system_msg.content)\n        reserved_user_tokens = (\n            count_tokens(current_user_content)\n            if actor_mode and current_user_content\n            else 0\n        )\n        content_budget = max_budget - reserved_user_tokens\n\n        included_layers = ["layer_0_system"]''',
)
replace(
    context_path,
    '''                    included_thesis_ids.append(str(thesis.id))\n\n            scene_tokens = count_tokens(scene_info)\n            if current_budget_used + scene_tokens < max_budget:\n                packages.append(scene_info)\n                current_budget_used += scene_tokens\n                included_layers.append("layer_1_scene")''',
    '''            scene_thesis_ids = [str(thesis.id) for thesis in visible_theses]\n            scene_tokens = count_tokens(scene_info)\n            if current_budget_used + scene_tokens < content_budget:\n                packages.append(scene_info)\n                current_budget_used += scene_tokens\n                included_thesis_ids.extend(scene_thesis_ids)\n                included_layers.append("layer_1_scene")''',
)
replace(
    context_path,
    '''                actor_package = await self._full_character_profile(\n                    character,\n                    include_private=True,\n                    included_item_ids=included_item_ids,\n                )\n                included_character_ids.append(str(character.id))''',
    '''                actor_item_ids: list[str] = []\n                actor_belief_ids: list[str] = []\n                actor_package = await self._full_character_profile(\n                    character,\n                    include_private=True,\n                    included_item_ids=actor_item_ids,\n                )''',
)
replace(
    context_path,
    '''                        included_belief_ids.append(str(belief.id))\n\n                other_ids = [''',
    '''                        actor_belief_ids.append(str(belief.id))\n\n                other_ids = [''',
)
replace(
    context_path,
    '''                if current_budget_used + actor_tokens < max_budget:\n                    packages.append(actor_package)\n                    current_budget_used += actor_tokens\n                    included_layers.append("layer_2_actor")''',
    '''                if current_budget_used + actor_tokens < content_budget:\n                    packages.append(actor_package)\n                    current_budget_used += actor_tokens\n                    included_character_ids.append(str(character.id))\n                    included_item_ids.extend(actor_item_ids)\n                    included_belief_ids.extend(actor_belief_ids)\n                    included_layers.append("layer_2_actor")''',
)
replace(
    context_path,
    '''            fact_package = "[Campaign Facts & History]\\n"\n            for fact in facts:\n                fact_package += f"- {fact.subject} {fact.predicate} {fact.object_value or ''}\\n"\n                included_fact_ids.append(str(fact.id))\n            fact_tokens = count_tokens(fact_package)\n            if current_budget_used + fact_tokens < max_budget:\n                packages.append(fact_package)\n                current_budget_used += fact_tokens\n                included_layers.append("layer_3_facts")''',
    '''            fact_package = "[Campaign Facts & History]\\n"\n            fact_ids: list[str] = []\n            for fact in facts:\n                fact_package += f"- {fact.subject} {fact.predicate} {fact.object_value or ''}\\n"\n                fact_ids.append(str(fact.id))\n            fact_tokens = count_tokens(fact_package)\n            if current_budget_used + fact_tokens < content_budget:\n                packages.append(fact_package)\n                current_budget_used += fact_tokens\n                included_fact_ids.extend(fact_ids)\n                included_layers.append("layer_3_facts")''',
)
replace(
    context_path,
    '''            participant_package = (\n                "[Other Present NPCs]\\n" if actor_mode else "[Present Character Cards]\\n"\n            )\n            for character in scene_characters:\n                if actor_mode and character.id == acting_character_id:\n                    continue\n                included_character_ids.append(str(character.id))''',
    '''            participant_package = (\n                "[Other Present NPCs]\\n" if actor_mode else "[Present Character Cards]\\n"\n            )\n            participant_character_ids: list[str] = []\n            participant_belief_ids: list[str] = []\n            participant_item_ids: list[str] = []\n            for character in scene_characters:\n                if actor_mode and character.id == acting_character_id:\n                    continue\n                participant_character_ids.append(str(character.id))''',
)
replace(
    context_path,
    '''                        included_item_ids=included_item_ids,''',
    '''                        included_item_ids=participant_item_ids,''',
)
replace(
    context_path,
    '''                            included_belief_ids.append(str(belief.id))\n\n            participant_tokens = count_tokens(participant_package)\n            if current_budget_used + participant_tokens < max_budget:\n                packages.append(participant_package)\n                current_budget_used += participant_tokens\n                included_layers.append("layer_4_character_cards")''',
    '''                            participant_belief_ids.append(str(belief.id))\n\n            participant_tokens = count_tokens(participant_package)\n            if current_budget_used + participant_tokens < content_budget:\n                packages.append(participant_package)\n                current_budget_used += participant_tokens\n                included_character_ids.extend(participant_character_ids)\n                included_belief_ids.extend(participant_belief_ids)\n                included_item_ids.extend(participant_item_ids)\n                included_layers.append("layer_4_character_cards")''',
)
replace(
    context_path,
    '''            if current_budget_used + message_tokens >= max_budget:''',
    '''            if current_budget_used + message_tokens >= content_budget:''',
)
replace(
    context_path,
    '''        if actor_mode and current_user_content:\n            user_tokens = count_tokens(current_user_content)\n            if current_budget_used + user_tokens < max_budget:\n                final_messages.append(\n                    ChatMessage(role="user", content=current_user_content)\n                )\n                current_budget_used += user_tokens\n                included_layers.append("layer_6_current_user")''',
    '''        if actor_mode and current_user_content:\n            final_messages.append(ChatMessage(role="user", content=current_user_content))\n            current_budget_used += reserved_user_tokens\n            included_layers.append("layer_6_current_user")''',
)
replace(
    context_path,
    '''            "history_turns_count": len(history_to_include),\n        }''',
    '''            "history_turns_count": len(history_to_include),\n            "current_user_reserved": bool(actor_mode and current_user_content),\n        }''',
)

memory_path = "src/backend/app/services/memory_scribe.py"
replace(
    memory_path,
    '''        except LLMProviderError as exc:\n            self.last_audit = CanonAudit(\n                envelope_valid=False,\n                error=str(exc),\n            ).model_dump()\n            return []''',
    '''        except LLMProviderError as exc:\n            self.last_audit = CanonAudit(\n                envelope_valid=False,\n                error=str(exc),\n            ).model_dump()\n            raise''',
)
replace(
    memory_path,
    '''            actor_id = str(acting_character_id) if acting_character_id else None\n            player_id = str(player_character_id) if player_character_id else None\n            source_id = resolved.get("source_character_id") or actor_id\n            recipient_id = resolved.get("recipient_id")\n            if not recipient_id and source_id and player_id and source_id != player_id:\n                recipient_id = player_id\n            if recipient_id == source_id and player_id and source_id != player_id:\n                recipient_id = player_id\n            proposition = resolved.get("proposition")\n            if not recipient_id or not proposition:\n                return None''',
    '''            actor_id = str(acting_character_id) if acting_character_id else None\n            source_id = resolved.get("source_character_id") or actor_id\n            recipient_id = resolved.get("recipient_id")\n            proposition = resolved.get("proposition")\n            if not recipient_id or recipient_id == source_id or not proposition:\n                return None''',
)

curator_path = "src/backend/app/services/thesis_curator.py"
replace(
    curator_path,
    '''        except LLMProviderError:\n            return None\n\n        desired = self._parse_response(response_text, set(entity_names))\n        if desired is None:\n            return None''',
    '''        except LLMProviderError:\n            raise\n\n        desired = self._parse_response(response_text, set(entity_names))\n        if desired is None:\n            raise LLMProviderError("Thesis Curator returned invalid structured output")''',
)

scene_repo_path = "src/backend/app/db/repositories/scene_repo.py"
replace(
    scene_repo_path,
    "from app.db.tables import Scene, SceneParticipant, SceneThesis",
    "from app.db.tables import Entity, Scene, SceneParticipant, SceneThesis",
)
replace(
    scene_repo_path,
    '''    async def add_participant(self, scene_id: UUID, entity_id: UUID) -> bool:\n        result = await self._session.execute(''',
    '''    async def add_participant(self, scene_id: UUID, entity_id: UUID) -> bool:\n        scene_result = await self._session.execute(\n            select(Scene).where(Scene.id == str(scene_id))\n        )\n        scene = scene_result.scalar_one_or_none()\n        if not scene:\n            raise ValueError("Scene not found")\n\n        entity_result = await self._session.execute(\n            select(Entity).where(Entity.id == str(entity_id))\n        )\n        entity = entity_result.scalar_one_or_none()\n        if not entity or entity.campaign_id != scene.campaign_id:\n            raise ValueError("Participant must belong to the same campaign as the scene")\n        if entity.entity_type != "character":\n            raise ValueError("Only character entities may participate in a scene")\n\n        result = await self._session.execute(''',
)

replace(
    "src/backend/app/api/scenes.py",
    '''    await SceneRepository(session).add_participant(scene_id, entity_id)\n    await session.commit()\n    return {"success": True}''',
    '''    try:\n        await SceneRepository(session).add_participant(scene_id, entity_id)\n        await session.commit()\n    except ValueError as exc:\n        await session.rollback()\n        raise HTTPException(status_code=400, detail=str(exc)) from exc\n    return {"success": True}''',
)

world_path = "src/backend/app/api/world_state.py"
replace(world_path, "from typing import Literal\n", "")
replace(
    world_path,
    '''    def one_destination(self):\n        if self.owner_id and self.location_id:\n            raise ValueError("item can have an owner or a location, not both")\n        return self''',
    '''    def one_destination(self):\n        destinations = int(self.owner_id is not None) + int(self.location_id is not None)\n        if destinations != 1:\n            raise ValueError("item must have exactly one owner or location")\n        return self''',
)

alias_test = "src/backend/tests/test_memory_scribe_aliases.py"
text = Path(alias_test).read_text(encoding="utf-8")
start = text.index("def test_direct_npc_knowledge_goes_to_player_not_speaker():")
end = text.index("\n\ndef test_public_event_expands_witnesses", start)
text = text[:start] + '''def test_knowledge_with_speaker_as_recipient_is_rejected():\n    actor, player, _, _, known, known_ids = setup_entities()\n    scribe = MemoryScribe(None)\n    payload = scribe._normalize_payload(\n        ChangeType.KNOWLEDGE,\n        {\n            "recipient_id": "SELF",\n            "source_character_id": "SELF",\n            "proposition": "Гаррик сообщил, что западная дорога перекрыта.",\n            "confidence": 0,\n        },\n        known,\n        known_ids,\n        actor,\n        player,\n        [str(actor), str(player)],\n    )\n\n    assert payload is None\n''' + text[end:]
Path(alias_test).write_text(text, encoding="utf-8")

Path("src/backend/tests/test_canon_safety_fixes.py").write_text('''from uuid import uuid4\n\nimport pytest\nfrom pydantic import ValidationError\nfrom sqlalchemy.ext.asyncio import AsyncSession\n\nfrom app.api.world_state import ItemTransferCommand\nfrom app.db.repositories.campaign_repo import CampaignRepository\nfrom app.db.repositories.entity_repo import EntityRepository\nfrom app.db.repositories.fact_repo import FactRepository\nfrom app.db.repositories.provider_config_repo import ProviderConfigRepository\nfrom app.db.repositories.scene_repo import SceneRepository\nfrom app.db.repositories.turn_repo import TurnRepository\nfrom app.models.campaign import CampaignCreate\nfrom app.models.character import CharacterCreate\nfrom app.models.entity import EntityCreate, EntityType\nfrom app.models.fact import FactCreate\nfrom app.models.provider_config import ProviderConfigCreate\nfrom app.models.proposed_change import ChangeType\nfrom app.models.scene import SceneCreate\nfrom app.models.turn import TurnCreate\nfrom app.services.context_compiler import ContextCompiler\nfrom app.services.memory_scribe import MemoryScribe\n\n\nasync def configured_campaign(db_session: AsyncSession, name: str):\n    campaign_id = uuid4()\n    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name=name))\n    await ProviderConfigRepository(db_session).create_or_update(\n        campaign_id,\n        ProviderConfigCreate(\n            base_url="http://localhost:11434/v1",\n            model_name="test",\n            context_window=2200,\n        ),\n    )\n    return campaign_id\n\n\n@pytest.mark.asyncio\nasync def test_manifest_lists_only_facts_actually_sent(db_session: AsyncSession):\n    campaign_id = await configured_campaign(db_session, "Manifest accuracy")\n    actor = await EntityRepository(db_session).create_character(\n        campaign_id,\n        CharacterCreate(\n            entity_type=EntityType.CHARACTER,\n            canonical_name="Actor",\n            description="A concise actor",\n        ),\n    )\n    scene = await SceneRepository(db_session).create(\n        campaign_id, SceneCreate(title="Small room")\n    )\n    await SceneRepository(db_session).add_participant(scene.id, actor.id)\n    for index in range(12):\n        await FactRepository(db_session).create(\n            campaign_id,\n            FactCreate(\n                subject=f"Oversized subject {index}",\n                predicate="contains",\n                object_value="x" * 600,\n                visibility="public",\n            ),\n        )\n    await db_session.commit()\n\n    messages, metadata = await ContextCompiler(db_session).compile_context(\n        campaign_id=campaign_id,\n        acting_character_id=actor.id,\n        scene_id=scene.id,\n        current_user_content="Continue.",\n    )\n\n    assert "[Campaign Facts & History]" not in "\\n".join(\n        message.content for message in messages\n    )\n    assert metadata["included_fact_ids"] == []\n\n\n@pytest.mark.asyncio\nasync def test_repeated_current_message_is_always_last(db_session: AsyncSession):\n    campaign_id = await configured_campaign(db_session, "Repeated input")\n    actor = await EntityRepository(db_session).create_character(\n        campaign_id,\n        CharacterCreate(\n            entity_type=EntityType.CHARACTER,\n            canonical_name="Actor",\n            description="y" * 2500,\n        ),\n    )\n    scene = await SceneRepository(db_session).create(\n        campaign_id, SceneCreate(title="Conversation")\n    )\n    await SceneRepository(db_session).add_participant(scene.id, actor.id)\n    await TurnRepository(db_session).create(\n        campaign_id,\n        TurnCreate(\n            role="user",\n            content="Я молчу.",\n            scene_id=scene.id,\n            acting_character_id=actor.id,\n        ),\n    )\n    await db_session.commit()\n\n    messages, metadata = await ContextCompiler(db_session).compile_context(\n        campaign_id=campaign_id,\n        acting_character_id=actor.id,\n        scene_id=scene.id,\n        current_user_content="Я молчу.",\n    )\n\n    assert messages[-1].role == "user"\n    assert messages[-1].content == "Я молчу."\n    assert metadata["current_user_reserved"] is True\n\n\n@pytest.mark.asyncio\nasync def test_knowledge_without_explicit_recipient_is_rejected(\n    db_session: AsyncSession,\n):\n    source_id = uuid4()\n    player_id = uuid4()\n    normalized = MemoryScribe(db_session)._normalize_payload(\n        ChangeType.KNOWLEDGE,\n        {\n            "source_character_id": str(source_id),\n            "proposition": "The gate is open",\n        },\n        known_entities={},\n        known_ids={str(source_id), str(player_id)},\n        acting_character_id=source_id,\n        player_character_id=player_id,\n        scene_participant_ids=[str(source_id), str(player_id)],\n    )\n    assert normalized is None\n\n\n@pytest.mark.asyncio\nasync def test_scene_rejects_cross_campaign_and_non_character_participants(\n    db_session: AsyncSession,\n):\n    campaign_a = uuid4()\n    campaign_b = uuid4()\n    await CampaignRepository(db_session).create(campaign_a, CampaignCreate(name="A"))\n    await CampaignRepository(db_session).create(campaign_b, CampaignCreate(name="B"))\n    scene = await SceneRepository(db_session).create(\n        campaign_a, SceneCreate(title="A scene")\n    )\n    outsider = await EntityRepository(db_session).create_character(\n        campaign_b,\n        CharacterCreate(\n            entity_type=EntityType.CHARACTER, canonical_name="Outsider"\n        ),\n    )\n    location = await EntityRepository(db_session).create(\n        campaign_a,\n        EntityCreate(\n            entity_type=EntityType.LOCATION, canonical_name="Courtyard"\n        ),\n    )\n\n    with pytest.raises(ValueError, match="same campaign"):\n        await SceneRepository(db_session).add_participant(scene.id, outsider.id)\n    with pytest.raises(ValueError, match="Only character"):\n        await SceneRepository(db_session).add_participant(scene.id, location.id)\n\n\ndef test_item_transfer_requires_exactly_one_destination():\n    with pytest.raises(ValidationError):\n        ItemTransferCommand()\n    with pytest.raises(ValidationError):\n        ItemTransferCommand(owner_id=uuid4(), location_id=uuid4())\n    assert ItemTransferCommand(owner_id=uuid4()).owner_id is not None\n    assert ItemTransferCommand(location_id=uuid4()).location_id is not None\n''', encoding="utf-8")

Path("tools/apply_canon_safety_fixes.py").unlink()
Path(".github/workflows/apply-canon-safety-fixes.yml").unlink()
