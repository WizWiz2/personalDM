"""QA-302 regressions: bootstrap."""
import pytest


@pytest.mark.asyncio
async def test_bootstrap_does_not_invent_an_unspecified_physical_item(db_session):
    from app.db.repositories.entity_repo import EntityRepository
    from app.services.playable_bootstrap import PlayableBootstrapService
    from tests.test_live_world_playability_p1 import _start

    cid, room, hero, scene = await _start(db_session)
    await PlayableBootstrapService(db_session).ensure(cid, scene.id,
        player_character_id=hero.id, starting_location_id=room.id,
        starting_situation="Наблюдаю утреннюю жизнь города и ищу работу.")
    assert not await EntityRepository(db_session).list_by_campaign(cid, "item")

