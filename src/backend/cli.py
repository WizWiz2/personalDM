import asyncio
import sys
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application import GameApplication
from app.config import settings
from app.db.engine import AsyncSessionLocal, Base, engine
from app.db.repositories.job_repo import PostTurnJobRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.provider_config import ProviderConfigCreate
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.runtime import install_runtime
from app.services.campaign_service import CampaignService
from app.services.session_zero_interview import (
    SessionZeroInterviewIncompleteError,
    SessionZeroInterviewService,
)
from app.services.session_zero_service import SessionZeroService
from app.services.post_turn_dispatcher import PostTurnDispatcher

install_runtime()


def clear_screen() -> None:
    print("\n" * 3)


def _yes(value: str) -> bool:
    return value.strip().casefold() in {"y", "yes", "д", "да"}


def _print_session_zero_provider_error(
    interview: SessionZeroInterviewService,
    error: LLMProviderError,
) -> None:
    if interview.is_rate_limited_error(error):
        print(
            "[Система] Провайдер временно отклонил запрос из-за лимита. "
            "Твой ответ сохранён. Подожди немного и введи /retry; "
            "выходить из нулевой сессии не нужно."
        )
    else:
        print(
            "[Система] Модель не смогла обработать запрос. Твой ответ сохранён. "
            "Введи /retry для повтора или /error, чтобы увидеть техническую причину."
        )


async def _finalize_session_zero_if_ready(
    campaign_id: UUID,
    interview: SessionZeroInterviewService,
    decision,
) -> bool:
    if not decision.ready_to_finalize:
        return False
    try:
        completed = await interview.finalize(campaign_id)
    except SessionZeroInterviewIncompleteError as exc:
        print(
            "[Система] Агент решил начать, но не подготовил технический минимум: "
            + ", ".join(exc.missing_fields)
        )
        return False

    state = await interview.get_state(campaign_id)
    print("=" * 80)
    print("   ИТОГОВЫЕ ДОГОВОРЁННОСТИ")
    print("=" * 80)
    print(state.last_summary or interview.summary(state.draft))
    print(
        f"\n[Система] Нулевая сессия завершена. Первая сцена: "
        f"{completed.scene.title}."
    )
    return True


