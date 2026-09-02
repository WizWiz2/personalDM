from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from fastapi.testclient import TestClient


@dataclass
class FixtureWorld:
    campaign_id: str
    hero_id: str
    scene_id: str
    locations: dict[str, str] = field(default_factory=dict)
    characters: dict[str, str] = field(default_factory=dict)
    items: dict[str, str] = field(default_factory=dict)
    facts: dict[str, str] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


def _must(response, expected: tuple[int, ...] = (200, 201)) -> dict:
    if response.status_code not in expected:
        raise RuntimeError(
            f"fixture API failed: {response.request.method} {response.request.url} -> "
            f"{response.status_code}: {response.text[:2000]}"
        )
    if response.status_code == 204:
        return {}
    return response.json()


def _character_draft(name: str, *, location_id: str | None, equipment: list[str]) -> dict:
    return {
        "canonical_name": name,
        "description": f"{name} — основной персонаж локального model-contract теста.",
        "appearance": "Высокий человек в тёмной практичной одежде без скрытых особенностей.",
        "face_description": "Спокойное лицо и внимательный взгляд.",
        "body_description": "Подтянутое телосложение.",
        "immutable_features": "Небольшой шрам над левой бровью.",
        "personality": "Наблюдательный и прямой.",
        "values": ["точность", "самостоятельность"],
        "fears": ["потерять контроль над собственными решениями"],
        "desires": ["разобраться в текущей ситуации"],
        "voice": "Спокойный голос.",
        "speech_patterns": "Короткие прямые фразы.",
        "biography": "Живёт в городе и хорошо знает свой дом и ближайшие улицы.",
        "backstory_public": "Местный житель.",
        "secrets": ["Нет скрытых способностей, влияющих на тест."],
        "emotional_state": "спокоен",
        "current_intentions": ["действовать только по решениям игрока"],
        "goals": ["решить текущую бытовую задачу"],
        "capabilities": ["ходить", "разговаривать", "осматривать обычные предметы"],
        "limitations": ["не обладает сверхъестественными способностями"],
        "equipment": equipment,
        "initial_beliefs": ["Комната, коридор и контора являются обычными безопасными местами."],
        "visual_profile": {"test_fixture": True},
        "current_location_id": location_id,
    }


def _npc_payload(name: str, location_id: str, *, status: str = "active") -> dict:
    return {
        "entity_type": "character",
        "canonical_name": name,
        "description": f"{name} — обычный местный житель, знакомый Каю.",
        "status": status,
        "appearance": "Тёмная куртка, короткие волосы, заметные рабочие часы на запястье.",
        "personality": "Практичный и спокойный.",
        "voice": "Низкий спокойный голос.",
        "current_location_id": location_id if status == "active" else None,
        "current_intentions": ["заниматься своими делами"],
    }


async def _configure_campaign_provider(campaign_id: str, base_url: str, model: str) -> None:
    from uuid import UUID

    from app.db.engine import AsyncSessionLocal
    from app.db.repositories.provider_config_repo import ProviderConfigRepository
    from app.models.provider_config import ProviderConfigCreate

    async with AsyncSessionLocal() as session:
        await ProviderConfigRepository(session).create_or_update(
            UUID(campaign_id),
            ProviderConfigCreate(
                base_url=base_url,
                model_name=model,
                api_key=None,
                context_window=131072,
            ),
        )
        await session.commit()


def configure_campaign_provider(campaign_id: str, base_url: str, model: str) -> None:
    asyncio.run(_configure_campaign_provider(campaign_id, base_url, model))


def new_client() -> TestClient:
    """Import the application only after the runner has installed isolated env settings."""
    from app.main import app

    return TestClient(app)


