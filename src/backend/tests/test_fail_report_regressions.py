import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.config import settings
from app.db.repositories.campaign_repo import CampaignRepository
from app.db.repositories.entity_repo import EntityRepository
from app.db.repositories.location_repo import LocationRepository
from app.db.repositories.scene_repo import SceneRepository
from app.models.campaign import CampaignCreate
from app.models.character import CharacterCreate
from app.models.location import LocationCreate
from app.models.scene import SceneCreate
from app.models.turn import ChatMessage
from app.models.turn_authority import PlannedNpcIntroduction, TurnAuthority
from app.providers.llm_provider import LLMProvider, LLMProviderError
from app.providers.local_inference_queue import local_inference_slot
from app.services.turn_authority_resolvers import AuthorityResolutionError, NpcIntroductionResolver
from app.services.turn_outcome_materializer import TurnOutcomeMaterializer
from app.services.role_model_router import ModelRole, RoleModelRouter, RoleModelSelection
from app.services.turn_outcome_resolver import (
    OutcomeNpcIntroductionDraft,
    _profile_wire_model,
    _travel_wire_model,
)


def _npc(**updates):
    return {
        "canonical_name": "Охранник",
        "role": "охранник",
        "temporary_name": True,
        "description": "Сотрудник охраны в простой серой форме стоит возле двери.",
        "appearance": "На поясе рация, на груди жетон, в руках небольшой планшет.",
        "reason": "Игрок обращается к охраннику у входа.",
        **updates,
    }


