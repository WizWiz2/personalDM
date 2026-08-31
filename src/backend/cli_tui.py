from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from uuid import UUID

from app.application import GameApplication
from app.cli_ui import confirm_menu, select_menu
from app.db.engine import AsyncSessionLocal, Base, engine
from app.models.campaign import CampaignCreate
from app.models.provider_config import ProviderConfigCreate
from app.services.campaign_service import CampaignService
from app.services.runtime_provider_service import RuntimeProviderError, RuntimeProviderService
from app.services.session_zero_service import SessionZeroService
from cli import (
    clear_screen,
    create_character_menu,
    create_scene_menu,
    play_game_loop,
    run_session_zero_interview,
)


async def select_campaign_menu(
    campaign_service: CampaignService,
    session,
) -> UUID | None:
    while True:
        clear_screen()
        campaigns = await campaign_service.list_campaigns()
        setup_service = SessionZeroService(session)
        choices: list[tuple[str, object]] = []
        for campaign in campaigns:
            setup = await setup_service.get(campaign.id)
            status = "готова к игре" if setup.status == "completed" else "нужна нулевая сессия"
            choices.append((f"{campaign.name} — {status}", campaign.id))
        choices.extend(
            [
                ("＋ Создать новую кампанию", "__new__"),
                ("Выйти из CLI", "__exit__"),
            ]
        )
        selected = select_menu("Кампании", choices)
        if selected in {None, "__exit__"}:
            return None
        if selected == "__new__":
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
            return campaign.id
        return selected if isinstance(selected, UUID) else UUID(str(selected))


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
        if view.participants:
            print("Сейчас: " + ", ".join(item.name for item in view.participants))
        else:
            print("Сейчас: никого")

        choices: list[tuple[str, object]] = []
        choices.extend((f"＋ Добавить: {item.name}", ("add", item.id)) for item in available)
        choices.extend(
            (f"− Удалить: {item.name}", ("remove", item.id))
            for item in view.participants
            if item.id != view.player_character_id
        )
        choices.append(("← Назад", ("back", None)))

        selected = select_menu("Изменить состав сцены", choices)
        if selected is None:
            return
        action, entity_id = selected
        if action == "back":
            return
        try:
            if action == "add":
                await application.add_participant(campaign_id, entity_id)
            elif action == "remove":
                await application.remove_participant(campaign_id, entity_id)
        except ValueError as exc:
            print(f"[Система] {exc}")
            await asyncio.sleep(1)


async def _sync_campaign_text_provider(
    campaign_id: UUID,
    campaign_service: CampaignService,
    runtime: RuntimeProviderService,
) -> None:
    profile = runtime.profile()["text"]
    env = runtime.read_env()
    await campaign_service.configure_provider(
        campaign_id,
        ProviderConfigCreate(
            base_url=profile["base_url"],
            model_name=profile["model"],
            api_key=env.get("PDM_LLM_API_KEY") or None,
            context_window=profile["context_window"],
        ),
    )


