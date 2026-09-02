from types import SimpleNamespace

from app.models.turn_authority import PlannedNpcIntroduction
from app.services.semantic_authority_guard import npc_profile_issues
from app.services.visual_generation import VisualGenerationService


def _plan(*introductions: PlannedNpcIntroduction):
    return SimpleNamespace(npc_introductions=list(introductions))


def test_new_npc_requires_durable_public_description_and_portrait_ready_appearance():
    thin = PlannedNpcIntroduction(
        canonical_name="Мартин Вэнс",
        role="делец",
        description="делец",
        appearance=None,
        reason="Игрок напрямую заговорил с местным делцом, и тот ответил.",
    )

    issues = npc_profile_issues(_plan(thin))

    assert len(issues) == 2
    assert any("public description" in issue for issue in issues)
    assert any("portrait-ready appearance" in issue for issue in issues)


def test_complete_new_npc_profile_passes_machine_minimum():
    complete = PlannedNpcIntroduction(
        canonical_name="Мартин Вэнс",
        role="местный делец",
        description=(
            "Уверенный местный делец средних лет, который держится с подчеркнутой вежливостью "
            "и внимательно следит за реакцией собеседника."
        ),
        appearance=(
            "Худощавый мужчина около сорока пяти лет с аккуратно зачёсанными тёмными волосами, "
            "серым костюмом, бордовым галстуком и заметным золотым перстнем."
        ),
        voice="Негромкий ровный баритон; говорит коротко и без лишних слов.",
        reason="Игрок напрямую заговорил с местным делцом, и тот ответил.",
    )

    assert npc_profile_issues(_plan(complete)) == []


def test_character_portrait_prompt_is_derived_from_saved_character_profile():
    character = SimpleNamespace(
        canonical_name="Мартин Вэнс",
        description="Сдержанный местный делец с подчёркнуто вежливыми манерами.",
        appearance="Худощавый мужчина в сером костюме с бордовым галстуком и золотым перстнем.",
        face_description=None,
        body_description=None,
        immutable_features=None,
    )

    prompt = VisualGenerationService._portrait_prompt(character)

    assert "Мартин Вэнс" in prompt
    assert character.description in prompt
    assert character.appearance in prompt
    assert "no text" in prompt
