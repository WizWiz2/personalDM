import asyncio
import json
import random
import re
import time
from uuid import uuid4, UUID
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.engine import get_session, Base, create_async_engine, async_sessionmaker
from app.services.campaign_service import CampaignService
from app.services.turn_runner import TurnRunner
from app.services.context_compiler import ContextCompiler
from app.services.memory_scribe import MemoryScribe
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.scene import SceneCreate
from app.models.entity import EntityCreate, EntityType
from app.models.character import CharacterCreate
from app.models.fact import FactCreate
from app.models.belief import BeliefCreate
from app.models.turn import TurnCreate, ChatMessage
from app.models.provider_config import ProviderConfigCreate
from app.models.proposed_change import ProposalAction, ChangeType
from app.providers.llm_provider import LLMProvider

# 20 Companions with unique lore, personalities and backstory secrets
COMPANIONS_DATA = [
    {
        "name": "Valerius",
        "description": "A solemn cleric of the Holy Light clad in silver-trimmed vestments.",
        "personality": "Pious, soft-spoken but harbors deep doubts. Speaks in blessings.",
        "secret": "The sacred temple chalice of restoration is filled with lethal, slow-acting nightshade poison."
    },
    {
        "name": "Sylvia",
        "description": "A slender elven mage wearing indigo robes embroidered with constellations.",
        "personality": "Arrogant, brilliant, and protective of her books. Speaks with ancient vocabulary.",
        "secret": "She stole the forbidden spellbook of black stars from the Grand Archmage of Loria."
    },
    {
        "name": "Garrick",
        "description": "A scarred rogue throwing silver daggers into the wooden benches.",
        "personality": "Sarcastic, hyper-vigilant, obsessed with gold. Slang-heavy speech.",
        "secret": "He is a secret spy sent by the outlaw Red Syndicate to lead the party into an ambush."
    },
    {
        "name": "Thorin",
        "description": "A battle-hardened Dwarf warrior wielding a soot-covered iron hammer.",
        "personality": "Gruff, loyal to his ancestors, fond of ale. Low guttural voice.",
        "secret": "He lost his clan's royal seal of ownership inside the dragon nests under the Mountain."
    },
    {
        "name": "Lyra",
        "description": "A traveling bard playing a dark spruce wood lute.",
        "personality": "Charismatic, always smiling, writes poems about current events.",
        "secret": "Every song she plays carries a subliminal charm spell designed to manipulate the listener's will."
    },
    {
        "name": "Kaelen",
        "description": "A quiet ranger dressed in worn leather and green leaf camouflage.",
        "personality": "Reclusive, smells of pine wood, talks to crows. Observant.",
        "secret": "He is hunting a cursed werewolf that is actually his own younger brother."
    },
    {
        "name": "Eldrin",
        "description": "An ancient scholar carrying a stack of yellowed parchment scrolls.",
        "personality": "Forgetful but possesses vast historical knowledge. Mumbles to himself.",
        "secret": "The great fire of the Imperial Library of Alexandria was ordered and lit by his own hand."
    },
    {
        "name": "Daphne",
        "description": "A druid with wild ivy leaves woven into her braided hair.",
        "personality": "Gentle, speaks in whispers, detests iron and metal.",
        "secret": "She carries a pitch-black corrupted seed that is slowly decaying the forest around her."
    },
    {
        "name": "Korgan",
        "description": "A massive mercenary covered in heavy iron plate armor.",
        "personality": " Ruthless, laughs at danger, values only hard gold.",
        "secret": "He was hired by the Dark Lord to assassinate the player's family line when the quest is done."
    },
    {
        "name": "Aria",
        "description": "A paladin of the Dawn Order with a golden shield.",
        "personality": "Righteous, strict follower of the Code of Valor. Loud commanding voice.",
        "secret": "Her legendary holy sword broke its vow and lost its divine blessing three moons ago."
    },
    {
        "name": "Zephyr",
        "description": "A silent assassin wearing a mask of grey silk.",
        "personality": "Cold, calculated, speaks only when absolutely necessary.",
        "secret": "He carries a royal poison dagger bearing the crest of the assassinated King."
    },
    {
        "name": "Morgana",
        "description": "A swamp witch smelling of damp earth and boiled toadstools.",
        "personality": "Cynical, speaks in riddles, loves to mock righteous paladins.",
        "secret": "She brews her healing potions with forbidden demonic essence extracted from imps."
    },
    {
        "name": "Brog",
        "description": "An Orc barbarian wearing trophies of monster teeth.",
        "personality": "Hot-tempered, values physical strength above all, easily offended.",
        "secret": "He is seeking blood vengeance for his fallen chieftain who was murdered by humans."
    },
    {
        "name": "Isabella",
        "description": "A noble duelist carrying a steel rapier with a golden hilt.",
        "personality": "Proud, elegant, easily offended by bad manners. Aristocratic dialect.",
        "secret": "She fled her home to escape an arranged political marriage with the corrupt Duke."
    },
    {
        "name": "Ignis",
        "description": "A fire sorcerer whose eyes glow like hot coals.",
        "personality": "Excitable, unstable, obsessed with burning things down.",
        "secret": "He accidentally burned down his entire home village during a magical tantrum."
    },
    {
        "name": "Seraphina",
        "description": "A young acolyte wearing plain white linen robes.",
        "personality": "Timid, prays constantly, flinches at loud noises.",
        "secret": "She hears whispers of the Void in her ears during her holy prayers."
    },
    {
        "name": "Gideon",
        "description": "An old sea captain with a wooden peg leg and a weathered face.",
        "personality": "Jovial, uses sailor slang, smells of rum and salt.",
        "secret": "He abandoned his sinking ship and entire crew during the Great Storm to save himself."
    },
    {
        "name": "Vesper",
        "description": "A shadow monk with tattooed forearms.",
        "personality": "Calm, moves like a shadow, speaks of balance between light and dark.",
        "secret": "His hidden monastery actually worships the forbidden god of the Black Sun."
    },
    {
        "name": "Rowan",
        "description": "An alchemist surrounded by glass flasks of colorful liquids.",
        "personality": "Nervous, constantly shaking, smells of sulfur and mercury.",
        "secret": "He is secretly hunting for the philosopher's stone to cure a terminal curse."
    },
    {
        "name": "Tariq",
        "description": "A desert scout wearing sand-colored robes and goggles.",
        "personality": "Quiet, possesses acute desert survival instincts. Speaks slowly.",
        "secret": "He knows that the secret oasis, the party's only hope for water, has completely dried up."
    }
]

