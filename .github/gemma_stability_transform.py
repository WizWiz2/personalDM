from pathlib import Path
import textwrap


def replace_once(path: str, old: str, new: str) -> None:
    file = Path(path)
    text = file.read_text(encoding="utf-8")
    if text.count(old) != 1:
        raise SystemExit(f"expected one match in {path}, found {text.count(old)}")
    file.write_text(text.replace(old, new), encoding="utf-8")


replace_once(
    "src/backend/app/config.py",
    '    SIM_PLAYER_MODE: str = "deterministic"\n\n',
    '    SIM_PLAYER_MODE: str = "deterministic"\n\n'
    '    # Keep small narrators focused on current scene state instead of long prose tails.\n'
    '    NARRATOR_HISTORY_LIMIT: int = 12\n'
    '    NARRATOR_STAGNATION_TURNS: int = 2\n'
    '    NARRATOR_RECEIPT_MAX_ITEMS: int = 6\n\n',
)

path = "src/backend/app/services/context_compiler.py"
replace_once(path, "from uuid import UUID\n", "import json\nfrom uuid import UUID\n")
replace_once(
    path,
    "from app.db.tables import CharacterGoal, Entity, Item\n",
    "from app.db.tables import CharacterGoal, Entity, Item, ProposedChange, Turn\n",
)
replace_once(
    path,
    "    ACTOR_HISTORY_LIMIT = 8\n    NARRATOR_HISTORY_LIMIT = 24\n",
    "    ACTOR_HISTORY_LIMIT = 8\n    RECEIPT_TURN_LIMIT = 4\n",
)

