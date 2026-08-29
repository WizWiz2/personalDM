from app.models.narration_validation import NarrationValidationResult, NarrationViolation
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narrator_quality_recovery_guard import literary_surgical_repair_candidate
from app.services.prompt_policy import CURRENT_PROMPT_POLICY


def _rejected(evidence: str) -> NarrationValidationResult:
    return NarrationValidationResult(
        verdict="repair_required",
        summary="Локальное нарушение agency.",
        violations=[
            NarrationViolation(
                violation_type="player_agency",
                severity="error",
                evidence=evidence,
                correction="Удалить только придуманное действие героя.",
            )
        ],
    )


def _paragraphs(text: str) -> list[str]:
    return [value.strip() for value in text.split("\n\n") if value.strip()]


def test_player_facing_prompt_requires_literary_scene_not_engine_receipt():
    contract = CURRENT_PROMPT_POLICY.narrator_surface_contract

    assert CURRENT_PROMPT_POLICY.version == "narrator-v5-literary"
    assert "2–3 cohesive prose paragraphs" in contract
    assert "2–3 relevant sensory channels" in contract
    assert "short piece of fiction" in contract
    assert "bare quote" in contract
    assert "generic padding" in contract


def test_actor_contract_is_explicitly_intermediate_not_final_surface():
    contract = CURRENT_PROMPT_POLICY.player_control_contract

    assert "not the finished player-facing literary" in contract
    assert "final Narrator" in contract
    assert "complete scene" in contract


def test_literary_surgical_repair_preserves_paragraphs_and_grounded_texture():
    bad = "Вы решаете немедленно отойти к воротам."
    candidate = (
        "Тёплый воздух пахнет пылью и старым деревом; где-то за стеной мерно звякает цепь. "
        "Тамар не спешит отводить взгляд.\n\n"
        "— Это мой груз, — говорит она и кладёт ладонь на край ящика. "
        f"{bad} "
        "Крышка едва слышно поскрипывает под её пальцами.\n\n"
        "Снаружи порыв ветра шевелит полог, и в комнату на миг тянет прохладой. "
        "Тамар остаётся рядом с ящиком, ожидая вашей реакции."
    )

    repaired, metadata = literary_surgical_repair_candidate(
        NarrationPublicationGuard,
        candidate,
        _rejected(bad),
    )

    assert repaired is not None
    assert bad not in repaired
    assert len(_paragraphs(repaired)) == 3
    assert "пахнет пылью" in repaired
    assert "едва слышно поскрипывает" in repaired
    assert "тянет прохладой" in repaired
    assert "— Это мой груз" in repaired
    assert metadata["status"] == "candidate"
    assert metadata["retained_ratio"] >= 0.70


def test_literary_surgical_repair_refuses_to_publish_scene_after_major_amputation():
    bad = (
        "Вы решаете, что Тамар врёт, делаете несколько шагов к двери, затем обещаете вернуться "
        "с охраной и мысленно прикидываете, как лучше её напугать."
    )
    candidate = (
        "В тесной комнате пахнет смолой, а с улицы доносится глухой стук тележных колёс. "
        "Тамар стоит возле груза, не заслоняя проход.\n\n"
        f"{bad} {bad} {bad}\n\n"
        "Она проводит большим пальцем по шершавой доске ящика. "
        "Холодный сквозняк касается рук и тут же стихает."
    )

    repaired, metadata = literary_surgical_repair_candidate(
        NarrationPublicationGuard,
        candidate,
        _rejected(bad),
    )

    assert repaired is None
    assert metadata["status"] == "skipped"
    assert metadata["reason"] == "literary_surface_degraded"
    assert metadata["retained_ratio"] < 0.70