async def run_session_zero_interview(
    campaign_id: UUID,
    session: AsyncSession,
) -> bool:
    setup = await SessionZeroService(session).get(campaign_id)
    if setup.status == "completed":
        return True

    interview = SessionZeroInterviewService(session)
    state = await interview.get_state(campaign_id)
    last_provider_error: LLMProviderError | None = None

    clear_screen()
    print("=" * 80)
    print("   НУЛЕВАЯ СЕССИЯ")
    print("=" * 80)
    print(
        "Это обычный разговор с мастером, а не анкета. Можно отвечать свободно, "
        "исправлять сказанное и начинать с любой стороны будущей игры."
    )
    print(
        "Мастер сам решит, когда информации достаточно, и достроит безопасные "
        "недостающие детали без лишнего допроса."
    )
    print(
        "Команды: /summary — сводка, /retry — повторить сохранённый ответ, "
        "/error — причина ошибки, /later — продолжить позже.\n"
    )

    if state.pending_user_message:
        print(
            "[Система] Последний ответ уже сохранён. Пробую получить ответ мастера..."
        )
        try:
            decision = await interview.retry_pending(campaign_id)
        except LLMProviderError as exc:
            last_provider_error = exc
            _print_session_zero_provider_error(interview, exc)
        else:
            if decision:
                print(f"\nМастер: {decision.assistant_message}\n")
                if await _finalize_session_zero_if_ready(
                    campaign_id,
                    interview,
                    decision,
                ):
                    return True
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
            print(f"Мастер: {last_assistant}\n")
    else:
        print(f"Мастер: {interview.OPENING_MESSAGE}\n")

    while True:
        state = await interview.get_state(campaign_id)
        has_pending = bool(state.pending_user_message)
        prompt = "Команда: " if has_pending else "Ты: "
        try:
            user_input = input(prompt).strip()
        except (EOFError, StopIteration):
            return False
        if not user_input:
            continue
        command = user_input.casefold()

        if command == "/later":
            print("[Система] Беседа сохранена. Её можно продолжить из меню кампании.")
            return False
        if command == "/summary":
            print("\n" + interview.summary(state.draft) + "\n")
            continue
        if command == "/error":
            if last_provider_error is None:
                print("[Система] Технической ошибки в этой сессии пока нет.")
            else:
                detail = " ".join(str(last_provider_error).split())[:2000]
                print(f"[Ошибка провайдера] {detail}")
            continue

        if has_pending:
            if command != "/retry":
                print(
                    "[Система] Сначала нужно обработать уже сохранённый ответ. "
                    "Введи /retry, /error, /summary или /later."
                )
                continue
            print("[Система] Повторяю сохранённый запрос...")
            try:
                decision = await interview.retry_pending(campaign_id)
            except LLMProviderError as exc:
                last_provider_error = exc
                _print_session_zero_provider_error(interview, exc)
                continue
            if decision is None:
                print("[Система] Сохранённого ответа для повтора уже нет.")
                continue
            last_provider_error = None
        else:
            if command == "/retry":
                print("[Система] Нет сохранённого ответа, ожидающего обработки.")
                continue
            try:
                decision = await interview.answer(campaign_id, user_input)
            except LLMProviderError as exc:
                last_provider_error = exc
                _print_session_zero_provider_error(interview, exc)
                continue
            except ValueError as exc:
                print(f"[Система] {exc}")
                continue
            last_provider_error = None

        print(f"\nМастер: {decision.assistant_message}\n")
        if await _finalize_session_zero_if_ready(
            campaign_id,
            interview,
            decision,
        ):
            return True


async def select_campaign_menu(
    campaign_service: CampaignService,
    session: AsyncSession,
) -> UUID | None:
    while True:
        clear_screen()
        print("=== УПРАВЛЕНИЕ КАМПАНИЯМИ ===")
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
            started = await run_session_zero_interview(campaign.id, session)
            if started:
                await play_game_loop(campaign.id, session)
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
    print("=== НАСТРОЙКА LLM ===")
    config = await campaign_service.get_provider_config(campaign_id)
    if config:
        print(f"Сейчас: {config.model_name} @ {config.base_url}")
        print(f"Размер контекста: {config.context_window}")
    base_url = input(f"Адрес API [{settings.LLM_BASE_URL}]: ").strip()
    model_name = input(f"Модель [{settings.LLM_MODEL}]: ").strip()
    api_key = input("Ключ API [Enter — оставить значение по умолчанию]: ").strip()
    context_raw = input(
        f"Размер контекста [{settings.LLM_CONTEXT_WINDOW}]: "
    ).strip()
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
    application: GameApplication,
) -> None:
    clear_screen()
    print("=== СОЗДАТЬ NPC ВРУЧНУЮ ===")
    name = input("Имя: ").strip()
    if not name:
        print("Имя обязательно.")
        return
    description = input("Кто это: ").strip()
    personality = input("Характер: ").strip()
    await application.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name=name,
            description=description,
            personality=personality,
        ),
    )
    print(f"NPC «{name}» создан.")


async def create_scene_menu(
    campaign_id: UUID,
    application: GameApplication,
) -> None:
    clear_screen()
    print("=== СОЗДАТЬ ЛОКАЦИЮ И СЦЕНУ ===")
    location_name = input("Название места: ").strip()
    if not location_name:
        print("Название обязательно.")
        return
    description = input("Описание места: ").strip()
    mood = input("Настроение сцены: ").strip()
    scene = await application.create_and_activate_scene(
        campaign_id,
        location_name=location_name,
        description=description,
        mood=mood,
    )
    print(f"Сцена «{scene.title}» создана и активирована.")


