import asyncio
import sys
import json
from uuid import uuid4, UUID
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.db.engine import AsyncSessionLocal, Base, engine
from app.services.campaign_service import CampaignService
from app.services.turn_runner import TurnRunner
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.belief_repo import BeliefRepository
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.models.campaign import CampaignCreate
from app.models.scene import SceneCreate
from app.models.entity import EntityCreate, EntityType
from app.models.character import CharacterCreate, CharacterUpdate
from app.models.fact import FactCreate
from app.models.belief import BeliefCreate
from app.models.turn import TurnCreate
from app.models.provider_config import ProviderConfigCreate
from app.models.proposed_change import ProposalAction

def clear_screen():
    print("\n" * 3)

async def select_campaign_menu(campaign_service: CampaignService) -> UUID | None:
    while True:
        clear_screen()
        print("=== Campaign Manager ===")
        campaigns = await campaign_service.list_campaigns()
        
        if not campaigns:
            print("No campaigns found.")
        else:
            for idx, c in enumerate(campaigns):
                print(f"[{idx + 1}] {c.name} (ID: {c.id})")
        print("\n[N] Create New Campaign")
        print("[Q] Back to Main Menu")
        
        choice = input("\nSelect campaign: ").strip()
        if choice.lower() == 'q':
            return None
        if choice.lower() == 'n':
            name = input("Enter campaign name: ").strip()
            desc = input("Enter description: ").strip()
            sys_prompt = input("Enter DM System Instructions: ").strip()
            if not name:
                print("Name is required.")
                await asyncio.sleep(1.5)
                continue
            campaign = await campaign_service.create_campaign(CampaignCreate(
                name=name,
                description=desc,
                system_instructions=sys_prompt or "You are a Tabletop RPG Game Master."
            ))
            print(f"Campaign '{name}' created successfully!")
            await asyncio.sleep(1)
            return campaign.id
            
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(campaigns):
                return campaigns[idx].id
        
        print("Invalid choice.")
        await asyncio.sleep(1)

async def configure_llm_menu(campaign_id: UUID, campaign_service: CampaignService):
    clear_screen()
    print("=== Configure LLM Provider ===")
    config = await campaign_service.get_provider_config(campaign_id)
    if config:
        print(f"Current Config:")
        print(f" - Base URL: {config.base_url}")
        print(f" - Model Name: {config.model_name}")
        print(f" - Context Window: {config.context_window}")
    else:
        print("No LLM configuration found (using system defaults).")
        
    base_url = input("\nEnter Base URL (default: http://localhost:11434/v1): ").strip() or "http://localhost:11434/v1"
    model_name = input("Enter Model Name (default: gemma4:e4b): ").strip() or "gemma4:e4b"
    api_key = input("Enter API Key (press Enter if local/Ollama): ").strip() or None
    context_win_str = input("Enter Context Window size (default: 8192): ").strip() or "8192"
    context_window = int(context_win_str) if context_win_str.isdigit() else 8192
    
    await campaign_service.configure_provider(campaign_id, ProviderConfigCreate(
        base_url=base_url,
        model_name=model_name,
        api_key=api_key,
        context_window=context_window
    ))
    print("LLM Provider configured successfully!")
    await asyncio.sleep(1.5)

async def create_character_menu(campaign_id: UUID, session: AsyncSession):
    clear_screen()
    print("=== Create Character / NPC ===")
    name = input("Enter character name: ").strip()
    desc = input("Enter description: ").strip()
    personality = input("Enter personality traits: ").strip()
    secret = input("Enter character secrets (private knowledge): ").strip()
    
    if not name:
        print("Name is required.")
        await asyncio.sleep(1.5)
        return

    entity_repo = EntityRepository(session)
    char = await entity_repo.create_character(campaign_id, CharacterCreate(
        entity_type=EntityType.CHARACTER,
        canonical_name=name,
        description=desc,
        personality=personality
    ))
    
    if secret:
        # Create a private belief containing the secret
        belief_repo = BeliefRepository(session)
        await belief_repo.create(BeliefCreate(
            character_id=char.id,
            proposition=secret,
            status="known",
            visibility="dm"
        ))
        
    await session.commit()
    print(f"Character '{name}' created!")
    await asyncio.sleep(1.5)

