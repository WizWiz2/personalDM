from fastapi.testclient import TestClient

from app.models.session_zero_interview import SessionZeroInterviewDraft
from app.services.session_zero_agent import SessionZeroAgent, SessionZeroInterviewService


def _complete(client: TestClient, *, situation: str, starter_npcs: list[dict]):
    campaign = client.post(
        "/api/campaigns",
        json={"name": "Structured starter contacts"},
    ).json()
    location = client.post(
        f"/api/campaigns/{campaign['id']}/locations",
        json={"canonical_name": "Контора над книжной лавкой"},
    ).json()
    hero = client.post(
        f"/api/campaigns/{campaign['id']}/characters",
        json={"canonical_name": "Виктор"},
    ).json()
    interview_state = {
        "version": 9,
        "response_language": "ru",
        "messages": [],
        "draft": {
            "world": {
                "starter_npcs": starter_npcs,
                "starter_presence_confirmed": True,
            },
            "character": {},
        },
        "pending_user_message": None,
        "last_summary": None,
        "last_question_topics": [],
        "delegated_fields": [],
    }
    update = client.put(
        f"/api/campaigns/{campaign['id']}/session-zero",
        json={
            "starting_situation": situation,
            "starting_location_id": location["id"],
            "player_character_id": hero["id"],
            "custom_fields": {"session_zero_interview": interview_state},
        },
    )
    assert update.status_code == 200, update.text
    completed = client.post(
        f"/api/campaigns/{campaign['id']}/session-zero/complete",
        json={},
    )
    assert completed.status_code == 200, completed.text
    return hero, completed.json()


def test_arbitrary_present_role_materializes_without_contact_marker(client: TestClient):
    hero, completed = _complete(
        client,
        situation=(
            "Виктор начинает утро в своей конторе; владелица редкой книжной лавки уже ждёт "
            "его за столом и хочет обсудить пропавшую рукопись."
        ),
        starter_npcs=[
            {
                "role": "владелица редкой книжной лавки",
                "description": "Нервная клиентка, пришедшая лично сообщить о пропаже.",
                "reason": "уже ждёт Виктора в конторе",
                "present_at_start": True,
            }
        ],
    )
    participants = completed["scene"]["participants"]
    assert hero["id"] in participants
    assert len(participants) == 2


def test_confirmed_empty_presence_suppresses_legacy_job_marker_inference(client: TestClient):
    hero, completed = _complete(
        client,
        situation=(
            "Виктор один в своей конторе просматривает письмо с предложением оплачиваемого заказа; "
            "с отправителем ещё только предстоит встретиться."
        ),
        starter_npcs=[],
    )
    assert completed["scene"]["participants"] == [hero["id"]]


def test_future_character_is_not_materialized(client: TestClient):
    hero, completed = _complete(
        client,
        situation="Виктор собирается разыскать пропавшего компаньона владельца галереи.",
        starter_npcs=[
            {
                "role": "пропавший компаньон",
                "reason": "его предстоит найти позже",
                "present_at_start": False,
            }
        ],
    )
    assert completed["scene"]["participants"] == [hero["id"]]


def test_presence_contract_is_required_for_conversational_finalize():
    missing = SessionZeroInterviewService.missing_fields(SessionZeroInterviewDraft())
    assert "world.starter_presence_confirmed" in missing
    assert "world.starter_npcs" in SessionZeroAgent.SYSTEM_PROMPT
    assert "starter_presence_confirmed" in SessionZeroAgent.SYSTEM_PROMPT
