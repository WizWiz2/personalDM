from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db.repositories.turn_repo import TurnRepository
from app.models.turn import TurnCreate
from app.providers.llm_provider import LLMProviderError
from app.services.authority_narration_pipeline import AuthorityNarrationPipeline
from app.services.narration_publication_guard import NarrationPublicationGuard
from app.services.narration_validator import NarrationValidationError
from app.services.turn_authority_validator import (
    TurnAuthorityValidator,
)
from tests.test_qa219_regressions import _world
from tests.test_qa220_regressions import _auth

pytestmark = pytest.mark.interagent_contract_enforced




BAD = {"position_claims": [{"item_id": "00000000-0000-0000-0000-000000000000", "evidence": "Осип",
                            "change": "moved", "scope": "current", "mode": "located"}]}










@pytest.mark.asyncio
async def test_provider_outage_is_not_a_schema_retry_loop():
    router = SimpleNamespace(generate_json=AsyncMock(side_effect=LLMProviderError("offline")))
    with pytest.raises(NarrationValidationError, match="offline"):
        await TurnAuthorityValidator(router).validate(SimpleNamespace(), _auth(), "Осип молчит.")
    assert router.generate_json.await_count == 1
