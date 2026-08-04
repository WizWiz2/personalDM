import asyncio
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.engine import AsyncSessionLocal, Base, engine
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.job_repo import PostTurnJobRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.campaign_service import CampaignService
from app.services.post_turn_processor import PostTurnProcessor
from app.services.session_zero_interview import (
    SessionZeroInterviewIncompleteError,
    SessionZeroInterviewService,
)
from app.services.session_zero_service import SessionZeroService
from app.services.turn_runner import TurnRunner


def clear_screen() -> None:
    print("\n" * 3)


def _yes(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes", "д", "да"}


def _rate_limited(error: str | None) -> bool:
    text = (error or "").casefold()
    return "429" in text or "rate limit" in text or "rate_limit" in text


async def run_session_zero_interview(
    campaign_id: UUID,
    session: AsyncSession,
) -> bool:
    setup = await SessionZeroService(session).get(campaign_id)
    if setup.status == "completed":
        return True

    interview = SessionZeroInterviewService(session)
    state = await interview.get_state(campaign_id)

    clear_screen()
    print("=" * 80)
    print("   НУЛЕВАЯ СЕССИЯ")
    print("=" * 80)
    print(
        "Это обычный разговор с мастером, а не анкета. Можно отвечать свободно, "
        "исправлять сказанное и начинать с любой стороны будущей игры."
    )
    print("Команды: /summary — текущая сводка, /later — продолжить позже.\n")

    if state.pending_user_message:
        print("[Система] Последний ответ сохранён, но модель ещё не успела ответить.")
        try:
            decision = await interview.retry_pending(campaign_id)
        except LLMProviderError as exc:
            print(
                "[Система] Не удалось продолжить беседу через текущую модель. "
                "Ответ сохранён; настрой провайдера и вернись позже."
            )
            print(f"[Техническая причина] {exc}")
            return False
        if decision:
            print(f"DM: {decision.assistant_message}\n")
            state = await interview.get_state(campaign_id)
    elif state.messages:
        last_assistant = next(
            (
                item["content"]
                for item in reversed(state.messages)
                if item.get("role") == "assistant" and item.get("content")
            ),
            None,
        )
        if last_assistant:
            print(f"DM: {last_assistant}\n")
    else:
        print(f"DM: {interview.OPENING_MESSAGE}\n")

    while True:
        user_input = input("Ты: ").strip()
        if not user_input:
            continue
        command = user_input.casefold()
        if command == "/later":
            print("[Система] Беседа сохранена. Её можно продолжить из меню кампании.")
            return False
        if command == "/summary":
            state = await interview.get_state(campaign_id)
            print("\n" + interview.summary(state.draft) + "\n")
            continue

        try:
            decision = await interview.answer(campaign_id, user_input)
        except LLMProviderError as exc:
            print(
                "[Система] Модель сейчас недоступна или достигла лимита. "
                "Твой ответ уже сохранён; после настройки провайдера беседа продолжится "
                "с этого места."
            )
            print(f"[Техническая причина] {exc}")
            return False
        except ValueError as exc:
            print(f"[Система] {exc}")
            continue

        print(f"\nDM: {decision.assistant_message}\n")
        if not decision.ready_to_finalize:
            continue

        state = await interview.get_state(campaign_id)
        print("=" * 80)
        print("   ИТОГОВЫЕ ДОГОВОРЁННОСТИ")
        print("=" * 80)
        print(state.last_summary or interview.summary(state.draft))
        print("\nНачать кампанию с этими договорённостями? [Да/Нет]")
        if not _yes(input("Выбор: ")):
            print(
                "DM: Хорошо. Скажи свободно, что нужно изменить или уточнить, "
                "и мы продолжим разговор.\n"
            )
            continue
        try:
            completed = await interview.finalize(campaign_id)
        except SessionZeroInterviewIncompleteError as exc:
            print(
                "[Система] Мастер преждевременно посчитал беседу завершённой. "
                "Не хватает: " + ", ".join(exc.missing_fields)
            )
            continue
        print(
            f"[Система] Нулевая сессия завершена. Первая сцена: "
            f"{completed.scene.title}."
        )
        return True


async def select_campaign_menu(
    campaign_service: CampaignService,
    session: AsyncSession,
) -> UUID | None:
    while True:
        clear_screen()
        print("=== Campaign Manager ===")
        campaigns = await campaign_service.list_campaigns()
        if not campaigns:
            print("Кампаний пока нет.")
        else:
            setup_service = SessionZeroService(session)
            for index, campaign in enumerate(campaigns, start=1):
                setup = await setup_service.get(campaign.id)
                status = (
                    "готова к игре"
                    if setup.status == "completed"
                    else "нужна нулевая сессия"
                )
                print(f"[{index}] {campaign.name} — {status}")
        print("\n[N] Создать новую кампанию")
        print("[Q] Назад")

        choice = input("\nВыбор: ").strip()
        if choice.casefold() == "q":
            return None
        if choice.casefold() == "n":
            name = input("Как назовём кампанию? ").strip()
            if not name:
                print("Название обязательно.")
                await asyncio.sleep(1)
                continue
            campaign = await campaign_service.create_campaign(
                CampaignCreate(
                    name=name,
                    system_instructions=(
                        "Ты мастер настольной ролевой игры. Следуй подтверждённым "
                        "договорённостям нулевой сессии и не управляй персонажем игрока."
                    ),
                )
            )
            await session.commit()
            print(f"Кампания «{name}» создана.")
            await run_session_zero_interview(campaign.id, session)
            return campaign.id
        if choice.isdigit():
            index = int(choice) - 1
            if 0 <= index < len(campaigns):
                return campaigns[index].id
        print("Неверный выбор.")
        await asyncio.sleep(1)


async def configure_llm_menu(
    campaign_id: UUID,
    campaign_service: CampaignService,
    session: AsyncSession,
) -> None:
    clear_screen()
    print("=== Настройка LLM ===")
    config = await campaign_service.get_provider_config(campaign_id)
    if config:
        print(f"Сейчас: {config.model_name} @ {config.base_url}")
        print(f"Контекст: {config.context_window}")
    base_url = input(f"Base URL [{settings.LLM_BASE_URL}]: ").strip()
    model_name = input(f"Model [{settings.LLM_MODEL}]: ").strip()
    api_key = input("API key [Enter — оставить default]: ").strip()
    context_raw = input(f"Context window [{settings.LLM_CONTEXT_WINDOW}]: ").strip()
    await campaign_service.configure_provider(
        campaign_id,
        ProviderConfigCreate(
            base_url=base_url or settings.LLM_BASE_URL,
            model_name=model_name or settings.LLM_MODEL,
            api_key=api_key or settings.LLM_API_KEY,
            context_window=(
                int(context_raw)
                if context_raw.isdigit()
                else settings.LLM_CONTEXT_WINDOW
            ),
        ),
    )
    await session.commit()
    print("[Система] Провайдер сохранён.")
    await asyncio.sleep(1)


async def create_character_menu(
    campaign_id: UUID,
    session: AsyncSession,
) -> None:
    clear_screen()
    print("=== Создать NPC вручную ===")
    name = input("Имя: ").strip()
    if not name:
        print("Имя обязательно.")
        return
    description = input("Кто это: ").strip()
    personality = input("Характер: ").strip()
    await EntityRepository(session).create_character(
        campaign_id,
        CharacterCreate(
            canonical_name=name,
            description=description,
            personality=personality,
        ),
    )
    await session.commit()
    print(f"NPC «{name}» создан.")


async def create_scene_menu(
    campaign_id: UUID,
    session: AsyncSession,
    campaign_service: CampaignService,
) -> None:
    clear_screen()
    print("=== Создать локацию и сцену ===")
    location_name = input("Название места: ").strip()
    if not location_name:
        print("Название обязательно.")
        return
    description = input("Описание места: ").strip()
    mood = input("Настроение сцены: ").strip()
    location = await LocationRepository(session).create(
        campaign_id,
        LocationCreate(
            canonical_name=location_name,
            description=description,
            atmosphere=mood,
        ),
    )
    scene = await SceneRepository(session).create(
        campaign_id,
        SceneCreate(
            title=location_name,
            location_id=location.id,
            mood=mood,
        ),
    )
    await campaign_service.update_campaign(
        campaign_id,
        CampaignUpdate(current_scene_id=scene.id),
    )
    await session.commit()
    print(f"Сцена «{scene.title}» создана и активирована.")


async def manage_participants_menu(
    campaign_id: UUID,
    session: AsyncSession,
) -> None:
    campaigns = CampaignRepository(session)
    scenes = SceneRepository(session)
    entities = EntityRepository(session)
    campaign = await campaigns.get_by_id(campaign_id)
    if not campaign or not campaign.current_scene_id:
        print("Активной сцены нет.")
        return

    while True:
        campaign = await campaigns.get_by_id(campaign_id)
        scene = await scenes.get_by_id(campaign.current_scene_id)
        participants = await entities.get_characters_in_scene(scene.id)
        all_characters = await entities.list_by_campaign(
            campaign_id,
            entity_type="character",
        )
        participant_ids = {item.id for item in participants}
        available = [item for item in all_characters if item.id not in participant_ids]
        clear_screen()
        print(f"=== Участники: {scene.title} ===")
        print("Сейчас:")
        for item in participants:
            print(f" - {item.canonical_name}")
        if not participants:
            print(" - никого")
        print("\nМожно добавить:")
        for index, item in enumerate(available, start=1):
            print(f" [{index}] {item.canonical_name}")
        print("\n[R имя] удалить   [Q] назад")
        choice = input("Выбор: ").strip()
        if choice.casefold() == "q":
            return
        if choice.casefold().startswith("r "):
            name = choice[2:].strip().casefold()
            matched = next(
                (
                    item
                    for item in participants
                    if item.canonical_name.casefold() == name
                ),
                None,
            )
            if matched:
                await scenes.remove_participant(scene.id, matched.id)
                await session.commit()
            continue
        if choice.isdigit() and 0 <= int(choice) - 1 < len(available):
            await scenes.add_participant(scene.id, available[int(choice) - 1].id)
            await session.commit()


async def _show_post_turn_status(
    session: AsyncSession,
    assistant_turn_id: UUID,
) -> None:
    jobs = await PostTurnJobRepository(session).list_for_turn(assistant_turn_id)
    failed = [job for job in jobs if job.status == "failed"]
    if not failed:
        return
    if any(_rate_limited(job.error) for job in failed):
        print(
            "[Система] Ход сохранён, но облачная модель достигла лимита. "
            "Обновление памяти отложено; введи /retry-memory позже."
        )
    else:
        print(
            "[Система] Ход сохранён, но часть фоновой обработки памяти не завершилась. "
            "Введи /retry-memory для повтора."
        )


async def _retry_failed_memory(
    campaign_id: UUID,
    session: AsyncSession,
) -> None:
    jobs = await PostTurnJobRepository(session).list_for_campaign(
        campaign_id,
        limit=200,
    )
    failed = [job for job in jobs if job.status == "failed"]
    if not failed:
        print("[Система] Неудачных задач памяти нет.")
        return
    succeeded = 0
    remaining = 0
    processor = PostTurnProcessor(session)
    repo = PostTurnJobRepository(session)
    for job in reversed(failed):
        await repo.retry(job.id)
        await session.commit()
        try:
            await processor.process_job(job.id)
            succeeded += 1
        except Exception:
            remaining += 1
    print(
        f"[Система] Повторено успешно: {succeeded}; всё ещё ожидают: {remaining}."
    )


async def play_game_loop(
    campaign_id: UUID,
    session: AsyncSession,
    campaign_service: CampaignService,
) -> None:
    setup = await SessionZeroService(session).get(campaign_id)
    if setup.status != "completed":
        if not await run_session_zero_interview(campaign_id, session):
            return

    campaigns = CampaignRepository(session)
    scenes = SceneRepository(session)
    entities = EntityRepository(session)
    turns = TurnRepository(session)
    runner = TurnRunner(session)
    active_listener_id: UUID | None = None
    active_listener_name = "Narrator / DM"
    last_assistant_turn_id: UUID | None = None

    clear_screen()
    campaign = await campaigns.get_by_id(campaign_id)
    print("=" * 80)
    print(f"   STARTING ADVENTURE: {campaign.name}")
    print("=" * 80)
    print("Commands:")
    print("  /DM <вопрос>      - спросить мастера вне сцены")
    print("  /talk <Name>      - обратиться к присутствующему NPC")
    print("  /talk narrator    - вернуться к общему Narrator")
    print("  /facts            - показать активные факты")
    print("  /retry-memory     - повторить фоновые задачи")
    print("  /undo             - отменить последний игровой ход")
    print("  /exit             - вернуться в меню")
    print("=" * 80)

    while True:
        campaign = await campaigns.get_by_id(campaign_id)
        if not campaign or not campaign.current_scene_id:
            print("[Система] Активная сцена отсутствует.")
            return
        scene = await scenes.get_by_id(campaign.current_scene_id)
        participants = await entities.get_characters_in_scene(scene.id)
        npcs = [
            item
            for item in participants
            if item.id != campaign.player_character_id
        ]
        if active_listener_id and all(
            item.id != active_listener_id for item in npcs
        ):
            active_listener_id = None
            active_listener_name = "Narrator / DM"

        print(f"\n[Scene: {scene.title}] | [Mood: {scene.mood or '—'}]")
        names = ", ".join(item.canonical_name for item in npcs) or "None"
        print(f"[Present NPCs: {names}] | [Talking to: {active_listener_name}]")
        print("-" * 80)
        user_input = input("\nYou: ").strip()
        if not user_input:
            continue
        command = user_input.casefold()
        if command == "/exit":
            return
        if command == "/undo":
            await turns.undo_last_pair(campaign_id)
            await session.commit()
            print("[Система] Последняя игровая пара отменена.")
            continue
        if command == "/facts":
            facts = await FactRepository(session).list_active(campaign_id)
            if not facts:
                print("[Система] Фактов пока нет.")
            for fact in facts:
                print(
                    f" - [{fact.memory_kind}] {fact.subject} "
                    f"{fact.predicate} {fact.object_value or ''}"
                )
            continue
        if command == "/retry-memory":
            await _retry_failed_memory(campaign_id, session)
            continue
        if command.startswith("/talk "):
            target = user_input[6:].strip().casefold()
            if target in {"narrator", "none", "dm"}:
                active_listener_id = None
                active_listener_name = "Narrator / DM"
            else:
                matched = next(
                    (
                        item
                        for item in npcs
                        if item.canonical_name.casefold() == target
                    ),
                    None,
                )
                if matched:
                    active_listener_id = matched.id
                    active_listener_name = matched.canonical_name
                else:
                    print("[Система] Этого персонажа нет в текущей сцене.")
            continue

        print("\nDM: ", end="", flush=True)
        async for token in runner.run_turn_stream(
            campaign_id,
            TurnCreate(
                role="user",
                content=user_input,
                scene_id=scene.id,
                acting_character_id=active_listener_id,
            ),
        ):
            print(token, end="", flush=True)
        print()

        history = await turns.get_history(campaign_id, limit=20, channel="all")
        assistants = [item for item in history if item.role == "assistant"]
        if assistants:
            last_assistant_turn_id = assistants[-1].id
            await _show_post_turn_status(session, last_assistant_turn_id)


async def main() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        campaign_service = CampaignService(session)
        while True:
            clear_screen()
            print("=" * 40)
            print("   Welcome to Personal DM Truth Engine")
            print("=" * 40)
            print(" [1] Campaign Manager (Load/Create)")
            print(" [Q] Quit")
            choice = input("\nSelect option: ").strip()
            if choice.casefold() == "q":
                return
            if choice != "1":
                continue
            campaign_id = await select_campaign_menu(campaign_service, session)
            if not campaign_id:
                continue

            while True:
                campaign = await campaign_service.get_campaign(campaign_id)
                if not campaign:
                    break
                setup = await SessionZeroService(session).get(campaign_id)
                clear_screen()
                print(f"=== Campaign: {campaign.name} ===")
                if setup.status == "completed":
                    print(f" Session Zero: completed — {setup.player_character_name}")
                    print(" [1] Start / Resume Game Session")
                else:
                    print(" Session Zero: incomplete")
                    print(" [1] Continue Session Zero")
                print(" [2] Create Character / NPC manually")
                print(" [3] Create Scene / Location manually")
                print(" [4] Manage Scene Participants")
                print(" [5] Configure LLM Settings")
                print(" [6] Delete Campaign")
                print(" [Q] Back to Main Menu")
                selected = input("\nSelect option: ").strip()
                if selected.casefold() == "q":
                    break
                if selected == "1":
                    if setup.status == "completed":
                        await play_game_loop(
                            campaign_id,
                            session,
                            campaign_service,
                        )
                    else:
                        await run_session_zero_interview(campaign_id, session)
                elif selected == "2":
                    await create_character_menu(campaign_id, session)
                elif selected == "3":
                    await create_scene_menu(
                        campaign_id,
                        session,
                        campaign_service,
                    )
                elif selected == "4":
                    await manage_participants_menu(campaign_id, session)
                elif selected == "5":
                    await configure_llm_menu(
                        campaign_id,
                        campaign_service,
                        session,
                    )
                elif selected == "6":
                    if _yes(input("Удалить кампанию безвозвратно? (да/нет): ")):
                        await campaign_service.delete_campaign(campaign_id)
                        await session.commit()
                        print("Кампания удалена.")
                        break


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