async def manage_participants_menu(
    campaign_id: UUID,
    application: GameApplication,
) -> None:
    while True:
        try:
            view, available = await application.participant_roster(campaign_id)
        except ValueError as exc:
            print(f"[Система] {exc}")
            return

        clear_screen()
        print(f"=== УЧАСТНИКИ СЦЕНЫ: {view.scene.title} ===")
        print("Сейчас присутствуют:")
        for item in view.participants:
            suffix = " (герой игрока)" if item.id == view.player_character_id else ""
            print(f" - {item.name}{suffix}")
        if not view.participants:
            print(" - никого")
        print("\nМожно добавить:")
        for index, item in enumerate(available, start=1):
            print(f" [{index}] {item.name}")
        print("\n[R имя] удалить   [Q] назад")
        choice = input("Выбор: ").strip()
        if choice.casefold() == "q":
            return
        if choice.casefold().startswith("r "):
            name = choice[2:].strip().casefold()
            matched = next(
                (item for item in view.participants if item.name.casefold() == name),
                None,
            )
            if matched:
                try:
                    await application.remove_participant(campaign_id, matched.id)
                except ValueError as exc:
                    print(f"[Система] {exc}")
            continue
        if choice.isdigit() and 0 <= int(choice) - 1 < len(available):
            await application.add_participant(
                campaign_id,
                available[int(choice) - 1].id,
            )


async def _show_post_turn_status(
    application: GameApplication,
    assistant_turn_id: UUID,
) -> None:
    status = await application.post_turn_status(assistant_turn_id)
    if not status.failed_count:
        return
    if status.rate_limited:
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
    application: GameApplication,
) -> None:
    result = await application.retry_failed_post_turn(campaign_id)
    if result.succeeded == 0 and result.remaining == 0:
        print("[Система] Неудачных задач памяти нет.")
        return
    print(
        f"[Система] Повторено успешно: {result.succeeded}; "
        f"всё ещё ожидают: {result.remaining}."
    )


