import asyncio
import pytest
import pytest_asyncio
from collections.abc import AsyncIterator
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker, AsyncSession
from app.db.engine import Base, get_session
from app.main import app

# Use in-memory SQLite database for testing
TEST_DATABASE_URL = "sqlite+aiosqlite:///:memory:"

@pytest.fixture(scope="session")
def event_loop():
    """Create an instance of the default event loop for each test case."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()

@pytest_asyncio.fixture(scope="function")
async def test_engine():
    engine = create_async_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False}
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
        autoflush=False
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
