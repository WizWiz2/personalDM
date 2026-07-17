from datetime import datetime, timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

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
        return None


class FakeSession:
    async def flush(self):
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
async def test_existing_duplicate_scope_is_fully_cleaned():
    older = thesis(priority=4, created_offset=0)
    newer = thesis(priority=7, created_offset=1)
    repo = FakeSceneRepository([older, newer])
    curator = ThesisCurator(FakeSession())
    curator._scene_repo = repo

    result = await curator.reconcile(uuid4(), uuid4(), desired=[])

    assert older.status == "superseded"
    assert newer.status == "resolved"
    assert result.duplicate_scopes == 1
    assert result.superseded == 1
    assert result.resolved == 1
    assert repo.created == []


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