async def create_scene_menu(campaign_id: UUID, session: AsyncSession, campaign_service: CampaignService):
    clear_screen()
    print("=== Create Scene ===")
    title = input("Enter scene title: ").strip()
    location_desc = input("Enter location description: ").strip()
    mood = input("Enter mood (e.g. tense, quiet): ").strip()
    
    if not title:
        print("Title is required.")
        await asyncio.sleep(1.5)
        return

    scene_repo = SceneRepository(session)
    scene = await scene_repo.create(campaign_id, SceneCreate(
        title=title,
        location_description=location_desc,
        mood=mood
    ))
    
    # Set as current campaign scene
    await campaign_service.update_campaign(campaign_id, CampaignCreate(
        name="",  # keep unchanged
        current_scene_id=scene.id
    ))
    await session.commit()
    print(f"Scene '{title}' created and set as active!")
    await asyncio.sleep(1.5)

async def manage_participants_menu(campaign_id: UUID, session: AsyncSession):
    campaign_repo = CampaignRepository(session)
    campaign = await campaign_repo.get_by_id(campaign_id)
    if not campaign or not campaign.current_scene_id:
        print("No active scene in campaign. Create a scene first.")
        await asyncio.sleep(2)
        return

    scene_repo = SceneRepository(session)
    entity_repo = EntityRepository(session)
    
    while True:
        clear_screen()
        scene = await scene_repo.get_by_id(campaign.current_scene_id)
        print(f"=== Scene Participants: {scene.title} ===")
        
        # Get characters in scene
        active_chars = await entity_repo.get_characters_in_scene(scene.id)
        print("\nCharacters currently in scene:")
        if not active_chars:
            print(" - None")
        else:
            for c in active_chars:
                print(f" - {c.canonical_name} (ID: {c.id})")
                
        # Get all characters in campaign
        all_chars = await entity_repo.list_by_campaign(campaign_id, entity_type="character")
        available_chars = [c for c in all_chars if c.id not in [ac.id for ac in active_chars]]
        
        print("\nAvailable characters to add:")
        if not available_chars:
            print(" - No other characters found in campaign.")
        else:
            for idx, c in enumerate(available_chars):
                print(f" [{idx + 1}] Add {c.canonical_name}")
                
        print("\n[R <name>] Remove character from scene")
        print("[Q] Go back")
        
        choice = input("\nChoose option: ").strip()
        if choice.lower() == 'q':
            break
        if choice.lower().startswith('r '):
            name_to_remove = choice[2:].strip().lower()
            to_remove = next((ac for ac in active_chars if ac.canonical_name.lower() == name_to_remove), None)
            if to_remove:
                await scene_repo.remove_participant(scene.id, to_remove.id)
                await session.commit()
                print(f"Removed {to_remove.canonical_name}")
            else:
                print("Character not found in scene.")
            await asyncio.sleep(1)
            continue
            
        if choice.isdigit():
            idx = int(choice) - 1
            if 0 <= idx < len(available_chars):
                await scene_repo.add_participant(scene.id, available_chars[idx].id)
                await session.commit()
                print(f"Added {available_chars[idx].canonical_name}")
                await asyncio.sleep(1)
                continue
                
        print("Invalid choice.")
        await asyncio.sleep(1)