async def runtime_provider_menu(
    campaign_id: UUID,
    campaign_service: CampaignService,
    session,
) -> None:
    runtime = RuntimeProviderService()
    while True:
        profile = await asyncio.to_thread(runtime.profile)
        text = profile["text"]
        image = profile["image"]
        clear_screen()
        selected = select_menu(
            "Модели и провайдеры",
            [
                (
                    f"Текст: {text['mode']} / {text['model']} — {text['status']['message']}",
                    "text",
                ),
                (
                    f"Графика: {image['mode']} / {image['model']} — {image['status']['message']}",
                    "image",
                ),
                ("Проверить всё", "check"),
                ("← Назад", "back"),
            ],
        )
        if selected in {None, "back"}:
            return

        if selected == "check":
            text_status, image_status = await asyncio.gather(
                asyncio.to_thread(runtime.check_text),
                asyncio.to_thread(runtime.check_image),
            )
            print(f"\nТекст: {text_status['message']}")
            print(f"Графика: {image_status['message']}")
            input("\nEnter — продолжить")
            continue

        if selected == "text":
            mode = select_menu(
                "Текстовая модель",
                [
                    ("Локально — Ollama", "local"),
                    ("Облачно — OpenAI-compatible API", "cloud"),
                    ("← Назад", "back"),
                ],
            )
            if mode in {None, "back"}:
                continue
            try:
                if mode == "local":
                    model = input(f"Модель [{text['model'] or 'gemma4:e4b'}]: ").strip()
                    runtime.configure_text("local", model=model or None)
                    status = await asyncio.to_thread(runtime.check_text)
                    if not status["ready"] and confirm_menu(
                        f"{status['message']}. Установить/починить локальный runtime?"
                    ):
                        print("[Setup] Подготавливаю Ollama и модель...")
                        await asyncio.to_thread(runtime.ensure_local_text)
                else:
                    base_url = input(f"Base URL [{text['base_url'] or 'https://api.openai.com/v1'}]: ").strip() or text["base_url"] or "https://api.openai.com/v1"
                    model = input(f"Модель [{text['model']}]: ").strip() or text["model"]
                    key = input("API key [Enter — оставить текущий]: ").strip() or None
                    context_raw = input(f"Контекст [{text['context_window']}]: ").strip()
                    context = int(context_raw) if context_raw.isdigit() else text["context_window"]
                    runtime.configure_text(
                        "cloud",
                        base_url=base_url,
                        model=model,
                        api_key=key,
                        context_window=context,
                    )
                await _sync_campaign_text_provider(campaign_id, campaign_service, runtime)
                await session.commit()
                print(f"[Система] {runtime.check_text()['message']}")
            except (ValueError, RuntimeProviderError) as exc:
                print(f"[Ошибка] {exc}")
            input("\nEnter — продолжить")
            continue

        if selected == "image":
            mode = select_menu(
                "Графическая модель",
                [
                    ("Локально — ComfyUI + FLUX.2 Klein", "local"),
                    ("Облачно — Images API", "cloud"),
                    ("Не использовать генерацию", "off"),
                    ("← Назад", "back"),
                ],
            )
            if mode in {None, "back"}:
                continue
            try:
                if mode == "local":
                    runtime.configure_image("local")
                    status = await asyncio.to_thread(runtime.check_image)
                    if not status["ready"] and confirm_menu(
                        f"{status['message']}. Установить/починить локальный runtime?"
                    ):
                        print("[Setup] Подготавливаю ComfyUI и модели...")
                        await asyncio.to_thread(runtime.ensure_local_image)
                elif mode == "cloud":
                    base_url = input(f"Images API Base URL [{image['base_url'] or 'https://api.openai.com/v1'}]: ").strip() or image["base_url"] or "https://api.openai.com/v1"
                    model = input(f"Image model [{image['model'] or 'gpt-image-2'}]: ").strip() or image["model"] or "gpt-image-2"
                    key = input("Image API key [Enter — оставить текущий]: ").strip() or None
                    runtime.configure_image("cloud", base_url=base_url, model=model, api_key=key)
                else:
                    runtime.configure_image("off")
                print(f"[Система] {runtime.check_image()['message']}")
            except (ValueError, RuntimeProviderError) as exc:
                print(f"[Ошибка] {exc}")
            input("\nEnter — продолжить")


async def campaign_menu(
    campaign_id: UUID,
    campaign_service: CampaignService,
    session,
) -> None:
    application = GameApplication(session)
    while True:
        campaign = await campaign_service.get_campaign(campaign_id)
        if not campaign:
            return
        setup = await SessionZeroService(session).get(campaign_id)
        clear_screen()
        print(f"=== {campaign.name} ===")
        if setup.status == "completed":
            print(f"Нулевая сессия: завершена — {setup.player_character_name or 'герой'}")
            play_label = "▶ Начать / продолжить игру"
        else:
            print("Нулевая сессия: не завершена")
            play_label = "◆ Продолжить нулевую сессию"

        selected = select_menu(
            "Кампания",
            [
                (play_label, "play"),
                ("Создать персонажа / NPC вручную", "npc"),
                ("Создать сцену / локацию вручную", "scene"),
                ("Управление участниками сцены", "participants"),
                ("Модели и провайдеры", "providers"),
                ("Удалить кампанию", "delete"),
                ("← К списку кампаний", "back"),
            ],
        )
        if selected in {None, "back"}:
            return
        if selected == "play":
            if setup.status == "completed":
                await play_game_loop(campaign_id, session)
            else:
                started = await run_session_zero_interview(campaign_id, session)
                if started:
                    await play_game_loop(campaign_id, session)
        elif selected == "npc":
            await create_character_menu(campaign_id, application)
        elif selected == "scene":
            await create_scene_menu(campaign_id, application)
        elif selected == "participants":
            await manage_participants_menu(campaign_id, application)
        elif selected == "providers":
            await runtime_provider_menu(campaign_id, campaign_service, session)
        elif selected == "delete":
            if confirm_menu(f"Удалить кампанию «{campaign.name}» безвозвратно?"):
                await campaign_service.delete_campaign(campaign_id)
                await session.commit()
                print("Кампания удалена.")
                return


async def main() -> None:
    # ``play.bat`` applies Alembic migrations before entering the CLI.  Calling
    # ``create_all`` here against an existing SQLite database makes SQLAlchemy
    # re-parse every sqlite_schema row and turns a stale/corrupt schema entry
    # into a hard crash before the campaign menu can open.  The CLI is allowed
    # to bootstrap only a genuinely new database; existing databases are owned
    # by Alembic and must not be implicitly rewritten by the UI.
    database_path = Path(engine.url.database) if engine.url.database else None
    if database_path is not None and not database_path.exists():
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        campaign_service = CampaignService(session)
        while True:
            campaign_id = await select_campaign_menu(campaign_service, session)
            if campaign_id is None:
                return
            await campaign_menu(campaign_id, campaign_service, session)


if __name__ == "__main__":
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
