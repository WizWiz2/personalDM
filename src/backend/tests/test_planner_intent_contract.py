from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_contact_identity_is_owned_by_semantic_plan_reviewer():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "CONTACT/IDENTITY" in prompt
    assert "previously" in prompt
    assert "unknown physical responder" in prompt
    assert "npc_introductions" in prompt
    assert "negative outcome must be explicit" in prompt


def test_destination_movement_is_owned_by_semantic_plan_reviewer():
    prompt = TurnAuthorityPlanner.SEMANTIC_REVIEW_PROMPT

    assert "MOVEMENT/TIME" in prompt
    assert "structured transition" in prompt
    assert "focus change" in prompt


def test_contract_issues_does_not_parse_human_language_anymore():
    plan = CoordinatedTurnPlan(player_intent="Проверка", resolution="observation")

    assert TurnAuthorityPlanner.contract_issues(
        plan, "Иду в таверну расспросить информатора"
    ) == []
    assert TurnAuthorityPlanner.contract_issues(
        plan, "Подхожу к двери и трижды стучу"
    ) == []
