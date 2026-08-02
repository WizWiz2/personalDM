import sqlite3
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.fact_repo import FactRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate
from app.models.fact import FactCreate
from app.models.scene import SceneCreate
from app.services.context_compiler import ContextCompiler
from tests import run_realistic_simulation_v2 as runtime
from tests import simulation_quality_controls as quality
from tests.simulation_database import current_revision, upgrade_simulation_database
from tests.simulation_dynamic_campaign import (
    CampaignCatalog,
    GeneratedArc,
    catalog_summary,
)
from tests.simulation_scenario import ObjectiveCriterion, ScenarioPhase


def generated_arc_payload():
    return {
        "arc_title": "Пепельная переправа",
        "premise": "Экспедиция должна пройти через город, который каждую ночь меняет улицы и цену выхода.",
        "terminal": False,
        "npcs": [
            {
                "name": "Мира",
                "concept": "Картограф, чьи карты меняются вместе с городом; скрывает, что одна карта написана её кровью.",
                "campaign_role": "проводник и источник проверяемых маршрутов",
                "tone": "сдержанная, точная, не любит обещаний",
            },
            {
                "name": "Орсен",
                "concept": "Сборщик пошлин, пытающийся освободить семью из долгового дома.",
                "campaign_role": "антагонист-посредник с личной ставкой",
                "tone": "вежливый, усталый, цепкий",
            },
        ],
        "phases": [
            {
                "slug": "ash_gate",
                "title": "Пепельные ворота",
                "location_description": "Каменная арка стоит посреди площади, а пепел ползёт против ветра к закрытым створкам.",
                "mood": "настороженность",
                "tension": "растущий долг",
                "objective": "Получить законный или рискованный способ пройти через ворота до ночной перестройки города.",
                "introduced_npcs": ["Мира", "Орсен"],
                "active_npcs": ["Мира", "Орсен"],
                "opening_theses": [
                    {
                        "thesis_type": "intention",
                        "text": "Группа должна выбрать способ оплаты прохода.",
                        "priority": 9,
                        "visibility": "public",
                        "related_names": [],
                    },
                    {
                        "thesis_type": "tension",
                        "text": "Орсен знает, что цена вырастет после заката.",
                        "priority": 7,
                        "visibility": "public",
                        "related_names": ["Орсен"],
                    },
                ],
                "pulses": [
                    {
                        "at_fraction": 0.3,
                        "event": "Створки начинают закрываться раньше расписания.",
                        "thesis": {
                            "thesis_type": "tension",
                            "text": "До закрытия осталось несколько минут.",
                            "priority": 9,
                            "visibility": "public",
                            "related_names": [],
                        },
                    },
                    {
                        "at_fraction": 0.7,
                        "event": "Долговой дом присылает за Орсеном двух писцов.",
                        "thesis": {
                            "thesis_type": "unresolved_beat",
                            "text": "Сделка с Орсеном теперь затрагивает его семью.",
                            "priority": 8,
                            "visibility": "public",
                            "related_names": ["Орсен"],
                        },
                    },
                ],
                "director_note": "Закончить конкретным пересечением ворот или явным провалом, а не обещанием вернуться к решению.",
                "completion_criteria": [
                    {
                        "key": "passage_method_chosen",
                        "description": "Группа выбрала и зафиксировала способ пройти ворота.",
                        "allowed_change_types": ["fact", "event", "knowledge"],
                    },
                    {
                        "key": "gate_crossed",
                        "description": "Элдон и группа физически оказались за воротами.",
                        "allowed_change_types": ["movement", "event"],
                    },
                ],
                "min_turns": 5,
                "max_turns": 14,
            },
            {
                "slug": "moving_streets",
                "title": "Движущиеся улицы",
                "location_description": "Дома скользят по рельсам под мостовой, разрывая знакомые переулки и открывая новые дворы.",
                "mood": "дезориентация",
                "tension": "неизбежная перестройка",
                "objective": "Найти устойчивый ориентир и добраться до долгового дома.",
                "introduced_npcs": [],
                "active_npcs": ["Мира"],
                "opening_theses": [
                    {
                        "thesis_type": "intention",
                        "text": "Нужен ориентир, который не движется вместе с домами.",
                        "priority": 9,
                        "visibility": "public",
                        "related_names": ["Мира"],
                    },
                    {
                        "thesis_type": "visual_state",
                        "text": "Колокольня остаётся на месте, пока кварталы вращаются вокруг неё.",
                        "priority": 7,
                        "visibility": "public",
                        "related_names": [],
                    },
                ],
                "pulses": [
                    {
                        "at_fraction": 0.3,
                        "event": "Переулок складывается и отрезает обратный путь.",
                        "thesis": {
                            "thesis_type": "tension",
                            "text": "Вернуться к воротам прежним путём уже нельзя.",
                            "priority": 8,
                            "visibility": "public",
                            "related_names": [],
                        },
                    },
                    {
                        "at_fraction": 0.7,
                        "event": "Карта Миры начинает показывать чужой маршрут.",
                        "thesis": {
                            "thesis_type": "secret",
                            "text": "Карта связана с человеком внутри долгового дома.",
                            "priority": 8,
                            "visibility": "dm",
                            "related_names": ["Мира"],
                        },
                    },
                ],
                "director_note": "Проверить ориентирование и цену доверия к карте.",
                "completion_criteria": [
                    {
                        "key": "stable_landmark_found",
                        "description": "Группа установила ориентир, который не меняется.",
                        "allowed_change_types": ["fact", "knowledge", "event"],
                    },
                    {
                        "key": "debt_house_reached",
                        "description": "Группа добралась до долгового дома.",
                        "allowed_change_types": ["movement", "event"],
                    },
                ],
                "min_turns": 5,
                "max_turns": 16,
            },
        ],
    }


