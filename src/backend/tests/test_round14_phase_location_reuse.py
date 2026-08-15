from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

from app.models.entity import EntityCreate, EntityType
from tests.simulation_phase_location_reuse import install_phase_location_reuse


class _Entities:
    def __init__(self, existing=()):
        self.rows = list(existing)
        self.create_calls = 0

    async def list_by_campaign(self, campaign_id, entity_type=None):
        if entity_type is None:
            return list(self.rows)
        return [row for row in self.rows if row.entity_type == entity_type]

    async def create(self, campaign_id, data):
        self.create_calls += 1
        row = SimpleNamespace(
            id=uuid4(),
            entity_type=data.entity_type.value,
            canonical_name=data.canonical_name,
        )
        self.rows.append(row)
        return row


class _Scenes:
    async def list_by_campaign(self, campaign_id):
        return []


def _runtime(existing=()):
    phase = SimpleNamespace(
        title="Старый порт",
        slug="old-port",
        location_description="Мокрые склады и причалы.",
    )

    class Director:
        def __init__(self):
            self.campaign_id = uuid4()
            self.entities = _Entities(existing)
            self.scenes = _Scenes()

        async def enter_phase(self, index, state):
            selected = runtime.PHASES[index]
            return await self.entities.create(
                self.campaign_id,
                EntityCreate(
                    entity_type=EntityType.LOCATION,
                    canonical_name=selected.title,
                    description=selected.location_description,
                    custom_fields={"scenario_phase": selected.slug},
                ),
            )

    runtime = SimpleNamespace(ScenarioDirector=Director, PHASES=[phase])
    return runtime


def test_existing_phase_location_is_reused_without_second_insert():
    existing = SimpleNamespace(
        id=uuid4(),
        entity_type="location",
        canonical_name="Старый порт",
    )
    runtime = _runtime([existing])
    install_phase_location_reuse(runtime)
    director = runtime.ScenarioDirector()

    result = asyncio.run(director.enter_phase(0, SimpleNamespace()))

    assert result.id == existing.id
    assert director.entities.create_calls == 0
    assert len(director.entities.rows) == 1


def test_new_phase_location_is_created_exactly_once():
    runtime = _runtime()
    install_phase_location_reuse(runtime)
    director = runtime.ScenarioDirector()

    result = asyncio.run(director.enter_phase(0, SimpleNamespace()))

    assert result.canonical_name == "Старый порт"
    assert director.entities.create_calls == 1
    assert len(director.entities.rows) == 1


def test_same_name_character_does_not_block_location_creation():
    character = SimpleNamespace(
        id=uuid4(),
        entity_type="character",
        canonical_name="Старый порт",
    )
    runtime = _runtime([character])
    install_phase_location_reuse(runtime)
    director = runtime.ScenarioDirector()

    result = asyncio.run(director.enter_phase(0, SimpleNamespace()))

    assert result.entity_type == "location"
    assert director.entities.create_calls == 1
    assert len(director.entities.rows) == 2
