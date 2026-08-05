from app.models.session_zero_interview import (
    SessionZeroInterviewDraft,
    SessionZeroInterviewPatch,
)
from app.services.session_zero_interview import SessionZeroInterviewService


def test_patch_preserves_complete_accumulated_draft():
    draft = SessionZeroInterviewDraft()
    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate(
            {
                "world": {
                    "setting_name": "Shadowrun",
                    "genre": "киберпанк с магией",
                    "rules_system": "Shadowrun",
                    "world_summary": "Корпорации, Матрица, магия и раннеры.",
                }
            }
        ),
    )
    draft = SessionZeroInterviewService._apply_patch(
        draft,
        SessionZeroInterviewPatch.model_validate(
            {
                "character": {
                    "name": "Кабуто",
                    "appearance": "Обожжённое лицо скрыто шлемом-маской.",
                }
            }
        ),
    )

    assert draft.world.setting_name == "Shadowrun"
    assert draft.world.rules_system == "Shadowrun"
    assert draft.character.name == "Кабуто"
    assert "Обожжённое лицо" in draft.character.appearance