def build_standard_world(client: TestClient, *, narrator_base_url: str, narrator_model: str) -> FixtureWorld:
    """Create a small deterministic world without asking any model to invent fixture truth."""
    campaign = _must(client.post("/api/campaigns", json={"name": "Live model contract"}))
    campaign_id = campaign["id"]

    location_payloads = {
        "Комната Кая": {
            "description": (
                "Небольшая жилая комната Кая: кровать у стены, рабочий стол, закрытый шкаф и "
                "обычная дверь в общий коридор. Здесь нет скрытой угрозы или текущего конфликта."
            ),
            "atmosphere": "тихо, безопасно, бытовая обстановка",
            "notable_features": "рабочий стол, кровать, дверь в коридор",
        },
        "Коридор": {
            "description": (
                "Обычный светлый коридор жилого этажа соединяет комнату Кая с лестницей и "
                "небольшой конторой управляющего. Проход открыт и не охраняется."
            ),
            "atmosphere": "спокойно",
            "notable_features": "двери комнат, лестница, дверь конторы",
        },
        "Контора": {
            "description": (
                "Небольшая контора управляющего с письменным столом, двумя стульями и шкафом "
                "для документов. В обычное время сюда можно пройти из коридора."
            ),
            "atmosphere": "деловая и спокойная",
            "notable_features": "письменный стол, шкаф документов",
        },
        "Склад": {
            "description": (
                "Сухой склад на первом этаже с металлическими стеллажами и маркированными "
                "контейнерами. Он является отдельным известным помещением здания."
            ),
            "atmosphere": "тихо",
            "notable_features": "металлические стеллажи, контейнеры",
        },
    }
    locations: dict[str, str] = {}
    for name, payload in location_payloads.items():
        created = _must(
            client.post(
                f"/api/campaigns/{campaign_id}/locations",
                json={"canonical_name": name, **payload},
            )
        )
        locations[name] = created["id"]

    hero_build = _must(
        client.post(
            f"/api/campaigns/{campaign_id}/characters/from-draft",
            json=_character_draft(
                "Кай",
                location_id=locations["Комната Кая"],
                equipment=["латунный ключ"],
            ),
        )
    )
    hero = hero_build["character"]
    item_id = hero_build["item_ids"][0]

    setup_payload = {
        "setting_name": "Тестовый город",
        "genre": "современная приземлённая история",
        "premise": "Кай решает обычные задачи в знакомом здании.",
        "tone": "спокойный и конкретный",
        "themes": ["наблюдение", "последствия решений"],
        "boundaries": [
            "не решать за Кая",
            "не добавлять угрозы без установленной причины",
            "не переносить персонажей между местами без основания",
        ],
        "boundaries_confirmed": True,
        "rules_system": "systemless narrative",
        "world_summary": (
            "Здание и ближайший квартал подчиняются обычной причинности. Комната Кая, коридор, "
            "контора и склад уже существуют; обычные безопасные действия не требуют бросков."
        ),
        "starting_situation": "Кай находится в своей комнате. Сейчас спокойно.",
        "starting_location_id": locations["Комната Кая"],
        "starting_scene_title": "Комната Кая",
        "play_style": "буквальная причинность и уважение agency игрока",
        "content_rating": "adult non-explicit",
        "player_character_id": hero["id"],
        "narrative_style": "короткая конкретная русская проза",
    }
    _must(client.put(f"/api/campaigns/{campaign_id}/session-zero", json=setup_payload))
    completed = _must(
        client.post(
            f"/api/campaigns/{campaign_id}/session-zero/complete",
            json={"player_character_id": hero["id"], "starting_scene_title": "Комната Кая"},
        )
    )
    scene_id = completed["scene"]["id"]

    martin = _must(
        client.post(
            f"/api/campaigns/{campaign_id}/characters",
            json=_npc_payload("Мартин Вэнс", locations["Комната Кая"]),
        )
    )
    lydia = _must(
        client.post(
            f"/api/campaigns/{campaign_id}/characters",
            json=_npc_payload("Лидия", locations["Комната Кая"], status="dead"),
        )
    )
    _must(
        client.post(
            f"/api/scenes/{scene_id}/participants",
            params={"entity_id": martin["id"]},
        )
    )

    # Known bidirectional routes make movement cases about model semantics rather than route discovery.
    _must(
        client.post(
            f"/api/campaigns/{campaign_id}/locations/{locations['Комната Кая']}/exits",
            json={
                "to_location_id": locations["Коридор"],
                "label": "дверь в коридор",
                "bidirectional": True,
                "reverse_label": "дверь в комнату Кая",
            },
        )
    )
    _must(
        client.post(
            f"/api/campaigns/{campaign_id}/locations/{locations['Коридор']}/exits",
            json={
                "to_location_id": locations["Контора"],
                "label": "дверь конторы",
                "bidirectional": True,
                "reverse_label": "дверь в коридор",
            },
        )
    )

    configure_campaign_provider(campaign_id, narrator_base_url, narrator_model)

    return FixtureWorld(
        campaign_id=campaign_id,
        hero_id=hero["id"],
        scene_id=scene_id,
        locations=locations,
        characters={"Кай": hero["id"], "Мартин Вэнс": martin["id"], "Лидия": lydia["id"]},
        items={"латунный ключ": item_id},
    )


def transfer_item(client: TestClient, world: FixtureWorld, item_name: str, *, owner: str | None = None, location: str | None = None) -> None:
    payload = {
        "owner_id": world.characters[owner] if owner else None,
        "location_id": world.locations[location] if location else None,
    }
    _must(
        client.post(
            f"/api/campaigns/{world.campaign_id}/items/{world.items[item_name]}/transfer",
            json=payload,
        )
    )


def add_fact(
    client: TestClient,
    world: FixtureWorld,
    key: str,
    *,
    subject: str,
    predicate: str,
    object_value: str,
    visibility: str = "dm",
) -> str:
    fact = _must(
        client.post(
            f"/api/campaigns/{world.campaign_id}/facts",
            json={
                "subject": subject,
                "predicate": predicate,
                "object_value": object_value,
                "visibility": visibility,
                "memory_kind": "world_canon",
            },
        )
    )
    world.facts[key] = fact["id"]
    return fact["id"]


def add_belief(
    client: TestClient,
    world: FixtureWorld,
    character: str,
    proposition: str,
    *,
    source_character: str | None = None,
) -> None:
    _must(
        client.post(
            f"/api/characters/{world.characters[character]}/beliefs",
            json={
                "character_id": world.characters[character],
                "proposition": proposition,
                "status": "known",
                "confidence": 1.0,
                "source_character_id": world.characters[source_character] if source_character else None,
                "visibility": "character_only",
            },
        )
    )
