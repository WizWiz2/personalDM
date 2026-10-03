"""Repairs from the Соль на ресницах playtest.

Player-facing text must not be a fact ledger or an English exception.
A missing addressee or a thing-name must not abort the turn or become a person.
"""

from uuid import uuid4

from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.play_surface_contract import snap_near_names


def test_fallback_publishes_only_the_typed_outcome():
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
    assert text == "Лира касается колокола."


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


def test_memory_name_snapping_repairs_a_near_miss_of_a_known_name():
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
