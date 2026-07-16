import json
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.provider_config_repo import ProviderConfigRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.db.tables import CharacterGoal
from app.models.turn import ChatMessage

# Exact cl100k counting is useful for OpenAI models. For unknown local models we
# deliberately overestimate instead of risking prompt overflow.
try:
    import tiktoken

    _encoder = tiktoken.get_encoding("cl100k_base")

    def count_tokens(text: str) -> int:
        return len(_encoder.encode(text))

except ImportError:

    def count_tokens(text: str) -> int:
        return max(1, len(text) // 3)


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
        self._turn_repo = TurnRepository(session)

    async def compile_context(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None = None,
        scene_id: UUID | None = None,
        current_user_content: str | None = None,
    ) -> tuple[list[ChatMessage], dict]:
        """Compile a prompt with an explicit narrator/actor knowledge boundary.

        Narrator mode (no acting_character_id) may receive the complete DM canon.
        Actor mode receives only public scene state plus the acting character's
        own profile, goals, beliefs and relationships. DM-only facts, DM-only
        scene theses and unscoped chat history are not included in actor mode.
        """
        config = await self._config_repo.get_by_campaign_id(campaign_id)
        context_window = config.context_window if config else settings.LLM_CONTEXT_WINDOW
        safety_margin = int(context_window * settings.SAFETY_MARGIN_PERCENT)
        max_budget = context_window - settings.RESPONSE_RESERVE_TOKENS - safety_margin
        actor_mode = acting_character_id is not None

        campaign = await self._campaign_repo.get_by_id(campaign_id)
        system_instr = (
            campaign.system_instructions
            if campaign and campaign.system_instructions
            else "You are a Tabletop Roleplaying Game Master."
        )
        style = (
            f"\nStyle instructions: {campaign.narrative_style}"
            if campaign and campaign.narrative_style
            else ""
        )
        boundary = (
            "\nYou are roleplaying one specific character. Use only the knowledge "
            "present in this prompt. Do not infer hidden DM facts or other characters' secrets."
            if actor_mode
            else "\nYou are the omniscient narrator and may use the complete DM canon."
        )
        system_msg = ChatMessage(role="system", content=f"{system_instr}{style}{boundary}")
        current_budget_used = count_tokens(system_msg.content)

        included_layers = ["layer_0_system"]
        included_fact_ids: list[str] = []
        included_belief_ids: list[str] = []
        included_thesis_ids: list[str] = []
        included_turn_ids: list[str] = []
        scene_info = ""
        npc_package = ""

        # Layer 1: observable scene state and visibility-filtered theses.
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

                theses = await self._scene_repo.list_theses_by_scene(scene_id, active_only=True)
                visible_theses = []
                for thesis in theses:
                    if not actor_mode:
                        visible_theses.append(thesis)
                    elif thesis.visibility == "public":
                        visible_theses.append(thesis)
                    elif thesis.visibility == "character_only" and thesis.related_entity_ids:
                        if acting_character_id in thesis.related_entity_ids:
                            visible_theses.append(thesis)

                if visible_theses:
                    scene_info += "Active Scene Notes:\n"
                    for thesis in visible_theses:
                        pinned_str = " (Important)" if thesis.pinned else ""
                        scene_info += f"- {thesis.text}{pinned_str}\n"
                        included_thesis_ids.append(str(thesis.id))

                scene_tokens = count_tokens(scene_info)
                if current_budget_used + scene_tokens < max_budget:
                    current_budget_used += scene_tokens
                    included_layers.append("layer_1_scene")
                else:
                    available_chars = max(0, (max_budget - current_budget_used) * 3)
                    scene_info = scene_info[:available_chars]
                    current_budget_used = max_budget

        scene_characters = []
        if scene_id:
            scene_characters = await self._entity_repo.get_characters_in_scene(scene_id)

        # Layer 2: the acting character's own private package.
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
                    npc_package += f"Your Secret Background: {char_data.backstory_secret}\n"
                if char_data.emotional_state:
                    npc_package += f"Current Emotional State: {char_data.emotional_state}\n"

                goals_result = await self._session.execute(
                    select(CharacterGoal).where(
                        CharacterGoal.character_id == str(acting_character_id),
                        CharacterGoal.status == "active",
                    )
                )
                char_goals = goals_result.scalars().all()
                if char_goals:
                    npc_package += "Goals:\n"
                    for goal in char_goals:
                        npc_package += f"- {goal.description} (Priority {goal.priority})\n"

                beliefs = await self._belief_repo.get_for_character(
                    acting_character_id, active_only=True
                )
                if beliefs:
                    npc_package += "Beliefs & Private Knowledge:\n"
                    for belief in beliefs:
                        npc_package += (
                            f"- {belief.proposition} (Confidence: {belief.confidence})\n"
                        )
                        included_belief_ids.append(str(belief.id))

                if scene_characters:
                    other_participant_ids = [
                        character.id
                        for character in scene_characters
                        if character.id != acting_character_id
                    ]
                    if other_participant_ids:
                        char_names_map = {
                            character.id: character.canonical_name
                            for character in scene_characters
                        }
                        relationships = await self._relationship_repo.get_for_character(
                            acting_character_id,
                            object_ids=other_participant_ids,
                        )
                        if relationships:
                            npc_package += "Relationships to others in scene:\n"
                            for relationship in relationships:
                                object_id = (
                                    UUID(relationship.object_id)
                                    if isinstance(relationship.object_id, str)
                                    else relationship.object_id
                                )
                                object_name = char_names_map.get(object_id, "Someone")
                                npc_package += (
                                    f"- Toward {object_name}: {relationship.relation_type} "
                                    f"({relationship.description})\n"
                                )

                npc_tokens = count_tokens(npc_package)
                if current_budget_used + npc_tokens < max_budget:
                    current_budget_used += npc_tokens
                    included_layers.append("layer_2_actor")
                else:
                    available_chars = max(0, (max_budget - current_budget_used) * 3)
                    npc_package = npc_package[:available_chars]
                    current_budget_used = max_budget

        # Layer 3: actor sees only public facts. Narrator sees the complete canon.
        world_facts = ""
        facts = await self._fact_repo.list_active(
            campaign_id,
            visibility="public" if actor_mode else None,
        )
        if facts:
            world_facts = "[Campaign Facts & History]\n"
            for fact in facts:
                world_facts += (
                    f"- {fact.subject} {fact.predicate} {fact.object_value or ''}\n"
                )
                included_fact_ids.append(str(fact.id))

            facts_tokens = count_tokens(world_facts)
            if current_budget_used + facts_tokens < max_budget:
                current_budget_used += facts_tokens
                included_layers.append("layer_3_facts")
            else:
                available_chars = max(0, (max_budget - current_budget_used) * 3)
                world_facts = world_facts[:available_chars]
                current_budget_used = max_budget

        # Layer 4: public profiles of other characters currently present.
        other_npcs_package = ""
        if scene_characters:
            other_chars = [
                character
                for character in scene_characters
                if character.id != acting_character_id
            ]
            if other_chars:
                other_npcs_package = "[Other Present NPCs]\n"
                for character in other_chars:
                    other_npcs_package += (
                        f"- {character.canonical_name} (Status: {character.status})\n"
                    )
                    if character.description:
                        other_npcs_package += f"  Description: {character.description}\n"
                    if character.personality:
                        other_npcs_package += f"  Personality: {character.personality}\n"

                other_npcs_tokens = count_tokens(other_npcs_package)
                if current_budget_used + other_npcs_tokens < max_budget:
                    current_budget_used += other_npcs_tokens
                    included_layers.append("layer_4_other_npcs")
                else:
                    available_chars = max(0, (max_budget - current_budget_used) * 3)
                    other_npcs_package = other_npcs_package[:available_chars]
                    current_budget_used = max_budget

        # Layer 5: unscoped chat history is safe only for the omniscient narrator.
        history_to_include: list[ChatMessage] = []
        if not actor_mode:
            history_records = await self._turn_repo.get_history(
                campaign_id,
                limit=30,
                active_only=True,
            )
            for turn in reversed(history_records):
                message_tokens = count_tokens(turn.content)
                if current_budget_used + message_tokens < max_budget:
                    current_budget_used += message_tokens
                    history_to_include.insert(
                        0, ChatMessage(role=turn.role, content=turn.content)
                    )
                    included_turn_ids.insert(0, str(turn.id))
                else:
                    break
            if history_to_include:
                included_layers.append("layer_5_history")

        final_messages = []
        system_content = system_msg.content
        for package in (scene_info, npc_package, world_facts, other_npcs_package):
            if package:
                system_content += f"\n\n{package}"
        final_messages.append(ChatMessage(role="system", content=system_content))
        final_messages.extend(history_to_include)

        # Actor mode deliberately excludes unscoped history, but still needs the
        # current player utterance/action to answer this turn.
        if actor_mode and current_user_content:
            user_tokens = count_tokens(current_user_content)
            if current_budget_used + user_tokens < max_budget:
                final_messages.append(
                    ChatMessage(role="user", content=current_user_content)
                )
                current_budget_used += user_tokens
                included_layers.append("layer_5_current_user")

        metadata = {
            "token_budget_max": max_budget,
            "token_budget_used": current_budget_used,
            "included_layers": included_layers,
            "acting_character_id": str(acting_character_id) if acting_character_id else None,
            "scene_id": str(scene_id) if scene_id else None,
            "actor_scope_strict": actor_mode,
            "included_fact_ids": included_fact_ids,
            "included_belief_ids": included_belief_ids,
            "included_thesis_ids": included_thesis_ids,
            "included_turn_ids": included_turn_ids,
            "history_turns_count": len(history_to_include),
        }

        return final_messages, metadata
