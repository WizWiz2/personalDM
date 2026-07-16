import asyncio
import os
import re
import time
from pathlib import Path
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

# name, description, personality, private secret
COMPANIONS = [
    (
        "Valerius",
        "A solemn cleric in silver-trimmed vestments.",
        "Pious, soft-spoken, privately doubtful.",
        "The restoration chalice contains slow-acting nightshade poison.",
    ),
    (
        "Sylvia",
        "A slender elven mage in indigo constellation robes.",
        "Arrogant, brilliant, protective of her books.",
        "She stole the forbidden spellbook of black stars.",
    ),
    (
        "Garrick",
        "A scarred rogue carrying silver daggers.",
        "Sarcastic, vigilant, obsessed with gold.",
        "He is a Red Syndicate spy leading the party toward an ambush.",
    ),
    (
        "Thorin",
        "A battle-hardened dwarf with a soot-covered hammer.",
        "Gruff, loyal to his ancestors, fond of ale.",
        "He lost his clan's royal seal in the dragon nests.",
    ),
    (
        "Lyra",
        "A travelling bard with a dark spruce lute.",
        "Charismatic, smiling, turns events into songs.",
        "Her songs carry subliminal charm magic.",
    ),
    (
        "Kaelen",
        "A quiet ranger in worn leather and leaf camouflage.",
        "Reclusive, observant, speaks to crows.",
        "The werewolf he hunts is his younger brother.",
    ),
    (
        "Eldrin",
        "An ancient scholar carrying yellowed scrolls.",
        "Forgetful, historically learned, constantly muttering.",
        "He personally burned the Imperial Library.",
    ),
    (
        "Daphne",
        "A druid with ivy woven into her braided hair.",
        "Gentle, whispering, detests worked iron.",
        "Her corrupted seed is poisoning the forest.",
    ),
    (
        "Korgan",
        "A massive mercenary in heavy iron plate.",
        "Ruthless, laughs at danger, values gold.",
        "The Dark Lord hired him to kill Eldon's family line.",
    ),
    (
        "Aria",
        "A Dawn Order paladin with a golden shield.",
        "Righteous, strict, loudly commanding.",
        "Her holy sword lost its blessing three moons ago.",
    ),
    (
        "Zephyr",
        "A silent assassin behind a grey silk mask.",
        "Cold, calculated, speaks only when necessary.",
        "He carries the poison dagger used to kill the King.",
    ),
    (
        "Morgana",
        "A swamp witch smelling of earth and toadstools.",
        "Cynical, riddling, mocks paladins.",
        "Her healing potions contain demonic essence.",
    ),
    (
        "Brog",
        "An orc barbarian wearing monster teeth.",
        "Hot-tempered, proud of physical strength.",
        "He seeks vengeance on humans for his murdered chieftain.",
    ),
    (
        "Isabella",
        "A noble duelist with a gold-hilted rapier.",
        "Proud, elegant, offended by poor manners.",
        "She fled an arranged marriage to a corrupt duke.",
    ),
    (
        "Ignis",
        "A fire sorcerer with coal-bright eyes.",
        "Excitable, unstable, fascinated by fire.",
        "He accidentally burned down his home village.",
    ),
    (
        "Seraphina",
        "A young acolyte in plain white robes.",
        "Timid, prayerful, flinches at loud sounds.",
        "She hears the Void whisper during prayer.",
    ),
    (
        "Gideon",
        "An old sea captain with a peg leg.",
        "Jovial, salty, speaks in sailor slang.",
        "He abandoned his crew during the Great Storm.",
    ),
    (
        "Vesper",
        "A shadow monk with tattooed forearms.",
        "Calm, balanced, economical in movement.",
        "His monastery worships the forbidden Black Sun.",
    ),
    (
        "Rowan",
        "A nervous alchemist surrounded by glass flasks.",
        "Shaking, analytical, smells of sulfur.",
        "He seeks the philosopher's stone to cure his fatal curse.",
    ),
    (
        "Tariq",
        "A desert scout in sand-coloured robes and goggles.",
        "Quiet, patient, exceptionally observant.",
        "The party's only known oasis has completely dried up.",
    ),
]


async def generate_player_action(
    provider: LLMProvider,
    config,
    history: list,
    companion_names: list[str],
) -> str:
    recent_history = "\n".join(
        f"{'DM' if turn.role == 'assistant' else 'Eldon'}: {turn.content}"
        for turn in history
    )
    prompt = f"""You are Eldon, a gritty human adventurer in the Obsidian Citadel.
Companions: {', '.join(companion_names)}.
Goal: find the ancient relic key.

Write only Eldon's next intention or attempted action.
Never declare success, discoveries, damage, opened doors, test results or other
outcomes. Eldon owns travelling clothes, a torch, rope, dagger and basic
lockpicks. He has no magic or advanced technology. Do not reveal companion
secrets unless Eldon learned them in play. Use one to three plain-text sentences.
Start dialogue with [/talk Name] and general actions with [/talk narrator].
"""
    messages = [
        ChatMessage(role="system", content=prompt),
        ChatMessage(
            role="user",
            content=f"Recent completed history:\n{recent_history}\n\nNext intention:",
        ),
    ]
    result = ""
    async for token in provider.generate_stream(messages, config):
        result += token
    return result.strip()


