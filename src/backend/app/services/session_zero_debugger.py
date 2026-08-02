from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.session_zero_service import SessionZeroService


class SessionZeroDebugger:
    def __init__(self, session: AsyncSession):
        self._session = session

    async def snapshot(self, campaign_id: UUID) -> dict:
        setup = await SessionZeroService(self._session).get(campaign_id)
        payload = setup.model_dump(mode="json")
        return {
            "session_zero": payload,
            "health": {
                "session_zero_incomplete": int(setup.status != "completed"),
                "session_zero_missing_fields": len(setup.missing_fields),
                "character_card_missing_fields": len(
                    setup.character_card_missing_fields
                ),
            },
        }
