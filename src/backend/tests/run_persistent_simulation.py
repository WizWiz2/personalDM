import asyncio
import os
import re
import time
from uuid import UUID

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.db.engine import Base
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.event_repo import EventRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.relationship_repo import RelationshipRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.belief import BeliefCreate
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate, CharacterUpdate
from app.models.entity import EntityType
from app.models.event import EventCreate
from app.models.fact import FactCreate
from app.models.proposed_change import ChangeType, ProposalAction
from app.models.provider_config import ProviderConfigCreate
from app.models.relationship import RelationshipCreate
from app.models.scene import SceneCreate
from app.models.scene_thesis import SceneThesisCreate, ThesisType
from app.models.turn import ChatMessage, TurnCreate
from app.providers.llm_provider import LLMProvider
from app.services.campaign_service import CampaignService
from app.services.turn_runner import TurnRunner

# Twenty companions with deliberately isolated private knowledge.
COMPANIONS_DATA = [
    {
        "name": "Valerius",
        "description": "A solemn cleric of the Holy Light clad in silver-trimmed vestments.",
        "personality": "Pious, soft-spoken but harbors deep doubts. Speaks in blessings.",
        "secret": "The sacred temple chalice of restoration is filled with lethal, slow-acting nightshade poison.",
    },
    {
        "name": "Sylvia",
        "description": "A slender elven mage wearing indigo robes embroidered with constellations.",
        "personality": "Arrogant, brilliant, and protective of her books. Speaks with ancient vocabulary.",
        "secret": "She stole the forbidden spellbook of black stars from the Grand Archmage of Loria.",
    },
    {
        "name": "Garrick",
        "description": "A scarred rogue throwing silver daggers into the wooden benches.",
        "personality": "Sarcastic, hyper-vigilant, obsessed with gold. Slang-heavy speech.",
        "secret": "He is a secret spy sent by the outlaw Red Syndicate to lead the party into an ambush.",
    },
    {
        "name": "Thorin",
        "description": "A battle-hardened Dwarf warrior wielding a soot-covered iron hammer.",
        "personality": "Gruff, loyal to his ancestors, fond of ale. Low guttural voice.",
        "secret": "He lost his clan's royal seal of ownership inside the dragon nests under the Mountain.",
    },
    {
        "name": "Lyra",
        "description": "A traveling bard playing a dark spruce wood lute.",
        "personality": "Charismatic, always smiling, writes poems about current events.",
        "secret": "Every song she plays carries a subliminal charm spell designed to manipulate the listener's will.",
    },
    {
        "name": "Kaelen",
        "description": "A quiet ranger dressed in worn leather and green leaf camouflage.",
        "personality": "Reclusive, smells of pine wood, talks to crows. Observant.",
        "secret": "He is hunting a cursed werewolf that is actually his own younger brother.",
    },
    {
        "name": "Eldrin",
        "description": "An ancient scholar carrying a stack of yellowed parchment scrolls.",
        "personality": "Forgetful but possesses vast historical knowledge. Mumbles to himself.",
        "secret": "The great fire of the Imperial Library of Alexandria was ordered and lit by his own hand.",
    },
    {
        "name": "Daphne",
        "description": "A druid with wild ivy leaves woven into her braided hair.",
        "personality": "Gentle, speaks in whispers, detests iron and metal.",
        "secret": "She carries a pitch-black corrupted seed that is slowly decaying the forest around her.",
    },
    {
        "name": "Korgan",
        "description": "A massive mercenary covered in heavy iron plate armor.",
        "personality": "Ruthless, laughs at danger, values only hard gold.",
        "secret": "He was hired by the Dark Lord to assassinate Eldon's family line when the quest is done.",
    },
    {
        "name": "Aria",
        "description": "A paladin of the Dawn Order with a golden shield.",
        "personality": "Righteous, strict follower of the Code of Valor. Loud commanding voice.",
        "secret": "Her legendary holy sword broke its vow and lost its divine blessing three moons ago.",
    },
    {
        "name": "Zephyr",
        "description": "A silent assassin wearing a mask of grey silk.",
        "personality": "Cold, calculated, speaks only when absolutely necessary.",
        "secret": "He carries a royal poison dagger bearing the crest of the assassinated King.",
    },
    {
        "name": "Morgana",
        "description": "A swamp witch smelling of damp earth and boiled toadstools.",
        "personality": "Cynical, speaks in riddles, loves to mock righteous paladins.",
        "secret": "She brews her healing potions with forbidden demonic essence extracted from imps.",
    },
    {
        "name": "Brog",
        "description": "An Orc barbarian wearing trophies of monster teeth.",
        "personality": "Hot-tempered, values physical strength above all, easily offended.",
        "secret": "He is seeking blood vengeance for his fallen chieftain who was murdered by humans.",
    },
    {
        "name": "Isabella",
        "description": "A noble duelist carrying a steel rapier with a golden hilt.",
        "personality": "Proud, elegant, easily offended by bad manners. Aristocratic dialect.",
        "secret": "She fled her home to escape an arranged political marriage with the corrupt Duke.",
    },
    {
        "name": "Ignis",
        "description": "A fire sorcerer whose eyes glow like hot coals.",
        "personality": "Excitable, unstable, obsessed with burning things down.",
        "secret": "He accidentally burned down his entire home village during a magical tantrum.",
    },
    {
        "name": "Seraphina",
        "description": "A young acolyte wearing plain white linen robes.",
        "personality": "Timid, prays constantly, flinches at loud noises.",
        "secret": "She hears whispers of the Void in her ears during her holy prayers.",
    },
    {
        "name": "Gideon",
        "description": "An old sea captain with a wooden peg leg and a weathered face.",
        "personality": "Jovial, uses sailor slang, smells of rum and salt.",
        "secret": "He abandoned his sinking ship and entire crew during the Great Storm to save himself.",
    },
    {
        "name": "Vesper",
        "description": "A shadow monk with tattooed forearms.",
        "personality": "Calm, moves like a shadow, speaks of balance between light and dark.",
        "secret": "His hidden monastery actually worships the forbidden god of the Black Sun.",
    },
    {
        "name": "Rowan",
        "description": "An alchemist surrounded by glass flasks of colorful liquids.",
        "personality": "Nervous, constantly shaking, smells of sulfur and mercury.",
        "secret": "He is secretly hunting for the philosopher's stone to cure a terminal curse.",
    },
    {
        "name": "Tariq",
        "description": "A desert scout wearing sand-colored robes and goggles.",
        "personality": "Quiet, possesses acute desert survival instincts. Speaks slowly.",
        "secret": "He knows that the secret oasis, the party's only hope for water, has completely dried up.",
    },
]