async def apply_valid_proposal(
    session,
    campaign_id: UUID,
    current_scene_id: UUID,
    proposal,
    entity_repo: EntityRepository,
    scene_repo: SceneRepository,
    proposal_repo: ProposedChangeRepository,
) -> str | None:
    """Apply one already-validated proposal and preserve its source turn."""
    if proposal.status == "invalid":
        await proposal_repo.resolve(
            proposal.id,
            ProposalAction(status="rejected"),
        )
        return None
    if proposal.status != "proposed":
        return None

    payload = proposal.payload
    change_type = ChangeType(proposal.change_type)
    source_turn_id = proposal.turn_id

    if change_type == ChangeType.FACT:
        await FactRepository(session).create(
            campaign_id,
            FactCreate(
                subject=payload["subject"],
                predicate=payload["predicate"],
                object_value=payload.get("object_value"),
                visibility=payload.get("visibility", "dm"),
                source_turn_id=source_turn_id,
            ),
        )
        description = (
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
            source_turn_id=source_turn_id,
        )
        description = f"THESIS[{thesis_type.value}]: {payload['text']}"

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
            source_turns=[source_turn_id],
        )
        description = f"EVENT: {payload['description']}"

    elif change_type == ChangeType.RELATIONSHIP:
        await RelationshipRepository(session).create(
            campaign_id,
            RelationshipCreate(
                subject_id=UUID(payload["subject_id"]),
                object_id=UUID(payload["object_id"]),
                relation_type=payload["relation_type"],
                description=payload["description"],
                reason=payload.get("reason"),
                source_turn_id=source_turn_id,
                provenance="extracted",
                visibility=payload.get("visibility", "dm"),
            ),
        )
        description = f"RELATIONSHIP: {payload['description']}"

    elif change_type == ChangeType.MOVEMENT:
        character_id = UUID(payload["character_id"])
        location_id = UUID(payload["location_id"])
        await entity_repo.update_character(
            character_id,
            CharacterUpdate(current_location_id=location_id),
        )
        description = f"MOVEMENT: {character_id} -> {location_id}"

    else:
        return None

    await proposal_repo.resolve(
        proposal.id,
        ProposalAction(status="accepted"),
    )
    return description


