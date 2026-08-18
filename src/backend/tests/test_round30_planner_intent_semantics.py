from app.services.player_intent_contract import intent_corresponds
from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_round30_dialogue_paraphrase_does_not_trigger_stale_plan_guard() -> None:
    assert intent_corresponds(
        "Во сколько это было?",
        "Уточнить время происшествия.",
    )
    assert intent_corresponds(
        "Что было необычного в этом месте?",
        "Узнать о странностях той ночи.",
    )
    assert intent_corresponds(
        "Вы видели, кто это сделал?",
        "Получить сведения о возможном виновнике.",
    )


def test_round30_opaque_semantic_summary_is_not_a_lexical_veto() -> None:
    assert intent_corresponds(
        "Осматриваю место происшествия.",
        "Понять обстановку вокруг произошедшего.",
    )
    assert intent_corresponds(
        "Что вы заметили первым?",
        "Получить дополнительные сведения.",
    )


def test_round30_movement_paraphrase_does_not_trigger_stale_plan_guard() -> None:
    assert intent_corresponds(
        "Выхожу из офиса и иду к тому месту, где всё произошло.",
        "Добраться до места происшествия.",
    )
    assert intent_corresponds(
        "Иду дальше по улице, к перекрёстку.",
        "Продолжить путь к перекрёстку.",
    )


def test_stale_high_risk_plan_is_still_rejected() -> None:
    assert not intent_corresponds(
        "Что изображено на старом гербе?",
        "Постучать в дверь и войти внутрь.",
    )
    assert not intent_corresponds(
        "Осматриваю фотографии на столе.",
        "Покинуть офис и отправиться в архив.",
    )
    # Sharing the same object noun must not excuse an extra voluntary interaction.
    assert not intent_corresponds(
        "Осматриваю старую дверь и герб над ней.",
        "Постучать в старую дверь и войти внутрь.",
    )


def test_valid_semantic_movement_plan_passes_full_handoff_contract() -> None:
    player_input = "Выхожу из офиса и иду к тому месту, где всё произошло."
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Добраться до места происшествия.",
            "resolution": "transition",
            "scene_transition": {
                "required": True,
                "transition_type": "location_transition",
                "destination_location": "Место происшествия",
                "scene_title": "Место происшествия",
                "reason": "Игрок явно направился туда.",
            },
            "observable_consequences": [],
            "character_beats": [],
            "canon_constraints": [],
            "new_fact_candidates": [],
            "narration_guidance": [],
            "ending_hook": "",
            "npc_introductions": [],
        }
    )

    issues = TurnAuthorityPlanner.contract_issues(plan, player_input)

    assert not any("player_intent does not correspond" in issue for issue in issues)
    assert not any("explicit destination movement" in issue for issue in issues)


def test_semantic_intent_does_not_excuse_missing_structured_movement() -> None:
    player_input = "Выхожу из офиса и иду к тому месту, где всё произошло."
    plan = CoordinatedTurnPlan.model_validate(
        {
            "player_intent": "Добраться до места происшествия.",
            "resolution": "observation",
            "observable_consequences": ["Игрок собирается в дорогу."],
            "character_beats": [],
            "canon_constraints": [],
            "new_fact_candidates": [],
            "narration_guidance": [],
            "ending_hook": "",
            "npc_introductions": [],
        }
    )

    issues = TurnAuthorityPlanner.contract_issues(plan, player_input)

    assert not any("player_intent does not correspond" in issue for issue in issues)
    assert any("explicit destination movement" in issue for issue in issues)