async def generate_player_action(
    llm_provider: LLMProvider,
    config: ProviderConfigCreate,
    history_turns: list,
    companion_names: list[str],
) -> str:
    """Generate only Eldon's intention, never the outcome of his action."""
    history_str = ""
    for turn in history_turns:
        role_label = "DM" if turn.role == "assistant" else "Eldon"
        history_str += f"{role_label}: {turn.content}\n"

    system_prompt = f"""You are Eldon, a gritty human adventurer in the Obsidian Citadel.
You travel with these companions: {', '.join(companion_names)}.
Your goal is to find the ancient relic key.

Write Eldon's next short action or line of dialogue.
Rules:
1. React directly to the latest completed DM response.
2. Describe only Eldon's intention or attempted action. Never declare success,
   discoveries, damage, opened doors, scanner results or other outcomes.
3. Eldon owns only ordinary travelling clothes, a torch, rope, a dagger and a
   basic set of lockpicks. Do not invent scanners, gauntlets, prisms or magic.
4. To address a companion, begin with [/talk Name].
5. For a general attempted action, begin with [/talk narrator].
6. Do not reveal a companion's secret unless it was explicitly learned in play.
7. Use plain text, one to three sentences, no markdown.
"""
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(
            role="user",
            content=f"Recent campaign history:\n{history_str}\nEldon's next intention:",
        ),
    ]

    response_text = ""
    async for token in llm_provider.generate_stream(messages, config):
        response_text += token
    return response_text.strip()