async def play_game_loop(
    campaign_id: UUID,
    session: AsyncSession,
) -> None:
    # CLI owns its event loop. Recover jobs left by an interrupted play.bat and let
    # the dispatcher finish the current turn before returning control to the player;
    # otherwise the loop closes and durable memory remains permanently pending.
    await PostTurnJobRepository(session).recover_stale()
    await session.commit()
    setup = await SessionZeroService(session).get(campaign_id)
    if setup.status != "completed":
        if not await run_session_zero_interview(campaign_id, session):
            return

    application = GameApplication(session)
    active_listener_id: UUID | None = None
    active_listener_name = "Рассказчик / Мастер"

    clear_screen()
    view = await application.current_scene_view(campaign_id)
    if view is None:
        print("[Система] Активная сцена отсутствует.")
        return
    print("=" * 80)
    print(f"   НАЧАЛО ПРИКЛЮЧЕНИЯ: {view.campaign_name}")
    print("=" * 80)
    print("Команды:")
    print("  /DM <вопрос>      — спросить мастера вне сцены")
    print("  /talk <имя>       — обратиться к присутствующему NPC")
    print("  /talk narrator    — вернуться к рассказчику")
    print("  /facts            — показать активные факты")
    print("  /retry-memory     — повторить фоновые задачи памяти")
    print("  /undo             — отменить последний игровой ход")
    print("  /exit             — вернуться в меню")
    print("=" * 80)

    while True:
        view = await application.current_scene_view(campaign_id)
        if view is None:
            print("[Система] Активная сцена отсутствует.")
            return
        npcs = list(view.npcs)
        if active_listener_id and all(item.id != active_listener_id for item in npcs):
            active_listener_id = None
            active_listener_name = "Рассказчик / Мастер"

        print(
            f"\n[Сцена: {view.scene.title}] | "
            f"[Настроение: {view.scene.mood or '—'}]"
        )
        names = ", ".join(item.name for item in npcs) or "нет"
        print(
            f"[Присутствуют NPC: {names}] | "
            f"[Разговор с: {active_listener_name}]"
        )
        print("-" * 80)
        user_input = input("\nТы: ").strip()
        if not user_input:
            continue
        command = user_input.casefold()
        if command == "/exit":
            return
        if command == "/undo":
            if await application.undo_last_turn(campaign_id):
                print("[Система] Последний игровой ход и его последствия отменены.")
            else:
                print("[Система] Последнюю игровую пару отменить нельзя.")
            continue
        if command == "/facts":
            facts = await application.list_active_facts(campaign_id)
            if not facts:
                print("[Система] Фактов пока нет.")
            for fact in facts:
                print(
                    f" - [{fact.memory_kind}] {fact.subject} "
                    f"{fact.predicate} {fact.object_value or ''}"
                )
            continue
        if command == "/retry-memory":
            await _retry_failed_memory(campaign_id, application)
            continue
        if command.startswith("/talk "):
            target = user_input[6:].strip().casefold()
            if target in {"narrator", "none", "dm", "рассказчик", "мастер"}:
                active_listener_id = None
                active_listener_name = "Рассказчик / Мастер"
            else:
                matched = next(
                    (item for item in npcs if item.name.casefold() == target),
                    None,
                )
                if matched:
                    active_listener_id = matched.id
                    active_listener_name = matched.name
                else:
                    print("[Система] Этого персонажа нет в текущей сцене.")
            continue

        try:
            route = await application.route_input(
                campaign_id,
                TurnCreate(
                    role="user",
                    content=user_input,
                    acting_character_id=active_listener_id,
                ),
            )
        except ValueError as exc:
            print(f"[Система] {exc}")
            continue

        label = "Мастер (вне игры)" if route.channel == "meta" else "Мастер"
        print(f"\n{label}: ", end="", flush=True)
        async for token in route.stream:
            print(token, end="", flush=True)
        print()

        if route.channel == "narrative":
            assistant_turn_id = await application.latest_assistant_turn_id(campaign_id)
            if assistant_turn_id:
                await PostTurnDispatcher.wait_for_idle()
                await _show_post_turn_status(application, assistant_turn_id)


async def main() -> None:
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        campaign_service = CampaignService(session)
        application = GameApplication(session)
        while True:
            clear_screen()
            print("=" * 40)
            print("   PERSONAL DM — TRUTH ENGINE")
            print("=" * 40)
            print(" [1] Управление кампаниями")
            print(" [Q] Выход")
            choice = input("\nВыбор: ").strip()
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
                print(f"=== КАМПАНИЯ: {campaign.name} ===")
                if setup.status == "completed":
                    print(
                        f" Нулевая сессия: завершена — {setup.player_character_name}"
                    )
                    print(" [1] Начать / продолжить игру")
                else:
                    print(" Нулевая сессия: не завершена")
                    print(" [1] Продолжить нулевую сессию")
                print(" [2] Создать персонажа / NPC вручную")
                print(" [3] Создать сцену / локацию вручную")
                print(" [4] Управление участниками сцены")
                print(" [5] Настройка LLM")
                print(" [6] Удалить кампанию")
                print(" [Q] Назад к списку кампаний")
                selected = input("\nВыбор: ").strip()
                if selected.casefold() == "q":
                    break
                if selected == "1":
                    if setup.status == "completed":
                        await play_game_loop(campaign_id, session)
                    else:
                        started = await run_session_zero_interview(
                            campaign_id,
                            session,
                        )
                        if started:
                            await play_game_loop(campaign_id, session)
                elif selected == "2":
                    await create_character_menu(campaign_id, application)
                elif selected == "3":
                    await create_scene_menu(campaign_id, application)
                elif selected == "4":
                    await manage_participants_menu(campaign_id, application)
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
