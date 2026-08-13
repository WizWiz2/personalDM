from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from app.application import GameApplication
from app.cli_ui import confirm_menu, select_menu
from app.db.engine import AsyncSessionLocal, Base, engine
from app.models.campaign import CampaignCreate
from app.services.campaign_service import CampaignService
from app.services.session_zero_service import SessionZeroService
from cli import (
    clear_screen,
    configure_llm_menu,
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
                ("Настройка LLM", "llm"),
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
        elif selected == "llm":
            await configure_llm_menu(campaign_id, campaign_service, session)
        elif selected == "delete":
            if confirm_menu(f"Удалить кампанию «{campaign.name}» безвозвратно?"):
                await campaign_service.delete_campaign(campaign_id)
                await session.commit()
                print("Кампания удалена.")
                return


async def main() -> None:
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