async def apply_valid_proposal(
    session,
    campaign_id: UUID,
    current_scene_id: UUID,
    proposal,
    entity_repo: EntityRepository,
    scene_repo: SceneRepository,
    proposed_repo: ProposedChangeRepository,
) -> str | None:
    """Apply a structurally valid benchmark proposal deterministically."""
    if proposal.status == "invalid":
        await proposed_repo.resolve(
            proposal.id,
            ProposalAction(status="rejected"),
        )
        return None
    if proposal.status != "proposed":
        return None

    payload = proposal.payload
    change_type = ChangeType(proposal.change_type)

    if change_type == ChangeType.FACT:
        await FactRepository(session).create(
            campaign_id,
            FactCreate(
                subject=payload["subject"],
                predicate=payload["predicate"],
                object_value=payload.get("object_value"),
                visibility=payload.get("visibility", "dm"),
            ),
        )
        summary = (
            f"FACT: {payload['subject']} {payload['predicate']} "
            f"{payload.get('object_value', '')}"
        )

    elif change_type == ChangeType.SCENE_THESIS:
        thesis_type = ThesisType(payload.get("thesis_type", "canon"))
        await scene_repo.create_thesis(
            UUID(payload.get("scene_id", str(current_scene_id))),
            SceneThesisCreate(
                thesis_type=thesis_type,
                text=payload["text"],
                visibility=payload.get("visibility", "dm"),
                priority=payload.get("priority", 0),
            ),
        )
        summary = f"THESIS[{thesis_type.value}]: {payload['text']}"

    elif change_type == ChangeType.EVENT:
        await EventRepository(session).create(
            campaign_id,
            EventCreate(
                event_type=payload.get("event_type", "general"),
                description=payload["description"],
                location_id=(
                    UUID(payload["location_id"])
                    if payload.get("location_id")
                    else None
                ),
                importance=payload.get("importance", "normal"),
                participant_ids=[
                    UUID(entity_id)
                    for entity_id in payload.get("participant_ids", [])
                ],
            ),
            source_turns=[proposal.turn_id],
        )
        summary = f"EVENT: {payload['description']}"

    elif change_type == ChangeType.RELATIONSHIP:
        await RelationshipRepository(session).create(
            campaign_id,
            RelationshipCreate(
                subject_id=UUID(payload["subject_id"]),
                object_id=UUID(payload["object_id"]),
                relation_type=payload["relation_type"],
                description=payload["description"],
                reason=payload.get("reason"),
                provenance="extracted",
                visibility=payload.get("visibility", "dm"),
            ),
        )
        summary = f"RELATIONSHIP: {payload['description']}"

    elif change_type == ChangeType.MOVEMENT:
        character_id = UUID(payload["character_id"])
        location_id = UUID(payload["location_id"])
        await entity_repo.update_character(
            character_id,
            CharacterUpdate(current_location_id=location_id),
        )
        summary = f"MOVEMENT: {character_id} -> {location_id}"

    else:
        return None

    await proposed_repo.resolve(
        proposal.id,
        ProposalAction(status="accepted"),
    )
    return summary


