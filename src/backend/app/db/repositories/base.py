from sqlalchemy.ext.asyncio import AsyncSession

class BaseRepository:
    """Base class for all repository implementations, providing access to SQLAlchemy AsyncSession."""
    def __init__(self, session: AsyncSession):
        self._session = session