async def play_game_loop(campaign_id: UUID, session: AsyncSession, campaign_service: CampaignService):
    campaign_repo = CampaignRepository(session)
    campaign = await campaign_repo.get_by_id(campaign_id)
    if not campaign or not campaign.current_scene_id:
        # Auto-create a default scene if missing
        scene_repo = SceneRepository(session)
        scene = await scene_repo.create(campaign_id, SceneCreate(
            title="Introduction Scene",
            location_description="A quiet tavern crossroad"
        ))
        campaign = await campaign_service.update_campaign(campaign_id, CampaignCreate(
            name=campaign.name,
            current_scene_id=scene.id
        ))
        await session.commit()

    scene_repo = SceneRepository(session)
    entity_repo = EntityRepository(session)
    proposed_repo = ProposedChangeRepository(session)
    turn_runner = TurnRunner(session)
    
    active_listener_id = None
    active_listener_name = "Narrator / DM"

    clear_screen()
    print("================================================================================")
    print(f"   STARTING ADVENTURE: {campaign.name}")
    print("================================================================================")
    print("Commands:")
    print("  /talk <Name>      - Start addressing a specific NPC (triggers private knowledge)")
    print("  /talk narrator    - Speak back to the general DM narrator")
    print("  /proposals        - View and resolve extracted changes from the last turn")
    print("  /facts            - Print all active facts in the campaign")
    print("  /undo             - Undo the last turn pair")
    print("  /exit             - Return to Campaign Menu")
    print("================================================================================")

    last_assistant_turn_id = None

    while True:
        scene = await scene_repo.get_by_id(campaign.current_scene_id)
        participants = await entity_repo.get_characters_in_scene(scene.id)
        
        print(f"\n[Scene: {scene.title}] | [Mood: {scene.mood or 'Quiet'}]")
        p_names = ", ".join([p.canonical_name for p in participants]) if participants else "None"
        print(f"[Present NPCs: {p_names}] | [Talking to: {active_listener_name}]")
        print("--------------------------------------------------------------------------------")
        
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
            
        if user_input.lower() == '/exit':
            break
            
        if user_input.lower() == '/undo':
            from app.db.repositories.turn_repo import TurnRepository
            await TurnRepository(session).undo_last_pair(campaign_id)
            await session.commit()
            print("[System] Last turn undone.")
            continue
            
        if user_input.lower() == '/facts':
            facts = await FactRepository(session).list_active(campaign_id)
            print("\n=== Active Campaign Facts ===")
            if not facts:
                print("No facts recorded in the canon yet.")
            for f in facts:
                print(f" - {f.subject} {f.predicate} {f.object_value or ''} (Visibility: {f.visibility})")
            continue

        if user_input.lower().startswith('/talk '):
            target = user_input[6:].strip().lower()
            if target in ["narrator", "none", "dm"]:
                active_listener_id = None
                active_listener_name = "Narrator / DM"
                print(f"[System] Focused on Narrator.")
            else:
                matched_char = next((p for p in participants if p.canonical_name.lower() == target), None)
                if not matched_char:
                    # Search entire campaign
                    all_chars = await entity_repo.list_by_campaign(campaign_id, entity_type="character")
                    matched_char = next((c for c in all_chars if c.canonical_name.lower() == target), None)
                    
                if matched_char:
                    active_listener_id = matched_char.id
                    active_listener_name = matched_char.canonical_name
                    print(f"[System] Focused on talking to {active_listener_name}.")
                else:
                    print(f"[System] Character '{target}' not found in campaign.")
            continue

        if user_input.lower() == '/proposals':
            if not last_assistant_turn_id:
                print("[System] No turns generated yet in this session.")
                continue
            proposals = await proposed_repo.get_for_turn(last_assistant_turn_id)
            if not proposals:
                print("[System] No proposed changes extracted for the last turn.")
            else:
                print("\n=== Proposed Changes (Assisted Canon) ===")
                for idx, p in enumerate(proposals):
                    warning = f" [WARNING: {p['payload']['_warning']}]" if "_warning" in p["payload"] else ""
                    print(f" [{idx + 1}] {p['change_type'].upper()}: {p['payload']}{warning} (Status: {p['status']})")
                
                action_idx = input("\nEnter proposal number to ACCEPT, or press Enter to skip: ").strip()
                if action_idx.isdigit():
                    idx = int(action_idx) - 1
                    if 0 <= idx < len(proposals):
                        p_to_resolve = proposals[idx]
                        # API Resolve logic
                        from app.api.memory import resolve_proposal
                        # Resolve directly
                        await proposed_repo.resolve(p_to_resolve["id"], ProposalAction(status="accepted"))
                        
                        # Apply change
                        payload = p_to_resolve["payload"]
                        ctype = p_to_resolve["change_type"]
                        if ctype == "fact":
                            await FactRepository(session).create(campaign_id, FactCreate(
                                subject=payload.get("subject"),
                                predicate=payload.get("predicate"),
                                object_value=payload.get("object_value"),
                                visibility=payload.get("visibility", "dm")
                            ))
                        elif ctype == "scene_thesis":
                            from app.models.scene_thesis import SceneThesisCreate, ThesisType
                            await SceneRepository(session).create_thesis(scene.id, SceneThesisCreate(
                                thesis_type=ThesisType(payload.get("thesis_type", "canon")),
                                text=payload.get("text"),
                                visibility=payload.get("visibility", "dm")
                            ))
                        elif ctype == "movement":
                            char_id = payload.get("character_id")
                            loc_id = payload.get("location_id")
                            if char_id:
                                await entity_repo.update_character(UUID(char_id), CharacterUpdate(
                                    current_location_id=UUID(loc_id) if loc_id else None
                                ))
                        await session.commit()
                        print(f"[System] Proposal {idx+1} accepted and applied to Campaign Canon!")
            continue

        # Execute turn pipeline
        turn_data = TurnCreate(
            role="user",
            content=user_input,
            scene_id=scene.id,
            acting_character_id=active_listener_id
        )

        print("\nDM: ", end="", flush=True)
        async for token in turn_runner.run_turn_stream(campaign_id, turn_data):
            print(token, end="", flush=True)
        print()

        # Update last assistant turn ID
        from app.db.repositories.turn_repo import TurnRepository
        history = await TurnRepository(session).get_history(campaign_id, limit=1)
        if history and history[-1].role == "assistant":
            last_assistant_turn_id = history[-1].id