def test_generated_catalog_converts_to_runtime_scenario():
    catalog = CampaignCatalog(
        seed="unit",
        arcs=[GeneratedArc.model_validate(generated_arc_payload())],
    )
    phases = catalog.runtime_phases()
    assert len(phases) == 2
    assert phases[0].completion_criteria[1].key == "gate_crossed"
    assert phases[0].max_turns == 14
    assert set(catalog.runtime_npcs()) == {"Мира", "Орсен"}
    assert catalog_summary(catalog)["phases"] == 2


def test_objective_contract_rejects_pretty_but_unsupported_resolution():
    phase = ScenarioPhase(
        slug="gate",
        title="Gate",
        location_description="A gate",
        mood="tense",
        tension="high",
        objective="Cross the gate",
        introduced_npcs=(),
        active_npcs=(),
        opening_theses=(),
        pulses=(),
        director_note="Cross it",
        completion_criteria=(
            ObjectiveCriterion(
                "method",
                "A passage method is chosen",
                ("fact", "event"),
            ),
            ObjectiveCriterion(
                "crossed",
                "The party crossed",
                ("movement", "event"),
            ),
        ),
    )
    phase_runtime = SimpleNamespace(
        phase=phase,
        durable_changes=["fact: выбран путь через служебную арку"],
        criteria_met=set(),
    )
    evaluation = runtime.ObjectiveEvaluation(
        status="resolved",
        evidence="Герои поняли гармонию ворот",
        criteria_met=["method", "crossed"],
        criterion_evidence={
            "method": "fact: выбран путь через служебную арку",
            "crossed": "красивое описание света",
        },
    )
    checked = quality.enforce_objective_contract(evaluation, phase_runtime)
    assert checked.status == "progressing"
    assert checked.criteria_met == ["method"]
    assert "crossed" in checked.evidence


