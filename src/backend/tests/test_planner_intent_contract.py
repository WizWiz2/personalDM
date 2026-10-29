from app.services.turn_authority_planner import CoordinatedTurnPlan, TurnAuthorityPlanner


def test_contract_issues_does_not_parse_human_language_anymore():
    plan = CoordinatedTurnPlan(player_intent="Проверка", resolution="observation")

    assert TurnAuthorityPlanner.contract_issues(
        plan, "Иду в таверну расспросить информатора"
    ) == []
    assert TurnAuthorityPlanner.contract_issues(
        plan, "Подхожу к двери и трижды стучу"
    ) == []
