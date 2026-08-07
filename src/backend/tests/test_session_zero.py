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
    # Since autonomous Session Zero, descriptive world/card fields are diagnostics,
    # not hard gates. Only material objects needed to start play remain blockers.
    assert "setup.starting_situation" in setup["missing_fields"]
    assert "setup.starting_location_id" in setup["missing_fields"]
    assert "setup.player_character_id" in setup["missing_fields"]
    assert "setup.world_anchor" not in setup["missing_fields"]

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

    complete = client.post(
        f"/api/campaigns/{campaign['id']}/session-zero/complete"
    )
    assert complete.status_code == 200, complete.text
    card = complete.json()["character"]
    assert card["canonical_name"] == "Безымянный герой"
    assert card["description"] is None
    assert card["appearance"] is None
    assert card["personality"] is None
    assert card["capabilities"] == []
    assert card["limitations"] == []


def test_session_zero_atomically_creates_playable_start(client: TestClient):
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Серый Брод"},
    ).json()
    location = client.post(
        f"/api/campaigns/{campaign['id']}/locations",
        json={"canonical_name": "Площадь Серого Брода"},
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign['id']}/characters",
        json=full_character_draft("Роуэн"),
    ).json()

    update = client.put(
        f"/api/campaigns/{campaign['id']}/session-zero",
        json={
            "setting_name": "Серый Брод",
            "genre": "низкое фэнтези",
            "premise": "Следопыт ищет пропавшую экспедицию на старом тракте.",
            "tone": "мрачное приключение без безысходности",
            "world_summary": "Пограничный край, старый тракт и исчезнувшая экспедиция.",
            "starting_situation": "Роуэн прибывает на площадь в поисках проводника.",
            "starting_location_id": location["id"],
            "boundaries": ["без сексуального насилия"],
            "boundaries_confirmed": True,
            "player_character_id": hero["id"],
        },
    )
    assert update.status_code == 200, update.text
    assert update.json()["ready_to_complete"] is True

    with patch(
        "app.providers.llm_provider.LLMProvider.generate_stream",
        side_effect=narrator_stream,
    ), patch(
        "app.services.entity_registrar.EntityRegistrar.extract_and_register",
        side_effect=no_entities,
    ), patch(
        "app.services.memory_scribe.MemoryScribe.extract_proposals",
        side_effect=no_proposals,
    ):
        complete = client.post(
            f"/api/campaigns/{campaign['id']}/session-zero/complete"
        )
        assert complete.status_code == 200, complete.text
        setup = complete.json()
        assert setup["status"] == "completed"

        turn = client.post(
            f"/api/campaigns/{campaign['id']}/turns",
            json={"role": "user", "content": "Осматриваю площадь."},
        )
        assert turn.status_code == 200, turn.text
        assert "площади Серого Брода" in turn.text