async def main():
    # Initialize database
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        campaign_service = CampaignService(session)
        
        while True:
            clear_screen()
            print("========================================")
            print("   Welcome to Personal DM Truth Engine  ")
            print("========================================")
            print(" [1] Campaign Manager (Load/Create)")
            print(" [Q] Quit")
            
            choice = input("\nSelect option: ").strip()
            if choice.lower() == 'q':
                break
                
            if choice == '1':
                campaign_id = await select_campaign_menu(campaign_service)
                if not campaign_id:
                    continue
                    
                # Campaign Loaded Sub-Menu
                while True:
                    campaign = await campaign_service.get_campaign(campaign_id)
                    if not campaign:
                        break
                    clear_screen()
                    print(f"=== Campaign: {campaign.name} ===")
                    print(" [1] Start / Resume Game Session")
                    print(" [2] Create Character / NPC")
                    print(" [3] Create Scene / Location")
                    print(" [4] Manage Scene Participants")
                    print(" [5] Configure LLM Settings")
                    print(" [6] Delete Campaign")
                    print(" [Q] Back to Main Menu")
                    
                    sub_choice = input("\nSelect option: ").strip()
                    if sub_choice.lower() == 'q':
                        break
                    if sub_choice == '1':
                        await play_game_loop(campaign_id, session, campaign_service)
                    elif sub_choice == '2':
                        await create_character_menu(campaign_id, session)
                    elif sub_choice == '3':
                        await create_scene_menu(campaign_id, session, campaign_service)
                    elif sub_choice == '4':
                        await manage_participants_menu(campaign_id, session)
                    elif sub_choice == '5':
                        await configure_llm_menu(campaign_id, campaign_service)
                    elif sub_choice == '6':
                        confirm = input("Are you sure you want to delete this campaign? (yes/no): ").strip().lower()
                        if confirm == 'yes':
                            await campaign_service.delete_campaign(campaign_id)
                            await session.commit()
                            print("Campaign deleted.")
                            await asyncio.sleep(1.5)
                            break

if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
