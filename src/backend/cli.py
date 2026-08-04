import asyncio
import contextlib
import io
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
from app.db.repositories.proposed_change_repo import ProposedChangeRepository
from app.db.repositories.scene_repo import SceneRepository
from app.db.repositories.turn_repo import TurnRepository
from app.models.campaign import CampaignCreate, CampaignUpdate
from app.models.character import CharacterCreate, CharacterUpdate
from app.models.fact import FactCreate
from app.models.location import LocationCreate
from app.models.proposed_change import ProposalAction
from app.models.provider_config import ProviderConfigCreate
from app.models.scene import SceneCreate
from app.models.turn import TurnCreate
from app.services.campaign_service import CampaignService
from app.services.conversational_session_zero import (
    ConversationalSessionZeroService,
)
from app.services.post_turn_processor import PostTurnProcessor
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
    setup_service = SessionZeroService(session)
    setup = await setup_service.get(campaign_id)
    if setup.status == "completed":
        return True

    interview = ConversationalSessionZeroService(session)
    clear_screen()
    print("=" * 80)
    print("   НУЛЕВАЯ СЕССИЯ — ЗНАКОМСТВО С ТВОЕЙ ИГРОЙ")
    print("=" * 80)
    print(
        "Я не буду просить заполнять техническую анкету. Отвечай свободно: "
        "мы последовательно определим мир, желаемый опыт, границы и героя."
    )
    print("Ответы сохраняются после каждого вопроса. /later — продолжить позже, ? — подсказка.\n")

    answers = await interview.get_answers(campaign_id)
    for question in interview.QUESTIONS:
        if question.key in answers:
            continue
        while True:
            print(f"DM: {question.prompt}")
            answer = input("Ты: ").strip()
            if answer.casefold() == "/later":
                print("[Система] Ответы сохранены. Нулевую сессию можно продолжить из меню кампании.")
                return False
            if answer == "?":
                print(f"[Подсказка] {question.hint}\n")
                continue
            if not answer and not question.allow_empty:
                print("[Система] Здесь нужен хотя бы короткий ответ. Можно ввести ? для примера.\n")
                continue
            if not answer:
                answer = "нет"
            answers = await interview.save_answer(campaign_id, question.key, answer)
            print()
            break

    while True:
        clear_screen()
        print("=" * 80)
        print("   ЧТО Я ПОНЯЛ О ТВОЕЙ ИГРЕ")
        print("=" * 80)
        print(interview.summary(answers))
        print("\n[Enter/Да] Начать игру   [E] Изменить ответ   [L] Продолжить позже")
        choice = input("Выбор: ").strip()
        if not choice or _yes(choice):
            try:
                completed = await interview.finalize(campaign_id)
            except ValueError as exc:
                print(f"[Система] Нулевая сессия пока не готова: {exc}")
                await asyncio.sleep(2)
                return False
            print(
                f"[Система] Нулевая сессия завершена. Первая сцена: "
                f"{completed.scene.title}."
            )
            await asyncio.sleep(1.5)
            return True
        if choice.casefold() in {"l", "later", "п", "позже"}:
            print("[Система] Ответы сохранены.")
            return False
        if choice.casefold() not in {"e", "edit", "и", "изменить"}:
            continue

        for index, question in enumerate(interview.QUESTIONS, start=1):
            current = answers.get(question.key, "—")
            print(f"[{index}] {question.prompt}\n    Сейчас: {current}")
        selected = input("Номер ответа для изменения: ").strip()
        if not selected.isdigit():
            continue
        index = int(selected) - 1
        if not 0 <= index < len(interview.QUESTIONS):
            continue
        question = interview.QUESTIONS[index]
        print(f"DM: {question.prompt}")
        print(f"[Подсказка] {question.hint}")
        answer = input("Ты: ").strip()
        if answer or question.allow_empty:
            answers = await interview.save_answer(
                campaign_id,
                question.key,
                answer or "нет",
            )


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
                status = "готова к игре" if setup.status == "completed" else "нужна нулевая сессия"
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
                await asyncio.sleep(1.5)
                continue
            campaign = await campaign_service.create_campaign(
                CampaignCreate(
                    name=name,
                    system_instructions=(
                        "Ты мастер настольной ролевой игры. Следуй договорённостям "
                        "нулевой сессии и не управляй персонажем игрока."
                    ),
                )
            )
            await session.commit()
            print(f"Кампания «{name}» создана. Теперь познакомимся с желаемой игрой.\n")
            await asyncio.sleep(0.5)
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
) -> None:
    clear_screen()
    print("=== Настройка LLM ===")
    config = await campaign_service.get_provider_config(campaign_id)
    if config:
        print(f"Сейчас: {config.model_name} @ {config.base_url}")
        print(f"Контекст: {config.context_window}")
    print(
        "\nВажно: Narrator и служебные роли могут расходовать общий лимит провайдера. "
        "Для отдельной дешёвой control-model можно задать CONTROL_LLM_MODEL и "
        "CONTROL_LLM_BASE_URL в .env."
    )
    base_url = input(f"Base URL [{settings.LLM_BASE_URL}]: ").strip() or settings.LLM_BASE_URL
    model_name = input(f"Model [{settings.LLM_MODEL}]: ").strip() or settings.LLM_MODEL
    api_key = input("API key [Enter — оставить default]: ").strip() or settings.LLM_API_KEY
    context_raw = input(f"Context window [{settings.LLM_CONTEXT_WINDOW}]: ").strip()
    context_window = int(context_raw) if context_raw.isdigit() else settings.LLM_CONTEXT_WINDOW
    await campaign_service.configure_provider(
        campaign_id,
        ProviderConfigCreate(
            base_url=base_url,
            model_name=model_name,
            api_key=api_key,
            context_window=context_window,
        ),
    )
    await campaign_service._session.commit()
    print("[Система] Провайдер сохранён.")
    await asyncio.sleep(1.5)


