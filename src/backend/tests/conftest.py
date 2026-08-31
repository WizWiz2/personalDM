import asyncio
from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.db.engine import Base, get_session
from app.main import app
from app.models.narration_validation import NarrationValidationResult
from app.models.turn import ChatMessage
from app.services.narration_validator import NarrationValidator
from app.services.post_turn_dispatcher import PostTurnDispatcher
from app.services.turn_authority_planner import (
    CoordinatedTurnPlan,
    TurnAuthorityPlanner,
)
from app.services.turn_authority_validator import TurnAuthorityValidator
from app.services.turn_planner import TurnPlan, TurnPlanner

# Use in-memory SQLite database for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


def _coordinated_from_legacy(plan: TurnPlan) -> CoordinatedTurnPlan:
    """Preserve old deterministic test intent while public runtime consumes typed authority."""
    if isinstance(plan, CoordinatedTurnPlan):
        return plan
    if plan.action_sequence.steps:
        disposition = "sequence"
    elif plan.scene_transition.required:
        disposition = plan.scene_transition.transition_type
    else:
        disposition = "stay"
    return CoordinatedTurnPlan(
        **plan.model_dump(mode="python"),
        scene_disposition=disposition,
        npc_introductions=[],
    )


@pytest.fixture(autouse=True)
def mock_turn_planner(request):
    """Keep unrelated endpoint tests offline without pretending this is acceptance coverage.

    Existing invariant tests often patch legacy ``TurnPlanner.plan`` with a precise transition or
    compound plan. The test-only bridge below converts that exact plan into the new typed shape, so
    those tests continue to assert state semantics rather than an implementation class name.

    ``interagent_contract_enforced`` tests keep the real authority planner/hand-off and provide
    their own deterministic model transport.
    """
    legacy_plan = TurnPlan(
        player_intent="Resolve the player's latest action.",
        resolution="observation",
        observable_consequences=["The action produces one visible consequence."],
        character_beats=[],
        canon_constraints=["Do not invent abilities, items, movement, or knowledge."],
        new_fact_candidates=[],
        narration_guidance=["Keep the response grounded and concise."],
        ending_hook="Return a meaningful situation to the player.",
    )

    async def bridge_authority_plan(_self, selection, context_messages, *, latest_user_input=None):
        # Tests may temporarily replace TurnPlanner.plan inside their own `with patch(...)` block.
        # Calling it here deliberately sees that more-specific patch.
        legacy = await TurnPlanner(AsyncMock()).plan(selection, context_messages)
        return _coordinated_from_legacy(legacy)

    authority_enabled = request.node.get_closest_marker("interagent_contract_enforced")
    with patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
        return_value=legacy_plan,
    ):
        if authority_enabled:
            yield
        else:
            with patch.object(
                TurnAuthorityPlanner,
                "plan",
                new=bridge_authority_plan,
            ):
                yield


@pytest.fixture(autouse=True)
def deterministic_narration_validator(request):
    """Unit tests stay offline; old validator fixtures bridge into the new public gate.

    This lets existing tests keep expressing semantic verdicts while
    ``interagent_contract_enforced`` exercises the real TurnAuthorityValidator transport.
    """
    result = NarrationValidationResult(
        verdict="pass",
        summary="Deterministic test harness accepted the mocked narration.",
        violations=[],
    )
    enforce_old = request.node.get_closest_marker("narration_validator_enforced")
    enforce_authority = request.node.get_closest_marker("interagent_contract_enforced")

    old_patch = (
        patch.object(
            NarrationValidator,
            "validate",
            new_callable=AsyncMock,
            return_value=result,
        )
        if not enforce_old
        else None
    )

    async def bridge_authority_validation(_self, selection, authority, candidate_text):
        legacy = NarrationValidator(None, None)
        return await legacy.validate(
            selection,
            [
                ChatMessage(
                    role="system",
                    content="Deterministic compatibility bridge for a typed authority verdict.",
                )
            ],
            candidate_text,
            confirmed_speaker_name=authority.acting_character_name,
        )

    authority_patch = (
        patch.object(
            TurnAuthorityValidator,
            "validate",
            new=bridge_authority_validation,
        )
        if not enforce_authority
        else None
    )

    if old_patch:
        old_patch.start()
    if authority_patch:
        authority_patch.start()
    try:
        yield
    finally:
        if authority_patch:
            authority_patch.stop()
        if old_patch:
            old_patch.stop()


@pytest.fixture(autouse=True)
def deterministic_post_turn_timing(request):
    """Existing tests may inspect memory immediately; production gameplay never waits for it."""
    previous = PostTurnDispatcher.wait_inline_for_tests
    PostTurnDispatcher.wait_inline_for_tests = not bool(
        request.node.get_closest_marker("post_turn_background_enforced")
    )
    try:
        yield
    finally:
        PostTurnDispatcher.wait_inline_for_tests = previous


@pytest.fixture(autouse=True)
def legacy_test_session_zero_bootstrap(request):
    """Do not rewrite every pre-session-zero unit test as a full game setup.

    Production code has no bypass. Existing tests that exercise unrelated turn,
    proposal or replay behavior mock only the new entry gate. Tests marked
    ``session_zero_enforced`` run through the real gate and full completion flow.
    """
    if request.node.get_closest_marker("session_zero_enforced"):
        yield
        return
    with patch(
        "app.services.session_zero_service.SessionZeroService.require_completed",
        new_callable=AsyncMock,
        return_value=None,
    ):
        yield


@pytest_asyncio.fixture(scope="function")
async def test_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)

    await engine.dispose()


@pytest_asyncio.fixture(scope="function")
async def db_session(test_engine) -> AsyncIterator[AsyncSession]:
    AsyncSessionLocal = async_sessionmaker(
        bind=test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )
    async with AsyncSessionLocal() as session:
        yield session


@pytest_asyncio.fixture(scope="function")
async def client(db_session) -> AsyncIterator[TestClient]:
    async def _get_test_session():
        yield db_session

    app.dependency_overrides[get_session] = _get_test_session

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