async def run_persistent_simulation() -> None:
    data_dir = Path("./data")
    data_dir.mkdir(parents=True, exist_ok=True)
    database_path = data_dir / "campaign.db"
    log_path = data_dir / "persistent_simulation_play.log"
    report_path = data_dir / "persistent_simulation_report.md"

    engine = create_async_engine(f"sqlite+aiosqlite:///{database_path}")
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session:
        campaign_service = CampaignService(session)
        entity_repo = EntityRepository(session)
        belief_repo = BeliefRepository(session)
        scene_repo = SceneRepository(session)
        turn_repo = TurnRepository(session)
        proposal_repo = ProposedChangeRepository(session)
        turn_runner = TurnRunner(session)
        provider = LLMProvider()

        campaign = await campaign_service.create_campaign(
            CampaignCreate(
                name="Хроники Бездны: проверка канона",
                description="Autonomous Canon Engine stress test.",
                system_instructions=(
                    "You are a compact dark-fantasy Dungeon Master. The player "
                    "describes attempts, never outcomes. Decide results yourself. "
                    "Do not grant unlisted items or abilities. Keep private NPC "
                    "knowledge isolated and advance the objective decisively."
                ),
                narrative_style=(
                    "Concrete sensory detail, restrained prose, decisive consequences."
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
                    "A human adventurer with a torch, rope, dagger and lockpicks."
                ),
                personality="Practical, suspicious, terse, cooperative when needed.",
            ),
        )

        npcs = []
        for name, description, personality, secret in COMPANIONS:
            npc = await entity_repo.create_character(
                campaign_id,
                CharacterCreate(
                    entity_type=EntityType.CHARACTER,
                    canonical_name=name,
                    description=description,
                    personality=personality,
                ),
            )
            npcs.append(npc)
            await belief_repo.create(
                BeliefCreate(
                    character_id=npc.id,
                    proposition=f"Private secret: {secret}",
                    status="known",
                    visibility="character_only",
                )
            )

        scene = await scene_repo.create(
            campaign_id,
            SceneCreate(
                title="Obsidian Citadel Sanctuary",
                location_description=(
                    "A vast hall of polished black stone lit by violet embers."
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

        log_path.write_text("CHRONICLES OF ABYSS: CANON STRESS TEST\n\n", encoding="utf-8")
        turns_count = int(os.getenv("PDM_SIM_TURNS", "1000"))
        stats = {
            "generation_failures": 0,
            "proposals_generated": 0,
            "proposals_accepted": 0,
            "proposals_rejected_invalid": 0,
            "apply_failures": 0,
        }
        started_at = time.time()
        companion_names = [npc.canonical_name for npc in npcs]

        for index in range(1, turns_count + 1):
            history = await turn_repo.get_history(
                campaign_id,
                limit=8,
                active_only=True,
            )
            if not history:
                player_content = (
                    "[/talk narrator] I light my torch and inspect the nearest "
                    "sealed doorway without touching it."
                )
            else:
                try:
                    player_content = await generate_player_action(
                        provider,
                        config,
                        history,
                        companion_names,
                    )
                except Exception as exc:
                    print(f"[{index}] Player generation failed: {exc}")
                    player_content = (
                        "[/talk narrator] I pause and observe the chamber for a safe path."
                    )

            target_name = "narrator"
            acting_character_id = None
            target_match = re.search(r"\[/talk\s+([\w-]+)\]", player_content)
            if target_match:
                target_name = target_match.group(1)
                acting_character_id = next(
                    (
                        npc.id
                        for npc in npcs
                        if npc.canonical_name.lower() == target_name.lower()
                    ),
                    None,
                )

            assistant_content = ""
            async for token in turn_runner.run_turn_stream(
                campaign_id,
                TurnCreate(
                    role="user",
                    content=player_content,
                    scene_id=scene.id,
                    acting_character_id=acting_character_id,
                ),
            ):
                assistant_content += token
            await session.commit()

            accepted = []
            rejected = []
            if assistant_content.startswith("[Generation failed"):
                stats["generation_failures"] += 1
            else:
                latest = await turn_repo.get_history(
                    campaign_id,
                    limit=1,
                    active_only=True,
                )
                if latest and latest[-1].role == "assistant":
                    proposals = await proposal_repo.get_for_turn(latest[-1].id)
                    stats["proposals_generated"] += len(proposals)
                    for proposal in proposals:
                        if proposal.status == "invalid":
                            stats["proposals_rejected_invalid"] += 1
                            rejected.append(
                                proposal.payload.get(
                                    "_validation_error",
                                    "invalid proposal",
                                )
                            )
                            await proposal_repo.resolve(
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
                                proposal_repo,
                            )
                            if summary:
                                accepted.append(summary)
                                stats["proposals_accepted"] += 1
                            await session.commit()
                        except Exception as exc:
                            await session.rollback()
                            stats["apply_failures"] += 1
                            rejected.append(f"apply failed: {exc}")

            with log_path.open("a", encoding="utf-8") as log_file:
                log_file.write("=" * 60 + "\n")
                log_file.write(f"TURN {index} / {turns_count}\n")
                log_file.write(f"TARGET: {target_name}\n")
                log_file.write(f"PLAYER: {player_content}\n")
                log_file.write("-" * 60 + "\n")
                log_file.write(f"DM: {assistant_content.strip()}\n")
                if accepted:
                    log_file.write("ACCEPTED CANON DELTAS:\n")
                    for item in accepted:
                        log_file.write(f" - {item}\n")
                if rejected:
                    log_file.write("REJECTED CANON DELTAS:\n")
                    for item in rejected:
                        log_file.write(f" - {item}\n")
                if not accepted and not rejected:
                    log_file.write("NO CANON DELTAS\n")
                log_file.write("\n")

            if index % 10 == 0:
                elapsed = time.time() - started_at
                print(
                    f"[{index}/{turns_count}] {elapsed / index:.2f}s/turn; "
                    f"generation failures={stats['generation_failures']}"
                )

        elapsed = time.time() - started_at
        all_turns = await turn_repo.get_history(
            campaign_id,
            limit=turns_count * 3,
            active_only=False,
        )
        total_facts = len(await FactRepository(session).list_active(campaign_id))
        total_theses = len(
            await scene_repo.list_theses_by_scene(scene.id, active_only=True)
        )
        report_path.write_text(
            "\n".join(
                [
                    "# Отчёт о canon stress test",
                    "",
                    f"- Кампания: {campaign.name}",
                    f"- Запрошено раундов: {turns_count}",
                    f"- Строк turns: {len(all_turns)}",
                    f"- Ошибок генерации: {stats['generation_failures']}",
                    f"- Предложений Scribe: {stats['proposals_generated']}",
                    f"- Принято: {stats['proposals_accepted']}",
                    f"- Отклонено невалидных: {stats['proposals_rejected_invalid']}",
                    f"- Ошибок применения: {stats['apply_failures']}",
                    f"- Активных фактов: {total_facts}",
                    f"- Активных тезисов: {total_theses}",
                    f"- Время: {elapsed / 60:.2f} минут",
                ]
            ),
            encoding="utf-8",
        )

        await session.commit()
        await session.execute(text("PRAGMA wal_checkpoint(TRUNCATE)"))

    await engine.dispose()
    print("=== PERSISTENT CANON STRESS TEST COMPLETED ===")


if __name__ == "__main__":
    if os.name == "nt":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_persistent_simulation())
