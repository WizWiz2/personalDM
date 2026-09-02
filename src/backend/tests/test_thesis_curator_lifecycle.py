from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.memory_operations import MemoryOperationsService
from app.services.thesis_curator import ThesisCurator


class FakeSceneRepository:
    def __init__(self, theses):
        self.theses = theses
        self.updates = []
        self.created = []

    async def list_theses_by_scene(self, scene_id, active_only=True):
        return [item for item in self.theses if not active_only or item.status == "active"]

    async def update_thesis(self, thesis_id, update):
        self.updates.append((thesis_id, update.status))
        for thesis in self.theses:
            if thesis.id == thesis_id and update.status is not None:
                thesis.status = update.status
        return True

    async def create_thesis(self, scene_id, data, source_turn_id=None):
        self.created.append((scene_id, data, source_turn_id))
        created = thesis(priority=data.priority)
        created.thesis_type = data.thesis_type.value
        created.text = data.text
        created.visibility = data.visibility
        created.related_entity_ids = data.related_entity_ids
        self.theses.append(created)
        return created


class FakeSession:
    async def flush(self):
        return None


class AuditableFakeSession(FakeSession):
    async def execute(self, *_args, **_kwargs):
        return None


class FullLifecycleFakeSession(AuditableFakeSession):
    async def get(self, *_args, **_kwargs):
        return None

    def add(self, *_args, **_kwargs):
        return None


def thesis(*, priority=5, created_offset=0, pinned=False):
    created = datetime.utcnow() + timedelta(seconds=created_offset)
    return SimpleNamespace(
        id=uuid4(),
        thesis_type="tension",
        related_entity_ids=[],
        priority=priority,
        pinned=pinned,
        text="Группа должна выбрать путь.",
        visibility="dm",
        status="active",
        created_at=created,
        updated_at=created,
    )


def test_russian_paraphrase_similarity_is_stable():
    left = "Группа должна выбрать путь до рассвета."
    same = "Группа должна выбрать путь до рассвета!"
    changed = "Ворота начали необратимо закрываться."

    assert ThesisCurator._similarity(left, same) >= ThesisCurator.PARAPHRASE_SIMILARITY
    assert ThesisCurator._similarity(left, changed) < ThesisCurator.PARAPHRASE_SIMILARITY


@pytest.mark.asyncio
async def test_existing_duplicate_semantic_slot_is_cleaned_without_resolving_keeper():
    older = thesis(priority=4, created_offset=0)
    newer = thesis(priority=7, created_offset=1)
    repo = FakeSceneRepository([older, newer])
    curator = ThesisCurator(FakeSession())
    curator._scene_repo = repo

    result = await curator.reconcile(uuid4(), uuid4(), desired=[])

    assert older.status == "superseded"
    assert newer.status == "active"
    assert result.duplicate_scopes == 1
    assert result.superseded == 1
    assert result.resolved == 0
    assert result.kept == 1
    assert repo.created == []


@pytest.mark.asyncio
async def test_unmentioned_working_memory_survives_curator_pass():
    open_thread = thesis(priority=5)
    repo = FakeSceneRepository([open_thread])
    curator = ThesisCurator(FakeSession())
    curator._scene_repo = repo

    result = await curator.reconcile(uuid4(), uuid4(), desired=[])

    assert open_thread.status == "active"
    assert result.kept == 1
    assert result.resolved == 0


@pytest.mark.asyncio
async def test_close_scene_resolves_pinned_and_unpinned_working_memory():
    pinned = thesis(priority=10, pinned=True)
    mutable = thesis(priority=5, pinned=False)
    repo = FakeSceneRepository([pinned, mutable])
    curator = ThesisCurator(FakeSession())
    curator._scene_repo = repo

    count = await curator.close_scene(uuid4())

    assert count == 2
    assert pinned.status == "resolved"
    assert mutable.status == "resolved"


@pytest.mark.asyncio
async def test_curator_does_not_call_old_scope_based_record_reconcile(monkeypatch):
    calls = []

    async def record_reconcile(self, scene_id, source_turn_id, desired):
        calls.append((scene_id, source_turn_id, desired))

    monkeypatch.setattr(MemoryOperationsService, "record_reconcile", record_reconcile)
    repo = FakeSceneRepository([thesis()])
    curator = ThesisCurator(AuditableFakeSession())
    curator._scene_repo = repo

    await curator.reconcile(uuid4(), uuid4(), desired=[])

    assert calls == []


@pytest.mark.asyncio
async def test_curator_records_scene_closure_when_lifecycle_storage_is_available(monkeypatch):
    calls = []

    async def record_closed_scene(self, scene_id):
        calls.append(scene_id)

    monkeypatch.setattr(MemoryOperationsService, "record_closed_scene", record_closed_scene)
    scene_id = uuid4()
    repo = FakeSceneRepository([thesis(pinned=True), thesis()])
    curator = ThesisCurator(FullLifecycleFakeSession())
    curator._scene_repo = repo

    await curator.close_scene(scene_id)

    assert calls == [scene_id]