helper = textwrap.dedent('''
    @staticmethod
    def _short(value: object, limit: int = 240) -> str:
        clean = " ".join(str(value or "").split())
        return clean if len(clean) <= limit else clean[: limit - 1].rstrip() + "…"

    @classmethod
    def _proposal_summary(
        cls,
        change_type: str,
        payload: dict,
        entity_names: dict[str, str],
    ) -> str | None:
        def display(value: object) -> str:
            raw = str(value or "").strip()
            return entity_names.get(raw, raw)

        if change_type == "event":
            return cls._short(payload.get("description")) or None
        if change_type == "fact":
            subject = display(payload.get("subject"))
            predicate = cls._short(payload.get("predicate"), 90)
            value = cls._short(payload.get("object_value"), 140)
            return cls._short(f"{subject}: {predicate} — {value}".strip(" —")) or None
        if change_type == "movement":
            character = display(payload.get("character_id"))
            location = display(payload.get("location_id"))
            return cls._short(f"{character} переместился: {location}") or None
        if change_type == "relationship":
            subject = display(payload.get("subject_id"))
            target = display(payload.get("object_id"))
            relation = cls._short(payload.get("relation_type"), 80)
            description = cls._short(payload.get("description"), 140)
            return cls._short(
                f"{subject} → {target}: {relation}; {description}".strip("; ")
            ) or None
        if change_type == "knowledge":
            recipient = display(payload.get("recipient_id"))
            proposition = cls._short(payload.get("proposition"), 190)
            return cls._short(f"{recipient} узнал: {proposition}") or None
        if change_type == "item_transfer":
            item = display(payload.get("item_id"))
            owner = display(payload.get("owner_id"))
            location = display(payload.get("location_id"))
            destination = owner or location
            return cls._short(f"{item} теперь у/в: {destination}") or None
        return None

    async def _scene_progress_receipt(
        self,
        campaign_id: UUID,
        scene_id: UUID,
    ) -> tuple[list[str], bool, int]:
        stagnation_window = max(1, int(settings.NARRATOR_STAGNATION_TURNS))
        turn_limit = max(self.RECEIPT_TURN_LIMIT, stagnation_window)
        result = await self._session.execute(
            select(Turn.id)
            .where(
                Turn.scene_id == str(scene_id),
                Turn.role == "assistant",
                Turn.status == "active",
            )
            .order_by(Turn.created_at.desc())
            .limit(turn_limit)
        )
        recent_turn_ids = [str(value) for value in result.scalars().all()]
        if not recent_turn_ids:
            return [], False, 0

        result = await self._session.execute(
            select(ProposedChange)
            .where(
                ProposedChange.turn_id.in_(recent_turn_ids),
                ProposedChange.status.in_(("proposed", "accepted", "edited")),
                ProposedChange.change_type.notin_(("scene_thesis", "canon_gap")),
            )
            .order_by(ProposedChange.created_at.asc())
        )
        proposals = result.scalars().all()
        entity_names = {
            str(entity.id): entity.canonical_name
            for entity in await self._entity_repo.list_by_campaign(campaign_id)
        }
        by_turn: dict[str, list[str]] = {turn_id: [] for turn_id in recent_turn_ids}
        for proposal in proposals:
            raw_payload = (
                proposal.user_edit
                if proposal.status == "edited" and proposal.user_edit
                else proposal.payload
            )
            try:
                payload = json.loads(raw_payload or "{}")
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(payload, dict) or payload.get("_validation_error"):
                continue
            summary = self._proposal_summary(
                proposal.change_type,
                payload,
                entity_names,
            )
            if summary and summary not in by_turn[proposal.turn_id]:
                by_turn[proposal.turn_id].append(summary)

        receipt: list[str] = []
        for turn_id in reversed(recent_turn_ids):
            receipt.extend(by_turn[turn_id])
        max_items = max(1, int(settings.NARRATOR_RECEIPT_MAX_ITEMS))
        receipt = receipt[-max_items:]

        watched = recent_turn_ids[:stagnation_window]
        stagnant = len(watched) == stagnation_window and all(
            not by_turn[turn_id] for turn_id in watched
        )
        return receipt, stagnant, len(recent_turn_ids)

''').lstrip("\n")
replace_once(
    path,
    "    async def _history_records(\n",
    helper + "    async def _history_records(\n",
)
replace_once(
    path,
    "            return records[-self.NARRATOR_HISTORY_LIMIT :]\n",
    "            history_limit = max(4, int(settings.NARRATOR_HISTORY_LIMIT))\n"
    "            return records[-history_limit:]\n",
)
replace_once(
    path,
    '            else "\\nYou are the omniscient narrator. Respect every character card, "\n'
    '            "capability, limitation, owned item and current location. Resolve the "\n'
    '            "player\'s attempted action with a concrete consequence. Do not invent a "\n'
    '            "successful ability, item or movement absent from structured state."\n',
    '            else "\\nYou are the omniscient narrator. Respect every character card, "\n'
    '            "capability, limitation, owned item and current location. Resolve the "\n'
    '            "player\'s attempted action with a concrete consequence. Do not invent a "\n'
    '            "successful ability, item or movement absent from structured state.\\n"\n'
    '            "Контракт прогрессии: прямо разреши текущую попытку; покажи хотя бы одно "\n'
    '            "наблюдаемое последствие, новую конкретную информацию или изменившееся "\n'
    '            "препятствие; закончи ситуацией, на которую игрок может осмысленно "\n'
    '            "ответить. Не пересказывай уже установленное и не управляй героем игрока."\n',
)
replace_once(
    path,
    "        included_character_ids: list[str] = []\n        packages: list[str] = []\n",
    "        included_character_ids: list[str] = []\n"
    "        packages: list[str] = []\n"
    "        scene_receipt_items = 0\n"
    "        stagnation_detected = False\n"
    "        recent_scene_turns = 0\n",
)
replace_once(
    path,
    "            scene_thesis_ids = [str(thesis.id) for thesis in visible_theses]\n",
    "            if not actor_mode:\n"
    "                receipt, stagnation_detected, recent_scene_turns = (\n"
    "                    await self._scene_progress_receipt(campaign_id, scene_id)\n"
    "                )\n"
    "                scene_receipt_items = len(receipt)\n"
    "                if receipt:\n"
    "                    scene_info += (\n"
    "                        \"Recent authoritative scene progress:\\n\"\n"
    "                        + \"\".join(f\"- {item}\\n\" for item in receipt)\n"
    "                        + \"Continue from these consequences; do not retell them.\\n\"\n"
    "                    )\n"
    "                if stagnation_detected:\n"
    "                    scene_info += (\n"
    "                        \"[Progress Watchdog] Последние ходы не дали нового \"\n"
    "                        \"устойчивого последствия. В этом ответе сдвинь сцену: \"\n"
    "                        \"раскрой конкретный факт, измени опасность, позицию или \"\n"
    "                        \"отношение, либо дай явное последствие текущей попытки. \"\n"
    "                        \"Не повторяй прежний вопрос или обсуждение.\\n\"\n"
    "                    )\n"
    "            scene_thesis_ids = [str(thesis.id) for thesis in visible_theses]\n",
)
replace_once(
    path,
    '            "current_user_reserved": bool(actor_mode and current_user_content),\n',
    '            "current_user_reserved": bool(actor_mode and current_user_content),\n'
    '            "narrator_history_limit": (\n'
    '                max(4, int(settings.NARRATOR_HISTORY_LIMIT)) if not actor_mode else None\n'
    '            ),\n'
    '            "scene_receipt_items": scene_receipt_items,\n'
    '            "recent_scene_turns_checked": recent_scene_turns,\n'
    '            "stagnation_detected": stagnation_detected,\n',
)