async def generate_player_action(
    llm_provider: LLMProvider,
    config: ProviderConfigCreate,
    history_turns: list,
    companion_names: list[str]
) -> str:
    """Uses Gemma to act as Eldon, the Player, reacting to recent history."""
    history_str = ""
    # Format last 8 turns of history
    for t in history_turns:
        role_label = "DM (Narrator/NPC)" if t.role == "assistant" else "Eldon (Player)"
        history_str += f"{role_label}: {t.content}\n"

    system_prompt = f"""You are Eldon, a gritty, cynical human adventurer exploring the mysterious Obsidian Citadel.
You are accompanied by 20 companions: {', '.join(companion_names)}.
Your immediate goal is to find the ancient relic key hidden somewhere in this citadel.
Look at the recent history of the campaign and write your next action or dialogue response.

CRITICAL RULES:
1. You MUST react directly to what the Dungeon Master (DM) or the companions just said. Keep the conversation organic.
2. If you talk to a specific companion, you MUST start your response with: [/talk CompanionName] "Your dialogue..."
   Example: [/talk Sylvia] "Listen, Sylvia, I know you stole that archmage's book. Open it now!"
3. If you want to do a general action (movement, searching, speaking to all), use: [/talk narrator] "Your action description..."
   Example: [/talk narrator] "I draw my torch and inspect the heavy bronze door."
4. Do NOT write any markdown wrapping tags like ```json or ```. Write only the plain text action.
5. Keep your response short (1 to 3 sentences) and highly immersive.
"""
    messages = [
        ChatMessage(role="system", content=system_prompt),
        ChatMessage(role="user", content=f"Here is the recent history:\n{history_str}\n\nWhat is your next action Eldon?")
    ]
    
    response_text = ""
    async for token in llm_provider.generate_stream(messages, config):
        response_text += token
    return response_text.strip()

