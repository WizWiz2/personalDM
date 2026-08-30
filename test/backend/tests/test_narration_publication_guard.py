from uuid import uuid4

from app.models.narration_validation import NarrationValidationResult
from app.models.turn_authority import TurnAuthority
from app.services.narration_publication_guard import NarrationPublicationGuard


def test_conservative_authority_discards_passing_hallucinated_narration():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Я еду к башням OmniCorp.",
        source_scene_id=uuid4(),
        target_scene_id=uuid4(),
        scene_disposition="stay",
        resolution="uncertain",
        canon_constraints=["Планировщик недоступен."],
    )
    validation = NarrationValidationResult(verdict="pass")

    published, metadata = NarrationPublicationGuard.publish(
        authority,
        "Ты приезжаешь к башням OmniCorp и входишь внутрь.",
        validation,
    )

    assert published == "Пока ничего заметно не меняется."
    assert metadata["candidate_discarded"] is True
    assert metadata["reason"] == "conservative_authority_without_observable_outcome"


def test_authoritative_observable_outcome_still_allows_validated_narration():
    authority = TurnAuthority(
        campaign_id=uuid4(),
        trigger_turn_id=uuid4(),
        player_input="Я ставлю чашку на стойку.",
        scene_disposition="stay",
        resolution="success",
        observable_consequences=["Чашка остаётся на стойке."],
    )
    validation = NarrationValidationResult(verdict="pass")

    published, metadata = NarrationPublicationGuard.publish(
        authority,
        "Чашка остаётся на стойке.",
        validation,
    )

    assert published == "Чашка остаётся на стойке."
    assert metadata["mode"] == "validated_candidate"