Path("src/backend/tests/test_narrator_stability.py").write_text(
    textwrap.dedent('''
    import json
    from uuid import uuid4

    import pytest
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.config import settings
    from app.db.repositories.campaign_repo import CampaignRepository
    from app.db.repositories.scene_repo import SceneRepository
    from app.db.repositories.turn_repo import TurnRepository
    from app.db.tables import ProposedChange
    from app.models.campaign import CampaignCreate
    from app.models.scene import SceneCreate
    from app.models.turn import TurnCreate
    from app.services.context_compiler import ContextCompiler


    async def add_pair(
        session: AsyncSession,
        campaign_id,
        scene_id,
        number: int,
        *,
        progress: str | None = None,
    ):
        turns = TurnRepository(session)
        user = await turns.create(
            campaign_id,
            TurnCreate(role="user", content=f"Попытка {number}", scene_id=scene_id),
        )
        assistant = await turns.create(
            campaign_id,
            TurnCreate(
                role="assistant",
                content=f"Ответ ДМа {number}",
                scene_id=scene_id,
                parent_turn_id=user.id,
            ),
        )
        if progress:
            session.add(
                ProposedChange(
                    turn_id=str(assistant.id),
                    change_type="event",
                    payload=json.dumps({"description": progress}, ensure_ascii=False),
                    status="accepted",
                )
            )
        await session.flush()
        return assistant


    @pytest.mark.asyncio
    async def test_narrator_receipt_replaces_long_history_with_structured_progress(
        db_session: AsyncSession,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "NARRATOR_HISTORY_LIMIT", 4)
        campaign_id = uuid4()
        await CampaignRepository(db_session).create(
            campaign_id,
            CampaignCreate(name="Focused Gemma"),
        )
        scene = await SceneRepository(db_session).create(
            campaign_id,
            SceneCreate(title="Башня"),
        )
        for number in range(1, 8):
            await add_pair(
                db_session,
                campaign_id,
                scene.id,
                number,
                progress=("Каменная дверь открылась" if number == 7 else None),
            )
        await db_session.commit()

        messages, metadata = await ContextCompiler(db_session).compile_context(
            campaign_id=campaign_id,
            scene_id=scene.id,
        )
        context = "\n".join(message.content for message in messages)

        assert "Каменная дверь открылась" in context
        assert "Continue from these consequences" in context
        assert metadata["scene_receipt_items"] == 1
        assert metadata["history_turns_count"] == 4
        assert metadata["narrator_history_limit"] == 4
        assert metadata["stagnation_detected"] is False


    @pytest.mark.asyncio
    async def test_watchdog_activates_after_two_turns_without_durable_progress(
        db_session: AsyncSession,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "NARRATOR_STAGNATION_TURNS", 2)
        campaign_id = uuid4()
        await CampaignRepository(db_session).create(
            campaign_id,
            CampaignCreate(name="Stagnation Watch"),
        )
        scene = await SceneRepository(db_session).create(
            campaign_id,
            SceneCreate(title="Лестница"),
        )
        await add_pair(db_session, campaign_id, scene.id, 1)
        await add_pair(db_session, campaign_id, scene.id, 2)
        await db_session.commit()

        messages, metadata = await ContextCompiler(db_session).compile_context(
            campaign_id=campaign_id,
            scene_id=scene.id,
        )
        context = "\n".join(message.content for message in messages)

        assert "[Progress Watchdog]" in context
        assert metadata["stagnation_detected"] is True


    @pytest.mark.asyncio
    async def test_recent_progress_suppresses_watchdog_and_receipt_stays_dm_only(
        db_session: AsyncSession,
        monkeypatch,
    ):
        monkeypatch.setattr(settings, "NARRATOR_STAGNATION_TURNS", 2)
        campaign_id = uuid4()
        await CampaignRepository(db_session).create(
            campaign_id,
            CampaignCreate(name="No Leakage"),
        )
        scene = await SceneRepository(db_session).create(
            campaign_id,
            SceneCreate(title="Архив"),
        )
        await add_pair(db_session, campaign_id, scene.id, 1)
        await add_pair(
            db_session,
            campaign_id,
            scene.id,
            2,
            progress="Хранитель назвал тайный пароль",
        )
        await db_session.commit()

        narrator_messages, narrator_metadata = await ContextCompiler(
            db_session
        ).compile_context(campaign_id=campaign_id, scene_id=scene.id)
        actor_messages, actor_metadata = await ContextCompiler(db_session).compile_context(
            campaign_id=campaign_id,
            acting_character_id=uuid4(),
            scene_id=scene.id,
            current_user_content="Что ты знаешь?",
        )
        narrator_context = "\n".join(message.content for message in narrator_messages)
        actor_context = "\n".join(message.content for message in actor_messages)

        assert "Хранитель назвал тайный пароль" in narrator_context
        assert narrator_metadata["stagnation_detected"] is False
        assert "Хранитель назвал тайный пароль" not in actor_context
        assert "[Progress Watchdog]" not in actor_context
        assert actor_metadata["scene_receipt_items"] == 0
    ''').lstrip(),
    encoding="utf-8",
)