async def run_persistent_simulation():
    print("=== STARTING PERSISTENT INTELLECTUAL LLM-VS-LLM SIMULATION (SQLITE FILE DB) ===")
    
    # Connect to persistent SQLite file database
    engine = create_async_engine("sqlite+aiosqlite:///./data/campaign.db")
    SessionLocal = async_sessionmaker(engine, expire_on_commit=False)
    
    # Initialize the DB structure
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        
    async with SessionLocal() as session:
        campaign_service = CampaignService(session)
        turn_runner = TurnRunner(session)
        compiler = ContextCompiler(session)
        scribe = MemoryScribe(session)
        proposed_repo = ProposedChangeRepository(session)
        entity_repo = EntityRepository(session)
        belief_repo = BeliefRepository(session)
        scene_repo = SceneRepository(session)
        llm_provider = LLMProvider()
        
        # 1. Setup Campaign
        campaign = await campaign_service.create_campaign(CampaignCreate(
            name="Хроники Бездны: Сага о 20 Спутниках",
            description="Persistent campaign running 1000 turns of autonomous LLM-vs-LLM RPG session.",
            system_instructions="You are a high-quality, dark RPG Dungeon Master. Describe scenes in rich, literary, dark fantasy style. Roleplay NPCs truthfully according to their profiles.",
            narrative_style="Grimdark, descriptive, rich dialogue, focus on mystery and dread."
        ))
        campaign_id = campaign.id
        
        # Configure model settings for Gemma
        config = await campaign_service.configure_provider(campaign_id, ProviderConfigCreate(
            base_url="http://127.0.0.1:11434/v1",
            model_name="gemma4:e4b",
            context_window=8192
        ))
        
        # 2. Create 20 companions
        print("Creating 20 fantasy companions with secrets on disk...")
        npcs = []
        companion_names = []
        for companion in COMPANIONS_DATA:
            npc = await entity_repo.create_character(campaign_id, CharacterCreate(
                entity_type=EntityType.CHARACTER,
                canonical_name=companion["name"],
                description=companion["description"],
                personality=companion["personality"]
            ))
            npcs.append(npc)
            companion_names.append(companion["name"])
            
            # Record private belief
            await belief_repo.create(BeliefCreate(
                character_id=npc.id,
                proposition=f"{companion['name']} secret: {companion['secret']}",
                status="known",
                visibility="dm"
            ))
            
        # 3. Create Scene
        scene = await scene_repo.create(campaign_id, SceneCreate(
            title="The Obsidian Citadel Sanctuary",
            location_description="A massive dark hall made of polished black stone, lit by violet embers. 20 companions gather here, whispers echoing."
        ))
        
        # Add participants
        for npc in npcs:
            await scene_repo.add_participant(scene.id, npc.id)
            
        # Update campaign's current scene
        await campaign_service.update_campaign(campaign_id, CampaignUpdate(
            name="Хроники Бездны: Сага о 20 Спутниках",
            current_scene_id=scene.id
        ))
        await session.commit()
        
        # Log file path
        log_file_path = "./data/persistent_simulation_play.log"
        with open(log_file_path, "w", encoding="utf-8") as lf:
            lf.write("============================================================\n")
            lf.write("    CHRONICLES OF ABYSS: AUTONOMOUS PLAYER VS DM SIMULATION  \n")
            lf.write("============================================================\n\n")
            
        print(f"Log will be recorded in real-time to {log_file_path}")
        
        turns_count = 1000
        stats = {
            "proposals_generated": 0,
            "proposals_accepted": 0,
            "proposals_rejected": 0,
            "facts_applied": 0,
            "theses_applied": 0
        }
        
        start_time = time.time()
        
        for i in range(1, turns_count + 1):
            # Fetch history from database
            from app.db.repositories.turn_repo import TurnRepository
            turn_repo = TurnRepository(session)
            history_turns = await turn_repo.get_history(campaign_id, limit=8, active_only=True)
            
            # Determine player's next move
            if not history_turns:
                # First turn starter
                player_content = '[/talk Sylvia] Sylvia, search your bag for the stolen spellbook. We need a strong warding spell immediately.'
            else:
                print(f"[{i}/{turns_count}] Generating Player (Eldon) turn via Gemma...")
                try:
                    player_content = await generate_player_action(llm_provider, config, history_turns, companion_names)
                except Exception as e:
                    print(f"  [Warning] Player generator failed: {str(e)}. Using fallback.")
                    player_content = '[/talk narrator] I look around the obsidian halls.'
            
            # Parse target NPC from player's content
            acting_character_id = None
            target_name = "narrator"
            match = re.search(r'\[/talk\s+(\w+)\]', player_content)
            if match:
                target_name = match.group(1)
                for npc in npcs:
                    if npc.canonical_name.lower() == target_name.lower():
                        acting_character_id = npc.id
                        break
            
            print(f"[{i}/{turns_count}] Executing DM response with target: {target_name if acting_character_id else 'narrator'}...")
            
            # Run actual turn through Gemma
            turn_data = TurnCreate(
                role="user",
                content=player_content,
                scene_id=scene.id,
                acting_character_id=acting_character_id
            )
            
            assistant_content = ""
            async for token in turn_runner.run_turn_stream(campaign_id, turn_data):
                assistant_content += token
                
            await session.commit()
            
            # Get proposals already extracted and saved by TurnRunner
            assistant_history = await turn_repo.get_history(campaign_id, limit=1)
            assistant_turn = assistant_history[-1]
            
            proposals = await proposed_repo.get_for_turn(assistant_turn.id)
            stats["proposals_generated"] += len(proposals)
            
            # Record proposal status
            applied_proposals = []
            for p in proposals:
                # 80% accept, 20% reject
                if random.random() < 0.80:
                    await proposed_repo.resolve(p.id, ProposalAction(status="accepted"))
                    stats["proposals_accepted"] += 1
                    
                    # Apply
                    payload = p.payload
                    ctype = p.change_type
                    if ctype == ChangeType.FACT:
                        await FactRepository(session).create(campaign_id, FactCreate(
                            subject=payload.get("subject"),
                            predicate=payload.get("predicate"),
                            object_value=payload.get("object_value"),
                            visibility="dm"
                        ))
                        stats["facts_applied"] += 1
                        applied_proposals.append(f"FACT: {payload.get('subject')} {payload.get('predicate')} {payload.get('object_value')}")
                    elif ctype == ChangeType.SCENE_THESIS:
                        from app.models.scene_thesis import SceneThesisCreate, ThesisType
                        await scene_repo.create_thesis(scene.id, SceneThesisCreate(
                            thesis_type=ThesisType("canon"),
                            text=payload.get("text"),
                            visibility="dm"
                        ))
                        stats["theses_applied"] += 1
                        applied_proposals.append(f"THESIS: {payload.get('text')}")
                else:
                    await proposed_repo.resolve(p.id, ProposalAction(status="rejected"))
                    stats["proposals_rejected"] += 1
                    
            await session.commit()
            
            # Log turn to text file
            with open(log_file_path, "a", encoding="utf-8") as lf:
                lf.write("============================================================\n")
                lf.write(f"TURN {i} / {turns_count}\n")
                lf.write("============================================================\n")
                lf.write(f"PLAYER: {player_content}\n")
                lf.write("------------------------------------------------------------\n")
                lf.write(f"DM (Gemma4): {assistant_content.strip()}\n")
                lf.write("------------------------------------------------------------\n")
                if applied_proposals:
                    lf.write("ACCEPTED PROPOSALS INJECTED TO CANON:\n")
                    for ap in applied_proposals:
                        lf.write(f" - {ap}\n")
                else:
                    lf.write("NO NEW CANON CHANGES INJECTED\n")
                lf.write("\n")
                
            # Periodic console output
            if i % 10 == 0:
                elapsed = time.time() - start_time
                avg_speed = elapsed / i
                eta = (turns_count - i) * avg_speed
                print(f" -> Completed {i} turns. Average speed: {avg_speed:.2f}s/turn. ETA: {eta/60:.2f} min.")
                
        # Build final report
        end_time = time.time()
        elapsed = end_time - start_time
        
        db_turns = await turn_repo.get_history(campaign_id, limit=5000, active_only=False)
        total_facts = len(await FactRepository(session).list_active(campaign_id))
        total_theses = len(await scene_repo.list_theses_by_scene(scene.id, active_only=True))
        
        report = f"""# 📚 Отчет о Персистентной Литературной Симуляции (Gemma 4)
 
Кампания «Хроники Бездны: Сага о 20 Спутниках» успешно записана в постоянную базу данных SQLite на диске.
 
## 📈 Итоговые Метрики
 
*   **Имя Кампании в БД:** Хроники Бездны: Сага о 20 Спутниках
*   **Идентификатор Кампании (UUID):** {campaign_id}
*   **Всего ходов записано в таблицу `turns`:** {len(db_turns)}
*   **Количество NPC с уникальными секретами:** 20
*   **Сгенерировано предложений реальным Scribe (Gemma):** {stats["proposals_generated"]}
*   **Принято в канон:** {stats["proposals_accepted"]}
*   **Отклонено:** {stats["proposals_rejected"]}
*   **Итого активных фактов в каноне БД (`facts`):** {total_facts}
*   **Итого активных тезисов в сцене (`scene_theses`):** {total_theses}
*   **Время работы генерации:** {elapsed/60:.2f} минут (в среднем {elapsed/turns_count:.2f} сек на ход)
 
## 📁 Пути к Файлам на Диске
 
*   **Файл базы данных SQLite:** [campaign.db](file:///c:/work/personalDM/src/backend/data/campaign.db)
*   **Текстовый лог художественной игры:** [persistent_simulation_play.log](file:///c:/work/personalDM/src/backend/data/persistent_simulation_play.log)
 
Симуляция завершена. База данных содержит полноценный лог взаимодействия со всеми 20 спутниками.
"""
        with open("C:/Users/User/.gemini/antigravity/brain/4877c6e1-77e6-43a3-b645-d0d21fc3bdcf/analysis_results.md", "w", encoding="utf-8") as f:
            f.write(report)
            
        print("=== PERSISTENT SIMULATION COMPLETED ===")

if __name__ == "__main__":
    import sys
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(run_persistent_simulation())
