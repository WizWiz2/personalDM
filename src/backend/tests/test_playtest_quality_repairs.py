"""Repairs from the Соль на ресницах playtest.

Player-facing text must not be a fact ledger or an English exception.
A missing addressee or a thing-name must not abort the turn or become a person.
"""

from uuid import uuid4

import pytest
from pydantic import ValidationError
from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.play_surface_contract import snap_near_names
from app.services.turn_outcome_resolver import _outcome_wire_model


def test_publication_drops_fact_ledger_and_obligation_beat():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Лира смотрит на колокол.",
        observable_consequences=["Лира касается колокола."],
        established_state=[
            "Лира Вереск — рисует: на бумаге.",
            "ставни открыты да",
        ],
        character_beats=[
            "Палка получает прямое обращение и даёт ответ, отказывает, уклоняется или жестом сообщает ответ."
        ],
        scene_disposition="actor_turn",
        acting_character_id=uuid4(),
    )
    text = NarrationPublicationGuard.render_authority(authority)
    assert "Лира касается колокола." in text
    assert "ставни открыты да" in text
    assert "рисует" not in text
    assert "прямое обращение" not in text


def test_repeated_question_requires_regeneration_without_inventing_ignorance():
    model = _outcome_wire_model(
        0,
        question_count=1,
        questions=["Куда смотрит стрелка?"],
        requires_response=True,
    )
    with pytest.raises(ValidationError, match="a repeated question is not an answer"):
        model.model_validate(
            {
                "action_outcomes": [],
                "npc_introductions": [],
                "resolution": "success",
                "direct_response": "Смотрю на латунь.",
                "response_speaker_name": None,
                "response_after_action_index": None,
                "response_revealed_name": None,
                "response_name_evidence": None,
                "question_responses": [
                    {
                        "question_index": 0,
                        "disposition": "answer",
                        "words": "Куда смотрит стрелка?",
                    }
                ],
            }
        )



def _authority(**kwargs) -> TurnAuthority:
    base = dict(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Лира смотрит.",
        player_character_name="Лира Вереск",
        observable_consequences=["Лира стоит на кромке."],
        scene_disposition="actor_turn",
        acting_character_id=uuid4(),
    )
    base.update(kwargs)
    return TurnAuthority(**base)


def test_publish_repairs_a_near_miss_of_the_hero_name():
    authority = _authority(player_character_name="Лира Вереск")
    published, guard = NarrationPublicationGuard.publish(
        authority,
        "Леры Вереск стоит на кромке.",
        NarrationValidationResult(verdict="pass", summary="ok", violations=[]),
    )
    assert guard["validated_surface"] is True
    assert "Лиры Вереск" in published
    assert "Леры" not in published
    assert snap_near_names("Леры Вереск", ["Лира Вереск"]) == "Лиры Вереск"


def test_player_stream_hides_the_technical_cause():
    from app.services.turn_runner import player_facing_stream_item

    raw = (
        "\n[Generation failed: frozen intent pipeline failed: "
        "travel adjudication has no selected endpoint]"
    )
    item = player_facing_stream_item(raw)
    assert "travel adjudication" not in item
    assert "ход не опубликован" in item
    assert "Generation failed" in item
    assert player_facing_stream_item("Мастер молчит.") == "Мастер молчит."