async def create_character_menu(campaign_id: UUID, session: AsyncSession) -> None:
    clear_screen()
    print("=== Создать NPC вручную ===")
    name = input("Имя: ").strip()
    if not name:
        print("Имя обязательно.")
        await asyncio.sleep(1.5)
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
    await asyncio.sleep(1.5)


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
        await asyncio.sleep(1.5)
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
    await asyncio.sleep(1.5)


async def manage_participants_menu(campaign_id: UUID, session: AsyncSession) -> None:
    campaign = await CampaignRepository(session).get_by_id(campaign_id)
    if not campaign or not campaign.current_scene_id:
        print("Активной сцены нет.")
        await asyncio.sleep(1.5)
        return
    scenes = SceneRepository(session)
    entities = EntityRepository(session)
    while True:
        campaign = await CampaignRepository(session).get_by_id(campaign_id)
        scene = await scenes.get_by_id(campaign.current_scene_id)
        participants = await entities.get_characters_in_scene(scene.id)
        all_characters = await entities.list_by_campaign(campaign_id, entity_type="character")
        available = [item for item in all_characters if item.id not in {p.id for p in participants}]
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
            matched = next((item for item in participants if item.canonical_name.casefold() == name), None)
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
            "Подробность сохранена в задании; введи /retry-memory для повтора."
        )


async def _retry_failed_memory(campaign_id: UUID, session: AsyncSession) -> None:
    jobs = await PostTurnJobRepository(session).list_for_campaign(campaign_id, limit=200)
    failed = [job for job in jobs if job.status == "failed"]
    if not failed:
        print("[Система] Неудачных задач памяти нет.")
        return
    succeeded = 0
    still_failed = 0
    processor = PostTurnProcessor(session)
    for job in reversed(failed):
        await PostTurnJobRepository(session).retry(job.id)
        await session.commit()
        try:
            with contextlib.redirect_stderr(io.StringIO()):
                await processor.process_job(job.id)
            succeeded += 1
        except Exception:
            still_failed += 1
    if still_failed:
        print(
            f"[Система] Повторено успешно: {succeeded}; всё ещё ожидают провайдера: "
            f"{still_failed}. Стектрейс скрыт, детали сохранены в job audit."
        )
    else:
        print(f"[Система] Все задачи памяти успешно повторены: {succeeded}.")


async def _show_proposals(
    campaign_id: UUID,
    scene_id: UUID,
    assistant_turn_id: UUID | None,
    session: AsyncSession,
) -> None:
    if not assistant_turn_id:
        print("[Система] В этой сессии ещё нет ответа ДМа.")
        return
    repo = ProposedChangeRepository(session)
    proposals = await repo.get_for_turn(assistant_turn_id)
    if not proposals:
        print("[Система] Предложений изменений для последнего хода нет.")
        return
    print("\n=== Assisted Canon ===")
    for index, proposal in enumerate(proposals, start=1):
        print(
            f"[{index}] {proposal['change_type'].upper()}: {proposal['payload']} "
            f"({proposal['status']})"
        )
    choice = input("Номер для принятия или Enter: ").strip()
    if not choice.isdigit() or not 0 <= int(choice) - 1 < len(proposals):
        return
    proposal = proposals[int(choice) - 1]
    await repo.resolve(proposal["id"], ProposalAction(status="accepted"))
    payload = proposal["payload"]
    if proposal["change_type"] == "fact":
        await FactRepository(session).create(
            campaign_id,
            FactCreate(
                subject=payload.get("subject"),
                predicate=payload.get("predicate"),
                object_value=payload.get("object_value"),
                visibility=payload.get("visibility", "dm"),
                scope=payload.get("scope", "campaign"),
                scene_id=scene_id if payload.get("scope") == "scene" else None,
                memory_kind=payload.get("memory_kind", "world_canon"),
                subject_entity_id=payload.get("subject_entity_id"),
            ),
        )
    elif proposal["change_type"] == "movement":
        character_id = payload.get("character_id")
        location_id = payload.get("location_id")
        if character_id:
            await EntityRepository(session).update_character(
                UUID(character_id),
                CharacterUpdate(
                    current_location_id=UUID(location_id) if location_id else None
                ),
            )
    await session.commit()
    print("[Система] Изменение принято.")


