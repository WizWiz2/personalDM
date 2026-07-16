import json
from uuid import UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.config import settings
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.tables import CharacterGoal
from app.models.turn import ChatMessage

# Try importing tiktoken for exact token counting, fall back to heuristic
try:
    import tiktoken
    _encoder = tiktoken.get_encoding("cl100k_base")
    def count_tokens(text: str) -> int:
        return len(_encoder.encode(text))
except ImportError:
    def count_tokens(text: str) -> int:
        return len(text) // 4

class ContextCompiler:
    def __init__(self, session: AsyncSession):
        self._session = session
        self._campaign_repo = CampaignRepository(session)
        self._scene_repo = SceneRepository(session)
        self._entity_repo = EntityRepository(session)
        self._fact_repo = FactRepository(session)
        self._belief_repo = BeliefRepository(session)
        self._relationship_repo = RelationshipRepository(session)
        self._config_repo = ProviderConfigRepository(session)

    async def compile_context(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None = None,
        scene_id: UUID | None = None
    ) -> tuple[list[ChatMessage], dict]:
        """Compiles LLM prompt context using a multi-pass token budgeting strategy (ADR-007).

        Returns:
            list[ChatMessage]: Context messages for the LLM
            dict: Compile metadata (token usage, included layers, source mapping)
        """
        # Fetch configurations
        config = await self._config_repo.get_by_campaign_id(campaign_id)
        context_window = config.context_window if config else settings.LLM_CONTEXT_WINDOW
        
        # Calculate budgets
        safety_margin = int(context_window * settings.SAFETY_MARGIN_PERCENT)
        max_budget = context_window - settings.RESPONSE_RESERVE_TOKENS - safety_margin
        
        # Fetch core data
        campaign = await self._campaign_repo.get_by_id(campaign_id)
        
        # 1. compile Layer 0: System Instructions
        system_instr = campaign.system_instructions if campaign and campaign.system_instructions else "You are a Tabletop Roleplaying Game Master."
        style = f"\nStyle instructions: {campaign.narrative_style}" if campaign and campaign.narrative_style else ""
        system_prompt = f"{system_instr}{style}"
        
        system_msg = ChatMessage(role="system", content=system_prompt)
        current_budget_used = count_tokens(system_msg.content)
        
        included_layers = ["layer_0_system"]
        scene_info = ""
        npc_package = ""
        
        # 2. compile Layer 1: Current Scene and Thesis
        if scene_id:
            scene = await self._scene_repo.get_by_id(scene_id)
            if scene:
                scene_info = f"[Current Scene: {scene.title}]\n"
                if scene.location_description:
                    scene_info += f"Location: {scene.location_description}\n"
                if scene.mood:
                    scene_info += f"Mood: {scene.mood}\n"
                if scene.tension:
                    scene_info += f"Tension: {scene.tension}\n"
                
                # Fetch theses visible to the acting character
                theses = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
                visible_theses = []
                for t in theses:
                    # Visible if public, or if dm (since this is DM model), or if character_only and belongs to acting character
                    if t.visibility == "public" or t.visibility == "dm":
                        visible_theses.append(t)
                    elif t.visibility == "character_only" and acting_character_id and t.related_entity_ids:
                        if acting_character_id in t.related_entity_ids:
                            visible_theses.append(t)
                            
                if visible_theses:
                    scene_info += "Active Scene Notes:\n"
                    for t in visible_theses:
                        pinned_str = " (Important)" if t.pinned else ""
                        scene_info += f"- {t.text}{pinned_str}\n"
                
                scene_tokens = count_tokens(scene_info)
                if current_budget_used + scene_tokens < max_budget:
                    current_budget_used += scene_tokens
                    included_layers.append("layer_1_scene")
                else:
                    scene_info = scene_info[:(max_budget - current_budget_used) * 4]  # Trim
                    current_budget_used = max_budget
        
        # 3. compile Layer 2: Acting NPC Package (Private Knowledge and Goals)
        scene_characters = []
        if scene_id:
            scene_characters = await self._entity_repo.get_characters_in_scene(scene_id)

        if acting_character_id:
            char_data = await self._entity_repo.get_character(acting_character_id)
            if char_data:
                npc_package = f"[Acting Character Profile: {char_data.canonical_name}]\n"
                if char_data.description:
                    npc_package += f"Description: {char_data.description}\n"
                if char_data.personality:
                    npc_package += f"Personality: {char_data.personality}\n"
                if char_data.appearance:
                    npc_package += f"Appearance: {char_data.appearance}\n"
                if char_data.speech_patterns:
                    npc_package += f"Speech style: {char_data.speech_patterns}\n"
                if char_data.biography:
                    npc_package += f"Public Background: {char_data.biography}\n"
                if char_data.backstory_secret:
                    npc_package += f"Secret Background: {char_data.backstory_secret}\n"
                if char_data.emotional_state:
                    npc_package += f"Current Emotional State: {char_data.emotional_state}\n"
                    
                # Fetch character goals
                goals = await self._session.execute(
                    select(CharacterGoal).where(CharacterGoal.character_id == str(acting_character_id), CharacterGoal.status == "active")
                )
                char_goals = goals.scalars().all()
                if char_goals:
                    npc_package += "Goals:\n"
                    for g in char_goals:
                        npc_package += f"- {g.description} (Priority {g.priority})\n"

                # Fetch character beliefs (known/believed facts)
                beliefs = await self._belief_repo.get_for_character(acting_character_id, active_only=True)
                if beliefs:
                    npc_package += "Beliefs & Private Knowledge:\n"
                    for b in beliefs:
                        npc_package += f"- {b.proposition} (Confidence: {b.confidence})\n"

                # Fetch character relationships to participants in current scene (optimized O(1) query)
                if scene_characters:
                    other_participant_ids = [c.id for c in scene_characters if c.id != acting_character_id]
                    if other_participant_ids:
                        char_names_map = {c.id: c.canonical_name for c in scene_characters}
                        rels = await self._relationship_repo.get_for_character(acting_character_id, object_ids=other_participant_ids)
                        if rels:
                            npc_package += "Relationships to others in scene:\n"
                            for r in rels:
                                obj_id = UUID(r.object_id) if isinstance(r.object_id, str) else r.object_id
                                obj_name = char_names_map.get(obj_id, "Someone")
                                npc_package += f"- Toward {obj_name}: {r.relation_type} ({r.description})\n"

                npc_tokens = count_tokens(npc_package)
                if current_budget_used + npc_tokens < max_budget:
                    current_budget_used += npc_tokens
                    included_layers.append("layer_2_npc")
                else:
                    npc_package = npc_package[:(max_budget - current_budget_used) * 4]
                    current_budget_used = max_budget

        # 4. compile Layer 3: World Facts (Global active facts)
        world_facts = ""
        facts = await self._fact_repo.list_active(campaign_id, visibility="dm")
        if facts:
            world_facts = "[Campaign Facts & History]\n"
            for f in facts:
                world_facts += f"- {f.subject} {f.predicate} {f.object_value or ''}\n"
                
            facts_tokens = count_tokens(world_facts)
            if current_budget_used + facts_tokens < max_budget:
                current_budget_used += facts_tokens
                included_layers.append("layer_3_facts")
            else:
                world_facts = world_facts[:(max_budget - current_budget_used) * 4]
                current_budget_used = max_budget

        # 5. compile Layer 4: Other Present NPCs Profiles (from already loaded list)
        other_npcs_package = ""
        if scene_characters:
            other_chars = [c for c in scene_characters if c.id != acting_character_id]
            if other_chars:
                other_npcs_package = "[Other Present NPCs]\n"
                for c in other_chars:
                    desc_str = f" - Description: {c.description}" if c.description else ""
                    personality_str = f" - Personality: {c.personality}" if c.personality else ""
                    other_npcs_package += f"- {c.canonical_name} (Status: {c.status})\n{desc_str}\n{personality_str}\n"
                
                other_npcs_tokens = count_tokens(other_npcs_package)
                if current_budget_used + other_npcs_tokens < max_budget:
                    current_budget_used += other_npcs_tokens
                    included_layers.append("layer_4_other_npcs")
                else:
                    other_npcs_package = other_npcs_package[:(max_budget - current_budget_used) * 4]
                    current_budget_used = max_budget

        # 6. compile Layer 5: Chat History (Sliding Window)
        from app.db.repositories.turn_repo import TurnRepository
        turn_repo = TurnRepository(self._session)
        history = await turn_repo.get_sliding_window(campaign_id, max_turns=30)
        
        history_to_include = []
        for msg in reversed(history):
            msg_tokens = count_tokens(msg.content)
            if current_budget_used + msg_tokens < max_budget:
                current_budget_used += msg_tokens
                history_to_include.insert(0, msg)
            else:
                break
                
        if history_to_include:
            included_layers.append("layer_5_history")

        # 7. compile final message list
        final_messages = []
        system_content = system_msg.content
        if scene_info:
            system_content += f"\n\n{scene_info}"
        if npc_package:
            system_content += f"\n\n{npc_package}"
        if world_facts:
            system_content += f"\n\n{world_facts}"
        if other_npcs_package:
            system_content += f"\n\n{other_npcs_package}"
            
        final_messages.append(ChatMessage(role="system", content=system_content))
        final_messages.extend(history_to_include)

        metadata = {
            "token_budget_max": max_budget,
            "token_budget_used": current_budget_used,
            "included_layers": included_layers,
            "acting_character_id": str(acting_character_id) if acting_character_id else None,
            "scene_id": str(scene_id) if scene_id else None,
            "history_turns_count": len(history_to_include)
        }
        
        return final_messages, metadata
