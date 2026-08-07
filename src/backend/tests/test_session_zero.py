from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient

from app.services.entity_registrar import EntityRegistrationResult


pytestmark = pytest.mark.session_zero_enforced


async def narrator_stream(*args, **kwargs):
    yield "Утро начинается на площади Серого Брода."


async def no_entities(*args, **kwargs):
    return EntityRegistrationResult()


async def no_proposals(*args, **kwargs):
    return []


def full_character_draft(name: str) -> dict:
    return {
        "canonical_name": name,
        "description": "Странствующий следопыт, ищущий пропавшую экспедицию.",
        "appearance": "Высокий человек в потёртом дорожном плаще.",
        "face_description": "Усталое лицо и внимательный взгляд.",
        "body_description": "Подтянутый, привыкший к долгим переходам.",
        "immutable_features": "Старый шрам над левой бровью.",
        "personality": "Наблюдательный, осторожный и упрямый.",
        "values": ["верность", "правда"],
        "fears": ["подвести спутников"],
        "desires": ["найти экспедицию", "понять тайну тракта"],
        "voice": "Спокойный низкий голос.",
        "speech_patterns": "Говорит коротко и задаёт уточняющие вопросы.",
        "biography": "Бывший проводник королевской картографической службы.",
        "backstory_public": "Несколько лет водил караваны через северные перевалы.",
        "secrets": ["Скрывает, что его брат был участником экспедиции."],
        "emotional_state": "собран",
        "current_intentions": ["расспросить жителей", "осмотреть старый тракт"],
        "goals": ["найти след экспедиции", "не подвергать жителей опасности"],
        "capabilities": ["ориентироваться в глуши", "читать следы"],
        "limitations": ["не владеет магией", "не умеет лечить тяжёлые раны"],
        "equipment": ["дорожный плащ", "карта северного тракта"],
        "initial_beliefs": ["Экспедиция не исчезла бесследно."],
        "visual_profile": {"palette": "серый, зелёный, медный"},
    }


def test_new_campaign_blocks_narration_until_session_zero(client: TestClient):
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Новая кампания"},
    ).json()

    setup = client.get(
        f"/api/campaigns/{campaign['id']}/session-zero"
    ).json()
    assert setup["status"] == "draft"
    assert setup["ready_to_complete"] is False
    # PR #60 made descriptive world/card fields diagnostic. Only the material
    # objects needed to start play remain hard blockers.
    assert "setup.world_anchor" not in setup["missing_fields"]
    assert "setup.starting_situation" in setup["missing_fields"]
    assert "setup.starting_location_id" in setup["missing_fields"]
    assert "setup.player_character_id" in setup["missing_fields"]

    response = client.post(
        f"/api/campaigns/{campaign['id']}/turns",
        json={"role": "user", "content": "Привет"},
    )
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert detail["code"] == "session_zero_required"
    assert "setup.starting_location_id" in detail["missing_fields"]
    assert "setup.player_character_id" in detail["missing_fields"]


def test_incomplete_character_card_is_diagnostic_not_completion_gate(
    client: TestClient,
):
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Растущая карточка"},
    ).json()
    location = client.post(
        f"/api/campaigns/{campaign['id']}/locations",
        json={"canonical_name": "Портовая площадь"},
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign['id']}/characters",
        json={"canonical_name": "Безымянный герой"},
    ).json()

    update = client.put(
        f"/api/campaigns/{campaign['id']}/session-zero",
        json={
            "setting_name": "Серый Берег",
            "genre": "низкое фэнтези",
            "premise": "Герой прибывает в город перед штормом.",
            "tone": "мрачное приключение",
            "world_summary": "Прибрежные города живут торговлей и старыми клятвами.",
            "starting_situation": "На площади ищут людей для опасной работы.",
            "starting_location_id": location["id"],
            "boundaries": [],
            "boundaries_confirmed": True,
            "player_character_id": hero["id"],
        },
    )
    assert update.status_code == 200, update.text
    payload = update.json()
    assert payload["ready_to_complete"] is True
    assert payload["missing_fields"] == []
    assert "character.description" in payload["character_card_missing_fields"]

    completed = client.post(
        f"/api/campaigns/{campaign['id']}/session-zero/complete",
        json={},
    )
    assert completed.status_code == 200, completed.text
    result = completed.json()
    assert result["setup"]["status"] == "completed"
    assert result["character_card"]["ready_for_play"] is False
    assert "goals" in result["character_card"]["missing_fields"]