async def play_game_loop(
    campaign_id: UUID,
    session: AsyncSession,
    campaign_service: CampaignService,
) -> None:
    setup = await SessionZeroService(session).get(campaign_id)
    if setup.status != "completed":
        completed = await run_session_zero_interview(campaign_id, session)
        if not completed:
            return

    campaign_repo = CampaignRepository(session)
    scene_repo = SceneRepository(session)
    entity_repo = EntityRepository(session)
    turn_repo = TurnRepository(session)
    turn_runner = TurnRunner(session)
    active_listener_id: UUID | None = None
    active_listener_name = "Narrator / DM"
    last_assistant_turn_id: UUID | None = None

    campaign = await campaign_repo.get_by_id(campaign_id)
    if not campaign or not campaign.current_scene_id:
        print("[Система] Нулевая сессия завершена некорректно: нет активной сцены.")
        return

    clear_screen()
    print("=" * 80)
    print(f"   STARTING ADVENTURE: {campaign.name}")
    print("=" * 80)
    print("Commands:")
    print("  /DM <вопрос>      - спросить мастера вне сцены, без изменения канона")
    print("  /talk <Name>      - обратиться к конкретному присутствующему NPC")
    print("  /talk narrator    - вернуться к общему Narrator")
    print("  /proposals        - просмотреть Assisted Canon последнего хода")
    print("  /facts            - показать активные факты")
    print("  /retry-memory     - повторить фоновые задачи после лимита провайдера")
    print("  /undo             - отменить последнюю игровую пару ходов")
    print("  /exit             - вернуться в меню")
    print("=" * 80)

    while True:
        campaign = await campaign_repo.get_by_id(campaign_id)
        if not campaign or not campaign.current_scene_id:
            print("[Система] Активная сцена потеряна.")
            return
        scene = await scene_repo.get_by_id(campaign.current_scene_id)
        participants = await entity_repo.get_characters_in_scene(scene.id)
        npcs = [
            participant
            for participant in participants
            if str(participant.id) != str(campaign.player_character_id)
        ]
        if active_listener_id and all(item.id != active_listener_id for item in participants):
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
            await turn_repo.undo_last_pair(campaign_id)
            await session.commit()
            print("[Система] Последняя игровая пара отменена.")
            continue
        if command == "/facts":
            facts = await FactRepository(session).list_active(campaign_id)
            print("\n=== Active Campaign Facts ===")
            for fact in facts:
                print(f" - [{fact.memory_kind}] {fact.subject} {fact.predicate} {fact.object_value or ''}")
            if not facts:
                print("Фактов пока нет.")
            continue
        if command == "/retry-memory":
            await _retry_failed_memory(campaign_id, session)
            continue
        if command == "/proposals":
            await _show_proposals(
                campaign_id,
                scene.id,
                last_assistant_turn_id,
                session,
            )
            continue
        if command.startswith("/talk "):
            target = user_input[6:].strip().casefold()
            if target in {"narrator", "none", "dm"}:
                active_listener_id = None
                active_listener_name = "Narrator / DM"
            else:
                matched = next(
                    (item for item in npcs if item.canonical_name.casefold() == target),
                    None,
                )
                if matched:
                    active_listener_id = matched.id
                    active_listener_name = matched.canonical_name
                else:
                    print("[Система] Этого персонажа физически нет в текущей сцене.")
            continue

        turn = TurnCreate(
            role="user",
            content=user_input,
            scene_id=scene.id,
            acting_character_id=active_listener_id,
        )
        print("\nDM: ", end="", flush=True)
        # The durable job table keeps the full technical error. A cloud control
        # model's 429 must not dump a Python traceback into the roleplaying text.
        with contextlib.redirect_stderr(io.StringIO()):
            async for token in turn_runner.run_turn_stream(campaign_id, turn):
                print(token, end="", flush=True)
        print()

        history = await turn_repo.get_history(campaign_id, limit=20, channel="all")
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
                    print(f" Session Zero: incomplete ({len(setup.missing_fields)} fields remain internally)")
                    print(" [1] Continue Conversational Session Zero")
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
                        await play_game_loop(campaign_id, session, campaign_service)
                    else:
                        await run_session_zero_interview(campaign_id, session)
                elif selected == "2":
                    await create_character_menu(campaign_id, session)
                elif selected == "3":
                    await create_scene_menu(campaign_id, session, campaign_service)
                elif selected == "4":
                    await manage_participants_menu(campaign_id, session)
                elif selected == "5":
                    await configure_llm_menu(campaign_id, campaign_service)
                elif selected == "6":
                    confirmation = input("Удалить кампанию безвозвратно? (да/нет): ").strip()
                    if _yes(confirmation):
                        await campaign_service.delete_campaign(campaign_id)
                        await session.commit()
                        print("Кампания удалена.")
                        await asyncio.sleep(1.5)
                        break


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
