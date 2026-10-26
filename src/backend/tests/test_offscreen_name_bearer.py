"""replay16 k3: «Прохор Белов» became a second offscreen twin of the already typed «Белов»."""

import pytest

from app.db.tables import Campaign, Entity
from app.models.player_intent import OutcomeNpcIntroduction
from app.services.turn_intent_pipeline import TurnIntentPlanningPipeline
from app.services.turn_outcome_resolver import TurnOutcomeDecision


def _person(name, *, sent_for=True, temporary=False):
    return OutcomeNpcIntroduction(canonical_name=name, role="речник", reason="Его ищут.",
                                  description="Речник, которого ищут на пристани у Мологи.",
                                  appearance="Никто здесь его не описал; известно только имя.",
                                  temporary_name=temporary, sent_for=sent_for,
                                  personal_name_evidence=None if temporary else name)


@pytest.mark.asyncio
async def test_a_personal_name_already_carried_is_not_a_new_offscreen_person(db_session):
    campaign = Campaign(name="r16")
    db_session.add(campaign)
    await db_session.flush()
    db_session.add(Entity(campaign_id=campaign.id, entity_type="character", canonical_name="Белов",
                          description="Отсутствующий речник"))
    await db_session.flush()
    pipeline = TurnIntentPlanningPipeline.__new__(TurnIntentPlanningPipeline)
    pipeline._session = db_session
    assert await pipeline._uncast(campaign.id, ["Прохор Белов", "Егор"]) == ("Егор",)
    decision = TurnOutcomeDecision(npc_introductions=[
        _person("Прохор Белов"), _person("Речник", temporary=True), _person("Егор")])
    kept = await pipeline._without_carried_offscreen(campaign.id, decision)
    assert [npc.canonical_name for npc in kept.npc_introductions] == ["Речник", "Егор"]


def test_a_spoken_errand_stays_an_executable_request():
    from app.services.player_intent_interpreter import PlayerIntentContractDraft, PlayerIntentInterpreter
    text = "«Пошлите кого-нибудь к Белову»."
    draft = PlayerIntentContractDraft.model_validate({"summary": "Просьба.", "actions": [
        {"evidence_quote": "Пошлите кого-нибудь к Белову", "contribution_kind": "speech",
         "action_type": "other", "intent": "Попросить послать за Беловым.", "expected_effect": "fetch_person"},
        {"evidence_quote": text, "contribution_kind": "speech", "action_type": "other",
         "intent": "Сказать.", "expected_effect": "ordinary"}]})
    interpreter = PlayerIntentInterpreter.__new__(PlayerIntentInterpreter)
    interpreter.audit = []
    interpreter._linguistic_analyzer = type("A", (), {"analyze": lambda self, *a: type("S", (), {
        "action_roles": [], "uniform_action_role": None, "information_request_only": None,
        "information_recipient": None})()})()
    interpreter._apply_ownership(draft, text, None)
    assert [(a.action_type, a.expected_effect) for a in draft.actions] == [("service", "fetch_person")]