def test_session_zero_atomically_creates_playable_start(client: TestClient):
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Серый Брод"},
    ).json()
    location = client.post(
        f"/api/campaigns/{campaign['id']}/locations",
        json={
            "canonical_name": "Площадь Серого Брода",
            "description": "Каменная площадь у старого моста.",
        },
    ).json()
    built = client.post(
        f"/api/campaigns/{campaign['id']}/characters/from-draft",
        json=full_character_draft("Рен"),
    )
    assert built.status_code == 201, built.text
    hero = built.json()["character"]

    card = client.get(f"/api/characters/{hero['id']}/card")
    assert card.status_code == 200, card.text
    assert card.json()["ready_for_play"] is True
    assert card.json()["completion_ratio"] == 1.0
    assert len(card.json()["equipment"]) == 2

    setup = client.put(
        f"/api/campaigns/{campaign['id']}/session-zero",
        json={
            "setting_name": "Серый Брод",
            "genre": "приключенческое низкое фэнтези",
            "premise": "Пропавшая экспедиция оставила след у северного тракта.",
            "tone": "приземлённый, тревожный, но не беспросветный",
            "themes": ["долг", "тайны прошлого"],
            "boundaries": ["без сексуального насилия", "не управлять героем игрока"],
            "boundaries_confirmed": True,
            "rules_system": "свободная повествовательная система",
            "world_summary": "Пограничные земли восстанавливаются после долгой войны.",
            "starting_situation": "На площади Рен замечает объявление о поиске проводника.",
            "starting_location_id": location["id"],
            "starting_scene_title": "Утро на площади",
            "play_style": "исследование, диалоги и конкретные последствия решений",
            "content_rating": "18+",
            "player_character_id": hero["id"],
            "narrative_style": "компактная атмосферная проза без решений за героя",
        },
    )
    assert setup.status_code == 200, setup.text
    assert setup.json()["ready_to_complete"] is True
    assert setup.json()["missing_fields"] == []

    completed = client.post(
        f"/api/campaigns/{campaign['id']}/session-zero/complete",
        json={},
    )
    assert completed.status_code == 200, completed.text
    payload = completed.json()
    assert payload["setup"]["status"] == "completed"
    assert payload["scene"]["title"] == "Утро на площади"
    assert payload["scene"]["location_id"] == location["id"]
    assert hero["id"] in payload["scene"]["participants"]
    assert payload["character_card"]["character"]["current_location_id"] == location["id"]

    campaign_after = client.get(
        f"/api/campaigns/{campaign['id']}"
    ).json()
    assert campaign_after["player_character_id"] == hero["id"]
    assert campaign_after["current_scene_id"] == payload["scene"]["id"]
    assert "[BEGIN SESSION ZERO CONTRACT]" in campaign_after["system_instructions"]
    assert "Пропавшая экспедиция" in campaign_after["system_instructions"]

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=narrator_stream,
    ), patch(
        "app.services.entity_registrar.EntityRegistrar.register_from_turn",
        side_effect=no_entities,
    ), patch(
        "app.services.memory_scribe.MemoryScribe.extract_proposals",
        side_effect=no_proposals,
    ), patch(
        "app.services.thesis_curator.ThesisCurator.curate_after_turn",
        return_value=None,
    ):
        turn = client.post(
            f"/api/campaigns/{campaign['id']}/turns",
            json={"role": "user", "content": "Осматриваю объявления."},
        )
    assert turn.status_code == 200, turn.text
    assert "Утро начинается" in turn.text

    debugger = client.get(
        f"/api/campaigns/{campaign['id']}/debugger"
    ).json()
    assert debugger["session_zero"]["status"] == "completed"
    assert debugger["health"]["session_zero_incomplete"] == 0
    assert debugger["health"]["character_card_missing_fields"] == 0
