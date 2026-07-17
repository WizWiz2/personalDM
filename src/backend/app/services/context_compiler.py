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
from app.db.tables import CharacterGoal, Entity, Item
from app.models.turn import ChatMessage

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

    async def _owned_items(self, owner_id: UUID) -> list[tuple[UUID, str]]:
        result = await self._session.execute(
            select(Entity.id, Entity.canonical_name)
            .join(Item, Item.entity_id == Entity.id)
            .where(Item.current_owner_id == str(owner_id))
            .order_by(Entity.canonical_name)
        )
        return [(UUID(entity_id), name) for entity_id, name in result.all()]

    @staticmethod
    def _profile_lists(custom_fields: dict | None) -> tuple[list[str], list[str]]:
        custom = custom_fields or {}
        capabilities = [
            str(value) for value in custom.get("capabilities", []) if str(value).strip()
        ]
        limitations = [
            str(value) for value in custom.get("limitations", []) if str(value).strip()
        ]
        return capabilities, limitations

    async def _full_character_profile(
        self,
        character,
        include_private: bool,
        included_item_ids: list[str],
    ) -> str:
        lines = [f"[Character Card: {character.canonical_name}]"]
        fields = [
            ("Description", character.description),
            ("Appearance", character.appearance),
            ("Personality", character.personality),
            ("Voice", character.voice),
            ("Speech style", character.speech_patterns),
            ("Public background", character.backstory_public or character.biography),
            ("Current emotional state", character.emotional_state),
        ]
        for label, value in fields:
            if value:
                lines.append(f"{label}: {value}")

        for label, values in (
            ("Values", character.values),
            ("Fears", character.fears),
            ("Desires", character.desires),
            ("Current intentions", character.current_intentions),
        ):
            if values:
                lines.append(f"{label}: " + "; ".join(values))

        capabilities, limitations = self._profile_lists(character.custom_fields)
        if capabilities:
            lines.append("Capabilities: " + "; ".join(capabilities))
        if limitations:
            lines.append("Explicit limitations: " + "; ".join(limitations))

        items = await self._owned_items(character.id)
        if items:
            lines.append("Owned equipment: " + "; ".join(name for _, name in items))
            included_item_ids.extend(str(item_id) for item_id, _ in items)
        else:
            lines.append("Owned equipment: none recorded")

        if include_private and character.backstory_secret:
            lines.append(f"Secret background: {character.backstory_secret}")
        return "\n".join(lines) + "\n"

    async def compile_context(
        self,
        campaign_id: UUID,
        acting_character_id: UUID | None = None,
        scene_id: UUID | None = None,
        current_user_content: str | None = None,
    ) -> tuple[list[ChatMessage], dict]:
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
            "present in this prompt. Do not infer hidden DM facts or other characters' "
            "secrets. Do not use abilities or equipment absent from your card."
            if actor_mode
            else "\nYou are the omniscient narrator. Respect every character card, "
            "capability, limitation, owned item and current location. Do not invent a "
            "successful ability, item or movement that is not represented in structured "
            "state; narrate uncertainty or failure instead."
        )
        system_msg = ChatMessage(role="system", content=f"{system_instr}{style}{boundary}")
        current_budget_used = count_tokens(system_msg.content)

        included_layers = ["layer_0_system"]
        included_fact_ids: list[str] = []
        included_belief_ids: list[str] = []
        included_thesis_ids: list[str] = []
        included_turn_ids: list[str] = []
        included_item_ids: list[str] = []
        included_character_ids: list[str] = []
        packages: list[str] = []

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

                visible_theses = []
                for thesis in await self._scene_repo.list_theses_by_scene(
                    scene_id,
                    active_only=True,
                ):
                    if not actor_mode:
                        visible_theses.append(thesis)
                    elif thesis.visibility == "public":
                        visible_theses.append(thesis)
                    elif (
                        thesis.visibility == "character_only"
                        and acting_character_id in thesis.related_entity_ids
                    ):
                        visible_theses.append(thesis)
                if visible_theses:
                    scene_info += "Active Scene Notes:\n"
                    for thesis in visible_theses:
                        scene_info += (
                            f"- {thesis.text}"
                            f"{' (Important)' if thesis.pinned else ''}\n"
                        )
                        included_thesis_ids.append(str(thesis.id))

                scene_tokens = count_tokens(scene_info)
                if current_budget_used + scene_tokens < max_budget:
                    packages.append(scene_info)
                    current_budget_used += scene_tokens
                    included_layers.append("layer_1_scene")

        scene_characters = (
            await self._entity_repo.get_characters_in_scene(scene_id)
            if scene_id
            else []
        )

        if acting_character_id:
            character = await self._entity_repo.get_character(acting_character_id)
            if character and character.campaign_id == campaign_id:
                actor_package = await self._full_character_profile(
                    character,
                    include_private=True,
                    included_item_ids=included_item_ids,
                )
                included_character_ids.append(str(character.id))

                goals_result = await self._session.execute(
                    select(CharacterGoal).where(
                        CharacterGoal.character_id == str(acting_character_id),
                        CharacterGoal.status == "active",
                    )
                )
                goals = goals_result.scalars().all()
                if goals:
                    actor_package += "Goals:\n"
                    for goal in goals:
                        actor_package += (
                            f"- {goal.description} (Priority {goal.priority})\n"
                        )

                beliefs = await self._belief_repo.get_for_character(
                    acting_character_id,
                    active_only=True,
                )
                if beliefs:
                    actor_package += "Beliefs & Private Knowledge:\n"
                    for belief in beliefs:
                        actor_package += (
                            f"- {belief.proposition} "
                            f"(Confidence: {belief.confidence})\n"
                        )
                        included_belief_ids.append(str(belief.id))

                other_ids = [
                    item.id
                    for item in scene_characters
                    if item.id != acting_character_id
                ]
                if other_ids:
                    names = {item.id: item.canonical_name for item in scene_characters}
                    relationships = await self._relationship_repo.get_for_character(
                        acting_character_id,
                        object_ids=other_ids,
                    )
                    if relationships:
                        actor_package += "Relationships to others in scene:\n"
                        for relationship in relationships:
                            object_id = UUID(str(relationship.object_id))
                            actor_package += (
                                f"- Toward {names.get(object_id, 'Someone')}: "
                                f"{relationship.relation_type} "
                                f"({relationship.description})\n"
                            )

                actor_tokens = count_tokens(actor_package)
                if current_budget_used + actor_tokens < max_budget:
                    packages.append(actor_package)
                    current_budget_used += actor_tokens
                    included_layers.append("layer_2_actor")

        facts = await self._fact_repo.list_active(
            campaign_id,
            visibility="public" if actor_mode else None,
        )
        if facts:
            fact_package = "[Campaign Facts & History]\n"
            for fact in facts:
                fact_package += (
                    f"- {fact.subject} {fact.predicate} {fact.object_value or ''}\n"
                )
                included_fact_ids.append(str(fact.id))
            fact_tokens = count_tokens(fact_package)
            if current_budget_used + fact_tokens < max_budget:
                packages.append(fact_package)
                current_budget_used += fact_tokens
                included_layers.append("layer_3_facts")

        if scene_characters:
            participant_package = (
                "[Other Present NPCs]\n" if actor_mode else "[Present Character Cards]\n"
            )
            for character in scene_characters:
                if actor_mode and character.id == acting_character_id:
                    continue
                included_character_ids.append(str(character.id))
                if actor_mode:
                    participant_package += (
                        f"- {character.canonical_name} (Status: {character.status})\n"
                    )
                    if character.description:
                        participant_package += (
                            f"  Public description: {character.description}\n"
                        )
                    if character.appearance:
                        participant_package += f"  Appearance: {character.appearance}\n"
                else:
                    participant_package += await self._full_character_profile(
                        character,
                        include_private=True,
                        included_item_ids=included_item_ids,
                    )
                    beliefs = await self._belief_repo.get_for_character(
                        character.id,
                        active_only=True,
                    )
                    if beliefs:
                        participant_package += "Private knowledge:\n"
                        for belief in beliefs:
                            participant_package += f"- {belief.proposition}\n"
                            included_belief_ids.append(str(belief.id))

            participant_tokens = count_tokens(participant_package)
            if current_budget_used + participant_tokens < max_budget:
                packages.append(participant_package)
                current_budget_used += participant_tokens
                included_layers.append("layer_4_character_cards")

        history_to_include: list[ChatMessage] = []
        if not actor_mode:
            history_records = await self._turn_repo.get_history(
                campaign_id,
                limit=30,
                active_only=True,
            )
            for turn in reversed(history_records):
                message_tokens = count_tokens(turn.content)
                if current_budget_used + message_tokens >= max_budget:
                    break
                current_budget_used += message_tokens
                history_to_include.insert(
                    0,
                    ChatMessage(role=turn.role, content=turn.content),
                )
                included_turn_ids.insert(0, str(turn.id))
            if history_to_include:
                included_layers.append("layer_5_history")

        system_content = system_msg.content
        for package in packages:
            system_content += f"\n\n{package}"
        final_messages = [ChatMessage(role="system", content=system_content)]
        final_messages.extend(history_to_include)

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
            "acting_character_id": (
                str(acting_character_id) if acting_character_id else None
            ),
            "scene_id": str(scene_id) if scene_id else None,
            "actor_scope_strict": actor_mode,
            "included_fact_ids": included_fact_ids,
            "included_belief_ids": list(dict.fromkeys(included_belief_ids)),
            "included_thesis_ids": included_thesis_ids,
            "included_turn_ids": included_turn_ids,
            "included_item_ids": list(dict.fromkeys(included_item_ids)),
            "included_character_ids": list(dict.fromkeys(included_character_ids)),
            "history_turns_count": len(history_to_include),
        }
        return final_messages, metadata
