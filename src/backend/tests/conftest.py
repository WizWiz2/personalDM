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
from app.services.narration_validator import NarrationValidator
from app.services.turn_planner import TurnPlan

# Use in-memory SQLite database for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(autouse=True)
def mock_turn_planner():
    """Keep unrelated endpoint tests offline while preserving planner integration."""
    plan = TurnPlan(
        player_intent="Resolve the player's latest action.",
        resolution="observation",
        observable_consequences=["The action produces one visible consequence."],
        character_beats=[],
        canon_constraints=["Do not invent abilities, items, movement, or knowledge."],
        new_fact_candidates=[],
        narration_guidance=["Keep the response grounded and concise."],
        ending_hook="Return a meaningful situation to the player.",
    )
    with patch(
        "app.services.turn_planner.TurnPlanner.plan",
        new_callable=AsyncMock,
        return_value=plan,
    ):
        yield


@pytest.fixture(autouse=True)
def deterministic_narration_validator(request):
    """Do not make deterministic endpoint tests depend on a live local control model.

    Tests explicitly marked ``narration_validator_enforced`` keep the real validator.
    Individual tests may also install a more specific patch inside this fixture; that
    nested patch temporarily overrides this default and is restored afterwards.
    """
    if request.node.get_closest_marker("narration_validator_enforced"):
        yield
        return

    result = NarrationValidationResult(
        verdict="pass",
        summary="Deterministic test harness accepted the mocked narration.",
        violations=[],
    )
    with patch.object(
        NarrationValidator,
        "validate",
        new_callable=AsyncMock,
        return_value=result,
    ):
        yield


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

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    yield engine

    # Drop tables
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
    # Override get_session dependency
    async def _get_test_session():
        yield db_session

    app.dependency_overrides[get_session] = _get_test_session

    with TestClient(app) as test_client:
        yield test_client

    app.dependency_overrides.clear()