def test_simulation_database_runs_real_alembic_chain(tmp_path):
    path = tmp_path / "simulation.db"
    revision = upgrade_simulation_database(path)
    assert revision == "a1b2c3d4e5f6"
    assert current_revision(path) == revision
    with sqlite3.connect(path) as connection:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(facts)").fetchall()
        }
        assert {"scope", "scene_id"} <= columns
        link_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(scene_location_links)"
            ).fetchall()
        }
        assert {"scene_id", "location_id"} <= link_columns
        transition_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(scene_transitions)"
            ).fetchall()
        }
        assert {
            "source_scene_id",
            "target_scene_id",
            "transition_type",
            "status",
            "trigger_turn_id",
        } <= transition_columns
        setup_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(campaign_setups)"
            ).fetchall()
        }
        assert {
            "campaign_id",
            "status",
            "setting_name",
            "starting_location_id",
            "boundaries_confirmed",
            "completed_at",
        } <= setup_columns
        scene_state_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(scene_runtime_states)"
            ).fetchall()
        }
        assert {
            "scene_id",
            "world_time_label",
            "world_time_order",
            "scene_goal",
            "active_conflict",
        } <= scene_state_columns
        exit_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(location_exits)"
            ).fetchall()
        }
        assert {
            "campaign_id",
            "from_location_id",
            "to_location_id",
            "label",
            "discovered",
            "active",
        } <= exit_columns
        sequence_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(action_sequences)"
            ).fetchall()
        }
        assert {
            "trigger_turn_id",
            "source_scene_id",
            "final_scene_id",
            "planned_steps",
            "completed_steps",
            "blocked_step_index",
        } <= sequence_columns
        step_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(action_steps)"
            ).fetchall()
        }
        assert {
            "sequence_id",
            "step_index",
            "action_type",
            "resolution",
            "safe_mundane",
            "status",
            "transition_id",
        } <= step_columns
        bridge_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(scene_bridges)"
            ).fetchall()
        }
        assert {
            "transition_id",
            "source_scene_id",
            "target_scene_id",
            "status",
            "previous_scene_summary",
            "carried_goals",
            "unresolved_threads",
            "departed_participant_ids",
            "negative_placement_facts",
        } <= bridge_columns
        validation_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(narration_validation_runs)"
            ).fetchall()
        }
        assert {
            "trigger_turn_id",
            "assistant_turn_id",
            "scene_id",
            "status",
            "draft_text",
            "final_text",
            "attempts_json",
            "violation_count",
            "repair_attempts",
            "validator_model_name",
            "failure_reason",
        } <= validation_columns
        profile_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(fact_memory_profiles)"
            ).fetchall()
        }
        assert {
            "fact_id",
            "memory_kind",
            "subject_entity_id",
        } <= profile_columns
        detail_columns = {
            row[1]
            for row in connection.execute(
                "PRAGMA table_info(narrative_details)"
            ).fetchall()
        }
        assert {
            "campaign_id",
            "scene_id",
            "source_turn_id",
            "subject_entity_id",
            "detail_type",
            "text",
            "visibility",
            "turn_window",
        } <= detail_columns


@pytest.mark.asyncio
async def test_scene_fact_does_not_leak_into_later_scene(db_session: AsyncSession):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(
        campaign_id,
        CampaignCreate(name="Scoped facts"),
    )
    first = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Первый овраг"),
    )
    second = await SceneRepository(db_session).create(
        campaign_id,
        SceneCreate(title="Кузница"),
    )
    facts = FactRepository(db_session)
    await facts.create(
        campaign_id,
        FactCreate(
            subject="Край оврага",
            predicate="содержит",
            object_value="металлическую перемычку",
            visibility="public",
            scope="scene",
            scene_id=first.id,
        ),
    )
    await facts.create(
        campaign_id,
        FactCreate(
            subject="Экспедиция",
            predicate="ищет",
            object_value="выход",
            visibility="public",
            scope="campaign",
        ),
    )
    await db_session.commit()

    first_messages, _ = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=first.id,
    )
    second_messages, _ = await ContextCompiler(db_session).compile_context(
        campaign_id=campaign_id,
        scene_id=second.id,
    )
    first_context = "\n".join(item.content for item in first_messages)
    second_context = "\n".join(item.content for item in second_messages)
    assert "металлическую перемычку" in first_context
    assert "металлическую перемычку" not in second_context
    assert "Экспедиция ищет выход" in first_context
    assert "Экспедиция ищет выход" in second_context


def test_real_benchmark_defaults_to_llm_player():
    assert settings.SIM_PLAYER_MODE == "llm"