Path("docs/gemma-narrator-stability.md").write_text(
    textwrap.dedent('''
    # Стабильность локального Narrator на Gemma

    Narrator не получает дополнительного LLM-агента. Контекст стабилизируется обычным
    кодом поверх уже созданных Scribe proposals.

    По умолчанию в prompt остаются последние 12 сообщений текущей сцены. Более старые
    подробности продолжают приходить через facts, карточки, активные theses и компактный
    scene receipt из последних структурированных последствий.

    ```env
    PDM_NARRATOR_HISTORY_LIMIT=12
    PDM_NARRATOR_STAGNATION_TURNS=2
    PDM_NARRATOR_RECEIPT_MAX_ITEMS=6
    ```

    Scene receipt доступен только Narrator и не попадает в actor-scoped контекст NPC.
    Он не пересказывает литературный текст, а перечисляет последние валидные последствия:
    события, факты, перемещения, знания, отношения и передачи предметов.

    Если несколько последних ответов не породили ни одного валидного последствия,
    deterministic watchdog добавляет в prompt требование сдвинуть сцену конкретным фактом,
    изменением опасности, позиции, отношения или прямым результатом текущей попытки.
    Watchdog не создаёт событие самостоятельно и не расходует отдельный LLM-запрос.
    ''').lstrip(),
    encoding="utf-8",
)