PROFILE = "Светлое помещение с широкими окнами и несколькими длинными столами вдоль стен. У двери стоят скамейки."


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "case", ["travel_index", "travel_evidence", "profile", "long_role", "long_name", "identity"]
)
async def test_invalid_semantics_are_repaired_at_provider_boundary(monkeypatch, case):
    if case.startswith("travel"):
        wire = _travel_wire_model(1, evidence="Дверь заперта.")
        good = {
            "obstacles": [
                {"action_index": 0, "blocking_reason": "Замок", "evidence_quote": "Дверь заперта."}
            ]
        }
        bad = json.loads(json.dumps(good))
        bad["obstacles"][0].update(
            {"action_index": 1}
            if case == "travel_index"
            else {"evidence_quote": "Охрана не пускает."}
        )
    elif case == "profile":
        wire = _profile_wire_model(1, action_indices=[2])
        good = {"patches": [{"action_index": 2, "profile": PROFILE}]}
        bad = {"patches": [{"action_index": 0, "profile": PROFILE}]}
    else:
        wire = OutcomeNpcIntroductionDraft
        good = _npc()
        bad = (
            _npc(canonical_name="Описание вместо имени. " * 8)
            if case == "long_name"
            else _npc(role="Описание вместо роли. " * 8 if case == "long_role" else "未知角色")
        )
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(
            200,
            json={
                "message": {"content": json.dumps(bad if len(requests) == 1 else good)},
                "done": True,
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    provider = LLMProvider()
    config = SimpleNamespace(
        base_url="http://localhost:11434/v1", model_name="test", context_window=4096
    )
    result = await provider.generate_json(
        [ChatMessage(role="user", content="Return JSON")], config, response_model=wire
    )
    assert wire.model_validate(result) == wire.model_validate(good)
    assert len(requests) == 2
    assert provider.last_telemetry["attempt"] == 2
    assert len(requests[1]["messages"]) > len(requests[0]["messages"])


@pytest.mark.asyncio
async def test_unrepairable_blocker_still_fails_closed(monkeypatch):
    calls = []

    def respond(request):
        calls.append(request)
        return httpx.Response(
            200,
            json={
                "message": {
                    "content": json.dumps(
                        {
                            "obstacles": [
                                {
                                    "action_index": 0,
                                    "blocking_reason": "Invented guard",
                                    "evidence_quote": None,
                                }
                            ]
                        }
                    )
                }
            },
        )

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: client(transport=httpx.MockTransport(respond), **kwargs),
    )
    with pytest.raises(LLMProviderError, match="verbatim"):
        await LLMProvider().generate_json(
            [],
            SimpleNamespace(
                base_url="http://localhost:11434/v1", model_name="test", context_window=4096
            ),
            response_model=_travel_wire_model(1),
        )
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_generic_npcs_in_different_places_materialize_without_teleport(db_session):
    campaign_id = uuid4()
    await CampaignRepository(db_session).create(campaign_id, CampaignCreate(name="Neon regression"))
    locations = LocationRepository(db_session)
    old = await locations.create(campaign_id, LocationCreate(canonical_name="Бар"))
    new = await locations.create(campaign_id, LocationCreate(canonical_name="Переулок"))
    scene = await SceneRepository(db_session).create(
        campaign_id, SceneCreate(title="У двери", location_id=new.id)
    )
    entities = EntityRepository(db_session)
    original = await entities.create_character(
        campaign_id,
        CharacterCreate(
            canonical_name="Охранник",
            current_location_id=old.id,
            custom_fields={"temporary_name": True, "role": "охранник"},
        ),
    )
    resolver = NpcIntroductionResolver(db_session)
    intro = PlannedNpcIntroduction(**_npc())
    resolved = await resolver.resolve(
        campaign_id=campaign_id, introductions=[intro], present_names=[], target_location_id=new.id
    )
    assert resolved.new_introductions[0].canonical_name == "Охранник 2"
    authority = TurnAuthority(
        campaign_id=campaign_id,
        trigger_turn_id=uuid4(),
        player_input="Спрашиваю охранника",
        target_scene_id=scene.id,
        allowed_new_npcs=resolved.new_introductions,
    )
    outcome = await TurnOutcomeMaterializer(db_session).materialize(
        authority, source_turn_id=uuid4()
    )
    newcomer = await entities.get_character(outcome.introduced_character_ids[0])
    assert newcomer.current_location_id == new.id
    assert (await entities.get_character(original.id)).current_location_id == old.id
    repeated = await resolver.resolve(
        campaign_id=campaign_id, introductions=[intro], present_names=[], target_location_id=new.id
    )
    assert repeated.new_introductions == []
    assert repeated.existing_arrivals[0].entity_id == newcomer.id

    # A stable name at another location must still be rejected, not cloned or moved.
    named = await entities.create_character(
        campaign_id, CharacterCreate(canonical_name="Илья", current_location_id=old.id)
    )
    with pytest.raises(AuthorityResolutionError, match="структурного перемещения"):
        await resolver.resolve(
            campaign_id=campaign_id,
            introductions=[
                PlannedNpcIntroduction(
                    **_npc(
                        canonical_name=named.canonical_name,
                        temporary_name=False,
                        personal_name_evidence="Илья",
                    )
                )
            ],
            present_names=[],
            target_location_id=new.id,
        )


@pytest.mark.asyncio
async def test_local_queue_prioritizes_live_work_and_releases_cancelled_waiters():
    order = []

    async def work(name, background=False):
        async with local_inference_slot("http://127.0.0.1:11434/v1", background=background):
            order.append(name)

    async with local_inference_slot("http://localhost:11434/v1"):
        background = asyncio.create_task(work("memory", True))
        cancelled = asyncio.create_task(work("cancelled"))
        foreground = asyncio.create_task(work("planner"))
        await asyncio.sleep(0)
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        assert order == []
    await asyncio.gather(background, foreground)
    assert order == ["planner", "memory"]
    async with local_inference_slot("http://localhost:11434/v1"):
        pass


@pytest.mark.asyncio
async def test_cancelled_slot_grant_does_not_leak_capacity():
    async def waiting():
        async with local_inference_slot("http://localhost:11434/v1"):
            pass

    async with local_inference_slot("http://localhost:11434/v1"):
        task = asyncio.create_task(waiting())
        await asyncio.sleep(0)
    task.cancel()  # slot granted but waiter has not resumed
    with pytest.raises(asyncio.CancelledError):
        await task
    async with asyncio.timeout(1):
        await waiting()


@pytest.mark.asyncio
async def test_control_execution_budget_starts_after_local_queue(monkeypatch):
    calls = []

    async def run_with_budget(request, *, timeout):
        calls.append(timeout)
        return await request

    class Provider:
        last_telemetry = {}

        async def generate_json(self, *args, **kwargs):
            return {"ok": True}

    monkeypatch.setattr(asyncio, "wait_for", run_with_budget)
    config = SimpleNamespace(base_url="http://localhost:11434/v1", model_name="control")
    selection = RoleModelSelection(ModelRole.PLANNER, config, None, config, None, "test")
    provider = Provider()
    async with local_inference_slot(config.base_url):
        request = asyncio.create_task(RoleModelRouter(None).generate_json(provider, selection, []))
        await asyncio.sleep(0)
        assert calls == []
    assert await request == {"ok": True}
    assert len(calls) == 1
    assert "queue_wait_ms" in provider.last_telemetry


@pytest.mark.asyncio
async def test_cloud_requests_are_not_queued_behind_local_inference():
    async with local_inference_slot("http://localhost:11434/v1"):
        async with local_inference_slot("https://cloud.example/v1") as wait:
            assert wait == 0


@pytest.mark.asyncio
async def test_queue_timeout_is_distinct_and_does_not_start_provider(monkeypatch):
    monkeypatch.setattr(settings, "LOCAL_LLM_QUEUE_TIMEOUT_SECONDS", 1.0)

    class Provider:
        last_telemetry = {}

        async def generate_json(self, *args, **kwargs):
            pytest.fail("Expired queued request must not reach transport")

    config = SimpleNamespace(base_url="http://localhost:11434/v1", model_name="control")
    selection = RoleModelSelection(ModelRole.PLANNER, config, None, config, None, "test")
    provider = Provider()
    async with local_inference_slot(config.base_url):
        with pytest.raises(LLMProviderError, match="queue wait budget"):
            await RoleModelRouter(None).generate_json(provider, selection, [])
    assert provider.last_telemetry["status"] == "local_queue_timeout"
    async with asyncio.timeout(1):
        async with local_inference_slot(config.base_url):
            pass


@pytest.mark.asyncio
async def test_closing_narration_closes_transport_and_releases_local_slot(monkeypatch):
    closed = []

    async def stream(*args, **kwargs):
        try:
            yield "Начало"
            await asyncio.Event().wait()
        finally:
            closed.append(True)

    provider = LLMProvider()
    monkeypatch.setattr(provider, "_generate_stream", stream)
    config = SimpleNamespace(base_url="http://localhost:11434/v1", model_name="narrator")
    narration = provider.generate_stream([], config)
    assert await anext(narration) == "Начало"
    await narration.aclose()
    assert closed == [True]
    async with asyncio.timeout(1):
        async with local_inference_slot(config.base_url):
            pass