async def run_persistent_simulation():
    print("=== STARTING PERSISTENT CANON STRESS TEST ===")

    engine = create_async_engine("sqlite+aiosqlite:///./data/campaign.db")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        campaign_service = CampaignService(session)
        turn_runner = TurnRunner(session)
        proposed_repo = ProposedChangeRepository(session)
        entity_repo = EntityRepository(session)
        belief_repo = BeliefRepository(session)
        scene_repo = SceneRepository(session)
        llm_provider = LLMProvider()

        campaign = await campaign_service.create_campaign(
            CampaignCreate(
                name="Хроники Бездны: Сага о 20 Спутниках",
                description="Autonomous canon stress test.",
                system_instructions=(
                    "You are a dark-fantasy Dungeon Master. The player describes "
                    "attempts, never outcomes. Decide results yourself. Do not grant "
                    "unlisted equipment or abilities. Keep NPC private knowledge "
                    "isolated and advance the objective instead of repeating puzzles."
                ),
                narrative_style=(
                    "Compact dark fantasy prose, concrete sensory details, decisive outcomes."
                ),
            )
        )
        campaign_id = campaign.id

        config = await campaign_service.configure_provider(
            campaign_id,
            ProviderConfigCreate(
                base_url="http://127.0.0.1:11434/v1",
                model_name="gemma4:e4b",
                context_window=8192,
            ),
        )

        eldon = await entity_repo.create_character(
            campaign_id,
            CharacterCreate(
                entity_type=EntityType.CHARACTER,
                canonical_name="Eldon",
                description=(
                    "A cynical human adventurer carrying a torch, rope, dagger and "
                    "basic lockpicks. He has no magic or advanced technology."
                ),
                personality="Practical, suspicious, terse, but capable of cooperation.",
            ),
        )

        npcs = []
        companion_names = []
        for companion in COMPANIONS_DATA:
            npc = await entity_repo.create_character(
                campaign_id,
                CharacterCreate(
                    entity_type=EntityType.CHARACTER,
                    canonical_name=companion["name"],
                    description=companion["description"],
                    personality=companion["personality"],
                ),
            )
            npcs.append(npc)
            companion_names.append(companion["name"])
            await belief_repo.create(
                BeliefCreate(
                    character_id=npc.id,
                    proposition=f"Private secret: {companion['secret']}",
                    status="known",
                    visibility="character_only",
                )
            )

        scene = await scene_repo.create(
            campaign_id,
            SceneCreate(
                title="The Obsidian Citadel Sanctuary",
                location_description=(
                    "A massive hall of polished black stone lit by violet embers."
                ),
                mood="oppressive curiosity",
                tension="guarded",
            ),
        )
        await scene_repo.add_participant(scene.id, eldon.id)
        for npc in npcs:
            await scene_repo.add_participant(scene.id, npc.id)

        await campaign_service.update_campaign(
            campaign_id,
            CampaignUpdate(current_scene_id=scene.id),
        )
        await session.commit()

        log_file_path = "./data/persistent_simulation_play.log"
        report_file_path = "./data/persistent_simulation_report.md"
        os.makedirs("./data", exist_ok=True)
        with open(log_file_path, "w", encoding="utf-8") as log_file:
            log_file.write("CHRONICLES OF ABYSS: CANON STRESS TEST\n\n")

        turns_count = int(os.getenv("PDM_SIM_TURNS", "1000"))
        stats = {
            "generation_failures": 0,
            "proposals_generated": 0,
            "proposals_accepted": 0,
            "proposals_rejected_invalid": 0,
            "apply_failures": 0,
        }
        start_time = time.time()
        turn_repo = TurnRepository(session)

        for index in range(1, turns_count + 1):
            history_turns = await turn_repo.get_history(
                campaign_id,
                limit=8,
                active_only=True,
            )

            if not history_turns:
                player_content = (
                    "[/talk narrator] I light my torch and carefully inspect the "
                    "nearest sealed doorway without touching it."
                )
            else:
                try:
                    player_content = await generate_player_action(
                        llm_provider,
                        config,
                        history_turns,
                        companion_names,
                    )
                except Exception as exc:
                    print(f"[{index}] Player generation failed: {exc}")
                    player_content = (
                        "[/talk narrator] I pause and observe the chamber for a safe path."
                    )

            acting_character_id = None
            target_name = "narrator"
            match = re.search(r"\[/talk\s+([\w-]+)\]", player_content)
            if match:
                target_name = match.group(1)
                for npc in npcs:
                    if npc.canonical_name.lower() == target_name.lower():
                        acting_character_id = npc.id
                        break

            turn_data = TurnCreate(
                role="user",
                content=player_content,
                scene_id=scene.id,
                acting_character_id=acting_character_id,
            )

            assistant_content = ""
            async for token in turn_runner.run_turn_stream(campaign_id, turn_data):
                assistant_content += token
            await session.commit()

            applied_proposals = []
            rejected_proposals = []
            generation_failed = assistant_content.startswith("[Generation failed")
            if generation_failed:
                stats["generation_failures"] += 1
            else:
                latest_turns = await turn_repo.get_history(
                    campaign_id,
                    limit=1,
                    active_only=True,
                )
                if latest_turns and latest_turns[-1].role == "assistant":
                    assistant_turn = latest_turns[-1]
                    proposals = await proposed_repo.get_for_turn(assistant_turn.id)
                    stats["proposals_generated"] += len(proposals)

                    for proposal in proposals:
                        if proposal.status == "invalid":
                            stats["proposals_rejected_invalid"] += 1
                            rejected_proposals.append(
                                proposal.payload.get(
                                    "_validation_error",
                                    "invalid proposal",
                                )
                            )
                            await proposed_repo.resolve(
                                proposal.id,
                                ProposalAction(status="rejected"),
                            )
                            continue

                        try:
                            summary = await apply_valid_proposal(
                                session,
                                campaign_id,
                                scene.id,
                                proposal,
                                entity_repo,
                                scene_repo,
                                proposed_repo,
                            )
                            if summary:
                                applied_proposals.append(summary)
                                stats["proposals_accepted"] += 1
                            await session.commit()
                        except Exception as exc:
                            await session.rollback()
                            stats["apply_failures"] += 1
                            rejected_proposals.append(f"apply failed: {exc}")

            with open(log_file_path, "a", encoding="utf-8") as log_file:
                log_file.write("=" * 60 + "\n")
                log_file.write(f"TURN {index} / {turns_count}\n")
                log_file.write("=" * 60 + "\n")
                log_file.write(f"TARGET: {target_name}\n")
                log_file.write(f"PLAYER: {player_content}\n")
                log_file.write("-" * 60 + "\n")
                log_file.write(f"DM: {assistant_content.strip()}\n")
                log_file.write("-" * 60 + "\n")
                if applied_proposals:
                    log_file.write("ACCEPTED CANON DELTAS:\n")
                    for proposal_summary in applied_proposals:
                        log_file.write(f" - {proposal_summary}\n")
                if rejected_proposals:
                    log_file.write("REJECTED CANON DELTAS:\n")
                    for error in rejected_proposals:
                        log_file.write(f" - {error}\n")
                if not applied_proposals and not rejected_proposals:
                    log_file.write("NO CANON DELTAS\n")
                log_file.write("\n")

            if index % 10 == 0:
                elapsed = time.time() - start_time
                print(
                    f"[{index}/{turns_count}] "
                    f"{elapsed / index:.2f}s/turn, "
                    f"generation failures={stats['generation_failures']}"
                )

        await session.commit()
        elapsed = time.time() - start_time
        db_turns = await turn_repo.get_history(
            campaign_id,
            limit=turns_count * 3,
            active_only=False,
        )
        total_facts = len(await FactRepository(session).list_active(campaign_id))
        total_theses = len(
            await scene_repo.list_theses_by_scene(scene.id, active_only=True)
        )

        report = f"""# Отчёт о canon stress test

- Кампания: {campaign.name}
- Запрошено раундов: {turns_count}
- Строк turns: {len(db_turns)}
- Ошибок генерации: {stats['generation_failures']}
- Предложений Scribe: {stats['proposals_generated']}
- Принято валидных предложений: {stats['proposals_accepted']}
- Отклонено невалидных: {stats['proposals_rejected_invalid']}
- Ошибок применения: {stats['apply_failures']}
- Активных фактов: {total_facts}
- Активных тезисов: {total_theses}
- Время: {elapsed / 60:.2f} минут
"""
        with open(report_file_path, "w", encoding="utf-8") as report_file:
            report_file.write(report)

        # Flush WAL pages so copying campaign.db after the process exits produces
        # a complete audit artifact.
        await session.commit()
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))
        print("=== PERSISTENT CANON STRESS TEST COMPLETED ===")

    await engine.dispose()


if __name__ == "__main__":
    import sys

    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_persistent_simulation())
